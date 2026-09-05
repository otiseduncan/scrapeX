"""Persistent, per-provider Playwright browser sessions for the Navigator.

Generalizes ``browser.py``'s dormant ``BrowserManager`` (same
``launch_persistent_context`` pattern) so each provider slug gets its own
authenticated, long-lived profile -- matching how ADAS Map's Work Chrome
bridge already relies on one already-authenticated session rather than
ScrapeX logging in with stored credentials.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


class NavigatorBrowserManager:
    def __init__(self, data_root: Path, *, headless: bool = False):
        self._data_root = data_root
        self._headless = headless
        self._contexts: dict[str, Any] = {}
        self._pages: dict[str, Any] = {}
        self._playwright: Any = None

    async def _ensure_playwright(self) -> Any:
        if self._playwright is None:
            from playwright.async_api import async_playwright

            self._playwright = await async_playwright().start()
        return self._playwright

    async def page_for(self, provider_slug: str, *, home_url: str) -> Any:
        """Return the persistent page for ``provider_slug``, opening it if needed."""
        if provider_slug in self._pages:
            page = self._pages[provider_slug]
            if not page.is_closed():
                return page

        pw = await self._ensure_playwright()
        profile_dir = self._data_root / "navigator-profiles" / provider_slug
        profile_dir.mkdir(parents=True, exist_ok=True)
        context = await pw.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=self._headless,
            args=["--disable-blink-features=AutomationControlled"],
        )
        self._contexts[provider_slug] = context
        page = context.pages[0] if context.pages else await context.new_page()
        if not page.url or page.url == "about:blank":
            # Confirmed live against ALLDATA (a client-rendered SPA):
            # "domcontentloaded" fires before Angular/React paints any real
            # content, so the very first observation after a fresh launch
            # could see zero elements -- "load" plus a short settle wait
            # (same philosophy as the post-click settle delay in
            # navigator_actions.py) means the first observe() is reliable
            # instead of racing the app's own render.
            await page.goto(home_url, wait_until="load")
            await page.wait_for_timeout(500)
        self._pages[provider_slug] = page
        return page

    async def close(self, provider_slug: str | None = None) -> None:
        slugs = [provider_slug] if provider_slug else list(self._contexts)
        for slug in slugs:
            context = self._contexts.pop(slug, None)
            self._pages.pop(slug, None)
            if context is not None:
                await context.close()
        if not self._contexts and self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None
