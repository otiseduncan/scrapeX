from pathlib import Path
from scrapex.db import Store
from scrapex.models import BatchCreate,VehicleSpec

def test_restart_recovery(tmp_path:Path):
    s=Store(tmp_path/"db.sqlite")
    bid=s.create_batch(BatchCreate(name="week",vehicles=[VehicleSpec(year=2023,make="Acura",model="TLX")]))
    item=s.batch(bid)["items"][0];s.set_batch_state(bid,"running");s.set_item(item["id"],"capturing")
    s.recover_after_restart();b=s.batch(bid)
    assert b["state"]=="paused";assert b["items"][0]["state"]=="paused"
