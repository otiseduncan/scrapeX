
from pathlib import Path
from scrapex.db import Store
from scrapex.models import BatchCreate, VehicleSpec

def test_store_has_adas_map_and_delete_methods(tmp_path: Path):
    store = Store(tmp_path / "db.sqlite")
    assert callable(store.adas_map_summary)
    assert callable(store.delete_batch)
    assert callable(store.exceptions)

def test_adas_map_summary_counts_vin(tmp_path: Path):
    store = Store(tmp_path / "db.sqlite")
    bid = store.create_batch(
        BatchCreate(
            name="test",
            vehicles=[VehicleSpec(year=2023, make="Acura", model="TLX")]
        )
    )
    item = store.batch(bid)["items"][0]
    assert store.adas_map_summary(bid)["vin_ready"] == 0
    store.set_item(
        item["id"],
        item["state"],
        adas_map_contract_version=0,
        adas_map_vin="TESTCAR0000000002",
    )
    assert store.adas_map_summary(bid)["vin_ready"] == 0
    store.set_item(
        item["id"],
        item["state"],
        vin="TESTCAR0000000002",
        adas_map_state="adas_map_complete",
        adas_map_contract_version=1,
        adas_map_vin="TESTCAR0000000002",
        adas_map_requirements_proven=1,
    )
    summary = store.adas_map_summary(bid)
    assert summary["adas_map_complete"] == 1
    assert summary["vin_ready"] == 1
    assert summary["vin_missing"] == 0
