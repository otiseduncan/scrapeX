from pathlib import Path
import inspect

import pytest

from scrapex.models import VehicleSpec
from scrapex.work_chrome import WorkChromeAdasMapSource, WorkChromeBridge


def observed_vehicle(**overrides):
    result = {
        "year": 2016,
        "make": "Toyota",
        "model_configuration": "Sienna L FWD w/7-Passenger Seating",
        "observed_label": "2016 Toyota Sienna L FWD w/7-Passenger Seating",
    }
    result.update(overrides)
    return result


def expected_vehicle(**overrides):
    values = {
        "ro_id": "ciq-ro-id",
        "ro_number": "9000000001",
        "year": 2016,
        "make": "TOYOTA",
        # This is the literal truncated CIQ Stage 1 value. It is validation
        # context, not permission to invent a model/configuration split.
        "model": "Sienna L FWD...",
        # Deliberately different: successful output must come from ADAS Map.
        "trim": "CIQ trim is not authoritative",
    }
    values.update(overrides)
    return VehicleSpec(**values)

def test_bridge_points_at_repo_script(tmp_path: Path):
    bridge = WorkChromeBridge(tmp_path)
    assert bridge.script == tmp_path / "scripts" / "work-chrome-adas-map.ps1"

def test_bridge_does_not_require_chrome_profile_path(tmp_path: Path):
    bridge = WorkChromeBridge(tmp_path)
    values = vars(bridge)
    assert "profile" not in " ".join(values.keys()).casefold()
    assert "cookie" not in " ".join(values.keys()).casefold()
    source = inspect.getsource(WorkChromeBridge).casefold()
    assert "user_data_dir" not in source
    assert "profile database" not in source
    assert "password store" in source  # prohibition in the class contract only


def test_bridge_supports_current_read_method(tmp_path: Path):
    bridge = WorkChromeBridge(tmp_path)
    assert callable(bridge.read_current)


def test_bridge_supports_details_method(tmp_path: Path):
    bridge = WorkChromeBridge(tmp_path)
    assert callable(bridge.details_test)
    assert callable(bridge.close_details)


PORTAL_SHOP = "Gerber Collision & Glass - Macon/Mercer University"


class FakeBridge:
    def __init__(
        self,
        details: dict,
        lookup: dict | None = None,
        close_result: dict | None = None,
        download_result: dict | None = None,
    ):
        self.details = details
        self.lookup = lookup if lookup is not None else {
            "success": True,
            "status": "ro_visible",
            "vin": "TESTCAR0000000001",
            "inspection_id": "9900001",
            "vehicle": observed_vehicle(),
            "row_binding_confirmed": True,
            "observed_shop": PORTAL_SHOP,
            "row_expansion": {
                "status": "row_expanded",
                "activation_method": "invoke_pattern",
                "vin": "TESTCAR0000000001",
                "inspection_id": "9900001",
            },
        }
        self.close_result = close_result or {
            "success": True,
            "status": "details_closed",
        }
        self.download_result = download_result or {
            "success": True,
            "status": "saved",
            "modal_close": {"closed": True},
        }
        self.detail_calls: list[tuple[str, str | None]] = []
        self.lookup_expected: list[object] = []
        self.details_expected: list[object] = []
        self.close_calls = 0
        self.download_calls: list[tuple[str, str, str | None]] = []

    async def status(self):
        return {
            "success": True,
            "target_found": True,
            "target": {"title": "ADAS - adas - Google Chrome"},
        }

    async def lookup_test(self, ro_number: str, expected=None):
        self.lookup_expected.append(expected)
        return self.lookup

    async def details_test(
        self,
        ro_number: str,
        inspection_id: str | None = None,
        expected=None,
    ):
        self.detail_calls.append((ro_number, inspection_id))
        self.details_expected.append(expected)
        return self.details

    async def close_details(self):
        self.close_calls += 1
        return self.close_result

    async def download_report(
        self,
        ro_number: str,
        save_path: str,
        inspection_id: str | None = None,
    ):
        self.download_calls.append((ro_number, save_path, inspection_id))
        return self.download_result


def proven_details(**overrides):
    result = {
        "success": True,
        "status": "details_visible",
        "ro_number": "9000000001",
        "vin": "TESTCAR0000000001",
        "vehicle": observed_vehicle(),
        "inspection_id": "9900001",
        "document_changed": True,
        "detail_confirmed": True,
        "row_binding_confirmed": True,
        "modal_inspection_confirmed": True,
        "modal_runtime_id": "modal-runtime",
        "required_context_confirmed": True,
        "required_region_confirmed": True,
        "requirements_parse_confident": True,
        "explicit_no_calibration": False,
        "requirements": [
            {
                "label": "Blind Spot Monitor Calibration",
                "source": "adas_map_required_list_item",
                "source_control_class": "custom-link",
                "source_context": "selected_required_modal",
                "source_context_runtime_id": "modal-runtime",
            }
        ],
        "source_url": "https://opus.adasmap.com/inspection/9900001",
        "report_links": ["https://opus.adasmap.com/report/9900001.pdf"],
        "alldata_links": [],
    }
    result.update(overrides)
    return result


@pytest.mark.asyncio
async def test_source_returns_normalized_proven_requirement_contract():
    bridge = FakeBridge(proven_details())
    source = WorkChromeAdasMapSource(bridge)
    expected = expected_vehicle()

    result = await source.lookup("9000000001", shop="Macon", expected=expected)

    assert result["success"] is True
    assert result["status"] == "complete"
    assert result["requirements_proven"] is True
    assert result["ciq_ro_id"] == "ciq-ro-id"
    assert result["shop"] == PORTAL_SHOP
    assert result["observed_shop"] == PORTAL_SHOP
    assert result["ciq_requested_shop"] == "Macon"
    assert result["inspection_id"] == "9900001"
    assert result["calibrations"] == ["Blind Spot Monitor Calibration"]
    assert result["requirements"] == ["Blind Spot Monitor Calibration"]
    assert result["requirement_records"][0]["source_control_class"] == "custom-link"
    assert result["vehicle"] == {
        "year": 2016,
        "make": "Toyota",
        "model": "Sienna L FWD w/7-Passenger Seating",
        "configuration": None,
        "model_configuration": "Sienna L FWD w/7-Passenger Seating",
    }
    assert result["vehicle_label"] == "2016 Toyota Sienna L FWD w/7-Passenger Seating"
    assert result["adas_map"]["vehicle"] == result["vehicle"]
    assert result["adas_map"]["inspection_id"] == "9900001"
    assert result["adas_map"]["requirements"] == ["Blind Spot Monitor Calibration"]
    assert bridge.detail_calls == [("9000000001", "9900001")]
    assert bridge.lookup_expected == [expected]
    assert bridge.details_expected == [expected]
    assert bridge.close_calls == 1
    assert result["detail_close"]["status"] == "details_closed"
    assert result["lookup_row_expansion"]["status"] == "row_expanded"
    assert result["details_row_expansion"] is None


@pytest.mark.asyncio
async def test_lookup_failure_preserves_shop_switch_and_search_diagnostics():
    bridge = FakeBridge(
        proven_details(),
        lookup={
            "success": False,
            "status": "no_ro_match_visible",
            "observed_shop": "Gerber Collision & Glass - Warner Robins",
            "shop_switch": {
                "changed": True,
                "selected": "Gerber Collision & Glass - Warner Robins",
                "selection_confirmed": True,
            },
            "search_action": {"invoked": True},
            "search_attempts": [
                {"attempt": 1, "result_status": "ro_not_visible"},
                {"attempt": 2, "result_status": "ro_not_visible"},
            ],
            "row_expansion": {
                "status": "post_expand_view_unproven",
                "attempts": [{"method": "invoke_pattern", "attempted": True}],
            },
        },
    )
    source = WorkChromeAdasMapSource(bridge)
    expected = expected_vehicle(
        ro_number="2400711836",
        year=2020,
        make="NISSAN",
        model="Murano SL FWD",
    )

    result = await source.lookup(
        "2400711836",
        shop="Warner Robins",
        expected=expected,
    )

    assert result["success"] is False
    assert result["status"] == "ro_not_found"
    assert result["bridge_status"] == "no_ro_match_visible"
    assert result["shop"] == "Gerber Collision & Glass - Warner Robins"
    assert result["observed_shop"] == result["shop"]
    assert result["ciq_requested_shop"] == "Warner Robins"
    assert result["shop_switch"]["selection_confirmed"] is True
    assert len(result["search_attempts"]) == 2
    assert result["row_expansion"]["status"] == "post_expand_view_unproven"
    assert bridge.lookup_expected == [expected]
    assert bridge.detail_calls == []


@pytest.mark.asyncio
async def test_complete_nontruncated_ciq_model_can_prove_split_boundary():
    bridge = FakeBridge(proven_details())
    source = WorkChromeAdasMapSource(bridge)

    result = await source.lookup(
        "9000000001",
        shop="Macon",
        expected=expected_vehicle(model="Sienna"),
    )

    assert result["success"] is True
    assert result["vehicle"]["model"] == "Sienna"
    assert result["vehicle"]["configuration"] == "L FWD w/7-Passenger Seating"
    assert result["vehicle"]["model_configuration"] == (
        "Sienna L FWD w/7-Passenger Seating"
    )


@pytest.mark.asyncio
async def test_source_fails_closed_when_required_rows_are_unparsed():
    bridge = FakeBridge(
        proven_details(requirements_parse_confident=False, requirements=[])
    )
    source = WorkChromeAdasMapSource(bridge)

    result = await source.lookup("9000000001", shop="Macon", expected=expected_vehicle())

    assert result["success"] is False
    assert result["status"] == "requirements_unparsed"
    assert bridge.close_calls == 1


@pytest.mark.asyncio
async def test_source_accepts_only_positive_explicit_no_calibration_evidence():
    bridge = FakeBridge(
        proven_details(requirements=[], explicit_no_calibration=True)
    )
    source = WorkChromeAdasMapSource(bridge)

    result = await source.lookup("9000000001", shop="Macon", expected=expected_vehicle())

    assert result["success"] is True
    assert result["requirements_proven"] is True
    assert result["explicit_no_calibration"] is True
    assert result["calibrations"] == []
    assert bridge.close_calls == 1


@pytest.mark.asyncio
async def test_source_rejects_unproven_navigation_even_if_model_like_text_exists():
    bridge = FakeBridge(
        proven_details(
            document_changed=False,
            values=["radar cal", "BSM Cal"],
        )
    )
    source = WorkChromeAdasMapSource(bridge)

    result = await source.lookup("9000000001", shop="Macon", expected=expected_vehicle())

    assert result["success"] is False
    assert result["status"] == "view_did_not_navigate"
    assert bridge.close_calls == 1


@pytest.mark.asyncio
async def test_source_accepts_exact_detail_that_was_already_visible():
    bridge = FakeBridge(
        proven_details(document_changed=False, detail_already_visible=True)
    )
    source = WorkChromeAdasMapSource(bridge)

    result = await source.lookup("9000000001", shop="Macon", expected=expected_vehicle())

    assert result["success"] is True
    assert result["requirements_proven"] is True
    assert bridge.close_calls == 1


@pytest.mark.asyncio
async def test_source_rejects_rows_outside_proven_required_context():
    bridge = FakeBridge(proven_details(required_context_confirmed=False))
    source = WorkChromeAdasMapSource(bridge)

    result = await source.lookup("9000000001", shop="Macon", expected=expected_vehicle())

    assert result["success"] is False
    assert result["status"] == "requirements_unparsed"
    assert bridge.close_calls == 1


@pytest.mark.asyncio
async def test_bookmark_like_names_cannot_enter_authoritative_requirements():
    bridge = FakeBridge(
        proven_details(values=["radar cal", "BSM Cal", "Front Camera Calibration"])
    )
    source = WorkChromeAdasMapSource(bridge)

    result = await source.lookup("9000000001", shop="Macon", expected=expected_vehicle())

    assert result["success"] is True
    assert result["calibrations"] == ["Blind Spot Monitor Calibration"]
    assert "radar cal" not in result["calibrations"]
    assert "BSM Cal" not in result["calibrations"]
    assert bridge.close_calls == 1


@pytest.mark.asyncio
async def test_source_rejects_inspection_mismatch():
    bridge = FakeBridge(proven_details(inspection_id="9999999"))
    source = WorkChromeAdasMapSource(bridge)

    result = await source.lookup("9000000001", shop="Macon", expected=expected_vehicle())

    assert result["success"] is False
    assert result["status"] == "inspection_mismatch"
    assert bridge.close_calls == 1


@pytest.mark.asyncio
async def test_source_fails_closed_when_observed_vehicle_is_missing():
    bridge = FakeBridge(proven_details(vehicle=None))
    source = WorkChromeAdasMapSource(bridge)

    result = await source.lookup(
        "9000000001",
        shop="Macon",
        expected=expected_vehicle(),
    )

    assert result["success"] is False
    assert result["status"] == "vehicle_identity_unparsed"
    assert bridge.close_calls == 1


@pytest.mark.asyncio
async def test_source_keeps_authoritative_combined_model_without_ciq_split_hint():
    bridge = FakeBridge(proven_details())
    source = WorkChromeAdasMapSource(bridge)

    result = await source.lookup("9000000001", shop="Macon")

    assert result["success"] is True
    assert result["vehicle"]["model"] == "Sienna L FWD w/7-Passenger Seating"
    assert result["vehicle"]["configuration"] is None
    assert result["vehicle"]["model_configuration"] == (
        "Sienna L FWD w/7-Passenger Seating"
    )
    assert bridge.detail_calls == [("9000000001", "9900001")]
    assert bridge.close_calls == 1


@pytest.mark.asyncio
async def test_source_uses_observed_year_and_make_instead_of_ciq_values():
    bridge = FakeBridge(proven_details())
    source = WorkChromeAdasMapSource(bridge)

    result = await source.lookup(
        "9000000001",
        shop="Macon",
        expected=expected_vehicle(year=2017, make="HONDA"),
    )

    assert result["success"] is True
    assert result["vehicle"]["year"] == 2016
    assert result["vehicle"]["make"] == "Toyota"
    assert bridge.detail_calls == [("9000000001", "9900001")]
    assert bridge.close_calls == 1


@pytest.mark.asyncio
async def test_source_rejects_vehicle_change_between_lookup_and_details():
    bridge = FakeBridge(
        proven_details(
            vehicle=observed_vehicle(
                model_configuration="Sienna LE FWD 8-Passenger"
            )
        )
    )
    source = WorkChromeAdasMapSource(bridge)

    result = await source.lookup(
        "9000000001",
        shop="Macon",
        expected=expected_vehicle(),
    )

    assert result["success"] is False
    assert result["status"] == "vehicle_identity_mismatch"
    assert bridge.close_calls == 1


@pytest.mark.asyncio
async def test_source_fails_before_navigation_without_exact_row_binding():
    bridge = FakeBridge(
        proven_details(),
        lookup={
            "success": True,
            "status": "ro_visible",
            "vin": "TESTCAR0000000001",
            "inspection_id": "9900001",
            "vehicle": observed_vehicle(),
            "observed_shop": PORTAL_SHOP,
        },
    )
    source = WorkChromeAdasMapSource(bridge)

    result = await source.lookup("9000000001", shop="Macon", expected=expected_vehicle())

    assert result["success"] is False
    assert result["status"] == "row_binding_unproven"
    assert bridge.detail_calls == []
    assert bridge.close_calls == 0


@pytest.mark.asyncio
async def test_source_fails_closed_when_detail_loses_exact_row_binding():
    bridge = FakeBridge(proven_details(row_binding_confirmed=False))
    source = WorkChromeAdasMapSource(bridge)

    result = await source.lookup("9000000001", shop="Macon", expected=expected_vehicle())

    assert result["success"] is False
    assert result["status"] == "row_binding_unproven"
    assert bridge.close_calls == 1


@pytest.mark.asyncio
async def test_source_rejects_background_inspection_id_without_modal_proof():
    bridge = FakeBridge(proven_details(modal_inspection_confirmed=False))
    source = WorkChromeAdasMapSource(bridge)

    result = await source.lookup("9000000001", shop="Macon", expected=expected_vehicle())

    assert result["success"] is False
    assert result["status"] == "view_did_not_navigate"
    assert bridge.close_calls == 1


@pytest.mark.asyncio
async def test_source_rejects_requirements_without_proven_modal_region():
    bridge = FakeBridge(proven_details(required_region_confirmed=False))
    source = WorkChromeAdasMapSource(bridge)

    result = await source.lookup("9000000001", shop="Macon", expected=expected_vehicle())

    assert result["success"] is False
    assert result["status"] == "requirements_unparsed"
    assert bridge.close_calls == 1


@pytest.mark.asyncio
async def test_source_requires_proven_detail_source_url():
    bridge = FakeBridge(proven_details(source_url=None))
    source = WorkChromeAdasMapSource(bridge)

    result = await source.lookup("9000000001", shop="Macon", expected=expected_vehicle())

    assert result["success"] is False
    assert result["status"] == "requirements_unparsed"
    assert "source URL" in result["reason"]
    assert bridge.close_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "record_override",
    [
        {"source_context": "whole_document"},
        {"source_context_runtime_id": "different-modal"},
        {"source_control_class": "page-action"},
    ],
)
async def test_source_rejects_requirement_records_without_live_modal_provenance(
    record_override,
):
    record = dict(proven_details()["requirements"][0])
    record.update(record_override)
    bridge = FakeBridge(proven_details(requirements=[record]))
    source = WorkChromeAdasMapSource(bridge)

    result = await source.lookup("9000000001", shop="Macon", expected=expected_vehicle())

    assert result["success"] is False
    assert result["status"] == "requirements_unparsed"
    assert bridge.close_calls == 1


@pytest.mark.asyncio
async def test_source_fails_closed_when_selected_portal_shop_is_missing():
    lookup = {
        "success": True,
        "status": "ro_visible",
        "vin": "TESTCAR0000000001",
        "inspection_id": "9900001",
        "vehicle": observed_vehicle(),
        "row_binding_confirmed": True,
    }
    bridge = FakeBridge(proven_details(), lookup=lookup)
    source = WorkChromeAdasMapSource(bridge)

    result = await source.lookup("9000000001", shop="Macon", expected=expected_vehicle())

    assert result["success"] is False
    assert result["status"] == "shop_identity_unparsed"
    assert bridge.detail_calls == []


@pytest.mark.asyncio
async def test_source_fails_closed_when_selected_portal_shop_does_not_match_request():
    lookup = {
        "success": True,
        "status": "ro_visible",
        "vin": "TESTCAR0000000001",
        "inspection_id": "9900001",
        "vehicle": observed_vehicle(),
        "row_binding_confirmed": True,
        "observed_shop": "Gerber Collision & Glass - Atlanta",
    }
    bridge = FakeBridge(proven_details(), lookup=lookup)
    source = WorkChromeAdasMapSource(bridge)

    result = await source.lookup("9000000001", shop="Macon", expected=expected_vehicle())

    assert result["success"] is False
    assert result["status"] == "shop_identity_mismatch"
    assert result["observed_shop"] == "Gerber Collision & Glass - Atlanta"
    assert result["ciq_requested_shop"] == "Macon"
    assert bridge.detail_calls == []


@pytest.mark.asyncio
async def test_shop_proof_requires_whole_tokens_not_substring_overlap():
    bridge = FakeBridge(proven_details())
    source = WorkChromeAdasMapSource(bridge)

    result = await source.lookup("9000000001", shop="Mac", expected=expected_vehicle())

    assert result["success"] is False
    assert result["status"] == "shop_identity_mismatch"
    assert bridge.detail_calls == []


@pytest.mark.asyncio
async def test_report_download_can_close_modal_before_final_cleanup(tmp_path):
    bridge = FakeBridge(
        proven_details(),
        close_result={"success": False, "status": "details_not_open"},
        download_result={
            "success": True,
            "status": "saved",
            "modal_close": {"closed": True},
        },
    )
    source = WorkChromeAdasMapSource(bridge, adas_si_root=tmp_path)

    result = await source.lookup(
        "9000000001",
        shop="Macon",
        expected=expected_vehicle(),
    )

    assert result["success"] is True
    assert result["status"] == "complete"
    assert result["download_modal_close_verified"] is True
    assert result["detail_close"]["status"] == "details_not_open"
    assert bridge.close_calls == 1
    assert len(bridge.download_calls) == 1
    ro_number, save_path, inspection_id = bridge.download_calls[0]
    assert ro_number == "9000000001"
    assert inspection_id == "9900001"
    assert save_path.endswith(
        "ADAS Map\\9000000001\\9000000001 ADAS Map.pdf"
    ) or save_path.endswith(
        "ADAS Map/9000000001/9000000001 ADAS Map.pdf"
    )


@pytest.mark.asyncio
async def test_source_requires_modal_close_before_returning_success():
    bridge = FakeBridge(
        proven_details(),
        close_result={"success": False, "status": "details_not_open"},
    )
    source = WorkChromeAdasMapSource(bridge)

    result = await source.lookup("9000000001", shop="Macon", expected=expected_vehicle())

    assert result["success"] is False
    assert result["status"] == "details_close_failed"
    assert result["detail_close"]["status"] == "details_not_open"
    assert bridge.close_calls == 1
