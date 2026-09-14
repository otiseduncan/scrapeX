from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from scrapex.alldata_navigator import AlldataNavigatorProvider
from scrapex import navigator_vehicle_identity as guard


class _Keyboard:
    def __init__(self, page):
        self.page = page
        self.typed: list[str] = []

    async def press(self, key):  # noqa: ARG002
        return None

    async def type(self, text, delay=None):  # noqa: ARG002
        self.typed.append(text)


class _SearchBox:
    def __init__(self):
        self.first = self

    async def is_visible(self, timeout=None):  # noqa: ARG002
        return True

    async def click(self, timeout=None):  # noqa: ARG002
        return None


class _Page:
    def __init__(self, url="https://my.alldata.com/repair/#/home"):
        self.url = url
        self.keyboard = _Keyboard(self)
        self.goto_calls: list[str] = []

    async def goto(self, url, wait_until=None):  # noqa: ARG002
        self.goto_calls.append(url)
        self.url = url

    async def wait_for_timeout(self, ms):  # noqa: ARG002
        return None

    def locator(self, selector):  # noqa: ARG002
        return _SearchBox()


class _Candidate:
    def __init__(self, page, *, href: str | None, final_url: str):
        self.page = page
        self.href = href
        self.final_url = final_url
        self.clicked = False

    async def get_attribute(self, name):
        return self.href if name == "href" else None

    async def click(self, timeout=None):  # noqa: ARG002
        self.clicked = True
        self.page.url = self.final_url


@pytest.mark.asyncio
async def test_picker_result_text_is_never_selected_vehicle_proof(monkeypatch):
    provider = AlldataNavigatorProvider("https://my.alldata.com/")
    page = _Page("https://my.alldata.com/repair/#/select-vehicle")

    async def observation(_page):
        return SimpleNamespace(
            elements=[SimpleNamespace(role="link", name="2025 Ford Explorer")]
        )

    monkeypatch.setattr(guard, "build_observation", observation)
    result = await provider.target_signal(
        page, {"year": 2025, "make": "Ford", "model": "Explorer"}
    )
    assert result["selected"] is False
    assert result["proof"] == "picker_not_identity"


@pytest.mark.asyncio
async def test_recent_vehicle_link_on_wrong_vehicle_is_not_identity_proof(monkeypatch):
    provider = AlldataNavigatorProvider("https://my.alldata.com/")
    page = _Page("https://my.alldata.com/repair/#/vehicle/toyota-camry")

    async def observation(_page):
        return SimpleNamespace(
            elements=[
                SimpleNamespace(role="link", name="2025 Ford Explorer"),
                SimpleNamespace(role="heading", name="2025 Toyota Camry"),
            ]
        )

    monkeypatch.setattr(guard, "build_observation", observation)
    result = await provider.target_signal(
        page, {"year": 2025, "make": "Ford", "model": "Explorer"}
    )
    assert result["selected"] is False
    assert result["proof"] == "ymm_not_proven"


@pytest.mark.asyncio
async def test_ymm_fast_path_types_exact_query_clicks_candidate_and_records_proof(monkeypatch):
    provider = AlldataNavigatorProvider("https://my.alldata.com/")
    page = _Page()
    candidate = _Candidate(
        page,
        href="https://my.alldata.com/repair/#/vehicle/ford-explorer-2025",
        final_url="https://my.alldata.com/repair/#/vehicle/ford-explorer-2025",
    )

    async def candidates(_page, vehicle):
        assert vehicle.year == 2025
        assert vehicle.make == "Ford"
        assert vehicle.model == "Explorer"
        return [(10, "2025 Ford Explorer", candidate, candidate.href)]

    async def strict(_page, _target):
        return {"selected": False, "reason": "no header needed when href route is exact"}

    monkeypatch.setattr(guard, "_candidate_rows", candidates)
    monkeypatch.setattr(guard, "_strict_page_signal", strict)

    result = await provider.select_vehicle(
        page,
        {
            "text": json.dumps(
                {"year": 2025, "make": "Ford", "model": "Explorer"}
            )
        },
    )
    assert result["selected"] is True
    assert result["identity_mode"] == "year_make_model"
    assert result["vehicle_id"] == "ford-explorer-2025"
    assert candidate.clicked is True
    assert page.keyboard.typed == ["2025 Ford Explorer"]
    assert page.goto_calls == [guard.PICKER_URL]

    proof = await provider.target_signal(
        page, {"year": 2025, "make": "Ford", "model": "Explorer"}
    )
    assert proof["selected"] is True
    assert proof["proof"] == "mechanical_selection_receipt"


@pytest.mark.asyncio
async def test_ymm_fast_path_refuses_ambiguous_variants(monkeypatch):
    provider = AlldataNavigatorProvider("https://my.alldata.com/")
    page = _Page()
    first = _Candidate(page, href="#/vehicle/a", final_url="#/vehicle/a")
    second = _Candidate(page, href="#/vehicle/b", final_url="#/vehicle/b")

    async def candidates(_page, _vehicle):
        return [
            (10, "2025 Ford Explorer 2.3L", first, first.href),
            (10, "2025 Ford Explorer 3.0L", second, second.href),
        ]

    monkeypatch.setattr(guard, "_candidate_rows", candidates)
    result = await provider.select_vehicle(
        page,
        {
            "text": json.dumps(
                {"year": 2025, "make": "Ford", "model": "Explorer"}
            )
        },
    )
    assert result["selected"] is False
    assert result["needs_operator"] is True
    assert len(result["candidates"]) == 2
    assert first.clicked is False and second.clicked is False


@pytest.mark.asyncio
async def test_different_mechanical_receipt_never_proves_next_vehicle():
    provider = AlldataNavigatorProvider("https://my.alldata.com/")
    provider._x_selected_identity = {
        "key": ("ymm", 2025, "toyota", "camry", ""),
        "mode": "year_make_model",
        "label": "2025 Toyota Camry",
    }
    page = _Page("https://my.alldata.com/repair/#/select-vehicle")
    result = await provider.target_signal(
        page, {"year": 2025, "make": "Ford", "model": "Explorer"}
    )
    assert result["selected"] is False
