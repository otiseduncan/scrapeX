"""HTTP-layer tests for the Navigator endpoints in ``scrapex/main.py``.

Uses fakes for the browser/provider so these run fast and in isolation --
the real end-to-end navigation logic (parsing, graph, verification) is
already covered by ``test_navigator_api.py`` against a live headless
browser and the local fixture site. This file only proves the HTTP
contract: routes exist, request/response shapes are right, and errors map
to sane status codes.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from scrapex.db import Store
from scrapex.main import AppServices, create_app


class FakeRefLocator:
    """Stands in for ``page.locator(f"aria-ref={ref}")``."""

    def __init__(self, page: "FakePage", ref: str):
        self._page = page
        self.ref = ref

    async def count(self):
        return 1 if self.ref in {"e1", "e2"} else 0

    @property
    def first(self):
        return self

    async def click(self, timeout=None):
        self._page.filled_or_clicked_refs.append(("click", self.ref))

    async def fill(self, text, timeout=None):
        self._page.filled_or_clicked_refs.append(("fill", self.ref, text))

    async def press(self, key, timeout=None):
        self._page.filled_or_clicked_refs.append(("press", self.ref, key))


class FakePage:
    def __init__(self):
        self.url = "https://fake.alldata.com/current"
        self.frames = [self]
        self._elements_yaml = (
            '- textbox "Vehicle search" [ref=e1]\n'
            '- button "Search" [ref=e2]\n'
        )
        self.clicks: list[tuple[float, float]] = []
        self.typed: list[str] = []
        self.pressed: list[str] = []
        self.filled_or_clicked_refs: list[tuple] = []
        self.mouse = SimpleNamespace(click=self._mouse_click)
        self.keyboard = SimpleNamespace(type=self._kb_type, press=self._kb_press)

    def locator(self, selector):
        if selector.startswith("aria-ref="):
            return FakeRefLocator(self, selector.split("=", 1)[1])
        return SimpleNamespace(aria_snapshot=self._aria_snapshot)

    async def _aria_snapshot(self, mode=None):
        return self._elements_yaml

    async def inner_text(self, selector):
        return "2023 Toyota Camry blind spot monitor calibration"

    async def title(self):
        return "Fake ALLDATA"

    async def screenshot(self, type="png"):
        return b"\x89PNG\r\n\x1a\nfake"

    async def wait_for_timeout(self, ms):
        pass

    async def _mouse_click(self, x, y):
        self.clicks.append((x, y))

    async def _kb_type(self, text):
        self.typed.append(text)

    async def _kb_press(self, key):
        self.pressed.append(key)


class FakeNavigatorBrowserManager:
    def __init__(self):
        self.pages: dict[str, FakePage] = {}
        self.page_for_calls: list[str] = []

    async def page_for(self, provider_slug, *, home_url):
        self.page_for_calls.append(provider_slug)
        return self.pages.setdefault(provider_slug, FakePage())

    async def close(self, provider_slug=None):
        pass


class FakeProvider:
    slug = "alldata"
    home_url = "https://fake.alldata.com/"
    allowed_domain_suffixes = ("fake.alldata.com",)

    async def authenticated(self, page):
        return True

    async def target_signal(self, page, target):
        selected = bool(target.get("year")) and bool(target.get("make"))
        return {"selected": selected, "reason": None if selected else "No target given."}

    async def current_page_signals(self, page):
        return ["2023 Toyota Camry - Fake ALLDATA"]

    def is_search_action(self, action):
        return action.get("action") == "fill"

    def match_terms(self, text, topic):
        words = [w for w in str(topic or "").casefold().split() if w in str(text or "").casefold()]
        return words, len(words)


async def _fake_status():
    return {"reachable": True, "authorized": True, "active": True, "authenticated": True}


def make_services(tmp_path: Path) -> AppServices:
    store = Store(tmp_path / "db.sqlite")
    return AppServices(
        settings=SimpleNamespace(data_root=tmp_path / "data", adas_si_root=tmp_path / "ADAS SI"),
        store=store,
        ciq=SimpleNamespace(status=_fake_status),
        work_chrome=SimpleNamespace(),
        adas_map_source=SimpleNamespace(status=_fake_status),
        adas_map_runner=SimpleNamespace(),
        navigator_manager=FakeNavigatorBrowserManager(),
        navigator_providers={"alldata": FakeProvider()},
    )


def test_health_lists_navigator_providers(tmp_path: Path):
    services = make_services(tmp_path)
    with TestClient(create_app(services)) as client:
        body = client.get("/api/health").json()
    assert body["navigator"]["providers"] == ["alldata"]


def test_create_task_unknown_provider_is_404(tmp_path: Path):
    services = make_services(tmp_path)
    with TestClient(create_app(services)) as client:
        response = client.post(
            "/api/navigator/tasks",
            json={"provider": "nope", "target": {}, "topic": "topic"},
        )
    assert response.status_code == 404


def test_get_unknown_task_is_404(tmp_path: Path):
    services = make_services(tmp_path)
    with TestClient(create_app(services)) as client:
        response = client.get("/api/navigator/tasks/does-not-exist")
    assert response.status_code == 404


def test_full_task_lifecycle_over_http(tmp_path: Path):
    services = make_services(tmp_path)
    with TestClient(create_app(services)) as client:
        created = client.post(
            "/api/navigator/tasks",
            json={
                "provider": "alldata",
                "target": {"year": 2023, "make": "Toyota", "model": "Camry"},
                "topic": "blind spot monitor calibration",
                "action_budget": 10,
            },
        )
        assert created.status_code == 200
        task_id = created.json()["task_id"]

        fetched = client.get(f"/api/navigator/tasks/{task_id}")
        assert fetched.status_code == 200
        assert fetched.json()["provider"] == "alldata"

        observed = client.post(f"/api/navigator/tasks/{task_id}/observe")
        assert observed.status_code == 200
        elements = observed.json()["elements"]
        assert {"ref": "e1", "role": "textbox", "name": "Vehicle search", "expanded": None} in elements
        search_box_ref = next(e["ref"] for e in elements if e["role"] == "textbox")

        acted = client.post(
            f"/api/navigator/tasks/{task_id}/act",
            json={"action": "fill", "ref": search_box_ref, "text": "Camry"},
        )
        assert acted.status_code == 200
        assert acted.json()["is_search_action"] is True

        verified = client.post(f"/api/navigator/tasks/{task_id}/verify")
        assert verified.status_code == 200
        assert "verified" in verified.json()

        evidence = client.get(f"/api/navigator/tasks/{task_id}/evidence")
        assert evidence.status_code == 200
        assert evidence.json()["task_id"] == task_id


def test_task_screenshot_requires_an_observation(tmp_path: Path):
    services = make_services(tmp_path)
    with TestClient(create_app(services)) as client:
        task_id = client.post(
            "/api/navigator/tasks",
            json={"provider": "alldata", "target": {}, "topic": "topic"},
        ).json()["task_id"]
        response = client.get(f"/api/navigator/tasks/{task_id}/screenshot")
    assert response.status_code == 409


def test_act_with_unknown_ref_is_422(tmp_path: Path):
    services = make_services(tmp_path)
    with TestClient(create_app(services)) as client:
        task_id = client.post(
            "/api/navigator/tasks",
            json={"provider": "alldata", "target": {}, "topic": "topic"},
        ).json()["task_id"]
        client.post(f"/api/navigator/tasks/{task_id}/observe")

        response = client.post(
            f"/api/navigator/tasks/{task_id}/act",
            json={"action": "click", "ref": "e999"},
        )
    assert response.status_code == 422


def test_act_on_unknown_task_is_404(tmp_path: Path):
    services = make_services(tmp_path)
    with TestClient(create_app(services)) as client:
        response = client.post(
            "/api/navigator/tasks/does-not-exist/act",
            json={"action": "extract"},
        )
    assert response.status_code == 404


def test_current_target_signal_reads_without_a_task(tmp_path: Path):
    services = make_services(tmp_path)
    with TestClient(create_app(services)) as client:
        response = client.get(
            "/api/navigator/providers/alldata/current-target-signal",
            params={"year": 2023, "make": "Toyota", "model": "Camry"},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["selected"] is True
    assert body["target"]["make"] == "Toyota"


def test_current_page_signals_reads_without_a_task(tmp_path: Path):
    services = make_services(tmp_path)
    with TestClient(create_app(services)) as client:
        response = client.get("/api/navigator/providers/alldata/current-page-signals")
    assert response.status_code == 200
    body = response.json()
    assert body["authenticated"] is True
    assert body["signals"] == ["2023 Toyota Camry - Fake ALLDATA"]


def test_current_page_signals_unknown_provider_is_404(tmp_path: Path):
    services = make_services(tmp_path)
    with TestClient(create_app(services)) as client:
        response = client.get("/api/navigator/providers/nope/current-page-signals")
    assert response.status_code == 404


def test_current_target_signal_unknown_provider_is_404(tmp_path: Path):
    services = make_services(tmp_path)
    with TestClient(create_app(services)) as client:
        response = client.get("/api/navigator/providers/nope/current-target-signal")
    assert response.status_code == 404


def test_screenshot_returns_png_bytes(tmp_path: Path):
    services = make_services(tmp_path)
    with TestClient(create_app(services)) as client:
        response = client.get("/api/navigator/providers/alldata/screenshot")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.content.startswith(b"\x89PNG")


def test_remote_input_click_and_type(tmp_path: Path):
    services = make_services(tmp_path)
    page = services.navigator_manager
    with TestClient(create_app(services)) as client:
        click_response = client.post(
            "/api/navigator/providers/alldata/input",
            json={"kind": "click", "x": 12, "y": 34},
        )
        assert click_response.status_code == 200
        type_response = client.post(
            "/api/navigator/providers/alldata/input",
            json={"kind": "type", "text": "hello"},
        )
        assert type_response.status_code == 200
        missing_text_response = client.post(
            "/api/navigator/providers/alldata/input",
            json={"kind": "type"},
        )
        assert missing_text_response.status_code == 422

    fake_page = page.pages["alldata"]
    assert fake_page.clicks == [(12, 34)]
    assert fake_page.typed == ["hello"]
