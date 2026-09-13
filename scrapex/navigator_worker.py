"""Per-task Navigator orchestration.

Unlike ``AdasMapBatchRunner``, this is not a background loop -- Navigator
tasks are driven turn-by-turn by the caller's HTTP requests (X Omni's model
loop, one action per model turn). This class just wires the browser, the
navigation graph, the action executor, and Store persistence together for
one task at a time, and enforces the server-side action budget and terminal
states independent of whatever turn budget the caller enforces on itself.

What the runtime owns here is execution truth: which observation an action
was bound to, whether its target still existed, what was actually extracted,
what was verified, what was written to disk and with which hash. It never
decides whether a page is the procedure the caller wanted; that judgement is
the caller's, and the caller's semantic review travels into the provenance
sidecar as data.
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
    DomControl,
    Mark,
    Observation,
    ObservationNode,
    annotated_viewport_screenshot,
    build_observation,
    page_text_all_frames,
    plain_viewport_png,
    window_size,
)
from .navigator_providers import NavigatorProvider
from .navigator_verification import evaluate_navigation_claim
from .navigator_visual import FRAMES
from .storage_policy import safe_component, service_information_directory

import logging

log = logging.getLogger("scrapex.navigator_worker")

TERMINAL_STATES = frozenset({"verified", "exhausted", "failed"})
DEFAULT_ACTION_BUDGET = 50
MAX_ACTION_BUDGET = 80
# Full extracted text kept per task, for the caller's semantic review and the
# machine-readable sidecar. The observation's own page_text stays bounded.
MAX_EXTRACT_CHARS = 60_000
MAX_EXTRACT_LINKS = 80
MAX_REVIEW_JSON_CHARS = 20_000
CAPTURE_METHOD = "rendered_page_images"


class NavigatorTaskError(Exception):
    def __init__(self, code: str, message: str, detail: Optional[dict[str, Any]] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail or {}


def _control_to_dict(control: DomControl) -> dict[str, Any]:
    return asdict(control)


def _control_from_dict(raw: Any) -> Optional[DomControl]:
    if not isinstance(raw, dict):
        return None
    try:
        return DomControl(**{k: v for k, v in raw.items() if k in DomControl.__dataclass_fields__})
    except TypeError:
        return None


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
        "observation_id": observation.observation_id,
        "page_identity": observation.page_identity,
        "window_width": observation.window_width,
        "window_height": observation.window_height,
        "elements": [asdict(el) for el in observation.elements],
        "controls": [_control_to_dict(control) for control in observation.controls],
        "marks": [
            {"mark": mark.mark, "control": _control_to_dict(mark.control)}
            for mark in observation.marks
        ],
    }


def _observation_from_dict(data: Optional[dict[str, Any]]) -> Optional[Observation]:
    if not isinstance(data, dict):
        return None
    elements = [
        ObservationNode(**{k: v for k, v in el.items() if k in ObservationNode.__dataclass_fields__})
        for el in (data.get("elements") or [])
    ]
    controls = [
        control
        for control in (_control_from_dict(raw) for raw in (data.get("controls") or []))
        if control is not None
    ]
    marks: list[Mark] = []
    for raw in data.get("marks") or []:
        if not isinstance(raw, dict):
            continue
        control = _control_from_dict(raw.get("control"))
        try:
            number = int(raw.get("mark"))
        except (TypeError, ValueError):
            continue
        if control is not None:
            marks.append(Mark(mark=number, control=control))
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
        observation_id=str(data.get("observation_id") or ""),
        page_identity=str(data.get("page_identity") or ""),
        window_width=int(data.get("window_width") or 0),
        window_height=int(data.get("window_height") or 0),
        controls=controls,
        marks=marks,
    )


def public_observation(observation: Observation, *, loop_warning=None, backtrack_available=False) -> dict[str, Any]:
    """Model-facing view of an Observation, bounded to what the caller needs to reason and act.

    The accessibility element map is the action authority, while bounded page
    text and element depth give the reasoning model enough semantic/contextual
    information to understand arbitrary provider drill-downs instead of
    guessing from button labels alone. Element boxes and marks let the caller
    ground what it sees in the screenshot; the full DOM control list stays
    server-side and is only summarised as a count.
    """
    elements = []
    for el in observation.elements:
        item: dict[str, Any] = {
            "ref": el.ref,
            "role": el.role,
            "name": el.name,
            "depth": el.depth,
            "expanded": el.expanded,
        }
        if el.box is not None:
            item["box"] = [el.box["x"], el.box["y"], el.box["w"], el.box["h"]]
        elements.append(item)
    ref_boxes = [el.box for el in observation.elements if el.box is not None]
    from .navigator_observation import point_in_box

    without_refs = sum(
        1
        for control in observation.controls
        if not any(point_in_box(control.center[0], control.center[1], box) for box in ref_boxes)
    )
    view: dict[str, Any] = {
        "observation_id": observation.observation_id,
        "page_identity": observation.page_identity,
        "url": observation.url,
        "title": observation.title,
        "breadcrumb": list(observation.breadcrumb),
        "viewport": {"width": observation.window_width, "height": observation.window_height},
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
        "elements": elements,
        "controls_without_refs": without_refs,
        "loop_warning": loop_warning,
        "backtrack_available": backtrack_available,
    }
    if observation.marks:
        view["marks"] = [
            {
                "mark": mark.mark,
                "tag": mark.control.tag,
                "name": mark.control.name,
                "classes": mark.control.classes,
                "box": [mark.control.x, mark.control.y, mark.control.w, mark.control.h],
            }
            for mark in observation.marks
        ]
    return view


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

    async def observe(self, task_id: str, *, marks: bool = False) -> dict[str, Any]:
        task = self._require_task(task_id)
        if task["state"] in TERMINAL_STATES:
            raise NavigatorTaskError("task_terminal", f"Task is already {task['state']}.")
        page = await self._authenticated_page()
        observation = await build_observation(
            page, breadcrumb=task.get("target", {}).get("breadcrumb"), marks=marks
        )
        graph = NavigationGraph.from_dict(task["graph"])
        step = graph.record(observation, action=None)
        self.store.cache_navigator_observation(task_id, _observation_to_dict(observation))
        self.store.set_navigator_task_state(task_id, "active", graph=graph.to_dict())
        return public_observation(
            observation, loop_warning=step.loop_warning, backtrack_available=step.backtrack_available
        )

    async def screenshot(
        self, task_id: str, *, observation_id: Optional[str] = None
    ) -> tuple[bytes, str]:
        """Return a task-bound annotated still of the current browser viewport
        and the id of the observation it belongs to.

        The cached observation supplies the only refs and marks that may be
        drawn. The unannotated frame is held in memory for the task so a later
        coordinate click can be checked against exactly what was shown; raw
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
        wanted = str(observation_id or "").strip()
        if wanted and observation.observation_id and wanted != observation.observation_id:
            raise NavigatorTaskError(
                "stale_observation",
                f"'{wanted}' is not the most recent observation "
                f"({observation.observation_id}); observe again before requesting a screenshot.",
            )
        page = await self._page()
        if observation.observation_id:
            try:
                plain = await plain_viewport_png(page)
                width, height = await window_size(page)
                FRAMES.put(task_id, observation.observation_id, plain, width, height)
            except Exception:  # noqa: BLE001 - the annotated still is still useful
                log.warning("navigator plain frame unavailable for %s", task_id, exc_info=True)
        jpeg = await annotated_viewport_screenshot(page, observation)
        return jpeg, observation.observation_id

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
            result = await self.executor.execute(
                page, last_observation, action, visual_frame=FRAMES.get(task_id)
            )
        except ActionError as exc:
            raise NavigatorTaskError(exc.code, exc.message, exc.detail) from exc

        extract_record: Optional[dict[str, Any]] = None
        if str(action.get("action") or "") == "extract":
            extract_record = await self._record_extract(page, last_observation)
            self.store.save_navigator_extract(task_id, extract_record)

        new_observation = await build_observation(page)
        graph = NavigationGraph.from_dict(task["graph"])
        step = graph.record(new_observation, action=dict(action))
        self.store.append_navigator_step(task_id, dict(action), _observation_to_dict(new_observation))

        new_step_count = task["step_count"] + 1
        if new_step_count >= task["action_budget"]:
            self.store.set_navigator_task_state(task_id, "exhausted", graph=graph.to_dict(), last_error="Action budget exhausted.")
        else:
            self.store.set_navigator_task_state(task_id, "active", graph=graph.to_dict())

        response = {
            **public_observation(
                new_observation, loop_warning=step.loop_warning, backtrack_available=step.backtrack_available
            ),
            "action_executed": result.executed,
            "is_search_action": result.is_search_action,
            "repeated_action_warning": step.repeated_action_warning,
        }
        if result.target:
            response["action_target"] = result.target
        if result.detail:
            response["action_detail"] = result.detail
        if extract_record is not None:
            response["extract"] = {
                "title": extract_record.get("title"),
                "url": extract_record.get("url"),
                "chars": extract_record.get("chars"),
                "sha256": extract_record.get("sha256"),
            }
        return response

    async def _record_extract(self, page: Any, observation: Optional[Observation]) -> dict[str, Any]:
        """Everything the page said at the moment it was marked as evidence."""
        try:
            raw_text = await page_text_all_frames(page)
        except Exception:
            raw_text = ""
        text = " ".join(str(raw_text or "").split())
        truncated = len(text) > MAX_EXTRACT_CHARS
        text = text[:MAX_EXTRACT_CHARS]
        try:
            title = await page.title()
        except Exception:
            title = observation.title if observation is not None else ""
        links = []
        if observation is not None:
            for element in observation.elements:
                if element.role == "link" and element.name and not element.name.startswith("unlabeled "):
                    if element.name not in links:
                        links.append(element.name[:120])
                if len(links) >= MAX_EXTRACT_LINKS:
                    break
        return {
            "url": str(getattr(page, "url", "") or ""),
            "title": title,
            "text": text,
            "chars": len(text),
            "truncated": truncated,
            "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest() if text else None,
            "links": links,
            "observation_id": observation.observation_id if observation is not None else "",
            "breadcrumb": list(observation.breadcrumb) if observation is not None else [],
            "extracted_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
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
            or str(step["action"].get("action") or "")
            in {"click", "open", "click_mark", "click_visual", "select_vehicle"}
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

        extract = task.get("extract") if isinstance(task.get("extract"), dict) else {}
        evidence_text = str(extract.get("text") or "") if extract.get("url") == observation.url else ""
        if not evidence_text:
            evidence_text = observation.page_text
        matched_terms, relevance_score = self.provider.match_terms(evidence_text, task["topic"])

        proof = evaluate_navigation_claim(
            target=task["target"],
            target_state=target_state,
            query_submitted=query_submitted,
            matched_terms=matched_terms,
            relevance_score=relevance_score,
            is_procedure_leaf=is_procedure_leaf,
            extracted_text=evidence_text,
            source_url=observation.url,
            provider=self.provider.slug,
        )
        proof["title"] = observation.title
        proof["observation_id"] = observation.observation_id
        self.store.save_navigator_verification(task_id, proof)
        return proof

    @staticmethod
    def _jpeg_size(data: bytes) -> tuple[int, int]:
        """Width and height from a JPEG's frame header."""
        index = 2
        while index < len(data) - 9:
            if data[index] != 0xFF:
                index += 1
                continue
            marker = data[index + 1]
            if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
                index += 2
                continue
            length = int.from_bytes(data[index + 2:index + 4], "big")
            if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                          0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                height = int.from_bytes(data[index + 5:index + 7], "big")
                width = int.from_bytes(data[index + 7:index + 9], "big")
                return width, height
            index += 2 + length
        raise ValueError("not a usable JPEG")

    @classmethod
    def _jpegs_to_pdf(cls, frames: list[bytes]) -> bytes:
        """One page per frame, each JPEG embedded without re-encoding.

        Deliberately dependency-free: a JPEG is already a valid PDF image
        stream under DCTDecode, so the bytes go in untouched and ScrapeX gains
        no imaging library it would otherwise not need.
        """
        objects: list[bytes] = []

        def add(body: bytes) -> int:
            objects.append(body)
            return len(objects)

        catalog_id = add(b"")   # reserved, filled once the page tree exists
        pages_id = add(b"")
        page_ids: list[int] = []

        for frame in frames:
            width, height = cls._jpeg_size(frame)
            image_id = add(
                b"<< /Type /XObject /Subtype /Image /Width " + str(width).encode()
                + b" /Height " + str(height).encode()
                + b" /ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /DCTDecode /Length "
                + str(len(frame)).encode() + b" >>\nstream\n" + frame + b"\nendstream"
            )
            content = (
                f"q {width} 0 0 {height} 0 0 cm /Im0 Do Q".encode()
            )
            content_id = add(
                b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n"
                + content + b"\nendstream"
            )
            page_ids.append(add(
                b"<< /Type /Page /Parent " + str(pages_id).encode()
                + b" 0 R /MediaBox [0 0 " + str(width).encode() + b" " + str(height).encode()
                + b"] /Resources << /XObject << /Im0 " + str(image_id).encode()
                + b" 0 R >> >> /Contents " + str(content_id).encode() + b" 0 R >>"
            ))

        kids = b" ".join(str(pid).encode() + b" 0 R" for pid in page_ids)
        objects[pages_id - 1] = (
            b"<< /Type /Pages /Count " + str(len(page_ids)).encode()
            + b" /Kids [" + kids + b"] >>"
        )
        objects[catalog_id - 1] = b"<< /Type /Catalog /Pages " + str(pages_id).encode() + b" 0 R >>"

        out = bytearray(b"%PDF-1.4\n")
        offsets = [0]
        for number, body in enumerate(objects, start=1):
            offsets.append(len(out))
            out += str(number).encode() + b" 0 obj\n" + body + b"\nendobj\n"
        xref_at = len(out)
        out += b"xref\n0 " + str(len(objects) + 1).encode() + b"\n"
        out += b"0000000000 65535 f \n"
        for offset in offsets[1:]:
            out += f"{offset:010d} 00000 n \n".encode()
        out += (
            b"trailer\n<< /Size " + str(len(objects) + 1).encode()
            + b" /Root " + str(catalog_id).encode() + b" 0 R >>\nstartxref\n"
            + str(xref_at).encode() + b"\n%%EOF\n"
        )
        return bytes(out)

    async def _render_by_screenshot(self, page: Any) -> bytes:
        """Capture the article as it renders on screen, page by page.

        ALLDATA's print stylesheet does not render its articles under print
        media: a direct page.pdf() returns the site's own "use the print icon"
        notice followed by blank sheets -- 11 pages, 46,510 bytes, no text past
        page one, identical on every attempt whatever is preloaded or clicked
        beforehand. Its print control ends in window.print(), which in a
        headless browser opens a dialog nothing can dismiss and hangs the page.

        On screen the same article renders perfectly, diagrams included, so the
        screen is the source. The result is a page-image PDF -- the same kind
        of artifact this shop already files by hand -- and the machine-readable
        text goes beside it, so nothing has to be read back off the pixels.
        """
        scroller_top = "() => { const d=document.scrollingElement||document.documentElement; d.scrollTop=0; }"
        try:
            await page.evaluate(scroller_top)
        except Exception:
            pass
        await page.wait_for_timeout(600)

        frames: list[bytes] = []
        seen: set[int] = set()
        step = 620
        for _ in range(40):
            shot = await page.screenshot(type="jpeg", quality=80)
            digest = hash(shot)
            if digest not in seen:
                seen.add(digest)
                frames.append(shot)
            try:
                at_bottom = await page.evaluate(
                    """(step) => {
                        const pick = () => {
                            const de = document.documentElement;
                            let best = de, area = -1;
                            for (const el of [de, document.body, ...document.querySelectorAll('*')]) {
                                if (!el) continue;
                                const sh = el.scrollHeight||0, ch = el.clientHeight||0;
                                if (ch <= 0 || sh - ch <= 8) continue;
                                if (el !== de && el !== document.body) {
                                    const oy = getComputedStyle(el).overflowY;
                                    if (oy!=='auto' && oy!=='scroll' && oy!=='overlay') continue;
                                }
                                const a = ch * (el.clientWidth||0);
                                if (a > area) { area = a; best = el; }
                            }
                            return best;
                        };
                        const s = pick();
                        s.scrollTop += step;
                        return s.scrollTop + s.clientHeight >= s.scrollHeight - 2;
                    }""",
                    step,
                )
            except Exception:
                break
            await page.wait_for_timeout(700)
            if at_bottom:
                shot = await page.screenshot(type="jpeg", quality=80)
                if hash(shot) not in seen:
                    frames.append(shot)
                break

        if not frames:
            raise NavigatorTaskError(
                "capture_failed", "No page frames could be captured for this article."
            )
        return self._jpegs_to_pdf(frames)

    def _display_title(self, title: Any) -> str:
        cleaner = getattr(self.provider, "display_title", None)
        text = " ".join(str(title or "").split())
        if cleaner is None:
            return text
        try:
            return " ".join(str(cleaner(text) or "").split()) or text
        except Exception:
            return text

    @staticmethod
    def _bounded_review(review: Any) -> Optional[dict[str, Any]]:
        """The caller's semantic review, kept as data and bounded in size."""
        if not isinstance(review, dict) or not review:
            return None
        encoded = json.dumps(review, default=str)
        if len(encoded) > MAX_REVIEW_JSON_CHARS:
            raise NavigatorTaskError(
                "invalid_arguments",
                f"semantic_review must encode to at most {MAX_REVIEW_JSON_CHARS} characters.",
            )
        return json.loads(encoded)

    async def capture(
        self,
        task_id: str,
        *,
        semantic_review: Any = None,
        objective: Any = None,
    ) -> dict[str, Any]:
        """Persist the verified leaf from this exact Navigator browser session.

        Service-information storage is always Year/Make/Model. A capture can
        only occur after the canonical verification proof succeeded, and the
        live page URL must still equal the verified source URL. Whatever the
        caller decided about the page semantically rides along as data in the
        provenance sidecar; nothing here re-decides it.
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
        review = self._bounded_review(semantic_review)
        objective_record = (
            json.loads(json.dumps(objective, default=str))
            if isinstance(objective, dict) and objective
            else None
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
                        "sha256": existing.get("saved_pdf_sha256"),
                        "title": existing.get("title"),
                        "storage_policy": "year/make/model",
                        "capture_method": existing.get("capture_method"),
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
        # one of them. The provider's own print control ends in window.print(),
        # which no driven browser can complete, so the rendered screen is the
        # artifact and the extracted text is filed beside it.
        try:
            pdf_bytes = await self._render_by_screenshot(page)
        except NavigatorTaskError:
            raise
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
        extract = task.get("extract") if isinstance(task.get("extract"), dict) else {}
        extracted_text = (
            str(extract.get("text") or "")
            if str(extract.get("url") or "").strip() == source_url
            else str(observation.get("page_text") or "")
        )
        page_title = self._display_title(observation.get("title"))
        topic = safe_component(task.get("topic"), "Service Information", maximum=120)
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        name_root = safe_component(page_title, "", maximum=120) or topic
        base = safe_component(
            f"{name_root} {self.provider.slug.upper()} {stamp}",
            f"Service Information {stamp}",
            maximum=150,
        )
        pdf_path = folder / f"{base}.pdf"
        sidecar = folder / f"{base}.source.json"
        text_path = folder / f"{base}.text.txt"
        index = 2
        while pdf_path.exists() or sidecar.exists() or text_path.exists():
            pdf_path = folder / f"{base} ({index}).pdf"
            sidecar = folder / f"{base} ({index}).source.json"
            text_path = folder / f"{base} ({index}).text.txt"
            index += 1

        pdf_path.write_bytes(pdf_bytes)
        text_sha256 = (
            hashlib.sha256(extracted_text.encode("utf-8")).hexdigest() if extracted_text else None
        )
        if extracted_text:
            text_path.write_text(extracted_text, encoding="utf-8")
        retrieved_at = datetime.now(UTC).replace(microsecond=0).isoformat()
        provenance = {
            "provider": self.provider.slug,
            "artifact_kind": "service_information",
            "storage_policy": "year/make/model",
            "task_id": task_id,
            "source_url": source_url,
            "title": page_title or observation.get("title"),
            "page_title": observation.get("title"),
            "topic": task.get("topic"),
            "vehicle": target,
            "breadcrumb": list(observation.get("breadcrumb") or []),
            "observation_id": observation.get("observation_id"),
            "retrieved_at": retrieved_at,
            "capture_method": CAPTURE_METHOD,
            "provider_export": {
                "attempted": False,
                "reason": (
                    "The provider's print control ends in window.print(), which a driven "
                    "browser cannot complete; the rendered page is captured instead."
                ),
            },
            "saved_pdf_sha256": digest,
            "extracted_text_path": text_path.name if extracted_text else None,
            "extracted_text_sha256": text_sha256,
            "extracted_text_chars": len(extracted_text),
            "verification": proof,
            "semantic_review": review,
            "objective": objective_record,
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
            "text_sidecar": (
                str(text_path.relative_to(self.adas_si_root)).replace("\\", "/")
                if extracted_text
                else None
            ),
            "source_url": source_url,
            "sha256": digest,
            "extracted_text_sha256": text_sha256,
            "title": page_title or observation.get("title"),
            "capture_method": CAPTURE_METHOD,
            "storage_policy": "year/make/model",
        }

    def evidence(self, task_id: str) -> dict[str, Any]:
        task = self._require_task(task_id)
        observation = task.get("last_observation") or {}
        extract = task.get("extract") if isinstance(task.get("extract"), dict) else {}
        current_url = observation.get("url")
        extract_matches = bool(extract) and str(extract.get("url") or "") == str(current_url or "")
        return {
            "task_id": task_id,
            "provider": task["provider"],
            "target": task["target"],
            "topic": task["topic"],
            "source_url": current_url,
            "title": observation.get("title"),
            "observation_id": observation.get("observation_id"),
            "breadcrumb": list(observation.get("breadcrumb") or []),
            "extracted_text": (
                extract.get("text") if extract_matches else observation.get("page_text")
            ),
            "extracted_text_chars": (
                extract.get("chars") if extract_matches else len(str(observation.get("page_text") or ""))
            ),
            "extracted_text_truncated": bool(extract.get("truncated")) if extract_matches else None,
            "extracted_text_sha256": extract.get("sha256") if extract_matches else None,
            "extracted_at": extract.get("extracted_at") if extract_matches else None,
            "referenced_links": list(extract.get("links") or []) if extract_matches else [],
            "verification": task.get("verification"),
            "verified": bool(task.get("verified")),
        }
