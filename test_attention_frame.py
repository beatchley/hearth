"""
Phase 7a — deterministic tests for hearth_attention_frame.py (the store
mechanics) and the pronoun-continuity helper in hearth_ask.py. No DB, no
network, no Gemini — synthetic inputs only, following the pattern in
test_assertion_validation.py (plain test_*() functions + bare assert,
collected and run by main()).

Run: venv/bin/python3 test_attention_frame.py
"""

import time

import hearth_ask
import hearth_attention_frame as af


# ---------------------------------------------------------------------------
# Store mechanics
# ---------------------------------------------------------------------------

def test_get_frame_missing_session_returns_none():
    assert af.get_frame("TAF_no_such_session") is None
    assert af.get_frame(None) is None
    assert af.get_frame("") is None


def test_get_or_create_frame_creates_once():
    sid = "TAF_create_once"
    af.clear_frame(sid)
    f1 = af.get_or_create_frame(sid)
    f2 = af.get_or_create_frame(sid)
    assert f1 is f2, "second call must return the same live frame, not a new one"
    assert f1.session_id == sid
    assert f1.focused_entity_id is None
    af.clear_frame(sid)


def test_frame_state_round_trips_through_get_frame():
    sid = "TAF_roundtrip"
    af.clear_frame(sid)
    f = af.get_or_create_frame(sid)
    f.focused_entity_id = 99
    f.focused_entity_name = "Zorion"
    f.last_evidence = {"get_recent_episodes": [{"id": 1}]}
    again = af.get_frame(sid)
    assert again is not None
    assert again.focused_entity_id == 99
    assert again.focused_entity_name == "Zorion"
    assert again.last_evidence == {"get_recent_episodes": [{"id": 1}]}
    af.clear_frame(sid)


def test_clear_frame_removes_it_and_is_idempotent():
    sid = "TAF_clear"
    af.get_or_create_frame(sid)
    assert af.get_frame(sid) is not None
    af.clear_frame(sid)
    assert af.get_frame(sid) is None
    af.clear_frame(sid)  # must not raise on an already-gone session
    af.clear_frame("TAF_never_existed")  # must not raise on a never-created one


def test_two_sessions_never_share_state():
    sid_a, sid_b = "TAF_isolation_a", "TAF_isolation_b"
    af.clear_frame(sid_a)
    af.clear_frame(sid_b)
    fa = af.get_or_create_frame(sid_a)
    fb = af.get_or_create_frame(sid_b)
    fa.focused_entity_name = "Alpha"
    fb.focused_entity_name = "Bravo"
    assert af.get_frame(sid_a).focused_entity_name == "Alpha"
    assert af.get_frame(sid_b).focused_entity_name == "Bravo"
    af.clear_frame(sid_a)
    af.clear_frame(sid_b)


def test_idle_timeout_expires_a_frame():
    sid = "TAF_idle_expiry"
    af.clear_frame(sid)
    f = af.get_or_create_frame(sid)
    f.focused_entity_id = 7
    # Simulate 30+ minutes of inactivity without a real sleep.
    f.last_active_at -= (af.IDLE_TIMEOUT_SECONDS + 1)
    assert af.get_frame(sid) is None, "an idle-expired frame must not be returned"


def test_idle_timeout_boundary_not_yet_expired():
    sid = "TAF_idle_boundary"
    af.clear_frame(sid)
    f = af.get_or_create_frame(sid)
    f.last_active_at -= (af.IDLE_TIMEOUT_SECONDS - 5)  # just inside the window
    assert af.get_frame(sid) is not None
    af.clear_frame(sid)


def test_get_or_create_on_expired_session_starts_fresh_not_stale():
    sid = "TAF_fresh_after_expiry"
    af.clear_frame(sid)
    f = af.get_or_create_frame(sid)
    f.focused_entity_id = 123
    f.focused_entity_name = "StaleBuilding"
    f.last_active_at -= (af.IDLE_TIMEOUT_SECONDS + 1)
    fresh = af.get_or_create_frame(sid)
    assert fresh.focused_entity_id is None, "must not inherit the expired frame's focus"
    assert fresh.focused_entity_name is None
    af.clear_frame(sid)


def test_get_or_create_frame_touches_activity():
    sid = "TAF_touch"
    af.clear_frame(sid)
    f = af.get_or_create_frame(sid)
    f.last_active_at -= 100  # simulate some idle time, well under the timeout
    stale_touch = f.last_active_at
    af.get_or_create_frame(sid)  # every fetch-for-use counts as activity
    assert f.last_active_at > stale_touch
    af.clear_frame(sid)


def test_frame_count_reflects_purges():
    sid = "TAF_count"
    af.clear_frame(sid)
    before = af.frame_count()
    f = af.get_or_create_frame(sid)
    assert af.frame_count() == before + 1
    f.last_active_at -= (af.IDLE_TIMEOUT_SECONDS + 1)
    assert af.frame_count() == before  # purged as a side effect of counting


# ---------------------------------------------------------------------------
# Pronoun continuity helper (hearth_ask._apply_attention_frame_pronouns) —
# pure text transformation, no DB/Gemini involved.
# ---------------------------------------------------------------------------

def test_pronoun_substitution_no_frame_is_noop():
    text = "What is connected to him?"
    assert hearth_ask._apply_attention_frame_pronouns(text, None) == text


def test_pronoun_substitution_frame_with_no_focus_is_noop():
    f = af.AttentionFrame(session_id="TAF_pronoun_nofocus")
    text = "What is connected to him?"
    assert hearth_ask._apply_attention_frame_pronouns(text, f) == text


def test_pronoun_substitution_no_pronoun_is_noop():
    f = af.AttentionFrame(session_id="TAF_pronoun_none", focused_entity_name="Ethan")
    text = "Tell me about Sarah"
    assert hearth_ask._apply_attention_frame_pronouns(text, f) == text


def test_pronoun_substitution_replaces_he_him_his():
    f = af.AttentionFrame(session_id="TAF_pronoun_he", focused_entity_name="Ethan")
    assert hearth_ask._apply_attention_frame_pronouns("What is connected to him?", f) == "What is connected to Ethan?"
    assert hearth_ask._apply_attention_frame_pronouns("Tell me about his coaching relationship", f) == \
        "Tell me about Ethan coaching relationship"
    assert hearth_ask._apply_attention_frame_pronouns("Does he need attention?", f) == "Does Ethan need attention?"


def test_pronoun_substitution_replaces_she_her_they():
    f = af.AttentionFrame(session_id="TAF_pronoun_she", focused_entity_name="Sarah")
    assert hearth_ask._apply_attention_frame_pronouns("Tell me about her", f) == "Tell me about Sarah"
    f2 = af.AttentionFrame(session_id="TAF_pronoun_they", focused_entity_name="Frogman")
    assert hearth_ask._apply_attention_frame_pronouns("What is connected to them?", f2) == \
        "What is connected to Frogman?"


def test_pronoun_substitution_does_not_touch_explicit_names():
    f = af.AttentionFrame(session_id="TAF_pronoun_explicit", focused_entity_name="Ethan")
    text = "Tell me about Sarah"
    # No pronoun present, so the focused Building (Ethan) must never be
    # injected over an explicitly-named different Building.
    assert hearth_ask._apply_attention_frame_pronouns(text, f) == text


def test_pronoun_substitution_case_insensitive():
    f = af.AttentionFrame(session_id="TAF_pronoun_case", focused_entity_name="Ethan")
    assert hearth_ask._apply_attention_frame_pronouns("What about HE?", f) == "What about Ethan?"


def main():
    tests = [obj for name, obj in sorted(globals().items()) if name.startswith("test_") and callable(obj)]
    failures = 0
    for test in tests:
        try:
            test()
            print(f"PASS  {test.__name__}")
        except AssertionError as exc:
            failures += 1
            print(f"FAIL  {test.__name__}: {exc}")
        except Exception as exc:
            failures += 1
            print(f"ERROR {test.__name__}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed.")
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
