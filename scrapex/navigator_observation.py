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
