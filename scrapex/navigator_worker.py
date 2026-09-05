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
    )


def public_observation(observation: Observation, *, loop_warning=None, backtrack_available=False) -> dict[str, Any]:
    """Model-facing view of an Observation, bounded to what the caller needs to act."""
    return {
        "url": observation.url,
        "title": observation.title,
        "breadcrumb": list(observation.breadcrumb),
        "elements": [
            {"ref": el.ref, "role": el.role, "name": el.name, "expanded": el.expanded}
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

    def create_task(self, target: dict[str, Any], topic: str, action_budget: Optional[int] = None) -> str:
        budget = min(MAX_ACTION_BUDGET, max(1, int(action_budget or DEFAULT_ACTION_BUDGET)))
        return self.store.create_navigator_task(self.provider.slug, target, topic, budget)

    async def observe(self, task_id: str) -> dict[str, Any]:
        task = self._require_task(task_id)
        if task["state"] in TERMINAL_STATES:
            raise NavigatorTaskError("task_terminal", f"Task is already {task['state']}.")
        page = await self._page()
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
        page = await self._page()
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
        query_submitted = any(self.provider.is_search_action(s["action"]) for s in steps)
        is_procedure_leaf = bool(steps) and steps[-1]["action"].get("action") == "extract"

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
