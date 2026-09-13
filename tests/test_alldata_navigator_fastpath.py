"""The ALLDATA provider's mechanical fast paths: titles and exact-VIN selection."""

from __future__ import annotations

import pytest

from scrapex.alldata_navigator import AlldataNavigatorProvider


def test_display_title_strips_provider_furniture_only():
    provider = AlldataNavigatorProvider("https://my.alldata.com/")
    assert provider.display_title(
        "Vehicle Information - 2021 Honda Civic Sedan L4-2.0L - ALLDATA Collision"
    ) == "2021 Honda Civic Sedan L4-2.0L"
    assert provider.display_title(
        "Millimeter Wave Radar Aiming (Collision Avoidance Sensor) - ALLDATA Collision"
    ) == "Millimeter Wave Radar Aiming (Collision Avoidance Sensor)"
    assert provider.display_title("  Plain   title  ") == "Plain title"


class _VinPage:
    """A picker that resolves a typed VIN to a vehicle page, the way ALLDATA does."""

    def __init__(
        self,
        *,
        resolves: bool = True,
        search_box: bool = True,
        resolved_vin: str | None = None,
    ):
        self.url = "https://my.alldata.com/repair/#/home"
        self._resolves = resolves
        self._search_box = search_box
        self.typed: list[str] = []
        self.goto_calls: list[str] = []
        self.resolved_vin = resolved_vin
        self.keyboard = self

    async def goto(self, url, wait_until=None):
        self.goto_calls.append(url)
        self.url = url

    async def wait_for_timeout(self, ms):
        if self.typed and self._resolves:
            self.url = "https://my.alldata.com/repair/#/vehicle/62139"

    def locator(self, selector):
        page = self

        class _Box:
            def __init__(self):
                self.first = self

            async def is_visible(self, timeout=None):
                return page._search_box and selector == "input[type='search']"

            async def click(self, timeout=None):
                return None

            async def inner_text(self, timeout=None):
                if selector != "body":
                    return ""
                return page.resolved_vin or (page.typed[-1] if page.typed else "")

        return _Box()

    async def type(self, text, delay=None):
        self.typed.append(text)

    async def title(self):
        return "Vehicle Information - 2021 Honda Civic Sedan L4-2.0L - ALLDATA Collision"


@pytest.mark.asyncio
async def test_select_vehicle_by_vin_types_keystrokes_and_reports_the_resolved_vehicle():
    provider = AlldataNavigatorProvider("https://my.alldata.com/")
    page = _VinPage()
    result = await provider.select_vehicle(page, {"vin": " 2hgfc2f59mh500001 "})
    assert result["selected"] is True
    assert result["vin"] == "2HGFC2F59MH500001"
    assert result["label"] == "2021 Honda Civic Sedan L4-2.0L"
    assert page.typed == ["2HGFC2F59MH500001"]
    assert page.goto_calls == [provider.picker_url]


@pytest.mark.asyncio
async def test_select_vehicle_reports_an_invalid_vin_and_an_unresolved_one_truthfully():
    provider = AlldataNavigatorProvider("https://my.alldata.com/")
    bad = await provider.select_vehicle(_VinPage(), {"vin": "NOT-A-VIN"})
    assert bad["selected"] is False and "17-character VIN" in bad["reason"]

    unresolved = await provider.select_vehicle(
        _VinPage(resolves=False), {"vin": "2HGFC2F59MH500001"}
    )
    assert unresolved["selected"] is False
    assert "did not resolve" in unresolved["reason"]

    no_box = await provider.select_vehicle(
        _VinPage(search_box=False), {"vin": "2HGFC2F59MH500001"}
    )
    assert no_box["selected"] is False and "search box" in no_box["reason"]

    wrong_vehicle = await provider.select_vehicle(
        _VinPage(resolved_vin="1HGCY2F73RA095331"),
        {"vin": "1HGCY1F35PA033515"},
    )
    assert wrong_vehicle["selected"] is False
    assert "did not show the requested VIN" in wrong_vehicle["reason"]


def test_type_and_select_vehicle_count_as_target_scoped_searches():
    provider = AlldataNavigatorProvider("https://my.alldata.com/")
    assert provider.is_search_action({"action": "type", "ref": "e1", "text": "x"}) is True
    assert provider.is_search_action({"action": "select_vehicle", "vin": "x"}) is True
    assert provider.is_search_action({"action": "click_visual"}) is False
