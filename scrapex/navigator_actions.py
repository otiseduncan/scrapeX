"""Bounded action execution against the most recent Observation.

Every action targets an ``element_ref`` from the last observation, resolved
back to a live element via Playwright's own ``aria-ref=`` locator engine
(the same ref namespace ``aria_snapshot(mode="ai")`` assigns) -- never a
model-authored CSS selector. Playwright itself tracks ref -> element
validity, including across iframes; a ref from a stale/prior snapshot
simply resolves to zero elements, which is treated as a stale-ref error
here rather than silently doing nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from .navigator_observation import Observation
from .navigator_providers import NavigatorProvider, domain_allowed

VALID_ACTIONS = frozenset({"click", "fill", "press", "back", "open", "extract", "done"})

# Some sites populate content asynchronously after a click/keypress (a
# lazy-loaded submenu, a client-side search results render) with no
# corresponding network request for Playwright to wait on. A short, bounded
# settle delay after these two action kinds means the next observation
# reliably sees that content instead of racing it -- negligible next to the
# real per-turn latency of the caller's own model loop.
_DOM_SETTLE_MS = 300


class ActionError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ActionResult:
    action: str
    ref: Optional[str]
    executed: bool
    is_search_action: bool
    detail: Optional[str] = None


def _ref_exists(observation: Observation, ref: str) -> bool:
    return any(element.ref == ref for element in observation.elements)


async def _locator(page: Any, ref: str) -> Any:
    locator = page.locator(f"aria-ref={ref}")
    count = await locator.count()
    if count < 1:
        raise ActionError(
            "stale_ref",
            f"'{ref}' no longer resolves to any element -- the page changed. "
            "Call observe again before acting.",
        )
    return locator.first


class NavigatorActionExecutor:
    """Executes one bounded action against a page + the last observation."""

    def __init__(self, provider: NavigatorProvider):
        self.provider = provider

    async def execute(
        self,
        page: Any,
        observation: Optional[Observation],
        action: dict[str, Any],
    ) -> ActionResult:
        kind = str(action.get("action") or "").strip()
        if kind not in VALID_ACTIONS:
            raise ActionError(
                "invalid_action",
                f"'{kind}' is not one of the allowed actions: {sorted(VALID_ACTIONS)}.",
            )

        if kind == "done":
            return ActionResult(action=kind, ref=None, executed=True, is_search_action=False)

        if kind == "back":
            await page.go_back(wait_until="domcontentloaded")
            return ActionResult(action=kind, ref=None, executed=True, is_search_action=False)

        if kind == "extract":
            return ActionResult(action=kind, ref=None, executed=True, is_search_action=False)

        if kind == "open":
            url = str(action.get("url") or "").strip()
            if not url:
                raise ActionError("invalid_arguments", "'open' requires a 'url'.")
            if not domain_allowed(url, self.provider.allowed_domain_suffixes):
                raise ActionError(
                    "domain_not_allowed",
                    f"'{url}' is outside this provider's allowed domains "
                    f"{self.provider.allowed_domain_suffixes}.",
                )
            await page.goto(url, wait_until="domcontentloaded")
            return ActionResult(
                action=kind, ref=None, executed=True,
                is_search_action=self.provider.is_search_action(action),
            )

        ref = str(action.get("ref") or "").strip()
        if not ref:
            raise ActionError("invalid_arguments", f"'{kind}' requires a 'ref'.")
        if observation is None:
            raise ActionError(
                "no_prior_observation",
                "No observation exists yet for this task -- call observe first.",
            )
        if not _ref_exists(observation, ref):
            raise ActionError(
                "unknown_ref",
                f"'{ref}' is not a ref from the most recent observation. Call observe again "
                "before acting -- the page may have changed, or this ref was never real.",
            )
        locator = await _locator(page, ref)

        if kind == "click":
            await locator.click(timeout=10_000)
            await page.wait_for_timeout(_DOM_SETTLE_MS)
        elif kind == "fill":
            text = action.get("text")
            if not isinstance(text, str):
                raise ActionError("invalid_arguments", "'fill' requires string 'text'.")
            await locator.fill(text, timeout=10_000)
        elif kind == "press":
            key = str(action.get("key") or "").strip()
            if not key:
                raise ActionError("invalid_arguments", "'press' requires a 'key'.")
            await locator.press(key, timeout=10_000)
            await page.wait_for_timeout(_DOM_SETTLE_MS)

        return ActionResult(
            action=kind, ref=ref, executed=True,
            is_search_action=self.provider.is_search_action(action),
        )
