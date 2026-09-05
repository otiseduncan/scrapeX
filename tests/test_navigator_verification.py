from scrapex.navigator_verification import evaluate_navigation_claim, unselected_target_claim


def _claim(**overrides):
    base = dict(
        target={"year": 2023, "make": "Toyota"},
        target_state={"selected": True, "reason": None},
        query_submitted=True,
        matched_terms=["blind spot", "calibration"],
        relevance_score=3,
        is_procedure_leaf=True,
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
    assert result["subject_verified"] is True
    assert result["procedure_leaf_verified"] is True
    assert result["content_extracted"] is True
    assert result["evidence_sha256"]


def test_target_not_selected_fails_first_gate():
    result = _claim(target_state={"selected": False, "reason": "no vehicle chosen"})
    assert result["verified"] is False
    assert result["vehicle_verified"] is False
    assert "no vehicle chosen" in result["reason"]


def test_no_query_submitted_fails():
    result = _claim(query_submitted=False)
    assert result["verified"] is False
    assert result["vehicle_verified"] is True
    assert result["subject_verified"] is False


def test_no_matched_terms_fails():
    result = _claim(matched_terms=[])
    assert result["verified"] is False
    assert result["subject_verified"] is False


def test_relevance_below_threshold_fails():
    result = _claim(relevance_score=1, min_relevance=2)
    assert result["verified"] is False
    assert result["subject_verified"] is False


def test_menu_or_result_list_is_not_a_procedure_leaf():
    result = _claim(is_procedure_leaf=False)
    assert result["verified"] is False
    assert result["subject_verified"] is True
    assert result["procedure_leaf_verified"] is False


def test_no_extracted_text_fails():
    result = _claim(extracted_text="")
    assert result["verified"] is False
    assert result["procedure_leaf_verified"] is True
    assert result["content_extracted"] is False


def test_identity_drift_fails_even_with_content():
    result = _claim(extracted_text="some unrelated Honda content with no matching vehicle tokens")
    assert result["verified"] is False
    assert result["content_extracted"] is False
    assert "drifted" in result["reason"]


def test_unselected_target_claim_never_verified():
    result = unselected_target_claim("keyword search only", provider="alldata")
    assert result["verified"] is False
    assert result["vehicle_verified"] is False
    assert result["provider"] == "alldata"


def test_verification_exposes_search_and_relevance_evidence():
    proof = evaluate_navigation_claim(
        target={"year": 2023, "make": "Toyota"},
        target_state={"selected": True, "reason": None},
        query_submitted=True,
        matched_terms=["blind", "spot"],
        relevance_score=2,
        is_procedure_leaf=True,
        extracted_text="2023 Toyota blind spot calibration procedure",
        source_url="https://my.alldata.com/leaf",
        provider="alldata",
    )
    assert proof["query_submitted"] is True
    assert proof["matched_terms"] == ["blind", "spot"]
    assert proof["relevance_score"] == 2
