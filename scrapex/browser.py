from __future__ import annotations
import asyncio
from typing import Any
from playwright.async_api import async_playwright, BrowserContext, Page
from .config import Settings

class BrowserManager:
    def __init__(self,settings: Settings):
        self.settings=settings; self._pw=None; self._context:BrowserContext|None=None; self._page:Page|None=None; self._lock=asyncio.Lock()
    @property
    def page(self): return self._page

    async def open(self)->dict[str,Any]:
        async with self._lock:
            if self._page and not self._page.is_closed():
                await self._page.bring_to_front(); return await self.status()
            self._pw=await async_playwright().start()
            profile=self.settings.data_root/"browser-profile"; profile.mkdir(parents=True,exist_ok=True)
            self._context=await self._pw.chromium.launch_persistent_context(
                user_data_dir=str(profile),headless=False,viewport={"width":1440,"height":1000},
                args=["--disable-blink-features=AutomationControlled"]
            )
            self._page=self._context.pages[0] if self._context.pages else await self._context.new_page()
            if "alldata.com" not in self._page.url:
                await self._page.goto(self.settings.alldata_home,wait_until="domcontentloaded",timeout=45000)
            return await self.status()

    async def status(self):
        p=self._page
        if not p or p.is_closed(): return {"active":False,"authenticated":False,"url":None,"title":None}
        try: title=await p.title()
        except Exception: title=""
        authenticated=False
        if "alldata.com" in p.url:
            try:
                body=(await p.locator("body").inner_text(timeout=2500)).casefold()
                login=any(x in body for x in ("forgot password","sign in","log in","username","password"))
                authenticated=not (login and "collision" not in title.casefold())
            except Exception:
                authenticated=True
        return {"active":True,"authenticated":authenticated,"url":p.url,"title":title}

    async def close(self):
        async with self._lock:
            try:
                if self._context: await self._context.close()
            finally:
                self._context=None; self._page=None
                if self._pw: await self._pw.stop(); self._pw=None
