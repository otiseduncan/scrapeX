"""Bounded action execution against the most recent Observation.

Every ref action targets an ``element_ref`` from the last observation,
resolved back to a live element via Playwright's own ``aria-ref=`` locator
engine (the same ref namespace ``aria_snapshot(mode="ai")`` assigns) -- never
a model-authored CSS selector. Playwright itself tracks ref -> element
validity, including across iframes; a ref from a stale/prior snapshot
simply resolves to zero elements, which is treated as a stale-ref error
here rather than silently doing nothing.

Actions are bound to the observation the caller acted from. The binding is
target-local rather than whole-page: a ref must still resolve, still be
where and what it was; a mark must still be the control the observation
numbered; a coordinate must still look, in the region around it, like the
frame the caller was shown. Whole-page churn -- a clock, a lazily loaded
sidebar -- does not block an action, while a moved, replaced, or covered
target does. A target that cannot be re-established is refused with a
fresh-observation instruction; the runtime never substitutes the element it
thinks the caller meant.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from . import navigator_observation
from .navigator_observation import (
    DomControl,
    Observation,
    boxes_overlap,
    control_signature,
    page_identity_of,
)
from .navigator_providers import NavigatorProvider, domain_allowed
from .navigator_visual import VisualFrame, compare_target_region

VALID_ACTIONS = frozenset({
    "click", "fill", "type", "press", "back", "open", "scroll", "wait",
    "extract", "done", "click_mark", "click_visual", "select_vehicle",
})
REF_ACTIONS = frozenset({"click", "fill", "type", "press"})
OBSERVATION_BOUND_ACTIONS = frozenset({"click_mark", "click_visual"})
# Roles whose accessible name is stable enough to be part of a target's
# identity. A textbox or combobox legitimately reads differently once a
# value is chosen, so its name is not held against it.
_NAME_CHECKED_ROLES = frozenset({"link", "button", "tab", "menuitem", "option", "treeitem"})

# Some sites populate content asynchronously after a click/keypress (a
# lazy-loaded submenu, a client-side search results render) with no
# corresponding network request for Playwright to wait on. A short, bounded
# settle delay after these two action kinds means the next observation
# reliably sees that content instead of racing it -- negligible next to the
# real per-turn latency of the caller's own model loop.
_DOM_SETTLE_MS = 300
_TYPE_DELAY_MS = 20

_ELEMENT_DESCRIPTOR_JS = """(el) => {
  const rect = el.getBoundingClientRect();
  const text = (el.getAttribute('aria-label') || el.getAttribute('title')
    || el.getAttribute('alt') || el.getAttribute('placeholder')
    || (el.innerText || el.textContent || el.value || '')).replace(/\\s+/g, ' ').trim();
  const rawClass = typeof el.className === 'string' ? el.className : (el.getAttribute('class') || '');
  return {
    tag: el.tagName.toLowerCase(), role: el.getAttribute('role') || '',
    name: text.slice(0, 80),
    classes: rawClass.trim().split(/\\s+/).filter(Boolean).slice(0, 6).join(' '),
    id: el.id || '', w: Math.round(rect.width), h: Math.round(rect.height)
  };
}"""


class ActionError(Exception):
    def __init__(self, code: str, message: str, detail: Optional[dict[str, Any]] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail or {}


@dataclass(frozen=True)
class ActionResult:
    action: str
    ref: Optional[str]
    executed: bool
    is_search_action: bool
    detail: Optional[str] = None
    # What was actually acted on, as the runtime established it: the ref or
    # mark, its box, its label, the observation it was bound to.
    target: dict[str, Any] = field(default_factory=dict)


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


def _normalized(text: Any) -> str:
    return " ".join(str(text or "").casefold().split())


def _names_agree(observed: str, current: str) -> bool:
    left = _normalized(observed)
    right = _normalized(current)
    if not left or not right:
        return True
    head_left = left[:24]
    head_right = right[:24]
    return head_left in right or head_right in left


async def _current_page_identity(page: Any) -> Optional[str]:
    try:
        title = await page.title()
    except Exception:
        return None
    return page_identity_of(str(getattr(page, "url", "") or ""), title)


async def _element_descriptor(locator: Any) -> Optional[dict[str, Any]]:
    evaluate = getattr(locator, "evaluate", None)
    if evaluate is None:
        return None
    try:
        raw = await evaluate(_ELEMENT_DESCRIPTOR_JS)
    except Exception:
        return None
    return raw if isinstance(raw, dict) else None


async def _current_box(locator: Any) -> Optional[dict[str, int]]:
    bounding_box = getattr(locator, "bounding_box", None)
    if bounding_box is None:
        return None
    try:
        box = await bounding_box()
    except Exception:
        return None
    if not box:
        return None
    try:
        return {
            "x": int(round(float(box.get("x") or 0))),
            "y": int(round(float(box.get("y") or 0))),
            "w": int(round(float(box.get("width") or 0))),
            "h": int(round(float(box.get("height") or 0))),
        }
    except (TypeError, ValueError):
        return None


class NavigatorActionExecutor:
    """Executes one bounded action against a page + the last observation."""

    def __init__(self, provider: NavigatorProvider):
        self.provider = provider

    # ------------------------------------------------------------- binding

    @staticmethod
    def _bind_observation(observation: Optional[Observation], action: dict[str, Any], kind: str) -> None:
        supplied = str(action.get("observation_id") or "").strip()
        if kind in OBSERVATION_BOUND_ACTIONS and not supplied:
            raise ActionError(
                "invalid_arguments",
                f"'{kind}' requires the observation_id of the observation it was chosen from.",
            )
        if observation is None:
            if kind in OBSERVATION_BOUND_ACTIONS or kind in REF_ACTIONS:
                raise ActionError(
                    "no_prior_observation",
                    "No observation exists yet for this task -- call observe first.",
                )
            return
        if supplied and observation.observation_id and supplied != observation.observation_id:
            raise ActionError(
                "stale_observation",
                f"'{supplied}' is not the most recent observation ({observation.observation_id}); "
                "the page has been observed again since. Read the newest observation and choose again.",
                {"current_observation_id": observation.observation_id},
            )

    async def _require_same_page(self, page: Any, observation: Observation, kind: str) -> None:
        if not observation.page_identity:
            return
        current = await _current_page_identity(page)
        if current is None or current == observation.page_identity:
            return
        raise ActionError(
            "stale_observation",
            f"The page changed after observation {observation.observation_id} was taken, so "
            f"'{kind}' was not performed. Call observe again and choose from the new page.",
            {"observed_url": observation.url, "current_url": str(getattr(page, "url", "") or "")},
        )

    async def _validate_ref_target(
        self, page: Any, observation: Observation, ref: str, locator: Any
    ) -> dict[str, Any]:
        node = observation.element(ref)
        target: dict[str, Any] = {"kind": "ref", "ref": ref}
        if node is None:
            return target
        target.update({"role": node.role, "name": node.name})
        is_visible = getattr(locator, "is_visible", None)
        if is_visible is not None:
            try:
                visible = await is_visible()
            except Exception:
                visible = True
            if visible is False:
                raise ActionError(
                    "stale_target",
                    f"'{ref}' ({node.role} '{node.name[:60]}') is no longer visible. "
                    "Call observe again before acting.",
                )
        current_box = await _current_box(locator)
        if node.box is not None and current_box is not None:
            target["observed_box"] = node.box
            target["current_box"] = current_box
            if not boxes_overlap(node.box, current_box):
                raise ActionError(
                    "stale_target",
                    f"'{ref}' ({node.role} '{node.name[:60]}') moved from where it was "
                    "observed; the page re-rendered. Call observe again before acting.",
                    {"observed_box": node.box, "current_box": current_box},
                )
        if node.role in _NAME_CHECKED_ROLES and not node.name.startswith("unlabeled "):
            descriptor = await _element_descriptor(locator)
            if descriptor is not None:
                current_name = str(descriptor.get("name") or "")
                target["current_name"] = current_name
                if not _names_agree(node.name, current_name):
                    raise ActionError(
                        "stale_target",
                        f"'{ref}' was observed as {node.role} '{node.name[:60]}' but now reads "
                        f"'{current_name[:60]}'; the page re-rendered under that ref. Call "
                        "observe again and choose from the new state.",
                        {"observed_name": node.name, "current_name": current_name},
                    )
        return target

    # ------------------------------------------------------------- execute

    async def execute(
        self,
        page: Any,
        observation: Optional[Observation],
        action: dict[str, Any],
        *,
        visual_frame: Optional[VisualFrame] = None,
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
            await page.wait_for_timeout(_DOM_SETTLE_MS)
            return ActionResult(action=kind, ref=None, executed=True, is_search_action=False)

        if kind == "extract":
            return ActionResult(action=kind, ref=None, executed=True, is_search_action=False)

        if kind == "scroll":
            raw_delta = action.get("delta_y", 800)
            if isinstance(raw_delta, bool):
                raise ActionError("invalid_arguments", "'scroll' requires integer 'delta_y'.")
            try:
                delta_y = int(raw_delta)
            except (TypeError, ValueError) as exc:
                raise ActionError("invalid_arguments", "'scroll' requires integer 'delta_y'.") from exc
            delta_y = max(-1600, min(1600, delta_y))
            # mouse.wheel fires wherever the pointer happens to sit -- (0, 0)
            # until something has been clicked -- so on ALLDATA it landed on
            # the shell and moved nothing at all. Scroll the container the
            # observation measures instead, so "scroll down" and "how far down
            # am I" refer to the same element. The wheel stays as a fallback
            # for pages with no such container.
            scrolled = False
            try:
                frame = await navigator_observation.content_frame(page)
                scrolled = bool(
                    await frame.evaluate(navigator_observation.SCROLL_BY_JS, delta_y)
                )
            except Exception:
                scrolled = False
            if not scrolled:
                await page.mouse.wheel(0, delta_y)
            await page.wait_for_timeout(_DOM_SETTLE_MS)
            return ActionResult(action=kind, ref=None, executed=True, is_search_action=False)

        if kind == "wait":
            raw_ms = action.get("milliseconds", 700)
            if isinstance(raw_ms, bool):
                raise ActionError("invalid_arguments", "'wait' requires integer 'milliseconds'.")
            try:
                milliseconds = int(raw_ms)
            except (TypeError, ValueError) as exc:
                raise ActionError("invalid_arguments", "'wait' requires integer 'milliseconds'.") from exc
            milliseconds = max(100, min(2500, milliseconds))
            await page.wait_for_timeout(milliseconds)
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

        if kind == "select_vehicle":
            hook = getattr(self.provider, "select_vehicle", None)
            if hook is None:
                raise ActionError(
                    "provider_unsupported",
                    f"{getattr(self.provider, 'slug', 'this provider')} has no exact vehicle "
                    "selection fast path; select the vehicle through the page instead.",
                )
            outcome = await hook(page, dict(action))
            outcome = outcome if isinstance(outcome, dict) else {"selected": False}
            return ActionResult(
                action=kind,
                ref=None,
                executed=True,
                is_search_action=True,
                detail=str(outcome.get("reason") or "") or None,
                target={"kind": "vehicle", **{k: v for k, v in outcome.items() if k != "reason"}},
            )

        self._bind_observation(observation, action, kind)
        assert observation is not None

        if kind == "click_mark":
            return await self._click_mark(page, observation, action)
        if kind == "click_visual":
            return await self._click_visual(page, observation, action, visual_frame)

        ref = str(action.get("ref") or "").strip()
        if not ref:
            raise ActionError("invalid_arguments", f"'{kind}' requires a 'ref'.")
        if not _ref_exists(observation, ref):
            raise ActionError(
                "unknown_ref",
                f"'{ref}' is not a ref from the most recent observation. Call observe again "
                "before acting -- the page may have changed, or this ref was never real.",
            )
        if str(action.get("observation_id") or "").strip():
            await self._require_same_page(page, observation, kind)
        locator = await _locator(page, ref)
        target = await self._validate_ref_target(page, observation, ref, locator)

        if kind == "click":
            await locator.click(timeout=10_000)
            await page.wait_for_timeout(_DOM_SETTLE_MS)
        elif kind == "fill":
            text = action.get("text")
            if not isinstance(text, str):
                raise ActionError("invalid_arguments", "'fill' requires string 'text'.")
            await locator.fill(text, timeout=10_000)
        elif kind == "type":
            text = action.get("text")
            if not isinstance(text, str) or not text:
                raise ActionError("invalid_arguments", "'type' requires non-empty string 'text'.")
            # Real keystrokes, one character at a time: some fields ignore a
            # programmatic fill (one synthetic input event, no reaction) and
            # only behave for a person's typing.
            await locator.click(timeout=10_000)
            await page.keyboard.type(text, delay=_TYPE_DELAY_MS)
            await page.wait_for_timeout(_DOM_SETTLE_MS)
        elif kind == "press":
            key = str(action.get("key") or "").strip()
            if not key:
                raise ActionError("invalid_arguments", "'press' requires a 'key'.")
            await locator.press(key, timeout=10_000)
            await page.wait_for_timeout(_DOM_SETTLE_MS)

        return ActionResult(
            action=kind, ref=ref, executed=True,
            is_search_action=self.provider.is_search_action(action),
            target={**target, "observation_id": observation.observation_id},
        )

    # ---------------------------------------------------------------- marks

    async def _click_mark(self, page: Any, observation: Observation, action: dict[str, Any]) -> ActionResult:
        raw_mark = action.get("mark")
        if isinstance(raw_mark, bool):
            raise ActionError("invalid_arguments", "'click_mark' requires integer 'mark'.")
        try:
            number = int(raw_mark)
        except (TypeError, ValueError) as exc:
            raise ActionError("invalid_arguments", "'click_mark' requires integer 'mark'.") from exc
        mark = observation.mark(number)
        if mark is None:
            raise ActionError(
                "unknown_mark",
                f"[m{number}] is not a mark on observation {observation.observation_id}. "
                "Marks are only offered by an observation that was asked for them; request "
                "marks again and use one it lists.",
            )
        await self._require_same_page(page, observation, "click_mark")
        control = mark.control
        current = await self._relocate_control(page, control)
        if current is None:
            raise ActionError(
                "stale_target",
                f"[m{number}] ({control.tag} '{control.name[:60]}') is no longer where the "
                "observation placed it; the page re-rendered. Request marks again.",
                {"observed_box": control.box},
            )
        current_box, descriptor = current
        if not boxes_overlap(control.box, current_box):
            raise ActionError(
                "stale_target",
                f"[m{number}] moved from where it was observed; the page re-rendered. "
                "Request marks again before acting.",
                {"observed_box": control.box, "current_box": current_box},
            )
        current_sig = control_signature(
            descriptor.get("tag"), descriptor.get("role"), descriptor.get("name"),
            descriptor.get("classes"), descriptor.get("id"), control.w, control.h,
        )
        if current_sig != control.sig:
            raise ActionError(
                "stale_target",
                f"[m{number}] is now a different control ({descriptor.get('tag')} "
                f"'{str(descriptor.get('name') or '')[:60]}') than the one observed; the page "
                "re-rendered. Request marks again before acting.",
                {"observed": {"tag": control.tag, "name": control.name}, "current": descriptor},
            )
        cx = current_box["x"] + current_box["w"] / 2
        cy = current_box["y"] + current_box["h"] / 2
        await page.mouse.click(cx, cy)
        await page.wait_for_timeout(_DOM_SETTLE_MS)
        return ActionResult(
            action="click_mark",
            ref=None,
            executed=True,
            is_search_action=self.provider.is_search_action({"action": "click"}),
            target={
                "kind": "mark",
                "mark": number,
                "tag": control.tag,
                "name": control.name,
                "observed_box": control.box,
                "current_box": current_box,
                "point": {"x": int(round(cx)), "y": int(round(cy))},
                "observation_id": observation.observation_id,
            },
        )

    async def _relocate_control(
        self, page: Any, control: DomControl
    ) -> Optional[tuple[dict[str, int], dict[str, Any]]]:
        try:
            frames = await navigator_observation.frame_offsets(page)
        except Exception:
            frames = [(0, page, 0, 0)]
        frame = None
        for index, candidate, _ox, _oy in frames:
            if index == control.frame:
                frame = candidate
                break
        if frame is None or not control.path:
            return None
        try:
            locator = frame.locator(control.path)
            if await locator.count() < 1:
                return None
            locator = locator.first
        except Exception:
            return None
        current_box = await _current_box(locator)
        descriptor = await _element_descriptor(locator)
        if current_box is None or descriptor is None:
            return None
        return current_box, descriptor

    # --------------------------------------------------------------- visual

    async def _click_visual(
        self,
        page: Any,
        observation: Observation,
        action: dict[str, Any],
        visual_frame: Optional[VisualFrame],
    ) -> ActionResult:
        x_norm = action.get("x_norm")
        y_norm = action.get("y_norm")
        for value in (x_norm, y_norm):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0.0 <= float(value) <= 1.0:
                raise ActionError(
                    "invalid_arguments",
                    "'click_visual' requires x_norm and y_norm between 0 and 1, measured on the "
                    "screenshot of the observation named by observation_id.",
                )
        if observation.window_width <= 0 or observation.window_height <= 0:
            raise ActionError(
                "invalid_arguments",
                "The observation carries no viewport size, so a normalized point cannot be placed.",
            )
        x = int(round(float(x_norm) * observation.window_width))
        y = int(round(float(y_norm) * observation.window_height))
        x = max(0, min(observation.window_width - 1, x))
        y = max(0, min(observation.window_height - 1, y))
        await self._require_same_page(page, observation, "click_visual")
        if visual_frame is None or visual_frame.observation_id != observation.observation_id:
            raise ActionError(
                "visual_frame_missing",
                "No screenshot frame is held for this observation, so a coordinate click cannot "
                "be checked against what was seen. Take a fresh observation with its screenshot "
                "and choose again.",
            )
        current_png = await navigator_observation.plain_viewport_png(page)
        comparison = compare_target_region(visual_frame.png, current_png, x=x, y=y)
        if not comparison.get("same"):
            raise ActionError(
                "stale_visual_target",
                "The region around that point no longer looks like the screenshot it was chosen "
                "from (mean difference "
                f"{comparison.get('difference')}, limit {comparison.get('threshold')}); the page "
                "changed there. Take a fresh observation and screenshot, then choose again.",
                {"comparison": comparison, "point": {"x": x, "y": y}},
            )
        expected = observation.control_at(x, y)
        probe = await self._probe_point(page, x, y)
        await page.mouse.click(x, y)
        await page.wait_for_timeout(_DOM_SETTLE_MS)
        target: dict[str, Any] = {
            "kind": "visual",
            "point": {"x": x, "y": y},
            "x_norm": round(float(x_norm), 4),
            "y_norm": round(float(y_norm), 4),
            "region_difference": comparison.get("difference"),
            "observation_id": observation.observation_id,
        }
        if expected is not None:
            target["observed_control"] = {
                "tag": expected.tag, "name": expected.name, "box": expected.box,
            }
        if probe:
            target["element_under_point"] = probe
        return ActionResult(
            action="click_visual",
            ref=None,
            executed=True,
            is_search_action=self.provider.is_search_action({"action": "click"}),
            target=target,
        )

    @staticmethod
    async def _probe_point(page: Any, x: int, y: int) -> Optional[dict[str, Any]]:
        evaluate = getattr(page, "evaluate", None)
        if evaluate is None:
            return None
        try:
            probe = await evaluate(navigator_observation.POINT_PROBE_JS, {"x": x, "y": y})
        except Exception:
            return None
        if not isinstance(probe, dict):
            return None
        if probe.get("is_frame"):
            # Descend one level: describe the element inside the frame at
            # the same top-viewport point.
            try:
                frames = await navigator_observation.frame_offsets(page)
            except Exception:
                frames = []
            for index, frame, offset_x, offset_y in frames:
                if index == 0:
                    continue
                if not (
                    probe["x"] <= x <= probe["x"] + probe["w"]
                    and probe["y"] <= y <= probe["y"] + probe["h"]
                ):
                    continue
                if abs(offset_x - probe["x"]) > 2 or abs(offset_y - probe["y"]) > 2:
                    continue
                try:
                    inner = await frame.evaluate(
                        navigator_observation.POINT_PROBE_JS,
                        {"x": x - offset_x, "y": y - offset_y, "offsetX": offset_x, "offsetY": offset_y},
                    )
                except Exception:
                    inner = None
                if isinstance(inner, dict):
                    inner["frame"] = index
                    return inner
        return probe
