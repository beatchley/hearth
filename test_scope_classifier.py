"""
Phase 8 — fast, deterministic unit tests for hearth_scope_classifier.py's
conservative-failure logic. No real Gemini call, no API key required — a
fake client returns canned JSON so every failure mode (missing client,
call failure, malformed JSON, invalid scope value, invalid confidence,
low-confidence demotion) is exercised exactly, every run.

Run: venv/bin/python3 test_scope_classifier.py
"""

import sys

import hearth_memory
import hearth_scope_classifier as sc

failures = []


def check(name, cond, msg=""):
    if not cond:
        failures.append(f"FAIL [{name}]" + (f": {msg}" if msg else ""))
        print(f"  FAIL: {name}" + (f" — {msg}" if msg else ""))
    else:
        print(f"  pass: {name}")


class _FakeModels:
    def __init__(self, text=None, raises=False):
        self._text = text
        self._raises = raises

    def generate_content(self, model, contents):
        if self._raises:
            raise RuntimeError("simulated Gemini outage")

        class _Resp:
            text = self._text
        return _Resp()


class _FakeClient:
    def __init__(self, text=None, raises=False):
        self.models = _FakeModels(text=text, raises=raises)


def main():
    conn = hearth_memory.get_memory_connection()
    hearth_memory.init_tables(conn)

    MARKER = "SCOPECLASSIFIER_TEST"
    import datetime as _dt
    now = _dt.datetime.now(_dt.timezone.utc).isoformat()
    entity_id = conn.execute(
        "INSERT INTO hearth_entities (display_name, entity_type, created_at) VALUES (?, 'person', ?);",
        (f"Marcus{MARKER}", now),
    ).lastrowid
    conn.commit()

    try:
        print("1. gemini_client=None: no signal -> uncertain_or_mixed")
        result = sc.classify_question_scope(conn, "What's the capital of Tennessee?", None)
        check("scope", result.scope == sc.SCOPE_UNCERTAIN_OR_MIXED, result.scope)
        check("confidence 0.0", result.confidence == 0.0)
        check("reason", result.reason == "gemini_unavailable")

        print("2. gemini_client=None: known Building mentioned -> organizational")
        result = sc.classify_question_scope(conn, f"Marcus{MARKER} is doing well", None)
        check("scope", result.scope == sc.SCOPE_ORGANIZATIONAL, result.scope)

        print("3. gemini_client=None: Attention Frame focus present, no explicit text mention -> still uncertain_or_mixed")
        # The Attention Frame's focus is informational context for the
        # model only (see hearth_scope_classifier.py's module docstring) —
        # it must never drive the deterministic no-client fallback, since a
        # stale/unrelated focus could otherwise force an unconnected
        # question into the organizational lane.
        class _FakeFrame:
            focused_entity_name = f"Marcus{MARKER}"
        result = sc.classify_question_scope(conn, "What do you think about that?", None, attention_frame=_FakeFrame())
        check("scope", result.scope == sc.SCOPE_UNCERTAIN_OR_MIXED, result.scope)

        print("4. Gemini call raises -> conservative fallback (no signal)")
        result = sc.classify_question_scope(conn, "What's 2+2?", _FakeClient(raises=True))
        check("scope", result.scope == sc.SCOPE_UNCERTAIN_OR_MIXED, result.scope)
        check("reason", result.reason == "gemini_call_failed")

        print("5. Malformed JSON -> conservative fallback")
        result = sc.classify_question_scope(conn, "What's 2+2?", _FakeClient(text="not json at all"))
        check("scope", result.scope == sc.SCOPE_UNCERTAIN_OR_MIXED, result.scope)
        check("reason", result.reason == "malformed_json")

        print("6. Valid JSON, invalid scope value -> conservative fallback")
        result = sc.classify_question_scope(
            conn, "What's 2+2?", _FakeClient(text='{"scope": "something_else", "confidence": 0.9}'),
        )
        check("scope", result.scope == sc.SCOPE_UNCERTAIN_OR_MIXED, result.scope)
        check("reason", result.reason == "invalid_scope_value")

        print("7. Valid scope, invalid confidence -> conservative fallback")
        result = sc.classify_question_scope(
            conn, "What's 2+2?", _FakeClient(text='{"scope": "general_knowledge", "confidence": "high"}'),
        )
        check("scope", result.scope == sc.SCOPE_UNCERTAIN_OR_MIXED, result.scope)
        check("reason", result.reason == "invalid_confidence_value")

        print("8. general_knowledge below confidence threshold -> demoted to uncertain_or_mixed")
        result = sc.classify_question_scope(
            conn, "What's 2+2?", _FakeClient(text='{"scope": "general_knowledge", "confidence": 0.3}'),
        )
        check("scope demoted", result.scope == sc.SCOPE_UNCERTAIN_OR_MIXED, result.scope)
        check("reason", result.reason == "low_confidence_general_knowledge_demoted")
        check("confidence preserved", result.confidence == 0.3)

        print("9. general_knowledge above threshold -> honored")
        result = sc.classify_question_scope(
            conn, "What's 2+2?", _FakeClient(text='{"scope": "general_knowledge", "confidence": 0.85}'),
        )
        check("scope", result.scope == sc.SCOPE_GENERAL_KNOWLEDGE, result.scope)
        check("reason", result.reason == "model")

        print("10. organizational below threshold, no signal -> demoted to uncertain_or_mixed")
        result = sc.classify_question_scope(
            conn, "What's 2+2?", _FakeClient(text='{"scope": "organizational", "confidence": 0.2}'),
        )
        check("scope demoted", result.scope == sc.SCOPE_UNCERTAIN_OR_MIXED, result.scope)

        print("11. organizational below threshold, WITH signal -> stays organizational")
        result = sc.classify_question_scope(
            conn, f"Marcus{MARKER} question", _FakeClient(text='{"scope": "organizational", "confidence": 0.2}'),
        )
        check("scope stays organizational", result.scope == sc.SCOPE_ORGANIZATIONAL, result.scope)

        print("12. uncertain_or_mixed from model, any confidence -> honored regardless")
        result = sc.classify_question_scope(
            conn, "ambiguous text", _FakeClient(text='{"scope": "uncertain_or_mixed", "confidence": 0.1}'),
        )
        check("scope", result.scope == sc.SCOPE_UNCERTAIN_OR_MIXED, result.scope)
        check("reason", result.reason == "model")

        print("13. confidence out of [0,1] range is clamped")
        result = sc.classify_question_scope(
            conn, "What's 2+2?", _FakeClient(text='{"scope": "general_knowledge", "confidence": 5.0}'),
        )
        check("confidence clamped to 1.0", result.confidence == 1.0)
        check("scope honored (was above threshold pre-clamp)", result.scope == sc.SCOPE_GENERAL_KNOWLEDGE)

        print("14. valid organizational, high confidence -> honored as-is")
        result = sc.classify_question_scope(
            conn, "What's 2+2?", _FakeClient(text='{"scope": "organizational", "confidence": 0.9}'),
        )
        check("scope", result.scope == sc.SCOPE_ORGANIZATIONAL, result.scope)
        check("reason", result.reason == "model")

        print("\nAll test_scope_classifier assertions evaluated.")
    finally:
        conn.execute("DELETE FROM hearth_entities WHERE id = ?;", (entity_id,))
        conn.commit()
        conn.close()

    print("\n" + ("=" * 60))
    if failures:
        print(f"{len(failures)} FAILURE(S):")
        for f in failures:
            print(f"  {f}")
        sys.exit(1)
    else:
        print("All test_scope_classifier assertions passed.")


if __name__ == "__main__":
    main()
