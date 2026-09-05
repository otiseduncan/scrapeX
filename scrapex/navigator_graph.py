"""Navigation graph: state fingerprinting, backtrack chain, loop detection.

Pure bookkeeping -- no I/O, no Playwright. This is what lets X Omni's model
say "explore another branch" without needing to remember the whole path
itself: ScrapeX tracks what's been visited and what got there, and tells the
caller when it's repeating itself.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Optional

from .navigator_observation import Observation

DEFAULT_MAX_STATE_VISITS = 2
DEFAULT_MAX_REPEATED_ACTION = 2


def fingerprint(observation: Observation) -> str:
    """Stable fingerprint for one page state.

    Deliberately coarser than the full URL (query strings/hash fragments on
    a SPA can churn without the visible page actually changing) and coarser
    than the full element list (a lazily-loaded submenu appending new rows
    to an otherwise-unchanged page must not register as a brand-new state).
    Uses the URL path, title, and the sorted (role, name) of just the
    top-level interactive elements.
    """
    from urllib.parse import urlsplit

    parsed = urlsplit(observation.url)
    path_basis = parsed.path or observation.url
    names = sorted(f"{el.role}:{el.name}" for el in observation.elements[:40])
    basis = "\n".join([path_basis, observation.title, *names])
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:24]


@dataclass
class GraphNode:
    fingerprint: str
    parent_fingerprint: Optional[str]
    arrived_via_action: Optional[dict[str, Any]]
    first_seen_step: int
    visit_count: int = 1


@dataclass
class StepResult:
    fingerprint: str
    is_new_state: bool
    visit_count: int
    loop_warning: Optional[str] = None
    repeated_action_warning: Optional[str] = None
    backtrack_available: bool = False


@dataclass
class NavigationGraph:
    max_state_visits: int = DEFAULT_MAX_STATE_VISITS
    max_repeated_action: int = DEFAULT_MAX_REPEATED_ACTION
    visited: dict[str, GraphNode] = field(default_factory=dict)
    history: list[str] = field(default_factory=list)
    _action_attempts: dict[tuple[str, str], int] = field(default_factory=dict)

    @staticmethod
    def _action_signature(action: Optional[dict[str, Any]]) -> str:
        if not isinstance(action, dict):
            return ""
        kind = str(action.get("action") or "")
        target = str(action.get("ref") or action.get("url") or action.get("key") or "")
        text = str(action.get("text") or "")
        return f"{kind}:{target}:{text}"

    def record(
        self,
        observation: Observation,
        *,
        action: Optional[dict[str, Any]] = None,
    ) -> StepResult:
        """Record one observation as the current step, after ``action`` (if any)."""
        fp = fingerprint(observation)
        step_index = len(self.history)
        previous_fp = self.history[-1] if self.history else None

        node = self.visited.get(fp)
        is_new = node is None
        if node is None:
            node = GraphNode(
                fingerprint=fp,
                parent_fingerprint=previous_fp,
                arrived_via_action=action,
                first_seen_step=step_index,
            )
            self.visited[fp] = node
        else:
            node.visit_count += 1

        self.history.append(fp)

        loop_warning = None
        if node.visit_count > self.max_state_visits:
            loop_warning = (
                f"This state has been visited {node.visit_count} times; "
                "backtrack and explore a different branch instead of repeating it."
            )

        repeated_action_warning = None
        if action is not None and previous_fp is not None:
            key = (previous_fp, self._action_signature(action))
            attempts = self._action_attempts.get(key, 0) + 1
            self._action_attempts[key] = attempts
            if attempts > self.max_repeated_action and not is_new:
                repeated_action_warning = (
                    "This exact action from this exact state has been tried "
                    f"{attempts} times with no new state reached; stop repeating it."
                )

        return StepResult(
            fingerprint=fp,
            is_new_state=is_new,
            visit_count=node.visit_count,
            loop_warning=loop_warning,
            repeated_action_warning=repeated_action_warning,
            backtrack_available=node.parent_fingerprint is not None,
        )

    def backtrack_target(self, current_fingerprint: Optional[str] = None) -> Optional[GraphNode]:
        """The graph node to return to from the current (or latest) state."""
        fp = current_fingerprint or (self.history[-1] if self.history else None)
        if fp is None:
            return None
        node = self.visited.get(fp)
        if node is None or node.parent_fingerprint is None:
            return None
        return self.visited.get(node.parent_fingerprint)

    def to_dict(self) -> dict[str, Any]:
        return {
            "history": list(self.history),
            "visited": {
                fp: {
                    "parent_fingerprint": node.parent_fingerprint,
                    "arrived_via_action": node.arrived_via_action,
                    "first_seen_step": node.first_seen_step,
                    "visit_count": node.visit_count,
                }
                for fp, node in self.visited.items()
            },
        }

    @classmethod
    def from_dict(
        cls,
        data: dict[str, Any],
        *,
        max_state_visits: int = DEFAULT_MAX_STATE_VISITS,
        max_repeated_action: int = DEFAULT_MAX_REPEATED_ACTION,
    ) -> "NavigationGraph":
        graph = cls(max_state_visits=max_state_visits, max_repeated_action=max_repeated_action)
        if not isinstance(data, dict):
            return graph
        graph.history = list(data.get("history") or [])
        visited = data.get("visited")
        if isinstance(visited, dict):
            for fp, raw in visited.items():
                if not isinstance(raw, dict):
                    continue
                graph.visited[fp] = GraphNode(
                    fingerprint=fp,
                    parent_fingerprint=raw.get("parent_fingerprint"),
                    arrived_via_action=raw.get("arrived_via_action"),
                    first_seen_step=int(raw.get("first_seen_step") or 0),
                    visit_count=int(raw.get("visit_count") or 1),
                )
        return graph
