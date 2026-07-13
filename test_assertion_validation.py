"""
Phase 5 proof — deterministic, non-Gemini tests for hearth_assertion_validation.py.

Unlike test_manager_advice_scenario.py (real end-to-end, real Gemini, real
DB), these tests exercise the validation module directly against synthetic
parsed model output, so every failure category (A/B/C) is reproducible on
demand rather than left to whatever a live Gemini call happens to produce.
No DB, no network, no Gemini client.

Run: venv/bin/python3 test_assertion_validation.py
"""

import hearth_assertion_validation as v

EVIDENCE = {
    "get_building_context": {
        "summary": {
            "summary": "Livestreamer, joined the program focused on variety gaming content.",
            "state": {"activity_level": "quiet"},
            "furniture": [{"fact_text": "Enjoys woodworking.", "fact_type": "interest"}],
        },
        "connections": [{"road": {"type": "coached_by"}, "building": {"name": "Sarah"}}],
    },
    "get_active_beliefs": [
        {"belief_text": "Engagement momentum has been slowing over the last several weeks.", "confidence": 0.55},
    ],
}


def _pool():
    return v.build_evidence_pool(EVIDENCE)


def test_category_a_missing_assertions_key():
    outcome = v.validate_assertions({"not_assertions": []}, _pool())
    assert outcome.failure_category == "A", outcome
    assert outcome.assertions == []


def test_category_a_missing_required_category():
    # No "uncertainty" assertion at all -> Category A, even though shape is otherwise fine.
    parsed = {"assertions": [
        {"category": "knowledge", "text": "X", "evidence_quote": "Enjoys woodworking."},
        {"category": "reasoning", "text": "Y"},
    ]}
    outcome = v.validate_assertions(parsed, _pool())
    assert outcome.failure_category == "A", outcome
    assert any("missing required" in n for n in outcome.notes), outcome.notes


def test_category_a_gemini_call_failed_returns_none():
    outcome = v.validate_assertions(None, _pool())
    assert outcome.failure_category == "A", outcome


def test_category_b_ungrounded_knowledge_then_closure():
    parsed = {"assertions": [
        {"category": "knowledge", "text": "Ethan streamed for 10 hours yesterday.", "evidence_quote": "streamed for 10 hours yesterday"},
        {"category": "reasoning", "text": "Reaching out seems reasonable."},
        {"category": "uncertainty", "text": "Not sure about the rest."},
    ]}
    outcome = v.validate_assertions(parsed, _pool())
    assert outcome.failure_category == "B", outcome
    assert not outcome.ok

    closed = v.close_out_ungrounded_knowledge(outcome)
    assert closed.ok
    assert closed.failure_category is None
    categories = {a.category for a in closed.assertions}
    assert "knowledge" not in categories, "ungrounded knowledge assertion must not survive as knowledge"
    converted = [a for a in closed.assertions if "streamed for 10 hours" in a.text]
    assert converted and converted[0].category == "uncertainty"


def test_category_c_hallucinated_quote_on_reasoning_converts_to_uncertainty():
    parsed = {"assertions": [
        {"category": "reasoning", "text": "Ethan definitely viewed Live Hosting training yesterday.", "evidence_quote": "viewed Live Hosting training yesterday"},
        {"category": "uncertainty", "text": "Not sure about anything else."},
    ]}
    outcome = v.validate_assertions(parsed, _pool())
    assert outcome.ok, outcome.notes  # Category C is corrected in place, not a blocking failure
    assert outcome.failure_category is None
    converted = [a for a in outcome.assertions if "Live Hosting" in a.text]
    assert converted and converted[0].category == "uncertainty"
    assert converted[0].evidence_quote is None
    assert any("Category C" in n for n in outcome.notes)


def test_reasoning_with_verifiable_quote_is_promoted_to_knowledge():
    parsed = {"assertions": [
        {"category": "reasoning", "text": "His momentum has been slowing.", "evidence_quote": "Engagement momentum has been slowing over the last several weeks."},
        {"category": "uncertainty", "text": "Not sure about anything else."},
    ]}
    outcome = v.validate_assertions(parsed, _pool())
    assert outcome.ok
    promoted = [a for a in outcome.assertions if "momentum" in a.text]
    assert promoted and promoted[0].category == "knowledge"
    assert promoted[0].source == "get_active_beliefs"
    assert any("Reclassified" in n for n in outcome.notes)


def test_uncertainty_without_hedge_language_is_flagged_but_not_blocked():
    parsed = {"assertions": [
        {"category": "reasoning", "text": "Reaching out seems reasonable."},
        {"category": "uncertainty", "text": "Ethan will definitely respond well."},  # no hedge markers
    ]}
    outcome = v.validate_assertions(parsed, _pool())
    assert outcome.ok  # a lexical miss is logged, not a blocking failure
    assert any("genuineness unconfirmed" in n for n in outcome.notes)


def test_category_d_deterministic_fallback_never_empty():
    assertions = v.deterministic_fallback_assertions(_pool(), ["Some limitation."])
    categories = {a.category for a in assertions}
    assert categories == {"knowledge", "reasoning", "uncertainty"}
    text = v.render_assertions(assertions)
    assert "Hearth Knowledge:" in text and "General Reasoning:" in text and "Uncertainty:" in text


def test_category_d_deterministic_fallback_with_no_evidence_at_all():
    assertions = v.deterministic_fallback_assertions([], ["Some limitation."])
    knowledge = [a for a in assertions if a.category == "knowledge"]
    assert len(knowledge) == 1
    assert "No organizational evidence" in knowledge[0].text


def test_render_assertions_never_blends_categories():
    parsed_assertions = [
        v.Assertion("knowledge", "K1"),
        v.Assertion("reasoning", "R1"),
        v.Assertion("uncertainty", "U1"),
    ]
    text = v.render_assertions(parsed_assertions)
    k_idx, r_idx, u_idx = text.index("K1"), text.index("R1"), text.index("U1")
    assert k_idx < r_idx < u_idx


def main():
    tests = [obj for name, obj in list(globals().items()) if name.startswith("test_") and callable(obj)]
    failures = 0
    for test in tests:
        try:
            test()
            print(f"PASS  {test.__name__}")
        except AssertionError as exc:
            failures += 1
            print(f"FAIL  {test.__name__}: {exc}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed.")
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
