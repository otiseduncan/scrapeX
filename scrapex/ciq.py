from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from .config import Settings
from .models import CalibrationSnapshot, VehicleSpec

PAGE_SIZE = 100
MAX_ROWS = 500
RECONCILIATION_CONTRACT_VERSION = 2

ACTIVE_DETERMINATIONS = {"REQUIRED", "LIKELY_REQUIRED", "NEEDS_RESEARCH"}
INACTIVE_DETERMINATIONS = {"NOT_REQUIRED", "REMOVED_AFTER_REVIEW"}
RESEARCH_STATES = {
    "research_required",
    "research_in_progress",
    "research_complete",
}

_CALIBRATION_ALIASES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("steering_angle", ("steering angle", "steering-angle", " sas ")),
    ("blind_spot", ("blind spot", "blind-spot", "bsm", "side radar", "lane change assistance")),
    ("surround_view", ("surround view", "around view", "360 degree", "panoramic view")),
    ("occupant", ("occupant classification", "seat weight", "passenger presence", "ocs")),
    ("seat_belt", ("seat belt", "seatbelt")),
    ("front_camera", ("front view camera", "forward camera", "multipurpose camera", "ipma camera", "monocamera", "mono-camera")),
    ("rear_camera", ("rearview camera", "rear view camera", "backup camera", "reverse camera")),
    ("front_radar", ("front radar", "forward range radar", "distance sensor", "millimeter wave radar", "adaptive cruise control sensor", "icc sensor")),
    ("parking_sensor", ("parking aid sensor", "parking assist distance", "park assist sensor")),
)


class CIQReconciliationError(RuntimeError):
    def __init__(self, message: str, *, result: dict[str, Any] | None = None):
        super().__init__(message)
        self.result = result or {}


def calibration_key(value: Any) -> str:
    """Return a conservative alias key; never invent a calibration type."""
    text = " ".join(str(value or "").casefold().replace("/", " ").split())
    padded = f" {text} "
    for key, aliases in _CALIBRATION_ALIASES:
        if any(alias in padded for alias in aliases):
            return key
    return re.sub(r"[^a-z0-9]+", "", text)


def _valid_authoritative_requirement(value: Any) -> bool:
    text = " ".join(str(value or "").split())
    folded = text.casefold()
    if not text or len(text) > 160:
        return False
    if folded in {
        "create calibration",
        "calibration",
        "calibration requirement",
        "calibration requirements",
        "required calibration",
        "required calibrations",
        "required",
        "not required",
        "inspection",
        "inspection details",
        "vehicle information",
        "vehicle details",
        "repair order",
        "report",
        "details",
        "n/a",
        "na",
        "none",
    }:
        return False
    if "calibration not required" in folded or folded.startswith("na:"):
        return False
    if (
        "?" in text
        or "inspection is complete" in folded
        or "create calibration" in folded
        or "click here" in folded
        or re.search(r"https?://|www\.", folded)
    ):
        return False
    if re.match(r"^adas\s+l\d\b", folded):
        return False
    if re.match(r"^(?:add|edit|delete|save|cancel|close|open|view|print|download|select)\b", folded):
        return False
    key = calibration_key(text)
    return bool(key) and bool(re.search(r"[a-z]{2}", folded)) and len(text.split()) <= 18

class CIQClient:
    def __init__(self, settings: Settings):
        self.settings = settings

    def token(self) -> str:
        return self.settings.ciq_token()

    def configured(self) -> bool:
        return bool(self.settings.ciq_base_url and self.token())

    def _headers(self) -> dict[str, str]:
        token = self.token()
        return {"Authorization": f"Bearer {token}"} if token else {}

    async def status(self) -> dict[str, Any]:
        base = self.settings.ciq_base_url
        token = self.token()
        env_path = self.settings.ciq_project_path / ".env"

        reachable = False
        health_status = None
        try:
            async with httpx.AsyncClient(timeout=4, trust_env=False) as client:
                r = await client.get(f"{base}/health")
                health_status = r.status_code
                reachable = r.status_code in {200, 401, 403}
        except httpx.HTTPError as exc:
            return {
                "configured": False,
                "reachable": False,
                "authorized": False,
                "base_url": base,
                "token_present": bool(token),
                "env_path": str(env_path),
                "error": type(exc).__name__,
            }

        if not token:
            return {
                "configured": False,
                "reachable": reachable,
                "authorized": False,
                "base_url": base,
                "token_present": False,
                "env_path": str(env_path),
                "env_keys_present": self.settings.ciq_token_key_names(),
                "http_status": health_status,
            }

        try:
            async with httpx.AsyncClient(timeout=8, trust_env=False) as client:
                probe = await client.get(
                    f"{base}/collection/ros",
                    params={"limit": 1, "offset": 0, "source_scope": "active"},
                    headers=self._headers(),
                )
            authorized = probe.status_code == 200
            return {
                "configured": authorized,
                "reachable": reachable,
                "authorized": authorized,
                "base_url": base,
                "token_present": True,
                "env_path": str(env_path),
                "http_status": probe.status_code,
            }
        except httpx.HTTPError as exc:
            return {
                "configured": False,
                "reachable": reachable,
                "authorized": False,
                "base_url": base,
                "token_present": True,
                "env_path": str(env_path),
                "error": type(exc).__name__,
            }

    async def operator_capabilities(self) -> dict[str, Any]:
        if not self.configured():
            raise CIQReconciliationError("Calibration IQ service token is unavailable.")
        async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
            response = await client.get(
                f"{self.settings.ciq_base_url}/operator/capabilities",
                headers=self._headers(),
            )
        if response.status_code in {401, 403}:
            raise CIQReconciliationError(
                f"Calibration IQ operator authorization failed (HTTP {response.status_code})."
            )
        response.raise_for_status()
        body = response.json()
        routine = set(((body.get("policy") or {}).get("routine") or []))
        if not {
            "update_ro",
            "add_calibration",
            "update_calibration",
            "update_research",
            "import_document",
        }.issubset(routine):
            raise CIQReconciliationError(
                "Calibration IQ does not advertise required reconciliation mutations."
            )
        if not ((body.get("batch") or {}).get("authoritative_verification")):
            raise CIQReconciliationError(
                "Calibration IQ does not advertise authoritative receipt verification."
            )
        return body

    @staticmethod
    def _items_from_body(body: Any) -> list[dict[str, Any]]:
        if not isinstance(body, dict):
            return []
        for key in ("items", "rows", "repair_orders", "results"):
            value = body.get(key)
            if isinstance(value, list):
                return [x for x in value if isinstance(x, dict)]
        data = body.get("data")
        if isinstance(data, dict):
            for key in ("items", "rows", "repair_orders", "results"):
                value = data.get(key)
                if isinstance(value, list):
                    return [x for x in value if isinstance(x, dict)]
        return []

    @staticmethod
    def _count_from_body(body: Any, fallback: int) -> int:
        if not isinstance(body, dict):
            return fallback
        for key in ("count", "total", "total_count", "match_count"):
            value = body.get(key)
            if isinstance(value, int):
                return value
        data = body.get("data")
        if isinstance(data, dict):
            for key in ("count", "total", "total_count", "match_count"):
                value = data.get(key)
                if isinstance(value, int):
                    return value
        return fallback

    async def _all_rows(
        self,
        phase: str | None,
        shop: str | None,
        source_scope: str = "active",
        query: str | None = None,
    ) -> list[dict[str, Any]]:
        status = await self.status()
        if not status.get("authorized"):
            raise RuntimeError(
                f"Calibration IQ is not authorized for ScrapeX "
                f"(HTTP {status.get('http_status')})."
            )

        base = self.settings.ciq_base_url
        offset = 0
        rows: list[dict[str, Any]] = []
        expected_total: int | None = None

        async with httpx.AsyncClient(timeout=20, trust_env=False) as client:
            for _ in range(100):
                params: dict[str, Any] = {
                    "limit": PAGE_SIZE,
                    "offset": offset,
                    "source_scope": source_scope,
                }
                if phase:
                    params["phase"] = phase
                if shop:
                    params["shop"] = shop
                if query:
                    params["q"] = query

                resp = await client.get(
                    f"{base}/collection/ros",
                    params=params,
                    headers=self._headers(),
                )
                if resp.status_code == 401:
                    raise RuntimeError("Calibration IQ rejected the service token.")
                resp.raise_for_status()

                body = resp.json()
                batch = self._items_from_body(body)
                if expected_total is None:
                    expected_total = self._count_from_body(body, len(batch))

                rows.extend(batch)
                if not batch:
                    break

                offset += len(batch)
                if offset >= expected_total or len(rows) >= MAX_ROWS:
                    break

        return rows[:MAX_ROWS]

    async def _snapshot(self, ro_id: str) -> dict[str, Any]:
        base = self.settings.ciq_base_url
        async with httpx.AsyncClient(timeout=20, trust_env=False) as client:
            resp = await client.get(
                f"{base}/operator/ros/{ro_id}/snapshot",
                headers=self._headers(),
            )
            if resp.status_code == 401:
                raise RuntimeError("Calibration IQ rejected the service token.")
            resp.raise_for_status()
            body = resp.json()

        if not isinstance(body, dict):
            return {}
        if isinstance(body.get("snapshot"), dict):
            return body["snapshot"]
        if isinstance(body.get("data"), dict) and isinstance(body["data"].get("snapshot"), dict):
            return body["data"]["snapshot"]
        return body

    @staticmethod
    def _v(obj: Any, *paths: str):
        for path in paths:
            cur = obj
            for part in path.split("."):
                cur = cur.get(part) if isinstance(cur, dict) else None
                if cur is None:
                    break
            if cur not in (None, "", [], {}):
                return cur
        return None

    async def vehicles(
        self,
        phase: str | None = None,
        shop: str | None = None,
        source_scope: str = "active",
        query: str | None = None,
    ) -> list[VehicleSpec]:
        rows = await self._all_rows(phase, shop, source_scope, query)
        out: list[VehicleSpec] = []

        for row in rows:
            ro_id = str(self._v(row, "id", "repair_order_id", "uuid") or "").strip()
            if not ro_id:
                raise RuntimeError("Calibration IQ collection row is missing its RO identity.")

            snap = await self._snapshot(ro_id)
            vehicle = snap.get("vehicle") if isinstance(snap.get("vehicle"), dict) else {}
            ro = snap.get("repair_order") if isinstance(snap.get("repair_order"), dict) else {}
            snapshot_id = str(ro.get("id") or "").strip()
            snapshot_ro_number = str(ro.get("ro_number") or ro.get("number") or "").strip()
            collection_ro_number = str(
                self._v(row, "ro_number", "number", "RO") or ""
            ).strip()
            if not snapshot_id or snapshot_id != ro_id or not snapshot_ro_number:
                raise RuntimeError(
                    f"Calibration IQ snapshot for RO identity {ro_id!r} is missing or inconsistent."
                )
            if (
                collection_ro_number
                and collection_ro_number.casefold() != snapshot_ro_number.casefold()
            ):
                raise RuntimeError(
                    f"Calibration IQ collection/snapshot RO mismatch for identity {ro_id!r}."
                )

            calibrations = snap.get("calibrations")
            if not isinstance(calibrations, list):
                calibrations = []

            existing_calibrations: list[CalibrationSnapshot] = []
            for calibration in calibrations:
                if not isinstance(calibration, dict):
                    continue
                calibration_id = str(calibration.get("id") or "").strip()
                calibration_type = str(calibration.get("calibration_type") or "").strip()
                if not calibration_id or not calibration_type:
                    continue
                existing_calibrations.append(
                    CalibrationSnapshot(
                        id=calibration_id,
                        calibration_type=calibration_type,
                        determination=str(calibration.get("determination") or "").upper(),
                        method=str(calibration.get("method") or "") or None,
                        version=max(1, int(calibration.get("version") or 1)),
                    )
                )

            requirements = [
                str(c.get("calibration_type") or "").strip()
                for c in calibrations
                if isinstance(c, dict)
                and str(c.get("determination") or "").upper()
                    in {"REQUIRED", "LIKELY_REQUIRED"}
                and str(c.get("calibration_type") or "").strip()
            ]

            year = self._v(vehicle, "year") or self._v(ro, "year")
            make = self._v(vehicle, "make") or self._v(ro, "make")
            model = self._v(vehicle, "model") or self._v(ro, "model")
            configuration = vehicle.get("configuration")
            if isinstance(configuration, str):
                configuration = configuration.strip()
            elif not isinstance(configuration, dict):
                configuration = {}
            location = snap.get("location") if isinstance(snap.get("location"), dict) else {}
            snapshot_shop_value = snap.get("shop")
            snapshot_shop = snapshot_shop_value if isinstance(snapshot_shop_value, dict) else {}
            shop_name = (
                self._v(ro, "shop", "shop_name", "location_name")
                or (snapshot_shop_value if isinstance(snapshot_shop_value, str) else None)
                or self._v(snapshot_shop, "name", "display_name")
                or self._v(location, "name", "display_name")
            )

            out.append(
                VehicleSpec(
                    ro_id=ro_id,
                    ro_number=snapshot_ro_number,
                    vin=str(
                        self._v(vehicle, "vin")
                        or self._v(ro, "vin")
                        or ""
                    ) or None,
                    shop=str(shop_name) if shop_name else None,
                    year=int(year) if year else None,
                    make=str(make) if make else None,
                    model=str(model) if model else None,
                    trim=str(
                        self._v(vehicle, "trim")
                        or self._v(ro, "trim")
                        or ""
                    ) or None,
                    engine=str(
                        self._v(vehicle, "configuration.engine", "engine")
                        or ""
                    ) or None,
                    configuration=configuration,
                    requirements=requirements,
                    existing_calibrations=existing_calibrations,
                )
            )

        return out


    async def vehicles_for_ro_numbers(
        self,
        ro_numbers: list[str],
        source_scope: str = "all",
    ) -> list[VehicleSpec]:
        """Resolve an explicit RO list with query narrowing and exact checks.

        CIQ's ``q`` parameter is only a candidate search.  ScrapeX still
        requires exactly one snapshot-backed, exact RO-number match for every
        requested value, and fails the whole selection if any value is absent
        or ambiguous.
        """
        requested = [str(value or "").strip() for value in ro_numbers]
        if not requested or any(not value for value in requested):
            raise ValueError("At least one non-empty CIQ RO number is required.")
        normalized = [value.casefold() for value in requested]
        if len(set(normalized)) != len(normalized):
            raise ValueError("CIQ RO numbers must be unique.")
        if len(requested) > MAX_ROWS:
            raise ValueError(f"At most {MAX_ROWS} CIQ RO numbers may be selected.")

        selected: list[VehicleSpec] = []
        selected_ids: set[str] = set()
        for expected, expected_key in zip(requested, normalized, strict=True):
            candidates = await self.vehicles(
                source_scope=source_scope,
                query=expected,
            )
            exact_by_id: dict[str, VehicleSpec] = {}
            for candidate in candidates:
                observed = str(candidate.ro_number or "").strip()
                if observed.casefold() != expected_key:
                    continue
                identity = str(candidate.ro_id or "").strip()
                if not identity:
                    continue
                exact_by_id[identity] = candidate
            exact = list(exact_by_id.values())
            if len(exact) != 1:
                qualifier = "no" if not exact else "multiple"
                raise RuntimeError(
                    f"CIQ returned {qualifier} exact snapshot-backed match for RO {expected!r}."
                )
            selected_id = str(exact[0].ro_id or "").strip()
            if not selected_id or selected_id in selected_ids:
                raise RuntimeError(
                    "CIQ exact-RO selection reused or omitted an internal RO identity."
                )
            selected_ids.add(selected_id)
            selected.append(exact[0])
        return selected


    async def vehicles_for_phases(
        self,
        phases: list[str],
        shop: str | None = None,
        source_scope: str = "active",
    ) -> list[VehicleSpec]:
        """Collect one or more explicit phases and dedupe by RO identity."""
        merged: dict[str, VehicleSpec] = {}
        for phase in phases:
            phase_value = str(phase or "").strip()
            if not phase_value:
                continue
            for vehicle in await self.vehicles(
                phase=phase_value,
                shop=shop,
                source_scope=source_scope,
            ):
                key = str(vehicle.ro_id or vehicle.ro_number or vehicle.vin or vehicle.label)
                previous = merged.get(key)
                if previous is not None and str(previous.ro_number or "").casefold() != str(
                    vehicle.ro_number or ""
                ).casefold():
                    raise RuntimeError(
                        "Calibration IQ reused one internal identity for different RO numbers."
                    )
                merged[key] = vehicle
        return list(merged.values())

    @staticmethod
    def _calibrations(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
        values = snapshot.get("calibrations")
        return [value for value in values if isinstance(value, dict)] if isinstance(values, list) else []

    @staticmethod
    def _idempotency(payload: dict[str, Any]) -> str:
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        return f"scrapex-{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"

    async def _post_actions(self, actions: list[dict[str, Any]]) -> dict[str, Any]:
        if not actions:
            return {
                "success": True,
                "partial": False,
                "requested_count": 0,
                "processed_count": 0,
                "receipts": [],
            }
        async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
            response = await client.post(
                f"{self.settings.ciq_base_url}/operator/actions",
                headers=self._headers(),
                json={"actions": actions, "continue_on_error": True},
            )
        if response.status_code in {401, 403}:
            raise CIQReconciliationError(
                f"Calibration IQ operator authorization failed (HTTP {response.status_code})."
            )
        response.raise_for_status()
        body = response.json()
        receipts = body.get("receipts") if isinstance(body, dict) else None
        if not isinstance(receipts, list) or len(receipts) != len(actions):
            raise CIQReconciliationError(
                "Calibration IQ returned an incomplete mutation receipt set.",
                result=body if isinstance(body, dict) else {},
            )
        for action, receipt in zip(actions, receipts, strict=True):
            verified = isinstance(receipt, dict) and bool(
                (receipt.get("verification") or {}).get("verified")
            )
            valid = (
                isinstance(receipt, dict)
                and receipt.get("idempotency_key") == action["idempotency_key"]
                and receipt.get("operation") == action["operation"]
                and receipt.get("status") == "completed"
                and receipt.get("success") is True
                and not receipt.get("indeterminate")
                and verified
            )
            if not valid:
                raise CIQReconciliationError(
                    "Calibration IQ did not return a verified completed receipt for every action.",
                    result=body,
                )
        return body

    @staticmethod
    def _research_documents(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        seen: set[str] = set()
        sources = [
            snapshot.get("documents"),
            (snapshot.get("research") or {}).get("documents")
            if isinstance(snapshot.get("research"), dict)
            else None,
            (snapshot.get("research_case") or {}).get("documents")
            if isinstance(snapshot.get("research_case"), dict)
            else None,
        ]
        for items in sources:
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict) or item.get("archived_at"):
                    continue
                key = str(
                    item.get("id")
                    or item.get("document_id")
                    or item.get("source_uri")
                    or ""
                ).strip()
                if not key or key in seen:
                    continue
                seen.add(key)
                output.append(item)
        return output

    async def _prepare_adas_map_evidence(
        self,
        *,
        snapshot: dict[str, Any],
        repair_order_id: str,
        adas_map_path: str,
        ro_number: str,
        batch_id: str,
        item_id: str,
        inspection_id: str | None,
        source_url: str | None,
    ) -> tuple[
        dict[str, Any],
        dict[str, Any],
        dict[str, Any] | None,
        list[dict[str, Any]],
    ]:
        """Attach the exact canonical ADAS Map PDF and enter active research.

        This is part of ADAS Map completion, not a later best-effort side effect:
        if the document or research-state transition cannot be authoritatively
        verified, reconciliation fails and the batch item cannot become complete.
        """
        report = Path(adas_map_path).expanduser()
        if (
            not ro_number
            or not report.is_file()
            or report.stat().st_size < 256
        ):
            raise CIQReconciliationError(
                "The canonical ADAS Map PDF is missing or invalid."
            )
        with report.open("rb") as handle:
            if handle.read(5) != b"%PDF-":
                raise CIQReconciliationError(
                    "The canonical ADAS Map capture is not a valid PDF."
                )

        relative = f"ADAS Map/{ro_number}/{ro_number} ADAS Map.pdf"
        source_uri = f"adas-si:///{quote(relative)}"
        source_name = report.name
        correlation = f"scrapex-{batch_id[:20]}-{item_id[:20]}"[:80]
        receipts: list[dict[str, Any]] = []

        def matching_document(current: dict[str, Any]) -> dict[str, Any] | None:
            exact = [
                document
                for document in self._research_documents(current)
                if str(document.get("source_uri") or "").strip().casefold()
                == source_uri.casefold()
            ]
            if len(exact) == 1:
                return exact[0]
            by_name = [
                document
                for document in self._research_documents(current)
                if str(document.get("source_name") or "").strip().casefold()
                == source_name.casefold()
                or str(document.get("original_filename") or "").strip().casefold()
                == source_name.casefold()
            ]
            return by_name[0] if len(by_name) == 1 else None

        def is_adas_map_document(document: dict[str, Any]) -> bool:
            return (
                str(document.get("semantic_type") or "").strip().casefold()
                == "adas_map_report"
                and str(document.get("document_type") or "").strip().casefold()
                == "adas_map_report"
            )

        document = matching_document(snapshot)
        if document is not None and not is_adas_map_document(document):
            document_id = str(
                document.get("id") or document.get("document_id") or ""
            ).strip()
            try:
                document_version = int(document.get("version") or 0)
            except (TypeError, ValueError):
                document_version = 0
            if not document_id or document_version < 1:
                raise CIQReconciliationError(
                    "The existing ADAS Map file cannot be safely reclassified in Calibration IQ."
                )
            retag_arguments = {
                "changes": {
                    "document_type": "adas_map_report",
                    "semantic_type": "ADAS_MAP_REPORT",
                    "status": "validated",
                    "source_uri": source_uri,
                    "source_name": source_name,
                    "title": f"{ro_number} ADAS Map",
                }
            }
            retag_identity = {
                "reconciliation_contract_version": RECONCILIATION_CONTRACT_VERSION,
                "operation": "update_document",
                "repair_order_id": repair_order_id,
                "target_id": document_id,
                "expected_version": document_version,
                "source_uri": source_uri,
            }
            response = await self._post_actions([
                {
                    "idempotency_key": self._idempotency(retag_identity),
                    "correlation_id": correlation,
                    "operation": "update_document",
                    "repair_order_id": repair_order_id,
                    "target_id": document_id,
                    "expected_version": document_version,
                    "arguments": retag_arguments,
                }
            ])
            receipts.extend(list(response.get("receipts") or []))
            snapshot = await self._snapshot(repair_order_id)
            document = matching_document(snapshot)
            if document is None or not is_adas_map_document(document):
                raise CIQReconciliationError(
                    "Calibration IQ did not verify the repaired ADAS Map classification.",
                    result={"receipts": receipts},
                )
        if document is None:
            digest = hashlib.sha256(report.read_bytes()).hexdigest()
            arguments = {
                "source_path": str(report.resolve()),
                "destination_path": f"supporting-documents/{source_name}",
                "document_type": "adas_map_report",
                "semantic_type": "ADAS_MAP_REPORT",
                "title": f"{ro_number} ADAS Map",
                "status": "validated",
                "source_uri": source_uri,
                "source_name": source_name,
                "citation": f"ADAS Map coverage report for RO {ro_number}.",
                "notes": (
                    f"Captured by ScrapeX"
                    f"{f' from inspection {inspection_id}' if inspection_id else ''}."
                ),
                "evidence_role": "JUSTIFICATION",
            }
            identity = {
                "reconciliation_contract_version": RECONCILIATION_CONTRACT_VERSION,
                "operation": "import_document",
                "repair_order_id": repair_order_id,
                "source_uri": source_uri,
                "sha256": digest,
            }
            response = await self._post_actions([
                {
                    "idempotency_key": self._idempotency(identity),
                    "correlation_id": correlation,
                    "operation": "import_document",
                    "repair_order_id": repair_order_id,
                    "arguments": arguments,
                }
            ])
            receipts.extend(list(response.get("receipts") or []))
            snapshot = await self._snapshot(repair_order_id)
            document = matching_document(snapshot)
            if document is None:
                raise CIQReconciliationError(
                    "Calibration IQ did not verify the attached ADAS Map document.",
                    result={"receipts": receipts},
                )

        attachment = {
            "attached": True,
            "document_id": str(
                document.get("id") or document.get("document_id") or ""
            ).strip()
            or None,
            "source_uri": source_uri,
            "source_name": source_name,
            "status": str(document.get("status") or "").strip().casefold() or None,
            "semantic_type": (
                str(document.get("semantic_type") or "").strip() or None
            ),
        }

        research = snapshot.get("research")
        if not isinstance(research, dict):
            raise CIQReconciliationError(
                "Calibration IQ did not provide an authoritative research case "
                "after the ADAS Map attachment.",
                result={"adas_map_attachment": attachment, "receipts": receipts},
            )
        research_state = str(research.get("state") or "").strip().casefold()
        try:
            research_version = int(research.get("version"))
        except (TypeError, ValueError):
            research_version = 0
        if (
            research_state not in RESEARCH_STATES
            or research_version < 1
        ):
            raise CIQReconciliationError(
                "Calibration IQ research state is not authoritative after ADAS Map attachment.",
                result={"adas_map_attachment": attachment, "receipts": receipts},
            )

        research_started: dict[str, Any] | None = None
        if research_state == "research_required":
            arguments = {
                "state": "research_in_progress",
                "reason": (
                    "ADAS Map acquired and attached; OEM service-information "
                    "research is now in progress."
                ),
            }
            identity = {
                "reconciliation_contract_version": RECONCILIATION_CONTRACT_VERSION,
                "operation": "update_research",
                "repair_order_id": repair_order_id,
                "expected_version": research_version,
                "arguments": arguments,
                "inspection_id": inspection_id,
            }
            response = await self._post_actions([
                {
                    "idempotency_key": self._idempotency(identity),
                    "correlation_id": correlation,
                    "operation": "update_research",
                    "repair_order_id": repair_order_id,
                    "expected_version": research_version,
                    "arguments": arguments,
                }
            ])
            start_receipts = list(response.get("receipts") or [])
            receipts.extend(start_receipts)
            snapshot = await self._snapshot(repair_order_id)
            final_research = snapshot.get("research")
            final_state = (
                str(final_research.get("state") or "").strip().casefold()
                if isinstance(final_research, dict)
                else ""
            )
            try:
                final_version = int(
                    final_research.get("version")
                    if isinstance(final_research, dict)
                    else 0
                )
            except (TypeError, ValueError):
                final_version = 0
            if final_state != "research_in_progress" or final_version <= research_version:
                raise CIQReconciliationError(
                    "Calibration IQ did not verify research-in-progress after "
                    "the ADAS Map attachment.",
                    result={
                        "adas_map_attachment": attachment,
                        "receipts": receipts,
                    },
                )
            start_receipt = start_receipts[0] if start_receipts else {}
            research_started = {
                "from_state": "research_required",
                "to_state": "research_in_progress",
                "from_version": research_version,
                "to_version": final_version,
                "mutation_id": start_receipt.get("mutation_id"),
                "idempotency_key": start_receipt.get("idempotency_key"),
                "replayed": bool(start_receipt.get("replayed")),
            }

        return snapshot, attachment, research_started, receipts

    async def reconcile_requirements(
        self,
        *,
        repair_order_id: str,
        requirements: list[Any],
        batch_id: str,
        item_id: str,
        inspection_id: str | None,
        vehicle: dict[str, Any] | None = None,
        explicit_no_calibration: bool = False,
        adas_map_path: str | None = None,
        adas_map_ro_number: str | None = None,
        adas_map_source_url: str | None = None,
    ) -> dict[str, Any]:
        """Keep/add/reactivate CIQ rows from ADAS Map and verify receipts.

        Absence from one ADAS Map result never deletes a CIQ row. ScrapeX has
        no reliable provenance field that would distinguish a prior ADAS Map
        row from a human-created requirement, so automatic deactivation would
        exceed the authority supplied by this observation.
        """
        if not repair_order_id:
            raise CIQReconciliationError("The CIQ repair_order_id is missing.")
        await self.operator_capabilities()

        normalized: list[dict[str, str]] = []
        seen: set[str] = set()
        for requirement in requirements:
            if isinstance(requirement, dict):
                label = str(
                    requirement.get("calibration_type")
                    or requirement.get("label")
                    or requirement.get("name")
                    or ""
                ).strip()
                method = str(requirement.get("method") or "UNKNOWN").upper()
            else:
                label = str(requirement or "").strip()
                method = "UNKNOWN"
            if not _valid_authoritative_requirement(label):
                raise CIQReconciliationError(
                    f"ADAS Map requirement is not safe to reconcile: {label!r}."
                )
            key = calibration_key(label)
            if key in seen:
                continue
            seen.add(key)
            normalized.append(
                {
                    "key": key,
                    "label": label,
                    "method": method if method in {"STATIC", "DYNAMIC", "BOTH", "INSPECTION_ONLY"} else "UNKNOWN",
                }
            )

        if not normalized and not explicit_no_calibration:
            raise CIQReconciliationError(
                "ADAS Map did not prove a calibration requirement set."
            )

        before = await self._snapshot(repair_order_id)
        adas_map_attachment: dict[str, Any] | None = None
        research_started: dict[str, Any] | None = None
        adas_map_receipts: list[dict[str, Any]] = []
        if adas_map_path:
            before, adas_map_attachment, research_started, adas_map_receipts = (
                await self._prepare_adas_map_evidence(
                    snapshot=before,
                    repair_order_id=repair_order_id,
                    adas_map_path=adas_map_path,
                    ro_number=str(adas_map_ro_number or "").strip(),
                    batch_id=batch_id,
                    item_id=item_id,
                    inspection_id=inspection_id,
                    source_url=adas_map_source_url,
                )
            )
        existing = self._calibrations(before)
        if explicit_no_calibration:
            active_conflicts = [
                {
                    "id": str(row.get("id") or ""),
                    "calibration_type": str(row.get("calibration_type") or ""),
                    "determination": str(row.get("determination") or "").upper(),
                }
                for row in existing
                if str(row.get("determination") or "").upper()
                in ACTIVE_DETERMINATIONS
            ]
            if active_conflicts:
                raise CIQReconciliationError(
                    "ADAS Map proved no calibration required, but CIQ has active "
                    "requirements; manual review is required before any deactivation.",
                    result={"active_calibration_conflicts": active_conflicts},
                )
        by_key: dict[str, list[dict[str, Any]]] = {}
        for row in existing:
            key = calibration_key(row.get("calibration_type"))
            if key:
                by_key.setdefault(key, []).append(row)

        kept: list[dict[str, Any]] = []
        planned: list[dict[str, Any]] = []
        correlation = f"scrapex-{batch_id[:20]}-{item_id[:20]}"[:80]
        observed_vehicle = vehicle if isinstance(vehicle, dict) else {}
        current_vehicle = before.get("vehicle") if isinstance(before.get("vehicle"), dict) else {}
        current_ro = before.get("repair_order") if isinstance(before.get("repair_order"), dict) else {}
        vehicle_changes: dict[str, Any] = {}
        vehicle_fields = {
            "vin": ("vin",),
            "year": ("year",),
            "make": ("make",),
            "model": ("model",),
            "trim": ("trim",),
        }
        for target_field, source_fields in vehicle_fields.items():
            observed = next(
                (observed_vehicle.get(name) for name in source_fields if observed_vehicle.get(name) not in (None, "")),
                None,
            )
            if observed in (None, ""):
                continue
            if target_field == "vin":
                observed = str(observed).strip().upper()
                if not re.fullmatch(r"[A-HJ-NPR-Z0-9]{17}", observed):
                    raise CIQReconciliationError("ADAS Map returned an invalid VIN.")
            elif target_field == "year":
                observed = int(observed)
                if observed < 1981 or observed > 2100:
                    raise CIQReconciliationError("ADAS Map returned an invalid model year.")
            else:
                observed = str(observed).strip()
            current = current_ro.get(target_field)
            if current in (None, ""):
                current = current_vehicle.get(target_field)
            if str(current or "").strip().casefold() != str(observed).strip().casefold():
                vehicle_changes[target_field] = observed
        observed_configuration = observed_vehicle.get("configuration")
        if isinstance(observed_configuration, str):
            observed_configuration = observed_configuration.strip()
        observed_model_configuration = str(
            observed_vehicle.get("model_configuration") or ""
        ).strip()
        if (
            isinstance(observed_configuration, (dict, str))
            and observed_configuration
        ) or observed_model_configuration:
            raw_current_configuration = (
                current_ro.get("vehicle_configuration")
                or current_vehicle.get("configuration")
                or {}
            )
            current_configuration = (
                dict(raw_current_configuration)
                if isinstance(raw_current_configuration, dict)
                else {}
            )
            desired_configuration = dict(current_configuration)
            if isinstance(observed_configuration, str):
                desired_configuration["adas_map_configuration"] = observed_configuration
            elif isinstance(observed_configuration, dict):
                desired_configuration.update(observed_configuration)
            if observed_model_configuration:
                desired_configuration["adas_map_model_configuration"] = (
                    observed_model_configuration
                )
            if current_configuration != desired_configuration:
                vehicle_changes["vehicle_configuration"] = desired_configuration

        if vehicle_changes:
            ro_version = max(1, int(current_ro.get("version") or before.get("version") or 1))
            identity = {
                "operation": "update_ro",
                "repair_order_id": repair_order_id,
                "expected_version": ro_version,
                "arguments": vehicle_changes,
                "inspection_id": inspection_id,
            }
            planned.append(
                {
                    "idempotency_key": self._idempotency(identity),
                    "correlation_id": correlation,
                    "operation": "update_ro",
                    "repair_order_id": repair_order_id,
                    "expected_version": ro_version,
                    "arguments": vehicle_changes,
                    "_kind": "vehicle",
                }
            )
        for requirement in normalized:
            matches = by_key.get(requirement["key"], [])
            required = next(
                (
                    row for row in matches
                    if str(row.get("determination") or "").upper() == "REQUIRED"
                ),
                None,
            )
            if required is not None:
                kept.append(
                    {
                        "key": requirement["key"],
                        "calibration_item_id": required.get("id"),
                        "operation": "keep",
                    }
                )
                continue

            reactivatable = next(
                (
                    row for row in matches
                    if str(row.get("determination") or "").upper()
                    in (INACTIVE_DETERMINATIONS | {"LIKELY_REQUIRED", "NEEDS_RESEARCH"})
                ),
                None,
            )
            if reactivatable is not None:
                operation = "update_calibration"
                target_id = str(reactivatable.get("id") or "")
                expected_version = max(1, int(reactivatable.get("version") or 1))
                arguments = {"determination": "REQUIRED"}
                identity = {
                    "operation": operation,
                    "repair_order_id": repair_order_id,
                    "target_id": target_id,
                    "expected_version": expected_version,
                    "arguments": arguments,
                    "inspection_id": inspection_id,
                }
                planned.append(
                    {
                        "correlation_id": correlation,
                        "operation": operation,
                        "target_id": target_id,
                        "expected_version": expected_version,
                        "arguments": arguments,
                        "_kind": "calibration",
                        "_requirement_key": requirement["key"],
                        "_identity": identity,
                    }
                )
                continue

            operation = "add_calibration"
            arguments = {
                "calibration_type": requirement["label"],
                "determination": "REQUIRED",
                "method": requirement["method"],
                "notes": (
                    f"Authoritative ADAS Map requirement"
                    f"{f' from inspection {inspection_id}' if inspection_id else ''}."
                ),
            }
            identity = {
                "operation": operation,
                "repair_order_id": repair_order_id,
                "arguments": arguments,
                "inspection_id": inspection_id,
            }
            planned.append(
                {
                    "correlation_id": correlation,
                    "operation": operation,
                    "repair_order_id": repair_order_id,
                    "arguments": arguments,
                    "_kind": "calibration",
                    "_requirement_key": requirement["key"],
                    "_identity": identity,
                }
            )

        calibration_plans = [
            action for action in planned if action.get("_kind") == "calibration"
        ]
        research_reopened: dict[str, Any] | None = None
        reopen_receipts: list[dict[str, Any]] = []
        research = before.get("research")
        research_context: dict[str, Any] = {
            "id": None,
            "state": None,
            "version": None,
        }
        if calibration_plans and research is not None:
            if not isinstance(research, dict):
                raise CIQReconciliationError(
                    "CIQ research state is not authoritative enough for calibration mutation."
                )
            research_id = str(research.get("id") or "").strip()
            research_state = str(research.get("state") or "").strip().casefold()
            try:
                research_version = int(research.get("version"))
            except (TypeError, ValueError):
                research_version = 0
            if (
                not research_id
                or research_state not in RESEARCH_STATES
                or research_version < 1
            ):
                raise CIQReconciliationError(
                    "CIQ research state is not authoritative enough for calibration mutation."
                )

            if research_state == "research_complete":
                reason = (
                    "Authoritative ADAS Map"
                    f"{f' inspection {inspection_id}' if inspection_id else ''} introduced "
                    "or reactivated required calibrations; research reopened for managed "
                    "evidence review."
                )
                arguments = {
                    "state": "research_in_progress",
                    "reason": reason,
                }
                reopen_identity = {
                    "reconciliation_contract_version": RECONCILIATION_CONTRACT_VERSION,
                    "operation": "update_research",
                    "repair_order_id": repair_order_id,
                    "expected_version": research_version,
                    "arguments": arguments,
                    "inspection_id": inspection_id,
                }
                reopen_action = {
                    "idempotency_key": self._idempotency(reopen_identity),
                    "correlation_id": correlation,
                    "operation": "update_research",
                    "repair_order_id": repair_order_id,
                    "expected_version": research_version,
                    "arguments": arguments,
                }
                reopen_response = await self._post_actions([reopen_action])
                reopen_receipts = list(reopen_response.get("receipts") or [])
                reopen_receipt = reopen_receipts[0]

                after_reopen = await self._snapshot(repair_order_id)
                reopened_research = after_reopen.get("research")
                if not isinstance(reopened_research, dict):
                    raise CIQReconciliationError(
                        "CIQ authoritative reread did not verify reopened research.",
                        result={"receipts": reopen_receipts},
                    )
                reopened_id = str(reopened_research.get("id") or "").strip()
                reopened_state = str(
                    reopened_research.get("state") or ""
                ).strip().casefold()
                try:
                    reopened_version = int(reopened_research.get("version"))
                except (TypeError, ValueError):
                    reopened_version = 0
                if (
                    reopened_id != research_id
                    or reopened_state != "research_in_progress"
                    or reopened_version <= research_version
                ):
                    raise CIQReconciliationError(
                        "CIQ authoritative reread did not verify reopened research.",
                        result={"receipts": reopen_receipts},
                    )
                research_id = reopened_id
                research_state = reopened_state
                research_version = reopened_version
                research_reopened = {
                    "operation": "update_research",
                    "research_case_id": reopen_receipt.get("resource_id"),
                    "mutation_id": reopen_receipt.get("mutation_id"),
                    "idempotency_key": reopen_receipt.get("idempotency_key"),
                    "replayed": bool(reopen_receipt.get("replayed")),
                    "from_state": "research_complete",
                    "to_state": "research_in_progress",
                    "from_version": int(research.get("version")),
                    "to_version": research_version,
                }

            research_context = {
                "id": research_id,
                "state": research_state,
                "version": research_version,
            }

        for action in calibration_plans:
            identity = {
                **action.pop("_identity"),
                "reconciliation_contract_version": RECONCILIATION_CONTRACT_VERSION,
                "research_context": research_context,
            }
            action["idempotency_key"] = self._idempotency(identity)

        wire_actions = [
            {key: value for key, value in action.items() if not key.startswith("_")}
            for action in planned
        ]
        response = await self._post_actions(wire_actions)
        receipts = response.get("receipts") or []
        changed: list[dict[str, Any]] = []
        vehicle_receipt: dict[str, Any] | None = None
        for plan, receipt in zip(planned, receipts, strict=True):
            if plan.get("_kind") == "vehicle":
                vehicle_receipt = {
                    "operation": "update_ro",
                    "mutation_id": receipt.get("mutation_id"),
                    "idempotency_key": receipt.get("idempotency_key"),
                    "replayed": bool(receipt.get("replayed")),
                    "changes": vehicle_changes,
                }
                continue
            changed.append(
                {
                    "key": plan["_requirement_key"],
                    "operation": plan["operation"],
                    "calibration_item_id": receipt.get("resource_id"),
                    "mutation_id": receipt.get("mutation_id"),
                    "idempotency_key": receipt.get("idempotency_key"),
                    "replayed": bool(receipt.get("replayed")),
                }
            )

        after = await self._snapshot(repair_order_id)
        if research_reopened is not None:
            final_research = after.get("research")
            final_research_state = (
                str(final_research.get("state") or "").strip().casefold()
                if isinstance(final_research, dict)
                else ""
            )
            if final_research_state != "research_in_progress":
                raise CIQReconciliationError(
                    "CIQ authoritative reread did not preserve reopened research state.",
                    result={"receipts": [*reopen_receipts, *receipts]},
                )
        active_by_key: dict[str, list[dict[str, Any]]] = {}
        for row in self._calibrations(after):
            if str(row.get("determination") or "").upper() != "REQUIRED":
                continue
            active_by_key.setdefault(calibration_key(row.get("calibration_type")), []).append(row)
        missing = [row["label"] for row in normalized if not active_by_key.get(row["key"])]
        if missing:
            raise CIQReconciliationError(
                "CIQ authoritative reread did not contain every ADAS Map requirement.",
                result={"missing": missing, "receipts": receipts},
            )
        if vehicle_changes:
            after_vehicle = after.get("vehicle") if isinstance(after.get("vehicle"), dict) else {}
            after_ro = after.get("repair_order") if isinstance(after.get("repair_order"), dict) else {}
            unverified_vehicle: list[str] = []
            for field, expected in vehicle_changes.items():
                source_field = "configuration" if field == "vehicle_configuration" else field
                # update_ro mutates the repair-order resource.  Some snapshots
                # also expose a denormalized vehicle object that may lag the
                # authoritative RO by one read, so verify the RO first and use
                # vehicle only when the RO field is absent.
                observed = after_ro.get(field)
                if observed in (None, "", {}):
                    observed = after_vehicle.get(source_field)
                if field == "vehicle_configuration":
                    matches = observed == expected
                else:
                    matches = str(observed or "").strip().casefold() == str(expected).strip().casefold()
                if not matches:
                    unverified_vehicle.append(field)
            if unverified_vehicle:
                raise CIQReconciliationError(
                    "CIQ authoritative reread did not verify ADAS Map vehicle identity.",
                    result={"unverified_vehicle_fields": unverified_vehicle, "receipts": receipts},
                )

        if not isinstance(adas_map_attachment, dict) or not adas_map_attachment.get("attached"):
            raise CIQReconciliationError(
                "CIQ reconciliation cannot complete without an attached ADAS Map PDF."
            )
        if (
            str(adas_map_attachment.get("semantic_type") or "").strip().casefold()
            != "adas_map_report"
        ):
            raise CIQReconciliationError(
                "CIQ reconciliation cannot complete until the document is classified as ADAS_MAP_REPORT."
            )
        final_research = after.get("research")
        final_research_state = (
            str(final_research.get("state") or "").strip().casefold()
            if isinstance(final_research, dict)
            else ""
        )
        if final_research_state not in {"research_in_progress", "research_complete"}:
            raise CIQReconciliationError(
                "CIQ reconciliation cannot complete while research remains required."
            )

        active_ids = {
            row["key"]: [str(item.get("id")) for item in active_by_key.get(row["key"], [])]
            for row in normalized
        }
        return {
            "verified": True,
            "repair_order_id": repair_order_id,
            "inspection_id": inspection_id,
            "requirements": normalized,
            "kept": kept,
            "changed": changed,
            "vehicle_changed": vehicle_receipt,
            "research_reopened": research_reopened,
            "research_started": research_started,
            "adas_map_attachment": adas_map_attachment,
            "active_calibration_item_ids": active_ids,
            "receipt_count": (
                len(adas_map_receipts) + len(reopen_receipts) + len(receipts)
            ),
            "snapshot_verified": True,
            "explicit_no_calibration": explicit_no_calibration,
        }
