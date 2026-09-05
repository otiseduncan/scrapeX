from pathlib import Path
from scrapex.config import Settings

def test_ciq_token_is_read_from_project_env(monkeypatch,tmp_path:Path):
    project=tmp_path/"calibration iq"
    project.mkdir()
    (project/".env").write_text("TOOL_SERVICE_TOKEN='abc123'\n",encoding="utf-8")
    monkeypatch.setenv("SCRAPEX_ROOT",str(tmp_path/"ScrapeX"))
    monkeypatch.setenv("SCRAPEX_DATA_ROOT",str(tmp_path/"ScrapeX"/"data"))
    monkeypatch.setenv("SCRAPEX_ADAS_SI_ROOT",str(tmp_path/"ADAS SI"))
    monkeypatch.setenv("SCRAPEX_CIQ_PROJECT_PATH",str(project))
    monkeypatch.delenv("SCRAPEX_CIQ_TOKEN",raising=False)
    s=Settings.load()
    assert s.ciq_token()=="abc123"
    assert s.ciq_base_url.endswith("/api/v1/tools/v1/calibration-iq")
