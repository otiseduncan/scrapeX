"""Per-task Navigator orchestration.

Unlike ``AdasMapBatchRunner``, this is not a background loop -- Navigator
tasks are driven turn-by-turn by the caller's HTTP requests (X Omni's model
loop, one action per model turn). This class just wires the browser, the
navigation graph, the action executor, and Store persistence together for
one task at a time, and enforces the server-side action budget and terminal
states independent of whatever turn budget the caller enforces on itself.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Optional

from .navigator_actions import ActionError, NavigatorActionExecutor
from .navigator_graph import NavigationGraph
from .navigator_observation import (
    Observation,
    ObservationNode,
    annotated_viewport_screenshot,
    build_observation,
)
from .navigator_providers import NavigatorProvider
from .navigator_verification import evaluate_navigation_claim
from .storage_policy import safe_component, service_information_directory

import logging

log = logging.getLogger("scrapex.navigator_worker")

TERMINAL_STATES = frozenset({"verified", "exhausted", "failed"})
DEFAULT_ACTION_BUDGET = 50
MAX_ACTION_BUDGET = 80


class NavigatorTaskError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _observation_to_dict(observation: Observation) -> dict[str, Any]:
    return {
        "url": observation.url,
        "title": observation.title,
        "breadcrumb": list(observation.breadcrumb),
        "page_text": observation.page_text,
        "page_text_truncated": observation.page_text_truncated,
        "page_text_total_chars": observation.page_text_total_chars,
        "scroll_y": observation.scroll_y,
        "scroll_height": observation.scroll_height,
        "viewport_height": observation.viewport_height,
        "elements": [asdict(el) for el in observation.elements],
    }


def _observation_from_dict(data: Optional[dict[str, Any]]) -> Optional[Observation]:
    if not isinstance(data, dict):
        return None
    elements = [
        ObservationNode(**{k: v for k, v in el.items() if k in ObservationNode.__dataclass_fields__})
        for el in (data.get("elements") or [])
    ]
    return Observation(
        url=data.get("url") or "",
        title=data.get("title") or "",
        elements=elements,
        page_text=data.get("page_text") or "",
        breadcrumb=list(data.get("breadcrumb") or []),
        page_text_truncated=bool(data.get("page_text_truncated") or False),
        page_text_total_chars=int(data.get("page_text_total_chars") or 0),
        scroll_y=int(data.get("scroll_y") or 0),
        scroll_height=int(data.get("scroll_height") or 0),
        viewport_height=int(data.get("viewport_height") or 0),
    )


def public_observation(observation: Observation, *, loop_warning=None, backtrack_available=False) -> dict[str, Any]:
    """Model-facing view of an Observation, bounded to what the caller needs to reason and act.

    The accessibility element map is the action authority, while bounded page
    text and element depth give the reasoning model enough semantic/contextual
    information to understand arbitrary provider drill-downs instead of
    guessing from button labels alone.
    """
    return {
        "url": observation.url,
        "title": observation.title,
        "breadcrumb": list(observation.breadcrumb),
        "page_text": observation.page_text,
        # Say plainly when the page holds more than was handed over, so a
        # cut-off procedure cannot read as a finished one.
        "page_text_truncated": observation.page_text_truncated,
        "page_text_total_chars": observation.page_text_total_chars,
        "scroll_position": {
            "scroll_y": observation.scroll_y,
            "scroll_height": observation.scroll_height,
            "viewport_height": observation.viewport_height,
            "at_page_bottom": observation.at_page_bottom,
        },
        "elements": [
            {
                "ref": el.ref,
                "role": el.role,
                "name": el.name,
                "depth": el.depth,
                "expanded": el.expanded,
            }
            for el in observation.elements
        ],
        "loop_warning": loop_warning,
        "backtrack_available": backtrack_available,
    }


class NavigatorTaskRunner:
    def __init__(
        self,
        store: Any,
        browser_manager: Any,
        provider: NavigatorProvider,
        *,
        adas_si_root: Path | None = None,
    ):
        self.store = store
        self.browser_manager = browser_manager
        self.provider = provider
        self.adas_si_root = (
            Path(adas_si_root).resolve() if adas_si_root is not None else None
        )
        self.executor = NavigatorActionExecutor(provider)

    def _require_task(self, task_id: str) -> dict[str, Any]:
        task = self.store.navigator_task(task_id)
        if task is None:
            raise NavigatorTaskError("not_found", f"No navigator task: {task_id}")
        return task

    async def _page(self) -> Any:
        return await self.browser_manager.page_for(self.provider.slug, home_url=self.provider.home_url)

    async def _authenticated_page(self) -> Any:
        """Return a page only once the provider session is actually signed in.

        An unauthenticated provider page is a login screen, and a login screen
        is not evidence: handed to the model as an ordinary observation it
        reads as "the procedure is not here", which is how a licensed source
        silently turns into a false negative. Fail closed instead, and let the
        provider sign itself in from the saved credential when it can.

        The error surfaces as HTTP 409 authentication_required (unmapped codes
        default to 409), which is what the caller distinguishes an
        authentication blocker by. No credential material is included.
        """
        page = await self._page()
        ensure = getattr(self.provider, "ensure_authenticated", None)
        if ensure is None:
            return page
        result = await ensure(page)
        if result.get("authenticated"):
            return page
        raise NavigatorTaskError(
            "authentication_required",
            f"{self.provider.slug} requires interactive authentication before "
            f"the Navigator can observe or act. "
            f"{result.get('reason') or ''}".strip(),
        )

    def create_task(self, target: dict[str, Any], topic: str, action_budget: Optional[int] = None) -> str:
        budget = min(MAX_ACTION_BUDGET, max(1, int(action_budget or DEFAULT_ACTION_BUDGET)))
        return self.store.create_navigator_task(self.provider.slug, target, topic, budget)

    async def observe(self, task_id: str) -> dict[str, Any]:
        task = self._require_task(task_id)
        if task["state"] in TERMINAL_STATES:
            raise NavigatorTaskError("task_terminal", f"Task is already {task['state']}.")
        page = await self._authenticated_page()
        observation = await build_observation(page, breadcrumb=task.get("target", {}).get("breadcrumb"))
        graph = NavigationGraph.from_dict(task["graph"])
        step = graph.record(observation, action=None)
        self.store.cache_navigator_observation(task_id, _observation_to_dict(observation))
        self.store.set_navigator_task_state(task_id, "active", graph=graph.to_dict())
        return public_observation(
            observation, loop_warning=step.loop_warning, backtrack_available=step.backtrack_available
        )

    async def screenshot(self, task_id: str) -> bytes:
        """Return a task-bound annotated still of the current browser viewport.

        The cached observation supplies the only refs that may be drawn. Raw
        pixels are not persisted in Navigator state and this method exposes no
        cookies, credentials, or browser-profile material.
        """
        task = self._require_task(task_id)
        observation = _observation_from_dict(task.get("last_observation"))
        if observation is None:
            raise NavigatorTaskError(
                "no_prior_observation",
                "Observe the Navigator task before requesting its screenshot.",
            )
        page = await self._page()
        return await annotated_viewport_screenshot(page, observation)

    async def act(self, task_id: str, action: dict[str, Any]) -> dict[str, Any]:
        task = self._require_task(task_id)
        if task["state"] in TERMINAL_STATES:
            raise NavigatorTaskError("task_terminal", f"Task is already {task['state']}.")
        if task["step_count"] >= task["action_budget"]:
            self.store.set_navigator_task_state(task_id, "exhausted", last_error="Action budget exhausted.")
            raise NavigatorTaskError("action_budget_exhausted", "This task's action budget is exhausted.")

        last_observation = _observation_from_dict(task.get("last_observation"))
        page = await self._authenticated_page()
        try:
            result = await self.executor.execute(page, last_observation, action)
        except ActionError as exc:
            raise NavigatorTaskError(exc.code, exc.message) from exc

        new_observation = await build_observation(page)
        graph = NavigationGraph.from_dict(task["graph"])
        step = graph.record(new_observation, action=dict(action))
        self.store.append_navigator_step(task_id, dict(action), _observation_to_dict(new_observation))

        new_step_count = task["step_count"] + 1
        if new_step_count >= task["action_budget"]:
            self.store.set_navigator_task_state(task_id, "exhausted", graph=graph.to_dict(), last_error="Action budget exhausted.")
        else:
            self.store.set_navigator_task_state(task_id, "active", graph=graph.to_dict())

        return {
            **public_observation(
                new_observation, loop_warning=step.loop_warning, backtrack_available=step.backtrack_available
            ),
            "action_executed": result.executed,
            "is_search_action": result.is_search_action,
            "repeated_action_warning": step.repeated_action_warning,
        }

    async def verify(self, task_id: str) -> dict[str, Any]:
        task = self._require_task(task_id)
        observation = _observation_from_dict(task.get("last_observation"))
        if observation is None:
            proof = evaluate_navigation_claim(
                target=task["target"], target_state={"selected": False, "reason": "No observation exists yet."},
                query_submitted=False, matched_terms=None, relevance_score=0,
                is_procedure_leaf=False, extracted_text=None, source_url="", provider=self.provider.slug,
            )
            self.store.save_navigator_verification(task_id, proof)
            return proof

        page = await self._page()
        target_state = await self.provider.target_signal(page, task["target"])

        steps = self.store.navigator_task_steps(task_id)
        # Dynamic service-information sites are often reached entirely through
        # menu/tree navigation. Requiring a typed search made valid ALLDATA
        # drill-downs unverifiable, so any real target-scoped navigation
        # action counts while passive observe/wait/scroll/extract/done do not.
        query_submitted = any(
            self.provider.is_search_action(step["action"])
            or str(step["action"].get("action") or "") in {"click", "open"}
            for step in steps
        )

        # Extract marks the candidate evidence leaf. A subsequent done action
        # is a control-loop signal, not browser navigation, and must not erase
        # that evidence marker before verification.
        substantive_actions = [
            str(step["action"].get("action") or "")
            for step in steps
            if str(step["action"].get("action") or "") not in {"done", "wait"}
        ]
        is_procedure_leaf = bool(substantive_actions) and substantive_actions[-1] == "extract"

        matched_terms, relevance_score = self.provider.match_terms(observation.page_text, task["topic"])

        proof = evaluate_navigation_claim(
            target=task["target"],
            target_state=target_state,
            query_submitted=query_submitted,
            matched_terms=matched_terms,
            relevance_score=relevance_score,
            is_procedure_leaf=is_procedure_leaf,
            extracted_text=observation.page_text,
            source_url=observation.url,
            provider=self.provider.slug,
        )
        self.store.save_navigator_verification(task_id, proof)
        return proof

    async def _preload_images(self, page: Any) -> None:
        """Scroll the whole document and wait for every image to finish loading.

        Runs in the frame that holds the content, not the top page. ALLDATA
        renders its articles inside an iframe, so evaluating against the outer
        document reaches a shell with no figures in it at all -- the first
        attempt at this changed the captured PDF by exactly zero bytes.
        """
        target = page
        try:
            best_length = -1
            for frame in list(getattr(page, "frames", []) or []) or [page]:
                try:
                    text = await frame.inner_text("body")
                except Exception:
                    continue
                if len(text or "") > best_length:
                    best_length = len(text or "")
                    target = frame
        except Exception:
            target = page
        # Diagnostic: which frames exist and what they hold. Written to a file
        # because the service's logging config does not surface warnings here.
        try:
            import json as _json
            from pathlib import Path as _Path
            rows = []
            for fr in list(getattr(page, "frames", []) or []):
                try:
                    body = await fr.inner_text("body")
                except Exception:
                    body = ""
                try:
                    imgs = await fr.evaluate("() => document.images.length")
                except Exception:
                    imgs = -1
                rows.append({"url": str(getattr(fr, "url", ""))[:200],
                             "text": len(body or ""), "images": imgs})
            _Path(r"X:\ScrapeX\data\capture_frames.json").write_text(
                _json.dumps(rows, indent=2), encoding="utf-8")
        except Exception:
            pass

        report = await target.evaluate(
            """async () => {
                const sleep = (ms) => new Promise(r => setTimeout(r, ms));
                const scroller = (() => {
                    const de = document.documentElement;
                    const all = [de, document.body, ...document.querySelectorAll('*')];
                    let best = de, bestArea = -1;
                    for (const el of all) {
                        if (!el) continue;
                        const sh = el.scrollHeight || 0, ch = el.clientHeight || 0;
                        if (ch <= 0 || sh - ch <= 8) continue;
                        if (el !== de && el !== document.body) {
                            const oy = getComputedStyle(el).overflowY;
                            if (oy !== 'auto' && oy !== 'scroll' && oy !== 'overlay') continue;
                        }
                        const area = ch * (el.clientWidth || 0);
                        if (area > bestArea) { bestArea = area; best = el; }
                    }
                    return best;
                })();

                for (const img of document.images) {
                    img.loading = 'eager';
                    if (img.decoding) img.decoding = 'sync';
                }

                const step = Math.max(200, (scroller.clientHeight || 720) - 80);
                const limit = (scroller.scrollHeight || 0) + step * 2;
                for (let y = 0; y <= limit; y += step) {
                    scroller.scrollTop = y;
                    await sleep(160);
                    if (scroller.scrollTop + scroller.clientHeight >= scroller.scrollHeight - 2) {
                        await sleep(240);
                        if (scroller.scrollTop + scroller.clientHeight >= scroller.scrollHeight - 2) break;
                    }
                }
                scroller.scrollTop = 0;
                await sleep(200);

                await Promise.all(Array.from(document.images).map(img => {
                    if (img.complete && img.naturalWidth > 0) return Promise.resolve();
                    return new Promise(resolve => {
                        const done = () => resolve();
                        img.addEventListener('load', done, {once: true});
                        img.addEventListener('error', done, {once: true});
                        setTimeout(done, 8000);
                    });
                }));

                const imgs = Array.from(document.images);
                return {
                    url: location.href.slice(0, 120),
                    images: imgs.length,
                    loaded: imgs.filter(i => i.complete && i.naturalWidth > 0).length,
                    broken: imgs.filter(i => i.complete && i.naturalWidth === 0).length,
                    scrollHeight: scroller.scrollHeight,
                    sample: imgs.slice(0, 3).map(i => ({
                        w: i.naturalWidth, h: i.naturalHeight,
                        src: (i.currentSrc || i.src || '').slice(0, 90),
                    })),
                };
            }"""
        )
        log.warning("navigator capture preload: %s", report)
        # Decoding can trail the load event; give the renderer a moment.
        await page.wait_for_timeout(1500)

    async def capture(self, task_id: str) -> dict[str, Any]:
        """Persist the verified leaf from this exact Navigator browser session.

        Service-information storage is always Year/Make/Model. A capture can
        only occur after the canonical verification proof succeeded, and the
        live page URL must still equal the verified source URL.
        """
        task = self._require_task(task_id)
        proof = (
            task.get("verification")
            if isinstance(task.get("verification"), dict)
            else {}
        )
        if not task.get("verified") or proof.get("verified") is not True:
            raise NavigatorTaskError(
                "evidence_not_verified",
                "Navigator evidence must verify before it can be preserved in ADAS SI.",
            )
        if self.adas_si_root is None:
            raise NavigatorTaskError(
                "adas_si_unavailable",
                "ADAS SI storage is not configured for this Navigator.",
            )
        target = task.get("target") if isinstance(task.get("target"), dict) else {}
        try:
            folder = service_information_directory(self.adas_si_root, target)
        except ValueError as exc:
            raise NavigatorTaskError("invalid_target", str(exc)) from exc

        observation = (
            task.get("last_observation")
            if isinstance(task.get("last_observation"), dict)
            else {}
        )
        source_url = str(observation.get("url") or "").strip()
        verified_url = str(proof.get("source_url") or "").strip()
        if not source_url or source_url != verified_url:
            raise NavigatorTaskError(
                "verified_page_changed",
                "The live Navigator page no longer matches the verified evidence source.",
            )

        folder.mkdir(parents=True, exist_ok=True)
        for sidecar in folder.glob("*.source.json"):
            try:
                existing = json.loads(sidecar.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, ValueError):
                continue
            if str(existing.get("source_url") or "").strip() == source_url:
                pdf_name = sidecar.name.removesuffix(".source.json") + ".pdf"
                pdf_path = sidecar.with_name(pdf_name)
                if pdf_path.is_file():
                    return {
                        "status": "success",
                        "saved": False,
                        "already_present": True,
                        "task_id": task_id,
                        "provider": task["provider"],
                        "relative_path": str(
                            pdf_path.relative_to(self.adas_si_root)
                        ).replace("\\", "/"),
                        "source_sidecar": str(
                            sidecar.relative_to(self.adas_si_root)
                        ).replace("\\", "/"),
                        "source_url": source_url,
                        "storage_policy": "year/make/model",
                    }

        page = await self._page()
        if str(page.url or "").strip() != source_url:
            raise NavigatorTaskError(
                "verified_page_changed",
                "The provider browser changed after verification; capture was refused.",
            )
        # ALLDATA states the problem itself, in the printed output: "When using
        # the browser's Print Button, images don't preload when first attempting
        # to print." A procedure's diagrams -- target layouts, reflector
        # positions, tool identification -- are the part a technician cannot
        # work without, and a PDF rendered before they load silently omits every
        # one of them. Confirmed on 2026-09-12: the Honda Civic millimeter wave
        # radar aiming procedure captured as 11 pages and 46KB with no figures,
        # against 1.3MB for the same document printed from a browser that had
        # them loaded.
        #
        # So walk the document to trigger whatever lazy-loads, then wait for
        # every image to finish decoding before rendering.
        try:
            await self._preload_images(page)
        except Exception:
            # Never fail a capture because preloading was imperfect; a PDF with
            # some images missing still beats no PDF at all.
            pass
        try:
            pdf_bytes = await page.pdf(
                format="Letter",
                print_background=True,
                prefer_css_page_size=True,
            )
        except Exception as exc:
            raise NavigatorTaskError(
                "capture_failed",
                f"Provider page could not be rendered as PDF: {type(exc).__name__}.",
            ) from exc
        if not pdf_bytes.startswith(b"%PDF") or len(pdf_bytes) < 1000:
            raise NavigatorTaskError(
                "capture_failed",
                "Provider returned an invalid or empty PDF capture.",
            )

        digest = hashlib.sha256(pdf_bytes).hexdigest()
        topic = safe_component(task.get("topic"), "Service Information", maximum=120)
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        base = safe_component(
            f"{topic} {self.provider.slug.upper()} {stamp}",
            f"Service Information {stamp}",
            maximum=150,
        )
        pdf_path = folder / f"{base}.pdf"
        sidecar = folder / f"{base}.source.json"
        index = 2
        while pdf_path.exists() or sidecar.exists():
            pdf_path = folder / f"{base} ({index}).pdf"
            sidecar = folder / f"{base} ({index}).source.json"
            index += 1

        pdf_path.write_bytes(pdf_bytes)
        provenance = {
            "provider": self.provider.slug,
            "artifact_kind": "service_information",
            "storage_policy": "year/make/model",
            "task_id": task_id,
            "source_url": source_url,
            "title": observation.get("title"),
            "topic": task.get("topic"),
            "vehicle": target,
            "retrieved_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
            "saved_pdf_sha256": digest,
            "verification": proof,
            "licensed_access": True,
            "credential_secret_stored_in_document": False,
        }
        sidecar.write_text(
            json.dumps(
                provenance,
                indent=2,
                ensure_ascii=False,
                default=str,
            ),
            encoding="utf-8",
        )
        return {
            "status": "success",
            "saved": True,
            "already_present": False,
            "task_id": task_id,
            "provider": task["provider"],
            "relative_path": str(
                pdf_path.relative_to(self.adas_si_root)
            ).replace("\\", "/"),
            "source_sidecar": str(
                sidecar.relative_to(self.adas_si_root)
            ).replace("\\", "/"),
            "source_url": source_url,
            "sha256": digest,
            "storage_policy": "year/make/model",
        }

    def evidence(self, task_id: str) -> dict[str, Any]:
        task = self._require_task(task_id)
        return {
            "task_id": task_id,
            "provider": task["provider"],
            "target": task["target"],
            "topic": task["topic"],
            "source_url": (task.get("last_observation") or {}).get("url"),
            "extracted_text": (task.get("last_observation") or {}).get("page_text"),
            "verification": task.get("verification"),
            "verified": bool(task.get("verified")),
        }
