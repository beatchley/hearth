"""
Hearth Answer Interpreter — Build 2: the general-purpose manager-answer
learning engine.

Turns a manager's answer to ANY properly stamped Worldview question
(question_family + question_version both non-null) into a durable, auditable
interpretation attempt, using a local model first (Ollama, JSON-schema-
constrained) and an optional guarded Gemini fallback, gated by deterministic
(non-model) validation plus a constrained local-model semantic check before
anything is treated as accepted.

Build 1 (see git history — the module docstring used to say "Scope for
Build 1: only question_family='checkin_feedback_waiting'") special-cased one
family end to end: a hand-authored FAMILY_DEFINITIONS taxonomy, a fixed
5-value conclusion vocabulary, keyword/phrase trigger lists, and module-level
_ACTIVE_FAMILY/_ACTIVE_VERSION constants baked into eligibility and stamping.
None of that exists anymore. This module never branches on question_family —
it reasons from whatever question_text/why_it_matters/answer_text a given
row actually contains, and stamps every attempt from that row's own
question_family/question_version. A brand-new question family with no
special-cased code anywhere becomes interpretable the moment a generator
stamps it (see hearth_soul.py's _QUESTION_FAMILIES_BY_EPISODE_TYPE).

This module does NOT create beliefs, candidates promoted to beliefs, or
change Hearth's future judgment, close/resolve uncertainties, or alter
question-generation behavior. It only interprets and durably records
structured claims, and (Build 2) surfaces repeated structured claims as
learning candidates for human review — see compute_learning_candidates().
Promotion into durable organizational knowledge remains a separate,
human-reviewed future action.

Runs only from the background Reflection pass (see morning_briefing.py's
call to process_eligible_answers() and compute_learning_candidates()). Never
called from the live Ask Hearth request path, and never called synchronously
from the answer-save route.

Historical Build 1 rows remain fully readable: their raw_conclusion/
conclusion columns (the old closed pattern taxonomy) are untouched, and the
new universal_status/raw_status/raw_claims_json columns are simply NULL on
those rows. See get_review_rows() for how both generations are presented
uniformly.
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

# Identifies the engine/schema behavior — NOT a question family. A version
# bump here means "the interpretation contract changed"; question_family/
# question_version (stamped per-row by the generator) remain the separate,
# orthogonal identity of *what was asked*. Build 1's default
# ("checkin_feedback_waiting_v1") named a family and is gone — see
# docs/HEARTH_MIND_09_QUESTIONS_AND_MAINTENANCE.md for why that coupling was
# structural, not cosmetic.
HEARTH_INTERPRETER_VERSION = os.environ.get(
    "HEARTH_INTERPRETER_VERSION", "manager_answer_general_v1"
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

# Constrained local-model (never Gemini) second pass that checks one
# structurally-valid claim at a time against the manager's actual words —
# see PART 6 of the Build 2 design doc / _call_ollama_semantic_check(). On by
# default because deterministic structural checks alone cannot honestly
# establish semantic entailment (see module docs).
HEARTH_INTERPRETER_SEMANTIC_CHECK_ENABLED = _env_bool(
    "HEARTH_INTERPRETER_SEMANTIC_CHECK_ENABLED", "1"
)

# Emergency operational kill-switch only — a comma-separated list of
# question_family values to temporarily skip. Deliberately NOT a semantic
# registry: it carries no meaning about what any family IS (contrast with
# Build 1's FAMILY_DEFINITIONS), only whether Reflection should currently
# attempt it. Empty by default: every properly stamped family is eligible.
HEARTH_INTERPRETER_FAMILY_DENYLIST = os.environ.get(
    "HEARTH_INTERPRETER_FAMILY_DENYLIST", ""
)

# Learning-candidate thresholds (Part 11) — conservative defaults requiring
# repeated, independent evidence before a structured claim surfaces for
# human review. Configurable for parity with other Hearth threshold configs
# (e.g. concern-volume thresholds in hearth_soul.py) without importing any
# concern-specific semantics.
HEARTH_LEARNING_CANDIDATE_MIN_INTERPRETATIONS = _env_int(
    "HEARTH_LEARNING_CANDIDATE_MIN_INTERPRETATIONS", 3
)
HEARTH_LEARNING_CANDIDATE_MIN_DISTINCT_SUBJECTS = _env_int(
    "HEARTH_LEARNING_CANDIDATE_MIN_DISTINCT_SUBJECTS", 3
)

# Ledger contract version (distinct from HEARTH_INTERPRETER_VERSION, which
# identifies the engine; this identifies the row *shape*). Build 1 rows were
# written with SCHEMA_VERSION "v1" (closed taxonomy, no claims table). Build
# 2 rows are "v2" (universal status + structured claims). Never rewritten
# retroactively on old rows.
SCHEMA_VERSION = "v2"


# ---------------------------------------------------------------------------
# Universal contract
# ---------------------------------------------------------------------------

UNIVERSAL_STATUSES = ("supported", "insufficient_information", "unrelated_or_unclear")
_UNIVERSAL_STATUS_SET = frozenset(UNIVERSAL_STATUSES)

REQUIRED_CLAIM_FIELDS = (
    "subject", "predicate", "value", "polarity", "scope",
    "temporal_status", "conclusion_text", "evidence_quote",
)
VALID_POLARITIES = frozenset({"affirmed", "negated", "unknown"})
VALID_TEMPORAL_STATUSES = frozenset({"current", "past", "future", "ongoing", "unknown"})
MAX_CLAIMS_PER_ANSWER = 3

_NEGATION_MARKERS = (
    "no", "not", "n't", "never", "without", "none", "neither", "nor",
    "denies", "denied", "cannot", "can't",
)

# Common capitalized words that are not proper nouns/entities — kept short
# and conservative; only exists to reduce false positives in the entity-
# grounding heuristic below, never to define family-specific vocabulary.
_ENTITY_GROUNDING_STOPWORDS = frozenset({
    "the", "this", "that", "these", "those", "it", "is", "are", "was",
    "were", "a", "an", "manager", "creator", "coach", "yes", "no", "i",
    "we", "they", "he", "she", "today", "yesterday", "currently",
    "overall", "generally", "hearth", "there", "here", "so", "but",
    "and", "or", "if", "when", "while", "since", "because", "however",
})
_CAPITALIZED_TOKEN_RE = re.compile(r"\b[A-Z][a-zA-Z]{2,}\b")


def _normalize_ws(text):
    return re.sub(r"\s+", " ", (text or "").strip())


def _normalize_quotes(text):
    return (text or "").replace("‘", "'").replace("’", "'") \
        .replace("“", '"').replace("”", '"')


def _normalize_for_quote_match(text):
    return _normalize_ws(_normalize_quotes(text))


def _family_denylist():
    return frozenset(
        f.strip() for f in HEARTH_INTERPRETER_FAMILY_DENYLIST.split(",") if f.strip()
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
    """Create/upgrade every table this module owns. Safe to call repeatedly.

    Mirrors migrate_add_answer_interpretations.py and
    migrate_add_universal_claims_schema.py for a genuinely fresh database —
    same CREATE TABLE IF NOT EXISTS convention already used by
    hearth_worldview.ensure_worldview_tables()/hearth_soul.ensure_reflections_table()/
    hearth_questions.ensure_questions_table(). Called once per Reflection
    pass from morning_briefing.run_pipeline()'s startup block, alongside
    those siblings — NOT from process_eligible_answers() below, which only
    ever reads schema state (see _schema_ready()) and skips gracefully
    rather than creating anything at interpretation time.
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

    for col, typ in (
        ("universal_status", "TEXT"),
        ("raw_status", "TEXT"),
        ("raw_claims_json", "TEXT"),
    ):
        try:
            conn.execute(f"ALTER TABLE hearth_answer_interpretations ADD COLUMN {col} {typ};")
            conn.commit()
        except sqlite3.OperationalError as exc:
            if "duplicate column name" not in str(exc).lower():
                raise

    conn.executescript("""
        CREATE TABLE IF NOT EXISTS hearth_answer_interpretation_claims (
            id                      INTEGER PRIMARY KEY AUTOINCREMENT,
            interpretation_id       INTEGER NOT NULL,
            uncertainty_id          INTEGER NOT NULL,
            question_family         TEXT    NOT NULL,
            question_version        INTEGER NOT NULL,
            claim_index             INTEGER NOT NULL,
            subject                 TEXT,
            predicate               TEXT,
            value                   TEXT,
            polarity                TEXT,
            scope                   TEXT,
            temporal_status         TEXT,
            conclusion_text         TEXT,
            evidence_quote          TEXT,
            accepted                INTEGER NOT NULL DEFAULT 0,
            rejection_reason        TEXT,
            semantic_check_provider TEXT,
            semantic_check_model    TEXT,
            semantic_check_result   TEXT,
            semantic_check_reason   TEXT,
            created_at              TEXT    NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_claims_interpretation
            ON hearth_answer_interpretation_claims (interpretation_id);
        CREATE INDEX IF NOT EXISTS idx_claims_uncertainty
            ON hearth_answer_interpretation_claims (uncertainty_id);
        CREATE INDEX IF NOT EXISTS idx_claims_family_version
            ON hearth_answer_interpretation_claims (question_family, question_version);
        CREATE INDEX IF NOT EXISTS idx_claims_predicate_value
            ON hearth_answer_interpretation_claims (predicate, value);
        CREATE INDEX IF NOT EXISTS idx_claims_accepted
            ON hearth_answer_interpretation_claims (accepted);

        CREATE TABLE IF NOT EXISTS hearth_learning_candidates (
            id                      INTEGER PRIMARY KEY AUTOINCREMENT,
            question_family         TEXT    NOT NULL,
            question_version        INTEGER NOT NULL,
            predicate_key           TEXT    NOT NULL,
            value_key               TEXT    NOT NULL,
            polarity                TEXT    NOT NULL,
            scope_key               TEXT    NOT NULL,
            interpretation_count    INTEGER NOT NULL DEFAULT 0,
            distinct_subject_count  INTEGER NOT NULL DEFAULT 0,
            first_observed_at       TEXT,
            last_observed_at        TEXT,
            sample_conclusion_text  TEXT,
            sample_evidence_quote   TEXT,
            status                  TEXT    NOT NULL DEFAULT 'pending',
            created_at              TEXT    NOT NULL,
            updated_at              TEXT    NOT NULL,
            UNIQUE (question_family, question_version, predicate_key, value_key, polarity, scope_key)
        );
        CREATE INDEX IF NOT EXISTS idx_candidates_family_version
            ON hearth_learning_candidates (question_family, question_version);
        CREATE INDEX IF NOT EXISTS idx_candidates_status
            ON hearth_learning_candidates (status);

        CREATE TABLE IF NOT EXISTS hearth_learning_candidate_members (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            candidate_id  INTEGER NOT NULL,
            claim_id      INTEGER NOT NULL,
            created_at    TEXT    NOT NULL,
            UNIQUE (candidate_id, claim_id)
        );
        CREATE INDEX IF NOT EXISTS idx_candidate_members_candidate
            ON hearth_learning_candidate_members (candidate_id);
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

    question_family/question_version are always the CALLING row's own
    stamped identity — never a module-level default — so every attempt
    carries its own provenance regardless of which family produced it.
    Returns the new attempt's id.
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


def _finalize_attempt(conn, attempt_id, *, status, raw_status=None, universal_status=None,
                       raw_claims=None, model_reason=None, validation_result=None,
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
        " SET status = ?, raw_status = ?, universal_status = ?, raw_claims_json = ?,"
        "     model_reason = ?, validation_result = ?, validation_reason = ?,"
        "     failure_category = ?, failure_detail = ?, is_current_accepted = ?,"
        "     completed_at = ?"
        " WHERE id = ?;",
        (status, raw_status, universal_status,
         json.dumps(raw_claims) if raw_claims is not None else None,
         model_reason, validation_result, validation_reason, failure_category,
         failure_detail, 1 if is_current_accepted else 0, now, attempt_id),
    )
    conn.commit()


def mark_attempt_succeeded(conn, attempt_id, raw_status, universal_status, raw_claims,
                            model_reason, validation_reason):
    _finalize_attempt(
        conn, attempt_id, status="succeeded", raw_status=raw_status,
        universal_status=universal_status, raw_claims=raw_claims, model_reason=model_reason,
        validation_result="accepted", validation_reason=validation_reason,
        is_current_accepted=True,
    )


def mark_attempt_ambiguous(conn, attempt_id, raw_status, raw_claims, model_reason,
                            validation_reason):
    """Not currently called by the Build 2 orchestrator — a status=supported
    attempt whose claims all fail validation is now accepted as
    insufficient_information instead (see _finalize_via_validation), since
    that is itself a safe, definitive conclusion rather than something
    requiring human review. Kept for schema completeness/possible future use
    and because historical Build 1 rows may already carry status='ambiguous'
    (retry-eligibility logic still treats it as terminal — see
    get_eligible_answered_uncertainties()).
    """
    _finalize_attempt(
        conn, attempt_id, status="ambiguous", raw_status=raw_status, raw_claims=raw_claims,
        model_reason=model_reason, validation_result="ambiguous",
        validation_reason=validation_reason,
    )


def mark_attempt_rejected(conn, attempt_id, raw_status, raw_claims, model_reason,
                           validation_reason):
    _finalize_attempt(
        conn, attempt_id, status="rejected_by_validation", raw_status=raw_status,
        raw_claims=raw_claims, model_reason=model_reason, validation_result="rejected",
        validation_reason=validation_reason,
    )


def mark_attempt_failed(conn, attempt_id, failure_category, failure_detail):
    _finalize_attempt(
        conn, attempt_id, status="failed", validation_result="not_applicable",
        failure_category=failure_category, failure_detail=failure_detail,
    )


def store_claims(conn, interpretation_id, uncertainty_id, question_family, question_version,
                  claims):
    """Persist every finalized claim (accepted AND rejected) for one attempt.

    Storing rejected claims too — with their rejection_reason and any
    semantic-check provenance — is what makes "validation outcome/reasons"
    reviewable per-claim on the generalized admin page (Part 12), not just
    as an opaque raw_claims_json blob. Only accepted=1 rows are ever read by
    grouping/compute_learning_candidates(), so this never inflates learning
    candidates.
    """
    now = _now()
    for idx, c in enumerate(claims):
        conn.execute(
            "INSERT INTO hearth_answer_interpretation_claims"
            " (interpretation_id, uncertainty_id, question_family, question_version,"
            "  claim_index, subject, predicate, value, polarity, scope, temporal_status,"
            "  conclusion_text, evidence_quote, accepted, rejection_reason,"
            "  semantic_check_provider, semantic_check_model, semantic_check_result,"
            "  semantic_check_reason, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);",
            (interpretation_id, uncertainty_id, question_family, question_version, idx,
             c.get("subject"), c.get("predicate"), c.get("value"), c.get("polarity"),
             c.get("scope"), c.get("temporal_status"), c.get("conclusion_text"),
             c.get("evidence_quote"), 1 if c.get("accepted") else 0, c.get("rejection_reason"),
             c.get("semantic_check_provider"), c.get("semantic_check_model"),
             c.get("semantic_check_result"), c.get("semantic_check_reason"), now),
        )
    conn.commit()


def get_claims_for_interpretation(conn, interpretation_id):
    return conn.execute(
        "SELECT * FROM hearth_answer_interpretation_claims"
        " WHERE interpretation_id = ? ORDER BY claim_index ASC;",
        (interpretation_id,),
    ).fetchall()


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
# Eligibility — family-agnostic
# ---------------------------------------------------------------------------

def get_eligible_answered_uncertainties(conn, batch_size=None, interpreter_version=None,
                                         max_retries=None, retry_cooldown_seconds=None):
    """Return answered uncertainties eligible for interpretation this run,
    across EVERY stamped question_family/version, bounded by batch_size,
    oldest-answered first.

    Eligible means: answered with non-empty text, both question_family and
    question_version stamped (NULL on either means the generator never
    opted this row into the learning loop — see hearth_soul.py), not on the
    emergency denylist, no current accepted interpretation, and — if a
    previous attempt for the current interpreter_version exists — that
    attempt is either a retryable 'failed'/stuck 'pending'/'attempted' row
    past its cooldown and under the retry cap, never an 'ambiguous' or
    'rejected_by_validation' terminal result (those stay put for human
    review rather than being resent to a model every Reflection run) or an
    already-successful one (excluded by the NOT IN below, which is
    intentionally version-agnostic: at most one accepted interpretation per
    uncertainty, ever, regardless of which interpreter_version produced it).
    """
    batch_size = HEARTH_INTERPRETER_BATCH_SIZE if batch_size is None else batch_size
    interpreter_version = interpreter_version or HEARTH_INTERPRETER_VERSION
    max_retries = HEARTH_INTERPRETER_MAX_RETRIES if max_retries is None else max_retries
    cooldown = (
        HEARTH_INTERPRETER_RETRY_COOLDOWN_SECONDS
        if retry_cooldown_seconds is None else retry_cooldown_seconds
    )
    denylist = _family_denylist()

    candidates = conn.execute(
        "SELECT * FROM hearth_worldview_uncertainties"
        " WHERE status = 'answered'"
        "   AND answer_text IS NOT NULL AND TRIM(answer_text) != ''"
        "   AND question_family IS NOT NULL AND question_version IS NOT NULL"
        "   AND id NOT IN ("
        "       SELECT uncertainty_id FROM hearth_answer_interpretations"
        "       WHERE is_current_accepted = 1"
        "   )"
        " ORDER BY answered_at ASC;"
    ).fetchall()

    eligible = []
    for row in candidates:
        if row["question_family"] in denylist:
            continue
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


def list_known_family_versions(conn):
    """Return distinct (question_family, question_version) pairs seen across
    stamped uncertainties, for admin filter UIs. Never a semantic registry —
    purely a reflection of what has actually been stamped so far.
    """
    rows = conn.execute(
        "SELECT DISTINCT question_family, question_version"
        " FROM hearth_worldview_uncertainties"
        " WHERE question_family IS NOT NULL AND question_version IS NOT NULL"
        " ORDER BY question_family ASC, question_version ASC;"
    ).fetchall()
    return [(r["question_family"], r["question_version"]) for r in rows]


# ---------------------------------------------------------------------------
# Generic prompt + JSON schemas
# ---------------------------------------------------------------------------

def _build_prompt(question_text, why_it_matters, context_text, answer_text):
    """Build the interpretation prompt from ONLY the actual row's content.

    No family name, no unconditional check-in/creator/pattern language, no
    hand-authored examples — see module docs and PART 3 of the design doc.
    why_it_matters/context_text are included only when present.
    """
    sections = [f'The question Hearth asked: "{question_text}"']
    if why_it_matters:
        sections.append(f'Why this question matters to Hearth: "{why_it_matters}"')
    if context_text and context_text.strip() != (question_text or "").strip():
        sections.append(f'Background Hearth recorded about this situation: "{context_text}"')
    sections.append(f'The manager\'s exact answer: "{answer_text}"')
    context_block = "\n\n".join(sections)

    return f"""You are helping Hearth (an internal organizational memory system for
Pathway Portal) safely interpret one manager's answer to one question Hearth
asked. Use ONLY the information below — never outside knowledge, never
assumptions, never facts not present in the question, its context, or the
answer.

{context_block}

Decide exactly one status:
- "supported": the answer contains grounded evidence that establishes at
  least one claim directly relevant to the question above.
- "insufficient_information": the answer is relevant or potentially relevant
  but does not establish a safe conclusion (ambiguous, overly tentative,
  missing needed detail, or too short to resolve what the question asks).
- "unrelated_or_unclear": the answer does not meaningfully address the
  question, is nonsensical in context, or cannot be connected to it.

If the manager's answer is short (e.g. a bare "yes" or "no"): decide based on
the shape of the question itself. If the question is a direct yes/no
question with an unambiguous referent, a bare "yes"/"no" MAY be enough to
establish exactly the proposition the question asked — nothing more. If the
question asks why, how, who, what happened, what action is needed, or
otherwise requires explanation, a bare yes/no is insufficient_information
because it supplies no explanation. Never expand a bare answer into details
that are not present in the question or answer.

If status is "supported", return one to three (never more than three)
distinct structured claims the answer actually establishes about this
specific question. If the answer supports more than three, choose only the
three most directly relevant to the question — do not produce a sprawling
summary. If status is anything other than "supported", return zero claims.

Each claim must have:
- subject: the specific person, project, event, process, or entity this
  claim concerns. Use only an identity already present in the question,
  its context, or the answer — never invent a new one.
- predicate: a short, concise, normalized machine-usable concept you
  generate from the answer (e.g. "discord_requirement", "onboarding_blocker",
  "follow_up_owner", "current_status"). You do not need to match any
  pre-existing vocabulary.
- value: a short, concise, normalized value you generate from the evidence
  (e.g. "optional", "waiting_on_staff", "temporary_spike", "resolved"). You
  do not need to match any pre-existing vocabulary.
- polarity: "affirmed", "negated", or "unknown" — preserve whether the
  manager affirmed or denied this claim.
- scope: whether this is about one individual case or a broad statement
  (e.g. "this_creator", "this_onboarding", "this_incident", "organizational",
  or "unknown" if it cannot be determined).
- temporal_status: "current", "past", "future", "ongoing", or "unknown".
- conclusion_text: one short, plain-English, grounded sentence stating the
  claim.
- evidence_quote: a short excerpt copied VERBATIM from the manager's answer
  above that directly supports this claim. It must be an exact quote, not a
  paraphrase.

Do not infer unstated organizational facts. Do not treat your own confidence
as evidence — ground every claim only in the manager's actual words. If the
answer is ambiguous, tentative, irrelevant, or insufficient, do not force a
claim; return status="insufficient_information" or "unrelated_or_unclear"
with zero claims instead."""


def _response_json_schema():
    return {
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": list(UNIVERSAL_STATUSES)},
            "claims": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "subject": {"type": "string"},
                        "predicate": {"type": "string"},
                        "value": {"type": "string"},
                        "polarity": {"type": "string", "enum": list(VALID_POLARITIES)},
                        "scope": {"type": "string"},
                        "temporal_status": {"type": "string", "enum": list(VALID_TEMPORAL_STATUSES)},
                        "conclusion_text": {"type": "string"},
                        "evidence_quote": {"type": "string"},
                    },
                    "required": list(REQUIRED_CLAIM_FIELDS),
                },
            },
            "reason": {"type": "string"},
        },
        "required": ["status", "claims", "reason"],
    }


def _semantic_check_prompt(question_text, why_it_matters, answer_text, claim):
    why_line = f'\nWhy this question matters to Hearth: "{why_it_matters}"' if why_it_matters else ""
    return f"""You previously extracted one proposed claim from a manager's answer to a
question Hearth asked. Verify ONLY whether the manager's answer actually,
directly supports this specific claim — using nothing but the manager's own
words. Do not use outside knowledge or assumptions.

The question Hearth asked: "{question_text}"{why_line}
The manager's exact answer: "{answer_text}"

Proposed claim:
- predicate: {claim.get("predicate")}
- value: {claim.get("value")}
- polarity: {claim.get("polarity")}
- conclusion: {claim.get("conclusion_text")}
- evidence quoted from the answer: "{claim.get("evidence_quote")}"

Does the manager's answer clearly and directly establish this claim? Return
supported=true only if it does. Return supported=false if the claim
overstates, misreads, or is not actually grounded in the manager's answer."""


def _semantic_check_json_schema():
    return {
        "type": "object",
        "properties": {
            "supported": {"type": "boolean"},
            "reason": {"type": "string"},
        },
        "required": ["supported", "reason"],
    }


# ---------------------------------------------------------------------------
# Deterministic validation — no model, no family-specific vocabulary
# ---------------------------------------------------------------------------

def _strip_sentence_initial_words(text):
    """Drop the first word of every sentence in `text`.

    Free-form prose (conclusion_text) always capitalizes its first word as a
    matter of ordinary grammar, not because it names an entity — without
    this, the entity-grounding heuristic below would flag nearly every claim
    on its opening word alone. A multi-word invented name mid-sentence (or
    a second+ word of an invented name that starts a sentence) is still
    caught; only a single-word invented entity used as literally the first
    word of conclusion_text could slip past this specific check — the
    constrained semantic check is the backstop for that gap (see module docs).
    """
    sentences = re.split(r"(?<=[.!?])\s+", text or "")
    remainders = []
    for sentence in sentences:
        parts = sentence.split(" ", 1)
        if len(parts) > 1:
            remainders.append(parts[1])
    return " ".join(remainders)


def _extract_ungrounded_entities(text, source_text):
    """Conservative heuristic: capitalized tokens in `text` that are not
    present anywhere (case-insensitive) in `source_text`, excluding a short
    stoplist of common capitalized non-entity words.

    This is honest about its limits (see module docs / PART 6): it can only
    catch a NEW capitalized name/entity the model invented out of thin air.
    It cannot verify deeper semantic entailment — that is what the
    constrained local-model semantic check exists for.
    """
    if not text:
        return []
    source_lower = (source_text or "").lower()
    ungrounded = []
    for token in _CAPITALIZED_TOKEN_RE.findall(text):
        if token.lower() in _ENTITY_GROUNDING_STOPWORDS:
            continue
        if token.lower() in source_lower:
            continue
        ungrounded.append(token)
    return ungrounded


def _claim_key(claim):
    return (
        _normalize_ws(str(claim.get("predicate", ""))).lower(),
        _normalize_ws(str(claim.get("value", ""))).lower(),
        str(claim.get("polarity", "")).lower(),
        _normalize_ws(str(claim.get("scope", ""))).lower(),
    )


def deterministic_validate(question_text, why_it_matters, context_text, answer_text,
                            raw_status, raw_claims):
    """Pure, model-free validation. Returns a dict:
        {
          "contract_violation": str | None,   # if set, the whole attempt is rejected
          "claim_checks": [
              {"claim": dict, "structurally_valid": bool, "reason": str | None}, ...
          ],
        }

    Never consults model confidence anywhere (see module docs — confidence is
    not even part of the claim schema). Only decides what deterministic code
    can honestly guarantee (evidence-quote presence, claim count, required
    fields, one-directional negation grounding, duplicate detection, a
    conservative entity-invention heuristic, and top-level status/claim-count
    consistency). Semantic entailment is intentionally NOT decided here —
    see _call_ollama_semantic_check.
    """
    if raw_status not in _UNIVERSAL_STATUS_SET:
        return {"contract_violation": f"model returned an invalid status: {raw_status!r}",
                "claim_checks": []}

    if not isinstance(raw_claims, list):
        return {"contract_violation": "model's claims field was not a list", "claim_checks": []}

    if raw_status != "supported" and len(raw_claims) > 0:
        return {
            "contract_violation": (
                f"status={raw_status!r} but {len(raw_claims)} claim(s) were returned —"
                " non-supported statuses must have zero claims"
            ),
            "claim_checks": [],
        }

    if raw_status == "supported" and len(raw_claims) == 0:
        return {
            "contract_violation": "status=supported but zero claims were returned",
            "claim_checks": [],
        }

    if len(raw_claims) > MAX_CLAIMS_PER_ANSWER:
        return {
            "contract_violation": f"{len(raw_claims)} claims proposed — never more than "
                                   f"{MAX_CLAIMS_PER_ANSWER} are allowed",
            "claim_checks": [],
        }

    if raw_status != "supported":
        return {"contract_violation": None, "claim_checks": []}

    answer_norm = _normalize_for_quote_match(answer_text)
    source_text = " ".join(filter(None, [question_text, why_it_matters, context_text, answer_text]))

    claim_checks = []
    seen_keys = set()
    for claim in raw_claims:
        if not isinstance(claim, dict):
            claim_checks.append({"claim": {}, "structurally_valid": False,
                                  "reason": "claim was not a JSON object"})
            continue

        missing = [f for f in REQUIRED_CLAIM_FIELDS if not str(claim.get(f, "")).strip()]
        if missing:
            claim_checks.append({"claim": claim, "structurally_valid": False,
                                  "reason": f"missing required field(s): {', '.join(missing)}"})
            continue

        if claim.get("polarity") not in VALID_POLARITIES:
            claim_checks.append({"claim": claim, "structurally_valid": False,
                                  "reason": f"invalid polarity: {claim.get('polarity')!r}"})
            continue

        if claim.get("temporal_status") not in VALID_TEMPORAL_STATUSES:
            claim_checks.append({"claim": claim, "structurally_valid": False,
                                  "reason": f"invalid temporal_status: {claim.get('temporal_status')!r}"})
            continue

        key = _claim_key(claim)
        if key in seen_keys:
            claim_checks.append({"claim": claim, "structurally_valid": False,
                                  "reason": "duplicate claim within the same answer"
                                            " (same predicate/value/polarity/scope)"})
            continue

        # Case-insensitive on top of whitespace/quote normalization — a model
        # that re-cases a word while copying (e.g. answer says "discord",
        # claim quotes "Discord") is still quoting verbatim in substance.
        # This is NOT paraphrase tolerance: the quote must still appear as a
        # contiguous substring, just without case sensitivity. The ORIGINAL
        # evidence_quote (and the original answer_text) are preserved as-is
        # everywhere else — only this comparison folds case.
        quote_norm = _normalize_for_quote_match(claim.get("evidence_quote"))
        if not quote_norm or quote_norm.lower() not in answer_norm.lower():
            claim_checks.append({"claim": claim, "structurally_valid": False,
                                  "reason": "evidence_quote does not appear verbatim"
                                            " (after whitespace/quote/case normalization) in"
                                            " the manager's answer"})
            continue

        ungrounded = _extract_ungrounded_entities(
            claim.get("subject", ""), source_text,
        ) + _extract_ungrounded_entities(
            _strip_sentence_initial_words(claim.get("conclusion_text", "")), source_text,
        )
        if ungrounded:
            claim_checks.append({"claim": claim, "structurally_valid": False,
                                  "reason": "introduces entity/name not present in the"
                                            f" question, context, or answer: {ungrounded[0]!r}"})
            continue

        if claim.get("polarity") == "negated":
            quote_lower = str(claim.get("evidence_quote", "")).lower()
            if not any(f" {m} " in f" {quote_lower} " or quote_lower.startswith(f"{m} ")
                       or quote_lower.endswith(f" {m}") or quote_lower == m
                       for m in _NEGATION_MARKERS):
                claim_checks.append({"claim": claim, "structurally_valid": False,
                                      "reason": "polarity=negated but evidence_quote contains"
                                                " no discernible negation marker"})
                continue

        seen_keys.add(key)
        claim_checks.append({"claim": claim, "structurally_valid": True, "reason": None})

    return {"contract_violation": None, "claim_checks": claim_checks}


# ---------------------------------------------------------------------------
# Ollama structured-output client
# ---------------------------------------------------------------------------

def _post_ollama(prompt, schema, host, model, timeout):
    """Shared low-level Ollama call. Returns (parsed_dict_or_None,
    failure_category_or_None, failure_detail_or_None, latency_seconds,
    model_identifier_or_None). Never raises.
    """
    try:
        import requests
    except ImportError:
        return None, "provider_unavailable", "requests library not installed", 0.0, None

    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "format": schema,
        "stream": False,
        "options": {"temperature": 0},
    }

    start = time.monotonic()
    try:
        response = requests.post(f"{host}/api/chat", json=payload, timeout=timeout)
    except Exception as exc:
        latency = time.monotonic() - start
        return None, _classify_request_exception(exc), str(exc)[:300], latency, None
    latency = time.monotonic() - start

    if response.status_code != 200:
        return (None, "provider_error", f"HTTP {response.status_code}: {response.text[:300]}",
                latency, None)

    try:
        outer = response.json()
        content = outer["message"]["content"]
        parsed = json.loads(content)
    except Exception as exc:
        return None, "malformed_output", f"could not parse structured response: {exc}", latency, None

    return parsed, None, None, latency, (outer.get("model") or model)


def _classify_request_exception(exc):
    name = type(exc).__name__
    if "Timeout" in name:
        return "timeout"
    if "ConnectionError" in name:
        return "connection_error"
    return "provider_error"


def _well_typed_response(parsed):
    if not isinstance(parsed, dict):
        return False
    if parsed.get("status") not in _UNIVERSAL_STATUS_SET:
        return False
    if not isinstance(parsed.get("claims"), list):
        return False
    if not isinstance(parsed.get("reason"), str) or not parsed.get("reason"):
        return False
    return True


def _call_ollama_structured(question_text, why_it_matters, context_text, answer_text,
                             host=None, model=None, timeout=None):
    """Returns (parsed_dict_or_None, failure_category_or_None,
    failure_detail_or_None, latency_seconds). Never raises.
    """
    host = host or HEARTH_INTERPRETER_OLLAMA_HOST
    model = model or HEARTH_INTERPRETER_OLLAMA_MODEL
    timeout = HEARTH_INTERPRETER_OLLAMA_TIMEOUT if timeout is None else timeout

    prompt = _build_prompt(question_text, why_it_matters, context_text, answer_text)
    parsed, failure_category, failure_detail, latency, model_identifier = _post_ollama(
        prompt, _response_json_schema(), host, model, timeout,
    )
    if parsed is None:
        return None, failure_category, failure_detail, latency
    if not _well_typed_response(parsed):
        return None, "malformed_output", f"response missing/invalid required fields: {parsed!r}"[:300], latency

    return {
        "status": parsed["status"],
        "claims": parsed["claims"],
        "reason": str(parsed["reason"]),
        "model_identifier": model_identifier,
    }, None, None, latency


def _call_ollama_semantic_check(question_text, why_it_matters, answer_text, claim,
                                 host=None, model=None, timeout=None):
    """Constrained local-model second pass for ONE structurally-valid claim.

    Local-model-first per module docs — never Gemini. Sees only the
    question/context, the manager's answer, and the single proposed claim;
    returns a tightly constrained supported/not-supported decision; never
    rewrites the claim. Returns (parsed_dict_or_None, failure_category,
    failure_detail, latency).
    """
    host = host or HEARTH_INTERPRETER_OLLAMA_HOST
    model = model or HEARTH_INTERPRETER_OLLAMA_MODEL
    timeout = HEARTH_INTERPRETER_OLLAMA_TIMEOUT if timeout is None else timeout

    prompt = _semantic_check_prompt(question_text, why_it_matters, answer_text, claim)
    parsed, failure_category, failure_detail, latency, model_identifier = _post_ollama(
        prompt, _semantic_check_json_schema(), host, model, timeout,
    )
    if parsed is None:
        return None, failure_category, failure_detail, latency
    if not isinstance(parsed, dict) or not isinstance(parsed.get("supported"), bool) \
            or not isinstance(parsed.get("reason"), str) or not parsed.get("reason"):
        return None, "malformed_output", f"response missing/invalid required fields: {parsed!r}"[:300], latency

    return {
        "supported": parsed["supported"],
        "reason": parsed["reason"],
        "model_identifier": model_identifier,
    }, None, None, latency


# ---------------------------------------------------------------------------
# Optional Gemini fallback (disabled by default) — same generic contract
# ---------------------------------------------------------------------------

def _call_gemini_structured(question_text, why_it_matters, context_text, answer_text):
    """Guarded fallback classifier. Same generic contract/schema as Ollama.

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

    prompt = _build_prompt(question_text, why_it_matters, context_text, answer_text) + (
        "\n\nReturn ONLY valid JSON in this exact format:\n"
        '{"status": "supported", "claims": [...], "reason": "..."}\n'
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

    if not _well_typed_response(parsed):
        return None, "malformed_output", f"response missing/invalid required fields: {parsed!r}"[:300], latency

    return {
        "status": parsed["status"],
        "claims": parsed["claims"],
        "reason": str(parsed["reason"]),
        "model_identifier": GEMINI_MODEL_NAME,
    }, None, None, latency


# ---------------------------------------------------------------------------
# Orchestration — family-agnostic
# ---------------------------------------------------------------------------

def _gemini_fallback_eligible():
    """Fallback is only for the genuine-ambiguity case (a supported status
    whose claims all failed validation — see _finalize_via_validation's
    zero_claims_survived signal), never for infrastructure failures or hard
    contract violations (see module docs). The call site is the sole gate on
    *which* case counts as eligible; this only gates the enabled/disabled
    switch and cap.
    """
    return HEARTH_INTERPRETER_GEMINI_FALLBACK_ENABLED


def _resolve_claims(conn, question_text, why_it_matters, answer_text, claim_checks):
    """Run the semantic check on every structurally-valid claim and return
    (final_claims, accepted_count). final_claims includes BOTH accepted and
    rejected claims, each carrying its own accepted flag and rejection
    reason/semantic-check provenance — see store_claims().
    """
    final_claims = []
    accepted_count = 0
    for cc in claim_checks:
        claim = cc["claim"]
        if not cc["structurally_valid"]:
            final_claims.append({**claim, "accepted": False, "rejection_reason": cc["reason"]})
            continue

        if not HEARTH_INTERPRETER_SEMANTIC_CHECK_ENABLED:
            final_claims.append({**claim, "accepted": True, "rejection_reason": None,
                                  "semantic_check_result": "skipped"})
            accepted_count += 1
            continue

        sem_result, sem_fail_cat, sem_fail_detail, _lat = _call_ollama_semantic_check(
            question_text, why_it_matters, answer_text, claim,
        )
        if sem_result is None:
            final_claims.append({
                **claim, "accepted": False,
                "rejection_reason": f"semantic_check_unavailable: {sem_fail_detail}",
                "semantic_check_provider": "ollama", "semantic_check_result": "unavailable",
            })
            continue

        if sem_result["supported"]:
            final_claims.append({
                **claim, "accepted": True, "rejection_reason": None,
                "semantic_check_provider": "ollama",
                "semantic_check_model": sem_result.get("model_identifier"),
                "semantic_check_result": "supported",
                "semantic_check_reason": sem_result["reason"],
            })
            accepted_count += 1
        else:
            final_claims.append({
                **claim, "accepted": False,
                "rejection_reason": f"semantic_check_failed: {sem_result['reason']}",
                "semantic_check_provider": "ollama",
                "semantic_check_model": sem_result.get("model_identifier"),
                "semantic_check_result": "not_supported",
                "semantic_check_reason": sem_result["reason"],
            })

    return final_claims, accepted_count


def process_one_uncertainty(conn, uncertainty_row, interpreter_version=None,
                             gemini_calls_remaining=0):
    """Process a single eligible uncertainty end-to-end: create a durable
    pending attempt (stamped from the ROW's own question_family/version —
    never a module default), call the local model, deterministically
    validate, run the constrained semantic check on surviving claims,
    persist the outcome and claims, and (only if genuinely warranted)
    attempt the guarded Gemini fallback.

    Returns a dict summarizing what happened, for batch-level logging/counts.
    Never raises — any unexpected error is caught and recorded as a failed
    attempt so the broader batch/Reflection pass is unaffected.
    """
    interpreter_version = interpreter_version or HEARTH_INTERPRETER_VERSION
    uncertainty_id = uncertainty_row["id"]
    question_family = uncertainty_row["question_family"]
    question_version = uncertainty_row["question_version"]
    question_text = uncertainty_row["possible_question"] or uncertainty_row["uncertainty_text"]
    why_it_matters = uncertainty_row["why_it_matters"]
    context_text = uncertainty_row["uncertainty_text"]
    answer_text = uncertainty_row["answer_text"]

    latest = _latest_attempt_for_version(conn, uncertainty_id, interpreter_version)
    retry_of = latest["id"] if latest is not None else None

    attempt_id = create_pending_attempt(
        conn, uncertainty_id, question_family, question_version,
        interpreter_version, retry_of_attempt_id=retry_of,
    )

    outcome = {"uncertainty_id": uncertainty_id, "attempt_id": attempt_id, "gemini_used": False}

    if not HEARTH_INTERPRETER_OLLAMA_ENABLED:
        mark_attempt_failed(conn, attempt_id, "provider_disabled", "Ollama disabled by configuration")
        outcome["status"] = "failed"
        return outcome

    mark_attempt_dispatched(conn, attempt_id, "ollama", HEARTH_INTERPRETER_OLLAMA_MODEL, None)
    result, failure_category, failure_detail, latency = _call_ollama_structured(
        question_text, why_it_matters, context_text, answer_text,
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

    outcome_result = _finalize_via_validation(
        conn, attempt_id, uncertainty_id, question_family, question_version,
        question_text, why_it_matters, context_text, answer_text, result,
    )
    outcome.update(outcome_result)

    if outcome_result.get("zero_claims_survived") and gemini_calls_remaining > 0 \
            and _gemini_fallback_eligible():
        fb_attempt_id = create_pending_attempt(
            conn, uncertainty_id, question_family, question_version,
            interpreter_version, retry_of_attempt_id=attempt_id,
        )
        mark_attempt_dispatched(conn, fb_attempt_id, "gemini", None, None)
        fb_result, fb_fail_cat, fb_fail_detail, fb_latency = _call_gemini_structured(
            question_text, why_it_matters, context_text, answer_text,
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

        fb_outcome = _finalize_via_validation(
            conn, fb_attempt_id, uncertainty_id, question_family, question_version,
            question_text, why_it_matters, context_text, answer_text, fb_result,
        )
        outcome.update(fb_outcome)

    return outcome


def _finalize_via_validation(conn, attempt_id, uncertainty_id, question_family, question_version,
                              question_text, why_it_matters, context_text, answer_text, result):
    """Shared deterministic-validate -> (semantic-check) -> persist path,
    used for both the primary Ollama attempt and any Gemini fallback attempt.
    """
    raw_status = result["status"]
    raw_claims = result["claims"]
    model_reason = result["reason"]

    det = deterministic_validate(question_text, why_it_matters, context_text, answer_text,
                                  raw_status, raw_claims)

    if det["contract_violation"]:
        mark_attempt_rejected(conn, attempt_id, raw_status, raw_claims, model_reason,
                               det["contract_violation"])
        return {"status": "rejected_by_validation"}

    if raw_status != "supported":
        mark_attempt_succeeded(conn, attempt_id, raw_status, raw_status, raw_claims,
                                model_reason, "model's conservative status accepted as-is;"
                                              " no claims to validate")
        return {"status": "succeeded", "universal_status": raw_status}

    final_claims, accepted_count = _resolve_claims(
        conn, question_text, why_it_matters, answer_text, det["claim_checks"],
    )

    if accepted_count >= 1:
        mark_attempt_succeeded(
            conn, attempt_id, raw_status, "supported", raw_claims, model_reason,
            f"{accepted_count} of {len(final_claims)} proposed claim(s) passed deterministic"
            " and semantic validation",
        )
        store_claims(conn, attempt_id, uncertainty_id, question_family, question_version,
                      final_claims)
        return {"status": "succeeded", "universal_status": "supported"}

    # The model proposed status=supported but none of its claims survived
    # deterministic/semantic validation. This is NOT the same as an
    # unresolved/ambiguous state needing human review: "none of what you
    # said actually held up" is itself a safe, definitive conclusion — the
    # accepted SYSTEM status is downgraded to insufficient_information while
    # the model's RAW status (supported) and every rejected claim + reason
    # are preserved for audit. Never accept a "supported" interpretation
    # with zero accepted claims (see module docs).
    mark_attempt_succeeded(
        conn, attempt_id, raw_status, "insufficient_information", raw_claims, model_reason,
        f"all_proposed_claims_failed_validation: {len(final_claims)} claim(s) proposed,"
        " 0 passed deterministic/semantic validation — raw model status was 'supported'"
        " but no claim could be safely grounded, so the accepted status was downgraded"
        " to insufficient_information",
    )
    store_claims(conn, attempt_id, uncertainty_id, question_family, question_version,
                  final_claims)
    return {"status": "succeeded", "universal_status": "insufficient_information",
            "zero_claims_survived": True}


def _ledger_table_exists(conn):
    return bool(conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
        " AND name='hearth_answer_interpretations';"
    ).fetchone())


def _claims_table_exists(conn):
    return bool(conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
        " AND name='hearth_answer_interpretation_claims';"
    ).fetchone())


def _schema_ready(conn):
    """Read-only precondition check — never mutates schema.

    hearth_answer_interpretations/claims/candidates tables are created for a
    fresh database by ensure_answer_interpretations_table(), called from
    morning_briefing.run_pipeline() alongside its siblings. question_family/
    question_version on hearth_worldview_uncertainties are a different kind
    of schema change (ALTER TABLE on a table that may already hold live
    rows) with no such routine-startup precedent — see
    hearth_worldview._has_question_family_columns. An existing database that
    hasn't run migrate_add_question_family_fields.py yet is a real, expected
    possibility, and this function's job is to detect that and let the
    caller skip gracefully instead of crashing Reflection.
    """
    import hearth_worldview
    return (
        _ledger_table_exists(conn) and _claims_table_exists(conn)
        and hearth_worldview._has_question_family_columns(conn)
    )


def process_eligible_answers(conn, batch_size=None, interpreter_version=None):
    """Bounded background pass: find eligible answered uncertainties ACROSS
    EVERY stamped question_family, durably record and validate an
    interpretation attempt for each, never raising into the caller
    (Reflection). This is the only entry point the Reflection pass should
    call for interpretation.
    """
    summary = {
        "eligible_found": 0, "processed": 0, "succeeded": 0, "ambiguous": 0,
        "rejected_by_validation": 0, "failed": 0, "gemini_calls": 0, "skipped": 0,
        "enabled": HEARTH_INTERPRETER_ENABLED, "schema_missing": False,
        "families_processed": set(),
    }

    if not HEARTH_INTERPRETER_ENABLED:
        logger.info("[interpreter] disabled via HEARTH_INTERPRETER_ENABLED — skipping batch")
        return summary

    if not _schema_ready(conn):
        logger.warning(
            "[interpreter] Build 2 schema not present yet (run"
            " migrate_add_question_family_fields.py, migrate_add_answer_interpretations.py,"
            " and migrate_add_universal_claims_schema.py against this database) —"
            " skipping this batch; Reflection continues normally."
        )
        print(
            "[HEARTH INTERPRETER] schema not migrated yet — run"
            " migrate_add_question_family_fields.py, migrate_add_answer_interpretations.py,"
            " and migrate_add_universal_claims_schema.py; skipping this batch."
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
    families_found = sorted({row["question_family"] for row in eligible})
    print(f"[HEARTH INTERPRETER] batch start: eligible={len(eligible)} families={families_found}")

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
                "[interpreter] unexpected error processing uncertainty_id=%s (family=%s): %s",
                row["id"], row["question_family"], exc,
            )
            summary["failed"] += 1
            summary["skipped"] += 1
            continue

        summary["processed"] += 1
        summary["families_processed"].add(row["question_family"])
        if outcome.get("gemini_used"):
            gemini_calls_remaining -= 1
            summary["gemini_calls"] += 1
        status = outcome.get("status", "failed")
        if status in summary:
            summary[status] += 1

    summary["families_processed"] = sorted(summary["families_processed"])
    print(
        f"[HEARTH INTERPRETER] batch end: processed={summary['processed']}"
        f" succeeded={summary['succeeded']} ambiguous={summary['ambiguous']}"
        f" rejected={summary['rejected_by_validation']} failed={summary['failed']}"
        f" gemini_calls={summary['gemini_calls']} families={summary['families_processed']}"
        + (" (batch limit reached)" if summary["eligible_found"] >= (
            batch_size or HEARTH_INTERPRETER_BATCH_SIZE
          ) else "")
    )
    return summary


# ---------------------------------------------------------------------------
# Deterministic aggregation — model-free, ordinary SQL/Python only
# ---------------------------------------------------------------------------

def aggregate_family(conn, family, version, interpreter_version=None):
    """Deterministic, model-free aggregate report for one question family.

    Never creates a belief, candidate, or lesson — purely observational
    counting over hearth_worldview_uncertainties + hearth_answer_interpretations.
    interpreter_version defaults to the CURRENT engine version (never a
    family-specific one) — passed explicitly so callers reviewing an older
    interpreter_version's pending/attempted counts can override it.

    by_label spans both generations of accepted rows: legacy Build 1 rows
    (5-value pattern taxonomy, stored in `conclusion`) and Build 2 rows
    (3-value universal status, stored in `universal_status`) — whichever is
    populated on the accepted row is used as the label, so historical
    check-in-feedback data keeps reporting exactly as it did before.
    """
    interpreter_version = interpreter_version or HEARTH_INTERPRETER_VERSION
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
        "by_label": {},
        "distinct_subjects_total": 0,
        "distinct_subjects_by_label": {},
        "distinct_answering_managers": 0,
    }

    if not eligible_ids:
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
    subjects_by_label = {}
    for uid in eligible_ids:
        unc = unc_by_id[uid]
        if unc["subject_id"] is not None:
            subjects_seen.add((unc["subject_type"], unc["subject_id"]))
        if unc["answered_by"]:
            answering_managers.add(unc["answered_by"])

        accepted = accepted_by_uncertainty.get(uid)
        if accepted is not None:
            report["total_accepted"] += 1
            label = accepted["universal_status"] or accepted["conclusion"]
            if label:
                report["by_label"][label] = report["by_label"].get(label, 0) + 1
                if unc["subject_id"] is not None:
                    subjects_by_label.setdefault(label, set()).add(
                        (unc["subject_type"], unc["subject_id"])
                    )
            continue

        latest = _latest_attempt_for_version(conn, uid, interpreter_version)
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
        label: len(subject_set) for label, subject_set in subjects_by_label.items()
    }
    report["distinct_answering_managers"] = len(answering_managers)
    return report


# ---------------------------------------------------------------------------
# Generalized review query — family-neutral admin support
# ---------------------------------------------------------------------------

def get_review_rows(conn, question_family=None, question_version=None, universal_status=None,
                     attempt_status=None, interpreter_version=None, provider=None, limit=200):
    """Return uncertainty+attempt+claims rows for the generalized admin
    review page, across every family unless filtered. Pure read, no model
    calls. Presents both Build 1 (legacy taxonomy) and Build 2 (universal
    status/claims) rows uniformly — legacy rows simply have empty claims and
    a legacy `conclusion` label instead of `universal_status`.
    """
    clauses = ["u.question_family IS NOT NULL", "u.question_version IS NOT NULL"]
    params = []
    if question_family:
        clauses.append("u.question_family = ?")
        params.append(question_family)
    if question_version:
        clauses.append("u.question_version = ?")
        params.append(question_version)

    rows = conn.execute(
        "SELECT u.* FROM hearth_worldview_uncertainties u"
        f" WHERE {' AND '.join(clauses)}"
        " ORDER BY COALESCE(u.answered_at, u.updated_at) DESC"
        " LIMIT ?;",
        params + [limit],
    ).fetchall()

    results = []
    for unc in rows:
        attempts = get_attempts_for_uncertainty(conn, unc["id"])
        if not attempts:
            continue
        accepted = get_current_accepted(conn, unc["id"])
        latest = attempts[-1]
        reference = accepted or latest

        if attempt_status and latest["status"] != attempt_status:
            continue
        ref_universal = reference["universal_status"] or reference["conclusion"]
        if universal_status and ref_universal != universal_status:
            continue
        if interpreter_version and latest["interpreter_version"] != interpreter_version:
            continue
        if provider and reference["provider"] != provider:
            continue

        claims = get_claims_for_interpretation(conn, reference["id"]) if reference else []
        results.append({
            "uncertainty_id": unc["id"],
            "question_family": unc["question_family"],
            "question_version": unc["question_version"],
            "subject_type": unc["subject_type"],
            "subject_id": unc["subject_id"],
            "question_text": unc["possible_question"] or unc["uncertainty_text"],
            "why_it_matters": unc["why_it_matters"],
            "answer_text": unc["answer_text"],
            "answered_by": unc["answered_by"],
            "answered_at": unc["answered_at"],
            "status": unc["status"],
            "interpretation_state": latest["status"],
            "universal_status": ref_universal,
            "provider": reference["provider"] if reference else None,
            "model_reason": reference["model_reason"] if reference else None,
            "validation_result": reference["validation_result"] if reference else None,
            "validation_reason": reference["validation_reason"] if reference else None,
            "interpreter_version": latest["interpreter_version"],
            "failure_category": latest["failure_category"],
            "failure_detail": latest["failure_detail"],
            "attempt_count": len(attempts),
            "last_attempt_at": latest["attempted_at"],
            "claims": [dict(c) for c in claims],
            "attempts": [dict(a) for a in attempts],
        })

    return results


# ---------------------------------------------------------------------------
# PART 10/11 — simple structured grouping + durable learning candidates
# ---------------------------------------------------------------------------

def normalize_claim_group_key(predicate, value, scope):
    """Conservative structural normalization for grouping — lowercase, trim,
    collapse whitespace/separators, and a narrow deterministic singular/
    plural fold. No synonym invention, no embeddings.
    """
    def _norm(text):
        text = _normalize_ws(text or "").lower()
        text = re.sub(r"[\s\-]+", "_", text)
        text = re.sub(r"[^\w]", "", text)
        if len(text) > 4 and text.endswith("s") and not text.endswith("ss"):
            text = text[:-1]
        return text

    return _norm(predicate), _norm(value), _norm(scope)


def compute_learning_candidates(conn, min_interpretations=None, min_distinct_subjects=None):
    """Recompute durable learning-candidate rollups from every currently
    ACCEPTED structured claim. Idempotent — safe to call every Reflection
    pass. Never deletes an existing candidate, never overwrites a human-set
    review status; only grows counts/timestamps/membership.

    Duplicate claims from one answer cannot inflate a candidate's count
    because deterministic_validate() already rejects duplicates within one
    attempt before they are ever stored as accepted=1. Repeated claims from
    one subject alone cannot satisfy the distinct-subject threshold because
    distinct_subject_count is computed over DISTINCT (subject_type,
    subject_id), not over claim/interpretation rows.
    """
    min_interpretations = (
        HEARTH_LEARNING_CANDIDATE_MIN_INTERPRETATIONS
        if min_interpretations is None else min_interpretations
    )
    min_distinct_subjects = (
        HEARTH_LEARNING_CANDIDATE_MIN_DISTINCT_SUBJECTS
        if min_distinct_subjects is None else min_distinct_subjects
    )

    claim_rows = conn.execute(
        "SELECT c.*, u.subject_type AS unc_subject_type, u.subject_id AS unc_subject_id,"
        "       i.is_current_accepted AS interp_is_current_accepted"
        " FROM hearth_answer_interpretation_claims c"
        " JOIN hearth_answer_interpretations i ON i.id = c.interpretation_id"
        " JOIN hearth_worldview_uncertainties u ON u.id = c.uncertainty_id"
        " WHERE c.accepted = 1 AND i.is_current_accepted = 1;"
    ).fetchall()

    groups = {}
    for row in claim_rows:
        predicate_key, value_key, scope_key = normalize_claim_group_key(
            row["predicate"], row["value"], row["scope"],
        )
        polarity = (row["polarity"] or "unknown").lower()
        gkey = (row["question_family"], row["question_version"], predicate_key, value_key,
                polarity, scope_key)
        g = groups.setdefault(gkey, {"claim_ids": [], "uncertainty_ids": set(), "subjects": set(),
                                      "sample_conclusion_text": row["conclusion_text"],
                                      "sample_evidence_quote": row["evidence_quote"]})
        g["claim_ids"].append(row["id"])
        g["uncertainty_ids"].add(row["uncertainty_id"])
        if row["unc_subject_id"] is not None:
            g["subjects"].add((row["unc_subject_type"], row["unc_subject_id"]))

    now = _now()
    surfaced = []
    for (family, version, predicate_key, value_key, polarity, scope_key), g in groups.items():
        interpretation_count = len(g["uncertainty_ids"])
        distinct_subject_count = len(g["subjects"])
        if interpretation_count < min_interpretations or distinct_subject_count < min_distinct_subjects:
            continue

        existing = conn.execute(
            "SELECT * FROM hearth_learning_candidates"
            " WHERE question_family = ? AND question_version = ? AND predicate_key = ?"
            "   AND value_key = ? AND polarity = ? AND scope_key = ?;",
            (family, version, predicate_key, value_key, polarity, scope_key),
        ).fetchone()

        if existing:
            candidate_id = existing["id"]
            conn.execute(
                "UPDATE hearth_learning_candidates"
                " SET interpretation_count = ?, distinct_subject_count = ?,"
                "     last_observed_at = ?, updated_at = ?"
                " WHERE id = ?;",
                (interpretation_count, distinct_subject_count, now, now, candidate_id),
            )
        else:
            cur = conn.execute(
                "INSERT INTO hearth_learning_candidates"
                " (question_family, question_version, predicate_key, value_key, polarity,"
                "  scope_key, interpretation_count, distinct_subject_count, first_observed_at,"
                "  last_observed_at, sample_conclusion_text, sample_evidence_quote, status,"
                "  created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?);",
                (family, version, predicate_key, value_key, polarity, scope_key,
                 interpretation_count, distinct_subject_count, now, now,
                 g["sample_conclusion_text"], g["sample_evidence_quote"], now, now),
            )
            candidate_id = cur.lastrowid

        for claim_id in g["claim_ids"]:
            conn.execute(
                "INSERT OR IGNORE INTO hearth_learning_candidate_members"
                " (candidate_id, claim_id, created_at) VALUES (?, ?, ?);",
                (candidate_id, claim_id, now),
            )

        surfaced.append(candidate_id)

    conn.commit()
    return surfaced


def get_learning_candidates(conn, question_family=None, question_version=None, status=None):
    clauses = []
    params = []
    if question_family:
        clauses.append("question_family = ?")
        params.append(question_family)
    if question_version:
        clauses.append("question_version = ?")
        params.append(question_version)
    if status:
        clauses.append("status = ?")
        params.append(status)
    sql = "SELECT * FROM hearth_learning_candidates"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY last_observed_at DESC;"
    return conn.execute(sql, params).fetchall()


def get_learning_candidate_examples(conn, candidate_id, limit=5):
    """Contributing claim/interpretation examples for one candidate, for
    human review — joined back to the uncertainty/answer they came from.
    """
    return conn.execute(
        "SELECT c.*, u.possible_question, u.uncertainty_text, u.answer_text, u.answered_by,"
        "       u.subject_type, u.subject_id"
        " FROM hearth_learning_candidate_members m"
        " JOIN hearth_answer_interpretation_claims c ON c.id = m.claim_id"
        " JOIN hearth_worldview_uncertainties u ON u.id = c.uncertainty_id"
        " WHERE m.candidate_id = ?"
        " ORDER BY c.created_at DESC"
        " LIMIT ?;",
        (candidate_id, limit),
    ).fetchall()


def set_learning_candidate_status(conn, candidate_id, status):
    if status not in ("pending", "reviewed", "rejected", "promoted"):
        raise ValueError(f"invalid learning candidate status: {status!r}")
    conn.execute(
        "UPDATE hearth_learning_candidates SET status = ?, updated_at = ? WHERE id = ?;",
        (status, _now(), candidate_id),
    )
    conn.commit()
