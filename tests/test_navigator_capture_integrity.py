from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from scrapex import navigator_capture_integrity as integrity


SOURCE = "https://my.alldata.com/repair/#/article/123"
TEXT = "OEM front view camera calibration procedure with target setup."


class FakeRunner:
    def __init__(self, root: Path, task: dict):
        self.adas_si_root = root
        self.task = task

    def _require_task(self, task_id):
        assert task_id == "task-1"
        return self.task

    async def capture(self, task_id, *, semantic_review=None, objective=None):  # noqa: ARG002
        return {
            "status": "success",
            "saved": False,
            "already_present": True,
            "task_id": task_id,
            "provider": "alldata",
            "relative_path": "2023/GMC/Acadia/Camera.pdf",
            "source_sidecar": "2023/GMC/Acadia/Camera.source.json",
            "source_url": SOURCE,
            "sha256": "f" * 64,
            "title": "Front View Camera Module Learn",
            "storage_policy": "year/make/model",
            "capture_method": "rendered_page_images",
        }


def _artifact(root: Path, *, text_sidecar: bool = False):
    folder = root / "2023" / "GMC" / "Acadia"
    folder.mkdir(parents=True)
    (folder / "Camera.pdf").write_bytes(b"%PDF-1.4\nlegacy")
    source = {
        "source_url": SOURCE,
        "title": "Front View Camera Module Learn",
        "saved_pdf_sha256": "f" * 64,
        "capture_method": "rendered_page_images",
    }
    if text_sidecar:
        (folder / "Camera.text.txt").write_text(TEXT, encoding="utf-8")
        source.update(
            {
                "extracted_text_path": "Camera.text.txt",
                "extracted_text_sha256": hashlib.sha256(TEXT.encode("utf-8")).hexdigest(),
                "extracted_text_chars": len(TEXT),
            }
        )
    (folder / "Camera.source.json").write_text(json.dumps(source), encoding="utf-8")
    return folder


def _task(text=TEXT):
    return {
        "extract": {"url": SOURCE, "text": text},
        "last_observation": {"url": SOURCE, "page_text": "bounded fallback"},
    }


@pytest.mark.asyncio
async def test_existing_pdf_backfills_missing_machine_readable_text(tmp_path: Path):
    root = tmp_path / "ADAS SI"
    folder = _artifact(root)
    runner = FakeRunner(root, _task())
    integrity.install(FakeRunner)

    result = await runner.capture("task-1")

    text_path = folder / "Camera.text.txt"
    assert result["already_present"] is True
    assert result["saved"] is False
    assert result["sidecar_backfilled"] is True
    assert result["text_sidecar"] == "2023/GMC/Acadia/Camera.text.txt"
    assert text_path.read_text(encoding="utf-8") == TEXT

    source = json.loads((folder / "Camera.source.json").read_text(encoding="utf-8"))
    digest = hashlib.sha256(TEXT.encode("utf-8")).hexdigest()
    assert source["extracted_text_path"] == "Camera.text.txt"
    assert source["extracted_text_sha256"] == digest
    assert source["extracted_text_chars"] == len(TEXT)
    assert source["text_sidecar_backfilled_at"]
    assert result["extracted_text_sha256"] == digest


@pytest.mark.asyncio
async def test_existing_text_is_never_overwritten_and_is_surfaced(tmp_path: Path):
    # Use a fresh class because install() is intentionally idempotent per class.
    class Runner(FakeRunner):
        pass

    root = tmp_path / "ADAS SI"
    folder = _artifact(root, text_sidecar=True)
    runner = Runner(root, _task(text="different current text"))
    integrity.install(Runner)

    result = await runner.capture("task-1")

    assert (folder / "Camera.text.txt").read_text(encoding="utf-8") == TEXT
    assert result["text_sidecar"] == "2023/GMC/Acadia/Camera.text.txt"
    assert result["extracted_text_sha256"] == hashlib.sha256(TEXT.encode("utf-8")).hexdigest()


@pytest.mark.asyncio
async def test_no_text_does_not_invent_a_sidecar_or_downgrade_existing_pdf(tmp_path: Path):
    class Runner(FakeRunner):
        pass

    root = tmp_path / "ADAS SI"
    folder = _artifact(root)
    runner = Runner(
        root,
        {
            "extract": {"url": SOURCE, "text": ""},
            "last_observation": {"url": SOURCE, "page_text": ""},
        },
    )
    integrity.install(Runner)

    result = await runner.capture("task-1")

    assert result["already_present"] is True
    assert "text_sidecar" not in result
    assert not (folder / "Camera.text.txt").exists()
