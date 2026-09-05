from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass
from pathlib import Path

TOKEN_KEYS = (
    "TOOL_SERVICE_TOKEN",
    "TOOLS_SERVICE_TOKEN",
    "TOOL_API_TOKEN",
    "SERVICE_TOKEN",
    "API_TOKEN",
    "XV12_CALIBRATION_IQ_ACCESS_TOKEN",
    "CALIBRATION_IQ_ACCESS_TOKEN",
)

def _path(value: str) -> Path:
    return Path(value).expanduser()

def _unquote(value: str) -> str:
    v = str(value or "").strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in {'"', "'"}:
        return v[1:-1]
    return v

def _read_env_file(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    try:
        raw = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return {}
    values: dict[str, str] = {}
    for raw_line in raw.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = _unquote(value)
    return values


def _configured_value(
    name: str,
    default: str,
    local_values: dict[str, str],
) -> str:
    return str(os.environ.get(name, local_values.get(name, default))).strip()


def _loopback_host(value: str) -> str:
    host = _unquote(value).strip()
    if not host:
        host = "127.0.0.1"
    if host.casefold() == "localhost":
        return "127.0.0.1"

    address_text = host[1:-1] if host.startswith("[") and host.endswith("]") else host
    try:
        address = ipaddress.ip_address(address_text)
    except ValueError as exc:
        raise ValueError(
            "SCRAPEX_HOST must be localhost or a literal loopback IP address."
        ) from exc
    if not address.is_loopback:
        raise ValueError(
            "ScrapeX has no authenticated remote-listener design; "
            "SCRAPEX_HOST must be loopback-only."
        )
    return address.compressed


def _port(value: str) -> int:
    try:
        port = int(_unquote(value))
    except (TypeError, ValueError) as exc:
        raise ValueError("SCRAPEX_PORT must be an integer from 1 through 65535.") from exc
    if not 1 <= port <= 65535:
        raise ValueError("SCRAPEX_PORT must be an integer from 1 through 65535.")
    return port

@dataclass(frozen=True)
class Settings:
    root: Path
    data_root: Path
    adas_si_root: Path
    host: str
    port: int
    alldata_home: str
    delay_seconds: float
    max_documents_per_vehicle: int
    ciq_base_url: str
    ciq_project_path: Path
    adas_map_home: str
    # Optional Chrome "Profile Directory" name (e.g. "Profile 2") that owns the
    # managed ADAS Map work session. When set, opening the sign-in page targets
    # exactly that profile. ADAS Map and ALLDATA/SI sessions are deliberately
    # separate; this never merges them.
    adas_map_chrome_profile: str

    @classmethod
    def load(cls):
        root = _path(os.environ.get("SCRAPEX_ROOT", r"X:\ScrapeX"))
        local_values = _read_env_file(root / ".env")
        data = _path(
            _configured_value("SCRAPEX_DATA_ROOT", str(root / "data"), local_values)
        )
        host = _loopback_host(
            _configured_value("SCRAPEX_HOST", "127.0.0.1", local_values)
        )
        port = _port(_configured_value("SCRAPEX_PORT", "8125", local_values))
        data.mkdir(parents=True, exist_ok=True)
        return cls(
            root=root,
            data_root=data,
            adas_si_root=_path(
                _configured_value("SCRAPEX_ADAS_SI_ROOT", r"X:\ADAS SI", local_values)
            ),
            host=host,
            port=port,
            alldata_home=_configured_value(
                "SCRAPEX_ALLDATA_HOME", "https://my.alldata.com/", local_values
            ),
            delay_seconds=max(
                1.25,
                float(
                    _configured_value(
                        "SCRAPEX_DELAY_SECONDS", "2.0", local_values
                    )
                ),
            ),
            max_documents_per_vehicle=min(
                75,
                max(
                    1,
                    int(
                        _configured_value(
                            "SCRAPEX_MAX_DOCUMENTS", "50", local_values
                        )
                    ),
                ),
            ),
            ciq_base_url=_configured_value(
                "SCRAPEX_CIQ_BASE_URL",
                "http://127.0.0.1:8084/api/v1/tools/v1/calibration-iq",
                local_values,
            ).rstrip("/"),
            ciq_project_path=_path(
                _configured_value(
                    "SCRAPEX_CIQ_PROJECT_PATH", r"X:\calibration iq", local_values
                )
            ),
            adas_map_home=_configured_value(
                "SCRAPEX_ADAS_MAP_HOME",
                "https://opus.adasmap.com/login",
                local_values,
            ),
            adas_map_chrome_profile=_configured_value(
                "SCRAPEX_ADAS_MAP_CHROME_PROFILE", "", local_values
            ),
        )

    def ciq_token(self) -> str:
        local_values = _read_env_file(self.root / ".env")
        override = str(
            os.environ.get(
                "SCRAPEX_CIQ_TOKEN", local_values.get("SCRAPEX_CIQ_TOKEN", "")
            )
        ).strip()
        if override:
            return _unquote(override)
        project_values = _read_env_file(self.ciq_project_path / ".env")
        for key in TOKEN_KEYS:
            value = local_values.get(key) or project_values.get(key)
            if value:
                return value
        return ""

    def ciq_token_key_names(self) -> list[str]:
        local_values = _read_env_file(self.root / ".env")
        project_values = _read_env_file(self.ciq_project_path / ".env")
        token_names = {"SCRAPEX_CIQ_TOKEN", *TOKEN_KEYS}
        return sorted(
            key
            for key in token_names
            if local_values.get(key) or project_values.get(key)
        )
