from pathlib import Path
from scrapex.db import Store
from scrapex.models import BatchCreate, VehicleSpec

def make_store(tmp_path: Path):
    return Store(tmp_path / "db.sqlite")

def test_pending_batch_can_be_deleted(tmp_path: Path):
    s=make_store(tmp_path)
    bid=s.create_batch(BatchCreate(name="wrong",vehicles=[VehicleSpec(year=2023,make="Acura",model="TLX")]))
    result=s.delete_batch(bid)
    assert result["deleted"] is True
    assert s.batch(bid) is None

def test_started_batch_refuses_delete(tmp_path: Path):
    s=make_store(tmp_path)
    bid=s.create_batch(BatchCreate(name="started",vehicles=[VehicleSpec(year=2023,make="Acura",model="TLX")]))
    item=s.batch(bid)["items"][0]
    s.set_item(item["id"],"capturing")
    result=s.delete_batch(bid)
    assert result["deleted"] is False
    assert result["reason"]=="batch_has_started"
