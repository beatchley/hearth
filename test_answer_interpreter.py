"""
Tests for Build 2 of the manager-answer-to-Worldview learning loop: the
general-purpose interpretation engine.

Covers: question-family stamping (hearth_soul.py / hearth_worldview.py,
including the aggregate recent_concern_volume family), the durable
interpretation ledger + structured-claims child table, deterministic
validation (evidence/claim-count/entity-grounding/negation/duplicate/status
checks), the constrained local-model semantic check, family-agnostic
eligibility/orchestration (including a SYNTHETIC never-before-seen family —
the guardrail proving the engine is truly generic, not four families with
extra steps), simple structured grouping + durable learning candidates, the
one-time backfill script, and the Reflection wiring point in
morning_briefing.py.

Never touches the real dev hearth_memory.db — uses isolated temp-file
databases via experience_evaluator_test_helpers.make_memory_db(), same
convention as test_question_auto_resolution.py and the
test_experience_evaluator_*.py suite.

No real Ollama or Gemini calls are made anywhere in this file — provider
calls (_call_ollama_structured, _call_ollama_semantic_check,
_call_gemini_structured) are monkeypatched. See
test_answer_interpreter_local_model_eval.py for the separate,
explicitly-invoked real-Ollama evaluation.

Run: venv/bin/python3 test_answer_interpreter.py
"""

import inspect
import os
import sqlite3
import tempfile

import experience_evaluator_test_helpers as h
import hearth_answer_interpreter as hai
import hearth_soul
import hearth_worldview
import migrate_add_answer_interpretations as ledger_migration
import migrate_add_question_family_fields as family_migration
import migrate_add_uncertainty_answer_fields as answer_fields_migration
import migrate_add_universal_claims_schema as claims_migration
import morning_briefing


def _make_db():
    """Fully migrated temp hearth_memory-shaped db, including Build 2's
    universal-claims-schema migration.
    """
    mconn, mpath = h.make_memory_db()
    answer_fields_migration.migrate(mconn)
    family_migration.migrate(mconn)  # no-op on a fresh db; ensure_worldview_tables already added the columns
    ledger_migration.migrate(mconn)
    claims_migration.migrate(mconn)
    return mconn, mpath


def _seed_answered_uncertainty(conn, subject_id, answer_text, family, version=1,
                                subject_type="entity_episode",
                                question_text=None, why_it_matters=None, context_text=None,
                                answered_by="stacy", answered_at="2026-07-20T00:00:00Z"):
    uid, _created = hearth_worldview.upsert_uncertainty(
        conn, subject_type=subject_type, subject_id=subject_id,
        uncertainty_text=context_text or f"Background about {subject_id}.",
        why_it_matters=why_it_matters,
        possible_question=question_text or f"Is {subject_id} a concern?",
        question_family=family, question_version=version,
    )
    conn.execute(
        "UPDATE hearth_worldview_uncertainties SET status='answered', answer_text=?,"
        " answered_by=?, answered_at=? WHERE id=?;",
        (answer_text, answered_by, answered_at, uid),
    )
    conn.commit()
    return uid


def _claim(subject="the creator", predicate="test_predicate", value="test_value",
           polarity="affirmed", scope="this_creator", temporal_status="current",
           conclusion_text="A grounded test conclusion.", evidence_quote="grounded evidence"):
    return {
        "subject": subject, "predicate": predicate, "value": value, "polarity": polarity,
        "scope": scope, "temporal_status": temporal_status, "conclusion_text": conclusion_text,
        "evidence_quote": evidence_quote,
    }


def _mock_structured(status, claims, reason="test reason", model_identifier="llama3.1:8b"):
    def _fake(*a, **k):
        return ({"status": status, "claims": claims, "reason": reason,
                 "model_identifier": model_identifier}, None, None, 0.01)
    return _fake


def _mock_structured_failure(category, detail="synthetic failure"):
    def _fake(*a, **k):
        return None, category, detail, 0.01
    return _fake


def _mock_semantic_check(supported=True, reason="semantic check ok"):
    def _fake(*a, **k):
        return {"supported": supported, "reason": reason, "model_identifier": "llama3.1:8b"}, None, None, 0.01
    return _fake


def _mock_semantic_check_failure(category="connection_error", detail="synthetic failure"):
    def _fake(*a, **k):
        return None, category, detail, 0.01
    return _fake


# ---------------------------------------------------------------------------
# 1. Schema / migration
# ---------------------------------------------------------------------------

def test_schema_and_migration():
    failures = []

    fresh_conn = sqlite3.connect(tempfile.mktemp(suffix=".db"))
    fresh_conn.row_factory = sqlite3.Row
    hearth_worldview.ensure_worldview_tables(fresh_conn)
    cols = {r["name"] for r in fresh_conn.execute("PRAGMA table_info(hearth_worldview_uncertainties);")}
    if not ({"question_family", "question_version"} <= cols):
        failures.append("fresh DB via ensure_worldview_tables() missing question_family/question_version")
    fresh_conn.close()

    # ensure_answer_interpretations_table() must create EVERYTHING this
    # build owns (ledger + universal columns + claims + candidates tables)
    # for a genuinely fresh DB, idempotently.
    conn = sqlite3.connect(tempfile.mktemp(suffix=".db"))
    conn.row_factory = sqlite3.Row
    hai.ensure_answer_interpretations_table(conn)
    hai.ensure_answer_interpretations_table(conn)  # idempotency
    for table in ("hearth_answer_interpretations", "hearth_answer_interpretation_claims",
                  "hearth_learning_candidates", "hearth_learning_candidate_members"):
        if not conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?;", (table,),
        ).fetchone():
            failures.append(f"{table} not created by ensure_answer_interpretations_table()")
    ledger_cols = {r["name"] for r in conn.execute("PRAGMA table_info(hearth_answer_interpretations);")}
    if not ({"universal_status", "raw_status", "raw_claims_json"} <= ledger_cols):
        failures.append("universal_status/raw_status/raw_claims_json columns missing")
    # Legacy Build 1 columns must still be present (backward compatibility).
    if not ({"raw_conclusion", "conclusion", "model_confidence"} <= ledger_cols):
        failures.append("legacy Build 1 columns were removed — breaks historical row compatibility")

    conn.execute(
        "INSERT INTO hearth_answer_interpretations (uncertainty_id, question_family, question_version,"
        " attempt_number, status, interpreter_version, schema_version, is_current_accepted, attempted_at, created_at)"
        " VALUES (1,'synthetic_family',1,1,'succeeded','v1','v1',1,'x','x');"
    )
    conn.commit()
    try:
        conn.execute(
            "INSERT INTO hearth_answer_interpretations (uncertainty_id, question_family, question_version,"
            " attempt_number, status, interpreter_version, schema_version, is_current_accepted, attempted_at, created_at)"
            " VALUES (1,'synthetic_family',1,2,'succeeded','v2','v1',1,'x','x');"
        )
        conn.commit()
        failures.append("partial unique index did not reject a second current-accepted row")
    except sqlite3.IntegrityError:
        pass
    conn.close()
    return failures


def test_schema_missing_graceful_degradation():
    failures = []
    import migrate_add_hearth_worldview as base_migration
    import migrate_add_uncertainty_last_seen_at as last_seen_migration
    import migrate_add_worldview_source_run as source_run_migration

    conn = sqlite3.connect(tempfile.mktemp(suffix=".db"))
    conn.row_factory = sqlite3.Row
    base_migration.migrate(conn)
    answer_fields_migration.migrate(conn)
    last_seen_migration.migrate(conn)
    source_run_migration.migrate(conn)

    cols = {r["name"] for r in conn.execute("PRAGMA table_info(hearth_worldview_uncertainties);")}
    if "question_family" in cols:
        failures.append("test setup invalid: base migration unexpectedly has question_family")
    if hai._ledger_table_exists(conn):
        failures.append("test setup invalid: ledger table unexpectedly already exists")

    try:
        hearth_worldview.upsert_uncertainty(
            conn, subject_type="entity_episode", subject_id="missing_discord:1",
            uncertainty_text="unrelated type, no family requested",
        )
    except Exception as exc:
        failures.append(f"family=None insert must never fail on an un-migrated DB: {exc}")

    summary = hai.process_eligible_answers(conn)
    if not summary.get("schema_missing"):
        failures.append(f"expected schema_missing=True on an un-migrated DB, got {summary}")
    if summary["eligible_found"] != 0 or summary["processed"] != 0:
        failures.append(f"no eligibility query/processing should happen when schema is missing: {summary}")
    if hai._ledger_table_exists(conn):
        failures.append("process_eligible_answers() must not create the ledger table itself")

    conn.close()
    return failures


def test_fresh_database_startup_sequence_matches_morning_briefing():
    failures = []
    conn = sqlite3.connect(tempfile.mktemp(suffix=".db"))
    conn.row_factory = sqlite3.Row
    import hearth_memory
    hearth_memory.init_tables(conn)
    hearth_soul.ensure_reflections_table(conn)
    import hearth_questions
    hearth_questions.ensure_questions_table(conn)
    hai.ensure_answer_interpretations_table(conn)
    hearth_worldview.ensure_worldview_tables(conn)

    if not hai._schema_ready(conn):
        failures.append("fresh database following morning_briefing's exact startup order is not schema-ready")

    conn.close()
    return failures


# ---------------------------------------------------------------------------
# 2. Question-family stamping at the generator (Part 8)
# ---------------------------------------------------------------------------

def test_family_stamping_single_episode_types():
    failures = []
    mconn, mpath = _make_db()
    try:
        cur = mconn.execute(
            "INSERT INTO hearth_entities (display_name, entity_type, created_at) VALUES (?, 'person', 'x');",
            ("STAMP_TEST_creator",),
        )
        entity_id = cur.lastrowid
        mconn.commit()

        # Rollout scope (reviewed 2026-08-02): only these three
        # _SINGLE_SIGNIFICANCE_TYPES members are stamped in this build.
        expected = {
            "checkin_feedback_waiting": ("checkin_feedback_waiting", 1),
            "missing_discord": ("missing_discord", 1),
            "new_creator_stuck": ("new_creator_stuck", 1),
        }
        for episode_type, (exp_family, exp_version) in expected.items():
            uid, _created = hearth_soul._upsert_single_episode_uncertainty(
                mconn, episode_type, entity_id, "manual_test",
            )
            row = hearth_worldview.get_uncertainty(mconn, uid)
            if row["question_family"] != exp_family or row["question_version"] != exp_version:
                failures.append(
                    f"{episode_type} not stamped correctly: family={row['question_family']!r}"
                    f" version={row['question_version']!r}, expected {exp_family}/{exp_version}"
                )

        # Shares the exact same generator/question template as the stamped
        # types above but was deliberately held back from this rollout — must
        # stay NULL until a separate rollout decision activates it.
        for episode_type in ("support_request_waiting", "training_comment_waiting", "onboarding_engagement"):
            uid_unstamped, _ = hearth_soul._upsert_single_episode_uncertainty(
                mconn, episode_type, entity_id, "manual_test",
            )
            row_unstamped = hearth_worldview.get_uncertainty(mconn, uid_unstamped)
            if row_unstamped["question_family"] is not None:
                failures.append(
                    f"{episode_type} was stamped despite being held back from this rollout:"
                    f" family={row_unstamped['question_family']!r}"
                )

        # creator_quiet writes to hearth_worldview_changes, not uncertainties
        # — not applicable to this learning loop, must never be stamped.
        if "creator_quiet" in hearth_soul._QUESTION_FAMILIES_BY_EPISODE_TYPE:
            failures.append("creator_quiet incorrectly registered as a question family (writes to changes, not uncertainties)")

        # Refresh path: re-observing the same living uncertainty must not
        # change/clear its stamped family/version.
        uid1, _ = hearth_soul._upsert_single_episode_uncertainty(mconn, "checkin_feedback_waiting", entity_id, "t1")
        uid1b, created1b = hearth_soul._upsert_single_episode_uncertainty(mconn, "checkin_feedback_waiting", entity_id, "t2")
        if uid1b != uid1 or created1b:
            failures.append("expected refresh (same living row), got a new row")
        row1b = hearth_worldview.get_uncertainty(mconn, uid1b)
        if row1b["question_family"] != "checkin_feedback_waiting":
            failures.append("family/version lost across a refresh of the same living row")

        # A pre-existing NULL-family living row must NOT get backfilled just
        # because it's re-observed by upsert_uncertainty with no explicit stamp.
        pre_uid, _ = hearth_worldview.upsert_uncertainty(
            mconn, subject_type="entity_episode", subject_id=f"checkin_feedback_waiting:{entity_id}_legacy",
            uncertainty_text="legacy pre-stamping row",
        )
        hearth_worldview.upsert_uncertainty(
            mconn, subject_type="entity_episode", subject_id=f"checkin_feedback_waiting:{entity_id}_legacy",
            uncertainty_text="legacy pre-stamping row, refreshed",
        )
        pre_row = hearth_worldview.get_uncertainty(mconn, pre_uid)
        if pre_row["question_family"] is not None:
            failures.append("refresh path incorrectly backfilled family onto a pre-existing NULL row")
    finally:
        h.cleanup_db(mconn, mpath)
    return failures


def test_family_stamping_recent_concern_volume():
    failures = []
    mconn, mpath = _make_db()
    try:
        cur = mconn.execute(
            "INSERT INTO hearth_entities (display_name, entity_type, created_at) VALUES (?, 'person', 'x');",
            ("CONCERN_VOLUME_TEST_creator",),
        )
        entity_id = cur.lastrowid
        mconn.commit()

        uid, _created = hearth_soul._upsert_entity_repeat_uncertainty(mconn, entity_id, 5, "manual_test")
        row = hearth_worldview.get_uncertainty(mconn, uid)
        if row["question_family"] != "recent_concern_volume" or row["question_version"] != 1:
            failures.append(
                f"recent_concern_volume aggregate question not stamped: family={row['question_family']!r}"
                f" version={row['question_version']!r}"
            )
    finally:
        h.cleanup_db(mconn, mpath)
    return failures


# ---------------------------------------------------------------------------
# 3. Deterministic validation (Part 6 / 15B) — pure function, no model calls
# ---------------------------------------------------------------------------

_Q = "Is the follow-up owner for this onboarding blocker clear?"
_WHY = "Unclear ownership delays resolution."
_CTX = "A new creator has been stuck in onboarding for two weeks."


def test_deterministic_validate_status_consistency():
    failures = []

    d = hai.deterministic_validate(_Q, _WHY, _CTX, "Some answer.", "supported", [])
    if not d["contract_violation"]:
        failures.append("supported with zero claims must be a contract violation")

    d2 = hai.deterministic_validate(_Q, _WHY, _CTX, "Some answer.", "insufficient_information",
                                     [_claim(evidence_quote="Some answer.")])
    if not d2["contract_violation"]:
        failures.append("non-supported status with claims must be a contract violation")

    d3 = hai.deterministic_validate(_Q, _WHY, _CTX, "Some answer.", "not_a_real_status", [])
    if not d3["contract_violation"]:
        failures.append("invalid top-level status must be a contract violation")

    d4 = hai.deterministic_validate(_Q, _WHY, _CTX, "It's unclear.", "unrelated_or_unclear", [])
    if d4["contract_violation"]:
        failures.append(f"valid non-supported/zero-claims combo incorrectly rejected: {d4['contract_violation']}")

    return failures


def test_deterministic_validate_claim_count():
    failures = []
    answer = "The follow-up owner is Alex, the assigned recruiter."
    claims = [
        _claim(predicate="follow_up_owner", value="alex", evidence_quote="follow-up owner is Alex"),
        _claim(predicate="onboarding_blocker", value="waiting_on_recruiter", evidence_quote="assigned recruiter"),
        _claim(predicate="current_status", value="assigned", evidence_quote="the assigned recruiter"),
    ]
    d = hai.deterministic_validate(_Q, _WHY, _CTX, answer, "supported", claims)
    if d["contract_violation"]:
        failures.append(f"3 valid claims incorrectly rejected at the contract level: {d['contract_violation']}")
    if len(d["claim_checks"]) != 3 or not all(c["structurally_valid"] for c in d["claim_checks"]):
        failures.append(f"3 valid claims not all structurally accepted: {d['claim_checks']}")

    too_many = claims + [_claim(predicate="extra", value="extra", evidence_quote="Alex")]
    d2 = hai.deterministic_validate(_Q, _WHY, _CTX, answer, "supported", too_many)
    if not d2["contract_violation"]:
        failures.append("more than 3 claims must be a contract violation")

    return failures


def test_deterministic_validate_evidence_and_duplicates():
    failures = []
    answer = "Yes, Alex owns the follow-up."

    good = _claim(predicate="follow_up_owner", value="alex", evidence_quote="Alex owns the follow-up")
    d = hai.deterministic_validate(_Q, _WHY, _CTX, answer, "supported", [good])
    if d["contract_violation"] or not d["claim_checks"][0]["structurally_valid"]:
        failures.append(f"verbatim evidence_quote incorrectly rejected: {d}")

    bad_quote = _claim(predicate="follow_up_owner", value="alex", evidence_quote="Jordan owns the follow-up")
    d2 = hai.deterministic_validate(_Q, _WHY, _CTX, answer, "supported", [bad_quote])
    if d2["claim_checks"][0]["structurally_valid"]:
        failures.append("evidence_quote absent from the answer was not rejected")

    dup = [good, dict(good)]
    d3 = hai.deterministic_validate(_Q, _WHY, _CTX, answer, "supported", dup)
    valid_flags = [c["structurally_valid"] for c in d3["claim_checks"]]
    if valid_flags != [True, False]:
        failures.append(f"duplicate claim within one answer not deduplicated correctly: {valid_flags}")

    missing_field = _claim(predicate="", value="alex", evidence_quote="Alex owns the follow-up")
    d4 = hai.deterministic_validate(_Q, _WHY, _CTX, answer, "supported", [missing_field])
    if d4["claim_checks"][0]["structurally_valid"]:
        failures.append("claim with missing required field was not rejected")

    bad_polarity = _claim(polarity="maybe", evidence_quote="Alex owns the follow-up")
    d5 = hai.deterministic_validate(_Q, _WHY, _CTX, answer, "supported", [bad_polarity])
    if d5["claim_checks"][0]["structurally_valid"]:
        failures.append("invalid polarity enum value was not rejected")

    return failures


def test_evidence_quote_case_insensitive_not_paraphrase():
    """Verbatim quote matching is case-insensitive on top of the existing
    whitespace/quote normalization, but still requires the quote to appear
    as a contiguous substring — paraphrases must still fail."""
    failures = []
    answer = "Yes, Discord   is required for   this program.  She said ‘it is mandatory.’"

    case_only = _claim(predicate="discord_requirement", value="required",
                        evidence_quote="discord IS REQUIRED for this program")
    d = hai.deterministic_validate(_Q, _WHY, _CTX, answer, "supported", [case_only])
    if not d["claim_checks"][0]["structurally_valid"]:
        failures.append(f"case-only difference incorrectly rejected: {d['claim_checks'][0]['reason']}")

    ws_and_quote_and_case = _claim(predicate="discord_requirement", value="required",
                                    evidence_quote="SHE SAID 'it is mandatory.'")
    d2 = hai.deterministic_validate(_Q, _WHY, _CTX, answer, "supported", [ws_and_quote_and_case])
    if not d2["claim_checks"][0]["structurally_valid"]:
        failures.append(f"combined whitespace/quote/case normalization incorrectly rejected: {d2['claim_checks'][0]['reason']}")

    paraphrase = _claim(predicate="discord_requirement", value="required",
                         evidence_quote="Discord must be used for this program")
    d3 = hai.deterministic_validate(_Q, _WHY, _CTX, answer, "supported", [paraphrase])
    if d3["claim_checks"][0]["structurally_valid"]:
        failures.append("paraphrased (non-verbatim) evidence_quote was incorrectly accepted")

    absent = _claim(predicate="discord_requirement", value="required",
                     evidence_quote="Discord is completely optional")
    d4 = hai.deterministic_validate(_Q, _WHY, _CTX, answer, "supported", [absent])
    if d4["claim_checks"][0]["structurally_valid"]:
        failures.append("evidence_quote absent from the answer was incorrectly accepted")

    # Original text must be preserved untouched for storage/display regardless.
    if case_only["evidence_quote"] != "discord IS REQUIRED for this program":
        failures.append("original evidence_quote text was mutated by the case-insensitive check")

    return failures


def test_deterministic_validate_entity_grounding():
    failures = []
    answer = "Yes, Alex owns the follow-up."

    grounded = _claim(subject="Alex", conclusion_text="Alex owns the follow-up.",
                       evidence_quote="Alex owns the follow-up")
    d = hai.deterministic_validate(_Q, _WHY, _CTX, answer, "supported", [grounded])
    if not d["claim_checks"][0]["structurally_valid"]:
        failures.append(f"grounded subject/entity incorrectly rejected: {d['claim_checks'][0]['reason']}")

    invented = _claim(subject="Jordan Smith", conclusion_text="Jordan Smith owns the follow-up.",
                       evidence_quote="Alex owns the follow-up")
    d2 = hai.deterministic_validate(_Q, _WHY, _CTX, answer, "supported", [invented])
    if d2["claim_checks"][0]["structurally_valid"]:
        failures.append("invented entity name not present in question/context/answer was not rejected")

    return failures


def test_deterministic_validate_negation():
    failures = []
    answer = "No, Discord is not required for this program."

    negated_grounded = _claim(predicate="discord_requirement", value="not_required", polarity="negated",
                               conclusion_text="Discord is not required.",
                               evidence_quote="Discord is not required for this program")
    d = hai.deterministic_validate(_Q, _WHY, _CTX, answer, "supported", [negated_grounded])
    if not d["claim_checks"][0]["structurally_valid"]:
        failures.append(f"grounded negation incorrectly rejected: {d['claim_checks'][0]['reason']}")

    negated_ungrounded = _claim(predicate="discord_requirement", value="not_required", polarity="negated",
                                 conclusion_text="Discord is not required.",
                                 evidence_quote="Discord is required for this program")
    d2 = hai.deterministic_validate(_Q, _WHY, _CTX, answer, "supported", [negated_ungrounded])
    if d2["claim_checks"][0]["structurally_valid"]:
        failures.append("polarity=negated with no negation marker in evidence_quote was not rejected")

    return failures


# ---------------------------------------------------------------------------
# 4. Short-answer handling (Part 7 / 15C) — engine defers to the model's own
#    judgment about question shape; no hardcoded bare-answer downgrade exists.
# ---------------------------------------------------------------------------

def test_short_answers_binary_vs_explanatory():
    failures = []
    mconn, mpath = _make_db()
    try:
        # Binary question, bare "Yes" -> model (correctly) says supported with
        # exactly the proposition asked; engine must not force a downgrade.
        uid_binary = _seed_answered_uncertainty(
            mconn, "synthetic_binary:1", "Yes.", family="synthetic_binary_q", version=1,
            question_text="Is Discord required for this program?",
            why_it_matters="Determines onboarding checklist requirements.",
        )
        claim = _claim(subject="this program", predicate="discord_requirement", value="required",
                        polarity="affirmed", evidence_quote="Yes")
        orig_structured = hai._call_ollama_structured
        orig_semantic = hai._call_ollama_semantic_check
        hai._call_ollama_structured = _mock_structured("supported", [claim])
        hai._call_ollama_semantic_check = _mock_semantic_check(supported=True)
        try:
            hai.process_eligible_answers(mconn)
        finally:
            hai._call_ollama_structured = orig_structured
            hai._call_ollama_semantic_check = orig_semantic

        accepted = hai.get_current_accepted(mconn, uid_binary)
        if accepted is None or accepted["universal_status"] != "supported":
            failures.append(f"binary bare-yes with grounded claim was not accepted: {dict(accepted) if accepted else None}")

        # Explanatory question, bare "Yes" -> the MODEL decides this is
        # insufficient (no explanation supplied); engine must simply respect it.
        uid_explanatory = _seed_answered_uncertainty(
            mconn, "synthetic_explanatory:1", "Yes.", family="synthetic_explanatory_q", version=1,
            question_text="Why has this creator been stuck in onboarding?",
        )
        hai._call_ollama_structured = _mock_structured("insufficient_information", [],
                                                         reason="Bare yes/no does not explain why.")
        try:
            hai.process_eligible_answers(mconn)
        finally:
            hai._call_ollama_structured = orig_structured

        accepted2 = hai.get_current_accepted(mconn, uid_explanatory)
        if accepted2 is None or accepted2["universal_status"] != "insufficient_information":
            failures.append(f"explanatory bare-yes not respected as insufficient_information: {dict(accepted2) if accepted2 else None}")
    finally:
        h.cleanup_db(mconn, mpath)
    return failures


# ---------------------------------------------------------------------------
# 5. Family-agnostic orchestration (Part 4 / 15A / 15D) — THE key guardrail:
#    a totally synthetic, never-before-registered family must process
#    identically to every known one, in the SAME Reflection cycle.
# ---------------------------------------------------------------------------

_KNOWN_FAMILIES = ["checkin_feedback_waiting", "missing_discord", "new_creator_stuck",
                    "recent_concern_volume"]
_SYNTHETIC_FAMILY = "future_family_never_seen_before"


def test_family_independence_including_synthetic_future_family():
    failures = []
    mconn, mpath = _make_db()
    try:
        uids = {}
        for i, family in enumerate(_KNOWN_FAMILIES):
            uids[family] = _seed_answered_uncertainty(
                mconn, f"{family}:{900 + i}", "Yes, this is confirmed by the manager.",
                family=family, version=1,
                question_text=f"Is this a concern for {family}?",
            )
        uids[_SYNTHETIC_FAMILY] = _seed_answered_uncertainty(
            mconn, "synthetic_subject:1",
            "Yes, it's blocked because the vendor hasn't delivered the API keys yet.",
            family=_SYNTHETIC_FAMILY, version=1,
            question_text="Is Widget Project X's rollout currently blocked?",
            why_it_matters="Blocked rollouts need executive attention.",
            context_text="Widget Project X is a new initiative Hearth has never modeled before.",
        )

        def _fake_structured(question_text, why_it_matters, context_text, answer_text, **kw):
            claim = _claim(
                subject="Widget Project X" if "Widget" in question_text else "the manager",
                predicate="rollout_status" if "Widget" in question_text else "confirmation",
                value="blocked" if "Widget" in question_text else "confirmed",
                evidence_quote=(
                    "it's blocked because the vendor hasn't delivered the API keys yet"
                    if "Widget" in question_text else "Yes, this is confirmed by the manager"
                ),
                conclusion_text="Grounded conclusion.",
            )
            return {"status": "supported", "claims": [claim], "reason": "grounded",
                    "model_identifier": "llama3.1:8b"}, None, None, 0.01

        orig_structured = hai._call_ollama_structured
        orig_semantic = hai._call_ollama_semantic_check
        hai._call_ollama_structured = _fake_structured
        hai._call_ollama_semantic_check = _mock_semantic_check(supported=True)
        try:
            summary = hai.process_eligible_answers(mconn)
        finally:
            hai._call_ollama_structured = orig_structured
            hai._call_ollama_semantic_check = orig_semantic

        if summary["succeeded"] != 5:
            failures.append(f"expected all 5 families (4 known + 1 synthetic) to succeed, got {summary}")
        if sorted(summary["families_processed"]) != sorted(uids.keys()):
            failures.append(f"families_processed did not include every stamped family: {summary['families_processed']}")

        for family, uid in uids.items():
            accepted = hai.get_current_accepted(mconn, uid)
            if accepted is None:
                failures.append(f"{family}: no accepted interpretation")
                continue
            if accepted["question_family"] != family:
                failures.append(f"{family}: attempt stamped with wrong family {accepted['question_family']!r}")
            if accepted["universal_status"] != "supported":
                failures.append(f"{family}: expected supported, got {accepted['universal_status']!r}")
            claims = hai.get_claims_for_interpretation(mconn, accepted["id"])
            if not any(c["accepted"] for c in claims):
                failures.append(f"{family}: no accepted claim stored")

        # Idempotency: repeated Reflection must not duplicate accepted interpretations.
        call_count = {"n": 0}
        def _counting(*a, **k):
            call_count["n"] += 1
            return _fake_structured(*a, **k)
        hai._call_ollama_structured = _counting
        try:
            summary2 = hai.process_eligible_answers(mconn)
        finally:
            hai._call_ollama_structured = orig_structured
        if call_count["n"] != 0 or summary2["eligible_found"] != 0:
            failures.append(f"repeated Reflection reprocessed already-accepted rows: {summary2}")
    finally:
        h.cleanup_db(mconn, mpath)
    return failures


def test_one_familys_failure_does_not_block_another():
    failures = []
    mconn, mpath = _make_db()
    try:
        uid_bad = _seed_answered_uncertainty(mconn, "family_a:1", "answer a", family="family_a")
        uid_good = _seed_answered_uncertainty(mconn, "family_b:1", "answer b", family="family_b")

        orig = hai._call_ollama_structured
        def _flaky(question_text, why_it_matters, context_text, answer_text, **kw):
            if "family_a" in question_text or answer_text == "answer a":
                raise RuntimeError("boom — simulated unexpected error for family_a only")
            return {"status": "insufficient_information", "claims": [], "reason": "x",
                    "model_identifier": "llama3.1:8b"}, None, None, 0.01
        hai._call_ollama_structured = _flaky
        try:
            summary = hai.process_eligible_answers(mconn)
        finally:
            hai._call_ollama_structured = orig

        if summary["succeeded"] != 1:
            failures.append(f"family_b should still succeed despite family_a's failure: {summary}")
        accepted_b = hai.get_current_accepted(mconn, uid_good)
        if accepted_b is None:
            failures.append("family_b was not processed after family_a's per-row exception")
    finally:
        h.cleanup_db(mconn, mpath)
    return failures


def test_eligibility_family_agnostic():
    failures = []
    mconn, mpath = _make_db()
    try:
        null_uid, _ = hearth_worldview.upsert_uncertainty(
            mconn, subject_type="entity_episode", subject_id="missing_discord:5",
            uncertainty_text="unrelated type, no family",
        )
        mconn.execute(
            "UPDATE hearth_worldview_uncertainties SET status='answered', answer_text='fine',"
            " answered_by='x', answered_at='x' WHERE id=?;",
            (null_uid,),
        )
        arbitrary_uid = _seed_answered_uncertainty(mconn, "totally_arbitrary:1", "fine",
                                                     family="totally_arbitrary_family_xyz", version=1)
        mconn.commit()

        eligible = hai.get_eligible_answered_uncertainties(mconn)
        eligible_ids = {row["id"] for row in eligible}
        if null_uid in eligible_ids:
            failures.append("NULL-family row was incorrectly treated as eligible")
        if arbitrary_uid not in eligible_ids:
            failures.append("an arbitrary, never-before-seen stamped family was NOT treated as eligible"
                             " — eligibility must not be restricted to a known-family allowlist")

        # Emergency denylist: operational-only, must not require semantic meaning.
        orig_denylist = hai.HEARTH_INTERPRETER_FAMILY_DENYLIST
        hai.HEARTH_INTERPRETER_FAMILY_DENYLIST = "totally_arbitrary_family_xyz"
        try:
            eligible2 = hai.get_eligible_answered_uncertainties(mconn)
            if arbitrary_uid in {row["id"] for row in eligible2}:
                failures.append("family denylist did not exclude the denied family")
        finally:
            hai.HEARTH_INTERPRETER_FAMILY_DENYLIST = orig_denylist

        eligible3 = hai.get_eligible_answered_uncertainties(mconn)
        if arbitrary_uid not in {row["id"] for row in eligible3}:
            failures.append("denylist change leaked past its own test (default should be empty again)")
    finally:
        h.cleanup_db(mconn, mpath)
    return failures


def test_batch_size_bound():
    failures = []
    mconn, mpath = _make_db()
    try:
        for i in range(5):
            _seed_answered_uncertainty(mconn, f"checkin_feedback_waiting:{700+i}", "This is normal.",
                                        family="checkin_feedback_waiting")
        eligible = hai.get_eligible_answered_uncertainties(mconn, batch_size=2)
        if len(eligible) != 2:
            failures.append(f"batch_size=2 not respected, got {len(eligible)}")
    finally:
        h.cleanup_db(mconn, mpath)
    return failures


# ---------------------------------------------------------------------------
# 6. Ledger lifecycle + retry + interpreter versioning
# ---------------------------------------------------------------------------

def test_ledger_lifecycle_success_not_reclassified():
    failures = []
    mconn, mpath = _make_db()
    try:
        uid = _seed_answered_uncertainty(mconn, "family_x:100", "Alex confirmed the plan.", family="family_x")
        orig = hai._call_ollama_structured
        orig_sem = hai._call_ollama_semantic_check
        claim = _claim(subject="Alex", conclusion_text="Alex confirmed the plan.",
                        evidence_quote="Alex confirmed the plan")
        hai._call_ollama_structured = _mock_structured("supported", [claim])
        hai._call_ollama_semantic_check = _mock_semantic_check(True)
        try:
            summary = hai.process_eligible_answers(mconn)
        finally:
            hai._call_ollama_structured = orig
            hai._call_ollama_semantic_check = orig_sem

        if summary["succeeded"] != 1:
            failures.append(f"expected 1 succeeded, got {summary}")
        accepted = hai.get_current_accepted(mconn, uid)
        if accepted is None or accepted["universal_status"] != "supported":
            failures.append("accepted interpretation not recorded correctly")

        call_count = {"n": 0}
        def _counting_fake(*a, **k):
            call_count["n"] += 1
            return {"status": "unrelated_or_unclear", "claims": [], "reason": "x",
                    "model_identifier": "x"}, None, None, 0.01
        hai._call_ollama_structured = _counting_fake
        try:
            summary2 = hai.process_eligible_answers(mconn)
        finally:
            hai._call_ollama_structured = orig
        if call_count["n"] != 0 or summary2["eligible_found"] != 0:
            failures.append(f"successful row was reclassified on rerun: calls={call_count['n']} summary={summary2}")

        attempts = hai.get_attempts_for_uncertainty(mconn, uid)
        if len(attempts) != 1:
            failures.append(f"expected exactly 1 durable attempt row, found {len(attempts)}")
    finally:
        h.cleanup_db(mconn, mpath)
    return failures


def test_zero_surviving_claims_one_proposed_rejected():
    """raw_status=supported, exactly one proposed claim, it fails validation
    -> accepted as insufficient_information (never left ambiguous, never
    accepted as supported with zero grounded claims)."""
    failures = []
    mconn, mpath = _make_db()
    try:
        uid = _seed_answered_uncertainty(mconn, "family_y:101", "Something happened.", family="family_y")
        claim = _claim(predicate="test_pred", value="test_val",
                        evidence_quote="this text is not in the answer at all")
        orig = hai._call_ollama_structured
        hai._call_ollama_structured = _mock_structured("supported", [claim])
        try:
            summary = hai.process_eligible_answers(mconn)
        finally:
            hai._call_ollama_structured = orig

        if summary["succeeded"] != 1 or summary.get("ambiguous", 0) != 0:
            failures.append(f"expected 1 succeeded (not ambiguous), got {summary}")

        accepted = hai.get_current_accepted(mconn, uid)
        if accepted is None:
            failures.append("expected an accepted interpretation, found none")
        else:
            if accepted["universal_status"] != "insufficient_information":
                failures.append(f"expected accepted universal_status=insufficient_information, got {accepted['universal_status']!r}")
            if accepted["raw_status"] != "supported":
                failures.append(f"raw_status=supported was not preserved, got {accepted['raw_status']!r}")
            if "all_proposed_claims_failed_validation" not in (accepted["validation_reason"] or ""):
                failures.append(f"validation_reason did not record the standard marker: {accepted['validation_reason']!r}")

            claims = hai.get_claims_for_interpretation(mconn, accepted["id"])
            if len(claims) != 1 or claims[0]["accepted"] or not claims[0]["rejection_reason"]:
                failures.append(f"rejected claim/reason not preserved: {[dict(c) for c in claims]}")

        # Never resent to the model — this is now a terminal accepted result,
        # not an ambiguous one awaiting retry.
        call_count = {"n": 0}
        def _counting_fake(*a, **k):
            call_count["n"] += 1
            return {"status": "supported", "claims": [claim], "reason": "x", "model_identifier": "x"}, None, None, 0.01
        hai._call_ollama_structured = _counting_fake
        try:
            hai.process_eligible_answers(mconn)
        finally:
            hai._call_ollama_structured = orig
        if call_count["n"] != 0:
            failures.append("accepted insufficient_information row was resent to the model on a later Reflection run")

        attempts = hai.get_attempts_for_uncertainty(mconn, uid)
        if len(attempts) != 1 or attempts[0]["status"] != "succeeded":
            failures.append(f"expected exactly 1 succeeded attempt, got {[dict(a) for a in attempts]}")
    finally:
        h.cleanup_db(mconn, mpath)
    return failures


def test_zero_surviving_claims_multiple_proposed_all_rejected():
    """raw_status=supported, three proposed claims, ALL fail validation ->
    still accepted as insufficient_information, all three rejected claims
    preserved with their own reasons."""
    failures = []
    mconn, mpath = _make_db()
    try:
        uid = _seed_answered_uncertainty(mconn, "family_y2:102", "Alex mentioned something.", family="family_y2")
        claims = [
            _claim(predicate="p1", value="v1", evidence_quote="not present anywhere"),
            _claim(predicate="p2", value="v2", polarity="not_a_real_polarity"),
            _claim(predicate="p1", value="v1", evidence_quote="not present anywhere"),  # duplicate of claim 1
        ]
        orig = hai._call_ollama_structured
        hai._call_ollama_structured = _mock_structured("supported", claims)
        try:
            summary = hai.process_eligible_answers(mconn)
        finally:
            hai._call_ollama_structured = orig

        if summary["succeeded"] != 1:
            failures.append(f"expected 1 succeeded (downgraded to insufficient_information), got {summary}")
        accepted = hai.get_current_accepted(mconn, uid)
        if accepted is None or accepted["universal_status"] != "insufficient_information":
            failures.append(f"expected accepted insufficient_information, got {dict(accepted) if accepted else None}")
        if accepted and accepted["raw_status"] != "supported":
            failures.append("raw_status=supported not preserved for a multi-claim all-rejected answer")

        stored_claims = hai.get_claims_for_interpretation(mconn, accepted["id"]) if accepted else []
        if len(stored_claims) != 3 or any(c["accepted"] for c in stored_claims):
            failures.append(f"expected all 3 proposed claims stored and none accepted: {[dict(c) for c in stored_claims]}")
        if not all(c["rejection_reason"] for c in stored_claims):
            failures.append("every rejected claim must carry its own rejection_reason")
    finally:
        h.cleanup_db(mconn, mpath)
    return failures


def test_zero_surviving_claims_mixed_valid_and_rejected():
    """raw_status=supported with 2 proposed claims — one grounded (survives),
    one not (rejected) — must accept as SUPPORTED (>=1 accepted claim), never
    downgraded, and must store both the accepted and rejected claim rows."""
    failures = []
    mconn, mpath = _make_db()
    try:
        uid = _seed_answered_uncertainty(mconn, "family_mix:103",
                                          "Alex confirmed the plan; the timeline is still unclear.",
                                          family="family_mix")
        good_claim = _claim(subject="Alex", predicate="plan_confirmation", value="confirmed",
                             conclusion_text="Alex confirmed the plan.",
                             evidence_quote="Alex confirmed the plan")
        bad_claim = _claim(predicate="timeline_status", value="fixed",
                            evidence_quote="the timeline is fixed")  # not actually in the answer
        orig_structured = hai._call_ollama_structured
        orig_semantic = hai._call_ollama_semantic_check
        hai._call_ollama_structured = _mock_structured("supported", [good_claim, bad_claim])
        hai._call_ollama_semantic_check = _mock_semantic_check(True)
        try:
            summary = hai.process_eligible_answers(mconn)
        finally:
            hai._call_ollama_structured = orig_structured
            hai._call_ollama_semantic_check = orig_semantic

        if summary["succeeded"] != 1:
            failures.append(f"expected 1 succeeded, got {summary}")
        accepted = hai.get_current_accepted(mconn, uid)
        if accepted is None or accepted["universal_status"] != "supported":
            failures.append(f"mixed valid+rejected claims must accept as supported, got {dict(accepted) if accepted else None}")

        stored_claims = hai.get_claims_for_interpretation(mconn, accepted["id"]) if accepted else []
        accepted_flags = sorted(bool(c["accepted"]) for c in stored_claims)
        if accepted_flags != [False, True]:
            failures.append(f"expected exactly one accepted and one rejected claim stored: {[dict(c) for c in stored_claims]}")
    finally:
        h.cleanup_db(mconn, mpath)
    return failures


def test_zero_surviving_claims_aggregation_and_admin_display():
    """Aggregation must count a zero-surviving-claims outcome under
    insufficient_information (accepted), not under the ambiguous/rejected
    bucket — and the review-row data used by the admin page must expose the
    rejected claims for display."""
    failures = []
    mconn, mpath = _make_db()
    try:
        uid = _seed_answered_uncertainty(mconn, "family_agg2:104", "Something unclear happened.",
                                          family="family_agg2")
        claim = _claim(evidence_quote="not present in the answer")
        orig = hai._call_ollama_structured
        hai._call_ollama_structured = _mock_structured("supported", [claim])
        try:
            hai.process_eligible_answers(mconn)
        finally:
            hai._call_ollama_structured = orig

        report = hai.aggregate_family(mconn, "family_agg2", 1)
        if report["total_accepted"] != 1:
            failures.append(f"expected 1 accepted (downgraded, not ambiguous), got total_accepted={report['total_accepted']}")
        if report["by_label"].get("insufficient_information") != 1:
            failures.append(f"expected by_label to count this under insufficient_information: {report['by_label']}")
        if report["total_ambiguous_or_rejected"] != 0:
            failures.append(f"a downgraded-but-accepted result must not count as ambiguous_or_rejected: {report['total_ambiguous_or_rejected']}")

        rows = hai.get_review_rows(mconn, question_family="family_agg2")
        if not rows or rows[0]["universal_status"] != "insufficient_information":
            failures.append(f"admin review row did not surface the downgraded universal_status: {rows}")
        if not rows or not rows[0]["claims"] or rows[0]["claims"][0]["accepted"]:
            failures.append(f"admin review row did not surface the rejected claim for display: {rows}")
    finally:
        h.cleanup_db(mconn, mpath)
    return failures


def test_ledger_rejects_contract_violations():
    failures = []
    mconn, mpath = _make_db()
    try:
        uid = _seed_answered_uncertainty(mconn, "family_z:102", "Some answer.", family="family_z")
        orig = hai._call_ollama_structured
        hai._call_ollama_structured = _mock_structured("supported", [])  # contract violation
        try:
            hai.process_eligible_answers(mconn)
        finally:
            hai._call_ollama_structured = orig
        attempts = hai.get_attempts_for_uncertainty(mconn, uid)
        if len(attempts) != 1 or attempts[0]["status"] != "rejected_by_validation":
            failures.append(f"expected rejected_by_validation, got {[dict(a) for a in attempts]}")
    finally:
        h.cleanup_db(mconn, mpath)
    return failures


def test_ledger_retry_policy_and_new_interpreter_version():
    failures = []
    mconn, mpath = _make_db()
    try:
        uid = _seed_answered_uncertainty(mconn, "family_r:103", "This is normal.", family="family_r")
        orig = hai._call_ollama_structured

        hai._call_ollama_structured = _mock_structured_failure("connection_error")
        try:
            hai.process_eligible_answers(mconn)
        finally:
            hai._call_ollama_structured = orig
        attempts = hai.get_attempts_for_uncertainty(mconn, uid)
        if len(attempts) != 1 or attempts[0]["status"] != "failed":
            failures.append(f"expected 1 failed attempt, got {[dict(a) for a in attempts]}")

        hai._call_ollama_structured = _mock_structured("insufficient_information", [])
        try:
            summary = hai.process_eligible_answers(mconn, batch_size=10)
        finally:
            hai._call_ollama_structured = orig
        if summary["eligible_found"] != 0:
            failures.append("retry happened before cooldown elapsed")

        hai._call_ollama_structured = _mock_structured_failure("timeout")
        try:
            eligible = hai.get_eligible_answered_uncertainties(mconn, retry_cooldown_seconds=0)
            if len(eligible) != 1:
                failures.append(f"expected retry to be eligible with 0s cooldown, got {len(eligible)}")
            for row in eligible:
                hai.process_one_uncertainty(mconn, row)
        finally:
            hai._call_ollama_structured = orig
        attempts = hai.get_attempts_for_uncertainty(mconn, uid)
        if len(attempts) != 2 or attempts[1]["retry_of_attempt_id"] != attempts[0]["id"]:
            failures.append(f"retry did not create a properly linked second attempt: {[dict(a) for a in attempts]}")

        hai._call_ollama_structured = _mock_structured_failure("timeout")
        try:
            eligible = hai.get_eligible_answered_uncertainties(mconn, retry_cooldown_seconds=0, max_retries=2)
            if len(eligible) != 0:
                failures.append(f"expected retry budget of 2 to already be exhausted, got {len(eligible)}")
        finally:
            hai._call_ollama_structured = orig

        # New interpreter_version creates a fresh attempt without touching old ones.
        hai._call_ollama_structured = _mock_structured("insufficient_information", [])
        try:
            eligible_v2 = hai.get_eligible_answered_uncertainties(
                mconn, interpreter_version="manager_answer_general_v2_test", retry_cooldown_seconds=0,
            )
            if len(eligible_v2) != 1:
                failures.append("new interpreter_version did not see the uncertainty as eligible")
            for row in eligible_v2:
                hai.process_one_uncertainty(mconn, row, interpreter_version="manager_answer_general_v2_test")
        finally:
            hai._call_ollama_structured = orig

        all_attempts = hai.get_attempts_for_uncertainty(mconn, uid)
        if len(all_attempts) != 3:
            failures.append(f"expected 3 total durable attempts, got {len(all_attempts)}")
        accepted = hai.get_current_accepted(mconn, uid)
        if accepted is None or accepted["interpreter_version"] != "manager_answer_general_v2_test":
            failures.append("new-version attempt was not correctly accepted as current")
        accepted_count = sum(1 for a in all_attempts if a["is_current_accepted"] == 1)
        if accepted_count != 1:
            failures.append(f"expected exactly 1 current-accepted row, found {accepted_count}")
    finally:
        h.cleanup_db(mconn, mpath)
    return failures


# ---------------------------------------------------------------------------
# 7. Provider adapters (mocked at the HTTP/SDK boundary — no network calls)
# ---------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, status_code=200, json_body=None, text=""):
        self.status_code = status_code
        self._json_body = json_body
        self.text = text

    def json(self):
        return self._json_body


def test_ollama_adapter_malformed_output_is_failed_not_accepted():
    failures = []
    import requests

    def _fake_post(url, json=None, timeout=None):
        return _FakeResponse(200, {"message": {"content": "not valid json"}})

    orig_post = requests.post
    requests.post = _fake_post
    try:
        result, category, detail, latency = hai._call_ollama_structured(_Q, _WHY, _CTX, "some answer")
    finally:
        requests.post = orig_post

    if result is not None:
        failures.append("malformed output was NOT treated as a failure")
    if category != "malformed_output":
        failures.append(f"expected malformed_output category, got {category}")
    return failures


def test_ollama_adapter_schema_violation_is_failed():
    failures = []
    import requests

    def _fake_post(url, json=None, timeout=None):
        return _FakeResponse(200, {"message": {"content": '{"status": "nonsense", "claims": [], "reason": "x"}'}})

    orig_post = requests.post
    requests.post = _fake_post
    try:
        result, category, detail, latency = hai._call_ollama_structured(_Q, _WHY, _CTX, "some answer")
    finally:
        requests.post = orig_post

    if result is not None or category != "malformed_output":
        failures.append(f"schema-violating output not rejected as malformed_output: result={result} category={category}")
    return failures


def test_ollama_adapter_connection_error():
    failures = []
    import requests

    def _fake_post(url, json=None, timeout=None):
        raise requests.exceptions.ConnectionError("simulated connection failure")

    orig_post = requests.post
    requests.post = _fake_post
    try:
        result, category, detail, latency = hai._call_ollama_structured(_Q, _WHY, _CTX, "some answer")
    finally:
        requests.post = orig_post

    if result is not None or category != "connection_error":
        failures.append(f"connection error not classified correctly: result={result} category={category}")
    return failures


def test_semantic_check_unavailable_rejects_claim_not_whole_batch():
    failures = []
    mconn, mpath = _make_db()
    try:
        uid = _seed_answered_uncertainty(mconn, "family_s:200", "Alex confirmed the plan.", family="family_s")
        claim = _claim(subject="Alex", evidence_quote="Alex confirmed the plan")
        orig_structured = hai._call_ollama_structured
        orig_semantic = hai._call_ollama_semantic_check
        hai._call_ollama_structured = _mock_structured("supported", [claim])
        hai._call_ollama_semantic_check = _mock_semantic_check_failure()
        try:
            summary = hai.process_eligible_answers(mconn)
        finally:
            hai._call_ollama_structured = orig_structured
            hai._call_ollama_semantic_check = orig_semantic

        if summary["succeeded"] != 1:
            failures.append(f"expected succeeded (downgraded to insufficient_information) when semantic check is unavailable, got {summary}")
        attempts = hai.get_attempts_for_uncertainty(mconn, uid)
        if attempts[0]["universal_status"] != "insufficient_information" or attempts[0]["raw_status"] != "supported":
            failures.append(f"expected raw_status=supported preserved, universal_status downgraded: {dict(attempts[0])}")
        claims = hai.get_claims_for_interpretation(mconn, attempts[0]["id"])
        if not claims or claims[0]["accepted"] or "unavailable" not in (claims[0]["rejection_reason"] or ""):
            failures.append(f"claim rejection reason did not reflect semantic-check unavailability: {[dict(c) for c in claims]}")
    finally:
        h.cleanup_db(mconn, mpath)
    return failures


def test_provider_failure_does_not_break_reflection_batch():
    failures = []
    mconn, mpath = _make_db()
    try:
        _seed_answered_uncertainty(mconn, "family_p:300", "This is normal.", family="family_p")
        _seed_answered_uncertainty(mconn, "family_p:301", "This is normal too.", family="family_p")

        orig = hai._call_ollama_structured
        calls = {"n": 0}
        def _flaky(*a, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("boom — simulated unexpected error inside the adapter")
            return {"status": "insufficient_information", "claims": [], "reason": "x",
                    "model_identifier": "llama3.1:8b"}, None, None, 0.01
        hai._call_ollama_structured = _flaky
        try:
            summary = hai.process_eligible_answers(mconn)
        finally:
            hai._call_ollama_structured = orig

        if summary["processed"] + summary["skipped"] < 2:
            failures.append(f"batch did not continue past a per-row failure: {summary}")
        if summary["succeeded"] != 1:
            failures.append(f"expected the second (working) row to still succeed: {summary}")
    finally:
        h.cleanup_db(mconn, mpath)
    return failures


def test_gemini_fallback_disabled_by_default_and_capped():
    failures = []
    if hai.HEARTH_INTERPRETER_GEMINI_FALLBACK_ENABLED:
        failures.append("Gemini fallback must default to disabled")

    mconn, mpath = _make_db()
    try:
        uid = _seed_answered_uncertainty(mconn, "family_g:400", "Something ambiguous happened.", family="family_g")
        orig_ollama = hai._call_ollama_structured
        orig_gemini = hai._call_gemini_structured
        gemini_calls = {"n": 0}
        def _fake_gemini(*a, **k):
            gemini_calls["n"] += 1
            return {"status": "insufficient_information", "claims": [], "reason": "fallback",
                    "model_identifier": "gemini-test"}, None, None, 0.01
        claim = _claim(evidence_quote="text absent from the seeded answer")
        hai._call_ollama_structured = _mock_structured("supported", [claim])
        hai._call_gemini_structured = _fake_gemini
        try:
            hai.process_eligible_answers(mconn)
        finally:
            hai._call_ollama_structured = orig_ollama
            hai._call_gemini_structured = orig_gemini

        if gemini_calls["n"] != 0:
            failures.append("Gemini fallback was called despite being disabled by default")
        attempts = hai.get_attempts_for_uncertainty(mconn, uid)
        if attempts[0]["status"] != "succeeded" or attempts[0]["universal_status"] != "insufficient_information":
            failures.append(f"expected the row accepted as insufficient_information (all claims failed): {[dict(a) for a in attempts]}")
    finally:
        h.cleanup_db(mconn, mpath)
    return failures


def test_gemini_fallback_enabled_only_for_ambiguous():
    failures = []
    mconn, mpath = _make_db()
    try:
        _seed_answered_uncertainty(mconn, "family_g2:401", "Something ambiguous happened.", family="family_g2")
        orig_ollama = hai._call_ollama_structured
        orig_gemini = hai._call_gemini_structured
        orig_flag = hai.HEARTH_INTERPRETER_GEMINI_FALLBACK_ENABLED
        gemini_calls = {"n": 0}
        def _fake_gemini(*a, **k):
            gemini_calls["n"] += 1
            return {"status": "insufficient_information", "claims": [], "reason": "fallback",
                    "model_identifier": "gemini-test"}, None, None, 0.01
        claim = _claim(evidence_quote="text absent from the seeded answer")
        hai._call_ollama_structured = _mock_structured("supported", [claim])
        hai._call_gemini_structured = _fake_gemini
        hai.HEARTH_INTERPRETER_GEMINI_FALLBACK_ENABLED = True
        try:
            hai.process_eligible_answers(mconn)
        finally:
            hai._call_ollama_structured = orig_ollama
            hai._call_gemini_structured = orig_gemini
            hai.HEARTH_INTERPRETER_GEMINI_FALLBACK_ENABLED = orig_flag

        if gemini_calls["n"] != 1:
            failures.append(f"expected exactly 1 Gemini fallback call for a genuinely ambiguous row, got {gemini_calls['n']}")
    finally:
        h.cleanup_db(mconn, mpath)
    return failures


def test_ollama_disabled_path():
    failures = []
    mconn, mpath = _make_db()
    try:
        uid = _seed_answered_uncertainty(mconn, "family_od:600", "This is normal.", family="family_od")
        orig_flag = hai.HEARTH_INTERPRETER_OLLAMA_ENABLED
        orig_call = hai._call_ollama_structured
        called = {"n": 0}
        def _fail_if_called(*a, **k):
            called["n"] += 1
            return {"status": "supported", "claims": [], "reason": "x", "model_identifier": "x"}, None, None, 0.01
        hai._call_ollama_structured = _fail_if_called
        hai.HEARTH_INTERPRETER_OLLAMA_ENABLED = False
        try:
            summary = hai.process_eligible_answers(mconn)
        finally:
            hai.HEARTH_INTERPRETER_OLLAMA_ENABLED = orig_flag
            hai._call_ollama_structured = orig_call

        if called["n"] != 0:
            failures.append("Ollama was called despite HEARTH_INTERPRETER_OLLAMA_ENABLED=False")
        attempts = hai.get_attempts_for_uncertainty(mconn, uid)
        if len(attempts) != 1 or attempts[0]["status"] != "failed" or attempts[0]["failure_category"] != "provider_disabled":
            failures.append(f"expected a durable 'failed'/provider_disabled attempt, got {[dict(a) for a in attempts]}")
        if summary["enabled"] is not True:
            failures.append("subsystem-level enabled flag should remain True — only the provider is disabled")
    finally:
        h.cleanup_db(mconn, mpath)
    return failures


def test_whole_subsystem_disabled_path():
    failures = []
    mconn, mpath = _make_db()
    try:
        _seed_answered_uncertainty(mconn, "family_wd:601", "This is normal.", family="family_wd")
        orig_flag = hai.HEARTH_INTERPRETER_ENABLED
        orig_call = hai._call_ollama_structured
        called = {"n": 0}
        def _fail_if_called(*a, **k):
            called["n"] += 1
            return {"status": "supported", "claims": [], "reason": "x", "model_identifier": "x"}, None, None, 0.01
        hai._call_ollama_structured = _fail_if_called
        hai.HEARTH_INTERPRETER_ENABLED = False
        try:
            summary = hai.process_eligible_answers(mconn)
        finally:
            hai.HEARTH_INTERPRETER_ENABLED = orig_flag
            hai._call_ollama_structured = orig_call

        if called["n"] != 0:
            failures.append("Ollama was called despite HEARTH_INTERPRETER_ENABLED=False")
        if summary["eligible_found"] != 0 or summary["enabled"] is not False:
            failures.append(f"whole-subsystem disable did not short-circuit cleanly: {summary}")
        remaining = mconn.execute("SELECT COUNT(*) AS n FROM hearth_answer_interpretations;").fetchone()["n"]
        if remaining != 0:
            failures.append(f"expected 0 ledger rows with the subsystem fully disabled, found {remaining}")
    finally:
        h.cleanup_db(mconn, mpath)
    return failures


# ---------------------------------------------------------------------------
# 8. Aggregation — generic across generations of rows (Part 5)
# ---------------------------------------------------------------------------

def test_aggregate_family_generic_and_interpreter_version_param():
    failures = []
    mconn, mpath = _make_db()
    try:
        # A synthetic Build-1-shaped legacy accepted row (old `conclusion`
        # column populated, universal_status NULL) must still be counted.
        legacy_uid, _ = hearth_worldview.upsert_uncertainty(
            mconn, subject_type="entity_episode", subject_id="checkin_feedback_waiting:legacy1",
            uncertainty_text="legacy", possible_question="legacy question?",
            question_family="checkin_feedback_waiting", question_version=1,
        )
        mconn.execute(
            "UPDATE hearth_worldview_uncertainties SET status='answered', answer_text='legacy answer',"
            " answered_by='stacy', answered_at='2026-01-01T00:00:00Z' WHERE id=?;", (legacy_uid,),
        )
        mconn.execute(
            "INSERT INTO hearth_answer_interpretations (uncertainty_id, question_family, question_version,"
            " attempt_number, status, interpreter_version, schema_version, conclusion, is_current_accepted,"
            " attempted_at, created_at)"
            " VALUES (?, 'checkin_feedback_waiting', 1, 1, 'succeeded', 'checkin_feedback_waiting_v1', 'v1',"
            " 'expected_pattern', 1, 'x', 'x');",
            (legacy_uid,),
        )
        mconn.commit()

        uid2 = _seed_answered_uncertainty(mconn, "checkin_feedback_waiting:new1", "This keeps happening.",
                                           family="checkin_feedback_waiting")
        claim = _claim(evidence_quote="This keeps happening")
        orig = hai._call_ollama_structured
        orig_sem = hai._call_ollama_semantic_check
        hai._call_ollama_structured = _mock_structured("supported", [claim])
        hai._call_ollama_semantic_check = _mock_semantic_check(True)
        try:
            hai.process_eligible_answers(mconn)
        finally:
            hai._call_ollama_structured = orig
            hai._call_ollama_semantic_check = orig_sem

        report = hai.aggregate_family(mconn, "checkin_feedback_waiting", 1)
        if report["total_accepted"] != 2:
            failures.append(f"expected 2 accepted (1 legacy + 1 new), got {report['total_accepted']}")
        if "expected_pattern" not in report["by_label"] or "supported" not in report["by_label"]:
            failures.append(f"by_label did not span both generations: {report['by_label']}")

        # interpreter_version must come from the argument, never a
        # family-specific global — verify pending/attempted lookups shift.
        report_other_version = hai.aggregate_family(
            mconn, "checkin_feedback_waiting", 1, interpreter_version="some_other_version_nobody_used",
        )
        if report_other_version["total_pending"] < report["total_pending"]:
            failures.append("interpreter_version override did not affect pending/attempted lookup as expected")
    finally:
        h.cleanup_db(mconn, mpath)
    return failures


# ---------------------------------------------------------------------------
# 9. Simple structured grouping + durable learning candidates (Parts 10/11/15F)
# ---------------------------------------------------------------------------

def test_normalize_claim_group_key():
    failures = []
    k1 = hai.normalize_claim_group_key("Discord Requirement", "Optional", "This Creator")
    k2 = hai.normalize_claim_group_key("discord_requirement", "optional", "this_creator")
    if k1 != k2:
        failures.append(f"structurally equivalent predicate/value/scope did not normalize identically: {k1} vs {k2}")

    k3 = hai.normalize_claim_group_key("follow up owner", "alex", "this_onboarding")
    k4 = hai.normalize_claim_group_key("follow_up_owner", "alex", "this_onboarding")
    if k3 != k4:
        failures.append(f"separator variants did not normalize identically: {k3} vs {k4}")
    return failures


def _seed_accepted_claim(conn, subject_num, predicate, value, polarity="affirmed", scope="this_onboarding",
                          family="onboarding_engagement", version=1):
    uid = _seed_answered_uncertainty(
        conn, f"{family}:{subject_num}", f"Answer about subject {subject_num}.", family=family, version=version,
    )
    attempt_id = hai.create_pending_attempt(conn, uid, family, version, hai.HEARTH_INTERPRETER_VERSION)
    hai._finalize_attempt(conn, attempt_id, status="succeeded", raw_status="supported",
                           universal_status="supported", is_current_accepted=True)
    hai.store_claims(conn, attempt_id, uid, family, version, [{
        "subject": f"subject {subject_num}", "predicate": predicate, "value": value, "polarity": polarity,
        "scope": scope, "temporal_status": "current", "conclusion_text": f"{predicate}={value}",
        "evidence_quote": "evidence", "accepted": True, "rejection_reason": None,
    }])
    return uid


def test_learning_candidates_thresholds():
    failures = []
    mconn, mpath = _make_db()
    try:
        # 3 distinct subjects, same normalized predicate/value/polarity/scope -> candidate.
        for i in range(3):
            _seed_accepted_claim(mconn, i, "discord_requirement", "optional")
        surfaced = hai.compute_learning_candidates(mconn, min_interpretations=3, min_distinct_subjects=3)
        if not surfaced:
            failures.append("3 matching claims from 3 distinct subjects did not surface a candidate")
        candidates = hai.get_learning_candidates(mconn)
        matching = [c for c in candidates if c["predicate_key"] == "discord_requirement" and c["value_key"] == "optional"]
        if not matching or matching[0]["distinct_subject_count"] != 3:
            failures.append(f"candidate distinct_subject_count wrong: {[dict(c) for c in candidates]}")

        # Repeated claims from ONE subject alone must never satisfy the
        # distinct-subject threshold, even with many interpretations.
        for i in range(5):
            _seed_accepted_claim(mconn, "same_subject", f"single_subject_predicate_{i}", "x")
        # Reuse the SAME subject repeatedly with the SAME predicate/value:
        mconn2 = mconn
        for _ in range(5):
            uid = _seed_answered_uncertainty(mconn2, "onboarding_engagement:same_subject_2",
                                              "answer", family="onboarding_engagement")
            attempt_id = hai.create_pending_attempt(mconn2, uid, "onboarding_engagement", 1, hai.HEARTH_INTERPRETER_VERSION)
            hai._finalize_attempt(mconn2, attempt_id, status="succeeded", raw_status="supported",
                                   universal_status="supported", is_current_accepted=True)
            hai.store_claims(mconn2, attempt_id, uid, "onboarding_engagement", 1, [{
                "subject": "same subject", "predicate": "repeat_predicate", "value": "repeat_value",
                "polarity": "affirmed", "scope": "this_onboarding", "temporal_status": "current",
                "conclusion_text": "x", "evidence_quote": "evidence", "accepted": True, "rejection_reason": None,
            }])
        hai.compute_learning_candidates(mconn, min_interpretations=3, min_distinct_subjects=3)
        one_subject_candidates = [
            c for c in hai.get_learning_candidates(mconn)
            if c["predicate_key"] == "repeat_predicate"
        ]
        if one_subject_candidates:
            failures.append("repeated claims from ONE subject alone incorrectly satisfied the distinct-subject threshold")

        # Opposite polarity / contradictory values must never group together.
        for i in range(3):
            _seed_accepted_claim(mconn, f"pol_{i}", "requirement_x", "required", polarity="affirmed")
        for i in range(3):
            _seed_accepted_claim(mconn, f"pol_neg_{i}", "requirement_x", "required", polarity="negated")
        hai.compute_learning_candidates(mconn, min_interpretations=3, min_distinct_subjects=3)
        req_candidates = [c for c in hai.get_learning_candidates(mconn) if c["predicate_key"] == "requirement_x"]
        if len(req_candidates) != 2:
            failures.append(f"opposite polarity was not kept in separate groups: {[dict(c) for c in req_candidates]}")
    finally:
        h.cleanup_db(mconn, mpath)
    return failures


def test_learning_candidate_status_never_auto_promoted():
    failures = []
    mconn, mpath = _make_db()
    try:
        for i in range(3):
            _seed_accepted_claim(mconn, i, "status_test_pred", "status_test_val")
        hai.compute_learning_candidates(mconn, min_interpretations=3, min_distinct_subjects=3)
        candidate = hai.get_learning_candidates(mconn, question_family="onboarding_engagement")[0]
        if candidate["status"] != "pending":
            failures.append(f"new candidate must default to 'pending', got {candidate['status']!r}")

        hai.set_learning_candidate_status(mconn, candidate["id"], "reviewed")
        # Recomputing again (idempotent Reflection) must not reset a human review decision.
        hai.compute_learning_candidates(mconn, min_interpretations=3, min_distinct_subjects=3)
        candidate2 = hai.get_learning_candidates(mconn, question_family="onboarding_engagement")[0]
        if candidate2["status"] != "reviewed":
            failures.append("recompute reset a human-reviewed candidate's status back to pending")
    finally:
        h.cleanup_db(mconn, mpath)
    return failures


# ---------------------------------------------------------------------------
# 10. Backfill script (Part 9 / 15E)
# ---------------------------------------------------------------------------

def test_backfill_script_dry_run_apply_idempotent():
    failures = []
    mconn, mpath = _make_db()
    try:
        import backfill_question_family_stamps as backfill

        cur = mconn.execute(
            "INSERT INTO hearth_entities (display_name, entity_type, created_at) VALUES (?, 'person', 'x');",
            ("BACKFILL_TEST_creator",),
        )
        entity_id = cur.lastrowid
        mconn.commit()

        # Living, unstamped row matching the entity_episode:episode_type shape.
        uid_mappable, _ = hearth_worldview.upsert_uncertainty(
            mconn, subject_type="entity_episode", subject_id=f"missing_discord:{entity_id}",
            uncertainty_text="unstamped historical row",
        )
        # Living, unstamped aggregate-pattern row (subject_type='entity', digit subject_id).
        uid_concern_volume, _ = hearth_worldview.upsert_uncertainty(
            mconn, subject_type="entity", subject_id=str(entity_id),
            uncertainty_text="unstamped concern-volume row",
        )
        # Ambiguous row that must be skipped, not guessed.
        uid_ambiguous, _ = hearth_worldview.upsert_uncertainty(
            mconn, subject_type="entity", subject_id="not_a_digit_subject_id",
            uncertainty_text="cannot be safely mapped",
        )
        # Resolved (non-living) row must not be touched even though it's mappable.
        uid_resolved, _ = hearth_worldview.upsert_uncertainty(
            mconn, subject_type="entity_episode", subject_id=f"new_creator_stuck:{entity_id}",
            uncertainty_text="resolved historical row",
        )
        mconn.execute("UPDATE hearth_worldview_uncertainties SET status='resolved' WHERE id=?;", (uid_resolved,))
        mconn.commit()

        plan = backfill.plan_backfill(mconn)
        mapped_ids = {u[0] for u in plan["to_update"]}
        if uid_mappable not in mapped_ids:
            failures.append("mappable entity_episode row not included in backfill plan")
        if uid_concern_volume not in mapped_ids:
            failures.append("mappable entity (recent_concern_volume) row not included in backfill plan")
        if uid_ambiguous in mapped_ids:
            failures.append("ambiguous row was incorrectly included in the backfill plan")
        if uid_resolved in mapped_ids:
            failures.append("resolved (non-living) row was incorrectly included in the backfill plan")

        # Dry run changes nothing.
        row_before = hearth_worldview.get_uncertainty(mconn, uid_mappable)
        if row_before["question_family"] is not None:
            failures.append("test setup invalid: row already stamped before dry run")

        backfill.apply_backfill(mconn, plan)
        row_after = hearth_worldview.get_uncertainty(mconn, uid_mappable)
        if row_after["question_family"] != "missing_discord" or row_after["question_version"] != 1:
            failures.append(f"apply did not correctly stamp the mappable row: {dict(row_after)}")
        row_cv_after = hearth_worldview.get_uncertainty(mconn, uid_concern_volume)
        if row_cv_after["question_family"] != "recent_concern_volume":
            failures.append(f"apply did not correctly stamp the recent_concern_volume row: {dict(row_cv_after)}")

        # Idempotent: second apply finds nothing left to change.
        plan2 = backfill.plan_backfill(mconn)
        if plan2["to_update"]:
            failures.append(f"backfill was not idempotent — rows remained after apply: {plan2['to_update']}")

        # Never overwrites an existing non-null stamp — simulate by re-running
        # against a row that already has a (different, hypothetical) stamp.
        already_stamped_uid, _ = hearth_worldview.upsert_uncertainty(
            mconn, subject_type="entity_episode", subject_id=f"missing_discord:{entity_id}_two",
            uncertainty_text="already stamped", question_family="checkin_feedback_waiting", question_version=1,
        )
        plan3 = backfill.plan_backfill(mconn)
        if already_stamped_uid in {u[0] for u in plan3["to_update"]}:
            failures.append("backfill plan included an already-stamped row")
    finally:
        h.cleanup_db(mconn, mpath)
    return failures


# ---------------------------------------------------------------------------
# 11. Reflection wiring (structural)
# ---------------------------------------------------------------------------

def test_reflection_wiring_structural():
    failures = []
    if morning_briefing.hearth_answer_interpreter is not hai:
        failures.append("morning_briefing does not import the real hearth_answer_interpreter module")

    source = inspect.getsource(morning_briefing.run_pipeline)
    if "hearth_answer_interpreter.process_eligible_answers(memory_conn)" not in source:
        failures.append("run_pipeline() does not call hearth_answer_interpreter.process_eligible_answers()")
    if "hearth_answer_interpreter.compute_learning_candidates(memory_conn)" not in source:
        failures.append("run_pipeline() does not call hearth_answer_interpreter.compute_learning_candidates()")

    call_idx = source.find("hearth_answer_interpreter.process_eligible_answers(memory_conn)")
    resolve_idx = source.find("resolve_cleared_worldview_questions(memory_conn)")
    if call_idx == -1 or resolve_idx == -1 or call_idx < resolve_idx:
        failures.append("interpreter batch is not positioned after the worldview-questions Reflection steps")

    try_idx = source.rfind("try:", 0, call_idx)
    except_idx = source.find("except Exception", call_idx)
    if try_idx == -1 or except_idx == -1 or try_idx > call_idx:
        failures.append("interpreter batch call in run_pipeline() is not wrapped in a try/except")

    candidates_idx = source.find("hearth_answer_interpreter.compute_learning_candidates(memory_conn)")
    candidates_try_idx = source.rfind("try:", 0, candidates_idx)
    candidates_except_idx = source.find("except Exception", candidates_idx)
    if candidates_try_idx == -1 or candidates_except_idx == -1 or candidates_try_idx > candidates_idx:
        failures.append("compute_learning_candidates() call is not wrapped in a try/except")

    import subprocess
    diff = subprocess.run(
        ["git", "diff", "--stat", "HEAD", "--", "hearth_ask.py"],
        cwd=os.path.dirname(os.path.abspath(__file__)), capture_output=True, text=True,
    )
    if diff.stdout.strip():
        failures.append(f"hearth_ask.py has uncommitted changes: {diff.stdout}")

    return failures


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def main():
    suites = [
        ("schema_and_migration", test_schema_and_migration),
        ("schema_missing_graceful_degradation", test_schema_missing_graceful_degradation),
        ("fresh_database_startup_sequence_matches_morning_briefing", test_fresh_database_startup_sequence_matches_morning_briefing),
        ("family_stamping_single_episode_types", test_family_stamping_single_episode_types),
        ("family_stamping_recent_concern_volume", test_family_stamping_recent_concern_volume),
        ("deterministic_validate_status_consistency", test_deterministic_validate_status_consistency),
        ("deterministic_validate_claim_count", test_deterministic_validate_claim_count),
        ("deterministic_validate_evidence_and_duplicates", test_deterministic_validate_evidence_and_duplicates),
        ("evidence_quote_case_insensitive_not_paraphrase", test_evidence_quote_case_insensitive_not_paraphrase),
        ("deterministic_validate_entity_grounding", test_deterministic_validate_entity_grounding),
        ("deterministic_validate_negation", test_deterministic_validate_negation),
        ("short_answers_binary_vs_explanatory", test_short_answers_binary_vs_explanatory),
        ("family_independence_including_synthetic_future_family", test_family_independence_including_synthetic_future_family),
        ("one_familys_failure_does_not_block_another", test_one_familys_failure_does_not_block_another),
        ("eligibility_family_agnostic", test_eligibility_family_agnostic),
        ("batch_size_bound", test_batch_size_bound),
        ("ledger_lifecycle_success_not_reclassified", test_ledger_lifecycle_success_not_reclassified),
        ("zero_surviving_claims_one_proposed_rejected", test_zero_surviving_claims_one_proposed_rejected),
        ("zero_surviving_claims_multiple_proposed_all_rejected", test_zero_surviving_claims_multiple_proposed_all_rejected),
        ("zero_surviving_claims_mixed_valid_and_rejected", test_zero_surviving_claims_mixed_valid_and_rejected),
        ("zero_surviving_claims_aggregation_and_admin_display", test_zero_surviving_claims_aggregation_and_admin_display),
        ("ledger_rejects_contract_violations", test_ledger_rejects_contract_violations),
        ("ledger_retry_policy_and_new_interpreter_version", test_ledger_retry_policy_and_new_interpreter_version),
        ("ollama_adapter_malformed_output_is_failed_not_accepted", test_ollama_adapter_malformed_output_is_failed_not_accepted),
        ("ollama_adapter_schema_violation_is_failed", test_ollama_adapter_schema_violation_is_failed),
        ("ollama_adapter_connection_error", test_ollama_adapter_connection_error),
        ("semantic_check_unavailable_rejects_claim_not_whole_batch", test_semantic_check_unavailable_rejects_claim_not_whole_batch),
        ("provider_failure_does_not_break_reflection_batch", test_provider_failure_does_not_break_reflection_batch),
        ("gemini_fallback_disabled_by_default_and_capped", test_gemini_fallback_disabled_by_default_and_capped),
        ("gemini_fallback_enabled_only_for_ambiguous", test_gemini_fallback_enabled_only_for_ambiguous),
        ("ollama_disabled_path", test_ollama_disabled_path),
        ("whole_subsystem_disabled_path", test_whole_subsystem_disabled_path),
        ("aggregate_family_generic_and_interpreter_version_param", test_aggregate_family_generic_and_interpreter_version_param),
        ("normalize_claim_group_key", test_normalize_claim_group_key),
        ("learning_candidates_thresholds", test_learning_candidates_thresholds),
        ("learning_candidate_status_never_auto_promoted", test_learning_candidate_status_never_auto_promoted),
        ("backfill_script_dry_run_apply_idempotent", test_backfill_script_dry_run_apply_idempotent),
        ("reflection_wiring_structural", test_reflection_wiring_structural),
    ]

    total_failures = []
    for name, fn in suites:
        print(f"\n{'#' * 78}\n# {name}\n{'#' * 78}")
        failures = fn()
        if failures:
            for f in failures:
                print(f"  [FAIL] {f}")
            total_failures.extend((name, f) for f in failures)
        else:
            print("  [OK]")

    print(f"\n{'=' * 78}")
    if total_failures:
        print(f"RESULT: {len(total_failures)} failure(s) across {len(suites)} suites")
        for name, f in total_failures:
            print(f"  - [{name}] {f}")
        raise SystemExit(1)
    else:
        print(f"RESULT: all {len(suites)} suites passed")


if __name__ == "__main__":
    main()
