"""Compatibility boundary for ALLDATA vehicle target signals.

The production Navigator now requires strict, route-aware vehicle identity on
real Playwright pages so picker results and Recent Vehicles controls cannot be
mistaken for the selected vehicle.  A small number of older unit adapters call
``target_signal`` with page-like objects that intentionally do not expose a
browser URL and rely on the historical ARIA fallback.

Keep that non-browser compatibility path without weakening live execution:
real pages (anything exposing ``url``) keep the strict identity result installed
by ``navigator_vehicle_identity``.  Only URL-less adapters may fall back to the
legacy DOM/ARIA signal reader.
"""

from __future__ import annotations

from typing import Any

from . import alldata as alldata_heuristics
from . import alldata_navigator
from .alldata_navigator import AlldataNavigatorProvider
from .models import VehicleSpec

_INSTALLED_ATTR = "__scrapex_vehicle_signal_compat_v1__"


def install() -> None:
    if getattr(AlldataNavigatorProvider, _INSTALLED_ATTR, False):
        return

    strict_target_signal = AlldataNavigatorProvider.target_signal

    async def target_signal(
        self: AlldataNavigatorProvider,
        page: Any,
        target: dict[str, Any],
    ) -> dict[str, Any]:
        strict = await strict_target_signal(self, page, target)
        if strict.get("selected") is True or hasattr(page, "url"):
            return strict

        vehicle = VehicleSpec(
            year=target.get("year"),
            make=target.get("make") or "",
            model=target.get("model") or "",
            trim=target.get("trim"),
            vin=target.get("vin"),
        )
        result = await alldata_heuristics.verify_selected_vehicle(page, vehicle)
        if result.get("verified"):
            return {
                "selected": True,
                "reason": None,
                "label": result.get("label"),
                "proof": "legacy_nonbrowser_dom_signal",
            }
        for candidate in await alldata_navigator._aria_signal_candidates(page):
            if alldata_heuristics.vehicle_matches(candidate, vehicle):
                return {
                    "selected": True,
                    "reason": None,
                    "label": candidate,
                    "proof": "legacy_nonbrowser_aria_signal",
                }
        return strict

    AlldataNavigatorProvider.target_signal = target_signal
    setattr(AlldataNavigatorProvider, _INSTALLED_ATTR, True)


install()
