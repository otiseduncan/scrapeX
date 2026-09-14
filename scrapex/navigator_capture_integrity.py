"""Repair legacy Navigator capture metadata when a verified PDF is reused.

Navigator capture de-duplicates by verified source URL. Older captures can have
the PDF and provenance ``.source.json`` but predate the machine-readable
``.text.txt`` sidecar. Returning ``already_present=True`` without that sidecar
is truthful about the PDF but incomplete for the current capture contract.

This wrapper never changes which page is accepted or whether a PDF is reused.
After the canonical capture method returns an existing artifact, it uses the
current task's already-extracted text for that exact same source URL to backfill
only a missing text sidecar and its provenance fields. Existing text is never
overwritten.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from functools import wraps
from pathlib import Path
from typing import Any

_INSTALLED_ATTR = "__scrapex_capture_integrity_installed__"


def _safe_text_path(sidecar: Path, existing: dict[str, Any]) -> Path:
    recorded = str(existing.get("extracted_text_path") or "").strip()
    if recorded and Path(recorded).name == recorded:
        return sidecar.with_name(recorded)
    base = sidecar.name.removesuffix(".source.json")
    return sidecar.with_name(base + ".text.txt")


def _current_extracted_text(task: dict[str, Any], source_url: str) -> str:
    extract = task.get("extract") if isinstance(task.get("extract"), dict) else {}
    if str(extract.get("url") or "").strip() == source_url:
        return str(extract.get("text") or "")
    observation = (
        task.get("last_observation")
        if isinstance(task.get("last_observation"), dict)
        else {}
    )
    if str(observation.get("url") or "").strip() == source_url:
        return str(observation.get("page_text") or "")
    return ""


def _relative(root: Path, path: Path) -> str:
    return str(path.relative_to(root)).replace("\\", "/")


def install(runner_class: Any) -> None:
    """Install a post-capture integrity repair on ``NavigatorTaskRunner``."""
    if getattr(runner_class, _INSTALLED_ATTR, False):
        return

    original_capture = runner_class.capture

    @wraps(original_capture)
    async def capture_with_integrity(
        self: Any,
        task_id: str,
        *,
        semantic_review: Any = None,
        objective: Any = None,
    ) -> dict[str, Any]:
        result = await original_capture(
            self,
            task_id,
            semantic_review=semantic_review,
            objective=objective,
        )
        if not isinstance(result, dict) or result.get("already_present") is not True:
            return result

        root_value = getattr(self, "adas_si_root", None)
        sidecar_relative = str(result.get("source_sidecar") or "").strip()
        source_url = str(result.get("source_url") or "").strip()
        if root_value is None or not sidecar_relative or not source_url:
            return result

        root = Path(root_value).resolve()
        sidecar = (root / sidecar_relative).resolve()
        try:
            sidecar.relative_to(root)
        except ValueError:
            return result
        if not sidecar.is_file():
            return result

        try:
            existing = json.loads(sidecar.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError):
            return result
        if not isinstance(existing, dict):
            return result
        if str(existing.get("source_url") or "").strip() != source_url:
            return result

        text_path = _safe_text_path(sidecar, existing)
        try:
            text_path.resolve().relative_to(root)
        except ValueError:
            return result

        # If the sidecar already exists, surface it on the dedupe result even
        # when the older API response omitted that field.
        if text_path.is_file():
            try:
                text = text_path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                return result
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
            changed = False
            if existing.get("extracted_text_path") != text_path.name:
                existing["extracted_text_path"] = text_path.name
                changed = True
            if existing.get("extracted_text_sha256") != digest:
                existing["extracted_text_sha256"] = digest
                changed = True
            if existing.get("extracted_text_chars") != len(text):
                existing["extracted_text_chars"] = len(text)
                changed = True
            if changed:
                sidecar.write_text(
                    json.dumps(existing, indent=2, ensure_ascii=False, default=str),
                    encoding="utf-8",
                )
            out = dict(result)
            out["text_sidecar"] = _relative(root, text_path)
            out["extracted_text_sha256"] = digest
            out["sidecar_backfilled"] = changed
            return out

        task = self._require_task(task_id)
        text = _current_extracted_text(task, source_url)
        if not text:
            # Image-only evidence can legitimately have no textual payload; do
            # not invent one and do not downgrade the already-verified PDF.
            return result

        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        text_path.write_text(text, encoding="utf-8")
        existing["extracted_text_path"] = text_path.name
        existing["extracted_text_sha256"] = digest
        existing["extracted_text_chars"] = len(text)
        existing["text_sidecar_backfilled_at"] = (
            datetime.now(UTC).replace(microsecond=0).isoformat()
        )
        sidecar.write_text(
            json.dumps(existing, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )

        out = dict(result)
        out["text_sidecar"] = _relative(root, text_path)
        out["extracted_text_sha256"] = digest
        out["sidecar_backfilled"] = True
        return out

    runner_class.capture = capture_with_integrity
    setattr(runner_class, _INSTALLED_ATTR, True)
