"""Canonical evidence contract for Navigator-acquired service-information.

Domain/session presence is not evidence, and neither is a model assertion.
This module therefore validates only facts ScrapeX can directly observe.

This is the single authority on *mechanically verified* Navigator evidence.
It proves browser state and extraction only.  Whether the candidate is the
requested procedure is deliberately absent from this contract and belongs to
X's independent semantic reviewer.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any, Optional


def evaluate_navigation_claim(
    *,
    target: dict[str, Any],
    target_state: dict[str, Any],
    navigation_performed: bool,
    candidate_extracted: bool,
    extracted_text: Optional[str],
    source_url: str,
    provider: str,
) -> dict[str, Any]:
    """Return mechanical proof for one Navigator candidate.

    The gates say only that the requested vehicle is selected in the live
    browser, X navigated after selection, X marked the current page as a
    candidate, and ScrapeX extracted real content from that page.  Page type,
    subject relevance, procedure completeness, and dependencies are semantic
    judgments and are intentionally left to X.
    """
    now = datetime.now(UTC).replace(microsecond=0).isoformat()
    base = {
        "vehicle_verified": False,
        "navigation_performed": bool(navigation_performed),
        "candidate_extracted": bool(candidate_extracted),
        "content_extracted": False,
        "source_url": source_url or None,
        "provider": provider,
        "captured_at": now,
        "evidence_sha256": None,
        "verified": False,
        "reason": None,
    }

    if not isinstance(target_state, dict) or not target_state.get("selected"):
        reason = (
            (isinstance(target_state, dict) and target_state.get("reason"))
            or "Target (vehicle/subject) selection was not confirmed."
        )
        return {**base, "reason": str(reason)}
    base["vehicle_verified"] = True

    if not navigation_performed:
        return {**base, "reason": "No target-scoped browser navigation was performed."}

    if not candidate_extracted:
        return {
            **base,
            "reason": "X did not mark the current page as a candidate for extraction.",
        }

    text = str(extracted_text or "").strip()
    if not text:
        return {**base, "reason": "No substantive content was extracted from the candidate page."}
    base["content_extracted"] = True
    base["evidence_sha256"] = hashlib.sha256(text.encode("utf-8")).hexdigest()

    return {**base, "verified": True, "reason": None}


def unselected_target_claim(reason: str, *, provider: str) -> dict[str, Any]:
    """Shared shape for a task that never proved target selection at all."""
    return evaluate_navigation_claim(
        target={},
        target_state={"selected": False, "reason": reason},
        navigation_performed=False,
        candidate_extracted=False,
        extracted_text=None,
        source_url="",
        provider=provider,
    )
