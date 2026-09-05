"""Compact browser-page observation for the Navigator.

Built on Playwright's own ``Locator.aria_snapshot(mode="ai")`` -- the same
ref-tracked accessibility snapshot format Playwright's own MCP tooling uses
for LLM-driven browser control. Playwright itself maintains the ref -> live
element mapping (including across iframes, which get their own ``fN``-
prefixed ref namespace automatically), so the Navigator never needs to
re-locate an element by role/name/nth guesswork -- ``page.locator(f"aria-ref={ref}")``
resolves it directly. A ref from a stale/prior snapshot simply fails to
resolve (0 matches), which the action layer treats as a stale-ref error.

The parser below (``parse_aria_snapshot``) is pure -- text in, node list out
-- so it is fully unit-testable without a browser.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

MAX_PAGE_TEXT_CHARS = 8_000
MAX_ELEMENTS = 300
MAX_SCREENSHOT_LABELS = 120
SCREENSHOT_JPEG_QUALITY = 72
_SCREENSHOT_OVERLAY_ID = "__scrapex_navigator_ref_overlay__"

_LEADING_DASH_RE = re.compile(r"^-\s+")
_ROLE_RE = re.compile(r"^([a-zA-Z][a-zA-Z0-9_-]*)")
_QUOTED_NAME_RE = re.compile(r'"((?:[^"\\]|\\.)*)"')
_REF_RE = re.compile(r"\[ref=([a-z0-9]+)\]", re.IGNORECASE)
_BRACKET_RE = re.compile(r"\[[^\]]*\]")
_TRAILING_TEXT_RE = re.compile(r":\s*(\S.*)$")
# Private-use-area codepoints are custom icon-font glyphs, not real text --
# confirmed live against ALLDATA, whose toolbar icons parse to names like
# "". A name that is *entirely* PUA characters carries no semantic
# value and only crowds out real, actionable elements within the bounded
# element list a caller feeds back to a model.
_PUA_ONLY_RE = re.compile(r"^[-\s]+$")


@dataclass(frozen=True)
class ObservationNode:
    ref: str
    role: str
    name: str
    depth: int = 0
    expanded: Optional[bool] = None


@dataclass(frozen=True)
class Observation:
    url: str
    title: str
    elements: list[ObservationNode] = field(default_factory=list)
    page_text: str = ""
    breadcrumb: list[str] = field(default_factory=list)


def _unescape(text: str) -> str:
    return text.replace('\\"', '"').replace("\\\\", "\\")


def parse_aria_snapshot(
    text: str, *, max_elements: int = MAX_ELEMENTS
) -> list[ObservationNode]:
    """Parse ``Locator.aria_snapshot(mode="ai")`` YAML-ish text.

    Only lines carrying a ``[ref=...]`` are actionable/observable elements;
    lines without one (e.g. a bare ``- list`` container) are structure-only
    and skipped -- there is nothing to click and no ref to click it with.
    """
    nodes: list[ObservationNode] = []
    for raw_line in str(text or "").splitlines():
        if len(nodes) >= max_elements:
            break
        stripped = raw_line.lstrip(" ")
        if not stripped.startswith("-"):
            continue
        indent = len(raw_line) - len(stripped)
        depth = indent // 2

        ref_match = _REF_RE.search(stripped)
        if not ref_match:
            continue
        ref = ref_match.group(1)

        body = _LEADING_DASH_RE.sub("", stripped, count=1)
        role_match = _ROLE_RE.match(body)
        role = role_match.group(1) if role_match else "generic"

        name_match = _QUOTED_NAME_RE.search(body)
        if name_match:
            name = _unescape(name_match.group(1))
        else:
            without_brackets = _BRACKET_RE.sub("", body).strip()
            trailing_match = _TRAILING_TEXT_RE.search(without_brackets)
            name = trailing_match.group(1).strip() if trailing_match else ""

        if not name or _PUA_ONLY_RE.match(name):
            continue

        expanded: Optional[bool] = None
        if "[expanded]" in body:
            expanded = True
        elif "[collapsed]" in body:
            expanded = False

        nodes.append(
            ObservationNode(ref=ref, role=role, name=name[:200], depth=depth, expanded=expanded)
        )

    return nodes


def bounded_text(raw: str, *, max_chars: int = MAX_PAGE_TEXT_CHARS) -> str:
    text = " ".join(str(raw or "").split())
    return text[:max_chars]


async def build_observation(page: Any, *, breadcrumb: Optional[list[str]] = None) -> Observation:
    """Build a full Observation from a live Playwright ``Page``."""
    try:
        raw_snapshot = await page.locator("body").aria_snapshot(mode="ai")
    except Exception:
        raw_snapshot = ""
    elements = parse_aria_snapshot(raw_snapshot)

    try:
        # Iframe content shows up in the element snapshot (Playwright's own
        # aria-ref namespace covers it automatically) but ``inner_text`` on
        # the top page's body does not reach into child frames -- walk every
        # frame so verification text-matching sees the same content a human
        # reading the rendered page would.
        texts = []
        for frame in page.frames:
            try:
                texts.append(await frame.inner_text("body"))
            except Exception:
                continue
        raw_text = " ".join(texts)
    except Exception:
        raw_text = ""

    try:
        title = await page.title()
    except Exception:
        title = ""

    return Observation(
        url=str(page.url or ""),
        title=title,
        elements=elements,
        page_text=bounded_text(raw_text),
        breadcrumb=list(breadcrumb or []),
    )


async def annotated_viewport_screenshot(
    page: Any,
    observation: Observation,
    *,
    max_labels: int = MAX_SCREENSHOT_LABELS,
) -> bytes:
    """Capture the current rendered viewport with task-action refs overlaid.

    The overlay is deliberately transient: it is injected only for the still
    image, has pointer-events disabled, and is removed in finally before
    control returns to the Navigator. The browser DOM/accessibility snapshot
    remains the action authority; pixels only give the multimodal model the
    visual/layout context a human operator has.

    Only refs from the exact cached Observation are eligible for labels. A
    stale ref that no longer resolves is skipped rather than re-located by
    text/role guesswork. This keeps the screenshot and action contract bound
    to the same observation without exposing credentials, cookies, or browser
    profile data through the API.
    """
    try:
        viewport = await page.evaluate(
            "() => ({width: window.innerWidth || 0, height: window.innerHeight || 0})"
        )
        viewport_width = float((viewport or {}).get("width") or 0)
        viewport_height = float((viewport or {}).get("height") or 0)
    except Exception:
        viewport_width = viewport_height = 0

    labels: list[dict[str, Any]] = []
    for element in observation.elements[: max(0, int(max_labels))]:
        try:
            locator = page.locator(f"aria-ref={element.ref}")
            if await locator.count() < 1:
                continue
            box = await locator.first.bounding_box()
        except Exception:
            continue
        if not box:
            continue
        x = float(box.get("x") or 0)
        y = float(box.get("y") or 0)
        width = float(box.get("width") or 0)
        height = float(box.get("height") or 0)
        if width <= 0 or height <= 0:
            continue
        if viewport_width > 0 and (x >= viewport_width or x + width <= 0):
            continue
        if viewport_height > 0 and (y >= viewport_height or y + height <= 0):
            continue
        labels.append(
            {
                "ref": element.ref,
                "x": x,
                "y": y,
                "width": width,
                "height": height,
            }
        )

    overlay_installed = False
    if labels:
        try:
            await page.evaluate(
                """({overlayId, labels}) => {
                    document.getElementById(overlayId)?.remove();
                    const root = document.createElement('div');
                    root.id = overlayId;
                    root.setAttribute('aria-hidden', 'true');
                    root.style.position = 'fixed';
                    root.style.left = '0';
                    root.style.top = '0';
                    root.style.width = '0';
                    root.style.height = '0';
                    root.style.zIndex = '2147483647';
                    root.style.pointerEvents = 'none';
                    for (const item of labels) {
                        const box = document.createElement('div');
                        box.style.position = 'fixed';
                        box.style.left = Math.max(0, item.x) + 'px';
                        box.style.top = Math.max(0, item.y) + 'px';
                        box.style.width = Math.max(2, item.width) + 'px';
                        box.style.height = Math.max(2, item.height) + 'px';
                        box.style.border = '2px solid #ff2d55';
                        box.style.borderRadius = '3px';
                        box.style.boxSizing = 'border-box';

                        const label = document.createElement('div');
                        label.textContent = '[' + item.ref + ']';
                        label.style.position = 'absolute';
                        label.style.left = '-2px';
                        label.style.top = '-20px';
                        label.style.padding = '1px 4px';
                        label.style.background = '#111';
                        label.style.color = '#fff';
                        label.style.border = '1px solid #ff2d55';
                        label.style.borderRadius = '3px';
                        label.style.font = '700 12px/16px monospace';
                        label.style.whiteSpace = 'nowrap';
                        box.appendChild(label);
                        root.appendChild(box);
                    }
                    document.documentElement.appendChild(root);
                }""",
                {"overlayId": _SCREENSHOT_OVERLAY_ID, "labels": labels},
            )
            overlay_installed = True
        except Exception:
            overlay_installed = False

    try:
        return await page.screenshot(
            type="jpeg", quality=SCREENSHOT_JPEG_QUALITY, full_page=False
        )
    finally:
        if overlay_installed:
            try:
                await page.evaluate(
                    "(overlayId) => document.getElementById(overlayId)?.remove()",
                    _SCREENSHOT_OVERLAY_ID,
                )
            except Exception:
                pass
