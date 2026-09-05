import asyncio
import inspect

import scrapex.adas_map as adas_map_module
from scrapex.adas_map import (
    AdasMapBrowser,
    calibration_labels_from_texts,
    extract_vin,
    normalize_shop,
    row_matches_shop,
)

def test_extract_vin():
    assert extract_vin("VIN TESTCAR0000000002") == "TESTCAR0000000002"

def test_extract_vin_rejects_invalid_letters():
    assert extract_vin("VIN 1HGCM82633O004352") is None

def test_shop_aliases():
    assert normalize_shop("Macon/Mercer University Dr.") == "macon"
    assert normalize_shop("Warner Robins") == "warnerrobins"

def test_row_shop_mismatch_rejected_when_explicit():
    assert row_matches_shop("RO 123 Warner Robins", "Macon") is False

def test_calibration_label_collection():
    result = calibration_labels_from_texts([
        "Front Camera Calibration",
        "Steering Angle Sensor Initialization",
        "Front Camera Calibration",
    ])
    assert result == [
        "Front Camera Calibration",
        "Steering Angle Sensor Initialization",
    ]


def test_separate_profile_adas_map_browser_is_retired_fail_closed():
    source = inspect.getsource(adas_map_module).casefold()
    assert "launch_persistent_context" not in source
    assert "adas-map-browser-profile" not in source

    browser = AdasMapBrowser()
    result = asyncio.run(browser.open())
    assert result["success"] is False
    assert result["status"] == "managed_work_chrome_required"
