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
    assert proof["source"] == "legacy_work_chrome_complete_v3"
    assert proof["vin"] == "TESTCAR0000000001"
    assert proof["ciq_ro_id"] == "ro-1"


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


def test_completed_legacy_map_requires_verified_ciq_adas_map_attachment():
    item = _item()
    item["ciq_adas_map_verified"] = 0
    assert (
        AdasMapBatchRunner._proven_identity(item, _legacy_success(), allow_legacy=True)
        is None
    )
