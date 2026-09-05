from pathlib import Path

from scrapex.db import ADAS_MAP_CONTRACT_VERSION, Store
from scrapex.models import BatchCreate, VehicleSpec


def _batch(store: Store, count: int = 1) -> str:
    return store.create_batch(
        BatchCreate(
            name="week",
            vehicles=[
                VehicleSpec(
                    ro_id=f"ro-{number}",
                    ro_number=f"24009{number}",
                    shop="Macon",
                    year=2024,
                    make="Toyota",
                    model="Camry",
                )
                for number in range(count)
            ],
        )
    )


def test_legacy_map_complete_is_invalidated(tmp_path: Path):
    store = Store(tmp_path / "db.sqlite")
    bid = _batch(store)
    item = store.batch(bid)["items"][0]
    store.set_item(
        item["id"],
        "pending",
        attempts=2,
        adas_map_state="complete",
        adas_map_vin="TESTCAR0000000002",
    )

    assert store.adas_map_summary(bid)["adas_map_complete"] == 0
    selected = store.next_adas_map_item(bid)
    assert selected["id"] == item["id"]
    assert selected["adas_map_attempts"] == 0

    store.set_item(item["id"], "pending", adas_map_state="needs_operator")
    assert store.next_adas_map_item(bid)["id"] == item["id"]
    assert store.exceptions(bid) == []

    store.set_item(
        item["id"],
        "pending",
        adas_map_state="needs_operator",
        adas_map_contract_version=0,
        adas_map_attempts=3,
    )
    exhausted_legacy = store.next_adas_map_item(bid)
    assert exhausted_legacy["id"] == item["id"]


def test_pre_attachment_contract_completion_is_not_current_readiness(tmp_path: Path):
    store = Store(tmp_path / "db.sqlite")
    bid = _batch(store)
    item = store.batch(bid)["items"][0]
    store.set_item(
        item["id"],
        "pending",
        adas_map_state="adas_map_complete",
        adas_map_contract_version=1,
        adas_map_requirements_proven=1,
        ciq_reconciliation_state="complete",
    )

    assert ADAS_MAP_CONTRACT_VERSION == 2
    assert store.batch(bid)["summary"]["complete"] == 0
    assert store.list_batches()[0]["complete_count"] == 0
    selected = store.next_adas_map_item(bid)
    assert selected["id"] == item["id"]

def test_map_readiness_requires_proof_and_verified_ciq_reconciliation(tmp_path: Path):
    store = Store(tmp_path / "db.sqlite")
    bid = _batch(store)
    item = store.batch(bid)["items"][0]
    store.set_item(
        item["id"],
        "pending",
        adas_map_state="adas_map_complete",
        adas_map_contract_version=ADAS_MAP_CONTRACT_VERSION,
        adas_map_requirements_proven=1,
        requirements_json='["Front View Camera"]',
    )
    assert store.pipeline_summary(bid)["ready"] == 0

    store.save_reconciliation(item["id"], "complete", {"verified": True})
    assert store.pipeline_summary(bid)["ready"] == 1


def test_explicit_map_checkpoint_survives_store_restart(tmp_path: Path):
    path = tmp_path / "db.sqlite"
    store = Store(path)
    bid = _batch(store)
    item = store.batch(bid)["items"][0]
    store.checkpoint_adas_map(
        item["id"],
        "opening_inspection",
        adas_map_attempts=1,
        adas_map_inspection_id="9900001",
    )

    restarted = Store(path)
    restarted.recover_after_restart()
    resumed = restarted.next_adas_map_item(bid)
    assert resumed["adas_map_state"] == "opening_inspection"
    assert resumed["adas_map_attempts"] == 1
    assert resumed["adas_map_inspection_id"] == "9900001"


def test_restart_pauses_all_explicit_runner_states(tmp_path: Path):
    path = tmp_path / "db.sqlite"
    store = Store(path)
    first = _batch(store)
    second = _batch(store)
    third = _batch(store)
    store.set_batch_state(first, "running_adas_map")
    store.set_batch_state(second, "running_alldata")
    store.set_batch_state(third, "pausing")

    Store(path).recover_after_restart()

    assert store.batch(first)["state"] == "paused"
    assert store.batch(second)["state"] == "paused"
    assert store.batch(third)["state"] == "paused"


def test_pipeline_summary_never_hides_map_attention(tmp_path: Path):
    store = Store(tmp_path / "db.sqlite")
    bid = _batch(store, count=2)
    first, second = store.batch(bid)["items"]
    store.set_item(
        first["id"], "complete",
        adas_map_state="adas_map_complete", adas_map_contract_version=ADAS_MAP_CONTRACT_VERSION,
        adas_map_requirements_proven=1,
        ciq_reconciliation_state="complete",
    )
    store.set_item(
        second["id"], "pending",
        adas_map_state="ro_not_found", adas_map_contract_version=ADAS_MAP_CONTRACT_VERSION,
    )
    summary = store.pipeline_summary(bid)
    assert summary["ready"] == 1
    assert summary["adas_map_attention"] == 1
    assert summary["needs_operator"] == 1
