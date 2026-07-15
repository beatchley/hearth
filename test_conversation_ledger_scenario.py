"""
Phase 7b scenario proof — end-to-end tests for the Conversation Ledger
(hearth_conversation_ledger.py) as the Furniture Fact Extractor's eighth
observational source. Run through the real hearth_ask.answer_question()
entry point and the real per-record extractor pipeline
(hearth_fact_extractor._process_record), against a real (test-seeded)
hearth_memory.db and a real Gemini client — not mocked, mirroring
test_manager_advice_scenario.py / test_attention_frame_scenario.py's
convention.

Covers every scenario named in the Phase 7b brief's Testing section:
    1.  Simple self-facts staged and extracted into a Furniture proposal
    2.  Preferences/interests/traits extracted the same way
    3.  A conversation mentioning another Building stages and extracts
        a proposal for that Building via the existing entity-mention scan
    4.  Duplicate suppression (same fact staged twice -> one proposal)
    5.  Process-restart safety (a row already marked processed is never
        re-evaluated, and is still purged on the next pass)
    6.  Successful purge after extraction
    7.  No extraction from Hearth's own responses (only human text is ever
        staged, by construction)
    8.  No staging from the three fixed-pattern routes (confirmed via the
        ledger row count, not by omission)
    9.  The manager-advice path (the only source of eligible messages)
        stays inaccessible to unauthorized roles, and unauthorized actors'
        text is never staged either

Run: venv/bin/python3 test_conversation_ledger_scenario.py
"""

import os
import sys
import uuid

from dotenv import load_dotenv

load_dotenv()

import hearth_ask
import hearth_attention_frame as af
import hearth_conversation_ledger as ledger
import hearth_fact_extractor as fx
import hearth_furniture_proposals as fp
import hearth_manager_advice
import hearth_memory
import migrate_add_conversation_ledger_schema as migrate_ledger
import migrate_add_furniture_proposals_schema as migrate_fp

MARKER = "CONVLEDGER_TEST"
AUTHOR_USER_ID = 900_000_001
MARCUS_USER_ID = 900_000_002
MARCUS_NAME = f"Marcus{MARKER}"

failures = []


def check(name, cond, msg=""):
    if not cond:
        failures.append(f"FAIL [{name}]" + (f": {msg}" if msg else ""))
        print(f"  FAIL: {name}" + (f" — {msg}" if msg else ""))
    else:
        print(f"  pass: {name}")


def _build_gemini_client():
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("GEMINI_API_KEY not set — cannot run real end-to-end test.")
        sys.exit(1)
    from google import genai
    return genai.Client(api_key=api_key)


def _cleanup(conn):
    """Remove every MARKER-tagged row this test ever creates, in every
    table it touches. Called both before (idempotency guard for a leftover
    interrupted run) and after (real cleanup)."""
    entity_ids = [
        r[0] for r in conn.execute(
            "SELECT id FROM hearth_entities WHERE display_name LIKE ? OR user_id IN (?, ?);",
            (f"%{MARKER}%", AUTHOR_USER_ID, MARCUS_USER_ID),
        ).fetchall()
    ]
    for eid in entity_ids:
        conn.execute("DELETE FROM hearth_furniture_proposals WHERE entity_id = ?;", (eid,))
        conn.execute("DELETE FROM hearth_entity_furniture WHERE entity_id = ?;", (eid,))
        conn.execute("DELETE FROM hearth_relationships WHERE entity_id_1=? OR entity_id_2=?;", (eid, eid))
    if entity_ids:
        conn.execute(
            f"DELETE FROM hearth_entities WHERE id IN ({','.join('?' * len(entity_ids))});", entity_ids,
        )
    conn.execute(
        "DELETE FROM hearth_processed_sources WHERE source_type = ?;", (ledger.SOURCE_TYPE,)
    )
    conn.execute(
        "DELETE FROM hearth_conversation_ledger WHERE message_text LIKE ? OR author_user_id IN (?, ?);",
        (f"%{MARKER}%", AUTHOR_USER_ID, MARCUS_USER_ID),
    )
    conn.commit()


def main():
    gemini_client = _build_gemini_client()
    conn = hearth_memory.get_memory_connection()
    hearth_memory.init_tables(conn)
    migrate_fp.create_tables(conn)
    migrate_ledger.create_tables(conn)

    _cleanup(conn)  # idempotency guard against a prior interrupted run

    try:
        import datetime as _dt
        now = _dt.datetime.now(_dt.timezone.utc).isoformat()

        author_id = conn.execute(
            "INSERT INTO hearth_entities (display_name, entity_type, user_id, created_at)"
            " VALUES (?, 'person', ?, ?);",
            (f"AuthorPerson{MARKER}", AUTHOR_USER_ID, now),
        ).lastrowid
        marcus_id = conn.execute(
            "INSERT INTO hearth_entities (display_name, entity_type, user_id, created_at)"
            " VALUES (?, 'person', ?, ?);",
            (MARCUS_NAME, MARCUS_USER_ID, now),
        ).lastrowid
        conn.commit()

        # ---------------------------------------------------------------
        # 8/9. Routing- and authorization-based staging scope, via the real
        # hearth_ask.answer_question() entry point.
        # ---------------------------------------------------------------
        print("\n=== 8/9. Staging scope: routing + authorization, not content ===")

        before = ledger.count_entries(conn)
        hearth_ask.answer_question(
            f"Tell me about {MARCUS_NAME}", memory_conn=conn, gemini_client=gemini_client,
            actor_role="manager", actor_user_id=AUTHOR_USER_ID,
        )
        check("fixed-pattern 'tell me about' does not stage",
              ledger.count_entries(conn) == before)

        hearth_ask.answer_question(
            f"What is connected to {MARCUS_NAME}", memory_conn=conn, gemini_client=gemini_client,
            actor_role="manager", actor_user_id=AUTHOR_USER_ID,
        )
        check("fixed-pattern 'connected to' does not stage",
              ledger.count_entries(conn) == before)

        hearth_ask.answer_question(
            "Who needs attention today?", memory_conn=conn, gemini_client=gemini_client,
            actor_role="manager", actor_user_id=AUTHOR_USER_ID,
        )
        check("fixed-pattern 'needs attention today' does not stage",
              ledger.count_entries(conn) == before)

        # Unauthorized actor: even a plainly eligible-shaped message must
        # never be staged, matching hearth_manager_advice's own fail-closed
        # authorization check.
        check("hearth_manager_advice.is_actor_authorized('coach') is False",
              hearth_manager_advice.is_actor_authorized("coach") is False)
        check("hearth_manager_advice.is_actor_authorized(None) is False",
              hearth_manager_advice.is_actor_authorized(None) is False)
        for role in ("ceo", "manager", "it"):
            check(f"hearth_manager_advice.is_actor_authorized({role!r}) is True",
                  hearth_manager_advice.is_actor_authorized(role) is True)

        unauth_result = hearth_ask.answer_question(
            f"My favorite color is blue. [{MARKER}]", memory_conn=conn, gemini_client=gemini_client,
            actor_role="coach", actor_user_id=AUTHOR_USER_ID,
        )
        check("unauthorized actor gets not_authorized status",
              unauth_result.status == "not_authorized")
        check("unauthorized actor's message is never staged",
              ledger.count_entries(conn) == before)

        unauth_result_none = hearth_ask.answer_question(
            f"My favorite color is blue. [{MARKER}]", memory_conn=conn, gemini_client=gemini_client,
            actor_role=None, actor_user_id=AUTHOR_USER_ID,
        )
        check("omitted actor_role also gets not_authorized status",
              unauth_result_none.status == "not_authorized")
        check("omitted actor_role's message is never staged",
              ledger.count_entries(conn) == before)

        # ---------------------------------------------------------------
        # 1/7. Simple self-fact, authorized manager, no active session —
        # the "original question that enters the path" case. Also proves
        # only the human's text (never Hearth's answer) reaches staging.
        # ---------------------------------------------------------------
        print("\n=== 1/7. Self-fact staging (authorized, no active session) ===")
        self_fact_text = f"My favorite food is cheeseburgers and french fries. [{MARKER}]"
        result = hearth_ask.answer_question(
            self_fact_text, memory_conn=conn, gemini_client=gemini_client,
            actor_role="manager", actor_user_id=AUTHOR_USER_ID,
        )
        check("self-fact question routed as unsupported/advice-shaped (not a fixed pattern)",
              result.status in ("success", "not_found", "ambiguous", "error", "unsupported"))
        check("self-fact staged exactly once", ledger.count_entries(conn) == before + 1)

        row = conn.execute(
            "SELECT * FROM hearth_conversation_ledger WHERE message_text = ?;", (self_fact_text,)
        ).fetchone()
        check("staged row has the exact human text (never Hearth's answer text)",
              row is not None and row["message_text"] == self_fact_text)
        check("staged row records the author's Pathway user id",
              row["author_user_id"] == AUTHOR_USER_ID)
        check("staged row records the actor role",
              row["actor_role"] == "manager")

        # ---------------------------------------------------------------
        # 3. A conversation mentioning another Building.
        # ---------------------------------------------------------------
        print("\n=== 3. Conversation mentioning another Building ===")
        mention_text = f"{MARCUS_NAME} really enjoys woodworking on weekends. [{MARKER}]"
        hearth_ask.answer_question(
            mention_text, memory_conn=conn, gemini_client=gemini_client,
            actor_role="manager", actor_user_id=AUTHOR_USER_ID,
        )
        mention_row = conn.execute(
            "SELECT * FROM hearth_conversation_ledger WHERE message_text = ?;", (mention_text,)
        ).fetchone()
        check("Building-mention message staged", mention_row is not None)

        # ---------------------------------------------------------------
        # 9 (continued). Follow-up turns within an active Attention Frame
        # session are also eligible, even when the follow-up alone would
        # not pass hearth_manager_advice's own advice-seeking classifier.
        # ---------------------------------------------------------------
        print("\n=== Follow-up turns within an active session also stage ===")
        session_id = f"convledger-test-session-{uuid.uuid4().hex}"
        frame = af.get_or_create_frame(session_id)
        before_frame = ledger.count_entries(conn)
        turn1_text = f"I noticed {MARCUS_NAME} hasn't posted much lately, what do you think? [{MARKER}]"
        hearth_ask.answer_question(
            turn1_text, memory_conn=conn, gemini_client=gemini_client,
            actor_role="manager", actor_user_id=AUTHOR_USER_ID, attention_frame=frame,
        )
        check("first (advice-seeking) turn in a session stages",
              ledger.count_entries(conn) == before_frame + 1)

        turn2_text = f"By the way, my favorite hobby is chess. [{MARKER}]"
        hearth_ask.answer_question(
            turn2_text, memory_conn=conn, gemini_client=gemini_client,
            actor_role="manager", actor_user_id=AUTHOR_USER_ID, attention_frame=frame,
        )
        check("plain-statement follow-up turn (not advice-seeking on its own) still stages",
              ledger.count_entries(conn) == before_frame + 2)
        af.clear_frame(session_id)

        # ---------------------------------------------------------------
        # 5/6. Process-restart safety + purge, exercised directly through
        # the same functions hearth_fact_extractor.run_batch()'s
        # Conversation Ledger block calls, scoped to just our rows (never
        # invoking run_batch() itself, which would also touch unrelated
        # real Pathway sources).
        # ---------------------------------------------------------------
        print("\n=== 5/6. Restart safety + purge, via the extractor's own per-record path ===")

        def _drain(marker_predicate):
            """Mirror run_batch()'s Conversation Ledger block exactly, but
            only for rows matching marker_predicate — never touches any
            other pending row a concurrent real batch might have staged."""
            processed_ids = []
            for r in ledger.get_pending_entries(conn, limit=1000):
                if not marker_predicate(r):
                    continue
                summary = fx._empty_source_summary()
                fx._process_record(
                    conn, gemini_client, ledger.SOURCE_TYPE, r["id"], r["message_text"],
                    r["author_user_id"], r["author_user_id"], summary, False, print,
                )
                ledger.delete_entry(conn, r["id"])
                processed_ids.append(r["id"])
            return processed_ids

        def _is_ours(r):
            return MARKER in (r["message_text"] or "")

        pending_before_drain = {r["id"] for r in ledger.get_pending_entries(conn, limit=1000) if _is_ours(r)}
        check("all MARKER rows staged so far are still present pre-drain (nothing lost)",
              len(pending_before_drain) >= 4)

        drained = _drain(_is_ours)
        check("every staged MARKER row was processed", set(drained) == pending_before_drain)
        check("no MARKER rows remain in the ledger after draining (purge succeeded)",
              not any(_is_ours(r) for r in ledger.get_pending_entries(conn, limit=1000)))

        proposals = fp.get_furniture_proposals(conn, status="all", limit=200)
        author_proposals = [p for p in proposals if p["entity_id"] == author_id]
        marcus_proposals = [p for p in proposals if p["entity_id"] == marcus_id]
        check("self-fact produced at least one Furniture proposal for the author",
              len(author_proposals) >= 1, f"got {[dict(p) for p in author_proposals]}")
        check("Building-mention produced a Furniture proposal for Marcus",
              len(marcus_proposals) >= 1, f"got {[dict(p) for p in marcus_proposals]}")
        if marcus_proposals:
            check("Marcus's proposal is about woodworking",
                  any("wood" in p["proposed_fact"].lower() for p in marcus_proposals))

        # -- Duplicate suppression: stage the exact same self-fact again.
        print("\n=== 4. Duplicate suppression ===")
        dup_id = ledger.stage_conversation_turn(
            conn, self_fact_text, session_id=None, author_user_id=AUTHOR_USER_ID, actor_role="manager",
        )
        proposals_before = len(fp.get_furniture_proposals(conn, status="all", limit=200))
        summary = fx._empty_source_summary()
        fx._process_record(
            conn, gemini_client, ledger.SOURCE_TYPE, dup_id, self_fact_text,
            AUTHOR_USER_ID, AUTHOR_USER_ID, summary, False, print,
        )
        ledger.delete_entry(conn, dup_id)
        proposals_after = len(fp.get_furniture_proposals(conn, status="all", limit=200))
        check("re-staging an identical fact does not create a second proposal",
              proposals_after == proposals_before, f"before={proposals_before} after={proposals_after}")
        check("duplicate row was still purged", not any(r["id"] == dup_id for r in ledger.get_pending_entries(conn, limit=1000)))

        # -- Process-restart safety: a row already marked processed (e.g. a
        # prior run crashed after marking it but before purging it) must
        # never be re-evaluated, and must still be purged on the next pass.
        print("\n--- restart-safety: pre-marked-processed row ---")
        restart_text = f"I really enjoy playing chess on weekends. [{MARKER}] restart-safety"
        restart_id = ledger.stage_conversation_turn(
            conn, restart_text, session_id=None, author_user_id=AUTHOR_USER_ID, actor_role="manager",
        )
        content_hash = fx.hashlib.sha256(restart_text.encode("utf-8")).hexdigest()
        fp.mark_source_processed(conn, ledger.SOURCE_TYPE, restart_id, content_hash, fx.EXTRACTOR_VERSION)
        proposals_before_restart = len(fp.get_furniture_proposals(conn, status="all", limit=200))
        summary = fx._empty_source_summary()
        fx._process_record(
            conn, gemini_client, ledger.SOURCE_TYPE, restart_id, restart_text,
            AUTHOR_USER_ID, AUTHOR_USER_ID, summary, False, print,
        )
        ledger.delete_entry(conn, restart_id)
        check("pre-marked-processed row is recognized, not re-evaluated",
              summary["records_already_ledgered"] == 1 and summary["candidates_evaluated"] == 0)
        check("no new proposal created from an already-processed row",
              len(fp.get_furniture_proposals(conn, status="all", limit=200)) == proposals_before_restart)
        check("already-processed row was still purged on this pass",
              not any(r["id"] == restart_id for r in ledger.get_pending_entries(conn, limit=1000)))

        print("\nAll test_conversation_ledger_scenario assertions evaluated.")
    finally:
        print("\nCleanup — removing all MARKER-tagged rows")
        _cleanup(conn)
        remaining_entities = conn.execute(
            "SELECT COUNT(*) FROM hearth_entities WHERE display_name LIKE ?;", (f"%{MARKER}%",)
        ).fetchone()[0]
        remaining_ledger = conn.execute(
            "SELECT COUNT(*) FROM hearth_conversation_ledger WHERE message_text LIKE ?;", (f"%{MARKER}%",)
        ).fetchone()[0]
        print(f"  Remaining MARKER entities: {remaining_entities}, remaining ledger rows: {remaining_ledger}")
        conn.close()

    print("\n" + ("=" * 60))
    if failures:
        print(f"{len(failures)} FAILURE(S):")
        for f in failures:
            print(f"  {f}")
        sys.exit(1)
    else:
        print("All test_conversation_ledger_scenario assertions passed.")


if __name__ == "__main__":
    main()
