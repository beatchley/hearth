"""
Phase 7a scenario proof — end-to-end tests for session-scoped
conversational continuity (hearth_attention_frame.py), run through the
real hearth_ask.answer_question() entry point, against a real
(test-seeded) hearth_memory.db and a real Gemini client. Not mocked — the
actual mechanism, actual data, actual model calls, mirroring
test_manager_advice_scenario.py's convention.

Covers every continuity behavior named in the Phase 7a brief: a Building
follow-up reference, a follow-up using previously retrieved evidence,
changing subjects mid-conversation, explicit conversation termination,
page refresh / session reset, idle-timeout expiration, and permission
checks re-running on every turn (not just the first).

Run: venv/bin/python3 test_attention_frame_scenario.py
"""

import datetime as _dt
import os
import sys

from dotenv import load_dotenv

load_dotenv()

import hearth_ask
import hearth_attention_frame as af
import hearth_manager_advice
import hearth_memory

MARKER = "ATTNFRAME_TEST"
ETHAN_NAME = f"Ethan{MARKER}"
COACH_NAME = f"Coach{MARKER}"
SARAH_NAME = f"Sarah{MARKER}"


def _build_gemini_client():
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("GEMINI_API_KEY not set — cannot run real end-to-end test.")
        sys.exit(1)
    from google import genai
    return genai.Client(api_key=api_key)


def _now():
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _print_result(label, result):
    print(f"\n{'=' * 78}\n{label}\n{'=' * 78}")
    print(f"status: {result.status}")
    print(f"entity_id: {result.entity_id}")
    print(f"source_summary: {result.source_summary}")
    print("answer:")
    print(result.answer[:500])
    print("=" * 78)


def main():
    gemini_client = _build_gemini_client()
    conn = hearth_memory.get_memory_connection()
    hearth_memory.init_tables(conn)

    # Idempotency guard.
    leftover_ids = [
        r[0] for r in conn.execute(
            "SELECT id FROM hearth_entities WHERE display_name LIKE ?;", (f"%{MARKER}%",)
        ).fetchall()
    ]
    for eid in leftover_ids:
        conn.execute("DELETE FROM hearth_relationships WHERE entity_id_1=? OR entity_id_2=?;", (eid, eid))
        conn.execute("DELETE FROM hearth_episodes WHERE entity_id=?;", (eid,))
    if leftover_ids:
        conn.execute(
            f"DELETE FROM hearth_entities WHERE id IN ({','.join('?' * len(leftover_ids))});", leftover_ids,
        )
    conn.commit()

    created_entity_ids = []
    session_ids_to_clear = []
    try:
        now = _now()
        cur = conn.execute(
            "INSERT INTO hearth_entities (display_name, entity_type, summary, created_at)"
            " VALUES (?, 'person', ?, ?);",
            (ETHAN_NAME, f"Livestreamer, variety gaming content. [{MARKER}]", now),
        )
        ethan_id = cur.lastrowid
        created_entity_ids.append(ethan_id)

        cur = conn.execute(
            "INSERT INTO hearth_entities (display_name, entity_type, summary, created_at)"
            " VALUES (?, 'person', ?, ?);",
            (COACH_NAME, f"{ETHAN_NAME}'s assigned coach. [{MARKER}]", now),
        )
        coach_id = cur.lastrowid
        created_entity_ids.append(coach_id)

        conn.execute(
            "INSERT INTO hearth_relationships"
            " (entity_id_1, entity_id_2, relationship_type, active, confidence,"
            "  first_observed_at, last_observed_at, origin, source, status)"
            " VALUES (?, ?, 'coached_by', 1, 0.95, ?, ?, 'coach_assignment', 'pathway_sync', 'active');",
            (ethan_id, coach_id, now, now),
        )

        cur = conn.execute(
            "INSERT INTO hearth_entities (display_name, entity_type, summary, created_at)"
            " VALUES (?, 'person', ?, ?);",
            (SARAH_NAME, f"A second, unrelated creator. [{MARKER}]", now),
        )
        sarah_id = cur.lastrowid
        created_entity_ids.append(sarah_id)
        conn.commit()

        recent_time = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=3)).isoformat()
        eid2, _ = hearth_memory.create_episode(
            conn, ethan_id, "creator_quiet",
            description=f"No training or community activity observed in 12 days. [{MARKER}]", severity="medium",
        )
        conn.execute("UPDATE hearth_episodes SET observed_at = ? WHERE id = ?;", (recent_time, eid2))
        conn.commit()

        # =====================================================================
        print(f"\n\n{'#' * 78}\n# SCENARIO 1: Building follow-up reference (fixed-pattern routes)\n{'#' * 78}")
        sid1 = f"{MARKER}_session_1"
        session_ids_to_clear.append(sid1)
        frame1 = af.get_or_create_frame(sid1)

        r1 = hearth_ask.answer_question(
            f"Tell me about {ETHAN_NAME}",
            memory_conn=conn, gemini_client=gemini_client, actor_role="manager", attention_frame=frame1,
        )
        _print_result("Turn 1: Tell me about Ethan", r1)
        assert r1.status == "success"
        assert r1.entity_id == ethan_id
        assert frame1.focused_entity_id == ethan_id
        assert frame1.focused_entity_name == ETHAN_NAME

        r2 = hearth_ask.answer_question(
            "What is connected to him?",  # no name — pronoun only
            memory_conn=conn, gemini_client=gemini_client, actor_role="manager", attention_frame=frame1,
        )
        _print_result("Turn 2: What is connected to him? (pronoun, no name)", r2)
        assert r2.status == "success", f"pronoun follow-up should resolve via the frame: {r2.answer}"
        assert r2.entity_id == ethan_id
        assert COACH_NAME in r2.answer, "expected Ethan's coach to appear in the connected-to answer"

        # =====================================================================
        print(f"\n\n{'#' * 78}\n# SCENARIO 2: follow-up reusing previously retrieved evidence\n{'#' * 78}")
        sid2 = f"{MARKER}_session_2"
        session_ids_to_clear.append(sid2)
        frame2 = af.get_or_create_frame(sid2)

        # Force a deterministic plan/tool selection so evidence reuse is
        # exercised unconditionally, not left to the model's own choice.
        original_plan_fn = hearth_manager_advice._get_plan_and_tool_selection

        def _fixed_plan(question_text, display_name, gemini_client):
            plan = {
                "goal": f"Determine whether {display_name} needs outreach.",
                "known": f"{display_name} has been identified.",
                "to_verify": "Recent episodes and active beliefs.",
            }
            return plan, ["get_recent_episodes", "get_active_beliefs"], "test_fixed_plan"

        hearth_manager_advice._get_plan_and_tool_selection = _fixed_plan

        call_counts = {"get_recent_episodes": 0, "get_active_beliefs": 0}
        original_recent_episodes = hearth_manager_advice.TOOL_REGISTRY["get_recent_episodes"]["fn"]
        original_active_beliefs = hearth_manager_advice.TOOL_REGISTRY["get_active_beliefs"]["fn"]

        def _counting_recent_episodes(entity_id):
            call_counts["get_recent_episodes"] += 1
            return original_recent_episodes(entity_id)

        def _counting_active_beliefs(entity_id):
            call_counts["get_active_beliefs"] += 1
            return original_active_beliefs(entity_id)

        hearth_manager_advice.TOOL_REGISTRY["get_recent_episodes"]["fn"] = _counting_recent_episodes
        hearth_manager_advice.TOOL_REGISTRY["get_active_beliefs"]["fn"] = _counting_active_beliefs

        try:
            r3 = hearth_ask.answer_question(
                f"I noticed {ETHAN_NAME} has not been going live much lately and I was "
                "thinking about reaching out. What do you think?",
                memory_conn=conn, gemini_client=gemini_client, actor_role="manager", attention_frame=frame2,
            )
            _print_result("Turn 1: advice-seeking question about Ethan", r3)
            assert r3.status == "success", r3.answer
            assert r3.entity_id == ethan_id
            assert call_counts == {"get_recent_episodes": 1, "get_active_beliefs": 1}, call_counts
            assert frame2.last_evidence is not None
            assert set(frame2.last_evidence.keys()) >= {"get_recent_episodes", "get_active_beliefs"}

            r4 = hearth_ask.answer_question(
                "And what about his coaching relationship — any concerns there, given what you already know?",
                memory_conn=conn, gemini_client=gemini_client, actor_role="manager", attention_frame=frame2,
            )
            _print_result("Turn 2: pronoun follow-up, same Building — should reuse evidence", r4)
            assert r4.status == "success", r4.answer
            assert r4.entity_id == ethan_id
            # The two optional tools were NOT called again — reused from the frame.
            assert call_counts == {"get_recent_episodes": 1, "get_active_beliefs": 1}, (
                f"expected no new optional-tool calls on turn 2 (reuse expected), got {call_counts}"
            )
            print(f"Evidence reuse confirmed: optional-tool call counts unchanged across turns: {call_counts}")
        finally:
            hearth_manager_advice._get_plan_and_tool_selection = original_plan_fn
            hearth_manager_advice.TOOL_REGISTRY["get_recent_episodes"]["fn"] = original_recent_episodes
            hearth_manager_advice.TOOL_REGISTRY["get_active_beliefs"]["fn"] = original_active_beliefs

        # =====================================================================
        print(f"\n\n{'#' * 78}\n# SCENARIO 3: changing subjects mid-conversation\n{'#' * 78}")
        r5 = hearth_ask.answer_question(
            f"Tell me about {SARAH_NAME}",
            memory_conn=conn, gemini_client=gemini_client, actor_role="manager", attention_frame=frame2,
        )
        _print_result("Turn 3: explicit subject change to Sarah", r5)
        assert r5.status == "success"
        assert r5.entity_id == sarah_id
        assert frame2.focused_entity_id == sarah_id, "frame focus must follow the explicit subject change"
        assert frame2.focused_entity_name == SARAH_NAME
        assert COACH_NAME not in r5.answer, "Ethan's coach must never leak into Sarah's answer"

        # =====================================================================
        print(f"\n\n{'#' * 78}\n# SCENARIO 4: explicit conversation termination\n{'#' * 78}")
        af.clear_frame(sid1)
        assert af.get_frame(sid1) is None
        r6 = hearth_ask.answer_question(
            "What is connected to him?",
            memory_conn=conn, gemini_client=gemini_client, actor_role="manager",
            attention_frame=af.get_frame(sid1),  # None — conversation ended
        )
        _print_result("Turn after end-conversation: pronoun with no active frame", r6)
        assert r6.status in ("not_found", "unsupported"), (
            f"a pronoun with no frame and no prior context must not resolve to a Building, got {r6.status}"
        )

        # =====================================================================
        print(f"\n\n{'#' * 78}\n# SCENARIO 5: page refresh / session reset (fresh session id)\n{'#' * 78}")
        sid_refresh = f"{MARKER}_session_refresh"
        session_ids_to_clear.append(sid_refresh)
        fresh_frame = af.get_or_create_frame(sid_refresh)  # simulates a brand-new page load's session id
        assert fresh_frame.focused_entity_id is None
        r7 = hearth_ask.answer_question(
            "What is connected to him?",
            memory_conn=conn, gemini_client=gemini_client, actor_role="manager", attention_frame=fresh_frame,
        )
        _print_result("Turn on a fresh session: pronoun with no history", r7)
        assert r7.status in ("not_found", "unsupported"), (
            f"a fresh session must carry no continuity from any other session, got {r7.status}"
        )

        # =====================================================================
        print(f"\n\n{'#' * 78}\n# SCENARIO 6: idle-timeout expiration\n{'#' * 78}")
        sid_idle = f"{MARKER}_session_idle"
        session_ids_to_clear.append(sid_idle)
        idle_frame = af.get_or_create_frame(sid_idle)
        r8 = hearth_ask.answer_question(
            f"Tell me about {ETHAN_NAME}",
            memory_conn=conn, gemini_client=gemini_client, actor_role="manager", attention_frame=idle_frame,
        )
        assert r8.status == "success"
        assert af.get_frame(sid_idle).focused_entity_id == ethan_id
        # Simulate 30+ minutes of inactivity.
        idle_frame.last_active_at -= (af.IDLE_TIMEOUT_SECONDS + 1)
        assert af.get_frame(sid_idle) is None, "frame must be gone after idle timeout"
        r9 = hearth_ask.answer_question(
            "What is connected to him?",
            memory_conn=conn, gemini_client=gemini_client, actor_role="manager",
            attention_frame=af.get_frame(sid_idle),  # None post-expiry
        )
        _print_result("Turn after idle timeout: pronoun with an expired frame", r9)
        assert r9.status in ("not_found", "unsupported"), (
            f"an idle-expired frame must not still resolve the pronoun, got {r9.status}"
        )

        # =====================================================================
        print(f"\n\n{'#' * 78}\n# SCENARIO 7: authorization re-checked every turn, not just the first\n{'#' * 78}")
        sid_auth = f"{MARKER}_session_auth"
        session_ids_to_clear.append(sid_auth)
        auth_frame = af.get_or_create_frame(sid_auth)

        r10 = hearth_ask.answer_question(
            f"I noticed {ETHAN_NAME} has not been going live much lately and I was "
            "thinking about reaching out. What do you think?",
            memory_conn=conn, gemini_client=gemini_client, actor_role="manager", attention_frame=auth_frame,
        )
        _print_result("Turn 1 (authorized manager): should succeed and set focus", r10)
        assert r10.status == "success"
        assert auth_frame.focused_entity_id == ethan_id

        r11 = hearth_ask.answer_question(
            "And what does his coach think of his recent activity?",
            memory_conn=conn, gemini_client=gemini_client, actor_role="coach",  # NOT an authorized role
            attention_frame=auth_frame,
        )
        _print_result("Turn 2 (unauthorized coach, same frame/focus): must still be refused", r11)
        assert r11.status == "not_authorized", (
            f"an unauthorized actor_role must be refused every turn regardless of an active frame, got {r11.status}"
        )

        r12 = hearth_ask.answer_question(
            "And what does his coach think of his recent activity?",
            memory_conn=conn, gemini_client=gemini_client, actor_role=None,  # omitted entirely
            attention_frame=auth_frame,
        )
        _print_result("Turn 3 (omitted actor_role, same frame/focus): must still be refused", r12)
        assert r12.status == "not_authorized"

        print("\nAll Phase 7a Attention Frame scenario assertions passed.")
    finally:
        print("\nCleanup — removing all test rows and frames")
        for sid in session_ids_to_clear:
            af.clear_frame(sid)
        conn.execute("DELETE FROM hearth_episodes WHERE description LIKE ?;", (f"%{MARKER}%",))
        conn.execute("DELETE FROM hearth_relationships WHERE source = 'pathway_sync' AND origin = 'coach_assignment' AND entity_id_1 IN (SELECT id FROM hearth_entities WHERE display_name LIKE ?);", (f"%{MARKER}%",))
        conn.execute("DELETE FROM hearth_entities WHERE display_name LIKE ?;", (f"%{MARKER}%",))
        conn.commit()
        remaining = conn.execute(
            "SELECT COUNT(*) FROM hearth_entities WHERE display_name LIKE ?;", (f"%{MARKER}%",)
        ).fetchone()[0]
        print(f"  Remaining test entities after cleanup: {remaining}")
        conn.close()
        print("Scenario test complete.")


if __name__ == "__main__":
    main()
