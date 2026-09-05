from pathlib import Path

from scrapex.storage_policy import (
    adas_map_pdf_path,
    migrate_legacy_adas_map_reports,
    service_information_directory,
)


def test_service_information_rule_is_year_make_model(tmp_path: Path):
    root = tmp_path / "ADAS SI"
    path = service_information_directory(
        root,
        {"year": 2024, "make": "Toyota", "model": "Camry"},
    )
    assert path == root.resolve() / "2024" / "Toyota" / "Camry"


def test_adas_map_rule_is_repair_order(tmp_path: Path):
    root = tmp_path / "ADAS SI"
    assert adas_map_pdf_path(root, "2400911731") == (
        root.resolve()
        / "ADAS Map"
        / "2400911731"
        / "2400911731 ADAS Map.pdf"
    )


def test_legacy_root_report_moves_into_ro_folder(tmp_path: Path):
    root = tmp_path / "ADAS SI"
    root.mkdir()
    legacy = root / "2400911731 adas map.pdf"
    legacy.write_bytes(b"%PDF legacy")
    old = str(legacy.resolve())
    moved = migrate_legacy_adas_map_reports(root)
    target = root / "ADAS Map" / "2400911731" / "2400911731 ADAS Map.pdf"
    assert target.is_file()
    assert not legacy.exists()
    assert moved[old] == str(target.resolve())
