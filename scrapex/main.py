from __future__ import annotations

import asyncio
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Literal

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, Field

from . import __version__
from .adas_map_worker import AdasMapBatchRunner
from .alldata_navigator import AlldataNavigatorProvider
from .ciq import CIQClient
from .config import Settings
from .db import ADAS_MAP_ATTENTION_STATES, ADAS_MAP_CONTRACT_VERSION, Store
from .models import BatchCreate, CIQBatchCreate, CIQPreviewRequest
from .navigator_browser import NavigatorBrowserManager
from .navigator_worker import NavigatorTaskError, NavigatorTaskRunner
from .work_chrome import WorkChromeAdasMapSource, WorkChromeBridge


@dataclass(slots=True)
class AppServices:
    """Runtime dependencies for the loopback app.

    Keeping the dependency bundle explicit lets API tests use an isolated
    temporary store and inert fakes. Importing ``scrapex.main`` never opens a
    browser, contacts CIQ, or initializes the production database.
    """

    settings: Settings
    store: Store
    ciq: Any
    work_chrome: Any
    adas_map_source: Any
    adas_map_runner: Any
    navigator_manager: Any = None
    navigator_providers: dict[str, Any] = field(default_factory=dict)


class ExactCIQBatchCreate(BaseModel):
    """A small, CIQ-authoritative queue for staged acceptance."""

    name: str = Field(default="ScrapeX staged acceptance", min_length=1, max_length=180)
    ro_numbers: list[str] = Field(min_length=1, max_length=10)
    source_scope: Literal["active", "all", "terminal"] = "all"


class NavigatorTaskCreate(BaseModel):
    provider: str = Field(default="alldata", min_length=1, max_length=40)
    target: dict[str, Any] = Field(default_factory=dict)
    topic: str = Field(min_length=1, max_length=400)
    action_budget: int | None = Field(default=None, ge=1, le=80)


class NavigatorActionRequest(BaseModel):
    action: str = Field(min_length=1, max_length=20)
    ref: str | None = None
    text: str | None = None
    key: str | None = None
    url: str | None = None


class NavigatorRemoteInput(BaseModel):
    kind: Literal["click", "type", "key"]
    x: float | None = None
    y: float | None = None
    text: str | None = None
    key: str | None = None


def build_default_services() -> AppServices:
    """Construct production services only when the ASGI lifespan starts."""
    settings = Settings.load()
    store = Store(settings.data_root / "scrapex.sqlite3")
    ciq = CIQClient(settings)
    work_chrome = WorkChromeBridge(settings.root)
    # Reverted 2026-08-26: enabling adas_si_root surfaced a real bug -- the
    # download-report interaction leaves the ADAS Map inspection modal in a
    # state close_details() can't close, which finish_details() then treats
    # as a hard failure even though the calibration data was captured fine.
    # Re-enable only after work-chrome-adas-map.ps1's close/download sequence
    # is fixed to tolerate the download step. See PLAN/handoff notes.
    adas_map_source = WorkChromeAdasMapSource(work_chrome)
    return AppServices(
        settings=settings,
        store=store,
        ciq=ciq,
        work_chrome=work_chrome,
        adas_map_source=adas_map_source,
        adas_map_runner=AdasMapBatchRunner(store, adas_map_source, ciq),
        navigator_manager=NavigatorBrowserManager(settings.data_root),
        navigator_providers={"alldata": AlldataNavigatorProvider(settings.alldata_home)},
    )


def _services(request: Request) -> AppServices:
    services = getattr(request.app.state, "services", None)
    if services is None:
        raise HTTPException(503, "ScrapeX runtime services are not initialized.")
    return services


@asynccontextmanager
async def lifespan(app: FastAPI):
    services = getattr(app.state, "services", None)
    if services is None:
        services = build_default_services()
        app.state.services = services
    services.store.recover_after_restart()
    yield


router = APIRouter()


def _batch_or_404(services: AppServices, batch_id: str) -> dict[str, Any]:
    value = services.store.batch(batch_id)
    if value is None:
        raise HTTPException(404, "Batch not found.")
    return value


def _exact_item(batch: dict[str, Any], ro_number: str) -> dict[str, Any]:
    value = str(ro_number or "").strip()
    if not value or len(value) > 80 or re.search(r"[\x00-\x1f\x7f]", value):
        raise HTTPException(422, "Enter a valid RO number.")
    matches = [
        item
        for item in batch.get("items", [])
        if str(item.get("ro_number") or "").strip() == value
    ]
    if not matches:
        raise HTTPException(404, f"No item with RO {value} exists in this batch.")
    if len(matches) != 1:
        raise HTTPException(409, f"RO {value} is not unique in this batch.")
    return matches[0]


def _item_is_adas_complete(item: dict[str, Any]) -> bool:
    return (
        item.get("adas_map_contract_version") == ADAS_MAP_CONTRACT_VERSION
        and item.get("adas_map_state") == "adas_map_complete"
        and bool(item.get("adas_map_requirements_proven"))
        and item.get("ciq_reconciliation_state") == "complete"
    )


def _adas_runner_is_running(services: AppServices, batch_id: str) -> bool:
    check = getattr(services.adas_map_runner, "is_running", None)
    return bool(check(batch_id)) if check is not None else False


def _readiness(services: AppServices, batch: dict[str, Any]) -> dict[str, Any]:
    """Summarize the currently automated ADAS Map + CIQ scope only."""
    items = list(batch.get("items") or [])
    total = len(items)
    requirements_complete = sum(
        item.get("adas_map_contract_version") == ADAS_MAP_CONTRACT_VERSION
        and item.get("adas_map_state") == "adas_map_complete"
        and bool(item.get("adas_map_requirements_proven"))
        for item in items
    )
    adas_complete = sum(_item_is_adas_complete(item) for item in items)
    adas_attention = sum(
        item.get("adas_map_contract_version") == ADAS_MAP_CONTRACT_VERSION
        and str(item.get("adas_map_state") or "") in ADAS_MAP_ATTENTION_STATES
        for item in items
    )
    adas_unresolved = max(total - adas_complete - adas_attention, 0)
    # Count only CIQ reconciliation bound to the current canonical ADAS Map
    # result; stale reconciliation flags on legacy/failed rows are not proof.
    ciq_reconciled = adas_complete
    unreconciled = max(requirements_complete - adas_complete, 0)
    needs_operator = adas_attention + unreconciled

    blockers: list[str] = []
    if total == 0:
        blockers.append("The batch has no vehicles.")
    if adas_unresolved:
        blockers.append(f"{adas_unresolved} vehicle(s) have unresolved ADAS Map work.")
    if adas_attention:
        blockers.append(f"{adas_attention} vehicle(s) need ADAS Map operator attention.")
    if unreconciled:
        blockers.append(f"{unreconciled} ADAS Map result(s) are not reconciled in CIQ.")

    ready = (
        total > 0
        and adas_complete == total
        and needs_operator == 0
    )
    if ready:
        state = "ready"
        blockers = []
    elif needs_operator or adas_attention:
        state = "needs_operator"
    elif str(batch.get("state") or "").startswith("running"):
        state = "running"
    elif adas_complete or requirements_complete:
        state = "in_progress"
    else:
        state = "pending"

    return {
        "total": total,
        "adas_map_complete": adas_complete,
        "adas_map_requirements_complete": requirements_complete,
        "adas_map_attention": adas_attention,
        "adas_map_unresolved": adas_unresolved,
        "ciq_reconciled": ciq_reconciled,
        "needs_operator": needs_operator,
        "adas_map_settled": total > 0 and adas_unresolved == 0,
        "ready": ready,
        "scope": "adas_map_and_ciq_reconciliation",
        "state": state,
        "blockers": blockers,
        "downstream": {
            "adas_si_coverage": "manual_future",
            "alldata_acquisition": "frozen_manual_future",
        },
    }


def _enrich_batch(services: AppServices, batch: dict[str, Any]) -> dict[str, Any]:
    value = dict(batch)
    value["adas_map"] = services.store.adas_map_summary(str(value["id"]))
    readiness = _readiness(services, value)
    value["summary"] = readiness
    value["readiness"] = readiness
    value["final_summary"] = readiness
    return value


async def _component_status(call, component: str) -> dict[str, Any]:
    try:
        result = await call()
        return result if isinstance(result, dict) else {"ok": True, "value": result}
    except Exception as exc:  # Health must report a failed dependency, not hide it in a 500.
        return {
            "ok": False,
            "component": component,
            "error": f"{type(exc).__name__}: {exc}",
        }


_NAVIGATOR_ERROR_STATUS = {
    "not_found": 404,
    "invalid_action": 422,
    "invalid_arguments": 422,
    "domain_not_allowed": 422,
    "unknown_ref": 422,
}


def _navigator_provider_or_404(services: AppServices, provider_slug: str) -> Any:
    provider = services.navigator_providers.get(provider_slug)
    if provider is None:
        raise HTTPException(404, f"Unknown navigator provider: {provider_slug}")
    return provider


def _navigator_runner(services: AppServices, provider_slug: str) -> NavigatorTaskRunner:
    provider = _navigator_provider_or_404(services, provider_slug)
    return NavigatorTaskRunner(services.store, services.navigator_manager, provider)


def _navigator_runner_for_task(services: AppServices, task_id: str) -> tuple[NavigatorTaskRunner, dict[str, Any]]:
    task = services.store.navigator_task(task_id)
    if task is None:
        raise HTTPException(404, "Navigator task not found.")
    return _navigator_runner(services, str(task["provider"])), task


async def _navigator_call(call):
    try:
        return await call()
    except NavigatorTaskError as exc:
        raise HTTPException(_NAVIGATOR_ERROR_STATUS.get(exc.code, 409), exc.message) from exc


async def _ensure_adas_map_authenticated(services: AppServices) -> dict[str, Any]:
    status = await services.adas_map_source.status()
    if not status.get("active"):
        status = await services.adas_map_source.open()
    if not status.get("authenticated"):
        raise HTTPException(
            409,
            "ADAS Map is not authenticated in the managed work Chrome window.",
        )
    return status


@router.get("/", response_class=HTMLResponse)
async def dashboard() -> str:
    return DASHBOARD


@router.get("/api/health")
async def health(request: Request) -> dict[str, Any]:
    services = _services(request)
    ciq_status, adas_status = await asyncio.gather(
        _component_status(services.ciq.status, "ciq"),
        _component_status(services.adas_map_source.status, "adas_map"),
    )
    return {
        "ok": True,
        "service": "ScrapeX",
        "version": __version__,
        "loopback": True,
        "adas_si_root": str(services.settings.adas_si_root),
        "data_root": str(services.settings.data_root),
        "ciq": ciq_status,
        "adas_map": adas_status,
        "alldata": {
            "frozen": True,
            "automation_enabled": False,
            "mode": "manual_future",
        },
        "navigator": {
            "providers": sorted(services.navigator_providers),
        },
    }


@router.get("/api/ciq/status")
async def ciq_status(request: Request) -> dict[str, Any]:
    return await _services(request).ciq.status()


@router.post("/api/ciq/preview")
async def ciq_preview(request: Request, payload: CIQPreviewRequest) -> dict[str, Any]:
    services = _services(request)
    status = await services.ciq.status()
    if not status.get("authorized"):
        raise HTTPException(409, "Calibration IQ is not authorized for ScrapeX.")
    try:
        vehicles = await services.ciq.vehicles_for_phases(
            phases=payload.phases,
            shop=payload.shop,
            source_scope=payload.source_scope,
        )
    except Exception as exc:
        raise HTTPException(
            502,
            f"Calibration IQ preview failed: {type(exc).__name__}: {exc}",
        ) from exc
    return {
        "count": len(vehicles),
        "phases": payload.phases,
        "shop": payload.shop,
        "source_scope": payload.source_scope,
        "vehicles": [vehicle.model_dump() for vehicle in vehicles],
    }


@router.get("/api/adas-map/status")
async def adas_map_status(request: Request) -> dict[str, Any]:
    return await _services(request).adas_map_source.status()


@router.post("/api/adas-map/open")
async def adas_map_open(request: Request) -> dict[str, Any]:
    return await _services(request).adas_map_source.open()


@router.get("/api/work-chrome/status")
async def work_chrome_status(request: Request) -> dict[str, Any]:
    return await _services(request).work_chrome.status()


@router.get("/api/browser/status")
async def browser_status(request: Request) -> dict[str, Any]:
    services = _services(request)
    providers = sorted(services.navigator_providers)
    return {
        "frozen": False,
        "automation_enabled": bool(providers),
        "mode": "agentic_navigator",
        "providers": providers,
        "legacy_batch_runner_frozen": True,
        "message": (
            "Dynamic SI research is enabled through the task-based Navigator. "
            "The retired legacy ALLDATA batch runner remains frozen."
        ),
    }


@router.post("/api/navigator/tasks")
async def create_navigator_task(request: Request, payload: NavigatorTaskCreate) -> dict[str, Any]:
    services = _services(request)
    runner = _navigator_runner(services, payload.provider)
    task_id = runner.create_task(payload.target, payload.topic, action_budget=payload.action_budget)
    return {"task_id": task_id, "provider": payload.provider, **services.store.navigator_task(task_id)}


@router.get("/api/navigator/tasks/{task_id}")
async def get_navigator_task(request: Request, task_id: str) -> dict[str, Any]:
    services = _services(request)
    task = services.store.navigator_task(task_id)
    if task is None:
        raise HTTPException(404, "Navigator task not found.")
    return task


@router.post("/api/navigator/tasks/{task_id}/observe")
async def observe_navigator_task(request: Request, task_id: str) -> dict[str, Any]:
    services = _services(request)
    runner, _ = _navigator_runner_for_task(services, task_id)
    return await _navigator_call(lambda: runner.observe(task_id))


@router.post("/api/navigator/tasks/{task_id}/act")
async def act_navigator_task(request: Request, task_id: str, payload: NavigatorActionRequest) -> dict[str, Any]:
    services = _services(request)
    runner, _ = _navigator_runner_for_task(services, task_id)
    action = payload.model_dump(exclude_none=True)
    return await _navigator_call(lambda: runner.act(task_id, action))


@router.post("/api/navigator/tasks/{task_id}/verify")
async def verify_navigator_task(request: Request, task_id: str) -> dict[str, Any]:
    services = _services(request)
    runner, _ = _navigator_runner_for_task(services, task_id)
    return await _navigator_call(lambda: runner.verify(task_id))


@router.get("/api/navigator/tasks/{task_id}/evidence")
async def navigator_task_evidence(request: Request, task_id: str) -> dict[str, Any]:
    services = _services(request)
    runner, _ = _navigator_runner_for_task(services, task_id)
    return runner.evidence(task_id)


@router.get("/api/navigator/tasks/{task_id}/screenshot")
async def navigator_task_screenshot(request: Request, task_id: str) -> Response:
    """Task-bound visual observation for X Omni's multimodal Navigator loop.

    Unlike the provider-level screenshot used for human MFA/CAPTCHA handoff,
    this image is tied to an existing task and annotated only with refs from
    that task's latest cached accessibility observation.
    """
    services = _services(request)
    runner, _ = _navigator_runner_for_task(services, task_id)
    jpeg_bytes = await _navigator_call(lambda: runner.screenshot(task_id))
    return Response(
        content=jpeg_bytes,
        media_type="image/jpeg",
        headers={
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "X-ScrapeX-Task-Id": task_id,
        },
    )


@router.get("/api/navigator/providers/{provider}/current-target-signal")
async def navigator_current_target_signal(
    request: Request,
    provider: str,
    year: int | None = None,
    make: str | None = None,
    model: str | None = None,
    trim: str | None = None,
    vin: str | None = None,
) -> dict[str, Any]:
    """Synchronous "what's currently selected" read -- no task, no model turns.

    For the small number of non-agentic callers (Calibration IQ work-prep
    matching) that only need to know what vehicle is currently selected in
    an already-authenticated Navigator session.
    """
    services = _services(request)
    prov = _navigator_provider_or_404(services, provider)
    page = await services.navigator_manager.page_for(prov.slug, home_url=prov.home_url)
    target = {"year": year, "make": make, "model": model, "trim": trim, "vin": vin}
    signal = await prov.target_signal(page, target)
    return {"provider": provider, "target": target, **signal}


@router.get("/api/navigator/providers/{provider}/current-page-signals")
async def navigator_current_page_signals(request: Request, provider: str) -> dict[str, Any]:
    """Bounded, generic "what's currently on screen" read -- no task, no
    specific candidate vehicle. Calibration IQ work-prep matching checks
    many candidate rows against this same signal list, mirroring the
    synchronous in-process browser read this replaced.
    """
    services = _services(request)
    prov = _navigator_provider_or_404(services, provider)
    page = await services.navigator_manager.page_for(prov.slug, home_url=prov.home_url)
    authenticated = await prov.authenticated(page)
    signal_reader = getattr(prov, "current_page_signals", None)
    signals = await signal_reader(page) if signal_reader is not None else []
    return {"provider": provider, "authenticated": authenticated, "signals": signals}


@router.get("/api/navigator/providers/{provider}/screenshot")
async def navigator_provider_screenshot(request: Request, provider: str) -> Response:
    """Same-origin, loopback-only screenshot for human MFA/CAPTCHA handoff."""
    services = _services(request)
    prov = _navigator_provider_or_404(services, provider)
    page = await services.navigator_manager.page_for(prov.slug, home_url=prov.home_url)
    png_bytes = await page.screenshot(type="png")
    return Response(content=png_bytes, media_type="image/png")


@router.post("/api/navigator/providers/{provider}/input")
async def navigator_provider_input(
    request: Request, provider: str, payload: NavigatorRemoteInput
) -> dict[str, Any]:
    """Same-origin, loopback-only human input relay (MFA/CAPTCHA handoff)."""
    services = _services(request)
    prov = _navigator_provider_or_404(services, provider)
    page = await services.navigator_manager.page_for(prov.slug, home_url=prov.home_url)
    if payload.kind == "click":
        if payload.x is None or payload.y is None:
            raise HTTPException(422, "'click' requires 'x' and 'y'.")
        await page.mouse.click(payload.x, payload.y)
    elif payload.kind == "type":
        if not payload.text:
            raise HTTPException(422, "'type' requires 'text'.")
        await page.keyboard.type(payload.text)
    elif payload.kind == "key":
        if not payload.key:
            raise HTTPException(422, "'key' requires 'key'.")
        await page.keyboard.press(payload.key)
    return {"ok": True, "provider": provider, "kind": payload.kind}


@router.get("/api/batches")
async def list_batches(request: Request) -> list[dict[str, Any]]:
    services = _services(request)
    output: list[dict[str, Any]] = []
    for row in services.store.list_batches():
        full = services.store.batch(str(row["id"]))
        if full is None:
            continue
        enriched = _enrich_batch(services, full)
        enriched.pop("items", None)
        result = {**row, **enriched}
        for downstream_field in ("complete_count", "needs_operator_count", "error_count"):
            result.pop(downstream_field, None)
        output.append(result)
    return output


@router.post("/api/batches/from-ciq")
async def create_from_ciq(request: Request, payload: CIQBatchCreate) -> dict[str, Any]:
    services = _services(request)
    phases = [str(phase).strip() for phase in payload.phases if str(phase).strip()]
    if not phases:
        raise HTTPException(422, "At least one explicit phase is required.")
    status = await services.ciq.status()
    if not status.get("authorized"):
        raise HTTPException(409, "Calibration IQ is not authorized for ScrapeX.")
    try:
        vehicles = await services.ciq.vehicles_for_phases(
            phases=phases,
            shop=payload.shop,
            source_scope=payload.source_scope,
        )
    except Exception as exc:
        raise HTTPException(
            502,
            f"Calibration IQ import failed: {type(exc).__name__}: {exc}",
        ) from exc
    if not vehicles:
        raise HTTPException(404, "Calibration IQ returned no matching vehicles.")
    batch_id = services.store.create_batch(BatchCreate(name=payload.name, vehicles=vehicles))
    result = _enrich_batch(services, _batch_or_404(services, batch_id))
    result.update(
        imported_from="Calibration IQ",
        source_scope=payload.source_scope,
        phases=phases,
        shop=payload.shop,
    )
    return result


@router.post("/api/batches/from-ciq/exact")
async def create_from_exact_ciq(
    request: Request,
    payload: ExactCIQBatchCreate,
) -> dict[str, Any]:
    """Build a bounded batch from exact RO identifiers without broad import."""
    services = _services(request)
    ro_numbers: list[str] = []
    seen: set[str] = set()
    for raw_value in payload.ro_numbers:
        value = str(raw_value or "").strip()
        if not value or len(value) > 80 or re.search(r"[\x00-\x1f\x7f]", value):
            raise HTTPException(422, "Every staged RO number must be valid.")
        key = value.casefold()
        if key not in seen:
            seen.add(key)
            ro_numbers.append(value)
    status = await services.ciq.status()
    if not status.get("authorized"):
        raise HTTPException(409, "Calibration IQ is not authorized for ScrapeX.")
    exact_lookup = getattr(services.ciq, "vehicles_for_ro_numbers", None)
    if exact_lookup is None:
        raise HTTPException(
            503,
            "This CIQ client does not support fail-closed exact-RO lookup; broad queue fallback is disabled.",
        )
    try:
        vehicles = await exact_lookup(
            ro_numbers=ro_numbers,
            source_scope=payload.source_scope,
        )
    except Exception as exc:
        raise HTTPException(
            502,
            f"Calibration IQ exact-RO import failed: {type(exc).__name__}: {exc}",
        ) from exc

    returned = [str(vehicle.ro_number or "").strip() for vehicle in vehicles]
    returned_ids = [str(vehicle.ro_id or "").strip() for vehicle in vehicles]
    returned_keys = [value.casefold() for value in returned]
    returned_set = set(returned_keys)
    missing = [value for value in ro_numbers if value.casefold() not in returned_set]
    unexpected = [value for value in returned if value.casefold() not in seen]
    duplicates = sorted(
        {
            value
            for value, key in zip(returned, returned_keys, strict=True)
            if returned_keys.count(key) > 1
        }
    )
    missing_ids = [
        returned[index] or f"result-{index + 1}"
        for index, identity in enumerate(returned_ids)
        if not identity
    ]
    reused_ids = sorted(
        {
            identity
            for identity in returned_ids
            if identity and returned_ids.count(identity) > 1
        }
    )
    if (
        missing
        or unexpected
        or duplicates
        or missing_ids
        or reused_ids
        or len(returned) != len(ro_numbers)
    ):
        raise HTTPException(
            409,
            {
                "message": "CIQ did not return one unique exact match for every requested RO.",
                "missing": missing,
                "unexpected": unexpected,
                "duplicates": duplicates,
                "missing_internal_ids": missing_ids,
                "reused_internal_ids": reused_ids,
            },
        )
    batch_id = services.store.create_batch(BatchCreate(name=payload.name, vehicles=vehicles))
    result = _enrich_batch(services, _batch_or_404(services, batch_id))
    result.update(
        imported_from="Calibration IQ exact RO lookup",
        source_scope=payload.source_scope,
        requested_ro_numbers=ro_numbers,
    )
    return result


@router.get("/api/batches/{batch_id}")
async def get_batch(request: Request, batch_id: str) -> dict[str, Any]:
    services = _services(request)
    return _enrich_batch(services, _batch_or_404(services, batch_id))


@router.get("/api/batches/{batch_id}/summary")
async def batch_summary(request: Request, batch_id: str) -> dict[str, Any]:
    services = _services(request)
    batch = _batch_or_404(services, batch_id)
    return {
        "batch_id": batch_id,
        "name": batch.get("name"),
        "batch_state": batch.get("state"),
        "readiness": _readiness(services, batch),
    }


@router.delete("/api/batches/{batch_id}")
async def delete_batch(request: Request, batch_id: str) -> dict[str, Any]:
    services = _services(request)
    result = services.store.delete_batch(batch_id)
    if result.get("reason") == "not_found":
        raise HTTPException(404, "Batch not found.")
    if not result.get("deleted"):
        raise HTTPException(409, result.get("message") or "Batch cannot be deleted.")
    return result


@router.post("/api/batches/{batch_id}/adas-map/process-one/{ro_number}")
async def process_one_adas_map(
    request: Request,
    batch_id: str,
    ro_number: str,
) -> dict[str, Any]:
    services = _services(request)
    batch = _batch_or_404(services, batch_id)
    if batch.get("state") in {"running_adas_map", "pausing"} or _adas_runner_is_running(
        services, batch_id
    ):
        raise HTTPException(409, "Pause the active batch before processing one RO.")
    item = _exact_item(batch, ro_number)
    await _ensure_adas_map_authenticated(services)
    await services.adas_map_runner.process_one(item)
    refreshed = _enrich_batch(services, _batch_or_404(services, batch_id))
    updated = _exact_item(refreshed, ro_number)
    completed = _item_is_adas_complete(updated)
    return {
        "attempted": True,
        "completed": completed,
        "status": "completed" if completed else str(updated.get("adas_map_state") or "unknown"),
        "batch_id": batch_id,
        "ro_number": str(ro_number).strip(),
        "item": updated,
        "readiness": refreshed["readiness"],
    }


@router.post("/api/batches/{batch_id}/adas-map/start")
async def start_adas_map(request: Request, batch_id: str) -> dict[str, Any]:
    services = _services(request)
    batch = _batch_or_404(services, batch_id)
    if batch.get("state") == "pausing":
        raise HTTPException(409, "Wait for the ADAS Map pause boundary to complete.")
    if _adas_runner_is_running(services, batch_id):
        return {
            "started": False,
            "already_running": True,
            "stage": "adas_map",
            "batch": _enrich_batch(services, batch),
        }
    await _ensure_adas_map_authenticated(services)
    await services.adas_map_runner.start(batch_id)
    return {
        "started": True,
        "stage": "adas_map",
        "batch": _enrich_batch(services, _batch_or_404(services, batch_id)),
    }


@router.post("/api/batches/{batch_id}/adas-map/pause")
async def pause_adas_map(request: Request, batch_id: str) -> dict[str, Any]:
    services = _services(request)
    _batch_or_404(services, batch_id)
    await services.adas_map_runner.pause(batch_id)
    return {
        "paused": True,
        "stage": "adas_map",
        "batch": _enrich_batch(services, _batch_or_404(services, batch_id)),
    }


@router.get("/api/batches/{batch_id}/exceptions")
async def batch_exceptions(request: Request, batch_id: str) -> dict[str, Any]:
    services = _services(request)
    batch = _batch_or_404(services, batch_id)
    items = [
        item
        for item in batch.get("items", [])
        if item.get("adas_map_contract_version") == ADAS_MAP_CONTRACT_VERSION
        and str(item.get("adas_map_state") or "")
        in (ADAS_MAP_ATTENTION_STATES | {"retryable_error"})
    ]
    return {
        "batch_id": batch_id,
        "count": len(items),
        "items": items,
        "readiness": _readiness(services, batch),
    }


def create_app(services: AppServices | None = None) -> FastAPI:
    application = FastAPI(title="ScrapeX", version=__version__, lifespan=lifespan)
    application.state.services = services
    application.include_router(router)
    return application


app = create_app()


DASHBOARD = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>ScrapeX</title>
<style>
:root{color-scheme:dark;--bg:#091019;--panel:#111c28;--line:#293b4c;--text:#e8f1f8;--muted:#9eb1c3;--blue:#2185c5;--green:#70dda0;--amber:#ffd479;--red:#ff9d9d}
*{box-sizing:border-box}body{font-family:Segoe UI,Arial,sans-serif;background:var(--bg);color:var(--text);margin:0}main{max-width:1320px;margin:auto;padding:26px}h1{margin:0}.muted{color:var(--muted)}.good{color:var(--green)}.warn{color:var(--amber)}.bad{color:var(--red)}
.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:14px}.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:16px;margin:14px 0}.pill{display:inline-block;background:#223345;border-radius:999px;padding:4px 8px;margin:2px;font-size:12px}
button{background:var(--blue);color:white;border:0;border-radius:8px;padding:9px 12px;font-weight:600;cursor:pointer;margin:3px}button.secondary{background:#304457}button.danger{background:#8b3d3d}button:disabled{opacity:.4;cursor:not-allowed}input,select{background:#0d1620;color:var(--text);border:1px solid #3a5065;border-radius:7px;padding:8px;margin:3px}table{width:100%;border-collapse:collapse;font-size:13px}th,td{padding:8px;border-bottom:1px solid #263747;text-align:left;vertical-align:top}.controls{min-width:365px}.progress{height:8px;background:#091019;border-radius:999px;overflow:hidden;margin-top:5px}.progress div{height:100%;background:var(--blue)}pre{white-space:pre-wrap;max-height:380px;overflow:auto;background:#0b141d;border-radius:8px;padding:10px}.preview{max-height:320px;overflow:auto}@media(max-width:900px){.grid{grid-template-columns:1fr}main{padding:14px}.controls{min-width:260px}}
</style></head><body><main>
<h1>ScrapeX</h1><div class="muted">Calibration IQ → ADAS Map authority → verified CIQ reconciliation · v__VERSION__</div>

<section class="grid">
 <div class="card"><h3>Calibration IQ</h3><div id="ciq">Checking…</div></div>
 <div class="card"><h3>ADAS Map · Work Chrome</h3><div id="adas">Checking…</div><button class="secondary" onclick="openAdas()">Find managed Chrome</button></div>
 <div class="card"><h3>Downstream SI</h3><div id="alldata">Frozen / manual future</div><div class="muted">ADAS SI coverage and ALLDATA acquisition are outside the current automated scope.</div></div>
</section>

<section class="card"><h3>1. Build the CIQ queue</h3>
<label>Batch <input id="batchName" value="Calibration IQ weekly queue" size="32"></label>
<label>Shop <select id="shop"><option value="">All shops</option><option>Macon</option><option>Warner Robins</option><option>Perry</option></select></label>
<div id="phases"></div><button onclick="previewCIQ()">Preview</button><button id="createBatch" disabled onclick="createCIQBatch()">Create batch</button><span id="importMessage" class="muted"></span><div id="preview" class="preview"></div>
<hr style="border-color:#293b4c"><label>Exact staged ROs <input id="exactRos" placeholder="RO1, RO2, RO3" size="36"></label><button onclick="createExactBatch()">Create exact staged batch</button>
</section>

<section class="card"><h3>2. Run staged research</h3>
<div class="muted">Use “Process one” for acceptance ROs, then run the full ADAS Map batch. Readiness means authoritative requirements were extracted and verified against the same CIQ RO. Downstream SI work remains manual/future.</div>
</section>

<section class="card"><h3>Batches and final readiness</h3><div id="batches">Loading…</div></section>
<section class="card"><h3>Details / exceptions</h3><pre id="details">Select a batch summary or exceptions.</pre></section>

<script>
let lastPreview=null;
const phaseBox=Array.from({length:10},(_,i)=>`<label><input type="checkbox" class="phase" value="${i+1}"> ${i+1}</label>`).join(' ');document.getElementById('phases').innerHTML='<b>Phases</b><br>'+phaseBox;
function esc(v){return String(v??'').replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]));}
async function req(url,options={}){const r=await fetch(url,{...options,headers:{'Content-Type':'application/json',...(options.headers||{})}});const text=await r.text();let body;try{body=JSON.parse(text)}catch{body={detail:text}}if(!r.ok)throw new Error(body.detail||text);return body;}
function filters(){return{phases:Array.from(document.querySelectorAll('.phase:checked')).map(x=>x.value),shop:document.getElementById('shop').value||null,source_scope:'active'};}
async function previewCIQ(){const f=filters(),msg=document.getElementById('importMessage');if(!f.phases.length){msg.textContent='Choose at least one phase.';return}msg.textContent=' Previewing…';try{lastPreview=await req('/api/ciq/preview',{method:'POST',body:JSON.stringify(f)});msg.textContent=` ${lastPreview.count} vehicle(s) found.`;document.getElementById('createBatch').disabled=!lastPreview.count;document.getElementById('preview').innerHTML='<table><tr><th>RO</th><th>Shop</th><th>Vehicle</th><th>VIN</th></tr>'+lastPreview.vehicles.map(v=>`<tr><td>${esc(v.ro_number||v.ro_id)}</td><td>${esc(v.shop||'')}</td><td>${esc([v.year,v.make,v.model,v.trim].filter(Boolean).join(' '))}</td><td>${esc(v.vin||'ADAS Map will resolve')}</td></tr>`).join('')+'</table>'}catch(e){msg.textContent=' '+e.message;}}
async function createCIQBatch(){if(!lastPreview)return;const body={name:document.getElementById('batchName').value||'Calibration IQ weekly queue',phases:lastPreview.phases,shop:lastPreview.shop,source_scope:lastPreview.source_scope};try{const b=await req('/api/batches/from-ciq',{method:'POST',body:JSON.stringify(body)});document.getElementById('importMessage').textContent=` Created ${b.readiness.total} vehicle(s).`;lastPreview=null;document.getElementById('createBatch').disabled=true;document.getElementById('preview').innerHTML='';await refresh()}catch(e){alert(e.message)}}
async function createExactBatch(){const ros=document.getElementById('exactRos').value.split(/[\s,]+/).map(x=>x.trim()).filter(Boolean);if(!ros.length){alert('Enter at least one exact RO.');return}const body={name:document.getElementById('batchName').value||'ScrapeX staged acceptance',ro_numbers:ros,source_scope:'all'};try{const b=await req('/api/batches/from-ciq/exact',{method:'POST',body:JSON.stringify(body)});document.getElementById('importMessage').textContent=` Created exact ${b.readiness.total}-RO staged batch.`;document.getElementById('exactRos').value='';await refresh()}catch(e){alert(e.message)}}
function statusClass(ok,warn){return ok?'good':warn?'warn':'bad'}
async function refresh(){
 const [c,a,d,rows]=await Promise.all([req('/api/ciq/status'),req('/api/adas-map/status'),req('/api/browser/status'),req('/api/batches')]);
 document.getElementById('ciq').innerHTML=`<span class="pill">${c.reachable?'reachable':'offline'}</span><span class="pill">${c.authorized?'authorized':'not authorized'}</span><div class="${statusClass(c.authorized,c.reachable)}">${c.authorized?'Ready':'Needs attention'}</div>`;
 document.getElementById('adas').innerHTML=`<span class="pill">${a.active?'active':'not found'}</span><span class="pill">${a.authenticated?'authenticated':'login needed'}</span><div class="${statusClass(a.authenticated,a.active)}">${esc(a.title||a.message||'Managed work Chrome')}</div>`;
 document.getElementById('alldata').innerHTML=`<span class="pill">${d.frozen?'frozen':'manual'}</span><span class="pill">automation off</span><div class="muted">${esc(d.message||'Manual/future downstream scope')}</div>`;
 document.getElementById('batches').innerHTML=rows.length?'<table><tr><th>Batch</th><th>ADAS Map</th><th>CIQ reconciliation</th><th>Current-scope truth</th><th>Controls</th></tr>'+rows.map(row=>{const r=row.readiness||{},total=r.total||0,pct=total?Math.round(100*(r.adas_map_complete||0)/total):0;return `<tr><td><b>${esc(row.name)}</b><br><span class="muted">${esc(row.state)}</span></td><td>${r.adas_map_complete||0}/${total} verified<div class="progress"><div style="width:${pct}%"></div></div><span class="${r.adas_map_attention?'warn':'muted'}">${r.adas_map_attention||0} attention · ${r.adas_map_unresolved||0} unresolved</span></td><td>${r.ciq_reconciled||0}/${total}<br><span class="muted">ADAS Map + CIQ only</span></td><td class="${r.ready?'good':r.needs_operator?'warn':'muted'}"><b>${r.ready?'ADAS/CIQ ready':esc(r.state)}</b><br>${esc((r.blockers||[]).join(' '))}<br><span class="muted">SI downstream: manual/future</span></td><td class="controls"><input id="ro-${row.id}" placeholder="Exact RO" size="14"><button onclick="oneAdas('${row.id}')">Process one ADAS Map</button><br><button onclick="runAdas('${row.id}')">Run ADAS Map batch</button><button class="secondary" onclick="pauseAdas('${row.id}')">Pause</button><br><button class="secondary" onclick="showSummary('${row.id}')">Summary</button><button class="secondary" onclick="showExceptions('${row.id}')">Exceptions</button></td></tr>`}).join('')+'</table>':'No batches yet.';
}
async function action(url){try{await req(url,{method:'POST',body:'{}'});await refresh()}catch(e){alert(e.message)}}
async function oneAdas(id){const ro=document.getElementById('ro-'+id).value.trim();if(!ro){alert('Enter the exact RO number.');return}await action(`/api/batches/${id}/adas-map/process-one/${encodeURIComponent(ro)}`)}
async function runAdas(id){await action(`/api/batches/${id}/adas-map/start`)}async function pauseAdas(id){await action(`/api/batches/${id}/adas-map/pause`)}
async function showSummary(id){try{document.getElementById('details').textContent=JSON.stringify(await req(`/api/batches/${id}/summary`),null,2)}catch(e){alert(e.message)}}async function showExceptions(id){try{document.getElementById('details').textContent=JSON.stringify(await req(`/api/batches/${id}/exceptions`),null,2)}catch(e){alert(e.message)}}
async function openAdas(){await action('/api/adas-map/open')}
refresh();setInterval(refresh,3500);
</script></main></body></html>""".replace("__VERSION__", __version__)
