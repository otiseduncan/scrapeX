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
        words = {
            w for w in re.findall(r"[a-z0-9]+", str(topic or "").casefold())
            if len(w) >= 3 and w not in _STOPWORDS
        }
        folded = str(text or "").casefold()
        matched = sorted(w for w in words if w in folded)
        return matched, len(matched)
