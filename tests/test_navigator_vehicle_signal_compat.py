import pytest

from scrapex import alldata_navigator
from scrapex import alldata as alldata_heuristics


TARGET = {"year": 2020, "make": "BENZ", "model": "E 350"}
MATCH = "2020 Mercedes Benz E 350 4MATIC Sedan (213.084)"


@pytest.mark.asyncio
async def test_url_less_legacy_adapter_keeps_aria_fallback(monkeypatch):
    async def no_dom_match(page, vehicle):  # noqa: ARG001
        return {"verified": False, "label": None, "candidates": []}

    async def aria_match(page):  # noqa: ARG001
        return [MATCH]

    monkeypatch.setattr(alldata_heuristics, "verify_selected_vehicle", no_dom_match)
    monkeypatch.setattr(alldata_navigator, "_aria_signal_candidates", aria_match)

    provider = alldata_navigator.AlldataNavigatorProvider("https://my.alldata.com/")
    result = await provider.target_signal(object(), TARGET)

    assert result["selected"] is True
    assert result["proof"] == "legacy_nonbrowser_aria_signal"


@pytest.mark.asyncio
async def test_real_picker_page_does_not_accept_matching_aria_choice(monkeypatch):
    class LivePickerPage:
        url = "https://my.alldata.com/repair/#/select-vehicle"

    async def no_dom_match(page, vehicle):  # noqa: ARG001
        return {"verified": False, "label": None, "candidates": []}

    async def aria_match(page):  # noqa: ARG001
        return [MATCH]

    monkeypatch.setattr(alldata_heuristics, "verify_selected_vehicle", no_dom_match)
    monkeypatch.setattr(alldata_navigator, "_aria_signal_candidates", aria_match)

    provider = alldata_navigator.AlldataNavigatorProvider("https://my.alldata.com/")
    result = await provider.target_signal(LivePickerPage(), TARGET)

    assert result["selected"] is False
    assert result["proof"] == "picker_not_identity"
