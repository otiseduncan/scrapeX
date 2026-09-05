from pathlib import Path

from scrapex.models import VehicleSpec
from scrapex.si_coverage import check_coverage


def _write(tmp_path: Path, name: str) -> None:
    (tmp_path / name).write_bytes(b"%PDF-1.4\n")


def test_matches_exact_vehicle_and_calibration(tmp_path):
    _write(tmp_path, "2017 Toyota Camry BSM SI.pdf")
    vehicle = VehicleSpec(year=2017, make="Toyota", model="Camry SE")
    result = check_coverage(vehicle, ["Blind spot monitor calibration"], tmp_path)
    assert result["si_ready"] is True
    assert result["missing"] == []


def test_does_not_match_different_year_outside_any_range(tmp_path):
    _write(tmp_path, "2017 Toyota Camry BSM SI.pdf")
    vehicle = VehicleSpec(year=2014, make="Toyota", model="Camry L")
    result = check_coverage(vehicle, ["Blind spot monitor calibration"], tmp_path)
    assert result["si_ready"] is False
    assert result["missing"] == ["Blind spot monitor calibration"]


def test_year_range_filenames_contain_target_year(tmp_path):
    _write(tmp_path, "2023-2026 Toyota Highlander BSM.pdf")
    vehicle = VehicleSpec(year=2024, make="Toyota", model="Highlander XLE")
    result = check_coverage(vehicle, ["Blind-spot / corner radar calibration"], tmp_path)
    assert result["si_ready"] is True


def test_short_nameplate_does_not_false_positive_on_unrelated_word(tmp_path):
    _write(tmp_path, "2022 Lexus ES 350 FWD parking assist monitor.pdf")
    vehicle = VehicleSpec(year=2022, make="Lexus", model="IS 350 F Sport RWD")
    result = check_coverage(
        vehicle,
        ["Parking Assist Distance Sensors calibration"],
        tmp_path,
    )
    assert result["si_ready"] is False
    assert result["missing"] == ["Parking Assist Distance Sensors calibration"]


def test_different_make_never_matches(tmp_path):
    _write(tmp_path, "2017 Toyota Camry BSM SI.pdf")
    vehicle = VehicleSpec(year=2017, make="Honda", model="Camry SE")
    result = check_coverage(vehicle, ["Blind spot monitor calibration"], tmp_path)
    assert result["si_ready"] is False


def test_empty_requirements_is_not_si_ready(tmp_path):
    vehicle = VehicleSpec(year=2017, make="Toyota", model="Camry SE")
    result = check_coverage(vehicle, [], tmp_path)
    assert result["si_ready"] is False
    assert result["total_requirements"] == 0
