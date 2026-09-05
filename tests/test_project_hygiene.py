from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

import scrapex
from scrapex.config import Settings


ROOT = Path(__file__).resolve().parents[1]


def _project() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def _requirement_name(value: str) -> str:
    return re.split(r"[\[<>=!~ ]", value, maxsplit=1)[0].casefold()


def _constraint_names() -> set[str]:
    names: set[str] = set()
    for raw_line in (ROOT / "constraints.txt").read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if line and not line.startswith("#"):
            names.add(_requirement_name(line))
    return names


def test_release_metadata_and_documentation_are_coherent() -> None:
    project = _project()["project"]
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert project["version"] == scrapex.__version__ == "0.5.0"
    assert readme.startswith("# ScrapeX v0.5.0\n")
    for term in ("calibration iq", "adas map", "work chrome"):
        assert term in project["description"].casefold()
        assert term in readme.casefold()
    assert "agentic service-information navigation" in readme.casefold()
    assert "old alldata batch runner remains retired/frozen" in readme.casefold()


def test_constraints_cover_every_declared_dependency() -> None:
    project = _project()["project"]
    declared = {
        _requirement_name(item)
        for item in (
            list(project["dependencies"])
            + list(project["optional-dependencies"]["dev"])
        )
    }

    assert declared <= _constraint_names()


def test_gitignore_covers_runtime_build_browser_and_secret_state() -> None:
    lines = {
        line.strip()
        for line in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    required = {
        ".venv/",
        "__pycache__/",
        "*.egg-info/",
        "build/",
        "dist/",
        "data/",
        "runtime/",
        "browser-profile/",
        "*-browser-profile/",
        "captures/",
        "downloads/",
        "downloaded/",
        "tmp/",
        "temp/",
        "*.sqlite3",
        "*.log",
        ".env",
        ".env.*",
        "!.env.example",
        "secrets/",
        "credentials/",
    }

    assert required <= lines


def test_env_example_contains_no_secret_value() -> None:
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "SCRAPEX_HOST=127.0.0.1" in example
    for raw_line in example.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if any(marker in key.casefold() for marker in ("token", "password", "secret", "key")):
            assert value == ""


def test_install_script_is_standalone_and_constraint_backed() -> None:
    script = (ROOT / "scripts" / "install.ps1").read_text(encoding="utf-8")
    folded = script.casefold()

    assert "x:\\x omni" not in folded
    assert "constraints.txt" in folded
    assert "python 3.11 through 3.14" in folded
    assert "pip check" in folded
    assert "-p no:cacheprovider" in script
    assert "playwright install" not in folded
    assert "task-based si navigator is available" in folded
    assert "legacy alldata batch runner remains retired/frozen" in folded


def test_start_script_uses_validated_settings_for_its_endpoint() -> None:
    script = (ROOT / "scripts" / "start.ps1").read_text(encoding="utf-8")

    assert "from scrapex.config import Settings" in script
    assert "Settings.load()" in script
    assert "$env:SCRAPEX_HOST = $BindHost" in script
    assert "$env:SCRAPEX_PORT = [string]$Port" in script
    assert "$env:SCRAPEX_DATA_ROOT =" not in script
    assert "$env:SCRAPEX_ADAS_SI_ROOT =" not in script


def test_git_bootstrap_never_auto_stages_commits_or_pushes() -> None:
    script = (ROOT / "scripts" / "init-github.ps1").read_text(encoding="utf-8")
    folded = script.casefold()

    assert '"add", "--dry-run", "--all"' in folded
    assert 'arguments @("commit"' not in folded
    assert 'arguments @("push"' not in folded
    assert "--push" not in folded
    assert "no files were staged, committed, or pushed" in folded


def _settings_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    root = tmp_path / "ScrapeX"
    root.mkdir()
    monkeypatch.setenv("SCRAPEX_ROOT", str(root))
    monkeypatch.setenv("SCRAPEX_DATA_ROOT", str(root / "data"))
    monkeypatch.setenv("SCRAPEX_ADAS_SI_ROOT", str(tmp_path / "ADAS SI"))
    monkeypatch.setenv("SCRAPEX_CIQ_PROJECT_PATH", str(tmp_path / "Calibration IQ"))
    monkeypatch.delenv("SCRAPEX_PORT", raising=False)
    return root


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        ("localhost", "127.0.0.1"),
        ("127.0.0.1", "127.0.0.1"),
        ("127.42.0.1", "127.42.0.1"),
        ("::1", "::1"),
        ("[::1]", "::1"),
    ],
)
def test_settings_accept_only_loopback_bindings(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    configured: str,
    expected: str,
) -> None:
    _settings_environment(monkeypatch, tmp_path)
    monkeypatch.setenv("SCRAPEX_HOST", configured)

    assert Settings.load().host == expected


@pytest.mark.parametrize("configured", ["0.0.0.0", "192.168.1.20", "scrapex.local"])
def test_settings_reject_external_or_hostname_bindings(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    configured: str,
) -> None:
    root = _settings_environment(monkeypatch, tmp_path)
    monkeypatch.setenv("SCRAPEX_HOST", configured)

    with pytest.raises(ValueError, match="loopback"):
        Settings.load()
    assert not (root / "data").exists()


@pytest.mark.parametrize("configured", ["0", "65536", "not-a-port"])
def test_settings_reject_invalid_ports(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    configured: str,
) -> None:
    _settings_environment(monkeypatch, tmp_path)
    monkeypatch.setenv("SCRAPEX_HOST", "127.0.0.1")
    monkeypatch.setenv("SCRAPEX_PORT", configured)

    with pytest.raises(ValueError, match="SCRAPEX_PORT"):
        Settings.load()


def test_local_env_is_loaded_and_its_scoped_token_wins(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = _settings_environment(monkeypatch, tmp_path)
    project = tmp_path / "Calibration IQ"
    project.mkdir()
    (project / ".env").write_text("TOOL_SERVICE_TOKEN=legacy-project-token\n", encoding="utf-8")
    (root / ".env").write_text(
        "SCRAPEX_HOST=127.0.0.1\n"
        "SCRAPEX_PORT=9125\n"
        "SCRAPEX_CIQ_TOKEN=local-scoped-token\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("SCRAPEX_HOST", raising=False)
    monkeypatch.delenv("SCRAPEX_PORT", raising=False)
    monkeypatch.delenv("SCRAPEX_CIQ_TOKEN", raising=False)

    settings = Settings.load()

    assert settings.port == 9125
    assert settings.ciq_token() == "local-scoped-token"
    assert settings.ciq_token_key_names() == [
        "SCRAPEX_CIQ_TOKEN",
        "TOOL_SERVICE_TOKEN",
    ]
