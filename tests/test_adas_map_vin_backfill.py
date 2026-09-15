from scrapex.adas_map_worker import AdasMapBatchRunner
from scrapex.db import ADAS_MAP_CONTRACT_VERSION


def _item():
    return {
        "ro_id": "ro-1",
        "ro_number": "2400911761",
        "shop": "Gerber Collision & Glass - Macon",
        "adas_map_contract_version": ADAS_MAP_CONTRACT_VERSION,
        "adas_map_state": "adas_map_complete",
        "adas_map_requirements_proven": 1,
        "ciq_reconciliation_state": "complete",
        "ciq_adas_map_verified": 1,
    }


def _legacy_success():
    return {
        "success": True,
        "status": "complete",
        "ro_number": "2400911761",
        "ciq_ro_id": "ro-1",
        "shop": "Gerber Collision & Glass - Macon/Mercer University",
        "inspection_id": "inspection-77",
        "vin": "TESTCAR0000000001",
        "row_binding_confirmed": True,
        "modal_inspection_confirmed": True,
        "required_region_confirmed": True,
        "requirements_proven": True,
    }


def test_completed_legacy_map_can_repair_its_already_proven_vin():
    proof = AdasMapBatchRunner._proven_identity(
        _item(), _legacy_success(), allow_legacy=True
    )
    assert proof is not None
    assert proof["source"] == "legacy_work_chrome_bound_evidence"
    assert proof["vin"] == "TESTCAR0000000001"
    assert proof["ciq_ro_id"] == "ro-1"


def test_older_bound_map_does_not_require_newer_bookkeeping_fields():
    item = _item()
    item.pop("adas_map_contract_version")
    item.pop("adas_map_requirements_proven")
    item.pop("ciq_adas_map_verified")
    # The older row still has the authoritative completed reconciliation plus
    # exact browser evidence tying VIN/RO/shop/inspection/Required modal.
    proof = AdasMapBatchRunner._proven_identity(
        item, _legacy_success(), allow_legacy=True
    )
    assert proof is not None
    assert proof["vin"] == "TESTCAR0000000001"


def test_completed_legacy_map_is_not_a_normal_acquisition_fallback():
    assert (
        AdasMapBatchRunner._proven_identity(
            _item(), _legacy_success(), allow_legacy=False
        )
        is None
    )


def test_completed_legacy_map_requires_exact_ciq_ro_binding():
    raw = _legacy_success()
    raw["ciq_ro_id"] = "another-ro"
    assert AdasMapBatchRunner._proven_identity(_item(), raw, allow_legacy=True) is None


def test_completed_legacy_map_requires_exact_shop_binding():
    raw = _legacy_success()
    raw["shop"] = "Gerber Collision & Glass - Perry"
    assert AdasMapBatchRunner._proven_identity(_item(), raw, allow_legacy=True) is None


def test_completed_legacy_map_requires_bound_inspection_requirement_proof():
    raw = _legacy_success()
    raw["modal_inspection_confirmed"] = False
    raw["vehicle_identity_proven"] = False
    assert AdasMapBatchRunner._proven_identity(_item(), raw, allow_legacy=True) is None


def test_completed_legacy_map_requires_completion_marker():
    item = _item()
    item["adas_map_state"] = "searching_adas_map"
    item["ciq_reconciliation_state"] = "pending"
    raw = _legacy_success()
    raw["success"] = False
    raw["status"] = "searching"
    assert AdasMapBatchRunner._proven_identity(item, raw, allow_legacy=True) is None
