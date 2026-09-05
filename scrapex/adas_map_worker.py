from __future__ import annotations

import asyncio
import json
import re
from datetime import UTC, datetime
from typing import Any, Protocol

from .ciq import CIQClient, CIQReconciliationError, _valid_authoritative_requirement
from .db import ADAS_MAP_CONTRACT_VERSION, Store
from .models import VehicleSpec


MAX_ATTEMPTS = 3
ITEM_DELAY_SECONDS = 1.5

_FAILURE_STATE = {
    "not_found": "ro_not_found",
    "ro_not_found": "ro_not_found",
    "no_ro_match_visible": "ro_not_found",
    "ambiguous": "ambiguous_ro",
    "ambiguous_ro": "ambiguous_ro",
    "ambiguous_inspection": "ambiguous_ro",
    "view_not_found": "view_not_found",
    "open_failed": "view_not_found",
    "view_did_not_navigate": "view_did_not_navigate",
    "vin_missing": "vin_missing",
    "requirements_unparsed": "requirements_unparsed",
    "inspection_id_missing": "requirements_unparsed",
    "inspection_mismatch": "requirements_unparsed",
    "login_required": "login_required",
}
_RETRYABLE_STATUSES = {
    "retryable_bridge_error",
    "bridge_error",
    "bridge_no_output",
    "bridge_invalid_json",
}
_VIN_RE = re.compile(r"^[A-HJ-NPR-Z0-9]{17}$")


def _shop_key(value: Any) -> str:
    """Compare CIQ and portal shop labels without weakening location identity."""
    folded = " ".join(str(value or "").casefold().split())
    for key in ("warner robins", "macon", "perry"):
        if key in folded:
            return key.replace(" ", "_")
    return re.sub(r"[^a-z0-9]+", "", folded)


class AdasMapSource(Protocol):
    async def status(self) -> dict[str, Any]: ...
    async def open(self) -> dict[str, Any]: ...
    async def lookup(
        self, ro_number: str, shop: str | None, expected: VehicleSpec | None
    ) -> dict[str, Any]: ...


class AdasMapBatchRunner:
    def __init__(
        self,
        store: Store,
        browser: AdasMapSource,
        ciq: CIQClient | None = None,
    ):
        self.store = store
        self.browser = browser
        self.ciq = ciq
        self._tasks: dict[str, asyncio.Task] = {}
        self._pause: set[str] = set()

    async def start(self, batch_id: str) -> None:
        if batch_id in self._tasks and not self._tasks[batch_id].done():
            return
        self._pause.discard(batch_id)
        self.store.set_batch_state(batch_id, "running_adas_map")
        self._tasks[batch_id] = asyncio.create_task(self._run(batch_id))

    async def pause(self, batch_id: str) -> None:
        self._pause.add(batch_id)
        task = self._tasks.get(batch_id)
        pause_error: str | None = None
        if task is not None and not task.done() and task is not asyncio.current_task():
            self.store.set_batch_state(batch_id, "pausing")
            try:
                await asyncio.shield(task)
            except Exception as exc:
                pause_error = f"ADAS Map runner stopped with {type(exc).__name__}: {exc}"
        self.store.set_batch_state(batch_id, "paused", pause_error)

    def is_running(self, batch_id: str) -> bool:
        task = self._tasks.get(batch_id)
        return bool(task is not None and not task.done())

    async def process_one(self, item: dict[str, Any]) -> None:
        """Public, API-safe single-item entry point used by staged acceptance."""
        batch_id = str(item.get("batch_id") or "")
        if self.is_running(batch_id):
            raise RuntimeError("Pause the ADAS Map batch before processing one RO.")
        await self._process_item(item)

    async def _run(self, batch_id: str) -> None:
        try:
            status = await self.browser.status()
            if not status.get("active"):
                status = await self.browser.open()
            if not status.get("authenticated"):
                self.store.set_batch_state(
                    batch_id,
                    "paused",
                    "ADAS Map is not open/authenticated in managed work Chrome.",
                )
                return

            while batch_id not in self._pause:
                item = self.store.next_adas_map_item(batch_id, MAX_ATTEMPTS)
                if item is None:
                    summary = self.store.adas_map_summary(batch_id)
                    attention = int(summary.get("needs_attention") or 0)
                    total = int(summary.get("total") or 0)
                    ready = int(summary.get("ready") or 0)
                    complete = bool(total and ready == total)
                    unresolved = total - ready
                    self.store.set_batch_state(
                        batch_id,
                        "complete" if complete else "paused",
                        (
                            f"{attention or unresolved} ADAS Map item(s) need attention."
                            if not complete else None
                        ),
                    )
                    return
                try:
                    await self._process_item(item)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    attempts = int(item.get("adas_map_attempts") or 0) + 1
                    state = "needs_operator" if attempts >= MAX_ATTEMPTS else "retryable_error"
                    self.store.checkpoint_adas_map(
                        item["id"],
                        state,
                        adas_map_attempts=attempts,
                        adas_map_last_error=f"{type(exc).__name__}: {exc}",
                        adas_map_checked_at=datetime.now(UTC).isoformat(),
                    )
                if batch_id not in self._pause:
                    await asyncio.sleep(ITEM_DELAY_SECONDS)
            self.store.set_batch_state(batch_id, "paused")
        except asyncio.CancelledError:
            self.store.set_batch_state(batch_id, "paused", "ADAS Map task cancelled.")
            raise
        except Exception as exc:
            self.store.set_batch_state(
                batch_id,
                "paused",
                f"ADAS Map runner stopped with {type(exc).__name__}: {exc}",
            )
        finally:
            self._tasks.pop(batch_id, None)

    @staticmethod
    def _requirement_label(requirement: Any) -> str:
        if isinstance(requirement, dict):
            return str(
                requirement.get("calibration_type")
                or requirement.get("label")
                or requirement.get("name")
                or ""
            ).strip()
        return str(requirement or "").strip()

    async def _process_item(self, item: dict[str, Any]) -> None:
        item_id = item["id"]
        current_contract = int(item.get("adas_map_contract_version") or 0)
        previous_attempts = int(item.get("adas_map_attempts") or 0)
        attempts = (
            0 if current_contract < ADAS_MAP_CONTRACT_VERSION else previous_attempts
        ) + 1
        ro_number = str(item.get("ro_number") or "").strip()
        checked_at = datetime.now(UTC).isoformat()
        if not ro_number:
            self.store.checkpoint_adas_map(
                item_id,
                "needs_operator",
                adas_map_attempts=attempts,
                adas_map_last_error="RO number is missing.",
                adas_map_checked_at=checked_at,
            )
            return

        expected = VehicleSpec(
            ro_id=item.get("ro_id"),
            ro_number=ro_number,
            vin=item.get("vin"),
            shop=item.get("shop"),
            year=item.get("year") or None,
            make=item.get("make") or None,
            model=item.get("model") or None,
            trim=item.get("trim"),
            engine=item.get("engine"),
            configuration=item.get("configuration") or {},
            requirements=item.get("ciq_requirements") or [],
        )
        self.store.checkpoint_adas_map(
            item_id,
            "searching_adas_map",
            adas_map_attempts=attempts,
            adas_map_last_error=None,
        )
        result = await self.browser.lookup(
            ro_number=ro_number,
            shop=item.get("shop"),
            expected=expected,
        )
        if not result.get("success"):
            source_status = str(result.get("status") or "retryable_bridge_error")
            if source_status in _RETRYABLE_STATUSES and attempts < MAX_ATTEMPTS:
                state = "retryable_error"
            else:
                state = _FAILURE_STATE.get(source_status, "needs_operator")
            self.store.checkpoint_adas_map(
                item_id,
                state,
                adas_map_attempts=attempts,
                adas_map_url=result.get("details_url") or result.get("url"),
                adas_map_source_url=result.get("source_url"),
                adas_map_inspection_id=result.get("inspection_id"),
                adas_map_last_error=result.get("reason") or result.get("message") or source_status,
                adas_map_raw_result_json=json.dumps(result, sort_keys=True, default=str),
                adas_map_checked_at=checked_at,
            )
            return

        if (
            result.get("report_capture_required") is True
            and result.get("report_capture_verified") is not True
        ):
            self.store.checkpoint_adas_map(
                item_id,
                "needs_operator",
                adas_map_attempts=attempts,
                adas_map_url=result.get("details_url") or result.get("url"),
                adas_map_source_url=result.get("source_url"),
                adas_map_inspection_id=result.get("inspection_id"),
                adas_map_last_error=(
                    result.get("report_error")
                    or "ADAS Map report was not saved to canonical storage."
                ),
                adas_map_raw_result_json=json.dumps(result, sort_keys=True, default=str),
                adas_map_checked_at=checked_at,
            )
            return

        local_report_path = str(result.get("local_report_path") or "").strip()
        if (
            result.get("report_capture_verified") is not True
            or not local_report_path
        ):
            self.store.checkpoint_adas_map(
                item_id,
                "needs_operator",
                adas_map_attempts=attempts,
                adas_map_url=result.get("details_url") or result.get("url"),
                adas_map_source_url=result.get("source_url"),
                adas_map_inspection_id=result.get("inspection_id"),
                adas_map_last_error=(
                    result.get("report_error")
                    or "ADAS Map completion requires a verified canonical PDF path."
                ),
                adas_map_raw_result_json=json.dumps(result, sort_keys=True, default=str),
                adas_map_checked_at=checked_at,
            )
            return

        returned_ro = str(result.get("ro_number") or ro_number).strip()
        returned_ciq_id = str(result.get("ciq_ro_id") or "").strip()
        expected_ciq_id = str(item.get("ro_id") or "").strip()
        returned_shop = " ".join(str(result.get("shop") or "").split())
        expected_shop = " ".join(str(item.get("shop") or "").split())
        if (
            returned_ro != ro_number
            or (expected_ciq_id and returned_ciq_id != expected_ciq_id)
            or (expected_shop and _shop_key(returned_shop) != _shop_key(expected_shop))
        ):
            self.store.checkpoint_adas_map(
                item_id,
                "ambiguous_ro",
                adas_map_attempts=attempts,
                adas_map_last_error="ADAS Map returned a different RO/shop/CIQ binding.",
                adas_map_raw_result_json=json.dumps(result, sort_keys=True, default=str),
                adas_map_checked_at=checked_at,
            )
            return
        self.store.checkpoint_adas_map(item_id, "ro_found")

        vin = str(result.get("vin") or "").strip().upper()
        if not _VIN_RE.fullmatch(vin):
            self.store.checkpoint_adas_map(
                item_id,
                "vin_missing",
                adas_map_last_error="ADAS Map did not prove a valid 17-character VIN.",
                adas_map_checked_at=checked_at,
            )
            return
        self.store.checkpoint_adas_map(item_id, "vin_verified", adas_map_vin=vin)

        inspection_id = str(result.get("inspection_id") or "").strip()
        source_url = str(result.get("source_url") or result.get("details_url") or "").strip()
        if not inspection_id or not source_url:
            self.store.checkpoint_adas_map(
                item_id,
                "requirements_unparsed",
                adas_map_last_error="ADAS Map detail identity/source could not be proven.",
                adas_map_checked_at=checked_at,
            )
            return
        self.store.checkpoint_adas_map(
            item_id,
            "opening_inspection",
            adas_map_inspection_id=inspection_id,
            adas_map_source_url=source_url,
            adas_map_url=result.get("details_url") or source_url,
        )

        explicit_none = result.get("explicit_no_calibration") is True
        proven = result.get("requirements_proven") is True
        requirement_records = result.get("requirement_records")
        modal_runtime_id = str(result.get("modal_runtime_id") or "").strip()
        structure_proven = bool(
            result.get("row_binding_confirmed") is True
            and result.get("modal_inspection_confirmed") is True
            and result.get("required_region_confirmed") is True
            and modal_runtime_id
        )
        record_proven = bool(
            explicit_none
            or (
                isinstance(requirement_records, list)
                and requirement_records
                and all(
                    isinstance(record, dict)
                    and str(record.get("source") or "").casefold()
                    == "adas_map_required_list_item"
                    and str(record.get("source_context") or "").casefold()
                    == "selected_required_modal"
                    and "custom-link"
                    in str(record.get("source_control_class") or "").casefold().split()
                    and str(record.get("source_context_runtime_id") or "").strip()
                    == modal_runtime_id
                    for record in requirement_records
                )
            )
        )
        provenance_proven = structure_proven and record_proven
        requirements = requirement_records if provenance_proven and not explicit_none else []
        labels = [self._requirement_label(value) for value in requirements]
        if (
            not proven
            or not provenance_proven
            or (not requirements and not explicit_none)
            or any(not _valid_authoritative_requirement(label) for label in labels)
        ):
            self.store.checkpoint_adas_map(
                item_id,
                "requirements_unparsed",
                adas_map_requirements_proven=0,
                adas_map_last_error="ADAS Map requirements were not parsed confidently.",
                adas_map_raw_result_json=json.dumps(result, sort_keys=True, default=str),
                adas_map_checked_at=checked_at,
            )
            return

        observed_vehicle = result.get("vehicle")
        if not isinstance(observed_vehicle, dict):
            observed_vehicle = {}
        observed_vehicle = {**observed_vehicle, "vin": vin}
        identity_fields: dict[str, Any] = {}
        for field in ("year", "make", "model", "trim", "engine"):
            value = observed_vehicle.get(field)
            if value not in (None, ""):
                identity_fields[field] = value
        configuration = observed_vehicle.get("configuration")
        if isinstance(configuration, str):
            configuration = configuration.strip()
        if isinstance(configuration, (dict, str)) and configuration:
            identity_fields["configuration_json"] = json.dumps(
                configuration, sort_keys=True
            )

        self.store.checkpoint_adas_map(
            item_id,
            "requirements_captured",
            vin=vin,
            adas_map_vin=vin,
            adas_map_vehicle_label=result.get("vehicle_label"),
            adas_map_calibrations_json=json.dumps(labels),
            adas_map_requirements_json=json.dumps(requirements, sort_keys=True),
            adas_map_alldata_links_json=json.dumps(result.get("alldata_links") or []),
            adas_map_report_links_json=json.dumps(result.get("report_links") or []),
            adas_map_requirements_proven=1,
            adas_map_raw_result_json=json.dumps(result, sort_keys=True, default=str),
            adas_map_checked_at=checked_at,
            requirements_json=json.dumps(labels),
            **identity_fields,
        )

        if self.ciq is None:
            self.store.save_reconciliation(
                item_id,
                "needs_operator",
                None,
                "Calibration IQ operator client is not wired into the ADAS Map runner.",
            )
            self.store.checkpoint_adas_map(
                item_id,
                "needs_operator",
                adas_map_last_error="CIQ reconciliation is unavailable.",
            )
            return

        try:
            reconciliation = await self.ciq.reconcile_requirements(
                repair_order_id=str(item.get("ro_id") or ""),
                requirements=requirements,
                batch_id=str(item.get("batch_id") or ""),
                item_id=item_id,
                inspection_id=inspection_id,
                vehicle=observed_vehicle,
                explicit_no_calibration=explicit_none,
                adas_map_path=local_report_path,
                adas_map_ro_number=ro_number,
                adas_map_source_url=source_url,
            )
        except CIQReconciliationError as exc:
            self.store.save_reconciliation(item_id, "needs_operator", exc.result, str(exc))
            self.store.checkpoint_adas_map(
                item_id,
                "needs_operator",
                adas_map_last_error=f"CIQ reconciliation failed: {exc}",
            )
            return

        self.store.save_reconciliation(item_id, "complete", reconciliation)
        self.store.checkpoint_adas_map(
            item_id,
            "adas_map_complete",
            adas_map_attempts=attempts,
            adas_map_last_error=None,
            adas_map_checked_at=checked_at,
        )
