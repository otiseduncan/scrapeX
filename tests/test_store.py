import json
from pathlib import Path
from scrapex.db import Store
from scrapex.models import BatchCreate,VehicleSpec

def test_restart_recovery(tmp_path:Path):
    s=Store(tmp_path/"db.sqlite")
    bid=s.create_batch(BatchCreate(name="week",vehicles=[VehicleSpec(year=2023,make="Acura",model="TLX")]))
    item=s.batch(bid)["items"][0];s.set_batch_state(bid,"running");s.set_item(item["id"],"capturing")
    s.recover_after_restart();b=s.batch(bid)
    assert b["state"]=="paused";assert b["items"][0]["state"]=="paused"


def test_normalize_adas_map_storage_paths_rewrites_legacy_absolute_path(tmp_path: Path):
    root = tmp_path / "ADAS SI"
    root.mkdir()
    store = Store(tmp_path / "db2.sqlite")
    bid = store.create_batch(
        BatchCreate(
            name="week",
            vehicles=[
                VehicleSpec(
                    ro_number="2400911731",
                    year=2023,
                    make="Toyota",
                    model="Camry",
                )
            ],
        )
    )
    item = store.batch(bid)["items"][0]
    legacy = str(root / "2400911731 adas map.pdf")
    store.set_item(
        item["id"],
        "requirements_captured",
        adas_map_report_links_json=json.dumps([legacy]),
        adas_map_raw_result_json=json.dumps({"report_links": [legacy]}),
    )
    changed = store.normalize_adas_map_storage_paths(root)
    refreshed = store.batch(bid)["items"][0]
    assert changed >= 1
    expected = str(
        root
        / "ADAS Map"
        / "2400911731"
        / "2400911731 ADAS Map.pdf"
    )
    assert expected in refreshed["adas_map_report_links_json"]
