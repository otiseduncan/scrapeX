from __future__ import annotations

import re
from typing import Any


VIN_RE = re.compile(r"\b[A-HJ-NPR-Z0-9]{17}\b", re.IGNORECASE)

CALIBRATION_HINT_RE = re.compile(
    r"\b(?:calibrat(?:e|ion)|aim(?:ing)?|relearn|initiali[sz](?:e|ation)|"
    r"steering\s+angle|occupant\s+classification|zero\s+point|radar|camera|"
    r"blind\s+spot|bsm|around\s+view|surround\s+view)\b",
    re.IGNORECASE,
)


def _plain(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())


def normalize_shop(value: Any) -> str:
    text = str(value or "").casefold()
    aliases = {
        "macon": "macon",
        "mercer university": "macon",
        "warner robins": "warnerrobins",
        "warner": "warnerrobins",
        "perry": "perry",
    }
    for token, normalized in aliases.items():
        if token in text:
            return normalized
    return _plain(text)


def row_matches_shop(row_text: str, shop: str | None) -> bool:
    if not shop:
        return True
    wanted = normalize_shop(shop)
    observed = normalize_shop(row_text)
    # Some portal grids do not repeat location in each row. Reject only when
    # a different supported shop is explicitly visible.
    known = {normalize_shop(x) for x in ("Macon", "Warner Robins", "Perry")}
    explicit = {location for location in known if location and location in observed}
    return not explicit or wanted in explicit or wanted in observed


def extract_vin(text: str) -> str | None:
    for match in VIN_RE.findall(str(text or "").upper()):
        candidate = match.upper()
        if len(candidate) == 17 and not any(ch in candidate for ch in "IOQ"):
            return candidate
    return None


def normalize_calibration_label(text: Any) -> str | None:
    """Normalize already-structured labels; never discover requirements.

    Requirement discovery belongs exclusively to the document-scoped Work
    Chrome UI Automation parser. This helper remains for compatibility and
    tests that normalize labels after that authoritative extraction.
    """

    value = " ".join(str(text or "").replace("\u00a0", " ").split()).strip(
        " -:\u2022"
    )
    if not value or len(value) > 220 or not CALIBRATION_HINT_RE.search(value):
        return None
    parts = [
        " ".join(part.split()).strip(" -:\u2022")
        for part in re.split(r"[\t\r\n|]+", value)
        if part.strip()
    ]
    candidates = [part for part in parts if CALIBRATION_HINT_RE.search(part)]
    best = min(candidates or [value], key=len)
    return re.sub(r"\s{2,}", " ", best).strip()[:200] or None


def calibration_labels_from_texts(texts: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for text in texts:
        label = normalize_calibration_label(text)
        if not label:
            continue
        key = _plain(label)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(label)
    return out


class AdasMapBrowser:
    """Retired separate-profile adapter retained as a fail-closed shim.

    ADAS Map authentication exists only in the managed work Chrome session.
    ScrapeX must use :class:`WorkChromeAdasMapSource`; it never launches or
    reads another Chrome profile for ADAS Map.
    """

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        self.page = None

    @staticmethod
    def _unsupported() -> dict[str, Any]:
        return {
            "success": False,
            "active": False,
            "authenticated": False,
            "status": "managed_work_chrome_required",
            "reason": (
                "ADAS Map is supported only through the already-open managed "
                "Work Chrome UI Automation bridge."
            ),
        }

    async def open(self) -> dict[str, Any]:
        return self._unsupported()

    async def status(self) -> dict[str, Any]:
        return self._unsupported()

    async def lookup(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return self._unsupported()

    async def close(self) -> None:
        return None
