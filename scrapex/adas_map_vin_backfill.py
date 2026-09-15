"""Recover VINs from already-proven legacy ADAS Map work.

Current ADAS Map runs persist the row-bound VIN into Calibration IQ before the
report/calibration reconciliation can finish. Older records can predate one or
more of the newer bookkeeping fields, though, while still retaining the actual
mechanical evidence that bound an ADAS Map inspection to an exact CIQ RO.

This adapter broadens *only* the explicit repair path (``allow_legacy=True``).
Normal acquisition remains on the current proof contract. Legacy recovery never
accepts a VIN merely because it looks valid: the stored browser result must bind
the same RO, CIQ id, shop and inspection, prove the selected row, and retain
strong inspection/requirements evidence (or the newer explicit identity proof).
The subsequent CIQ identity reconciliation independently rereads the exact RO,
shop and current VIN before any write, so conflicts still fail closed.
"""

from __future__ import annotations

import re
from typing import Any

from .adas_map_worker import AdasMapBatchRunner, _shop_key


_INSTALLED_ATTR = "__scrapex_legacy_completed_vin_backfill_v2__"
_VIN_RE = re.compile(r"^[A-HJ-NPR-Z0-9]{17}$")


def _legacy_completed_proof(
    item: dict[str, Any], result: dict[str, Any]
) -> dict[str, Any] | None:
    expected_ro = str(item.get("ro_number") or "").strip()
    expected_ciq_id = str(item.get("ro_id") or "").strip()
    expected_shop = " ".join(str(item.get("shop") or "").split())
    if not expected_ro or not expected_ciq_id or not expected_shop:
        return None
    if result.get("bridge_status"):
        return None

    evidence = result.get("identity_evidence")
    evidence = evidence if isinstance(evidence, dict) else {}

    returned_ro = str(result.get("ro_number") or evidence.get("ro_number") or "").strip()
    returned_ciq_id = str(
        result.get("ciq_ro_id") or evidence.get("ciq_ro_id") or ""
    ).strip()
    returned_shop = " ".join(
        str(result.get("shop") or evidence.get("shop") or "").split()
    )
    inspection_id = str(
        result.get("inspection_id") or evidence.get("inspection_id") or ""
    ).strip()
    vin = str(result.get("vin") or evidence.get("vin") or "").strip().upper()
    row_bound = bool(
        result.get("row_binding_confirmed") is True
        or evidence.get("row_binding_confirmed") is True
    )
    if (
        returned_ro != expected_ro
        or returned_ciq_id != expected_ciq_id
        or _shop_key(returned_shop) != _shop_key(expected_shop)
        or not inspection_id
        or not row_bound
        or not _VIN_RE.fullmatch(vin)
    ):
        return None

    # Newer rows carry a dedicated identity receipt. Older rows often do not,
    # but the selected inspection modal plus its Required region is the same
    # mechanical provenance from which the ADAS Map calibration list was read.
    explicit_identity = bool(
        result.get("vehicle_identity_proven") is True
        and evidence.get("proven") is True
        and evidence.get("source") == "adas_map_bound_ro_row"
    )
    modal_requirements_proof = bool(
        result.get("modal_inspection_confirmed") is True
        and result.get("required_region_confirmed") is True
        and result.get("requirements_proven") is True
    )
    if not (explicit_identity or modal_requirements_proof):
        return None

    # Require evidence that this was real ADAS Map work that progressed beyond
    # an initial lookup. Do not require today's contract version or today's
    # ciq_adas_map_verified flag: those are exactly the metadata older records
    # can lack. At least one durable completion/reconciliation marker must exist.
    completion_proven = bool(
        str(item.get("adas_map_state") or "") == "adas_map_complete"
        or str(item.get("ciq_reconciliation_state") or "") == "complete"
        or (
            result.get("success") is True
            and str(result.get("status") or "").casefold() == "complete"
        )
        or result.get("report_capture_verified") is True
    )
    if not completion_proven:
        return None

    return {
        "proven": True,
        "source": "legacy_work_chrome_bound_evidence",
        "ro_number": expected_ro,
        "ciq_ro_id": expected_ciq_id,
        "shop": returned_shop,
        "inspection_id": inspection_id,
        "vin": vin,
        "row_binding_confirmed": True,
    }


def install() -> None:
    # A process can import an older patch before this module revision is loaded
    # during tests/reload. The v2 marker is intentionally distinct so this
    # stronger compatibility wrapper can still install once around it.
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
