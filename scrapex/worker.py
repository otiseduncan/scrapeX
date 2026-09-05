from __future__ import annotations

from typing import Any

from .browser import BrowserManager
from .config import Settings
from .db import Store


MANUAL_FUTURE_MESSAGE = (
    "ALLDATA and ADAS SI acquisition are frozen for a future manual workflow; "
    "ScrapeX will not open or automate ALLDATA."
)


class BatchRunner:
    """Compatibility surface for the intentionally frozen ALLDATA stage."""

    def __init__(
        self,
        settings: Settings,
        store: Store,
        browser: BrowserManager,
        ciq: Any | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.browser = browser
        self.ciq = ciq
        self._tasks: dict[str, Any] = {}
        self._pause: set[str] = set()

    async def start(self, batch_id: str) -> None:
        self._pause.add(batch_id)
        self.store.set_batch_state(batch_id, "manual_future", MANUAL_FUTURE_MESSAGE)

    async def pause(self, batch_id: str) -> None:
        self._pause.add(batch_id)
        self.store.set_batch_state(batch_id, "paused", MANUAL_FUTURE_MESSAGE)

    async def resume(self, batch_id: str) -> None:
        await self.start(batch_id)

    def is_running(self, batch_id: str) -> bool:
        return False

    async def process_one(self, item: dict[str, Any]) -> None:
        raise RuntimeError(MANUAL_FUTURE_MESSAGE)

