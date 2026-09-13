"""Compact browser-page observation for the Navigator.

Built on Playwright's own ``Locator.aria_snapshot(mode="ai")`` -- the same
ref-tracked accessibility snapshot format Playwright's own MCP tooling uses
for LLM-driven browser control. Playwright itself maintains the ref -> live
element mapping (including across iframes, which get their own ``fN``-
prefixed ref namespace automatically), so the Navigator never needs to
re-locate an element by role/name/nth guesswork -- ``page.locator(f"aria-ref={ref}")``
resolves it directly. A ref from a stale/prior snapshot simply fails to
resolve (0 matches), which the action layer treats as a stale-ref error.

Every observation also carries an ``observation_id``, the page identity it
described, the viewport size, the geometry of the interactive refs that are
on screen, and the visible interactive DOM controls the accessibility tree
does not expose as usable refs. Those last two are what let an action be
bound to exactly the state the model saw -- a ref click is checked against
the box and label it had, a mark click against the control it named, and a
coordinate click against the frame it was aimed at -- and what let the
runtime offer a bounded set of visual marks for controls that carry their
meaning in a glyph.

The parser below (``parse_aria_snapshot``) is pure -- text in, node list out
-- so it is fully unit-testable without a browser.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

MAX_PAGE_TEXT_CHARS = 8_000
MAX_ELEMENTS = 300
MAX_SCREENSHOT_LABELS = 120
# Interactive refs whose geometry is measured per observation. The screenshot
# overlay reuses these boxes, so measuring them here costs nothing extra.
MAX_REF_GEOMETRY = 120
# Visible interactive DOM controls enumerated per page (all frames).
MAX_DOM_CONTROLS = 160
# Set-of-Mark budget per frame. Marks are offered only for controls that have
# no usable accessibility ref, and only when asked for; a page-wide label
# storm would hide the page it is meant to explain.
MAX_MARKS = 24
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
# "". A name that is *entirely* PUA characters carries no semantic
# value and only crowds out real, actionable elements within the bounded
# element list a caller feeds back to a model.
_PUA_ONLY_RE = re.compile(r"^[-\s]+$")

# Roles a caller can actually act on. An icon-only control -- a magnifier,
# a chevron, a close X -- carries its meaning in a glyph rather than a name,
# so dropping unnamed nodes wholesale removed the controls themselves.
# Confirmed live on 2026-09-12: ALLDATA's vehicle page reported zero
# buttons, and the search button beside its search box was absent from
# every observation ever taken -- the one control that reaches a vehicle's
# content without guessing that manufacturer's menu tree. Nameless
# *non*-interactive nodes stay dropped; those are only noise.
_INTERACTIVE_ROLES = frozenset({
    "button", "link", "searchbox", "textbox", "combobox", "checkbox",
    "radio", "tab", "menuitem", "menuitemcheckbox", "menuitemradio",
    "option", "treeitem", "switch", "slider", "spinbutton",
})


@dataclass(frozen=True)
class ObservationNode:
    ref: str
    role: str
    name: str
    depth: int = 0
    expanded: Optional[bool] = None
    # Viewport box {x, y, w, h} in CSS pixels when the ref was measured on
    # screen; None when it was off screen or not measured.
    box: Optional[dict[str, int]] = None


@dataclass(frozen=True)
class DomControl:
    """A visible, interactive-looking DOM element, ref or no ref."""

    tag: str
    role: str
    name: str
    classes: str
    element_id: str
    path: str
    frame: int
    x: int
    y: int
    w: int
    h: int
    sig: str

    @property
    def box(self) -> dict[str, int]:
        return {"x": self.x, "y": self.y, "w": self.w, "h": self.h}

    @property
    def center(self) -> tuple[int, int]:
        return self.x + self.w // 2, self.y + self.h // 2


@dataclass(frozen=True)
class Mark:
    mark: int
    control: DomControl


@dataclass(frozen=True)
class Observation:
    url: str
    title: str
    elements: list[ObservationNode] = field(default_factory=list)
    page_text: str = ""
    breadcrumb: list[str] = field(default_factory=list)
    # A long OEM procedure does not fit in MAX_PAGE_TEXT_CHARS, and handing
    # back the first 8k of it silently is indistinguishable from handing back
    # the whole page. Confirmed live on 2026-09-12: the ALLDATA article
    # "Front Radar Unit (ADAS) - Repair Procedures" for a 2021 Hyundai Truck
    # Palisade came back as exactly 8000 characters ending mid-word, so the
    # calibration target distance -- which sits at the bottom of that
    # procedure -- was absent with nothing to say so. The reader clicked
    # around the part it could see instead of scrolling, because as far as
    # the observation showed there was nothing below.
    page_text_truncated: bool = False
    page_text_total_chars: int = 0
    # Where the viewport sits in the document, so "is there more below?" is
    # answerable from the observation instead of guessed from pixels.
    scroll_y: int = 0
    scroll_height: int = 0
    viewport_height: int = 0
    # Identity of this exact observation and of the rendered page it
    # describes; actions bind to these.
    observation_id: str = ""
    page_identity: str = ""
    window_width: int = 0
    window_height: int = 0
    controls: list[DomControl] = field(default_factory=list)
    marks: list[Mark] = field(default_factory=list)

    @property
    def at_page_bottom(self) -> bool:
        if self.scroll_height <= 0 or self.viewport_height <= 0:
            return False
        return self.scroll_y + self.viewport_height >= self.scroll_height - 2

    def element(self, ref: str) -> Optional[ObservationNode]:
        for node in self.elements:
            if node.ref == ref:
                return node
        return None

    def mark(self, number: int) -> Optional[Mark]:
        for item in self.marks:
            if item.mark == number:
                return item
        return None

    def control_at(self, x: int, y: int) -> Optional[DomControl]:
        """The smallest observed control whose box contains (x, y)."""
        best: Optional[DomControl] = None
        for control in self.controls:
            if control.x <= x <= control.x + control.w and control.y <= y <= control.y + control.h:
                if best is None or control.w * control.h < best.w * best.h:
                    best = control
        return best


def new_observation_id() -> str:
    return "obs_" + uuid.uuid4().hex[:12]


def page_identity_of(url: str, title: str) -> str:
    """Identity of one rendered page: the full URL (an SPA moves in its
    fragment) and its title."""
    basis = f"{str(url or '').strip()}\n{str(title or '').strip()}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:20]


# ALLDATA renders its content inside an iframe whose own inner container does
# the scrolling -- the top document never scrolls at all. Measured live on
# 2026-09-12 across a whole run: document.documentElement.scrollHeight equalled
# window.innerHeight (720 == 720) on every page, including a 7,758-character
# procedure article, so geometry read from the top document reported
# "at the bottom" everywhere and was worse than reporting nothing.
#
# This finds the element that actually governs reading: the largest visible
# box whose content overflows it, preferring real scroll containers. Used for
# both measuring position and performing a scroll, so the two always agree
# about what "the page" means.
SCROLLER_JS = """() => {
  const doc = document;
  const de = doc.documentElement;
  const seen = new Set();
  const found = [];
  const consider = (el) => {
    if (!el || seen.has(el)) return;
    seen.add(el);
    const sh = el.scrollHeight || 0;
    const ch = el.clientHeight || 0;
    if (ch <= 0 || sh - ch <= 8) return;
    if (el !== de && el !== doc.body) {
      const oy = getComputedStyle(el).overflowY;
      if (oy !== 'auto' && oy !== 'scroll' && oy !== 'overlay') return;
    }
    found.push({el: el, top: el.scrollTop || 0, sh: sh, ch: ch, w: el.clientWidth || 0});
  };
  consider(de);
  consider(doc.body);
  const all = doc.querySelectorAll('*');
  const cap = Math.min(all.length, 4000);
  for (let i = 0; i < cap; i++) consider(all[i]);
  if (!found.length) {
    return {scrollY: 0,
            scrollHeight: de ? (de.scrollHeight || 0) : 0,
            innerHeight: window.innerHeight || 0,
            scrollable: false};
  }
  found.sort((a, b) => (b.ch * b.w) - (a.ch * a.w) || (b.sh - a.sh));
  const best = found[0];
  return {scrollY: Math.round(best.top),
          scrollHeight: Math.round(best.sh),
          innerHeight: Math.round(best.ch),
          scrollable: true};
}"""

SCROLL_BY_JS = """(delta) => {
  const doc = document;
  const de = doc.documentElement;
  const seen = new Set();
  const found = [];
  const consider = (el) => {
    if (!el || seen.has(el)) return;
    seen.add(el);
    const sh = el.scrollHeight || 0;
    const ch = el.clientHeight || 0;
    if (ch <= 0 || sh - ch <= 8) return;
    if (el !== de && el !== doc.body) {
      const oy = getComputedStyle(el).overflowY;
      if (oy !== 'auto' && oy !== 'scroll' && oy !== 'overlay') return;
    }
    found.push({el: el, ch: ch, sh: sh, w: el.clientWidth || 0});
  };
  consider(de);
  consider(doc.body);
  const all = doc.querySelectorAll('*');
  const cap = Math.min(all.length, 4000);
  for (let i = 0; i < cap; i++) consider(all[i]);
  if (!found.length) { window.scrollBy(0, delta); return false; }
  found.sort((a, b) => (b.ch * b.w) - (a.ch * a.w) || (b.sh - a.sh));
  const target = found[0].el;
  const before = target.scrollTop;
  target.scrollTop = before + delta;
  return target.scrollTop !== before;
}"""

# Visible, interactive-looking DOM elements with their viewport geometry.
# This is mechanical enumeration -- tag, role attribute, label text, class
# tokens, a generated path -- and never a judgement about what a control is
# for. The click-target check and Set-of-Mark are built on it.
DOM_CONTROLS_JS = """(args) => {
  const maxItems = args.maxItems || 160;
  const ox = args.offsetX || 0;
  const oy = args.offsetY || 0;
  const vw = window.innerWidth || 0;
  const vh = window.innerHeight || 0;
  const maxArea = vw * vh * 0.4;
  const selectors = [
    'button', 'a[href]', 'input', 'select', 'textarea', 'summary', 'label',
    '[role="button"]', '[role="link"]', '[role="tab"]', '[role="menuitem"]',
    '[role="option"]', '[role="checkbox"]', '[role="radio"]', '[role="switch"]',
    '[role="treeitem"]', '[onclick]', '[tabindex]', 'i[class]', 'svg',
    '[class*="icon" i]', '[class*="btn" i]', '[class*="print" i]',
    '[aria-label]', '[title]'
  ].join(',');
  const cssPath = (el) => {
    const parts = [];
    let node = el;
    let depth = 0;
    while (node && node.nodeType === 1 && depth < 14) {
      let part = node.tagName.toLowerCase();
      if (node.id && /^[A-Za-z][-A-Za-z0-9_:.]*$/.test(node.id)) {
        parts.unshift('#' + CSS.escape(node.id));
        break;
      }
      const parent = node.parentElement;
      if (parent) {
        let index = 1;
        let sibling = node.previousElementSibling;
        while (sibling) {
          if (sibling.tagName === node.tagName) index += 1;
          sibling = sibling.previousElementSibling;
        }
        part += ':nth-of-type(' + index + ')';
      }
      parts.unshift(part);
      node = parent;
      depth += 1;
    }
    return parts.join(' > ');
  };
  const nodes = document.querySelectorAll(selectors);
  const cap = Math.min(nodes.length, 3000);
  const kept = [];
  const keptSet = new Set();
  const seenBoxes = new Set();
  for (let i = 0; i < cap && kept.length < maxItems; i++) {
    const el = nodes[i];
    const rect = el.getBoundingClientRect();
    if (rect.width < 6 || rect.height < 6) continue;
    if (rect.bottom <= 0 || rect.right <= 0 || rect.top >= vh || rect.left >= vw) continue;
    if (rect.width * rect.height > maxArea) continue;
    const style = getComputedStyle(el);
    if (style.visibility === 'hidden' || style.display === 'none') continue;
    if (parseFloat(style.opacity || '1') === 0) continue;
    const tag = el.tagName.toLowerCase();
    const role = el.getAttribute('role') || '';
    const nativelyClickable = ['button', 'a', 'input', 'select', 'textarea', 'summary', 'label'].includes(tag);
    const clickable = nativelyClickable || !!role || el.hasAttribute('onclick')
      || el.hasAttribute('tabindex') || style.cursor === 'pointer';
    if (!clickable) continue;
    let ancestorKept = false;
    let up = el.parentElement;
    while (up) { if (keptSet.has(up)) { ancestorKept = true; break; } up = up.parentElement; }
    if (ancestorKept) continue;
    const cx = Math.min(vw - 1, Math.max(0, rect.left + rect.width / 2));
    const cy = Math.min(vh - 1, Math.max(0, rect.top + rect.height / 2));
    const hit = document.elementFromPoint(cx, cy);
    if (!hit || !(hit === el || el.contains(hit) || hit.contains(el))) continue;
    const key = [Math.round(rect.left), Math.round(rect.top), Math.round(rect.width), Math.round(rect.height)].join(':');
    if (seenBoxes.has(key)) continue;
    seenBoxes.add(key);
    const text = (el.getAttribute('aria-label') || el.getAttribute('title')
      || el.getAttribute('alt') || el.getAttribute('placeholder')
      || (el.innerText || el.textContent || '')).replace(/\\s+/g, ' ').trim();
    const rawClass = typeof el.className === 'string' ? el.className : (el.getAttribute('class') || '');
    const classes = rawClass.trim().split(/\\s+/).filter(Boolean).slice(0, 6).join(' ');
    kept.push({
      tag: tag, role: role, name: text.slice(0, 80), classes: classes.slice(0, 120),
      id: el.id || '', path: cssPath(el),
      x: Math.round(rect.left + ox), y: Math.round(rect.top + oy),
      w: Math.round(rect.width), h: Math.round(rect.height)
    });
    keptSet.add(el);
  }
  return kept;
}"""

# The element under a viewport point, described the same way DOM controls
# are, so a coordinate click can be receipted and checked against what the
# observation recorded there.
POINT_PROBE_JS = """(args) => {
  const el = document.elementFromPoint(args.x, args.y);
  if (!el) return null;
  const rect = el.getBoundingClientRect();
  const text = (el.getAttribute('aria-label') || el.getAttribute('title')
    || el.getAttribute('alt') || (el.innerText || el.textContent || '')).replace(/\\s+/g, ' ').trim();
  const rawClass = typeof el.className === 'string' ? el.className : (el.getAttribute('class') || '');
  return {
    tag: el.tagName.toLowerCase(), role: el.getAttribute('role') || '',
    name: text.slice(0, 80), classes: rawClass.trim().split(/\\s+/).filter(Boolean).slice(0, 6).join(' '),
    id: el.id || '', is_frame: el.tagName.toLowerCase() === 'iframe',
    x: Math.round(rect.left + (args.offsetX || 0)), y: Math.round(rect.top + (args.offsetY || 0)),
    w: Math.round(rect.width), h: Math.round(rect.height)
  };
}"""


def control_signature(tag: str, role: str, name: str, classes: str, element_id: str, w: int, h: int) -> str:
    basis = "|".join([
        str(tag or "").casefold(),
        str(role or "").casefold(),
        " ".join(str(name or "").casefold().split())[:80],
        " ".join(sorted(str(classes or "").casefold().split()))[:120],
        str(element_id or ""),
        str(int(w) // 4),
        str(int(h) // 4),
    ])
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:16]


def control_from_raw(raw: dict[str, Any], frame: int) -> Optional[DomControl]:
    if not isinstance(raw, dict):
        return None
    try:
        w = int(raw.get("w") or 0)
        h = int(raw.get("h") or 0)
        x = int(raw.get("x") or 0)
        y = int(raw.get("y") or 0)
    except (TypeError, ValueError):
        return None
    if w <= 0 or h <= 0:
        return None
    tag = str(raw.get("tag") or "")
    role = str(raw.get("role") or "")
    name = str(raw.get("name") or "")
    classes = str(raw.get("classes") or "")
    element_id = str(raw.get("id") or "")
    return DomControl(
        tag=tag,
        role=role,
        name=name[:80],
        classes=classes[:120],
        element_id=element_id[:80],
        path=str(raw.get("path") or "")[:600],
        frame=int(frame),
        x=x,
        y=y,
        w=w,
        h=h,
        sig=str(raw.get("sig") or control_signature(tag, role, name, classes, element_id, w, h)),
    )


def boxes_overlap(a: dict[str, int], b: dict[str, int], *, min_iou: float = 0.3, center_slack: int = 24) -> bool:
    """Approximate geometric identity of two viewport boxes.

    True when the boxes overlap substantially, or when the centre of one lies
    within a small slack of the other -- a control that re-rendered a few
    pixels over is the same control; one that moved across the page is not.
    """
    try:
        ax, ay, aw, ah = int(a["x"]), int(a["y"]), int(a["w"]), int(a["h"])
        bx, by, bw, bh = int(b["x"]), int(b["y"]), int(b["w"]), int(b["h"])
    except (KeyError, TypeError, ValueError):
        return False
    inter_w = min(ax + aw, bx + bw) - max(ax, bx)
    inter_h = min(ay + ah, by + bh) - max(ay, by)
    inter = max(0, inter_w) * max(0, inter_h)
    union = aw * ah + bw * bh - inter
    if union > 0 and inter / union >= min_iou:
        return True
    acx, acy = ax + aw / 2, ay + ah / 2
    bcx, bcy = bx + bw / 2, by + bh / 2
    return abs(acx - bcx) <= center_slack and abs(acy - bcy) <= center_slack


def point_in_box(x: int, y: int, box: dict[str, int], slack: int = 2) -> bool:
    try:
        return (
            box["x"] - slack <= x <= box["x"] + box["w"] + slack
            and box["y"] - slack <= y <= box["y"] + box["h"] + slack
        )
    except (KeyError, TypeError):
        return False


def select_marks(
    elements: list[ObservationNode],
    controls: list[DomControl],
    *,
    limit: int = MAX_MARKS,
) -> list[Mark]:
    """Marks for controls no usable accessibility ref already covers.

    A control whose centre falls inside the measured box of an interactive
    ref is reachable by that ref and gets no mark. The remainder -- icon
    buttons the accessibility tree hides, glyph-only tools, clickable spans --
    are numbered in document order up to the per-frame budget. This makes no
    judgement about which control matters; the model asks for marks when it
    can see a control it cannot name.
    """
    ref_boxes = [
        node.box
        for node in elements
        if node.box is not None and node.role in _INTERACTIVE_ROLES
    ]
    marks: list[Mark] = []
    number = 21
    for control in controls:
        cx, cy = control.center
        if any(point_in_box(cx, cy, box) for box in ref_boxes):
            continue
        marks.append(Mark(mark=number, control=control))
        number += 1
        if len(marks) >= max(0, int(limit)):
            break
    return marks


async def content_frame(page):
    """The frame carrying the most rendered text -- where the reader is looking.

    ALLDATA's refs are frame-scoped (``f8e1370`` is frame 8), so the top page
    is only a shell. Geometry and scrolling must both act on the frame that
    actually holds the procedure, not on the shell around it.
    """
    best = page
    best_length = -1
    for frame in list(getattr(page, "frames", []) or []) or [page]:
        try:
            text = await frame.inner_text("body")
        except Exception:
            continue
        length = len(text or "")
        if length > best_length:
            best_length = length
            best = frame
    return best


async def frame_offsets(page: Any) -> list[tuple[int, Any, int, int]]:
    """(index, frame, offset_x, offset_y) for every frame, main frame first.

    Offsets place a frame's own viewport coordinates in the top viewport, so
    geometry from every frame shares one coordinate space with the screenshot.
    """
    frames = list(getattr(page, "frames", []) or [])
    if not frames:
        return [(0, page, 0, 0)]
    out: list[tuple[int, Any, int, int]] = []
    for index, frame in enumerate(frames):
        offset_x = offset_y = 0
        if index > 0:
            try:
                element = await frame.frame_element()
                box = await element.bounding_box() if element is not None else None
            except Exception:
                box = None
            if not box:
                continue
            offset_x = int(box.get("x") or 0)
            offset_y = int(box.get("y") or 0)
        out.append((index, frame, offset_x, offset_y))
    return out or [(0, page, 0, 0)]


async def enumerate_dom_controls(page: Any, *, limit: int = MAX_DOM_CONTROLS) -> list[DomControl]:
    controls: list[DomControl] = []
    try:
        frames = await frame_offsets(page)
    except Exception:
        frames = [(0, page, 0, 0)]
    for index, frame, offset_x, offset_y in frames:
        if len(controls) >= limit:
            break
        try:
            raw_items = await frame.evaluate(
                DOM_CONTROLS_JS,
                {"maxItems": limit - len(controls), "offsetX": offset_x, "offsetY": offset_y},
            )
        except Exception:
            continue
        for raw in raw_items or []:
            control = control_from_raw(raw, index)
            if control is not None:
                controls.append(control)
            if len(controls) >= limit:
                break
    return controls


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
    last_named = ""
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
            if role not in _INTERACTIVE_ROLES:
                continue
            # Keep the control, and say where it sits: the nearest named thing
            # before it is usually what it acts on, which is how a person reads
            # an unlabelled magnifier sitting next to a search box.
            name = (
                f"unlabeled {role} after '{last_named[:40]}'"
                if last_named
                else f"unlabeled {role}"
            )
        else:
            last_named = name

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


def text_extent(raw: str, *, max_chars: int = MAX_PAGE_TEXT_CHARS) -> tuple[bool, int]:
    """Whether bounded_text had to cut, and the full length it cut from."""
    total = len(" ".join(str(raw or "").split()))
    return total > max_chars, total


async def page_text_all_frames(page: Any) -> str:
    """Rendered text of every frame, unbounded (the caller bounds it)."""
    texts = []
    for frame in list(getattr(page, "frames", []) or []) or [page]:
        try:
            texts.append(await frame.inner_text("body"))
        except Exception:
            continue
    return " ".join(texts)


async def measure_ref_geometry(
    page: Any,
    elements: list[ObservationNode],
    *,
    limit: int = MAX_REF_GEOMETRY,
    window_width: float = 0,
    window_height: float = 0,
) -> list[ObservationNode]:
    """Attach on-screen viewport boxes to the first ``limit`` interactive refs."""
    measured: list[ObservationNode] = []
    budget = max(0, int(limit))
    for node in elements:
        box_value: Optional[dict[str, int]] = None
        if budget > 0 and node.role in _INTERACTIVE_ROLES:
            budget -= 1
            try:
                locator = page.locator(f"aria-ref={node.ref}")
                if await locator.count() >= 1:
                    box = await locator.first.bounding_box()
                else:
                    box = None
            except Exception:
                box = None
            if box:
                x = float(box.get("x") or 0)
                y = float(box.get("y") or 0)
                width = float(box.get("width") or 0)
                height = float(box.get("height") or 0)
                on_screen = width > 0 and height > 0
                if window_width > 0 and (x >= window_width or x + width <= 0):
                    on_screen = False
                if window_height > 0 and (y >= window_height or y + height <= 0):
                    on_screen = False
                if on_screen:
                    box_value = {
                        "x": int(round(x)),
                        "y": int(round(y)),
                        "w": int(round(width)),
                        "h": int(round(height)),
                    }
        measured.append(
            ObservationNode(
                ref=node.ref,
                role=node.role,
                name=node.name,
                depth=node.depth,
                expanded=node.expanded,
                box=box_value,
            )
        )
    return measured


async def window_size(page: Any) -> tuple[int, int]:
    try:
        viewport = await page.evaluate(
            "() => ({width: window.innerWidth || 0, height: window.innerHeight || 0})"
        )
        return int((viewport or {}).get("width") or 0), int((viewport or {}).get("height") or 0)
    except Exception:
        return 0, 0


async def build_observation(
    page: Any,
    *,
    breadcrumb: Optional[list[str]] = None,
    marks: bool = False,
) -> Observation:
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
        raw_text = await page_text_all_frames(page)
    except Exception:
        raw_text = ""

    try:
        title = await page.title()
    except Exception:
        title = ""

    try:
        geometry = await (await content_frame(page)).evaluate(SCROLLER_JS)
    except Exception:
        geometry = {}

    width, height = await window_size(page)
    elements = await measure_ref_geometry(
        page, elements, window_width=width, window_height=height
    )
    controls = await enumerate_dom_controls(page)
    truncated, total_chars = text_extent(raw_text)
    url = str(page.url or "")
    observation = Observation(
        url=url,
        title=title,
        elements=elements,
        page_text=bounded_text(raw_text),
        breadcrumb=list(breadcrumb or []),
        page_text_truncated=truncated,
        page_text_total_chars=total_chars,
        scroll_y=int((geometry or {}).get("scrollY") or 0),
        scroll_height=int((geometry or {}).get("scrollHeight") or 0),
        viewport_height=int((geometry or {}).get("innerHeight") or 0),
        observation_id=new_observation_id(),
        page_identity=page_identity_of(url, title),
        window_width=width,
        window_height=height,
        controls=controls,
        marks=select_marks(elements, controls) if marks else [],
    )
    return observation


async def plain_viewport_png(page: Any) -> bytes:
    """The current viewport with nothing drawn over it, as PNG."""
    return await page.screenshot(type="png", full_page=False)


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

    Only refs from the exact cached Observation are eligible for labels, using
    the boxes measured when that observation was built. A ref that was not
    on screen then is skipped rather than re-located by text/role guesswork.
    Marks, when the observation carries them, are drawn in a second colour
    with their ``[mN]`` numbers.
    """
    labels: list[dict[str, Any]] = []
    for element in observation.elements:
        if len(labels) >= max(0, int(max_labels)):
            break
        if element.box is None:
            continue
        labels.append(
            {
                "ref": element.ref,
                "x": element.box["x"],
                "y": element.box["y"],
                "width": element.box["w"],
                "height": element.box["h"],
                "kind": "ref",
            }
        )
    for mark in observation.marks:
        labels.append(
            {
                "ref": f"m{mark.mark}",
                "x": mark.control.x,
                "y": mark.control.y,
                "width": mark.control.w,
                "height": mark.control.h,
                "kind": "mark",
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
                        const color = item.kind === 'mark' ? '#ffb000' : '#ff2d55';
                        const box = document.createElement('div');
                        box.style.position = 'fixed';
                        box.style.left = Math.max(0, item.x) + 'px';
                        box.style.top = Math.max(0, item.y) + 'px';
                        box.style.width = Math.max(2, item.width) + 'px';
                        box.style.height = Math.max(2, item.height) + 'px';
                        box.style.border = '2px solid ' + color;
                        box.style.borderRadius = '3px';
                        box.style.boxSizing = 'border-box';

                        const label = document.createElement('div');
                        label.textContent = '[' + item.ref + ']';
                        label.style.position = 'absolute';
                        label.style.left = '-2px';
                        label.style.top = '-20px';
                        label.style.padding = '1px 4px';
                        label.style.background = item.kind === 'mark' ? '#3a2a00' : '#111';
                        label.style.color = '#fff';
                        label.style.border = '1px solid ' + color;
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
