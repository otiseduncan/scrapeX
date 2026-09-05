from pathlib import Path

import pytest

from scrapex.db import Store
from scrapex.models import BatchCreate, VehicleSpec
from scrapex.worker import BatchRunner, MANUAL_FUTURE_MESSAGE


class ExplodingBrowser:
    def __init__(self):
        self.calls = 0

    @property
    def page(self):
        self.calls += 1
        raise AssertionError("ALLDATA page must not be accessed")

    async def status(self):
        self.calls += 1
        raise AssertionError("ALLDATA status must not be checked")

    async def open(self):
        self.calls += 1
        raise AssertionError("ALLDATA must not be opened")


@pytest.mark.asyncio
async def test_alldata_runner_is_manual_future_and_never_touches_browser(tmp_path: Path):
    store = Store(tmp_path / "db.sqlite")
    batch_id = store.create_batch(
        BatchCreate(
            name="map only",
            vehicles=[
                VehicleSpec(
                    ro_id="ro-1",
                    ro_number="9000000001",
                    year=2016,
                    make="Toyota",
                    model="Sienna",
                )
            ],
        )
    )
    browser = ExplodingBrowser()
    runner = BatchRunner(object(), store, browser, ciq=object())

    await runner.start(batch_id)

    batch = store.batch(batch_id)
    assert batch["state"] == "manual_future"
    assert batch["last_error"] == MANUAL_FUTURE_MESSAGE
    assert runner.is_running(batch_id) is False
    assert browser.calls == 0

    with pytest.raises(RuntimeError, match="frozen"):
        await runner.process_one(batch["items"][0])
    assert browser.calls == 0

