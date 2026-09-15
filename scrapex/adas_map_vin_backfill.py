"""Recover VINs from already-proven legacy ADAS Map work.

Current ADAS Map runs persist the row-bound VIN into Calibration IQ before the
report/calibration reconciliation can finish.  Older contract-v3 rows can predate
that identity write, though, so they may already have a verified ADAS Map report
and required calibrations while the CIQ repair order still has no VIN.

This adapter broadens *only* the explicit repair path (``allow_legacy=True``).
Normal acquisition remains on the current proof contract.  A legacy completed
row is eligible only when the stored ScrapeX item proves a completed CIQ ADAS Map
reconciliation and the raw result independently proves the same RO, CIQ id,
shop, inspection, selected row, requirement modal and 17-character VIN.
"""

from __future__ import annotations

import re
from typing import Any

from .adas_map_worker import AdasMapBatchRunner, _shop_key
from .db import ADAS_MAP_CONTRACT_VERSION


_INSTALLED_ATTR = "__scrapex_legacy_completed_vin_backfill_v1__"
_VIN_RE = re.compile(r"^[A-HJ-NPR-Z0-9]{17}$")


def _legacy_completed_proof(
    item: dict[str, Any], result: dict[str, Any]
) -> dict[str, Any] | None:
    expected_ro = str(item.get("ro_number") or "").strip()
    expected_ciq_id = str(item.get("ro_id") or "").strip()
    expected_shop = " ".join(str(item.get("shop") or "").split())
    if not expected_ro or not expected_ciq_id or not expected_shop:
        return None

    # This is a repair of an already-completed authoritative ADAS Map result,
    # not a new way to infer identity from arbitrary old browser output.
    if not (
        int(item.get("adas_map_contract_version") or 0)
        == ADAS_MAP_CONTRACT_VERSION
        and str(item.get("adas_map_state") or "") == "adas_map_complete"
        and bool(item.get("adas_map_requirements_proven"))
        and str(item.get("ciq_reconciliation_state") or "") == "complete"
        and item.get("ciq_adas_map_verified") in (1, True)
        and result.get("success") is True
        and str(result.get("status") or "") == "complete"
        and result.get("row_binding_confirmed") is True
        and result.get("modal_inspection_confirmed") is True
        and result.get("required_region_confirmed") is True
        and result.get("requirements_proven") is True
        and not result.get("bridge_status")
    ):
        return None

    returned_ro = str(result.get("ro_number") or "").strip()
    returned_ciq_id = str(result.get("ciq_ro_id") or "").strip()
    returned_shop = " ".join(str(result.get("shop") or "").split())
    inspection_id = str(result.get("inspection_id") or "").strip()
    vin = str(result.get("vin") or "").strip().upper()
    if (
        returned_ro != expected_ro
        or returned_ciq_id != expected_ciq_id
        or _shop_key(returned_shop) != _shop_key(expected_shop)
        or not inspection_id
        or not _VIN_RE.fullmatch(vin)
    ):
        return None

    return {
        "proven": True,
        "source": "legacy_work_chrome_complete_v3",
        "ro_number": expected_ro,
        "ciq_ro_id": expected_ciq_id,
        "shop": returned_shop,
        "inspection_id": inspection_id,
        "vin": vin,
        "row_binding_confirmed": True,
    }


def install() -> None:
    if getattr(AdasMapBatchRunner, _INSTALLED_ATTR, False):
        return

    original = AdasMapBatchRunner._proven_identity

    def proven_identity(
        item: dict[str, Any],
        result: dict[str, Any],
        *,
        allow_legacy: bool = False,
    ) -> dict[str, Any] | None:
        proof = original(item, result, allow_legacy=allow_legacy)
        if proof is not None or not allow_legacy:
            return proof
        return _legacy_completed_proof(item, result)

    AdasMapBatchRunner._proven_identity = staticmethod(proven_identity)
    setattr(AdasMapBatchRunner, _INSTALLED_ATTR, True)


install()
