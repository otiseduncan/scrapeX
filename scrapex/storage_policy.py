"""Canonical storage rules shared by ScrapeX acquisition paths.

Hard rules:
- service information is filed Year / Make / Model
- ADAS Map evidence is filed ADAS Map / RO

The helpers here do not infer research intent. They only validate already-
structured vehicle/RO identities and convert them into bounded filesystem
paths. Legacy ADAS Map reports are migrated in-place on service startup.
"""

from __future__ import annotations

import hashlib
import re
import shutil
from pathlib import Path
from typing import Any

ADAS_MAP_DIRNAME = "ADAS Map"
_RO_RE = re.compile(r"^\d{6,20}$")
_ADAS_MAP_FILE_RE = re.compile(
    r"^(?P<ro>\d{6,20})\s+adas\s+map(?:\s+.*)?\.pdf$",
    re.IGNORECASE,
)
_SAFE_COMPONENT_RE = re.compile(r"[^A-Za-z0-9._()&+\- ]+")


def safe_component(value: object, fallback: str, *, maximum: int = 96) -> str:
    cleaned = _SAFE_COMPONENT_RE.sub(" ", str(value or ""))
    cleaned = " ".join(cleaned.split()).strip(" .")
    return (cleaned[:maximum] or fallback).strip()


def vehicle_parts(target: dict[str, Any]) -> tuple[str, str, str]:
    if not isinstance(target, dict):
        raise ValueError("structured vehicle target is required")
    raw_year = str(target.get("year") or "").strip()
    if not re.fullmatch(r"(?:19|20)\d{2}", raw_year):
        raise ValueError("service-information storage requires a four-digit vehicle year")
    make = safe_component(target.get("make"), "", maximum=64)
    model = safe_component(
        target.get("model") or target.get("model_trim"),
        "",
        maximum=96,
    )
    if not make or not model:
        raise ValueError("service-information storage requires vehicle make and model")
    return raw_year, make, model


def service_information_directory(root: Path, target: dict[str, Any]) -> Path:
    year, make, model = vehicle_parts(target)
    return Path(root).resolve() / year / make / model


def adas_map_directory(root: Path, ro_number: object) -> Path:
    ro = str(ro_number or "").strip()
    if not _RO_RE.fullmatch(ro):
        raise ValueError("ADAS Map storage requires a numeric repair-order number")
    return Path(root).resolve() / ADAS_MAP_DIRNAME / ro


def adas_map_pdf_path(root: Path, ro_number: object) -> Path:
    ro = str(ro_number or "").strip()
    return adas_map_directory(root, ro) / f"{ro} ADAS Map.pdf"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _unique_target(target: Path, source: Path) -> Path:
    if not target.exists():
        return target
    try:
        digest = _sha256(source)[:10]
        if _sha256(target) == _sha256(source):
            return target.with_name(f"{target.stem} legacy-{digest}{target.suffix}")
    except OSError:
        pass
    index = 2
    while True:
        candidate = target.with_name(f"{target.stem} ({index}){target.suffix}")
        if not candidate.exists():
            return candidate
        index += 1


def _unique_json_target(target: Path) -> Path:
    if not target.exists():
        return target
    index = 2
    while True:
        candidate = target.with_name(f"{target.stem} ({index}){target.suffix}")
        if not candidate.exists():
            return candidate
        index += 1


def migrate_legacy_adas_map_reports(root: Path) -> dict[str, str]:
    """Move legacy ADAS Map PDFs into ADAS Map/<RO>/.

    Returns absolute old->new mappings so persisted SQLite path metadata can
    be rewritten by the caller. Existing canonical files are untouched.
    """
    root = Path(root).resolve()
    if not root.is_dir():
        return {}
    moved: dict[str, str] = {}
    for source in sorted(root.rglob("*.pdf"), key=lambda p: str(p).casefold()):
        try:
            relative_parts = source.resolve().relative_to(root).parts
        except ValueError:
            continue
        if relative_parts and relative_parts[0].casefold() == ADAS_MAP_DIRNAME.casefold():
            continue
        match = _ADAS_MAP_FILE_RE.fullmatch(source.name)
        if not match:
            continue
        ro = match.group("ro")
        destination = _unique_target(adas_map_pdf_path(root, ro), source)
        destination.parent.mkdir(parents=True, exist_ok=True)
        old_abs = str(source.resolve())
        shutil.move(str(source), str(destination))
        moved[old_abs] = str(destination.resolve())

        sidecar = source.with_name(source.stem + ".source.json")
        if sidecar.is_file():
            side_target = _unique_json_target(
                destination.with_name(destination.stem + ".source.json")
            )
            shutil.move(str(sidecar), str(side_target))
    return moved


def canonicalize_adas_map_path(value: object, root: Path) -> str:
    """Rewrite a stored local legacy report path to its canonical RO path.

    HTTP(S) report links are source URLs and are deliberately not rewritten.
    """
    text = str(value or "").strip()
    if not text or "://" in text:
        return text
    match = _ADAS_MAP_FILE_RE.fullmatch(Path(text).name)
    if not match:
        return text
    return str(adas_map_pdf_path(root, match.group("ro")))


def rewrite_nested_adas_map_paths(value: Any, root: Path) -> Any:
    if isinstance(value, str):
        return canonicalize_adas_map_path(value, root)
    if isinstance(value, list):
        return [rewrite_nested_adas_map_paths(item, root) for item in value]
    if isinstance(value, dict):
        return {
            key: rewrite_nested_adas_map_paths(item, root)
            for key, item in value.items()
        }
    return value
