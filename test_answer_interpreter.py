"""
Tests for Build 1 of the manager-answer-to-Worldview learning loop:
question-family stamping (hearth_soul.py / hearth_worldview.py), the durable
interpretation ledger, deterministic validation, eligibility/retry policy,
mocked provider adapters, aggregation, and the Reflection wiring point in
morning_briefing.py.

Never touches the real dev hearth_memory.db — uses isolated temp-file
databases via experience_evaluator_test_helpers.make_memory_db(), same
convention as test_question_auto_resolution.py and the
test_experience_evaluator_*.py suite.

No real Ollama or Gemini calls are made anywhere in this file — provider
calls are monkeypatched. See test_answer_interpreter_local_model_eval.py for
the separate, explicitly-invoked real-Ollama evaluation.

Run: venv/bin/python3 test_answer_interpreter.py
"""

import inspect
import sqlite3
import tempfile
import os

import experience_evaluator_test_helpers as h
import hearth_answer_interpreter as hai
import hearth_soul
import hearth_worldview
import migrate_add_answer_interpretations as ledger_migration
import migrate_add_question_family_fields as family_migration
import migrate_add_uncertainty_answer_fields as answer_fields_migration
import morning_briefing


def _make_db():
    """Fully migrated temp hearth_memory-shaped db, plus the answer-fields
    and interpretation-ledger migrations this build adds.
    """
    mconn, mpath = h.make_memory_db()
    answer_fields_migration.migrate(mconn)
    family_migration.migrate(mconn)  # no-op on a fresh db; ensure_worldview_tables already added the columns
    ledger_migration.migrate(mconn)
    return mconn, mpath


def _seed_answered_uncertainty(conn, subject_id, answer_text, family="checkin_feedback_waiting",
                                version=1, answered_by="stacy", answered_at="2026-07-20T00:00:00Z"):
    uid, _created = hearth_worldview.upsert_uncertainty(
        conn, subject_type="entity_episode", subject_id=subject_id,
        uncertainty_text=f"It is unclear whether the checkin feedback waiting episode for {subject_id} reflects a meaningful pattern or an isolated event.",
        possible_question=f"Is the checkin feedback waiting episode for {subject_id} part of a larger pattern?",
        question_family=family, question_version=version,
    )
    conn.execute(
        "UPDATE hearth_worldview_uncertainties SET status='answered', answer_text=?,"
        " answered_by=?, answered_at=? WHERE id=?;",
        (answer_text, answered_by, answered_at, uid),
    )
    conn.commit()
    return uid


# ---------------------------------------------------------------------------
# 1. Schema / migration
# ---------------------------------------------------------------------------

def test_schema_and_migration():
    failures = []

    # Fresh DB via ensure_worldview_tables() includes question_family/version directly.
    fresh_conn = sqlite3.connect(tempfile.mktemp(suffix=".db"))
    fresh_conn.row_factory = sqlite3.Row
    hearth_worldview.ensure_worldview_tables(fresh_conn)
    cols = {r["name"] for r in fresh_conn.execute("PRAGMA table_info(hearth_worldview_uncertainties);")}
    if not ({"question_family", "question_version"} <= cols):
        failures.append("fresh DB via ensure_worldview_tables() missing question_family/question_version")
    fresh_conn.close()

    # Migration on an "older" schema (base migration only): columns absent
    # before, present + NULL after, idempotent on rerun.
    import migrate_add_hearth_worldview as base_migration
    old_conn = sqlite3.connect(tempfile.mktemp(suffix=".db"))
    old_conn.row_factory = sqlite3.Row
    base_migration.migrate(old_conn)
    old_conn.execute(
        "INSERT INTO hearth_worldview_uncertainties (subject_type, subject_id, uncertainty_text, created_at, updated_at)"
        " VALUES ('entity_episode', 'checkin_feedback_waiting:999', 'pre-existing row', 'x', 'x');"
    )
    old_conn.commit()
    cols_before = {r["name"] for r in old_conn.execute("PRAGMA table_info(hearth_worldview_uncertainties);")}
    if "question_family" in cols_before:
        failures.append("test setup invalid: base migration already has question_family")
    family_migration.migrate(old_conn)
    family_migration.migrate(old_conn)  # idempotency
    row = old_conn.execute(
        "SELECT question_family, question_version FROM hearth_worldview_uncertainties"
        " WHERE subject_id = 'checkin_feedback_waiting:999';"
    ).fetchone()
    if row["question_family"] is not None or row["question_version"] is not None:
        failures.append("historical row did not remain NULL after migration")
    old_conn.close()

    # Ledger table: idempotent creation + partial-unique current-accepted index.
    ledger_conn = sqlite3.connect(tempfile.mktemp(suffix=".db"))
    ledger_conn.row_factory = sqlite3.Row
    ledger_migration.migrate(ledger_conn)
    ledger_migration.migrate(ledger_conn)
    ledger_conn.execute(
        "INSERT INTO hearth_answer_interpretations (uncertainty_id, question_family, question_version,"
        " attempt_number, status, interpreter_version, schema_version, is_current_accepted, attempted_at, created_at)"
        " VALUES (1,'checkin_feedback_waiting',1,1,'succeeded','v1','v1',1,'x','x');"
    )
    ledger_conn.commit()
    try:
        ledger_conn.execute(
            "INSERT INTO hearth_answer_interpretations (uncertainty_id, question_family, question_version,"
            " attempt_number, status, interpreter_version, schema_version, is_current_accepted, attempted_at, created_at)"
            " VALUES (1,'checkin_feedback_waiting',1,2,'succeeded','v2','v1',1,'x','x');"
        )
        ledger_conn.commit()
        failures.append("partial unique index did not reject a second current-accepted row")
    except sqlite3.IntegrityError:
        pass
    ledger_conn.close()

    return failures


def test_schema_missing_graceful_degradation():
    """Runtime must never auto-ALTER an un-migrated database — it should
    detect the missing schema, log/print a clear warning, and degrade
    gracefully (write side: insert without identity metadata; read side:
    skip the interpretation batch entirely), never crash Reflection.
    """
    failures = []
    import migrate_add_hearth_worldview as base_migration
    import migrate_add_uncertainty_last_seen_at as last_seen_migration
    import migrate_add_worldview_source_run as source_run_migration

    # A database with every OTHER migration already applied (realistic —
    # every real deployed hearth_memory.db has run these) but neither of
    # Build 1's two new migrations yet. This is the actual scenario to
    # guard against: an existing, already-in-production database that
    # simply hasn't been upgraded for Build 1 yet.
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

    # Write side: a normal (family=None) call must work exactly as before —
    # no warning, no crash, regardless of schema state.
    try:
        uid, _ = hearth_worldview.upsert_uncertainty(
            conn, subject_type="entity_episode", subject_id="missing_discord:1",
            uncertainty_text="unrelated type, no family requested",
        )
    except Exception as exc:
        failures.append(f"family=None insert must never fail on an un-migrated DB: {exc}")

    # Write side: a call that DOES want to stamp identity, on a DB missing
    # the columns, must still succeed (row created) rather than raising —
    # degrade gracefully, don't silently ALTER.
    try:
        uid2, _ = hearth_worldview.upsert_uncertainty(
            conn, subject_type="entity_episode", subject_id="checkin_feedback_waiting:1",
            uncertainty_text="wants family stamping but columns are missing",
            question_family="checkin_feedback_waiting", question_version=1,
        )
    except Exception as exc:
        failures.append(f"family stamping request must degrade gracefully, not raise: {exc}")
    else:
        cols_after = {r["name"] for r in conn.execute("PRAGMA table_info(hearth_worldview_uncertainties);")}
        if "question_family" in cols_after:
            failures.append(
                "hearth_worldview must NOT auto-ALTER the table at runtime —"
                " columns appeared after a write that requested stamping"
            )

    # Read side: process_eligible_answers() must detect the missing schema,
    # report it, and return without ever creating the ledger table itself.
    summary = hai.process_eligible_answers(conn)
    if not summary.get("schema_missing"):
        failures.append(f"expected schema_missing=True on an un-migrated DB, got {summary}")
    if summary["eligible_found"] != 0 or summary["processed"] != 0:
        failures.append(f"no eligibility query/processing should happen when schema is missing: {summary}")
    if hai._ledger_table_exists(conn):
        failures.append(
            "process_eligible_answers() must not create hearth_answer_interpretations"
            " itself — that's ensure_answer_interpretations_table()'s job, called at"
            " pipeline startup, not from inside the interpretation batch"
        )

    conn.close()
    return failures


def test_fresh_database_startup_sequence_matches_morning_briefing():
    """Mirrors morning_briefing.run_pipeline()'s exact startup call order —
    a genuinely fresh database should be immediately schema-ready afterward,
    with no separate migration step required.
    """
    failures = []
    conn = sqlite3.connect(tempfile.mktemp(suffix=".db"))
    conn.row_factory = sqlite3.Row
    import hearth_memory
    hearth_memory.init_tables(conn)
    hearth_soul.ensure_reflections_table(conn)
    import hearth_questions
    hearth_questions.ensure_questions_table(conn)
    hai.ensure_answer_interpretations_table(conn)
    # ensure_worldview_tables() itself is called inside reflect_on_worldview(),
    # not directly in run_pipeline()'s startup block — call it the same way here.
    hearth_worldview.ensure_worldview_tables(conn)

    if not hai._schema_ready(conn):
        failures.append("fresh database following morning_briefing's exact startup order is not schema-ready")

    conn.close()
    return failures


# ---------------------------------------------------------------------------
# 2. Question-family stamping at the generator
# ---------------------------------------------------------------------------

def test_family_stamping():
    failures = []
    mconn, mpath = _make_db()
    try:
        entity = h.make_memory_db  # placeholder to keep flake tools quiet; real entity created below
        cur = mconn.execute(
            "INSERT INTO hearth_entities (display_name, entity_type, created_at) VALUES (?, 'person', 'x');",
            ("STAMP_TEST_creator",),
        )
        entity_id = cur.lastrowid
        mconn.commit()

        # checkin_feedback_waiting -> stamped
        uid1, created1 = hearth_soul._upsert_single_episode_uncertainty(
            mconn, "checkin_feedback_waiting", entity_id, "manual_test",
        )
        row1 = hearth_worldview.get_uncertainty(mconn, uid1)
        if row1["question_family"] != "checkin_feedback_waiting" or row1["question_version"] != 1:
            failures.append(
                f"checkin_feedback_waiting not stamped correctly: family={row1['question_family']!r} version={row1['question_version']!r}"
            )

        # missing_discord shares the same generator but must stay NULL
        uid2, created2 = hearth_soul._upsert_single_episode_uncertainty(
            mconn, "missing_discord", entity_id, "manual_test",
        )
        row2 = hearth_worldview.get_uncertainty(mconn, uid2)
        if row2["question_family"] is not None or row2["question_version"] is not None:
            failures.append("missing_discord (unrelated shared-generator type) was incorrectly stamped")

        # support_request_waiting also shares the generator — same check
        uid3, created3 = hearth_soul._upsert_single_episode_uncertainty(
            mconn, "support_request_waiting", entity_id, "manual_test",
        )
        row3 = hearth_worldview.get_uncertainty(mconn, uid3)
        if row3["question_family"] is not None:
            failures.append("support_request_waiting (unrelated shared-generator type) was incorrectly stamped")

        # Refresh path: re-observing the same living checkin_feedback_waiting
        # uncertainty must not change/clear its stamped family/version.
        uid1b, created1b = hearth_soul._upsert_single_episode_uncertainty(
            mconn, "checkin_feedback_waiting", entity_id, "manual_test_2",
        )
        if uid1b != uid1 or created1b:
            failures.append("expected refresh (same living row), got a new row")
        row1b = hearth_worldview.get_uncertainty(mconn, uid1b)
        if row1b["question_family"] != "checkin_feedback_waiting" or row1b["question_version"] != 1:
            failures.append("family/version lost across a refresh of the same living row")

        # A pre-existing (pre-Build-1-style) NULL-family living row must NOT
        # get backfilled just because it's re-observed by upsert_uncertainty.
        pre_uid, _ = hearth_worldview.upsert_uncertainty(
            mconn, subject_type="entity_episode", subject_id=f"checkin_feedback_waiting:{entity_id}_legacy",
            uncertainty_text="legacy pre-Build-1 row",
        )
        hearth_worldview.upsert_uncertainty(
            mconn, subject_type="entity_episode", subject_id=f"checkin_feedback_waiting:{entity_id}_legacy",
            uncertainty_text="legacy pre-Build-1 row, refreshed",
            question_family="checkin_feedback_waiting", question_version=1,
        )
        pre_row = hearth_worldview.get_uncertainty(mconn, pre_uid)
        if pre_row["question_family"] is not None:
            failures.append("refresh path incorrectly backfilled family onto a pre-existing NULL row")

    finally:
        h.cleanup_db(mconn, mpath)
    return failures


# ---------------------------------------------------------------------------
# 3. Deterministic validator — mandatory dataset + guardrails
# ---------------------------------------------------------------------------

_VALIDATOR_CASES = [
    # (answer, model_raw_conclusion, model_raw_reason, expected_conclusion, expected_status)
    #
    # These test answers to the ACTUAL generated question — "is this part of
    # a LARGER PATTERN" — not the nearby-but-different "is this delay normal
    # or concerning" question. See the semantic-contract review notes above
    # FAMILY_DEFINITIONS/the trigger vocabulary for why several of Build 1's
    # original examples ("overdue", duration-based "concern") were replaced:
    # being overdue or long-waited does not by itself establish recurrence.

    # expected_pattern — isolated / not part of a larger pattern
    ("No, this is an isolated delay.", "expected_pattern", "This is a one-off, isolated delay.", "expected_pattern", "succeeded"),
    ("Managers just need a little more time to finish the feedback.", "expected_pattern", "The manager just needs more time.", "expected_pattern", "succeeded"),
    ("This is normal; the check-in was only submitted yesterday.", "expected_pattern", "This is normal.", "expected_pattern", "succeeded"),
    ("Nothing unusual here yet.", "expected_pattern", "Nothing unusual.", "expected_pattern", "succeeded"),
    ("Managers just need more time.", "expected_pattern", "Managers just need more time.", "expected_pattern", "succeeded"),

    # meaningful_signal — yes, this IS part of a larger pattern
    ("Yes, this manager is behind on several check-ins.", "meaningful_signal", "This manager is behind on several check-ins.", "meaningful_signal", "succeeded"),
    ("The manager has repeatedly failed to complete these.", "meaningful_signal", "Repeated failure.", "meaningful_signal", "succeeded"),
    ("This keeps happening with this manager.", "meaningful_signal", "This keeps happening.", "meaningful_signal", "succeeded"),
    ("This has happened multiple times before.", "meaningful_signal", "It has happened multiple times before.", "meaningful_signal", "succeeded"),

    # context_dependent — whether it's a pattern depends on something specific
    ("It depends on how many other check-ins this manager currently has outstanding.", "context_dependent", "Depends on how many other check-ins are outstanding.", "context_dependent", "succeeded"),
    ("It depends on how long it has been waiting.", "context_dependent", "Depends on timing.", "context_dependent", "succeeded"),
    ("A few days is fine, but more than a week needs follow-up.", "context_dependent", "Depends on how long.", "context_dependent", "succeeded"),

    # insufficient_information — manager doesn't yet know if it's a pattern
    ("It may be; I need to check their other outstanding feedback.", "insufficient_information", "Need to check their other outstanding feedback.", "insufficient_information", "succeeded"),
    ("I cannot tell yet whether this is happening elsewhere.", "insufficient_information", "Cannot tell yet whether this is happening elsewhere.", "insufficient_information", "succeeded"),
    ("I need to check when it was submitted.", "insufficient_information", "Needs more info.", "insufficient_information", "succeeded"),
    ("I can't tell without knowing how old the request is.", "insufficient_information", "Not enough info.", "insufficient_information", "succeeded"),
    ("Let's wait another day before deciding.", "insufficient_information", "Need to wait.", "insufficient_information", "succeeded"),

    # unrelated_or_unclear — off-topic, empty, or internally incoherent
    ("The creator missed yesterday's battle.", "unrelated_or_unclear", "Unrelated to check-in feedback.", "unrelated_or_unclear", "succeeded"),
    ("Ask Sarah about the payroll report.", "expected_pattern", "Unrelated.", "unrelated_or_unclear", "succeeded"),
    ("The battle schedule needs updated.", "unrelated_or_unclear", "Off topic.", "unrelated_or_unclear", "succeeded"),
    ("", "expected_pattern", "N/A", "unrelated_or_unclear", "succeeded"),
    ("   ", "expected_pattern", "N/A", "unrelated_or_unclear", "succeeded"),
    ("This is an isolated delay, but honestly this manager has been behind on several check-ins before.", "expected_pattern", "Mixed signals.", "unrelated_or_unclear", "succeeded"),

    # bare/short answers — unsafe regardless of literal yes/no content: no
    # independent semantic evidence to ground future organizational learning
    ("yes", "expected_pattern", "Confirmed.", "insufficient_information", "succeeded"),
    ("no", "meaningful_signal", "Denied.", "insufficient_information", "succeeded"),
    ("maybe", "context_dependent", "Unclear.", "insufficient_information", "succeeded"),
    ("I don't know", "insufficient_information", "Unknown.", "insufficient_information", "succeeded"),
    ("not sure", "unrelated_or_unclear", "Unclear.", "insufficient_information", "succeeded"),
    ("wait", "insufficient_information", "Wait.", "insufficient_information", "succeeded"),

    # non-bare boundary case: substantive despite being short — evaluated
    # against real evidence, not the bare-answer shortcut
    ("they need more time", "expected_pattern", "Needs more time.", "expected_pattern", "succeeded"),
]

_Q = "Is the checkin feedback waiting episode for a creator part of a larger pattern?"


def test_validator_mandatory_dataset():
    failures = []
    for answer, raw_conc, raw_reason, expected_conclusion, expected_status in _VALIDATOR_CASES:
        out = hai.validate_interpretation(_Q, answer, raw_conc, raw_reason)
        if out.status != expected_status or out.conclusion != expected_conclusion:
            failures.append(
                f"answer={answer!r} raw_conclusion={raw_conc!r} -> got"
                f" status={out.status} conclusion={out.conclusion}, expected"
                f" status={expected_status} conclusion={expected_conclusion}"
                f" (reason={out.validation_reason!r})"
            )
    return failures


def test_validator_guardrails():
    failures = []

    # Model self-contradiction (conclusion vs its own reason) -> rejected, never accepted.
    out = hai.validate_interpretation(
        _Q, "The manager will look at it soon.", "expected_pattern",
        "This has happened before with this manager and keeps happening.",
    )
    if out.status != "rejected_by_validation" or out.conclusion is not None:
        failures.append(f"self-contradiction case not rejected: {out.status}/{out.conclusion}")

    # Hallucinated conclusion outside the closed taxonomy -> rejected, never accepted.
    out2 = hai.validate_interpretation(_Q, "This is normal.", "totally_fine", "It's fine.")
    if out2.status != "rejected_by_validation" or out2.conclusion is not None:
        failures.append(f"out-of-taxonomy conclusion not rejected: {out2.status}/{out2.conclusion}")

    # On-topic but genuinely unresolvable -> ambiguous, never a substantive label.
    out3 = hai.validate_interpretation(
        _Q, "The manager mentioned something during the meeting.", "expected_pattern",
        "Mentioned during meeting.",
    )
    if out3.status != "ambiguous" or out3.conclusion is not None:
        failures.append(f"unresolvable on-topic case not marked ambiguous: {out3.status}/{out3.conclusion}")

    # High model confidence must never bypass validation — confidence isn't
    # even a parameter to validate_interpretation(); assert that directly.
    sig = inspect.signature(hai.validate_interpretation)
    if "confidence" in sig.parameters:
        failures.append("validate_interpretation() must not accept confidence as an input at all")

    # Unrelated answer must not become expected_pattern merely because the
    # model claims it is.
    out4 = hai.validate_interpretation(_Q, "The battle schedule needs updated.", "expected_pattern", "It's expected.")
    if out4.conclusion != "unrelated_or_unclear":
        failures.append(f"unrelated answer was force-fit into {out4.conclusion!r} despite model's claim")

    return failures


# ---------------------------------------------------------------------------
# 4. Ledger lifecycle + eligibility + orchestration (mocked provider)
# ---------------------------------------------------------------------------

def _mock_ollama(conclusion, confidence, reason):
    def _fake(*a, **k):
        return (
            {"conclusion": conclusion, "confidence": confidence, "reason": reason, "model_identifier": "llama3.1:8b"},
            None, None, 0.01,
        )
    return _fake


def _mock_ollama_failure(category, detail="synthetic failure"):
    def _fake(*a, **k):
        return None, category, detail, 0.01
    return _fake


def test_ledger_lifecycle_success_not_reclassified():
    failures = []
    mconn, mpath = _make_db()
    try:
        uid = _seed_answered_uncertainty(mconn, "checkin_feedback_waiting:100", "This is normal, they just need more time.")
        orig = hai._call_ollama_structured
        hai._call_ollama_structured = _mock_ollama("expected_pattern", 0.9, "Normal, needs more time.")
        try:
            summary = hai.process_eligible_answers(mconn)
        finally:
            hai._call_ollama_structured = orig

        if summary["succeeded"] != 1:
            failures.append(f"expected 1 succeeded, got {summary}")

        accepted = hai.get_current_accepted(mconn, uid)
        if accepted is None or accepted["conclusion"] != "expected_pattern":
            failures.append("accepted interpretation not recorded correctly")
        if accepted["attempt_number"] != 1:
            failures.append("expected first attempt to be attempt_number=1")

        # Re-run: must not reclassify a successful row (no new attempt, no model call).
        call_count = {"n": 0}
        def _counting_fake(*a, **k):
            call_count["n"] += 1
            return {"conclusion": "meaningful_signal", "confidence": 0.9, "reason": "different", "model_identifier": "llama3.1:8b"}, None, None, 0.01
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


def test_ledger_ambiguous_not_repeatedly_reclassified():
    failures = []
    mconn, mpath = _make_db()
    try:
        # An answer that lands in the "on-topic, unresolvable" ambiguous branch.
        uid = _seed_answered_uncertainty(mconn, "checkin_feedback_waiting:101", "The manager mentioned something during the meeting.")
        orig = hai._call_ollama_structured
        hai._call_ollama_structured = _mock_ollama("expected_pattern", 0.95, "Mentioned during meeting.")
        try:
            summary = hai.process_eligible_answers(mconn)
        finally:
            hai._call_ollama_structured = orig
        if summary["ambiguous"] != 1:
            failures.append(f"expected 1 ambiguous, got {summary}")

        call_count = {"n": 0}
        def _counting_fake(*a, **k):
            call_count["n"] += 1
            return {"conclusion": "expected_pattern", "confidence": 0.95, "reason": "x", "model_identifier": "llama3.1:8b"}, None, None, 0.01
        hai._call_ollama_structured = _counting_fake
        try:
            hai.process_eligible_answers(mconn)
        finally:
            hai._call_ollama_structured = orig
        if call_count["n"] != 0:
            failures.append("ambiguous row was resent to the model on a later Reflection run")

        attempts = hai.get_attempts_for_uncertainty(mconn, uid)
        if len(attempts) != 1 or attempts[0]["status"] != "ambiguous":
            failures.append(f"expected exactly 1 ambiguous attempt, got {[dict(a) for a in attempts]}")
    finally:
        h.cleanup_db(mconn, mpath)
    return failures


def test_ledger_retry_policy_and_new_interpreter_version():
    failures = []
    mconn, mpath = _make_db()
    try:
        uid = _seed_answered_uncertainty(mconn, "checkin_feedback_waiting:102", "This is normal.")
        orig = hai._call_ollama_structured

        # First failure: operational (connection error).
        hai._call_ollama_structured = _mock_ollama_failure("connection_error")
        try:
            hai.process_eligible_answers(mconn)
        finally:
            hai._call_ollama_structured = orig
        attempts = hai.get_attempts_for_uncertainty(mconn, uid)
        if len(attempts) != 1 or attempts[0]["status"] != "failed":
            failures.append(f"expected 1 failed attempt, got {[dict(a) for a in attempts]}")

        # Immediately re-running should NOT retry yet (cooldown not elapsed).
        hai._call_ollama_structured = _mock_ollama("expected_pattern", 0.9, "Normal.")
        try:
            summary = hai.process_eligible_answers(mconn, batch_size=10)
        finally:
            hai._call_ollama_structured = orig
        if summary["eligible_found"] != 0:
            failures.append("retry happened before cooldown elapsed")

        # Force retry by simulating cooldown having elapsed (0-second cooldown override).
        hai._call_ollama_structured = _mock_ollama_failure("timeout")
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

        # Exhaust retries (max_retries=2 -> after 2 failed attempts, no more retries).
        hai._call_ollama_structured = _mock_ollama_failure("timeout")
        try:
            eligible = hai.get_eligible_answered_uncertainties(
                mconn, retry_cooldown_seconds=0, max_retries=2,
            )
            if len(eligible) != 0:
                failures.append(f"expected retry budget of 2 to already be exhausted (2 failed attempts exist), got {len(eligible)}")
        finally:
            hai._call_ollama_structured = orig

        # A new interpreter_version must be able to create a fresh auditable
        # attempt without deleting/overwriting the old (failed) ones.
        hai._call_ollama_structured = _mock_ollama("expected_pattern", 0.9, "Normal.")
        try:
            eligible_v2 = hai.get_eligible_answered_uncertainties(
                mconn, interpreter_version="checkin_feedback_waiting_v2", retry_cooldown_seconds=0,
            )
            if len(eligible_v2) != 1:
                failures.append("new interpreter_version did not see the uncertainty as eligible")
            for row in eligible_v2:
                hai.process_one_uncertainty(mconn, row, interpreter_version="checkin_feedback_waiting_v2")
        finally:
            hai._call_ollama_structured = orig

        all_attempts = hai.get_attempts_for_uncertainty(mconn, uid)
        if len(all_attempts) != 3:
            failures.append(f"expected 3 total durable attempts (2 old + 1 new version), got {len(all_attempts)}")
        accepted = hai.get_current_accepted(mconn, uid)
        if accepted is None or accepted["interpreter_version"] != "checkin_feedback_waiting_v2":
            failures.append("new-version attempt was not correctly accepted as current")
        accepted_count = sum(1 for a in all_attempts if a["is_current_accepted"] == 1)
        if accepted_count != 1:
            failures.append(f"expected exactly 1 current-accepted row, found {accepted_count}")
    finally:
        h.cleanup_db(mconn, mpath)
    return failures


def test_eligibility_excludes_null_and_other_families():
    failures = []
    mconn, mpath = _make_db()
    try:
        # NULL family (e.g. a historical or unrelated-type row) must be ignored.
        null_uid, _ = hearth_worldview.upsert_uncertainty(
            mconn, subject_type="entity_episode", subject_id="missing_discord:5",
            uncertainty_text="unrelated type, no family",
        )
        mconn.execute(
            "UPDATE hearth_worldview_uncertainties SET status='answered', answer_text='fine', answered_by='x', answered_at='x' WHERE id=?;",
            (null_uid,),
        )
        # A different family/version must also be ignored.
        other_uid = _seed_answered_uncertainty(
            mconn, "entity_repeat_concern:7", "irrelevant", family="entity_repeat_concern", version=1,
        )
        other_version_uid = _seed_answered_uncertainty(
            mconn, "checkin_feedback_waiting:8", "irrelevant", family="checkin_feedback_waiting", version=2,
        )
        mconn.commit()

        eligible = hai.get_eligible_answered_uncertainties(mconn)
        eligible_ids = {row["id"] for row in eligible}
        if null_uid in eligible_ids:
            failures.append("NULL-family row was incorrectly treated as eligible")
        if other_uid in eligible_ids:
            failures.append("other-family row was incorrectly treated as eligible")
        if other_version_uid in eligible_ids:
            failures.append("other-version row was incorrectly treated as eligible")
    finally:
        h.cleanup_db(mconn, mpath)
    return failures


def test_batch_size_bound():
    failures = []
    mconn, mpath = _make_db()
    try:
        for i in range(5):
            _seed_answered_uncertainty(mconn, f"checkin_feedback_waiting:{200+i}", "This is normal.")
        eligible = hai.get_eligible_answered_uncertainties(mconn, batch_size=2)
        if len(eligible) != 2:
            failures.append(f"batch_size=2 not respected, got {len(eligible)}")
    finally:
        h.cleanup_db(mconn, mpath)
    return failures


# ---------------------------------------------------------------------------
# 5. Provider adapters (mocked at the HTTP/SDK boundary — no network calls)
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
        result, category, detail, latency = hai._call_ollama_structured(
            "checkin_feedback_waiting", 1, _Q, "some answer",
        )
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
        # valid JSON, but conclusion is outside the enum entirely (defensive
        # check in case Ollama doesn't perfectly honor the schema).
        return _FakeResponse(200, {"message": {"content": '{"conclusion": "nonsense", "confidence": 0.9, "reason": "x"}'}})

    orig_post = requests.post
    requests.post = _fake_post
    try:
        result, category, detail, latency = hai._call_ollama_structured(
            "checkin_feedback_waiting", 1, _Q, "some answer",
        )
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
        result, category, detail, latency = hai._call_ollama_structured(
            "checkin_feedback_waiting", 1, _Q, "some answer",
        )
    finally:
        requests.post = orig_post

    if result is not None or category != "connection_error":
        failures.append(f"connection error not classified correctly: result={result} category={category}")
    return failures


def test_provider_failure_does_not_break_reflection_batch():
    failures = []
    mconn, mpath = _make_db()
    try:
        _seed_answered_uncertainty(mconn, "checkin_feedback_waiting:300", "This is normal.")
        _seed_answered_uncertainty(mconn, "checkin_feedback_waiting:301", "This keeps happening with this manager.")

        orig = hai._call_ollama_structured
        calls = {"n": 0}
        def _flaky(*a, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("boom — simulated unexpected error inside the adapter")
            return {"conclusion": "meaningful_signal", "confidence": 0.9, "reason": "this keeps happening", "model_identifier": "llama3.1:8b"}, None, None, 0.01
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
        _seed_answered_uncertainty(mconn, "checkin_feedback_waiting:400", "The manager mentioned something during the meeting.")
        orig_ollama = hai._call_ollama_structured
        orig_gemini = hai._call_gemini_structured
        gemini_calls = {"n": 0}
        def _fake_gemini(*a, **k):
            gemini_calls["n"] += 1
            return {"conclusion": "insufficient_information", "confidence": 0.5, "reason": "fallback", "model_identifier": "gemini-test"}, None, None, 0.01
        hai._call_ollama_structured = _mock_ollama("expected_pattern", 0.9, "Mentioned during meeting.")
        hai._call_gemini_structured = _fake_gemini
        try:
            hai.process_eligible_answers(mconn)
        finally:
            hai._call_ollama_structured = orig_ollama
            hai._call_gemini_structured = orig_gemini

        if gemini_calls["n"] != 0:
            failures.append("Gemini fallback was called despite being disabled by default")
    finally:
        h.cleanup_db(mconn, mpath)
    return failures


def test_gemini_fallback_enabled_only_for_ambiguous():
    failures = []
    mconn, mpath = _make_db()
    try:
        # Bare answer -> insufficient_information via step-2 short-circuit,
        # never ambiguous — fallback must not be invoked even if enabled.
        _seed_answered_uncertainty(mconn, "checkin_feedback_waiting:401", "yes")
        orig_ollama = hai._call_ollama_structured
        orig_gemini = hai._call_gemini_structured
        orig_flag = hai.HEARTH_INTERPRETER_GEMINI_FALLBACK_ENABLED
        gemini_calls = {"n": 0}
        def _fake_gemini(*a, **k):
            gemini_calls["n"] += 1
            return {"conclusion": "insufficient_information", "confidence": 0.5, "reason": "fallback", "model_identifier": "gemini-test"}, None, None, 0.01
        hai._call_ollama_structured = _mock_ollama("expected_pattern", 0.9, "yes")
        hai._call_gemini_structured = _fake_gemini
        hai.HEARTH_INTERPRETER_GEMINI_FALLBACK_ENABLED = True
        try:
            hai.process_eligible_answers(mconn)
        finally:
            hai._call_ollama_structured = orig_ollama
            hai._call_gemini_structured = orig_gemini
            hai.HEARTH_INTERPRETER_GEMINI_FALLBACK_ENABLED = orig_flag

        if gemini_calls["n"] != 0:
            failures.append("Gemini fallback was called for a bare/short answer, not just genuine ambiguity")
    finally:
        h.cleanup_db(mconn, mpath)
    return failures


def test_ollama_disabled_path():
    failures = []
    mconn, mpath = _make_db()
    try:
        uid = _seed_answered_uncertainty(mconn, "checkin_feedback_waiting:600", "This is normal.")
        orig_flag = hai.HEARTH_INTERPRETER_OLLAMA_ENABLED
        orig_call = hai._call_ollama_structured
        called = {"n": 0}
        def _fail_if_called(*a, **k):
            called["n"] += 1
            return {"conclusion": "expected_pattern", "confidence": 0.9, "reason": "x", "model_identifier": "x"}, None, None, 0.01
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
        # The rest of Hearth (Reflection) must not require Ollama to function.
        if summary["enabled"] is not True:
            failures.append("subsystem-level enabled flag should remain True — only the provider is disabled")
    finally:
        h.cleanup_db(mconn, mpath)
    return failures


def test_whole_subsystem_disabled_path():
    failures = []
    mconn, mpath = _make_db()
    try:
        _seed_answered_uncertainty(mconn, "checkin_feedback_waiting:601", "This is normal.")
        orig_flag = hai.HEARTH_INTERPRETER_ENABLED
        orig_call = hai._call_ollama_structured
        called = {"n": 0}
        def _fail_if_called(*a, **k):
            called["n"] += 1
            return {"conclusion": "expected_pattern", "confidence": 0.9, "reason": "x", "model_identifier": "x"}, None, None, 0.01
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
        # No ledger rows should have been created at all — the feature is fully off.
        remaining = mconn.execute("SELECT COUNT(*) AS n FROM hearth_answer_interpretations;").fetchone()["n"]
        if remaining != 0:
            failures.append(f"expected 0 ledger rows with the subsystem fully disabled, found {remaining}")
    finally:
        h.cleanup_db(mconn, mpath)
    return failures


# ---------------------------------------------------------------------------
# 6. Aggregation
# ---------------------------------------------------------------------------

def test_aggregation_counts_only_accepted_and_distinct_subjects():
    failures = []
    mconn, mpath = _make_db()
    try:
        u1 = _seed_answered_uncertainty(mconn, "checkin_feedback_waiting:500", "This is normal.")
        u2 = _seed_answered_uncertainty(mconn, "checkin_feedback_waiting:501", "This manager is behind on several check-ins.")
        u3 = _seed_answered_uncertainty(mconn, "checkin_feedback_waiting:502", "This is normal too.")
        u4_pending = _seed_answered_uncertainty(mconn, "checkin_feedback_waiting:503", "Not processed yet — no attempt made.")

        orig = hai._call_ollama_structured

        hai._call_ollama_structured = _mock_ollama("expected_pattern", 0.9, "Normal.")
        hai.process_one_uncertainty(mconn, hearth_worldview.get_uncertainty(mconn, u1))
        hai.process_one_uncertainty(mconn, hearth_worldview.get_uncertainty(mconn, u3))

        hai._call_ollama_structured = _mock_ollama("meaningful_signal", 0.9, "Behind on several check-ins.")
        hai.process_one_uncertainty(mconn, hearth_worldview.get_uncertainty(mconn, u2))

        hai._call_ollama_structured = orig  # u4 left untouched -> pending

        report = hai.aggregate_family(mconn)
        if report["total_answered_eligible"] != 4:
            failures.append(f"expected 4 eligible, got {report['total_answered_eligible']}")
        if report["total_accepted"] != 3:
            failures.append(f"expected 3 accepted, got {report['total_accepted']}")
        if report["total_pending"] != 1:
            failures.append(f"expected 1 pending (never attempted), got {report['total_pending']}")
        if report["by_label"]["expected_pattern"] != 2 or report["by_label"]["meaningful_signal"] != 1:
            failures.append(f"label counts wrong: {report['by_label']}")
        if report["distinct_subjects_total"] != 4:
            failures.append(f"expected 4 distinct subjects, got {report['distinct_subjects_total']}")
        if report["distinct_subjects_by_label"]["expected_pattern"] != 2:
            failures.append(f"expected 2 distinct subjects for expected_pattern, got {report['distinct_subjects_by_label']}")
        if report["distinct_answering_managers"] != 1:  # all seeded with answered_by='stacy'
            failures.append(f"expected 1 distinct answering manager, got {report['distinct_answering_managers']}")
    finally:
        h.cleanup_db(mconn, mpath)
    return failures


# ---------------------------------------------------------------------------
# 7. Reflection wiring (structural — see file docstring for why this is
# structural rather than a full run_pipeline() integration run)
# ---------------------------------------------------------------------------

def test_reflection_wiring_structural():
    failures = []
    if morning_briefing.hearth_answer_interpreter is not hai:
        failures.append("morning_briefing does not import the real hearth_answer_interpreter module")

    source = inspect.getsource(morning_briefing.run_pipeline)
    if "hearth_answer_interpreter.process_eligible_answers(memory_conn)" not in source:
        failures.append("run_pipeline() does not call hearth_answer_interpreter.process_eligible_answers()")

    call_idx = source.find("hearth_answer_interpreter.process_eligible_answers(memory_conn)")
    resolve_idx = source.find("resolve_cleared_worldview_questions(memory_conn)")
    if call_idx == -1 or resolve_idx == -1 or call_idx < resolve_idx:
        failures.append("interpreter batch is not positioned after the worldview-questions Reflection steps")

    try_idx = source.rfind("try:", 0, call_idx)
    except_idx = source.find("except Exception", call_idx)
    if try_idx == -1 or except_idx == -1 or try_idx > call_idx:
        failures.append("interpreter batch call in run_pipeline() is not wrapped in a try/except")

    # hearth_ask.py must remain completely untouched by this build.
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
        ("family_stamping", test_family_stamping),
        ("validator_mandatory_dataset", test_validator_mandatory_dataset),
        ("validator_guardrails", test_validator_guardrails),
        ("ledger_lifecycle_success_not_reclassified", test_ledger_lifecycle_success_not_reclassified),
        ("ledger_ambiguous_not_repeatedly_reclassified", test_ledger_ambiguous_not_repeatedly_reclassified),
        ("ledger_retry_policy_and_new_interpreter_version", test_ledger_retry_policy_and_new_interpreter_version),
        ("eligibility_excludes_null_and_other_families", test_eligibility_excludes_null_and_other_families),
        ("batch_size_bound", test_batch_size_bound),
        ("ollama_adapter_malformed_output_is_failed_not_accepted", test_ollama_adapter_malformed_output_is_failed_not_accepted),
        ("ollama_adapter_schema_violation_is_failed", test_ollama_adapter_schema_violation_is_failed),
        ("ollama_adapter_connection_error", test_ollama_adapter_connection_error),
        ("provider_failure_does_not_break_reflection_batch", test_provider_failure_does_not_break_reflection_batch),
        ("ollama_disabled_path", test_ollama_disabled_path),
        ("whole_subsystem_disabled_path", test_whole_subsystem_disabled_path),
        ("gemini_fallback_disabled_by_default_and_capped", test_gemini_fallback_disabled_by_default_and_capped),
        ("gemini_fallback_enabled_only_for_ambiguous", test_gemini_fallback_enabled_only_for_ambiguous),
        ("aggregation_counts_only_accepted_and_distinct_subjects", test_aggregation_counts_only_accepted_and_distinct_subjects),
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
