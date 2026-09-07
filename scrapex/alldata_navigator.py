"""ALLDATA provider adapter for the Navigator.

Ports the existing, dormant vehicle-identity heuristics in ``alldata.py``
(``verify_selected_vehicle``, ``vehicle_matches``) rather than re-implementing
a fourth version of vehicle matching -- ScrapeX already has one, it was just
never wired into a live path.
"""

from __future__ import annotations

import re
from typing import Any

from . import alldata as alldata_heuristics
from .models import VehicleSpec
from .navigator_observation import build_observation

_STOPWORDS = frozenset({
    "the", "and", "for", "with", "this", "that", "from", "into", "your",
    "procedure", "calibration", "system",
})

# Provider terminology varies by OEM and even by article family. These
# semantic groups are verification aliases, not routing rules: the model
# still decides where to navigate from the live page. They simply let the
# deterministic evidence gate recognize that e.g. "Beam Axis Adjustment"
# can satisfy a request phrased as "calibration".
_CONCEPT_GROUPS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "operation:calibration",
        re.compile(
            r"\b(?:calibrat\w*|recalibrat\w*|aim\w*|align\w*|adjust\w*|"
            r"initializ\w*|relearn\w*|reset\w*|set\s*up|setup|learn\w*|"
            r"register\w*|zero[-\s]?point|memor(?:ize|ization)\w*)\b",
            re.I,
        ),
    ),
    (
        "system:blind_spot",
        re.compile(
            r"\b(?:blind\s*spot|bsm|bsd|rear\s*(?:corner|side)\s*radar|"
            r"side\s*radar|lane\s*change\s*assist|cross\s*traffic)\b",
            re.I,
        ),
    ),
    (
        "system:front_radar",
        re.compile(
            r"\b(?:front|forward|millimeter\s*wave)\s*radar\b|"
            r"\b(?:distance\s*sensor|adaptive\s*cruise|cruise\s*control\s*module|ccm)\b",
            re.I,
        ),
    ),
    (
        "system:forward_camera",
        re.compile(
            r"\b(?:forward|front|windshield|monocular|recognition)\s*(?:facing\s*)?camera\b|"
            r"\b(?:lane\s*(?:keep|keeping|departure)|image\s*processing\s*module|ipma)\b",
            re.I,
        ),
    ),
    (
        "system:rear_camera",
        re.compile(
            r"\b(?:rear|backup|back|surround|around\s*view|360|parking\s*assist)\s*(?:view\s*)?camera\b",
            re.I,
        ),
    ),
    (
        "system:steering_angle",
        re.compile(r"\b(?:steering\s*(?:angle|center)|sas|neutral\s*point)\b", re.I),
    ),
    (
        "system:occupant",
        re.compile(
            r"\b(?:occupant\s*classification|ocs|passenger\s*presence|seat\s*weight|weight\s*sensor)\b",
            re.I,
        ),
    ),
    (
        "system:parking_sensor",
        re.compile(r"\b(?:parking\s*aid|park\s*assist|ultrasonic|sonar|parking\s*sensor)\b", re.I),
    ),
    (
        "system:lidar",
        re.compile(r"\b(?:lidar|laser\s*(?:radar|sensor))\b", re.I),
    ),
)


async def _aria_signal_candidates(page: Any) -> list[str]:
    """Fallback candidate source using the same aria-snapshot mechanism the
    Navigator's own observation already uses reliably.

    Confirmed live: alldata.py's selected_vehicle_signal (its CSS-selector
    scan plus "Change/Selected/Current Vehicle" text patterns) was written
    against an older ALLDATA layout and finds zero candidates on the
    current live UI even when a vehicle is plainly selected in the header
    -- it silently falls back to just the page <title>, which never
    contains vehicle text. The aria-snapshot element list does contain it.
    """
    observation = await build_observation(page)
    return [element.name for element in observation.elements if element.name]


class AlldataNavigatorProvider:
    slug = "alldata"

    def __init__(self, home_url: str):
        self.home_url = home_url
        self.allowed_domain_suffixes = ("alldata.com",)

    async def authenticated(self, page: Any) -> bool:
        """Fail closed: a title-only check is not proof.

        Confirmed live against a fresh, never-signed-in profile: ALLDATA's
        login page's own <title> is just "ALLDATA" -- it contains neither
        "login" nor "sign in" -- so a title-substring check alone reports
        "authenticated" while a password field is plainly on screen. A
        visible password input or a "Log In" control is the actual signal.
        """
        try:
            password_field = page.locator("input[type='password']").first
            if await password_field.is_visible(timeout=500):
                return False
        except Exception:
            pass
        try:
            login_control = page.get_by_text(re.compile(r"\bLog\s*In\b", re.I)).first
            if await login_control.is_visible(timeout=400):
                return False
        except Exception:
            pass
        try:
            title = (await page.title() or "").casefold()
        except Exception:
            return False
        return "login" not in title and "sign in" not in title

    async def target_signal(self, page: Any, target: dict[str, Any]) -> dict[str, Any]:
        vehicle = VehicleSpec(
            year=target.get("year"),
            make=target.get("make") or "",
            model=target.get("model") or "",
            trim=target.get("trim"),
            vin=target.get("vin"),
        )
        result = await alldata_heuristics.verify_selected_vehicle(page, vehicle)
        if result.get("verified"):
            return {"selected": True, "reason": None, "label": result.get("label")}
        for candidate in await _aria_signal_candidates(page):
            if alldata_heuristics.vehicle_matches(candidate, vehicle):
                return {"selected": True, "reason": None, "label": candidate}
        return {
            "selected": False,
            "reason": "ALLDATA vehicle selection was not confirmed.",
            "label": result.get("label"),
        }

    async def current_page_signals(self, page: Any) -> list[str]:
        """Bounded, generic "what vehicle is on screen" text signals.

        Not bound to any specific candidate vehicle -- callers (e.g.
        Calibration IQ work-prep matching) check many candidate rows against
        this same bounded signal list, mirroring the synchronous read this
        replaced.
        """
        signal = await alldata_heuristics.selected_vehicle_signal(page)
        candidates = list(signal.get("candidates") or [])
        if not candidates:
            candidates = await _aria_signal_candidates(page)
        return candidates

    def is_search_action(self, action: dict[str, Any]) -> bool:
        kind = action.get("action")
        if kind == "fill":
            return True
        if kind == "press" and str(action.get("key") or "").casefold() == "enter":
            return True
        return False

    def match_terms(self, text: str, topic: str) -> tuple[list[str], int]:
        topic_text = str(topic or "")
        page_text = str(text or "")
        words = {
            w for w in re.findall(r"[a-z0-9]+", topic_text.casefold())
            if len(w) >= 3 and w not in _STOPWORDS
        }
        folded = page_text.casefold()
        matched = {w for w in words if w in folded}

        # Add semantic concept matches so verification follows user intent
        # rather than requiring ALLDATA to use the exact same article title.
        for label, pattern in _CONCEPT_GROUPS:
            if pattern.search(topic_text) and pattern.search(page_text):
                matched.add(label)

        ordered = sorted(matched)
        return ordered, len(ordered)
