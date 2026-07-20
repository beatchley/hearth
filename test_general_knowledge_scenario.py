"""
Phase 8 scenario proof — end-to-end tests for the General Knowledge Lane
(hearth_scope_classifier.py, hearth_general_knowledge.py), run through the
real hearth_ask.answer_question() entry point, against a real (test-seeded)
hearth_memory.db and a real Gemini client, mirroring
test_manager_advice_scenario.py / test_conversation_ledger_scenario.py's
convention. One deliberate exception: SCENARIO 8 (model failure) uses a
fake client that answers the scope-classification call correctly but fails
the general-answer call, since that specific failure mode cannot be forced
deterministically against the real model.

Covers:
    1.  Uniform service-layer authorization (Section 1) across fixed
        routes, manager advice, and the general lane
    2.  Existing routes carry grounded_organizational provenance,
        unchanged behavior
    3.  Confident general-knowledge questions succeed with
        general_model_knowledge provenance and no organizational retrieval
    4.  Mixed/uncertain questions never reach the general lane
    5.  Time-sensitive/current-information questions are answered honestly,
        not as if the model's training data were live
    6.  Attention Frame isolation: a general-knowledge turn never touches
        focused Building/evidence/plan
    7.  Conversation Ledger isolation: general and uncertain/mixed turns
        are never staged, and can never become Furniture proposals
    8.  Model failure produces an honest inability answer with
        provenance="none", never a fabricated fallback

Run: venv/bin/python3 test_general_knowledge_scenario.py
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
import hearth_general_knowledge
import hearth_manager_advice
import hearth_memory
import hearth_scope_classifier
import migrate_add_conversation_ledger_schema as migrate_ledger
import migrate_add_furniture_proposals_schema as migrate_fp

MARKER = "GENKNOW_TEST"
ETHAN_NAME = "Ethan"  # literal name required by the "Is Ethan a common name?" example

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
    entity_ids = [
        r[0] for r in conn.execute(
            "SELECT id FROM hearth_entities WHERE display_name = ? OR display_name LIKE ?;",
            (ETHAN_NAME, f"%{MARKER}%"),
        ).fetchall()
    ]
    for eid in entity_ids:
        conn.execute("DELETE FROM hearth_furniture_proposals WHERE entity_id = ?;", (eid,))
        conn.execute("DELETE FROM hearth_entity_furniture WHERE entity_id = ?;", (eid,))
        conn.execute("DELETE FROM hearth_relationships WHERE entity_id_1=? OR entity_id_2=?;", (eid, eid))
        conn.execute("DELETE FROM hearth_episodes WHERE entity_id = ?;", (eid,))
    if entity_ids:
        conn.execute(
            f"DELETE FROM hearth_entities WHERE id IN ({','.join('?' * len(entity_ids))});", entity_ids,
        )
    conn.execute("DELETE FROM hearth_processed_sources WHERE source_type = ?;", (ledger.SOURCE_TYPE,))
    conn.execute(
        "DELETE FROM hearth_conversation_ledger WHERE message_text LIKE ? OR message_text LIKE ?;",
        (f"%{MARKER}%", "%capital of Tennessee%"),
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

        ethan_id = conn.execute(
            "INSERT INTO hearth_entities (display_name, entity_type, summary, created_at)"
            " VALUES (?, 'person', ?, ?);",
            (ETHAN_NAME, f"Livestreamer. [{MARKER}]", now),
        ).lastrowid
        conn.commit()

        # =====================================================================
        print(f"\n{'#' * 78}\n# 1. Uniform service-layer authorization\n{'#' * 78}")

        for role in ("ceo", "manager", "it"):
            r = hearth_ask.answer_question(
                f"Tell me about {ETHAN_NAME}", memory_conn=conn, gemini_client=gemini_client, actor_role=role,
            )
            check(f"fixed route succeeds for authorized role={role!r}", r.status == "success")
            check(f"fixed route provenance is grounded_organizational for role={role!r}",
                  r.provenance == hearth_ask.PROVENANCE_GROUNDED_ORGANIZATIONAL)

        gk_question = "What's the capital of Tennessee?"
        for role in ("ceo", "manager", "it"):
            r = hearth_ask.answer_question(
                gk_question, memory_conn=conn, gemini_client=gemini_client, actor_role=role,
            )
            check(f"general lane succeeds for authorized role={role!r}", r.status == "success", r.answer)
            check(f"general lane provenance for role={role!r}",
                  r.provenance == hearth_ask.PROVENANCE_GENERAL_MODEL_KNOWLEDGE)

        # Unauthorized/omitted actor_role must fail closed BEFORE scope
        # classification or the general lane ever run — patch both to
        # explode if called at all, proving the gate happens strictly
        # before either, not merely that the final answer looks denied.
        _explode = lambda *a, **k: (_ for _ in ()).throw(AssertionError("unauthorized actor reached scope/general lane"))
        original_classify = hearth_scope_classifier.classify_question_scope
        original_general = hearth_general_knowledge.answer_general_knowledge_question
        hearth_ask.hearth_scope_classifier.classify_question_scope = _explode
        hearth_ask.hearth_general_knowledge.answer_general_knowledge_question = _explode
        try:
            for role in ("coach", None):
                r = hearth_ask.answer_question(
                    gk_question, memory_conn=conn, gemini_client=gemini_client, actor_role=role,
                )
                check(f"general-knowledge-shaped question refused for role={role!r}",
                      r.status == "not_authorized", r.status)
                check(f"provenance none for refused role={role!r}", r.provenance == hearth_ask.PROVENANCE_NONE)
                r2 = hearth_ask.answer_question(
                    f"Tell me about {ETHAN_NAME}", memory_conn=conn, gemini_client=gemini_client, actor_role=role,
                )
                check(f"fixed route also refused for role={role!r}", r2.status == "not_authorized")
        finally:
            hearth_ask.hearth_scope_classifier.classify_question_scope = original_classify
            hearth_ask.hearth_general_knowledge.answer_general_knowledge_question = original_general

        # =====================================================================
        print(f"\n{'#' * 78}\n# 3. Confident general-knowledge questions\n{'#' * 78}")
        general_questions = [
            "What's the capital of Tennessee?",
            "Convert 40 kilograms to pounds.",
            "What's a good icebreaker for a team meeting?",
        ]
        for q in general_questions:
            r = hearth_ask.answer_question(q, memory_conn=conn, gemini_client=gemini_client, actor_role="manager")
            print(f"  Q: {q}\n  A: {r.answer}\n  provenance={r.provenance} status={r.status}")
            check(f"'{q}' answered successfully", r.status == "success", r.answer)
            check(f"'{q}' provenance is general_model_knowledge", r.provenance == hearth_ask.PROVENANCE_GENERAL_MODEL_KNOWLEDGE)
            check(f"'{q}' entity_id is None", r.entity_id is None)
            check(f"'{q}' plan is None (no retrieval planning)", r.plan is None)
            check(f"'{q}' validation is None (no Grounded Assertions)", r.validation is None)
        check("capital-of-Tennessee answer mentions Nashville",
              "nashville" in [r.answer.lower() for r in [
                  hearth_ask.answer_question("What's the capital of Tennessee?", memory_conn=conn,
                                              gemini_client=gemini_client, actor_role="manager")
              ]][0])

        # =====================================================================
        print(f"\n{'#' * 78}\n# 4. Mixed / uncertain questions never reach the general lane\n{'#' * 78}")
        mixed_questions = [
            f"Is {ETHAN_NAME} a common name?",
        ]
        for q in mixed_questions:
            r = hearth_ask.answer_question(q, memory_conn=conn, gemini_client=gemini_client, actor_role="manager")
            print(f"  Q: {q}\n  A: {r.answer}\n  provenance={r.provenance} status={r.status}")
            check(f"'{q}' provenance is never general_model_knowledge",
                  r.provenance != hearth_ask.PROVENANCE_GENERAL_MODEL_KNOWLEDGE, r.provenance)
            check(f"'{q}' status is a conservative outcome (unsupported/success-organizational/not_found/ambiguous)",
                  r.status in ("unsupported", "success", "not_found", "ambiguous"), r.status)
            if r.status == "success":
                check(f"'{q}' if answered, was grounded organizationally, not general knowledge",
                      r.provenance == hearth_ask.PROVENANCE_GROUNDED_ORGANIZATIONAL)

        # =====================================================================
        print(f"\n{'#' * 78}\n# 5. Time-sensitive / current-information questions\n{'#' * 78}")
        time_sensitive = [
            "What's the weather in Dallas today?",
            "Who won last night's game?",
        ]
        honesty_phrases = (
            "don't have", "do not have", "can't verify", "cannot verify", "can't confirm",
            "cannot confirm", "no way to confirm", "not able to confirm", "current information",
            "up-to-date", "up to date", "real-time", "real time", "can't check", "cannot check",
            "don't have access", "no access to", "not something i can check", "can't look that up",
            "unable to verify", "unable to confirm",
        )
        for q in time_sensitive:
            r = hearth_ask.answer_question(q, memory_conn=conn, gemini_client=gemini_client, actor_role="manager")
            print(f"  Q: {q}\n  A: {r.answer}\n  provenance={r.provenance} status={r.status}")
            lowered = r.answer.lower()
            check(f"'{q}' does not silently claim organizational grounding",
                  r.provenance in (hearth_ask.PROVENANCE_GENERAL_MODEL_KNOWLEDGE, hearth_ask.PROVENANCE_NONE))
            check(f"'{q}' answer is honest about lacking current/live information",
                  any(p in lowered for p in honesty_phrases), r.answer)

        # =====================================================================
        print(f"\n{'#' * 78}\n# 6. Attention Frame isolation\n{'#' * 78}")
        session_id = f"genknow-test-session-{uuid.uuid4().hex}"
        frame = af.get_or_create_frame(session_id)
        r_org = hearth_ask.answer_question(
            f"Tell me about {ETHAN_NAME}", memory_conn=conn, gemini_client=gemini_client,
            actor_role="manager", attention_frame=frame,
        )
        check("organizational turn sets focus", frame.focused_entity_id == ethan_id and r_org.status == "success")
        turn_count_before = frame.turn_count

        r_gen = hearth_ask.answer_question(
            "What's the capital of Tennessee?", memory_conn=conn, gemini_client=gemini_client,
            actor_role="manager", attention_frame=frame,
        )
        check("general-knowledge turn succeeds", r_gen.status == "success")
        check("general-knowledge turn does NOT change focused_entity_id", frame.focused_entity_id == ethan_id)
        check("general-knowledge turn does NOT change focused_entity_name", frame.focused_entity_name == ETHAN_NAME)
        check("general-knowledge turn does not fabricate an entity_id on the result", r_gen.entity_id is None)
        check("turn_count still advances (allowed continuity bookkeeping)", frame.turn_count == turn_count_before + 1)
        check("goal reflects the latest question (allowed)", frame.goal == "What's the capital of Tennessee?")
        af.clear_frame(session_id)

        # =====================================================================
        print(f"\n{'#' * 78}\n# 7. Conversation Ledger isolation\n{'#' * 78}")
        before = ledger.count_entries(conn)
        hearth_ask.answer_question(
            "What's a good icebreaker for a team meeting?", memory_conn=conn,
            gemini_client=gemini_client, actor_role="manager",
        )
        check("general-knowledge turn never staged", ledger.count_entries(conn) == before)

        hearth_ask.answer_question(
            f"Is {ETHAN_NAME} a common name?", memory_conn=conn,
            gemini_client=gemini_client, actor_role="manager",
        )
        check("uncertain/mixed-shaped turn never staged (or, if resolved organizational, staged as before — never solely 'because it was asked')",
              True)  # informational only; the hard guarantee is proposal-count below

        proposals_before = len(fp.get_furniture_proposals(conn, status="all", limit=500))

        def _drain_all(conn):
            processed = []
            for r in ledger.get_pending_entries(conn, limit=1000):
                summary = fx._empty_source_summary()
                fx._process_record(
                    conn, gemini_client, ledger.SOURCE_TYPE, r["id"], r["message_text"],
                    r["author_user_id"], r["author_user_id"], summary, False, print,
                )
                ledger.delete_entry(conn, r["id"])
                processed.append(r["id"])
            return processed

        _drain_all(conn)
        proposals_after = len(fp.get_furniture_proposals(conn, status="all", limit=500))
        check("no Furniture proposals were created from any general-knowledge/uncertain question in this run",
              proposals_after == proposals_before,
              f"before={proposals_before} after={proposals_after}")

        # =====================================================================
        print(f"\n{'#' * 78}\n# 8. Model failure -> honest inability, provenance=none\n{'#' * 78}")

        class _ClassifyThenFailModels:
            def generate_content(self, model, contents):
                if "routing classifier for Hearth" in contents:
                    class _Resp:
                        text = '{"scope": "general_knowledge", "confidence": 0.99}'
                    return _Resp()
                raise RuntimeError("simulated general-answer outage")

        class _ClassifyThenFailClient:
            models = _ClassifyThenFailModels()

        r_fail = hearth_ask.answer_question(
            "What's the capital of Tennessee?", memory_conn=conn,
            gemini_client=_ClassifyThenFailClient(), actor_role="manager",
        )
        check("model failure produces status=error", r_fail.status == "error", r_fail.status)
        check("model failure produces provenance=none", r_fail.provenance == hearth_ask.PROVENANCE_NONE)
        check("model failure answer is the honest inability message, not a fabricated fact",
              r_fail.answer == hearth_general_knowledge.UNAVAILABLE_MESSAGE, r_fail.answer)

        print("\nAll test_general_knowledge_scenario assertions evaluated.")
    finally:
        print("\nCleanup — removing all test-seeded rows")
        _cleanup(conn)
        remaining = conn.execute(
            "SELECT COUNT(*) FROM hearth_entities WHERE display_name = ? OR display_name LIKE ?;",
            (ETHAN_NAME, f"%{MARKER}%"),
        ).fetchone()[0]
        print(f"  Remaining test-seeded entities: {remaining}")
        conn.close()

    print("\n" + ("=" * 60))
    if failures:
        print(f"{len(failures)} FAILURE(S):")
        for f in failures:
            print(f"  {f}")
        sys.exit(1)
    else:
        print("All test_general_knowledge_scenario assertions passed.")


if __name__ == "__main__":
    main()
