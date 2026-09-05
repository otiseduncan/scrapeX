from __future__ import annotations

import re
from pathlib import Path

from .models import VehicleSpec

# Mirrors alldata.py's alias handling -- kept separate so this module has no
# dependency on Playwright.
MAKE_ALIASES = {
    "chevrolet": {"chevrolet", "chevy"},
    "nissan": {"nissan", "datsun"},
    "mercedes-benz": {"mercedes-benz", "mercedes", "benz"},
    "volkswagen": {"volkswagen", "vw"},
    "ram": {"ram", "dodge ram"},
    "gmc": {"gmc"},
}

# Groups of near-synonymous ADAS calibration terms. A calibration requirement
# and a candidate SI filename are considered a match only when they share at
# least one bucket -- this is deliberately coarse-grained (radar is radar,
# whichever exact sensor) rather than exact-string matching, because ADAS Map
# and the SI filenames never phrase things identically.
CALIBRATION_BUCKETS: dict[str, tuple[str, ...]] = {
    "camera": ("camera", "windshield", "mono-camera", "monocamera", "multipurpose camera"),
    "radar": ("radar", "millimeter wave", "mm wave", "long-range", "long range", "front radar"),
    "blind_spot": ("blind spot", "blind-spot", "bsm", "corner radar", "rear cross"),
    "steering_angle": ("steering angle", "steering-angle", "sas"),
    "parking": ("parking", "park assist", "ipa", "parking sensor", "parking aid", "sodcm", "ccm"),
    "surround_view": ("360", "surround view", "around view", "avm", "panoramic"),
    "occupant": ("occupant", "seat weight", "seat-weight", "ocs", "seat related"),
    "lkas": ("lkas", "lane keep", "lane departure"),
    "rear_camera": ("rear camera", "reverse camera", "backup camera", "rear television"),
    "acc": ("acc", "adaptive cruise"),
}

_YEAR_RANGE_RE = re.compile(r"(?<!\d)(19|20)\d{2}\s*-\s*(19|20)\d{2}(?!\d)")
_YEAR_RE = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")


def _plain(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())


def _make_tokens(make: str) -> set[str]:
    key = str(make or "").casefold()
    return {_plain(alias) for alias in MAKE_ALIASES.get(key, {key})}


def _calibration_buckets(text: str) -> set[str]:
    folded = str(text or "").casefold()
    return {
        bucket
        for bucket, hints in CALIBRATION_BUCKETS.items()
        if any(hint in folded for hint in hints)
    }


def _year_matches(filename: str, year: int) -> bool:
    range_match = _YEAR_RANGE_RE.search(filename)
    if range_match:
        lo = int(filename[range_match.start():range_match.start() + 4])
        hi = int(filename[range_match.end() - 4:range_match.end()])
        lo, hi = min(lo, hi), max(lo, hi)
        return lo <= year <= hi
    for match in _YEAR_RE.finditer(filename):
        if int(match.group(1)) == year:
            return True
    return False


def _model_matches(filename_plain: str, filename_casefold: str, model: str) -> bool:
    # Require the nameplate token to appear -- prefer a token that contains
    # a letter (e.g. "Camry", "F-150", "IS", "ES") over a purely numeric one
    # ("350", "500"), since trim/displacement numbers are shared across
    # unrelated nameplates (Lexus "IS 350" vs "ES 350" is exactly this
    # trap: matching on "350" alone would treat them as the same vehicle).
    stopwords = {
        "fwd", "awd", "rwd", "4wd", "2wd", "se", "le", "xle", "sport",
        "sedan", "coupe", "hatchback", "crew", "cab", "truck", "auto",
        "automatic", "manual", "base", "limited", "premium", "plus",
        "package", "pkg", "w", "with",
    }
    tokens = [token for token in re.split(r"[^A-Za-z0-9]+", model) if token]
    candidates = [token for token in tokens if token.casefold() not in stopwords]
    if not candidates:
        candidates = tokens
    if not candidates:
        return False

    lettered = [token for token in candidates if re.search(r"[A-Za-z]", token)]
    pool = lettered or candidates
    best = max(pool, key=len)
    needle = _plain(best)
    if not needle:
        return False

    if len(needle) <= 3:
        # Short alphabetic nameplates ("IS", "ES", "RX") are exactly the
        # ambiguous case above -- concatenated-plain substring matching
        # would also hit inside ordinary words ("aSSIst" contains "is").
        # Require it as a real standalone word in the filename instead,
        # which costs matching on filenames that glue words together with
        # zero separators at all -- an acceptable trade since a missed
        # match just means "not proven covered", not a false positive.
        if not re.search(rf"\b{re.escape(needle)}\b", filename_casefold):
            return False
    elif needle not in filename_plain:
        return False

    return True


def check_coverage(
    vehicle: VehicleSpec,
    requirements: list[str],
    adas_si_root: Path,
) -> dict:
    """Determine conservative local coverage for an already-known scope.

    This frozen helper never creates calibration requirements and is not part
    of the current automated ADAS Map workflow.
    """
    root = Path(adas_si_root)
    files: list[tuple[Path, str, str, str]] = []
    if root.exists():
        for path in root.rglob("*.pdf"):
            try:
                relative = path.relative_to(root)
            except ValueError:
                continue
            relative_text = relative.as_posix()
            files.append(
                (path, relative_text, _plain(relative_text), relative_text.casefold())
            )

    make_tokens = _make_tokens(str(vehicle.make or ""))
    vehicle_candidates = [
        (path, relative)
        for path, relative, plain, casefold in files
        if vehicle.year is not None
        and _year_matches(relative, vehicle.year)
        and any(token and token in plain for token in make_tokens)
        and _model_matches(plain, casefold, str(vehicle.model or ""))
    ]

    results = []
    for requirement in requirements:
        requirement_buckets = _calibration_buckets(requirement)
        matches = []
        if requirement_buckets:
            for path, relative in vehicle_candidates:
                if _calibration_buckets(relative) & requirement_buckets:
                    matches.append(str(path))

        results.append({
            "requirement": requirement,
            "covered": bool(matches),
            "matched_files": matches[:5],
        })

    covered_count = sum(1 for result in results if result["covered"])
    return {
        "vehicle_label": vehicle.label,
        "total_requirements": len(requirements),
        "covered_count": covered_count,
        "missing": [result["requirement"] for result in results if not result["covered"]],
        "results": results,
        "si_ready": len(requirements) > 0 and covered_count == len(requirements),
    }
