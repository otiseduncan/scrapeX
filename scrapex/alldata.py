from __future__ import annotations
import asyncio, base64, re
from typing import Any
from urllib.parse import urljoin
from playwright.async_api import Page
from .dedupe import canonical_alldata_url
from .models import VehicleSpec

MAKE_ALIASES={
    "chevrolet":{"chevrolet","chevy"},
    "nissan":{"nissan","nissan-datsun"},
    "mercedes-benz":{"mercedes-benz","mercedes","benz"},
    "volkswagen":{"volkswagen","vw"},
}
def plain(v): return re.sub(r"[^a-z0-9]+","",str(v or "").casefold())
def make_matches(text,make):
    aliases=MAKE_ALIASES.get(make.casefold(),{make.casefold()})
    return any(a in text.casefold() for a in aliases)
def vehicle_matches(text,vehicle:VehicleSpec):
    t=str(text or "").casefold()
    return str(vehicle.year) in t and make_matches(t,vehicle.make) and plain(vehicle.model) in plain(t)

async def _text(loc):
    try:
        if not await loc.is_visible(timeout=300): return ""
        return " ".join((await loc.inner_text(timeout=900)).split())
    except Exception: return ""

async def selected_vehicle_signal(page:Page):
    candidates=[]
    for sel in ("[data-testid*='vehicle' i]","[data-test*='vehicle' i]","[aria-label*='vehicle' i]",
                "[class*='selected-vehicle' i]","[class*='vehicle-context' i]","[id*='selectedVehicle' i]"):
        try:
            locs=page.locator(sel)
            for i in range(min(await locs.count(),20)):
                t=await _text(locs.nth(i))
                if 4<=len(t)<=350: candidates.append(t)
        except Exception: pass
    for pat in (r"Change\s+Vehicle",r"Selected\s+Vehicle",r"Current\s+Vehicle"):
        try:
            c=page.get_by_text(re.compile(pat,re.I)).first
            if await c.is_visible(timeout=300):
                t=await _text(c.locator("xpath=.."))
                if t: candidates.append(t)
        except Exception: pass
    try:
        title=await page.title()
        if title: candidates.append(title)
    except Exception: pass
    candidates=list(dict.fromkeys(candidates)); candidates.sort(key=len,reverse=True)
    return {"candidates":candidates,"label":candidates[0] if candidates else None}

async def verify_selected_vehicle(page,vehicle):
    sig=await selected_vehicle_signal(page)
    matched=[t for t in sig["candidates"] if vehicle_matches(t,vehicle)]
    return {"verified":bool(matched),"label":matched[0] if matched else sig.get("label"),"candidates":sig["candidates"][:10]}

async def _click_candidate(page,vehicle):
    locs=page.locator("a,button,[role='option'],[role='row'],[role='link'],li")
    found=[]
    for i in range(min(await locs.count(),350)):
        loc=locs.nth(i); t=await _text(loc)
        if not t or len(t)>500 or not vehicle_matches(t,vehicle): continue
        score=10+(4 if vehicle.trim and plain(vehicle.trim) in plain(t) else 0)+(3 if vehicle.engine and plain(vehicle.engine) in plain(t) else 0)
        found.append((score,t,loc))
    if not found: return {"selected":False,"reason":"No exact year/make/model candidate was visible."}
    found.sort(key=lambda x:(x[0],-len(x[1])),reverse=True)
    tied=[x for x in found if x[0]==found[0][0]]
    if len({plain(x[1]) for x in tied})>1 and not (vehicle.trim or vehicle.engine or vehicle.vin):
        return {"selected":False,"needs_operator":True,"reason":"Multiple equally plausible ALLDATA vehicle configurations.","candidates":[x[1] for x in tied[:10]]}
    try:
        await found[0][2].click(timeout=6000); await asyncio.sleep(1)
        return {"selected":True,"clicked":found[0][1]}
    except Exception as e:
        return {"selected":False,"reason":f"Vehicle candidate click failed: {type(e).__name__}"}

async def select_vehicle(page,vehicle):
    chk=await verify_selected_vehicle(page,vehicle)
    if chk["verified"]: return {"selected":True,"verified":True,"label":chk["label"],"path":"already_selected"}
    if vehicle.vin:
        for sel in ("input[placeholder*='VIN' i]","input[aria-label*='VIN' i]","input[name*='vin' i]","input[id*='vin' i]"):
            try:
                box=page.locator(sel).first
                if await box.is_visible(timeout=350):
                    await box.fill(vehicle.vin); await page.keyboard.press("Enter"); await asyncio.sleep(1.2)
                    c=await _click_candidate(page,vehicle)
                    if c.get("selected"):
                        v=await verify_selected_vehicle(page,vehicle)
                        if v["verified"]: return {"selected":True,"verified":True,"label":v["label"],"path":"vin"}
            except Exception: pass
    for pat in (r"Change\s+Vehicle",r"Select\s+Vehicle",r"Choose\s+Vehicle"):
        try:
            loc=page.get_by_text(re.compile(pat,re.I)).first
            if await loc.is_visible(timeout=400):
                await loc.click(timeout=5000); await asyncio.sleep(.8); break
        except Exception: pass
    query=f"{vehicle.year} {vehicle.make} {vehicle.model}"
    for sel in ("input[placeholder*='Year, Make, Model' i]","input[aria-label*='Year, Make, Model' i]",
                "input[name*='yyme' i]","input[id*='yyme' i]","input[name*='vehicle' i]",
                "input[id*='vehicle' i]","input[placeholder*='Search' i]"):
        try:
            box=page.locator(sel).first
            if not await box.is_visible(timeout=350): continue
            await box.fill(query); await asyncio.sleep(1)
            c=await _click_candidate(page,vehicle)
            if c.get("needs_operator"): return c
            if c.get("selected"):
                v=await verify_selected_vehicle(page,vehicle)
                if v["verified"]: return {"selected":True,"verified":True,"label":v["label"],"path":"search"}
        except Exception: pass
    c=await _click_candidate(page,vehicle)
    if c.get("selected"):
        v=await verify_selected_vehicle(page,vehicle)
        if v["verified"]: return {"selected":True,"verified":True,"label":v["label"],"path":"visible_result"}
    v=await verify_selected_vehicle(page,vehicle)
    return {"selected":False,"verified":False,"needs_operator":bool(c.get("needs_operator")),
            "reason":c.get("reason") or "Could not prove exact vehicle selection.","observed_vehicle":v.get("label"),
            "candidates":c.get("candidates") or v.get("candidates")}

async def open_quick_reference(page):
    for role in ("link","button"):
        try:
            loc=page.get_by_role(role,name=re.compile(r"ADAS\s+Quick\s+Reference",re.I)).first
            if await loc.is_visible(timeout=500):
                await loc.click(timeout=6000); await asyncio.sleep(1); return {"opened":True,"url":page.url}
        except Exception: pass
    try:
        loc=page.get_by_text(re.compile(r"ADAS\s+Quick\s+Reference",re.I),exact=False).first
        if await loc.is_visible(timeout=500):
            await loc.click(timeout=6000); await asyncio.sleep(1); return {"opened":True,"url":page.url}
    except Exception: pass
    try:
        ref=page.get_by_text(re.compile(r"Reference\s*-\s*Collision",re.I),exact=False).first
        if await ref.is_visible(timeout=400):
            await ref.click(timeout=5000); await asyncio.sleep(.6)
            loc=page.get_by_text(re.compile(r"ADAS\s+Quick\s+Reference",re.I),exact=False).first
            if await loc.is_visible(timeout=700):
                await loc.click(timeout=6000); await asyncio.sleep(1); return {"opened":True,"url":page.url}
    except Exception: pass
    return {"opened":False,"reason":"ADAS Quick Reference link was not found."}

_NAV={"home","help","help & feedback","bookmarks","library","change vehicle","select vehicle","logout","back","next","previous","print","save","close"}
async def quick_reference_links(page,limit=50):
    out=[]; seen=set(); anchors=page.locator("a[href]")
    for i in range(min(await anchors.count(),500)):
        a=anchors.nth(i)
        try: href=await a.get_attribute("href"); text=" ".join((await a.inner_text(timeout=700)).split()).strip()
        except Exception: continue
        if not href or not text or text.casefold() in _NAV: continue
        absolute=urljoin(page.url,href); canonical=canonical_alldata_url(absolute)
        if not canonical or canonical in seen: continue
        if not any(x in canonical.casefold() for x in ("/article/","/guid/","/component/")): continue
        seen.add(canonical); out.append({"title":text[:240],"url":absolute,"canonical_url":canonical})
        if len(out)>=limit: break
    return out

async def print_pdf(page):
    session=await page.context.new_cdp_session(page)
    try:
        result=await session.send("Page.printToPDF",{"printBackground":True,"preferCSSPageSize":True,
            "marginTop":0.35,"marginBottom":0.35,"marginLeft":0.35,"marginRight":0.35})
        return base64.b64decode(result["data"])
    finally:
        await session.detach()
