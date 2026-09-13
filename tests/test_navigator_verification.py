from pathlib import Path

from scrapex.navigator_verification import evaluate_navigation_claim, unselected_target_claim


def _claim(**overrides):
    base = dict(
        target={"year": 2023, "make": "Toyota"},
        target_state={"selected": True, "reason": None},
        navigation_performed=True,
        candidate_extracted=True,
        extracted_text="2023 Toyota blind spot calibration procedure: step one, step two.",
        source_url="https://my.alldata.com/repair/article/123",
        provider="alldata",
    )
    base.update(overrides)
    return evaluate_navigation_claim(**base)


def test_fully_satisfied_claim_is_verified():
    result = _claim()
    assert result["verified"] is True
    assert result["reason"] is None
    assert result["vehicle_verified"] is True
    assert result["navigation_performed"] is True
    assert result["candidate_extracted"] is True
    assert result["content_extracted"] is True
    assert result["evidence_sha256"]


def test_target_not_selected_fails_first_gate():
    result = _claim(target_state={"selected": False, "reason": "no vehicle chosen"})
    assert result["verified"] is False
    assert result["vehicle_verified"] is False
    assert "no vehicle chosen" in result["reason"]


def test_no_navigation_performed_fails():
    result = _claim(navigation_performed=False)
    assert result["verified"] is False
    assert result["vehicle_verified"] is True
    assert "navigation" in result["reason"].casefold()


def test_candidate_not_extracted_fails():
    result = _claim(candidate_extracted=False)
    assert result["verified"] is False
    assert "candidate" in result["reason"].casefold()


def test_no_extracted_text_fails():
    result = _claim(extracted_text="")
    assert result["verified"] is False
    assert result["candidate_extracted"] is True
    assert result["content_extracted"] is False


def test_scrapex_does_not_semantically_reject_unrelated_content():
    result = _claim(extracted_text="some unrelated Honda content with no matching vehicle tokens")
    assert result["verified"] is True
    assert result["content_extracted"] is True
    assert result["reason"] is None


def test_unselected_target_claim_never_verified():
    result = unselected_target_claim("keyword search only", provider="alldata")
    assert result["verified"] is False
    assert result["vehicle_verified"] is False
    assert result["provider"] == "alldata"


def test_verification_exposes_only_mechanical_evidence():
    proof = evaluate_navigation_claim(
        target={"year": 2023, "make": "Toyota"},
        target_state={"selected": True, "reason": None},
        navigation_performed=True,
        candidate_extracted=True,
        extracted_text="2023 Toyota blind spot calibration procedure",
        source_url="https://my.alldata.com/leaf",
        provider="alldata",
    )
    assert proof["navigation_performed"] is True
    assert proof["candidate_extracted"] is True
    assert "matched_terms" not in proof
    assert "relevance_score" not in proof
    assert "subject_verified" not in proof


def test_production_navigator_contains_no_semantic_scoring_gate():
    root = Path(__file__).resolve().parents[1] / "scrapex"
    source = "\n".join(
        (root / name).read_text(encoding="utf-8")
        for name in (
            "navigator_verification.py",
            "navigator_worker.py",
            "navigator_providers.py",
            "alldata_navigator.py",
        )
    )
    for forbidden in ("match_terms", "relevance_score", "_CONCEPT_GROUPS", "subject_verified"):
        assert forbidden not in source


def test_mechanical_verification_does_not_make_the_browser_task_terminal(tmp_path):
    from scrapex.db import Store

    store = Store(tmp_path / "db.sqlite")
    task_id = store.create_navigator_task("alldata", {}, "topic", 10)
    store.set_navigator_task_state(task_id, "active")
    store.save_navigator_verification(task_id, {"verified": True})
    task = store.navigator_task(task_id)
    assert task["verified"] is True
    assert task["state"] == "active"
