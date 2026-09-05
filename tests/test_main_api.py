from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from scrapex import __version__
from scrapex.db import ADAS_MAP_CONTRACT_VERSION, Store
from scrapex.main import AppServices, create_app
from scrapex.models import BatchCreate, VehicleSpec


class FakeCIQ:
    def __init__(self) -> None:
        self.exact_calls: list[tuple[list[str], str]] = []
        self.exact_override: list[VehicleSpec] | None = None

    async def status(self):
        return {"reachable": True, "authorized": True}

    async def vehicles_for_phases(self, phases, shop=None, source_scope="active"):
        return [vehicle("2400000001", shop=shop or "Macon")]

    async def vehicles_for_ro_numbers(self, ro_numbers, source_scope="all"):
        self.exact_calls.append((list(ro_numbers), source_scope))
        if self.exact_override is not None:
            return self.exact_override
        return [vehicle(value) for value in ro_numbers]


class FakeStatusSource:
    def __init__(self, *, authenticated: bool = True) -> None:
        self.authenticated = authenticated
        self.opened = 0

    async def status(self):
        return {
            "active": True,
            "authenticated": self.authenticated,
            "title": "ADAS Map",
        }

    async def open(self):
        self.opened += 1
        return await self.status()


class FakeWorkChrome:
    async def status(self):
        return {"target_found": True, "active": True, "authenticated": True}


class FakeAdasRunner:
    def __init__(self, store: Store) -> None:
        self.store = store
        self.started: list[str] = []
        self.paused: list[str] = []
        self.processed: list[str] = []

    async def start(self, batch_id: str):
        self.started.append(batch_id)
        self.store.set_batch_state(batch_id, "running_adas_map")

    async def pause(self, batch_id: str):
        self.paused.append(batch_id)
        self.store.set_batch_state(batch_id, "paused")

    async def process_one(self, item):
        self.processed.append(item["ro_number"])
        requirements = [{"calibration_type": "Occupant Classification System"}]
        self.store.set_item(
            item["id"],
            item["state"],
            vin="TESTCAR0000000001",
            adas_map_vin="TESTCAR0000000001",
            adas_map_state="adas_map_complete",
            adas_map_contract_version=ADAS_MAP_CONTRACT_VERSION,
            adas_map_requirements_proven=1,
            adas_map_requirements_json=json.dumps(requirements),
            requirements_json=json.dumps(["Occupant Classification System"]),
            ciq_reconciliation_state="complete",
            final_state="pending",
        )


def vehicle(ro_number: str, *, shop: str = "Macon") -> VehicleSpec:
    return VehicleSpec(
        ro_id=f"ciq-{ro_number}",
        ro_number=ro_number,
        shop=shop,
        year=2016,
        make="Toyota",
        model="Sienna",
    )


def make_services(tmp_path: Path) -> AppServices:
    store = Store(tmp_path / "data" / "test.sqlite3")
    ciq = FakeCIQ()
    adas_source = FakeStatusSource()
    return AppServices(
        settings=SimpleNamespace(
            data_root=tmp_path / "data",
            adas_si_root=tmp_path / "ADAS SI",
        ),
        store=store,
        ciq=ciq,
        work_chrome=FakeWorkChrome(),
        adas_map_source=adas_source,
        adas_map_runner=FakeAdasRunner(store),
    )


def create_local_batch(services: AppServices, *ros: str) -> str:
    return services.store.create_batch(
        BatchCreate(name="isolated test", vehicles=[vehicle(value) for value in ros])
    )


def mark_adas_complete(services: AppServices, batch_id: str, ro_number: str) -> None:
    item = next(
        row for row in services.store.batch(batch_id)["items"]
        if row["ro_number"] == ro_number
    )
    services.store.set_item(
        item["id"],
        item["state"],
        adas_map_state="adas_map_complete",
        adas_map_contract_version=ADAS_MAP_CONTRACT_VERSION,
        adas_map_requirements_proven=1,
        adas_map_vin="TESTCAR0000000001",
        ciq_reconciliation_state="complete",
        requirements_json=json.dumps(["Occupant Classification System"]),
        final_state="pending",
    )


def test_health_dashboard_and_production_route_surface(tmp_path: Path):
    services = make_services(tmp_path)
    with TestClient(create_app(services)) as client:
        health = client.get("/api/health")
        assert health.status_code == 200
        assert health.json()["version"] == __version__ == "0.5.0"
        page = client.get("/").text
        assert "Run ADAS Map batch" in page
        assert "ADAS Map Batch (test bridge first)" not in page
        assert "v0.4.8" not in page
        assert client.post("/api/browser/click-collision-debug").status_code == 404
        assert client.get("/api/work-chrome/inspect").status_code == 404
        assert client.post("/api/browser/open").status_code == 404
        assert client.post("/api/batches/anything/alldata/start").status_code == 404
        assert client.post("/api/batches", json={"name": "forged", "vehicles": []}).status_code == 405
        navigator = client.get("/api/browser/status").json()
        assert navigator == {
            "frozen": False,
            "automation_enabled": True,
            "mode": "agentic_navigator",
            "providers": ["alldata"],
            "legacy_batch_runner_frozen": True,
            "message": (
                "Dynamic SI research is enabled through the task-based Navigator. "
                "The retired legacy ALLDATA batch runner remains frozen."
            ),
        }


def test_exact_ciq_batch_is_bounded_and_authoritative(tmp_path: Path):
    services = make_services(tmp_path)
    with TestClient(create_app(services)) as client:
        response = client.post(
            "/api/batches/from-ciq/exact",
            json={
                "name": "stage two",
                "ro_numbers": ["9000000001", "9000000002", "9000000001"],
                "source_scope": "all",
            },
        )
    assert response.status_code == 200
    body = response.json()
    assert body["requested_ro_numbers"] == ["9000000001", "9000000002"]
    assert body["readiness"]["total"] == 2
    assert services.ciq.exact_calls == [(["9000000001", "9000000002"], "all")]


def test_exact_ciq_batch_fails_closed_on_missing_or_extra_result(tmp_path: Path):
    services = make_services(tmp_path)
    services.ciq.exact_override = [vehicle("not-requested")]
    with TestClient(create_app(services)) as client:
        response = client.post(
            "/api/batches/from-ciq/exact",
            json={"ro_numbers": ["9000000001"], "source_scope": "all"},
        )
        assert response.status_code == 409
        assert client.get("/api/batches").json() == []


def test_exact_ciq_batch_rejects_reused_internal_identity(tmp_path: Path):
    services = make_services(tmp_path)
    services.ciq.exact_override = [
        vehicle("9000000001"),
        vehicle("9000000002"),
    ]
    services.ciq.exact_override[1].ro_id = services.ciq.exact_override[0].ro_id
    with TestClient(create_app(services)) as client:
        response = client.post(
            "/api/batches/from-ciq/exact",
            json={"ro_numbers": ["9000000001", "9000000002"], "source_scope": "all"},
        )
        assert response.status_code == 409
        assert response.json()["detail"]["reused_internal_ids"]
        assert client.get("/api/batches").json() == []


def test_process_one_adas_map_uses_public_runner_and_updates_truth(tmp_path: Path):
    services = make_services(tmp_path)
    batch_id = create_local_batch(services, "9000000001")
    with TestClient(create_app(services)) as client:
        response = client.post(
            f"/api/batches/{batch_id}/adas-map/process-one/9000000001"
        )
    assert response.status_code == 200
    body = response.json()
    assert services.adas_map_runner.processed == ["9000000001"]
    assert body["attempted"] is True
    assert body["completed"] is True
    assert body["status"] == "completed"
    assert body["item"]["adas_map_state"] == "adas_map_complete"
    assert body["readiness"]["adas_map_complete"] == 1
    assert body["readiness"]["ciq_reconciled"] == 1
    assert body["readiness"]["ready"] is True
    assert body["readiness"]["scope"] == "adas_map_and_ciq_reconciliation"


def test_process_one_reports_attempt_without_false_completion(tmp_path: Path):
    services = make_services(tmp_path)
    batch_id = create_local_batch(services, "9000000001")

    async def fail_closed(item):
        services.store.checkpoint_adas_map(
            item["id"],
            "requirements_unparsed",
            adas_map_requirements_proven=0,
            adas_map_last_error="Required rows were not proven.",
        )

    services.adas_map_runner.process_one = fail_closed
    with TestClient(create_app(services)) as client:
        response = client.post(
            f"/api/batches/{batch_id}/adas-map/process-one/9000000001"
        )

    assert response.status_code == 200
    body = response.json()
    assert body["attempted"] is True
    assert body["completed"] is False
    assert body["status"] == "requirements_unparsed"
    assert body["readiness"]["ready"] is False


def test_full_adas_map_start_and_pause_use_public_runner(tmp_path: Path):
    services = make_services(tmp_path)
    batch_id = create_local_batch(services, "9000000001")
    with TestClient(create_app(services)) as client:
        started = client.post(f"/api/batches/{batch_id}/adas-map/start")
        assert started.status_code == 200
        assert started.json()["stage"] == "adas_map"
        paused = client.post(f"/api/batches/{batch_id}/adas-map/pause")
        assert paused.status_code == 200
    assert services.adas_map_runner.started == [batch_id]
    assert services.adas_map_runner.paused == [batch_id]


def test_legacy_map_state_never_claims_current_scope_ready(tmp_path: Path):
    services = make_services(tmp_path)
    batch_id = create_local_batch(services, "9000000001")
    item = services.store.batch(batch_id)["items"][0]
    services.store.set_item(
        item["id"],
        "complete",
        adas_map_state="complete",
        adas_map_requirements_proven=1,
        ciq_reconciliation_state="complete",
        final_state="si_ready",
    )
    with TestClient(create_app(services)) as client:
        summary = client.get(f"/api/batches/{batch_id}/summary").json()["readiness"]
    assert summary["ready"] is False
    assert summary["adas_map_unresolved"] == 1
    assert summary["ciq_reconciled"] == 0


def test_stale_contract_never_claims_ready_with_canonical_state_names(tmp_path: Path):
    services = make_services(tmp_path)
    batch_id = create_local_batch(services, "9000000001")
    item = services.store.batch(batch_id)["items"][0]
    services.store.set_item(
        item["id"],
        "complete",
        adas_map_state="adas_map_complete",
        adas_map_contract_version=0,
        adas_map_requirements_proven=1,
        ciq_reconciliation_state="complete",
    )
    with TestClient(create_app(services)) as client:
        summary = client.get(f"/api/batches/{batch_id}/summary").json()["readiness"]
    assert summary["ready"] is False
    assert summary["adas_map_complete"] == 0
    assert summary["ciq_reconciled"] == 0
    assert summary["adas_map_unresolved"] == 1


def test_one_adas_exception_does_not_hide_other_verified_ro_truth(tmp_path: Path):
    services = make_services(tmp_path)
    batch_id = create_local_batch(services, "good-ro", "missing-ro")
    mark_adas_complete(services, batch_id, "good-ro")
    missing = next(
        row for row in services.store.batch(batch_id)["items"]
        if row["ro_number"] == "missing-ro"
    )
    services.store.set_item(
        missing["id"],
        "needs_operator",
        adas_map_state="ro_not_found",
        adas_map_contract_version=ADAS_MAP_CONTRACT_VERSION,
        final_state="needs_operator",
        adas_map_last_error="Exact RO not found.",
    )
    with TestClient(create_app(services)) as client:
        summary = client.get(f"/api/batches/{batch_id}/summary").json()["readiness"]
    assert summary["adas_map_complete"] == 1
    assert summary["adas_map_attention"] == 1
    assert summary["ready"] is False
    assert summary["state"] == "needs_operator"


def test_final_summary_and_exceptions_never_claim_false_readiness(tmp_path: Path):
    services = make_services(tmp_path)
    batch_id = create_local_batch(services, "ready-ro", "blocked-ro")
    mark_adas_complete(services, batch_id, "ready-ro")
    ready = next(
        row for row in services.store.batch(batch_id)["items"]
        if row["ro_number"] == "ready-ro"
    )
    services.store.set_item(ready["id"], "complete", final_state="si_ready")
    blocked = next(
        row for row in services.store.batch(batch_id)["items"]
        if row["ro_number"] == "blocked-ro"
    )
    services.store.set_item(
        blocked["id"],
        "needs_operator",
        adas_map_state="requirements_unparsed",
        adas_map_contract_version=ADAS_MAP_CONTRACT_VERSION,
        final_state="needs_operator",
    )
    with TestClient(create_app(services)) as client:
        summary = client.get(f"/api/batches/{batch_id}/summary").json()["readiness"]
        exceptions = client.get(f"/api/batches/{batch_id}/exceptions").json()
    assert summary["adas_map_complete"] == 1
    assert summary["downstream"]["alldata_acquisition"] == "agentic_navigator"
    assert summary["downstream"]["adas_si_coverage"] == "x_omni_vehicle_library"
    assert summary["ready"] is False
    assert summary["state"] == "needs_operator"
    assert exceptions["count"] == 1
    assert exceptions["items"][0]["ro_number"] == "blocked-ro"


def test_browser_status_reports_agentic_navigator_without_reactivating_legacy_batch(tmp_path: Path):
    from scrapex.main import AppServices, create_app
    from types import SimpleNamespace
    from fastapi.testclient import TestClient

    async def _status():
        return {"reachable": True, "authorized": True, "active": True, "authenticated": True}

    services = AppServices(
        settings=SimpleNamespace(data_root=tmp_path / "data", adas_si_root=tmp_path / "ADAS SI"),
        store=Store(tmp_path / "status.sqlite"),
        ciq=SimpleNamespace(status=_status),
        work_chrome=SimpleNamespace(),
        adas_map_source=SimpleNamespace(status=_status),
        adas_map_runner=SimpleNamespace(),
        navigator_manager=SimpleNamespace(),
        navigator_providers={"alldata": object()},
    )
    with TestClient(create_app(services)) as client:
        body = client.get("/api/browser/status").json()
    assert body["automation_enabled"] is True
    assert body["mode"] == "agentic_navigator"
    assert body["providers"] == ["alldata"]
    assert body["legacy_batch_runner_frozen"] is True
