from pathlib import Path

import pytest

import scrapex.adas_map_worker as adas_worker_module
from scrapex.adas_map_worker import AdasMapBatchRunner
from scrapex.db import Store
from scrapex.models import BatchCreate, VehicleSpec


class RecordingStore(Store):
    def __init__(self, path: Path):
        super().__init__(path)
        self.checkpoints = []

    def checkpoint_adas_map(self, item_id, stage, **fields):
        self.checkpoints.append((item_id, stage))
        super().checkpoint_adas_map(item_id, stage, **fields)


def _batch(store: Store, count: int = 1) -> str:
    return store.create_batch(
        BatchCreate(
            name="map",
            vehicles=[
                VehicleSpec(
                    ro_id=f"ro-{number}",
                    ro_number=f"900000001{number}",
                    shop="Gerber Collision & Glass - Macon",
                    year=2016,
                    make="Toyota",
                    model="Sienna",
                )
                for number in range(1, count + 1)
            ],
        )
    )


def _success(ro_number: str) -> dict:
    return {
        "success": True,
        "status": "complete",
        "ro_number": ro_number,
        "ciq_ro_id": f"ro-{ro_number[-1]}",
        "shop": "Gerber Collision & Glass - Macon",
        "vin": "TESTCAR0000000001",
        "vehicle": {
            "year": 2016,
            "make": "Toyota",
            "model": "Sienna",
            "configuration": "L FWD w/7-Passenger Seating",
        },
        "vehicle_label": "2016 Toyota Sienna L FWD w/7-Passenger Seating",
        "inspection_id": "9900001",
        "row_binding_confirmed": True,
        "modal_inspection_confirmed": True,
        "modal_runtime_id": "42.9900001",
        "required_region_confirmed": True,
        "source_url": "https://opus.adasmap.com/details/9900001",
        "details_url": "https://opus.adasmap.com/details/9900001",
        "requirement_records": [
            {
                "label": "Seat Belt",
                "source": "adas_map_required_list_item",
                "source_context": "selected_required_modal",
                "source_context_runtime_id": "42.9900001",
                "source_url": "https://opus.adasmap.com/details/9900001",
                "source_control_class": "btn btn-link custom-link",
            }
        ],
        "requirements_proven": True,
        "explicit_no_calibration": False,
    }


class FakeCIQ:
    def __init__(self):
        self.calls = []

    async def reconcile_requirements(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "verified": True,
            "snapshot_verified": True,
            "requirements": [{"key": "seat_belt", "label": "Seat Belt"}],
            "active_calibration_item_ids": {"seat_belt": ["cal-seat"]},
        }


class FakeSource:
    def __init__(self, *, fail_once: str | None = None):
        self.fail_once = fail_once
        self.calls = []

    async def status(self):
        return {"active": True, "authenticated": True}

    async def open(self):
        raise AssertionError("already active")

    async def lookup(self, ro_number, shop, expected):
        self.calls.append(ro_number)
        if self.fail_once == ro_number:
            self.fail_once = None
            raise RuntimeError("unexpected bridge failure")
        return _success(ro_number)


@pytest.mark.asyncio
async def test_success_persists_every_map_checkpoint_and_verified_ciq(tmp_path: Path):
    store = RecordingStore(tmp_path / "db.sqlite")
    batch_id = _batch(store)
    item = store.batch(batch_id)["items"][0]
    ciq = FakeCIQ()
    runner = AdasMapBatchRunner(store, FakeSource(), ciq)

    await runner.process_one(item)

    stages = [stage for _, stage in store.checkpoints]
    assert stages == [
        "searching_adas_map",
        "ro_found",
        "vin_verified",
        "opening_inspection",
        "requirements_captured",
        "adas_map_complete",
    ]
    refreshed = store.batch(batch_id)["items"][0]
    assert refreshed["adas_map_state"] == "adas_map_complete"
    assert refreshed["adas_map_contract_version"] == 1
    assert refreshed["adas_map_requirements_proven"] == 1
    assert refreshed["ciq_reconciliation_state"] == "complete"
    assert refreshed["configuration"] == "L FWD w/7-Passenger Seating"
    assert ciq.calls[0]["vehicle"]["configuration"] == "L FWD w/7-Passenger Seating"


@pytest.mark.asyncio
async def test_required_report_capture_must_be_verified_before_ciq_reconciliation(
    tmp_path: Path,
):
    store = RecordingStore(tmp_path / "db.sqlite")
    batch_id = _batch(store)
    item = store.batch(batch_id)["items"][0]
    ciq = FakeCIQ()
    source = FakeSource()

    async def missing_report_lookup(ro_number, shop, expected):
        result = _success(ro_number)
        result.update(
            {
                "report_capture_required": True,
                "report_capture_verified": False,
                "report_error": "Canonical ADAS Map PDF was not saved.",
            }
        )
        return result

    source.lookup = missing_report_lookup
    runner = AdasMapBatchRunner(store, source, ciq)

    await runner.process_one(item)

    refreshed = store.batch(batch_id)["items"][0]
    assert refreshed["adas_map_state"] == "needs_operator"
    assert "not saved" in refreshed["adas_map_last_error"]
    assert ciq.calls == []


@pytest.mark.asyncio
async def test_unexpected_failure_on_one_ro_continues_to_the_next(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setattr(adas_worker_module, "ITEM_DELAY_SECONDS", 0)
    store = Store(tmp_path / "db.sqlite")
    batch_id = _batch(store, count=2)
    source = FakeSource(fail_once="9000000011")
    runner = AdasMapBatchRunner(store, source, FakeCIQ())

    await runner._run(batch_id)

    assert source.calls[:2] == ["9000000011", "9000000012"]
    assert store.pipeline_summary(batch_id)["ready"] == 2
    assert store.batch(batch_id)["state"] == "complete"


@pytest.mark.asyncio
async def test_runner_preflight_failure_clears_running_state(tmp_path: Path):
    store = Store(tmp_path / "db.sqlite")
    batch_id = _batch(store)

    class BrokenStatusSource(FakeSource):
        async def status(self):
            raise RuntimeError("UIA unavailable")

    runner = AdasMapBatchRunner(store, BrokenStatusSource(), FakeCIQ())
    store.set_batch_state(batch_id, "running_adas_map")
    await runner._run(batch_id)

    batch = store.batch(batch_id)
    assert batch["state"] == "paused"
    assert "UIA unavailable" in batch["last_error"]


@pytest.mark.asyncio
async def test_stale_contract_resets_map_attempt_budget(tmp_path: Path):
    store = Store(tmp_path / "db.sqlite")
    batch_id = _batch(store)
    item = store.batch(batch_id)["items"][0]
    store.set_item(
        item["id"],
        item["state"],
        adas_map_contract_version=0,
        adas_map_attempts=9,
        adas_map_state="needs_operator",
    )
    stale = store.next_adas_map_item(batch_id)
    await AdasMapBatchRunner(store, FakeSource(), FakeCIQ()).process_one(stale)
    refreshed = store.batch(batch_id)["items"][0]
    assert refreshed["adas_map_contract_version"] == 1
    assert refreshed["adas_map_attempts"] == 1
    assert refreshed["adas_map_state"] == "adas_map_complete"


@pytest.mark.asyncio
async def test_ui_noise_never_reaches_ciq_reconciliation(tmp_path: Path):
    store = Store(tmp_path / "db.sqlite")
    batch_id = _batch(store)
    item = store.batch(batch_id)["items"][0]
    ciq = FakeCIQ()
    source = FakeSource()

    async def noisy_lookup(ro_number, shop, expected):
        result = _success(ro_number)
        result["requirement_records"] = [
            {"label": "Create Calibration", "source_control_class": "custom-link"}
        ]
        return result

    source.lookup = noisy_lookup
    await AdasMapBatchRunner(store, source, ciq).process_one(item)

    refreshed = store.batch(batch_id)["items"][0]
    assert refreshed["adas_map_state"] == "requirements_unparsed"
    assert refreshed["adas_map_requirements_proven"] == 0
    assert ciq.calls == []


@pytest.mark.asyncio
async def test_requirement_requires_authoritative_list_source_marker(tmp_path: Path):
    store = Store(tmp_path / "db.sqlite")
    batch_id = _batch(store)
    item = store.batch(batch_id)["items"][0]
    ciq = FakeCIQ()
    source = FakeSource()

    async def wrong_source_lookup(ro_number, shop, expected):
        result = _success(ro_number)
        result["requirement_records"][0]["source"] = "document_text_scan"
        return result

    source.lookup = wrong_source_lookup
    await AdasMapBatchRunner(store, source, ciq).process_one(item)

    refreshed = store.batch(batch_id)["items"][0]
    assert refreshed["adas_map_state"] == "requirements_unparsed"
    assert refreshed["adas_map_requirements_proven"] == 0
    assert ciq.calls == []


@pytest.mark.asyncio
async def test_requirement_requires_same_proven_modal_identity(tmp_path: Path):
    store = Store(tmp_path / "db.sqlite")
    batch_id = _batch(store)
    item = store.batch(batch_id)["items"][0]
    ciq = FakeCIQ()
    source = FakeSource()

    async def wrong_modal_lookup(ro_number, shop, expected):
        result = _success(ro_number)
        result["requirement_records"][0]["source_context_runtime_id"] = "other-modal"
        return result

    source.lookup = wrong_modal_lookup
    await AdasMapBatchRunner(store, source, ciq).process_one(item)
    refreshed = store.batch(batch_id)["items"][0]
    assert refreshed["adas_map_state"] == "requirements_unparsed"
    assert ciq.calls == []


@pytest.mark.asyncio
async def test_shop_binding_uses_portal_observation_with_known_location_equivalence(
    tmp_path: Path,
):
    store = Store(tmp_path / "db.sqlite")
    batch_id = _batch(store)
    item = store.batch(batch_id)["items"][0]
    ciq = FakeCIQ()
    source = FakeSource()

    async def observed_shop_lookup(ro_number, shop, expected):
        result = _success(ro_number)
        result["shop"] = "Gerber Collision & Glass - Macon/Mercer University"
        return result

    source.lookup = observed_shop_lookup
    await AdasMapBatchRunner(store, source, ciq).process_one(item)
    assert store.batch(batch_id)["items"][0]["adas_map_state"] == "adas_map_complete"


@pytest.mark.asyncio
async def test_shop_binding_rejects_different_portal_location(tmp_path: Path):
    store = Store(tmp_path / "db.sqlite")
    batch_id = _batch(store)
    item = store.batch(batch_id)["items"][0]
    ciq = FakeCIQ()
    source = FakeSource()

    async def wrong_shop_lookup(ro_number, shop, expected):
        result = _success(ro_number)
        result["shop"] = "Gerber Collision & Glass - Perry (GA)"
        return result

    source.lookup = wrong_shop_lookup
    await AdasMapBatchRunner(store, source, ciq).process_one(item)
    refreshed = store.batch(batch_id)["items"][0]
    assert refreshed["adas_map_state"] == "ambiguous_ro"
    assert ciq.calls == []
