"""
Phase 4 scenario proof — end-to-end tests for the manager-advice cognitive
path (hearth_manager_advice.py), run through the real hearth_ask.answer_question()
entry point, against a real (test-seeded) hearth_memory.db and a real Gemini
client. Not mocked/stubbed — this is the actual mechanism, actual data,
actual model calls.

Seeds realistic data under a MARKER-suffixed set of entities, prints full,
unabridged output for every scenario, then cleans up every row it created.

Run: venv/bin/python3 test_manager_advice_scenario.py
"""

import datetime as _dt
import os
import sys

from dotenv import load_dotenv

load_dotenv()

import hearth_ask
import hearth_manager_advice
import hearth_memory
import hearth_worldview

MARKER = "MGRADVICE_TEST"
ETHAN_NAME = "Ethan"          # literal name required by the scenario question text
COACH_NAME = f"Sarah{MARKER}"
DUP_NAME = f"Marcus{MARKER}"
GHOST_NAME = "Zorionclaude"  # clean proper-noun token, deliberately never created in the DB


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
    if result.plan is not None:
        print("plan:")
        print(f"  goal:      {result.plan.get('goal')}")
        print(f"  known:     {result.plan.get('known')}")
        print(f"  to_verify: {result.plan.get('to_verify')}")
    print("answer:")
    print(result.answer)
    print("=" * 78)


def main():
    gemini_client = _build_gemini_client()
    conn = hearth_memory.get_memory_connection()
    hearth_memory.init_tables(conn)
    hearth_worldview.ensure_worldview_tables(conn)

    # Idempotency guard: remove any leftover rows from a previous interrupted
    # run before seeding fresh ones.
    leftover_ids = [
        r[0] for r in conn.execute(
            "SELECT id FROM hearth_entities WHERE display_name = ? OR display_name LIKE ?;",
            (ETHAN_NAME, f"%{MARKER}%"),
        ).fetchall()
    ]
    for eid in leftover_ids:
        conn.execute("DELETE FROM hearth_worldview_changes WHERE subject_type='entity' AND subject_id=?;", (eid,))
        conn.execute("DELETE FROM hearth_worldview_beliefs WHERE subject_type='entity' AND subject_id=?;", (eid,))
        conn.execute("DELETE FROM hearth_worldview_uncertainties WHERE subject_type='entity' AND subject_id=?;", (eid,))
        conn.execute("DELETE FROM hearth_relationships WHERE entity_id_1=? OR entity_id_2=?;", (eid, eid))
        conn.execute("DELETE FROM hearth_episodes WHERE entity_id=?;", (eid,))
    if leftover_ids:
        conn.execute(
            f"DELETE FROM hearth_entities WHERE id IN ({','.join('?' * len(leftover_ids))});", leftover_ids,
        )
    conn.commit()

    created_entity_ids = []
    try:
        # --- Seed: Ethan, a creator who has gone quiet ------------------------
        now = _now()
        cur = conn.execute(
            "INSERT INTO hearth_entities (display_name, entity_type, summary, created_at)"
            " VALUES (?, 'person', ?, ?);",
            (ETHAN_NAME, f"Livestreamer, joined the program focused on variety gaming content. [{MARKER}]", now),
        )
        ethan_id = cur.lastrowid
        created_entity_ids.append(ethan_id)

        cur = conn.execute(
            "INSERT INTO hearth_entities (display_name, entity_type, summary, created_at)"
            " VALUES (?, 'person', ?, ?);",
            (COACH_NAME, f"Ethan's assigned coach. [{MARKER}]", now),
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
        conn.commit()

        # Episode history: was active, then went quiet.
        old_time = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=35)).isoformat()
        recent_time = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=3)).isoformat()
        eid1, _ = hearth_memory.create_episode(
            conn, ethan_id, "checkin_feedback_waiting",
            description=f"Check-in feedback waiting, resolved same week. [{MARKER}]", severity="low",
        )
        conn.execute(
            "UPDATE hearth_episodes SET observed_at = ?, resolved = 1, resolved_at = ? WHERE id = ?;",
            (old_time, old_time, eid1),
        )
        eid2, _ = hearth_memory.create_episode(
            conn, ethan_id, "creator_quiet",
            description=f"No training or community activity observed in 12 days. [{MARKER}]", severity="medium",
        )
        conn.execute("UPDATE hearth_episodes SET observed_at = ? WHERE id = ?;", (recent_time, eid2))
        conn.commit()

        # Worldview: a watched change tracking the quiet trend, plus a belief.
        hearth_worldview.record_change(
            conn, subject_type="entity", subject_id=ethan_id,
            change_text=f"Activity level dropped from regular to sparse over the last two weeks. [{MARKER}]",
            previous_state="active", current_state="quiet", direction="worsening", confidence=0.6,
        )
        hearth_worldview.add_belief(
            conn, subject_type="entity", subject_id=ethan_id, belief_type="engagement_momentum",
            belief_text=f"Engagement momentum has been slowing over the last several weeks. [{MARKER}]",
            confidence=0.55,
        )
        # Mirrors the actual production bug verbatim: hearth_soul.py's
        # _upsert_responsiveness_belief() writes belief_text as
        # f"Entity {entity} has shown episodes resolving, suggesting
        # responsiveness to outreach." — a raw numeric entity_id embedded in
        # stored free text, unlike _upsert_engagement_momentum_belief() above
        # which correctly resolves display_name before writing. This belief
        # exercises the rendering-time fix (_resolve_entity_id_mentions()).
        hearth_worldview.add_belief(
            conn, subject_type="entity", subject_id=ethan_id, belief_type="responsiveness",
            belief_text=(
                f"Entity {ethan_id} has shown episodes resolving, suggesting responsiveness "
                f"to outreach. [{MARKER}]"
            ),
            confidence=0.6,
        )
        conn.commit()

        # --- Seed: ambiguous duplicate-name pair -------------------------------
        cur = conn.execute(
            "INSERT INTO hearth_entities (display_name, entity_type, created_at) VALUES (?, 'person', ?);",
            (DUP_NAME, now),
        )
        dup1_id = cur.lastrowid
        created_entity_ids.append(dup1_id)
        cur = conn.execute(
            "INSERT INTO hearth_entities (display_name, entity_type, created_at) VALUES (?, 'person', ?);",
            (DUP_NAME, now),
        )
        dup2_id = cur.lastrowid
        created_entity_ids.append(dup2_id)
        conn.commit()

        # =======================================================================
        print(f"\n\n{'#' * 78}\n# SCENARIO 1 (PRIMARY): Toxie/Ethan — real advice-seeking question\n{'#' * 78}")
        result = hearth_ask.answer_question(
            "I noticed Ethan has not been going live much lately and I was thinking "
            "about reaching out to him. What do you think?",
            memory_conn=conn, gemini_client=gemini_client,
        )
        _print_result("Scenario 1: Toxie/Ethan (primary scenario)", result)
        assert result.status == "success", f"expected success, got {result.status}"
        assert result.entity_id == ethan_id
        assert result.plan is not None, "expected the retrieval plan to be present on the response"
        assert isinstance(result.plan, dict)
        for key in ("goal", "known", "to_verify"):
            assert result.plan.get(key), f"expected non-empty plan.{key}, got {result.plan.get(key)!r}"
        # Best-effort: if the model's plan happened to select get_active_beliefs
        # this round, confirm the raw-entity-ID belief above didn't leak here
        # too. Scenario 7 below forces this tool so the check is guaranteed,
        # not left to chance.
        assert f"Entity {ethan_id}" not in result.answer, (
            f"raw entity ID leaked into the primary scenario's response: {result.answer!r}"
        )

        # =======================================================================
        print(f"\n\n{'#' * 78}\n# SCENARIO 2: entity not found\n{'#' * 78}")
        result = hearth_ask.answer_question(
            f"I noticed {GHOST_NAME} has not been going live much lately and I was "
            "thinking about reaching out. What do you think?",
            memory_conn=conn, gemini_client=gemini_client,
        )
        _print_result("Scenario 2: entity not found", result)
        assert result.status == "not_found", f"expected not_found, got {result.status}: {result.answer}"

        # =======================================================================
        print(f"\n\n{'#' * 78}\n# SCENARIO 3: ambiguous name matching multiple Buildings\n{'#' * 78}")
        result = hearth_ask.answer_question(
            f"I noticed {DUP_NAME} hasn't been very active lately — what do you think, "
            "should I reach out?",
            memory_conn=conn, gemini_client=gemini_client,
        )
        _print_result("Scenario 3: ambiguous entity", result)
        assert result.status == "ambiguous", f"expected ambiguous, got {result.status}: {result.answer}"
        assert DUP_NAME in result.answer

        # =======================================================================
        print(f"\n\n{'#' * 78}\n# SCENARIO 4a: existing fast path still handles its own case\n{'#' * 78}")
        result = hearth_ask.answer_question(
            f"Tell me about {ETHAN_NAME}", memory_conn=conn, gemini_client=gemini_client,
        )
        _print_result("Scenario 4a: fast path (tell_me_about_entity) unaffected", result)
        assert result.status == "success"
        assert result.entity_id == ethan_id

        # =======================================================================
        print(f"\n\n{'#' * 78}\n# SCENARIO 4b: ordinary conversation, no advice-seeking intent\n{'#' * 78}")
        result = hearth_ask.answer_question(
            f"{ETHAN_NAME} is one of my favorite creators, his content is really funny.",
            memory_conn=conn, gemini_client=gemini_client,
        )
        _print_result("Scenario 4b: no advice-seeking intent", result)
        assert result.status == "unsupported", f"expected unsupported, got {result.status}: {result.answer}"

        # =======================================================================
        print(f"\n\n{'#' * 78}\n# SCENARIO 5: nonessential tool deliberately failing\n{'#' * 78}")
        original_fn = hearth_manager_advice.TOOL_REGISTRY["get_active_beliefs"]["fn"]

        def _exploding_get_active_beliefs(entity_id):
            return {"status": "error", "data": None, "error": "simulated failure for fail-closed test"}

        hearth_manager_advice.TOOL_REGISTRY["get_active_beliefs"]["fn"] = _exploding_get_active_beliefs
        try:
            # Force this tool into the plan deterministically so the failure is
            # actually exercised regardless of what the model would have chosen.
            original_plan_fn = hearth_manager_advice._get_plan_and_tool_selection

            def _forced_plan(question_text, display_name, gemini_client):
                plan, _tools, source = original_plan_fn(question_text, display_name, gemini_client)
                return plan, ["get_active_beliefs", "get_recent_episodes"], source

            hearth_manager_advice._get_plan_and_tool_selection = _forced_plan
            try:
                result = hearth_ask.answer_question(
                    "I noticed Ethan has not been going live much lately and I was "
                    "thinking about reaching out to him. What do you think?",
                    memory_conn=conn, gemini_client=gemini_client,
                )
            finally:
                hearth_manager_advice._get_plan_and_tool_selection = original_plan_fn
        finally:
            hearth_manager_advice.TOOL_REGISTRY["get_active_beliefs"]["fn"] = original_fn

        _print_result("Scenario 5: nonessential tool failure (get_active_beliefs)", result)
        assert result.status == "success", f"expected success (degraded, not blocked), got {result.status}"
        assert "beliefs" in result.source_summary.lower() or "active beliefs" in result.source_summary.lower()

        # =======================================================================
        print(f"\n\n{'#' * 78}\n# SCENARIO 6: essential tool (primary evidence source) deliberately failing\n{'#' * 78}")
        # run_manager_advice_path()'s Step 2 calls get_building_context(...)
        # by its module-level name directly (not via TOOL_REGISTRY), so both
        # the module attribute and the registry entry must be patched — and,
        # critically, both must be restored, or every scenario run after this
        # one silently keeps using the broken stub.
        original_context_module_fn = hearth_manager_advice.get_building_context
        original_context_registry_fn = hearth_manager_advice.TOOL_REGISTRY["get_building_context"]["fn"]

        def _exploding_get_building_context(entity_id):
            return {"status": "error", "data": None, "error": "simulated traversal outage"}

        hearth_manager_advice.get_building_context = _exploding_get_building_context
        hearth_manager_advice.TOOL_REGISTRY["get_building_context"]["fn"] = _exploding_get_building_context
        try:
            result = hearth_ask.answer_question(
                "I noticed Ethan has not been going live much lately and I was "
                "thinking about reaching out to him. What do you think?",
                memory_conn=conn, gemini_client=gemini_client,
            )
        finally:
            hearth_manager_advice.get_building_context = original_context_module_fn
            hearth_manager_advice.TOOL_REGISTRY["get_building_context"]["fn"] = original_context_registry_fn

        _print_result("Scenario 6: essential tool failure (get_building_context)", result)
        assert result.status == "error", f"expected error (fail-closed), got {result.status}"
        assert "don't want to guess" in result.answer.lower() or "couldn't retrieve enough" in result.answer.lower()

        # =======================================================================
        print(f"\n\n{'#' * 78}\n# SCENARIO 7: raw entity-ID leak in stored belief text must not reach the response\n{'#' * 78}")
        # Forces get_active_beliefs into the plan deterministically, so this
        # is guaranteed to exercise the responsiveness belief seeded above
        # (whose belief_text mirrors hearth_soul.py's actual
        # f"Entity {entity} has shown episodes resolving..." bug verbatim),
        # regardless of what the model would have chosen on its own.
        original_plan_fn = hearth_manager_advice._get_plan_and_tool_selection

        def _forced_beliefs_plan(question_text, display_name, gemini_client):
            plan, _tools, source = original_plan_fn(question_text, display_name, gemini_client)
            return plan, ["get_active_beliefs"], source

        hearth_manager_advice._get_plan_and_tool_selection = _forced_beliefs_plan
        try:
            result = hearth_ask.answer_question(
                "I noticed Ethan has not been going live much lately and I was "
                "thinking about reaching out to him. What do you think?",
                memory_conn=conn, gemini_client=gemini_client,
            )
        finally:
            hearth_manager_advice._get_plan_and_tool_selection = original_plan_fn

        _print_result("Scenario 7: raw entity-ID leak must resolve to display name", result)
        assert result.status == "success", f"expected success, got {result.status}"
        assert f"Entity {ethan_id}" not in result.answer, (
            f"raw entity ID 'Entity {ethan_id}' leaked into the rendered response: {result.answer!r}"
        )
        assert "Ethan" in result.answer, "expected the resolved display name 'Ethan' to appear instead"

        # =======================================================================
        print(f"\n\n{'#' * 78}\n# SCENARIO 8: outer-quote-wrapped input reaches the manager-advice path unaffected\n{'#' * 78}")
        # hearth_ask.answer_question() now normalizes (incl. outer-quote
        # stripping) once and passes that same normalized text into
        # hearth_manager_advice.answer_manager_advice_question() — this is
        # the primary scenario's exact question text, wrapped in a
        # literal outer quote pair the way a browser/OS might produce one.
        # A direct check (see completion report) already confirmed
        # check_entity_mentions() itself tolerates outer quotes even without
        # this fix — this proves the full pipeline (gate + cognitive path)
        # still reaches the same "success" outcome end to end now that the
        # normalized text flows all the way through, not just at the gate.
        result = hearth_ask.answer_question(
            '"I noticed Ethan has not been going live much lately and I was '
            'thinking about reaching out to him. What do you think?"',
            memory_conn=conn, gemini_client=gemini_client,
        )
        _print_result("Scenario 8: quote-wrapped input, manager-advice path", result)
        assert result.status == "success", f"expected success, got {result.status}: {result.answer}"
        assert result.entity_id == ethan_id
        assert result.plan is not None

        print("\n\nAll manager-advice scenario assertions passed.")

    finally:
        print("\nCleanup — removing all test-seeded rows")
        for eid in created_entity_ids:
            conn.execute("DELETE FROM hearth_worldview_changes WHERE subject_type='entity' AND subject_id=?;", (eid,))
            conn.execute("DELETE FROM hearth_worldview_beliefs WHERE subject_type='entity' AND subject_id=?;", (eid,))
            conn.execute("DELETE FROM hearth_worldview_uncertainties WHERE subject_type='entity' AND subject_id=?;", (eid,))
        conn.execute(f"DELETE FROM hearth_episodes WHERE description LIKE '%{MARKER}%';")
        conn.execute(
            "DELETE FROM hearth_relationships WHERE source = 'pathway_sync' AND relationship_type = 'coached_by'"
            " AND (entity_id_1 IN (%s) OR entity_id_2 IN (%s));" % (
                ",".join(str(i) for i in created_entity_ids) or "-1",
                ",".join(str(i) for i in created_entity_ids) or "-1",
            )
        )
        for eid in created_entity_ids:
            conn.execute("DELETE FROM hearth_entities WHERE id = ?;", (eid,))
        conn.commit()
        remaining = conn.execute(
            "SELECT COUNT(*) FROM hearth_entities WHERE display_name = ? OR display_name LIKE ?;",
            (ETHAN_NAME, f"%{MARKER}%"),
        ).fetchone()[0]
        print(f"Remaining test-seeded entities after cleanup: {remaining}")
        conn.close()
        print("Cleanup complete.")


if __name__ == "__main__":
    main()
