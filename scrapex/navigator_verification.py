"""Canonical evidence contract for Navigator-acquired service-information.

This is the ScrapeX-side counterpart to X Omni's
``core/services/research_verification.py::evaluate_alldata_claim`` -- same
five-gate philosophy (domain/session presence is not evidence), adapted to
the Navigator's Observation shape and extended with an explicit "reached a
procedure leaf, not just a menu/result listing" gate, since dynamic
drill-down means the terminal page can be many clicks deep and a model can
plausibly stop one level too early.

This is the single authority on "verified" for Navigator-sourced evidence.
Callers (the HTTP layer, X Omni's contract validators) must not recompute
browser semantics themselves -- they only re-check the *shape* of what this
function returns.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any, Optional


def evaluate_navigation_claim(
    *,
    target: dict[str, Any],
    target_state: dict[str, Any],
    query_submitted: bool,
    matched_terms: Optional[list[str]],
    relevance_score: int,
    is_procedure_leaf: bool,
    extracted_text: Optional[str],
    source_url: str,
    provider: str,
    min_relevance: int = 2,
) -> dict[str, Any]:
    """Return a structured proof object for one Navigator task's outcome.

    Each gate corresponds to one claim a truthful acquisition report must be
    able to make: the target (vehicle/subject) was actually selected, a
    search/navigation action was actually taken against it, the destination
    is on-topic, the destination is a procedure leaf rather than a menu or a
    search-results listing, and real content was extracted from it.
    """
    now = datetime.now(UTC).replace(microsecond=0).isoformat()
    base = {
        "vehicle_verified": False,
        "subject_verified": False,
        "procedure_leaf_verified": False,
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

    if not query_submitted:
        return {**base, "reason": "No target-scoped search/navigation action was submitted."}

    matched_terms = list(matched_terms or [])
    if not matched_terms:
        return {
            **base,
            "reason": "The destination did not contain any of the requested subject's terms.",
        }
    if int(relevance_score or 0) < min_relevance:
        return {
            **base,
            "reason": (
                f"Relevance score {relevance_score} is below the verification "
                f"threshold ({min_relevance})."
            ),
        }
    base["subject_verified"] = True

    if not is_procedure_leaf:
        return {
            **base,
            "reason": (
                "Navigation stopped at a menu or result listing, not a procedure "
                "leaf -- the agent must open the actual content, not just find it."
            ),
        }
    base["procedure_leaf_verified"] = True

    text = str(extracted_text or "").strip()
    if not text:
        return {**base, "reason": "No substantive content was extracted from the leaf page."}
    base["content_extracted"] = True
    base["evidence_sha256"] = hashlib.sha256(text.encode("utf-8")).hexdigest()

    identity_tokens = [
        str(target.get(key) or "").casefold() for key in ("year", "make") if target.get(key)
    ]
    folded_text = text.casefold()
    if identity_tokens and not all(token in folded_text for token in identity_tokens):
        return {
            **base,
            "content_extracted": False,
            "evidence_sha256": None,
            "reason": (
                "The leaf page no longer carries the requested vehicle's identity "
                "-- navigation may have drifted off the selected target."
            ),
        }

    return {**base, "verified": True, "reason": None}


def unselected_target_claim(reason: str, *, provider: str) -> dict[str, Any]:
    """Shared shape for a task that never proved target selection at all."""
    return evaluate_navigation_claim(
        target={},
        target_state={"selected": False, "reason": reason},
        query_submitted=False,
        matched_terms=None,
        relevance_score=0,
        is_procedure_leaf=False,
        extracted_text=None,
        source_url="",
        provider=provider,
    )
