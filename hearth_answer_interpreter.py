"""
Hearth Answer Interpreter — Build 1 of the manager-answer-to-Worldview
learning loop.

Turns a manager's answer to a surfaced Worldview question into a durable,
auditable interpretation attempt, using a local model first (Ollama,
JSON-schema-constrained) and an optional guarded Gemini fallback, gated by
deterministic (non-model) validation before anything is treated as accepted.

Scope for Build 1: only question_family="checkin_feedback_waiting",
question_version=1 is active. See docs/HEARTH_MIND_09_QUESTIONS_AND_MAINTENANCE.md
and the hearth_worldview_uncertainties/hearth_answer_interpretations schemas
for the surrounding architecture.

This module does NOT create beliefs, candidates, or change Hearth's future
judgment. It only interprets and durably records — see aggregate_family()
for the deterministic, model-free reporting layer.

Runs only from the background Reflection pass (see morning_briefing.py's
call to process_eligible_answers()). Never called from the live Ask Hearth
request path, and never called synchronously from the answer-save route.
"""

import json
import logging
import os
import re
import sqlite3
import time
from datetime import datetime, timezone

from hearth_memory import MEMORY_DB_PATH

logger = logging.getLogger("hearth.answer_interpreter")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def _env_bool(name, default):
    return os.environ.get(name, default) == "1"


def _env_int(name, default):
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_float(name, default):
    try:
        return float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


HEARTH_INTERPRETER_ENABLED = _env_bool("HEARTH_INTERPRETER_ENABLED", "1")
HEARTH_INTERPRETER_OLLAMA_ENABLED = _env_bool("HEARTH_INTERPRETER_OLLAMA_ENABLED", "1")
HEARTH_INTERPRETER_OLLAMA_HOST = os.environ.get(
    "HEARTH_INTERPRETER_OLLAMA_HOST", "http://localhost:11434"
)
HEARTH_INTERPRETER_OLLAMA_MODEL = os.environ.get(
    "HEARTH_INTERPRETER_OLLAMA_MODEL", "llama3.1:8b"
)
HEARTH_INTERPRETER_OLLAMA_TIMEOUT = _env_float("HEARTH_INTERPRETER_OLLAMA_TIMEOUT", 60.0)
HEARTH_INTERPRETER_BATCH_SIZE = _env_int("HEARTH_INTERPRETER_BATCH_SIZE", 10)
HEARTH_INTERPRETER_VERSION = os.environ.get(
    "HEARTH_INTERPRETER_VERSION", "checkin_feedback_waiting_v1"
)
HEARTH_INTERPRETER_MAX_RETRIES = _env_int("HEARTH_INTERPRETER_MAX_RETRIES", 3)
HEARTH_INTERPRETER_RETRY_COOLDOWN_SECONDS = _env_int(
    "HEARTH_INTERPRETER_RETRY_COOLDOWN_SECONDS", 3600
)
HEARTH_INTERPRETER_GEMINI_FALLBACK_ENABLED = _env_bool(
    "HEARTH_INTERPRETER_GEMINI_FALLBACK_ENABLED", "0"
)
HEARTH_INTERPRETER_GEMINI_MAX_FALLBACK_CALLS_PER_RUN = _env_int(
    "HEARTH_INTERPRETER_GEMINI_MAX_FALLBACK_CALLS_PER_RUN", 5
)

SCHEMA_VERSION = "v1"

# The only family/version active in Build 1. Deliberately not a generic
# registry — a future build can add entries here and to FAMILY_DEFINITIONS
# without touching the ledger, eligibility, or orchestrator plumbing below,
# but only entries actually present in FAMILY_DEFINITIONS are ever processed.
_ACTIVE_FAMILY = "checkin_feedback_waiting"
_ACTIVE_VERSION = 1


# ---------------------------------------------------------------------------
# Shared closed taxonomy
# ---------------------------------------------------------------------------

TAXONOMY = (
    "expected_pattern",
    "meaningful_signal",
    "context_dependent",
    "insufficient_information",
    "unrelated_or_unclear",
)
_TAXONOMY_SET = frozenset(TAXONOMY)

# Family-specific label meanings, grounded in the actual question Hearth asks
# for this family (see hearth_soul.py's _upsert_single_episode_uncertainty:
# "Is the checkin feedback waiting episode for {display_name} part of a
# larger pattern?"). Used verbatim in the model prompt.
FAMILY_DEFINITIONS = {
    "checkin_feedback_waiting": {
        1: {
            "question_context": (
                "Hearth flagged that a creator submitted check-in responses "
                "but has not yet received feedback from their coach/manager, "
                "and asked: \"Is the checkin feedback waiting episode for "
                "this creator part of a larger pattern?\""
            ),
            # These meanings answer the exact question Hearth asked — "is
            # this part of a LARGER PATTERN" (recurring/systemic for this
            # manager, or across other creators/check-ins) — not the nearby
            # but different question "is this delay itself normal or
            # concerning". A delay can be perfectly normal-feeling and still
            # be part of a pattern (this manager is chronically behind), and
            # a delay can feel worth mentioning while still being a genuine
            # one-off. Keep that distinction in the model prompt and in the
            # deterministic trigger vocabulary below — see the Build 1
            # semantic-contract review notes.
            "definitions": {
                "expected_pattern": (
                    "The manager indicates this occurrence is isolated or a "
                    "one-off for this manager/creator — NOT part of a broader "
                    "or recurring pattern. (The delay itself may or may not "
                    "feel routine; what matters for this label is that it "
                    "does not reflect a larger pattern.)"
                ),
                "meaningful_signal": (
                    "The manager indicates this occurrence IS part of a "
                    "larger pattern — e.g. this manager is behind on several "
                    "check-ins, this keeps happening, it has happened before, "
                    "or it reflects a systemic/recurring issue rather than a "
                    "single isolated event."
                ),
                "context_dependent": (
                    "The manager indicates whether this is part of a larger "
                    "pattern depends on an additional condition — e.g. how "
                    "many other check-ins this manager currently has "
                    "outstanding, whether this happens with other creators "
                    "too, or another specific factor — rather than a direct "
                    "yes or no."
                ),
                "insufficient_information": (
                    "The manager cannot yet say whether this reflects a "
                    "larger pattern because they haven't checked other "
                    "outstanding feedback, other creators, or need more "
                    "observation before judging."
                ),
                "unrelated_or_unclear": (
                    "The answer does not address whether this is part of a "
                    "larger pattern, is internally incoherent, is too "
                    "ambiguous to resolve, or discusses an unrelated topic."
                ),
            },
            "few_shot": [
                ("Yes, this manager is behind on several check-ins.", "meaningful_signal"),
                ("No, this is an isolated delay.", "expected_pattern"),
                ("I cannot tell yet whether this is happening elsewhere.", "insufficient_information"),
                ("It depends on how many other check-ins this manager currently has outstanding.", "context_dependent"),
                ("The creator missed yesterday's battle.", "unrelated_or_unclear"),
            ],
        },
    },
}


# ---------------------------------------------------------------------------
# Deterministic validation vocabulary
#
# Small, conservative substring/phrase checks — not a brittle keyword engine.
# These exist only to gate/downgrade a model's proposed conclusion against
# textual evidence; they never invent a *more* substantive label than the
# model proposed (see validate_interpretation() for the exact precedence).
#
# Semantic-contract review note: this vocabulary judges PATTERN vs. ISOLATED
# occurrence — whether this reflects something recurring/systemic for this
# manager (or across creators), not whether the delay itself feels long or
# concerning. "Overdue"/"concerning" language alone does not establish a
# pattern (a first-time delay can still be overdue), so those words are
# deliberately NOT meaningful_signal triggers here — recurrence/repetition
# language is. General normalcy language ("normal", "just needs more time")
# is kept as expected_pattern (isolated/non-pattern) evidence: in practice
# that phrasing is how managers most often deny a systemic issue exists (see
# "Managers just need more time" in the labeled test dataset) even though it
# doesn't name a specific other instance — a defensible but judgment-call
# boundary, flagged for review against real manager answers post-launch.
# ---------------------------------------------------------------------------

_BARE_ANSWERS = frozenset({
    "yes", "no", "maybe", "unsure", "not sure", "i don't know", "i dont know",
    "idk", "possibly", "wait",
})

_ON_TOPIC_KEYWORDS = (
    "feedback", "check-in", "checkin", "check in", "wait", "waiting", "delay",
    "delayed", "overdue", "respond", "response", "coach", "manager", "submit",
    "submission", "time", "normal", "pattern", "follow up", "follow-up",
    "review", "late", "week", "weeks", "day", "days", "check-ins", "checkins",
    "elsewhere", "outstanding", "creators", "history",
)

_EXPECTED_PATTERN_TRIGGERS = (
    # direct isolation language — the strongest, most on-point evidence
    "isolated", "one-off", "one time", "just this once", "first time",
    "hasn't happened before", "has not happened before", "not otherwise",
    "only this one", "not a pattern", "no pattern", "not recurring",
    # general normalcy/timing language — weaker, secondary evidence that a
    # manager typically uses to imply "nothing systemic going on" (see note above)
    "normal", "expected", "nothing unusual", "not unusual", "acceptable",
    "routine", "more time", "need more time", "needs more time", "fine",
    "not a problem", "just submitted", "only submitted",
)

_MEANINGFUL_SIGNAL_TRIGGERS = (
    "behind on several", "behind on multiple", "several check-ins",
    "multiple check-ins", "repeatedly", "keeps happening", "happens a lot",
    "happened before", "has happened before", "not the first time",
    "recurring", "a pattern of", "every time", "always does this",
    "always late", "with other creators too", "other creators as well",
    "systemic", "ongoing issue", "chronic", "several times", "multiple times",
    "this keeps",
)

_CONTEXT_DEPENDENT_TRIGGERS = (
    "depends", "only matters if", "matters if", "unless", "more than a week",
    "more than a day", "a few days is fine", "provided that", "as long as",
    "if it", "if this", "if the creator", "if the manager",
    "depends on how many", "depends whether", "depends on whether",
    "other creators too",
)

_INSUFFICIENT_TRIGGERS = (
    "don't know", "do not know", "not sure", "need to check", "can't tell",
    "cannot tell", "let's wait", "need more info", "need to know",
    "unclear how long", "unclear", "need to see", "hard to say",
    "have to check", "may be", "might be", "not certain",
    "outstanding feedback", "happening elsewhere", "check their other",
    "yet whether",
)


def _normalize(text):
    return re.sub(r"\s+", " ", (text or "").strip())


def _matches_any(lower_text, phrases):
    return any(phrase in lower_text for phrase in phrases)


def _has_meaningful_signal_trigger(lower_text):
    # No negation-neutralization needed: unlike the old "concern"/"concerning"
    # vocabulary (which "not concerning" could falsely trigger), the current
    # pattern-confirming phrases below are all multi-word and don't collide
    # with the expected_pattern (isolation) phrase list.
    return _matches_any(lower_text, _MEANINGFUL_SIGNAL_TRIGGERS)


# ---------------------------------------------------------------------------
# Deterministic validation
# ---------------------------------------------------------------------------

class ValidationOutcome:
    """Result of deterministic validation.

    status is the ledger processing state this outcome maps to:
    'succeeded' | 'ambiguous' | 'rejected_by_validation'.
    conclusion is non-None only when status == 'succeeded'.
    """

    __slots__ = ("status", "conclusion", "validation_result", "validation_reason")

    def __init__(self, status, conclusion, validation_result, validation_reason):
        self.status = status
        self.conclusion = conclusion
        self.validation_result = validation_result
        self.validation_reason = validation_reason


def validate_interpretation(question_text, answer_text, raw_conclusion, raw_reason):
    """Deterministically gate/resolve a model's proposed conclusion.

    question_text is accepted (and intended to be used by future families
    with multiple question variants) even though Build 1's single fixed
    question means the rules below key almost entirely off answer_text —
    see the "they need more time" case in the required test dataset, which
    must be evaluated against the real question context rather than in
    isolation, hence the explicit parameter rather than answer-only.

    Never trusts raw model confidence as a safety gate (see module docs) —
    confidence is not consulted anywhere in this function.
    """
    answer_norm = _normalize(answer_text)
    lower = answer_norm.lower()

    if not answer_norm:
        return ValidationOutcome(
            "succeeded", "unrelated_or_unclear", "accepted",
            "empty or whitespace-only answer",
        )

    bare_key = lower.strip(" .!?")
    if bare_key in _BARE_ANSWERS:
        # Even though the generated question is a direct yes/no ("is this
        # part of a larger pattern?"), a bare "yes"/"no" carries no
        # corroborating detail (which manager, which other check-ins, how
        # this was determined) — it lacks the independent semantic evidence
        # this label needs to safely become a future organizational-learning
        # input, so it's treated as insufficient regardless of the literal
        # answer. A non-bare answer that DOES say "yes, ..." with supporting
        # detail is evaluated normally below, not short-circuited here.
        return ValidationOutcome(
            "succeeded", "insufficient_information", "accepted",
            f"bare/short answer ({bare_key!r}) provides no independent"
            " semantic evidence to safely ground a substantive conclusion",
        )

    if raw_conclusion not in _TAXONOMY_SET:
        return ValidationOutcome(
            "rejected_by_validation", None, "rejected",
            f"model returned a conclusion outside the closed taxonomy: {raw_conclusion!r}",
        )

    on_topic = _matches_any(lower, _ON_TOPIC_KEYWORDS) or any((
        _matches_any(lower, _EXPECTED_PATTERN_TRIGGERS),
        _has_meaningful_signal_trigger(lower),
        _matches_any(lower, _CONTEXT_DEPENDENT_TRIGGERS),
        _matches_any(lower, _INSUFFICIENT_TRIGGERS),
    ))
    if not on_topic:
        return ValidationOutcome(
            "succeeded", "unrelated_or_unclear", "accepted",
            "no discernible connection to check-in feedback waiting",
        )

    reason_lower = (raw_reason or "").lower()
    if raw_conclusion == "expected_pattern" and _has_meaningful_signal_trigger(reason_lower) \
            and not _matches_any(reason_lower, _EXPECTED_PATTERN_TRIGGERS):
        return ValidationOutcome(
            "rejected_by_validation", None, "rejected",
            "model's conclusion (expected_pattern) conflicts with its own stated reason",
        )
    if raw_conclusion == "meaningful_signal" and _matches_any(reason_lower, _EXPECTED_PATTERN_TRIGGERS) \
            and not _has_meaningful_signal_trigger(reason_lower):
        return ValidationOutcome(
            "rejected_by_validation", None, "rejected",
            "model's conclusion (meaningful_signal) conflicts with its own stated reason",
        )

    # Conservative labels are never risky to accept once we know the answer
    # is on-topic — accept the model's own cautious call outright rather
    # than second-guessing it with weaker deterministic evidence.
    if raw_conclusion in ("insufficient_information", "unrelated_or_unclear"):
        return ValidationOutcome(
            "succeeded", raw_conclusion, "accepted",
            "model's conservative conclusion accepted; no contrary evidence found",
        )

    ctx = _matches_any(lower, _CONTEXT_DEPENDENT_TRIGGERS)
    exp = _matches_any(lower, _EXPECTED_PATTERN_TRIGGERS)
    sig = _has_meaningful_signal_trigger(lower)
    insuf = _matches_any(lower, _INSUFFICIENT_TRIGGERS)

    if ctx:
        candidate = "context_dependent"
    elif exp and sig:
        # Conflicting signals the answer itself never resolves — per this
        # family's own taxonomy definition, that's unrelated_or_unclear
        # ("is internally incoherent ... or too ambiguous to resolve"), not
        # a ledger-level ambiguous non-result.
        return ValidationOutcome(
            "succeeded", "unrelated_or_unclear", "accepted",
            "answer contains conflicting expected-pattern and meaningful-signal"
            " language; treated as unclear per taxonomy definition",
        )
    elif exp:
        candidate = "expected_pattern"
    elif sig:
        candidate = "meaningful_signal"
    elif insuf:
        candidate = "insufficient_information"
    else:
        candidate = None

    if candidate is not None and candidate == raw_conclusion:
        return ValidationOutcome(
            "succeeded", candidate, "accepted",
            f"deterministic evidence for {candidate} matches model's conclusion",
        )

    if candidate is not None and candidate != raw_conclusion:
        if insuf and candidate != "insufficient_information":
            return ValidationOutcome(
                "succeeded", "insufficient_information", "accepted",
                f"model proposed {raw_conclusion!r} but deterministic evidence"
                " pointed elsewhere; downgraded to insufficient_information",
            )
        return ValidationOutcome(
            "ambiguous", None, "ambiguous",
            f"model proposed {raw_conclusion!r} but deterministic evidence"
            f" supports {candidate!r} instead — could not safely confirm",
        )

    return ValidationOutcome(
        "ambiguous", None, "ambiguous",
        "on-topic answer but no deterministic evidence for any taxonomy label;"
        " model's substantive conclusion could not be independently confirmed",
    )


# ---------------------------------------------------------------------------
# Connection / schema
# ---------------------------------------------------------------------------

def get_interpreter_connection():
    conn = sqlite3.connect(MEMORY_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def ensure_answer_interpretations_table(conn):
    """Create hearth_answer_interpretations if missing. Safe to call repeatedly.

    Mirrors migrate_add_answer_interpretations.py, for a genuinely fresh
    database — same CREATE TABLE IF NOT EXISTS convention already used by
    hearth_worldview.ensure_worldview_tables()/hearth_soul.ensure_reflections_table()/
    hearth_questions.ensure_questions_table(). Called once per Reflection pass
    from morning_briefing.run_pipeline()'s startup block, alongside those
    siblings — NOT from process_eligible_answers() below, which only ever
    reads schema state (see _schema_ready()) and skips gracefully rather than
    creating anything at interpretation time.
    """
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS hearth_answer_interpretations (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            uncertainty_id      INTEGER NOT NULL,
            question_family     TEXT    NOT NULL,
            question_version    INTEGER NOT NULL,
            attempt_number      INTEGER NOT NULL,
            retry_of_attempt_id INTEGER,
            status              TEXT    NOT NULL DEFAULT 'pending',
            interpreter_version TEXT    NOT NULL,
            schema_version      TEXT    NOT NULL,
            provider            TEXT,
            model_name          TEXT,
            model_identifier    TEXT,
            raw_conclusion      TEXT,
            conclusion          TEXT,
            model_confidence    REAL,
            model_reason        TEXT,
            validation_result   TEXT,
            validation_reason   TEXT,
            is_current_accepted INTEGER NOT NULL DEFAULT 0,
            failure_category    TEXT,
            failure_detail      TEXT,
            attempted_at        TEXT    NOT NULL,
            completed_at        TEXT,
            created_at          TEXT    NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_answer_interp_uncertainty
            ON hearth_answer_interpretations (uncertainty_id);
        CREATE INDEX IF NOT EXISTS idx_answer_interp_family_version
            ON hearth_answer_interpretations (question_family, question_version);
        CREATE INDEX IF NOT EXISTS idx_answer_interp_status
            ON hearth_answer_interpretations (status);
        CREATE INDEX IF NOT EXISTS idx_answer_interp_interpreter_version
            ON hearth_answer_interpretations (interpreter_version);
        CREATE UNIQUE INDEX IF NOT EXISTS uq_answer_interp_current_accepted
            ON hearth_answer_interpretations (uncertainty_id)
            WHERE is_current_accepted = 1;
    """)
    conn.commit()


def _now():
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Ledger CRUD
# ---------------------------------------------------------------------------

def _next_attempt_number(conn, uncertainty_id):
    row = conn.execute(
        "SELECT COALESCE(MAX(attempt_number), 0) AS n FROM hearth_answer_interpretations"
        " WHERE uncertainty_id = ?;",
        (uncertainty_id,),
    ).fetchone()
    return row["n"] + 1


def create_pending_attempt(conn, uncertainty_id, question_family, question_version,
                            interpreter_version, retry_of_attempt_id=None):
    """Insert a durable pending attempt row before any provider call is made.

    Every model call must create or update a specifically identifiable
    attempt — this is that row. Returns the new attempt's id.
    """
    now = _now()
    attempt_number = _next_attempt_number(conn, uncertainty_id)
    cur = conn.execute(
        "INSERT INTO hearth_answer_interpretations"
        " (uncertainty_id, question_family, question_version, attempt_number,"
        "  retry_of_attempt_id, status, interpreter_version, schema_version,"
        "  is_current_accepted, attempted_at, created_at)"
        " VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, 0, ?, ?);",
        (uncertainty_id, question_family, question_version, attempt_number,
         retry_of_attempt_id, interpreter_version, SCHEMA_VERSION, now, now),
    )
    conn.commit()
    return cur.lastrowid


def mark_attempt_dispatched(conn, attempt_id, provider, model_name, model_identifier):
    conn.execute(
        "UPDATE hearth_answer_interpretations"
        " SET status = 'attempted', provider = ?, model_name = ?, model_identifier = ?"
        " WHERE id = ?;",
        (provider, model_name, model_identifier, attempt_id),
    )
    conn.commit()


def _finalize_attempt(conn, attempt_id, *, status, raw_conclusion=None, conclusion=None,
                       model_confidence=None, model_reason=None, validation_result=None,
                       validation_reason=None, failure_category=None, failure_detail=None,
                       is_current_accepted=False):
    now = _now()
    if is_current_accepted:
        row = conn.execute(
            "SELECT uncertainty_id FROM hearth_answer_interpretations WHERE id = ?;",
            (attempt_id,),
        ).fetchone()
        conn.execute(
            "UPDATE hearth_answer_interpretations SET is_current_accepted = 0"
            " WHERE uncertainty_id = ? AND is_current_accepted = 1;",
            (row["uncertainty_id"],),
        )
    conn.execute(
        "UPDATE hearth_answer_interpretations"
        " SET status = ?, raw_conclusion = ?, conclusion = ?, model_confidence = ?,"
        "     model_reason = ?, validation_result = ?, validation_reason = ?,"
        "     failure_category = ?, failure_detail = ?, is_current_accepted = ?,"
        "     completed_at = ?"
        " WHERE id = ?;",
        (status, raw_conclusion, conclusion, model_confidence, model_reason,
         validation_result, validation_reason, failure_category, failure_detail,
         1 if is_current_accepted else 0, now, attempt_id),
    )
    conn.commit()


def mark_attempt_succeeded(conn, attempt_id, raw_conclusion, conclusion, model_confidence,
                            model_reason, validation_reason):
    _finalize_attempt(
        conn, attempt_id, status="succeeded", raw_conclusion=raw_conclusion,
        conclusion=conclusion, model_confidence=model_confidence, model_reason=model_reason,
        validation_result="accepted", validation_reason=validation_reason,
        is_current_accepted=True,
    )


def mark_attempt_ambiguous(conn, attempt_id, raw_conclusion, model_confidence,
                            model_reason, validation_reason):
    _finalize_attempt(
        conn, attempt_id, status="ambiguous", raw_conclusion=raw_conclusion,
        model_confidence=model_confidence, model_reason=model_reason,
        validation_result="ambiguous", validation_reason=validation_reason,
    )


def mark_attempt_rejected(conn, attempt_id, raw_conclusion, model_confidence,
                           model_reason, validation_reason):
    _finalize_attempt(
        conn, attempt_id, status="rejected_by_validation", raw_conclusion=raw_conclusion,
        model_confidence=model_confidence, model_reason=model_reason,
        validation_result="rejected", validation_reason=validation_reason,
    )


def mark_attempt_failed(conn, attempt_id, failure_category, failure_detail):
    _finalize_attempt(
        conn, attempt_id, status="failed", validation_result="not_applicable",
        failure_category=failure_category, failure_detail=failure_detail,
    )


def get_attempts_for_uncertainty(conn, uncertainty_id):
    return conn.execute(
        "SELECT * FROM hearth_answer_interpretations WHERE uncertainty_id = ?"
        " ORDER BY attempt_number ASC;",
        (uncertainty_id,),
    ).fetchall()


def get_current_accepted(conn, uncertainty_id):
    return conn.execute(
        "SELECT * FROM hearth_answer_interpretations"
        " WHERE uncertainty_id = ? AND is_current_accepted = 1 LIMIT 1;",
        (uncertainty_id,),
    ).fetchone()


def _latest_attempt_for_version(conn, uncertainty_id, interpreter_version):
    return conn.execute(
        "SELECT * FROM hearth_answer_interpretations"
        " WHERE uncertainty_id = ? AND interpreter_version = ?"
        " ORDER BY attempt_number DESC LIMIT 1;",
        (uncertainty_id, interpreter_version),
    ).fetchone()


def _count_failed_attempts_for_version(conn, uncertainty_id, interpreter_version):
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM hearth_answer_interpretations"
        " WHERE uncertainty_id = ? AND interpreter_version = ? AND status = 'failed';",
        (uncertainty_id, interpreter_version),
    ).fetchone()
    return row["n"]


def _seconds_since(iso_ts):
    try:
        ts = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - ts).total_seconds()
    except Exception:
        return float("inf")


# ---------------------------------------------------------------------------
# Eligibility
# ---------------------------------------------------------------------------

def get_eligible_answered_uncertainties(conn, batch_size=None, interpreter_version=None,
                                         max_retries=None, retry_cooldown_seconds=None):
    """Return answered checkin_feedback_waiting/v1 uncertainties eligible for
    interpretation this run, bounded by batch_size, oldest-answered first.

    Eligible means: answered with non-empty text, correct family/version, no
    current accepted interpretation, and — if a previous attempt for the
    current interpreter_version exists — that attempt is either a retryable
    'failed'/stuck 'pending'/'attempted' row past its cooldown and under the
    retry cap, never an 'ambiguous' or 'rejected_by_validation' terminal
    result (those stay put for human review rather than being resent to a
    model every Reflection run) or an already-successful one (excluded by
    the NOT EXISTS below).
    """
    batch_size = HEARTH_INTERPRETER_BATCH_SIZE if batch_size is None else batch_size
    interpreter_version = interpreter_version or HEARTH_INTERPRETER_VERSION
    max_retries = HEARTH_INTERPRETER_MAX_RETRIES if max_retries is None else max_retries
    cooldown = (
        HEARTH_INTERPRETER_RETRY_COOLDOWN_SECONDS
        if retry_cooldown_seconds is None else retry_cooldown_seconds
    )

    candidates = conn.execute(
        "SELECT * FROM hearth_worldview_uncertainties"
        " WHERE status = 'answered'"
        "   AND answer_text IS NOT NULL AND TRIM(answer_text) != ''"
        "   AND question_family = ? AND question_version = ?"
        "   AND id NOT IN ("
        "       SELECT uncertainty_id FROM hearth_answer_interpretations"
        "       WHERE is_current_accepted = 1"
        "   )"
        " ORDER BY answered_at ASC;",
        (_ACTIVE_FAMILY, _ACTIVE_VERSION),
    ).fetchall()

    eligible = []
    for row in candidates:
        latest = _latest_attempt_for_version(conn, row["id"], interpreter_version)
        if latest is None:
            eligible.append(row)
        elif latest["status"] in ("ambiguous", "rejected_by_validation"):
            continue  # terminal for this interpreter_version — needs a human or a new version
        elif latest["status"] in ("failed", "pending", "attempted"):
            failed_count = _count_failed_attempts_for_version(conn, row["id"], interpreter_version)
            if failed_count >= max_retries:
                continue
            reference_time = latest["completed_at"] or latest["attempted_at"]
            if _seconds_since(reference_time) < cooldown:
                continue
            eligible.append(row)
        if len(eligible) >= batch_size:
            break

    return eligible[:batch_size]


# ---------------------------------------------------------------------------
# Ollama structured-output client
# ---------------------------------------------------------------------------

def _family_json_schema():
    return {
        "type": "object",
        "properties": {
            "conclusion": {"type": "string", "enum": list(TAXONOMY)},
            "confidence": {"type": "number"},
            "reason": {"type": "string"},
        },
        "required": ["conclusion", "confidence", "reason"],
    }


def _build_prompt(family, version, question_text, answer_text):
    contract = FAMILY_DEFINITIONS[family][version]
    definitions_text = "\n".join(
        f"- {label}: {meaning}" for label, meaning in contract["definitions"].items()
    )
    few_shot_text = "\n".join(
        f'  Answer: "{ans}" -> {label}' for ans, label in contract["few_shot"]
    )
    return f"""You are classifying one manager's answer to a single Hearth Worldview question for Pathway Portal.

Context: {contract["question_context"]}

The exact question asked: {question_text}

The manager's exact answer: {answer_text}

IMPORTANT: the question is specifically whether this occurrence is part of a
LARGER, RECURRING PATTERN (e.g. this manager doing this repeatedly, or across
multiple check-ins/creators) — NOT simply whether this particular delay feels
long, normal, or concerning in isolation. A delay can feel completely routine
and still be part of a recurring pattern for this manager; judge pattern vs.
isolated occurrence specifically, not general normalcy.

Choose exactly one conclusion from this closed set, using ONLY the manager's
answer and the question context above. Do not invent context, facts, dates,
or history that are not present in the answer.

{definitions_text}

Examples (for calibration only — do not copy wording):
{few_shot_text}

Return your conclusion, a confidence between 0.0 and 1.0, and a short (one or
two sentence) reason grounded only in the manager's actual words."""


def _call_ollama_structured(family, version, question_text, answer_text,
                             host=None, model=None, timeout=None):
    """Call Ollama's JSON-schema-constrained chat endpoint.

    Returns (parsed_dict_or_None, failure_category_or_None, failure_detail_or_None, latency_seconds).
    Never raises.
    """
    host = host or HEARTH_INTERPRETER_OLLAMA_HOST
    model = model or HEARTH_INTERPRETER_OLLAMA_MODEL
    timeout = HEARTH_INTERPRETER_OLLAMA_TIMEOUT if timeout is None else timeout

    try:
        import requests
    except ImportError:
        return None, "provider_unavailable", "requests library not installed", 0.0

    prompt = _build_prompt(family, version, question_text, answer_text)
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "format": _family_json_schema(),
        "stream": False,
        "options": {"temperature": 0},
    }

    start = time.monotonic()
    try:
        response = requests.post(f"{host}/api/chat", json=payload, timeout=timeout)
    except Exception as exc:
        latency = time.monotonic() - start
        category = _classify_request_exception(exc)
        return None, category, str(exc)[:300], latency
    latency = time.monotonic() - start

    if response.status_code != 200:
        return None, "provider_error", f"HTTP {response.status_code}: {response.text[:300]}", latency

    try:
        outer = response.json()
        content = outer["message"]["content"]
        parsed = json.loads(content)
    except Exception as exc:
        return None, "malformed_output", f"could not parse structured response: {exc}", latency

    if not _well_typed(parsed):
        return None, "malformed_output", f"response missing/invalid required fields: {parsed!r}"[:300], latency

    model_identifier = outer.get("model") or model
    return {
        "conclusion": parsed["conclusion"],
        "confidence": float(parsed["confidence"]),
        "reason": str(parsed["reason"]),
        "model_identifier": model_identifier,
    }, None, None, latency


def _classify_request_exception(exc):
    name = type(exc).__name__
    if "Timeout" in name:
        return "timeout"
    if "ConnectionError" in name:
        return "connection_error"
    return "provider_error"


def _well_typed(parsed):
    if not isinstance(parsed, dict):
        return False
    if parsed.get("conclusion") not in _TAXONOMY_SET:
        return False
    try:
        conf = float(parsed.get("confidence"))
    except (TypeError, ValueError):
        return False
    if not (0.0 <= conf <= 1.0):
        return False
    if not isinstance(parsed.get("reason"), str) or not parsed.get("reason"):
        return False
    return True


# ---------------------------------------------------------------------------
# Optional Gemini fallback (disabled by default)
# ---------------------------------------------------------------------------

def _call_gemini_structured(family, version, question_text, answer_text):
    """Guarded fallback classifier. Same contract/taxonomy as Ollama.

    Returns (parsed_dict_or_None, failure_category_or_None, failure_detail_or_None, latency_seconds).
    Never raises.
    """
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return None, "provider_unavailable", "GEMINI_API_KEY not set", 0.0

    try:
        from google import genai
    except ImportError:
        return None, "provider_unavailable", "google-genai not installed", 0.0

    try:
        from hearth_gemini_config import GEMINI_MODEL_NAME
    except ImportError:
        GEMINI_MODEL_NAME = "gemini-2.5-flash"

    prompt = _build_prompt(family, version, question_text, answer_text) + (
        "\n\nReturn ONLY valid JSON in this exact format:\n"
        '{"conclusion": "expected_pattern", "confidence": 0.8, "reason": "..."}\n'
        "Do not include markdown or any other text."
    )

    start = time.monotonic()
    try:
        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(model=GEMINI_MODEL_NAME, contents=prompt)
        raw = response.text.strip() if response.text else ""
    except Exception as exc:
        latency = time.monotonic() - start
        return None, "provider_error", str(exc)[:300], latency
    latency = time.monotonic() - start

    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        return None, "malformed_output", f"could not parse Gemini response as JSON: {exc}", latency

    if not _well_typed(parsed):
        return None, "malformed_output", f"response missing/invalid required fields: {parsed!r}"[:300], latency

    return {
        "conclusion": parsed["conclusion"],
        "confidence": float(parsed["confidence"]),
        "reason": str(parsed["reason"]),
        "model_identifier": GEMINI_MODEL_NAME,
    }, None, None, latency


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def _gemini_fallback_eligible(uncertainty_row, ollama_status, ollama_result):
    """Fallback is only for genuine semantic ambiguity, never for bare/empty/
    unrelated answers or infrastructure failures (see module docs, section 8)."""
    if not HEARTH_INTERPRETER_GEMINI_FALLBACK_ENABLED:
        return False
    if ollama_status != "ambiguous":
        return False
    answer_norm = _normalize(uncertainty_row["answer_text"])
    if not answer_norm:
        return False
    if answer_norm.lower().strip(" .!?") in _BARE_ANSWERS:
        return False
    return True


def process_one_uncertainty(conn, uncertainty_row, interpreter_version=None,
                             gemini_calls_remaining=0):
    """Process a single eligible uncertainty end-to-end: create a durable
    pending attempt, call the local model, validate, persist the outcome,
    and (only if genuinely warranted) attempt the guarded Gemini fallback.

    Returns a dict summarizing what happened, for batch-level logging/counts.
    Never raises — any unexpected error is caught and recorded as a failed
    attempt so the broader batch/Reflection pass is unaffected.
    """
    interpreter_version = interpreter_version or HEARTH_INTERPRETER_VERSION
    uncertainty_id = uncertainty_row["id"]
    question_text = uncertainty_row["possible_question"] or uncertainty_row["uncertainty_text"]
    answer_text = uncertainty_row["answer_text"]

    latest = _latest_attempt_for_version(conn, uncertainty_id, interpreter_version)
    retry_of = latest["id"] if latest is not None else None

    attempt_id = create_pending_attempt(
        conn, uncertainty_id, _ACTIVE_FAMILY, _ACTIVE_VERSION,
        interpreter_version, retry_of_attempt_id=retry_of,
    )

    outcome = {"uncertainty_id": uncertainty_id, "attempt_id": attempt_id, "gemini_used": False}

    if not HEARTH_INTERPRETER_OLLAMA_ENABLED:
        mark_attempt_failed(conn, attempt_id, "provider_disabled", "Ollama disabled by configuration")
        outcome["status"] = "failed"
        return outcome

    mark_attempt_dispatched(conn, attempt_id, "ollama", HEARTH_INTERPRETER_OLLAMA_MODEL, None)
    result, failure_category, failure_detail, latency = _call_ollama_structured(
        _ACTIVE_FAMILY, _ACTIVE_VERSION, question_text, answer_text,
    )
    outcome["latency"] = latency

    if result is None:
        mark_attempt_failed(conn, attempt_id, failure_category, failure_detail)
        outcome["status"] = "failed"
        outcome["failure_category"] = failure_category
        return outcome

    conn.execute(
        "UPDATE hearth_answer_interpretations SET model_identifier = ? WHERE id = ?;",
        (result["model_identifier"], attempt_id),
    )
    conn.commit()

    validated = validate_interpretation(
        question_text, answer_text, result["conclusion"], result["reason"]
    )

    if validated.status == "succeeded":
        mark_attempt_succeeded(
            conn, attempt_id, result["conclusion"], validated.conclusion,
            result["confidence"], result["reason"], validated.validation_reason,
        )
        outcome["status"] = "succeeded"
        outcome["conclusion"] = validated.conclusion
        return outcome

    if validated.status == "rejected_by_validation":
        mark_attempt_rejected(
            conn, attempt_id, result["conclusion"], result["confidence"],
            result["reason"], validated.validation_reason,
        )
        outcome["status"] = "rejected_by_validation"
        return outcome

    # ambiguous — consider the guarded Gemini fallback before finalizing
    if gemini_calls_remaining > 0 and _gemini_fallback_eligible(
        uncertainty_row, validated.status, result
    ):
        fb_attempt_id = create_pending_attempt(
            conn, uncertainty_id, _ACTIVE_FAMILY, _ACTIVE_VERSION,
            interpreter_version, retry_of_attempt_id=attempt_id,
        )
        mark_attempt_ambiguous(
            conn, attempt_id, result["conclusion"], result["confidence"],
            result["reason"], validated.validation_reason,
        )
        mark_attempt_dispatched(conn, fb_attempt_id, "gemini", None, None)
        fb_result, fb_fail_cat, fb_fail_detail, fb_latency = _call_gemini_structured(
            _ACTIVE_FAMILY, _ACTIVE_VERSION, question_text, answer_text,
        )
        outcome["gemini_used"] = True
        outcome["gemini_latency"] = fb_latency
        if fb_result is None:
            mark_attempt_failed(conn, fb_attempt_id, fb_fail_cat, fb_fail_detail)
            outcome["status"] = "failed"
            outcome["failure_category"] = fb_fail_cat
            return outcome
        conn.execute(
            "UPDATE hearth_answer_interpretations SET model_identifier = ? WHERE id = ?;",
            (fb_result["model_identifier"], fb_attempt_id),
        )
        conn.commit()
        fb_validated = validate_interpretation(
            question_text, answer_text, fb_result["conclusion"], fb_result["reason"]
        )
        if fb_validated.status == "succeeded":
            mark_attempt_succeeded(
                conn, fb_attempt_id, fb_result["conclusion"], fb_validated.conclusion,
                fb_result["confidence"], fb_result["reason"], fb_validated.validation_reason,
            )
            outcome["status"] = "succeeded"
            outcome["conclusion"] = fb_validated.conclusion
        elif fb_validated.status == "rejected_by_validation":
            mark_attempt_rejected(
                conn, fb_attempt_id, fb_result["conclusion"], fb_result["confidence"],
                fb_result["reason"], fb_validated.validation_reason,
            )
            outcome["status"] = "rejected_by_validation"
        else:
            mark_attempt_ambiguous(
                conn, fb_attempt_id, fb_result["conclusion"], fb_result["confidence"],
                fb_result["reason"], fb_validated.validation_reason,
            )
            outcome["status"] = "ambiguous"
        return outcome

    mark_attempt_ambiguous(
        conn, attempt_id, result["conclusion"], result["confidence"],
        result["reason"], validated.validation_reason,
    )
    outcome["status"] = "ambiguous"
    return outcome


def _ledger_table_exists(conn):
    return bool(conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
        " AND name='hearth_answer_interpretations';"
    ).fetchone())


def _schema_ready(conn):
    """Read-only precondition check — never mutates schema.

    hearth_answer_interpretations is created for a fresh database by the
    same routine "ensure tables" startup call every other Hearth table uses
    (see morning_briefing.run_pipeline(), which calls
    ensure_answer_interpretations_table() alongside ensure_reflections_table()/
    ensure_questions_table() — that's CREATE TABLE IF NOT EXISTS, identical
    in kind and risk to what those siblings already do on every single
    Reflection pass, so it is retained rather than treated as a hazard).

    question_family/question_version on hearth_worldview_uncertainties are a
    different kind of schema change (ALTER TABLE on a table that may already
    hold live rows) with no such routine-startup precedent anywhere in this
    module's own core library — see hearth_worldview._has_question_family_columns.
    An existing database that hasn't run migrate_add_question_family_fields.py
    yet is a real, expected possibility, and this function's job is to detect
    that and let the caller skip gracefully instead of crashing Reflection.
    """
    import hearth_worldview
    return _ledger_table_exists(conn) and hearth_worldview._has_question_family_columns(conn)


def process_eligible_answers(conn, batch_size=None, interpreter_version=None):
    """Bounded background pass: find eligible answered uncertainties, durably
    record and validate an interpretation attempt for each, never raising
    into the caller (Reflection). This is the only entry point the Reflection
    pass should call.
    """
    summary = {
        "eligible_found": 0, "processed": 0, "succeeded": 0, "ambiguous": 0,
        "rejected_by_validation": 0, "failed": 0, "gemini_calls": 0, "skipped": 0,
        "enabled": HEARTH_INTERPRETER_ENABLED, "schema_missing": False,
    }

    if not HEARTH_INTERPRETER_ENABLED:
        logger.info("[interpreter] disabled via HEARTH_INTERPRETER_ENABLED — skipping batch")
        return summary

    if not _schema_ready(conn):
        logger.warning(
            "[interpreter] Build 1 schema not present yet (run"
            " migrate_add_question_family_fields.py and"
            " migrate_add_answer_interpretations.py against this database) —"
            " skipping this batch; Reflection continues normally."
        )
        print(
            "[HEARTH INTERPRETER] schema not migrated yet — run"
            " migrate_add_question_family_fields.py and"
            " migrate_add_answer_interpretations.py; skipping this batch."
        )
        summary["schema_missing"] = True
        return summary

    try:
        eligible = get_eligible_answered_uncertainties(
            conn, batch_size=batch_size, interpreter_version=interpreter_version,
        )
    except Exception as exc:
        logger.warning("[interpreter] eligibility query failed, skipping batch: %s", exc)
        print(f"[HEARTH INTERPRETER] batch aborted before start: {exc}")
        return summary

    summary["eligible_found"] = len(eligible)
    print(
        f"[HEARTH INTERPRETER] batch start: family={_ACTIVE_FAMILY} version={_ACTIVE_VERSION}"
        f" eligible={len(eligible)}"
    )

    gemini_calls_remaining = (
        HEARTH_INTERPRETER_GEMINI_MAX_FALLBACK_CALLS_PER_RUN
        if HEARTH_INTERPRETER_GEMINI_FALLBACK_ENABLED else 0
    )

    for row in eligible:
        try:
            outcome = process_one_uncertainty(
                conn, row, interpreter_version=interpreter_version,
                gemini_calls_remaining=gemini_calls_remaining,
            )
        except Exception as exc:
            logger.warning(
                "[interpreter] unexpected error processing uncertainty_id=%s: %s",
                row["id"], exc,
            )
            summary["failed"] += 1
            summary["skipped"] += 1
            continue

        summary["processed"] += 1
        if outcome.get("gemini_used"):
            gemini_calls_remaining -= 1
            summary["gemini_calls"] += 1
        status = outcome.get("status", "failed")
        if status in summary:
            summary[status] += 1

    print(
        f"[HEARTH INTERPRETER] batch end: processed={summary['processed']}"
        f" succeeded={summary['succeeded']} ambiguous={summary['ambiguous']}"
        f" rejected={summary['rejected_by_validation']} failed={summary['failed']}"
        f" gemini_calls={summary['gemini_calls']}"
        + (" (batch limit reached)" if summary["eligible_found"] >= (
            batch_size or HEARTH_INTERPRETER_BATCH_SIZE
          ) else "")
    )
    return summary


# ---------------------------------------------------------------------------
# Deterministic aggregation — model-free, ordinary SQL/Python only
# ---------------------------------------------------------------------------

def aggregate_family(conn, family=_ACTIVE_FAMILY, version=_ACTIVE_VERSION):
    """Deterministic, model-free aggregate report for one question family.

    Never creates a belief, candidate, or lesson — purely observational
    counting over hearth_worldview_uncertainties + hearth_answer_interpretations.
    """
    uncertainties = conn.execute(
        "SELECT * FROM hearth_worldview_uncertainties"
        " WHERE status = 'answered' AND question_family = ? AND question_version = ?;",
        (family, version),
    ).fetchall()
    eligible_ids = [row["id"] for row in uncertainties]

    report = {
        "family": family,
        "version": version,
        "total_answered_eligible": len(eligible_ids),
        "total_accepted": 0,
        "total_pending": 0,
        "total_attempted": 0,
        "total_failed": 0,
        "total_ambiguous_or_rejected": 0,
        "by_label": {label: 0 for label in TAXONOMY},
        "distinct_subjects_total": 0,
        "distinct_subjects_by_label": {label: set() for label in TAXONOMY},
        "distinct_answering_managers": 0,
    }

    if not eligible_ids:
        report["distinct_subjects_by_label"] = {
            label: 0 for label in report["distinct_subjects_by_label"]
        }
        return report

    placeholders = ",".join("?" for _ in eligible_ids)
    unc_by_id = {row["id"]: row for row in uncertainties}

    accepted_rows = conn.execute(
        f"SELECT * FROM hearth_answer_interpretations"
        f" WHERE is_current_accepted = 1 AND uncertainty_id IN ({placeholders});",
        eligible_ids,
    ).fetchall()
    accepted_by_uncertainty = {r["uncertainty_id"]: r for r in accepted_rows}

    subjects_seen = set()
    answering_managers = set()
    for uid in eligible_ids:
        unc = unc_by_id[uid]
        if unc["subject_id"] is not None:
            subjects_seen.add((unc["subject_type"], unc["subject_id"]))
        if unc["answered_by"]:
            answering_managers.add(unc["answered_by"])

        accepted = accepted_by_uncertainty.get(uid)
        if accepted is not None:
            report["total_accepted"] += 1
            label = accepted["conclusion"]
            if label in report["by_label"]:
                report["by_label"][label] += 1
                if unc["subject_id"] is not None:
                    report["distinct_subjects_by_label"][label].add(
                        (unc["subject_type"], unc["subject_id"])
                    )
            continue

        latest = _latest_attempt_for_version(conn, uid, HEARTH_INTERPRETER_VERSION)
        if latest is None:
            report["total_pending"] += 1
        elif latest["status"] in ("pending", "attempted"):
            report["total_attempted"] += 1
        elif latest["status"] == "failed":
            report["total_failed"] += 1
        elif latest["status"] in ("ambiguous", "rejected_by_validation"):
            report["total_ambiguous_or_rejected"] += 1
        else:
            report["total_pending"] += 1

    report["distinct_subjects_total"] = len(subjects_seen)
    report["distinct_subjects_by_label"] = {
        label: len(subject_set)
        for label, subject_set in report["distinct_subjects_by_label"].items()
    }
    report["distinct_answering_managers"] = len(answering_managers)
    return report
