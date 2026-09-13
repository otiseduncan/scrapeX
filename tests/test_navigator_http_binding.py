"""HTTP contract for observation identity, marks, bound actions, and extract.

Built on the same fakes as ``test_navigator_http_api.py``; this file only
proves the wire shapes the caller depends on for binding an action to the
exact observation it was chosen from.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from scrapex.main import create_app
from tests.test_navigator_http_api import make_services


def _task(client: TestClient) -> str:
    return client.post(
        "/api/navigator/tasks",
        json={"provider": "alldata", "target": {}, "topic": "topic"},
    ).json()["task_id"]


def test_observe_can_request_marks_and_reports_the_observation_identity(tmp_path: Path):
    services = make_services(tmp_path)
    with TestClient(create_app(services)) as client:
        task_id = _task(client)
        plain = client.post(f"/api/navigator/tasks/{task_id}/observe").json()
        marked = client.post(
            f"/api/navigator/tasks/{task_id}/observe", json={"marks": True}
        ).json()
    assert plain["observation_id"].startswith("obs_")
    assert plain["page_identity"]
    assert plain["viewport"] == {"width": 0, "height": 0}
    assert "marks" not in plain
    assert marked["observation_id"] != plain["observation_id"]
    # The fake page exposes no DOM controls, so a marks request truthfully
    # offers none rather than inventing labels.
    assert marked.get("marks", []) == []
    assert marked["controls_without_refs"] == 0


def test_act_from_a_superseded_observation_is_a_409_with_the_current_id(tmp_path: Path):
    services = make_services(tmp_path)
    with TestClient(create_app(services)) as client:
        task_id = _task(client)
        first = client.post(f"/api/navigator/tasks/{task_id}/observe").json()
        second = client.post(f"/api/navigator/tasks/{task_id}/observe").json()
        response = client.post(
            f"/api/navigator/tasks/{task_id}/act",
            json={"action": "click", "ref": "e1", "observation_id": first["observation_id"]},
        )
        assert response.status_code == 409
        detail = response.json()["detail"]
        assert detail["code"] == "stale_observation"
        assert detail["current_observation_id"] == second["observation_id"]
        # Bound to the current observation the same click proceeds.
        acted = client.post(
            f"/api/navigator/tasks/{task_id}/act",
            json={"action": "click", "ref": "e1", "observation_id": second["observation_id"]},
        )
        assert acted.status_code == 200
        assert acted.json()["action_target"]["ref"] == "e1"
        assert acted.json()["observation_id"] not in {
            first["observation_id"], second["observation_id"]
        }
    page = services.navigator_manager.pages["alldata"]
    assert ("click", "e1") in page.filled_or_clicked_refs


def test_click_mark_and_click_visual_never_run_blind(tmp_path: Path):
    services = make_services(tmp_path)
    with TestClient(create_app(services)) as client:
        task_id = _task(client)
        observed = client.post(f"/api/navigator/tasks/{task_id}/observe").json()
        unbound = client.post(
            f"/api/navigator/tasks/{task_id}/act",
            json={"action": "click_mark", "mark": 21},
        )
        assert unbound.status_code == 422
        no_such_mark = client.post(
            f"/api/navigator/tasks/{task_id}/act",
            json={"action": "click_mark", "mark": 21, "observation_id": observed["observation_id"]},
        )
        assert no_such_mark.status_code == 422
        no_viewport = client.post(
            f"/api/navigator/tasks/{task_id}/act",
            json={
                "action": "click_visual", "x_norm": 0.5, "y_norm": 0.5,
                "observation_id": observed["observation_id"],
            },
        )
        # No viewport on the fake page, so a normalized point has nowhere to go.
        assert no_viewport.status_code == 422
    page = services.navigator_manager.pages["alldata"]
    assert page.clicks == []


def test_screenshot_echoes_the_observation_it_belongs_to(tmp_path: Path):
    services = make_services(tmp_path)
    with TestClient(create_app(services)) as client:
        task_id = _task(client)
        observed = client.post(f"/api/navigator/tasks/{task_id}/observe").json()
        page = services.navigator_manager.pages["alldata"]

        async def tolerant_screenshot(type="png", full_page=False, quality=None):
            if type == "png":
                return b"\x89PNG\r\n\x1a\nfake"
            return b"\xff\xd8\xfffake"

        page.screenshot = tolerant_screenshot
        stale = client.get(
            f"/api/navigator/tasks/{task_id}/screenshot",
            params={"observation_id": "obs_never"},
        )
        assert stale.status_code == 409
        response = client.get(
            f"/api/navigator/tasks/{task_id}/screenshot",
            params={"observation_id": observed["observation_id"]},
        )
    assert response.status_code == 200
    assert response.headers["x-scrapex-observation-id"] == observed["observation_id"]


def test_extract_records_the_full_page_text_for_evidence(tmp_path: Path):
    services = make_services(tmp_path)
    with TestClient(create_app(services)) as client:
        task_id = _task(client)
        client.post(f"/api/navigator/tasks/{task_id}/observe")
        acted = client.post(
            f"/api/navigator/tasks/{task_id}/act", json={"action": "extract"}
        ).json()
        evidence = client.get(f"/api/navigator/tasks/{task_id}/evidence").json()
    assert acted["extract"]["chars"] > 0 and acted["extract"]["sha256"]
    assert evidence["extracted_text"] == "2023 Toyota Camry blind spot monitor calibration"
    assert evidence["extracted_text_sha256"] == acted["extract"]["sha256"]
    assert evidence["extracted_at"]
    assert evidence["referenced_links"] == []
    assert evidence["observation_id"].startswith("obs_")


def test_capture_carries_the_callers_review_only_as_data(tmp_path: Path):
    services = make_services(tmp_path)
    with TestClient(create_app(services)) as client:
        task_id = _task(client)
        client.post(f"/api/navigator/tasks/{task_id}/observe")
        # Not verified yet: the review does not authorize anything.
        refused = client.post(
            f"/api/navigator/tasks/{task_id}/capture",
            json={"semantic_review": {"decision": "ACCEPT", "confidence": 0.99}},
        )
    assert refused.status_code == 409
