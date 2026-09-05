from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, UTC
from pathlib import Path
from typing import Any

from .storage_policy import adas_map_pdf_path


def _compact_text(value: Any) -> str:
    return " ".join(str(value or "").split())


def _identity_key(value: Any) -> str:
    return "".join(ch for ch in _compact_text(value).casefold() if ch.isalnum())


def _expected_field(expected: Any, name: str) -> Any:
    if isinstance(expected, dict):
        return expected.get(name)
    return getattr(expected, name, None)


def _normalize_observed_vehicle(
    evidence: Any,
    expected: Any,
) -> tuple[dict[str, Any] | None, str | None, str | None]:
    """Return only ADAS Map-observed identity.

    ADAS Map exposes year and make as row columns but model and configuration as
    one authoritative combined column. A complete, nontruncated CIQ model may
    prove a split boundary; otherwise the combined ADAS value remains the model
    and configuration stays unknown rather than being guessed.
    """
    if not isinstance(evidence, dict):
        return None, "vehicle_identity_unparsed", "ADAS Map did not expose vehicle row evidence."

    raw_year = evidence.get("year")
    try:
        year = int(raw_year)
    except (TypeError, ValueError):
        return None, "vehicle_identity_unparsed", "ADAS Map vehicle year was not parseable."

    make = _compact_text(evidence.get("make"))
    model_configuration = _compact_text(evidence.get("model_configuration"))
    if not (1900 <= year <= 2099 and make and model_configuration):
        return None, "vehicle_identity_unparsed", "ADAS Map vehicle row evidence was incomplete."

    expected_model = _compact_text(_expected_field(expected, "model"))
    hint_is_truncated = expected_model.endswith("...") or expected_model.endswith("…")
    model = model_configuration
    configuration = None
    if expected_model and not hint_is_truncated:
        folded_hint = expected_model.casefold()
        folded_observed = model_configuration.casefold()
        if folded_observed == folded_hint or folded_observed.startswith(folded_hint + " "):
            model = model_configuration[: len(expected_model)]
            configuration = model_configuration[len(expected_model) :].strip() or None

    # Every returned display value is observed in ADAS Map. CIQ is never used
    # to replace year, make, model text, or configuration text.
    vehicle = {
        "year": year,
        "make": make,
        "model": model,
        "configuration": configuration,
        "model_configuration": model_configuration,
    }
    vehicle_label = " ".join(
        str(value) for value in (year, make, model, configuration) if value not in (None, "")
    )
    return vehicle, None, vehicle_label


def _normalized_vehicle_key(vehicle: dict[str, Any]) -> tuple[Any, ...]:
    return (
        vehicle.get("year"),
        _identity_key(vehicle.get("make")),
        _identity_key(vehicle.get("model")),
        _identity_key(vehicle.get("configuration")),
        _identity_key(vehicle.get("model_configuration")),
    )


def _shop_matches(observed: str, requested: str) -> bool:
    def tokens(value: str) -> tuple[str, ...]:
        separated = "".join(
            character.casefold() if character.isalnum() else " "
            for character in _compact_text(value)
        )
        return tuple(part for part in separated.split() if part)

    def contains_phrase(haystack: tuple[str, ...], needle: tuple[str, ...]) -> bool:
        return bool(
            needle
            and len(needle) <= len(haystack)
            and any(
                haystack[index : index + len(needle)] == needle
                for index in range(len(haystack) - len(needle) + 1)
            )
        )

    observed_tokens = tokens(observed)
    requested_tokens = tokens(requested)
    return contains_phrase(observed_tokens, requested_tokens) or contains_phrase(
        requested_tokens,
        observed_tokens,
    )


class WorkChromeBridge:
    """No-admin bridge to an already-open managed Chrome window.

    The bridge does not read Chrome profile files, cookies, credentials, or
    password stores. It invokes Windows UI Automation against the visible
    authenticated ADAS Map window the operator already opened.
    """

    def __init__(self, root: Path):
        self.root = Path(root)
        self.script = self.root / "scripts" / "work-chrome-adas-map.ps1"
        # Windows UI Automation is a single visible-browser control plane.
        # Serializing bridge calls prevents a dashboard diagnostic request from
        # racing the batch runner and acting on a different RO/modal.
        self._lock = asyncio.Lock()

    async def _run(
        self,
        action: str,
        ro_number: str | None = None,
        save_path: str | None = None,
        inspection_id: str | None = None,
        expected: Any = None,
    ) -> dict[str, Any]:
        async with self._lock:
            return await self._run_locked(
                action,
                ro_number=ro_number,
                save_path=save_path,
                inspection_id=inspection_id,
                expected=expected,
            )

    async def _run_locked(
        self,
        action: str,
        ro_number: str | None = None,
        save_path: str | None = None,
        inspection_id: str | None = None,
        expected: Any = None,
    ) -> dict[str, Any]:
        if os.name != "nt":
            return {
                "success": False,
                "status": "windows_required",
                "message": "The work-Chrome bridge requires Windows.",
            }

        if not self.script.is_file():
            return {
                "success": False,
                "status": "bridge_script_missing",
                "message": f"Bridge script not found: {self.script}",
            }

        args = [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(self.script),
            "-Action",
            action,
        ]
        if ro_number:
            args += ["-RoNumber", str(ro_number)]
        if save_path:
            args += ["-SavePath", str(save_path)]
        if inspection_id:
            args += ["-InspectionId", str(inspection_id)]
        expected_year = _expected_field(expected, "year")
        expected_make = _compact_text(_expected_field(expected, "make"))
        expected_model = _compact_text(_expected_field(expected, "model"))
        if expected_year not in (None, ""):
            args += ["-ExpectedYear", str(expected_year)]
        if expected_make:
            args += ["-ExpectedMake", expected_make]
        if expected_model:
            args += ["-ExpectedModel", expected_model]

        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
        except TimeoutError:
            proc.kill()
            await proc.communicate()
            return {
                "success": False,
                "status": "bridge_timeout",
                "message": "The work-Chrome bridge exceeded its 60-second bound.",
            }

        text = stdout.decode("utf-8", errors="replace").strip()
        error_text = stderr.decode("utf-8", errors="replace").strip()

        if not text:
            return {
                "success": False,
                "status": "bridge_no_output",
                "exit_code": proc.returncode,
                "stderr": error_text[:3000],
            }

        # PowerShell should emit one compact JSON object. Be tolerant of a
        # diagnostic prefix and parse the last JSON-looking line.
        candidates = [line.strip() for line in text.splitlines() if line.strip()]
        for candidate in reversed(candidates):
            try:
                payload = json.loads(candidate)
                if error_text:
                    payload.setdefault("stderr", error_text[:3000])
                payload.setdefault("exit_code", proc.returncode)
                return payload
            except json.JSONDecodeError:
                continue

        return {
            "success": False,
            "status": "bridge_invalid_json",
            "exit_code": proc.returncode,
            "stdout": text[-5000:],
            "stderr": error_text[:3000],
        }

    async def status(self) -> dict[str, Any]:
        return await self._run("status")

    async def inspect(self) -> dict[str, Any]:
        return await self._run("inspect")

    async def lookup_test(self, ro_number: str, expected: Any = None) -> dict[str, Any]:
        return await self._run("lookup", ro_number=ro_number, expected=expected)

    async def read_current(self, ro_number: str) -> dict[str, Any]:
        return await self._run("read-current", ro_number=ro_number)

    async def details_test(
        self,
        ro_number: str,
        inspection_id: str | None = None,
        expected: Any = None,
    ) -> dict[str, Any]:
        return await self._run(
            "details",
            ro_number=ro_number,
            inspection_id=inspection_id,
            expected=expected,
        )

    async def close_details(self) -> dict[str, Any]:
        return await self._run("close-details")

    async def download_report(
        self,
        ro_number: str,
        save_path: str,
        inspection_id: str | None = None,
    ) -> dict[str, Any]:
        return await self._run(
            "download-report",
            ro_number=ro_number,
            save_path=save_path,
            inspection_id=inspection_id,
        )


class WorkChromeAdasMapSource:
    """Adapts the Windows UI Automation work-Chrome bridge to the
    status()/open()/lookup() contract AdasMapBatchRunner expects.

    This replaces AdasMapBrowser (a separate Playwright browser) as the
    runner's ADAS Map source. That separate browser was never able to
    authenticate against the managed work Chrome profile -- see the
    work-Chrome bridge design notes in work-chrome-adas-map.ps1. The batch
    runner must drive the same already-authenticated window the operator
    already has open, not a second unauthenticated one.
    """

    # PS1 bridge failure statuses that mean ADAS Map itself could not
    # resolve this RO -- fail closed (needs_operator), never blind-retry.
    _TERMINAL_STATUS_MAP = {
        "no_ro_match_visible": "ro_not_found",
        "search_control_not_found": "portal_layout_unknown",
        "search_value_failed": "portal_layout_unknown",
        "ro_not_visible": "ro_not_found",
        "web_document_not_found": "portal_layout_unknown",
        "ambiguous_ro": "ambiguous_ro",
        "ambiguous_inspection": "ambiguous_inspection",
        "inspection_id_missing": "inspection_id_missing",
        "inspection_mismatch": "inspection_mismatch",
        "vehicle_identity_unparsed": "vehicle_identity_unparsed",
        "vehicle_identity_mismatch": "vehicle_identity_mismatch",
        "row_binding_unproven": "row_binding_unproven",
        "shop_identity_unparsed": "shop_identity_unparsed",
        "shop_identity_mismatch": "shop_identity_mismatch",
        "view_not_found": "view_not_found",
        "view_click_failed": "view_click_failed",
        "view_did_not_navigate": "view_did_not_navigate",
        "requirements_unparsed": "requirements_unparsed",
        "adas_map_window_not_found": "portal_layout_unknown",
        "business_selection_failed": "portal_layout_unknown",
        "business_selection_unconfirmed": "portal_layout_unknown",
    }

    def __init__(self, bridge: WorkChromeBridge, adas_si_root: Path | None = None):
        self.bridge = bridge
        self.adas_si_root = Path(adas_si_root) if adas_si_root else None

    async def status(self) -> dict[str, Any]:
        result = await self.bridge.status()
        target = result.get("target") or {}
        title = str(target.get("title") or "")
        active = bool(result.get("target_found"))
        return {
            "active": active,
            "authenticated": active and "login" not in title.casefold(),
            "url": None,
            "title": title or None,
        }

    async def open(self) -> dict[str, Any]:
        # There is no separate browser to launch here -- ADAS Map only ever
        # runs in the operator's already-open, already-authenticated managed
        # work Chrome window. "Opening" just means checking it is there.
        return await self.status()

    async def lookup(
        self,
        ro_number: str,
        shop: str | None = None,
        expected: Any = None,
    ) -> dict[str, Any]:
        status = await self.status()
        if not status.get("active"):
            return {
                "success": False,
                "status": "portal_layout_unknown",
                "ro_number": ro_number,
                "shop": shop,
                "reason": "No open, authenticated ADAS Map Chrome window was found.",
            }

        lookup_result = await self.bridge.lookup_test(ro_number, expected=expected)
        if not lookup_result.get("success") or lookup_result.get("status") != "ro_visible":
            ps_status = lookup_result.get("status") or "bridge_error"
            observed_shop = _compact_text(lookup_result.get("observed_shop")) or None
            return {
                "success": False,
                "status": self._TERMINAL_STATUS_MAP.get(ps_status, "retryable_bridge_error"),
                "ro_number": ro_number,
                "shop": observed_shop or shop,
                "observed_shop": observed_shop,
                "ciq_requested_shop": shop,
                "bridge_status": ps_status,
                "reason": lookup_result.get("message")
                or f"ADAS Map lookup returned '{ps_status}'.",
                "vin": lookup_result.get("vin"),
                "shop_switch": lookup_result.get("shop_switch"),
                "search_action": lookup_result.get("search_action"),
                "search_attempts": lookup_result.get("search_attempts"),
                "row_expansion": lookup_result.get("row_expansion"),
            }

        if lookup_result.get("row_binding_confirmed") is not True:
            return {
                "success": False,
                "status": "row_binding_unproven",
                "ro_number": ro_number,
                "ciq_requested_shop": shop,
                "reason": "ADAS Map did not prove that View belongs to the exact RO row.",
                "vin": lookup_result.get("vin"),
            }

        observed_shop = _compact_text(lookup_result.get("observed_shop"))
        requested_shop = _compact_text(shop)
        if not observed_shop:
            return {
                "success": False,
                "status": "shop_identity_unparsed",
                "ro_number": ro_number,
                "ciq_requested_shop": shop,
                "reason": "ADAS Map did not expose the selected portal business/shop.",
                "vin": lookup_result.get("vin"),
            }
        if requested_shop and not _shop_matches(observed_shop, requested_shop):
            return {
                "success": False,
                "status": "shop_identity_mismatch",
                "ro_number": ro_number,
                "shop": observed_shop,
                "observed_shop": observed_shop,
                "ciq_requested_shop": shop,
                "reason": "The selected ADAS Map business did not match the CIQ requested shop.",
                "vin": lookup_result.get("vin"),
            }

        inspection_id = str(lookup_result.get("inspection_id") or "").strip() or None
        if not inspection_id:
            return {
                "success": False,
                "status": "inspection_id_missing",
                "ro_number": ro_number,
                "shop": observed_shop,
                "observed_shop": observed_shop,
                "ciq_requested_shop": shop,
                "reason": "ADAS Map exposed the RO, but its inspection ID was not proven.",
                "vin": lookup_result.get("vin"),
            }

        lookup_vehicle, vehicle_status, lookup_vehicle_label = _normalize_observed_vehicle(
            lookup_result.get("vehicle"),
            expected,
        )
        lookup_row_expansion = lookup_result.get("row_expansion")
        if vehicle_status:
            return {
                "success": False,
                "status": vehicle_status,
                "ro_number": ro_number,
                "shop": observed_shop,
                "observed_shop": observed_shop,
                "ciq_requested_shop": shop,
                "vin": lookup_result.get("vin"),
                "inspection_id": inspection_id,
                "reason": lookup_vehicle_label,
            }

        details_result = await self.bridge.details_test(
            ro_number,
            inspection_id=inspection_id,
            expected=expected,
        )
        download_closed_modal = False

        async def finish_details(payload: dict[str, Any]) -> dict[str, Any]:
            payload.setdefault("lookup_row_expansion", lookup_row_expansion)
            payload.setdefault("details_row_expansion", details_result.get("row_expansion"))
            try:
                close_result = await self.bridge.close_details()
            except Exception as exc:  # A dirty modal is an operator-visible failure.
                close_result = {
                    "success": False,
                    "status": "details_close_error",
                    "message": f"{type(exc).__name__}: {exc}",
                }
            payload["detail_close"] = close_result
            closed = (
                (
                    close_result.get("success") is True
                    and close_result.get("status") == "details_closed"
                )
                or (
                    download_closed_modal
                    and close_result.get("status") == "details_not_open"
                )
            )
            if download_closed_modal:
                payload["download_modal_close_verified"] = True
            if payload.get("success") and not closed:
                return {
                    "success": False,
                    "status": "details_close_failed",
                    "ro_number": ro_number,
                    "shop": observed_shop,
                    "observed_shop": observed_shop,
                    "ciq_requested_shop": shop,
                    "vin": payload.get("vin"),
                    "inspection_id": payload.get("inspection_id") or inspection_id,
                    "reason": "ADAS Map details were captured, but the inspection modal did not close.",
                    "detail_close": close_result,
                    "lookup_row_expansion": lookup_row_expansion,
                    "details_row_expansion": details_result.get("row_expansion"),
                }
            return payload

        if not details_result.get("success"):
            ps_status = details_result.get("status") or "bridge_error"
            return await finish_details({
                "success": False,
                "status": self._TERMINAL_STATUS_MAP.get(ps_status, "retryable_bridge_error"),
                "ro_number": ro_number,
                "shop": observed_shop,
                "observed_shop": observed_shop,
                "ciq_requested_shop": shop,
                "reason": details_result.get("message")
                or f"ADAS Map details returned '{ps_status}'.",
                "vin": details_result.get("vin"),
                "inspection_id": details_result.get("inspection_id") or inspection_id,
            })

        vin = details_result.get("vin")
        details_inspection_id = str(details_result.get("inspection_id") or "").strip()
        if details_inspection_id != inspection_id:
            return await finish_details({
                "success": False,
                "status": "inspection_mismatch",
                "ro_number": ro_number,
                "shop": observed_shop,
                "observed_shop": observed_shop,
                "ciq_requested_shop": shop,
                "vin": vin,
                "inspection_id": details_inspection_id or None,
                "reason": (
                    f"ADAS Map opened inspection {details_inspection_id or 'unknown'}, "
                    f"not the proven inspection {inspection_id}."
                ),
            })

        if details_result.get("row_binding_confirmed") is not True:
            return await finish_details({
                "success": False,
                "status": "row_binding_unproven",
                "ro_number": ro_number,
                "shop": observed_shop,
                "observed_shop": observed_shop,
                "ciq_requested_shop": shop,
                "vin": vin,
                "inspection_id": inspection_id,
                "reason": (
                    "ADAS Map detail proof was not bound back to the exact RO row."
                ),
            })


        vehicle, vehicle_status, vehicle_label = _normalize_observed_vehicle(
            details_result.get("vehicle"),
            expected,
        )
        if vehicle_status:
            return await finish_details({
                "success": False,
                "status": vehicle_status,
                "ro_number": ro_number,
                "shop": observed_shop,
                "observed_shop": observed_shop,
                "ciq_requested_shop": shop,
                "vin": vin,
                "inspection_id": inspection_id,
                "reason": vehicle_label,
            })
        if (
            lookup_vehicle is None
            or vehicle is None
            or _normalized_vehicle_key(lookup_vehicle) != _normalized_vehicle_key(vehicle)
        ):
            return await finish_details({
                "success": False,
                "status": "vehicle_identity_mismatch",
                "ro_number": ro_number,
                "shop": observed_shop,
                "observed_shop": observed_shop,
                "ciq_requested_shop": shop,
                "vin": vin,
                "inspection_id": inspection_id,
                "reason": (
                    "ADAS Map vehicle identity changed between the exact row lookup "
                    "and detail proof."
                ),
            })

        detail_confirmed = details_result.get("detail_confirmed") is True
        document_changed = details_result.get("document_changed") is True
        detail_already_visible = details_result.get("detail_already_visible") is True
        modal_inspection_confirmed = (
            details_result.get("modal_inspection_confirmed") is True
        )
        modal_runtime_id = _compact_text(details_result.get("modal_runtime_id"))
        required_context_confirmed = (
            details_result.get("required_context_confirmed") is True
        )
        required_region_confirmed = (
            details_result.get("required_region_confirmed") is True
        )
        parse_confident = details_result.get("requirements_parse_confident") is True
        explicit_no_calibration = details_result.get("explicit_no_calibration") is True

        requirement_records: list[dict[str, Any]] = []
        labels: list[str] = []
        seen_labels: set[str] = set()
        for record in details_result.get("requirements") or []:
            if not isinstance(record, dict):
                continue
            source_class_tokens = {
                token.casefold()
                for token in _compact_text(record.get("source_control_class")).split()
            }
            if (
                record.get("source") != "adas_map_required_list_item"
                or record.get("source_context") != "selected_required_modal"
                or "custom-link" not in source_class_tokens
                or _compact_text(record.get("source_context_runtime_id"))
                != modal_runtime_id
            ):
                continue
            label = " ".join(str(record.get("label") or "").split())
            if not label:
                continue
            key = "".join(ch for ch in label.casefold() if ch.isalnum())
            if not key or key in seen_labels:
                continue
            seen_labels.add(key)
            normalized = dict(record)
            normalized["label"] = label
            requirement_records.append(normalized)
            labels.append(label)

        requirements_proven = bool(
            detail_confirmed
            and (document_changed or detail_already_visible)
            and modal_inspection_confirmed
            and modal_runtime_id
            and required_context_confirmed
            and required_region_confirmed
            and parse_confident
            and (labels or explicit_no_calibration)
        )
        if not requirements_proven:
            navigation_unproven = bool(
                not detail_confirmed
                or not (document_changed or detail_already_visible)
                or not modal_inspection_confirmed
                or not modal_runtime_id
            )
            return await finish_details({
                "success": False,
                "status": (
                    "view_did_not_navigate"
                    if navigation_unproven
                    else "requirements_unparsed"
                ),
                "ro_number": ro_number,
                "shop": observed_shop,
                "observed_shop": observed_shop,
                "ciq_requested_shop": shop,
                "vin": vin,
                "inspection_id": inspection_id,
                "row_binding_confirmed": True,
                "modal_inspection_confirmed": modal_inspection_confirmed,
                "modal_runtime_id": modal_runtime_id or None,
                "required_region_confirmed": required_region_confirmed,
                "reason": (
                    "ADAS Map detail navigation was not authoritatively proven."
                    if navigation_unproven
                    else "ADAS Map requirement rows could not be parsed confidently."
                ),
            })

        calibrations = labels

        if not vin:
            return await finish_details({
                "success": False,
                "status": "vin_missing",
                "ro_number": ro_number,
                "shop": observed_shop,
                "observed_shop": observed_shop,
                "ciq_requested_shop": shop,
                "reason": "ADAS Map details opened, but no VIN could be proven.",
                "calibrations": calibrations,
                "inspection_id": inspection_id,
            })

        report_links: list[str] = []
        report_error: str | None = None

        if self.adas_si_root is not None:
            # Hard storage rule: ADAS Map evidence is organized by RO, never
            # mixed into the Year/Make/Model SI hierarchy.
            save_path = adas_map_pdf_path(self.adas_si_root, ro_number)
            save_path.parent.mkdir(parents=True, exist_ok=True)
            download_result = await self.bridge.download_report(
                ro_number,
                str(save_path),
                inspection_id=inspection_id,
            )
            download_status = download_result.get("status")
            modal_close = (
                download_result.get("modal_close")
                if isinstance(download_result.get("modal_close"), dict)
                else {}
            )
            download_closed_modal = modal_close.get("closed") is True

            if download_result.get("success") or download_status == "target_already_exists":
                # target_already_exists means the report is already on disk
                # from a prior run -- the local ADAS SI library already has
                # it, which is the point (see the dedup rule: don't re-save
                # what's already captured).
                report_links = [str(save_path)]
            else:
                report_error = (
                    download_result.get("message")
                    or f"Report download returned '{download_status or 'bridge_error'}'."
                )

        source_url = _compact_text(details_result.get("source_url")) or None
        if not source_url:
            return await finish_details({
                "success": False,
                "status": "requirements_unparsed",
                "ro_number": ro_number,
                "shop": observed_shop,
                "observed_shop": observed_shop,
                "ciq_requested_shop": shop,
                "vin": vin,
                "inspection_id": inspection_id,
                "reason": "ADAS Map detail source URL could not be proven.",
            })
        observed_report_links = list(details_result.get("report_links") or [])
        report_links = list(dict.fromkeys([*observed_report_links, *report_links]))
        alldata_links = list(dict.fromkeys(details_result.get("alldata_links") or []))
        return await finish_details({
            "success": True,
            "status": "complete",
            "ciq_ro_id": _expected_field(expected, "ro_id") if expected else None,
            "ro_number": ro_number,
            "shop": observed_shop,
            "observed_shop": observed_shop,
            "ciq_requested_shop": shop,
            "vin": vin,
            "vehicle_label": vehicle_label,
            "vehicle": vehicle,
            "inspection_id": inspection_id,
            "row_binding_confirmed": True,
            "modal_inspection_confirmed": modal_inspection_confirmed,
            "modal_runtime_id": modal_runtime_id,
            "required_region_confirmed": required_region_confirmed,
            "calibrations": calibrations,
            "requirements": calibrations,
            "requirement_records": requirement_records,
            "requirements_proven": True,
            "explicit_no_calibration": explicit_no_calibration,
            "source_url": source_url,
            "details_url": source_url,
            "alldata_links": alldata_links,
            "report_links": report_links,
            "report_error": report_error,
            "adas_map": {
                "inspection_id": inspection_id,
                "vehicle": vehicle,
                "requirements": calibrations,
                "requirement_records": requirement_records,
                "requirements_proven": True,
                "explicit_no_calibration": explicit_no_calibration,
                "source_url": source_url,
                "report_links": report_links,
                "alldata_links": alldata_links,
            },
            "captured_at": datetime.now(UTC).isoformat(),
        })
