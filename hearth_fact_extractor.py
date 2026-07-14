"""
Hearth Furniture Fact Extractor — V1.

Reads organizational text from approved Pathway sources and proposes
durable Furniture facts about Buildings, for human review. This is not a
Watcher in the "did something happen?" sense — it asks "did Hearth learn a
durable fact about a Building?"

It NEVER writes Furniture directly. It NEVER creates beliefs,
uncertainties, watched changes, or reflection references. It only writes
rows to hearth_furniture_proposals; a human approves or dismisses each one
(see hearth_furniture_proposals.approve_furniture_proposal /
dismiss_furniture_proposal).

The because test (the governing rule for every decision this module makes,
not just the final confidence check): "if a statement only makes sense
because of something else, it does not belong in Furniture." Facts like
"creates cooking content" or "prefers evening livestreams" pass; conclusions
like "is struggling" or "seems discouraged" do not — those belong to Soul
and Worldview.

Conservative bias, throughout: prefer false negatives over false positives.
A missed fact can surface again in a future message; a noisy or speculative
proposal spends a manager's trust. Candidate generation, prompting, and
validation are all written to err toward proposing nothing when uncertain.

Pipeline, per approved source record:
    fetch record -> build text -> check processed-source ledger ->
    generate candidate Buildings deterministically (never via Gemini,
    never via pronouns) -> one Gemini evaluation per candidate ->
    strict validation -> hybrid duplicate suppression -> write proposal

Architectural separation (no layer performs another layer's job):
    - deterministic code: candidate selection, output validation, duplicate
      checks, proposal writes
    - Gemini: Furniture extraction only (and, narrowly, semantic-equivalence
      judgments for duplicate suppression step 2)
    - a human: approval

Go-live policy: this extractor does not backfill history. It only
evaluates source records created at or after GO_LIVE_AT. The processed-
source ledger (hearth_processed_sources) is what makes daily re-runs
replay-safe technically; a future, separate backfill effort could reuse it,
but none is implemented here.

Approved sources (V1): Creator Notes, Community Posts, Community Comments,
Training Comments, Check-in answers, Support messages, Announcements.
"Live Discussions" is explicitly out of scope — its storage location is
still undefined. Private creator DMs, private creator-to-creator metadata,
and anything outside these approved sources are never inspected — this is
Hearth's permanent privacy boundary (see HEARTH_SENSORY_POLICY.md).

Source edits/deletions: no special handling. hearth_processed_sources keys
on content_hash, so an edited record is a new identity and will be
evaluated again on its own terms; a previously-written proposal is never
retroactively dismissed, flagged, or re-evaluated. Captured evidence is
part of the proposal's historical record.
"""

import hashlib
import json
import logging
import os
import re
import sqlite3
from datetime import datetime, timezone

import hearth_entity_resolution
import hearth_furniture
import hearth_furniture_proposals
import hearth_memory
from hearth_furniture_taxonomy import FURNITURE_CATEGORIES
from hearth_gemini_config import GEMINI_MODEL_NAME

logger = logging.getLogger(__name__)

EXTRACTOR_VERSION = "v1"

# Go-live cutoff: only source records created at or after this timestamp are
# ever evaluated — see module docstring's Go-live policy note. No historical
# backfill in V1. Adjust before first production run if the actual
# introduction date differs from when this constant was written.
GO_LIVE_AT = "2026-07-11T00:00:00+00:00"

# Conservative bias floor: Gemini output below this confidence is treated as
# "do not propose," even if every other validation check passes.
MIN_CONFIDENCE = 0.6

DEFAULT_BATCH_LIMIT_PER_SOURCE = 50

# How many of a Building's existing active Furniture facts / other pending
# proposals to include in the step-2 semantic-duplicate prompt. Existing
# Furniture per Building is expected to stay small (Traversal caps its own
# summary read at 5 for the same reason); this is a defensive cap, not a
# tuning target.
MAX_EXISTING_FACTS_FOR_DEDUP_CHECK = 30


# ---------------------------------------------------------------------------
# Approved sources
# ---------------------------------------------------------------------------
#
# subject_user_column: the Pathway users.id column representing who this
# record's content is naturally *about* (self-descriptive text) — feeds
# deterministic candidate generation via resolve_entity_by_user_id().
# None means "no self-describing subject; candidates come from text-mention
# scanning only" (e.g. a staff-authored announcement).
#
# author_user_column: who literally wrote/submitted the record, for
# source_author_user_id provenance on the proposal — independent of
# subject_user_column (a creator_note's author is staff; its subject is the
# creator being noted about).

SOURCE_CONFIGS = (
    {
        "source_type": "creator_notes",
        "table": "creator_notes",
        "id_column": "id",
        "text_columns": ("content",),
        "subject_user_column": "user_id",
        "author_user_column": "author_id",
        "created_at_column": "created_at",
        "deleted_column": None,
        "html": False,
    },
    {
        # Distinct source_type from creator_notes: a separate physical table
        # with its own overlapping id sequence — collapsing the two under one
        # source_type would break the (source_type, source_record_id) ledger
        # identity. "Creator Notes" is one conceptual source, two tables.
        "source_type": "shop_creator_notes",
        "table": "shop_creator_notes",
        "id_column": "id",
        "text_columns": ("content",),
        "subject_user_column": "user_id",
        "author_user_column": "author_id",
        "created_at_column": "created_at",
        "deleted_column": None,
        "html": False,
    },
    {
        "source_type": "community_posts",
        "table": "posts",
        "id_column": "id",
        "text_columns": ("title", "body"),
        "subject_user_column": "author_id",
        "author_user_column": "author_id",
        "created_at_column": "created_at",
        "deleted_column": "is_deleted",
        "html": False,
    },
    {
        "source_type": "community_comments",
        "table": "comments",
        "id_column": "id",
        "text_columns": ("body",),
        "subject_user_column": "author_id",
        "author_user_column": "author_id",
        "created_at_column": "created_at",
        "deleted_column": "is_deleted",
        "html": False,
    },
    {
        "source_type": "training_comments",
        "table": "training_comments",
        "id_column": "id",
        "text_columns": ("content",),
        "subject_user_column": "user_id",
        "author_user_column": "user_id",
        "created_at_column": "created_at",
        "deleted_column": None,
        "html": False,
    },
    {
        "source_type": "support_messages",
        "table": "support_messages",
        "id_column": "id",
        "text_columns": ("body",),
        "subject_user_column": "author_id",
        "author_user_column": "author_id",
        "created_at_column": "created_at",
        "deleted_column": None,
        "html": False,
    },
    {
        "source_type": "announcements",
        "table": "announcements",
        "id_column": "id",
        "text_columns": ("title", "content"),
        "subject_user_column": None,
        "author_user_column": "author_id",
        "created_at_column": "created_at",
        "deleted_column": None,
        "html": True,
    },
)

# checkin_answers is handled by _fetch_checkin_answers() below instead of the
# generic single-table path — its author/timestamp live on the parent
# checkin_submissions row, not on the answer row itself (see investigation).
CHECKIN_ANSWERS_SOURCE_TYPE = "checkin_answers"


# ---------------------------------------------------------------------------
# Connections
# ---------------------------------------------------------------------------

def _get_pathway_connection():
    """Open a read-only connection to the Pathway Portal database.

    Same pattern as hearth_soul.py's _collect_momentum_activity and
    morning_briefing.py's get_connection: DATABASE_URL, read-only URI mode.
    Returns None if DATABASE_URL is unset or the connection fails — a
    missing/unreachable Pathway DB must never crash the extractor.
    """
    db_url = os.environ.get("DATABASE_URL", "")
    if not db_url:
        logger.warning("[fact_extractor] DATABASE_URL not set — skipping this run.")
        return None
    db_path = db_url[len("sqlite:///"):] if db_url.startswith("sqlite:///") else db_url
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn
    except Exception as exc:
        logger.warning("[fact_extractor] Could not open Pathway DB: %s: %s", type(exc).__name__, exc)
        return None


# ---------------------------------------------------------------------------
# Source record fetching
# ---------------------------------------------------------------------------

def _strip_html(text):
    """Minimal tag stripper for announcement HTML bodies — no external deps."""
    if not text:
        return ""
    import html as _html
    text = re.sub(r"<[^>]+>", " ", text)
    text = _html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _build_text(row, config):
    parts = []
    for col in config["text_columns"]:
        val = row[col] if col in row.keys() else None
        if val:
            parts.append(_strip_html(val) if config["html"] else str(val))
    return "\n".join(p.strip() for p in parts if p and p.strip())


def _fetch_generic_records(pathway_conn, config, since_iso, limit):
    """Fetch up to limit not-necessarily-yet-processed records for one simple
    (single-table) source, created at/after since_iso, oldest first, skipping
    soft-deleted rows. Ledger filtering happens per-record in run_batch(),
    not here — see module docstring's Go-live policy note on why this SELECT
    doesn't itself try to be a replay-safety cursor."""
    where = [f"{config['created_at_column']} >= ?"]
    params = [since_iso]
    if config["deleted_column"]:
        where.append(f"({config['deleted_column']} IS NULL OR {config['deleted_column']} = 0)")

    sql = (
        f"SELECT * FROM {config['table']}"
        f" WHERE {' AND '.join(where)}"
        f" ORDER BY {config['created_at_column']} ASC LIMIT ?;"
    )
    params.append(limit)
    try:
        return pathway_conn.execute(sql, params).fetchall()
    except sqlite3.Error as exc:
        logger.warning("[fact_extractor] query failed for %s: %s", config["table"], exc)
        return []


def _fetch_checkin_answers(pathway_conn, since_iso, limit):
    """Check-in answers have no author/timestamp of their own — both live on
    the parent checkin_submissions row. Returns rows shaped like the generic
    path expects: id, content(paired question+answer), subject_user_id,
    created_at."""
    sql = (
        "SELECT a.id AS id, a.creator_response AS creator_response,"
        " q.question_text AS question_text,"
        " s.user_id AS subject_user_id, s.submitted_at AS created_at"
        " FROM checkin_answers a"
        " JOIN checkin_submissions s ON s.id = a.submission_id"
        " LEFT JOIN checkin_questions q ON q.id = a.question_id"
        " WHERE s.submitted_at >= ? AND a.creator_response IS NOT NULL"
        "   AND TRIM(a.creator_response) != ''"
        " ORDER BY s.submitted_at ASC LIMIT ?;"
    )
    try:
        return pathway_conn.execute(sql, (since_iso, limit)).fetchall()
    except sqlite3.Error as exc:
        logger.warning("[fact_extractor] query failed for checkin_answers: %s", exc)
        return []


def _is_hearth_authored(source_type, row):
    """Whether this record was authored by Hearth itself, not a human.

    None of the 7 approved source tables have a Hearth-authorship marker
    today (confirmed during investigation: is_hearth exists only on
    admin_chat_messages / role_hub_chat_messages, neither an approved
    source, and every approved-source author column is a NOT NULL FK to a
    real human user). This always returns False today. Kept as a real,
    named check rather than silently omitted, so that if a future source
    type or column ever introduces a Hearth-authorship marker, there is an
    obvious single place to wire it in — see module docstring's privacy
    boundary note.
    """
    return False


# ---------------------------------------------------------------------------
# Candidate Building generation (deterministic — never Gemini, never pronouns)
# ---------------------------------------------------------------------------

def _generate_candidates(memory_conn, text, subject_user_id):
    """Return a list of distinct hearth_entities rows this record could be
    about: the record's self-describing subject (if any), plus every
    Building whose name is literally mentioned in the text. Restricted to
    entity_type='person' — no non-person Building ever reaches these
    approved source types in practice (consistent with the equivalent
    finding for worldview entity refs)."""
    candidates = {}

    if subject_user_id is not None:
        subject_row = hearth_entity_resolution.resolve_entity_by_user_id(memory_conn, subject_user_id)
        if subject_row is not None and subject_row["entity_type"] == "person":
            candidates[subject_row["id"]] = subject_row

    for row in hearth_entity_resolution.find_entity_mentions(memory_conn, text, entity_type="person"):
        candidates[row["id"]] = row

    return list(candidates.values())


# ---------------------------------------------------------------------------
# Gemini — Furniture extraction (narrow: one Building, one source record)
# ---------------------------------------------------------------------------

_EXTRACTION_PROMPT_TEMPLATE = """You are Hearth's Furniture Fact Extractor. Your only job is to decide whether the text below teaches us one durable, stand-alone fact about ONE specific person: {display_name}.

THE BECAUSE TEST (apply this to every decision):
"If a statement only makes sense because of something else, it does not belong in Furniture."

Furniture facts stand on their own, with no reasoning required. Examples of facts that BELONG:
- Creates cooking content during livestreams.
- Enjoys woodworking.
- Frequently streams FPS games.
- Prefers evening livestreams.
- Has experience in retail management.
- Favorite food is cheeseburgers and french fries.

Furniture is NOT limited to facts about platform or creator activity. A genuinely
personal preference or trait — a favorite food, a favorite color, a hobby, a pet,
and similar — is just as valid a Furniture candidate as a platform-related one,
as long as it still passes the because test above: a standalone, durable fact
stated with reasonable confidence, needing no external context to make sense. Do
not reject a fact merely because it has nothing to do with the platform or
creator work. This does not lower the bar on confidence or evidence — the same
because test and the same conservative bias below still apply in full.

Examples that DO NOT belong (these require interpretation/reasoning — reject them):
- Is struggling because sales have dropped.
- Seems discouraged.
- Might be burning out.
- Appears highly motivated.
- Is improving lately.
- Is becoming more confident.

Allowed categories (choose exactly one, only if you find a fact): skill, interest, content, preference, role, trait, other.
"relationship" is NOT an allowed category. If the only thing you can extract is fundamentally about a relationship between {display_name} and someone else, you MUST return has_furniture_candidate: false. Do not use "other" as a fallback for a relationship fact.

Rules:
- The fact must be about {display_name} specifically, not about anyone else mentioned in the text.
- Never guess. If you are not confident the text supports a durable fact about {display_name}, return has_furniture_candidate: false.
- If uncertain, ambiguous, ANY doubt at all, or the evidence is thin — return has_furniture_candidate: false. A missed fact is fine; a wrong or speculative one is not.
- evidence_quote must be copied EXACTLY, character-for-character, from the Source Text below. Do not paraphrase, summarize, reword, or fix typos/punctuation.
- confidence is a decimal 0.0-1.0 reflecting how directly the Source Text states this fact about {display_name}.

Return ONLY valid JSON in this exact shape:
{{
  "has_furniture_candidate": true,
  "subject_name": "{display_name}",
  "fact": "Enjoys woodworking.",
  "category": "interest",
  "confidence": 0.85,
  "evidence_quote": "exact substring from the source text"
}}

If there is no Furniture candidate, return exactly:
{{"has_furniture_candidate": false}}

Do not include markdown. Do not include explanations. Do not include additional fields. Return JSON only.

Source Text:
{source_text}"""


def _call_gemini_json(gemini_client, prompt):
    """Best-effort structured-JSON Gemini call. Returns a parsed dict, or
    None on any failure (missing client, call error, unparseable response,
    non-object JSON) — mirrors hearth_comment_classifier.py's manual
    json.loads + never-raise convention."""
    if gemini_client is None:
        return None
    try:
        response = gemini_client.models.generate_content(model=GEMINI_MODEL_NAME, contents=prompt)
        raw = response.text.strip() if response.text else ""
    except Exception as exc:
        logger.warning("[fact_extractor] Gemini call failed: %s: %s", type(exc).__name__, exc)
        return None

    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        logger.warning("[fact_extractor] Could not parse Gemini response as JSON: %r", raw[:200])
        return None

    if not isinstance(parsed, dict):
        # "multiple Buildings returned" / malformed shape — we only ever ask
        # for a single object describing a single candidate Building.
        logger.warning("[fact_extractor] Gemini returned a non-object JSON shape: %r", raw[:200])
        return None

    return parsed


def _validate_extraction(parsed, candidate_row, source_text):
    """Strict validation of one Gemini extraction result. Returns a clean
    dict {fact, category, confidence, evidence_quote} or None if the output
    should be rejected (including the ordinary "no candidate" case).

    Rejects if: entity changes (subject_name doesn't match the candidate
    given), category invalid, confidence outside [0,1] or below the
    conservative-bias floor, fact empty, evidence is not an exact substring
    of source_text.
    """
    if not parsed.get("has_furniture_candidate"):
        return None

    subject_name = (parsed.get("subject_name") or "").strip()
    display_name = (candidate_row["display_name"] or "").strip()
    if not display_name or subject_name.lower() != display_name.lower():
        logger.info(
            "[fact_extractor] rejected: entity changed (expected %r, got %r)",
            display_name, subject_name,
        )
        return None

    category = parsed.get("category")
    if category not in FURNITURE_CATEGORIES:
        logger.info("[fact_extractor] rejected: invalid category %r", category)
        return None

    fact = (parsed.get("fact") or "").strip()
    if not fact:
        logger.info("[fact_extractor] rejected: empty fact")
        return None

    try:
        confidence = float(parsed.get("confidence"))
    except (TypeError, ValueError):
        logger.info("[fact_extractor] rejected: non-numeric confidence %r", parsed.get("confidence"))
        return None
    if not (0.0 <= confidence <= 1.0):
        logger.info("[fact_extractor] rejected: confidence out of range %r", confidence)
        return None
    if confidence < MIN_CONFIDENCE:
        logger.info("[fact_extractor] rejected: confidence %.2f below conservative floor", confidence)
        return None

    evidence_quote = parsed.get("evidence_quote") or ""
    if not evidence_quote or evidence_quote not in source_text:
        logger.info("[fact_extractor] rejected: evidence_quote is not an exact substring of source text")
        return None

    return {
        "fact": fact,
        "category": category,
        "confidence": confidence,
        "evidence_quote": evidence_quote,
    }


def _evaluate_candidate(gemini_client, candidate_row, source_text):
    """One Gemini evaluation for one (Building, source text) pair. Returns a
    validated dict or None (no candidate / rejected)."""
    prompt = _EXTRACTION_PROMPT_TEMPLATE.format(
        display_name=candidate_row["display_name"] or f"Building {candidate_row['id']}",
        source_text=source_text,
    )
    parsed = _call_gemini_json(gemini_client, prompt)
    if parsed is None:
        return None
    return _validate_extraction(parsed, candidate_row, source_text)


# ---------------------------------------------------------------------------
# Duplicate suppression — hybrid: fingerprint, then narrow Gemini comparison
# ---------------------------------------------------------------------------

def _normalize_for_fingerprint(fact_text):
    normalized = fact_text.strip().lower()
    normalized = re.sub(r"[^\w\s]", "", normalized)
    return re.sub(r"\s+", " ", normalized).strip()


def fingerprint_fact(fact_text):
    normalized = _normalize_for_fingerprint(fact_text)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _exact_duplicate_exists(memory_conn, entity_id, fingerprint):
    return (
        hearth_furniture_proposals.find_pending_by_fingerprint(memory_conn, entity_id, fingerprint) is not None
        or hearth_furniture_proposals.find_dismissed_by_fingerprint(memory_conn, entity_id, fingerprint) is not None
        or hearth_furniture_proposals.find_approved_by_fingerprint(memory_conn, entity_id, fingerprint) is not None
    )


_DEDUP_PROMPT_TEMPLATE = """You are checking whether a new candidate fact is semantically equivalent to any fact already on record for the same person. Only say yes if a reasonable person would consider them the same underlying fact stated differently — not merely related or thematically similar.

New candidate fact:
{new_fact}

Facts already on record:
{existing_facts}

Return ONLY valid JSON in this exact shape:
{{"is_duplicate": true}}

Do not include markdown, explanations, or additional fields."""


def _is_semantic_duplicate(gemini_client, new_fact, existing_facts):
    """Narrow Gemini semantic-equivalence check — step 2 of duplicate
    suppression. Only called when step 1 (exact fingerprint match) found
    nothing and there is at least one existing fact/pending proposal to
    compare against. Conservative: any failure to get a clean answer is
    treated as "not a duplicate" (suppression only happens on a clear,
    parseable yes), consistent with the extractor's bias toward false
    negatives over false positives — better to occasionally leave a genuine
    duplicate for a human to dismiss than to silently drop a real fact.
    """
    if not existing_facts:
        return False

    prompt = _DEDUP_PROMPT_TEMPLATE.format(
        new_fact=new_fact,
        existing_facts="\n".join(f"- {f}" for f in existing_facts[:MAX_EXISTING_FACTS_FOR_DEDUP_CHECK]),
    )
    parsed = _call_gemini_json(gemini_client, prompt)
    if parsed is None or not isinstance(parsed, dict):
        return False
    return bool(parsed.get("is_duplicate"))


def _is_duplicate(memory_conn, gemini_client, entity_id, fact_text, fingerprint):
    if _exact_duplicate_exists(memory_conn, entity_id, fingerprint):
        return True

    existing_facts = [row["fact_text"] for row in hearth_furniture.get_active_furniture(memory_conn, entity_id)]
    existing_facts += [
        row["proposed_fact"]
        for row in hearth_furniture_proposals.get_pending_proposals_for_entity(memory_conn, entity_id)
    ]
    if not existing_facts:
        return False

    return _is_semantic_duplicate(gemini_client, fact_text, existing_facts)


# ---------------------------------------------------------------------------
# Batch runner
# ---------------------------------------------------------------------------

def _empty_source_summary():
    return {
        "records_seen": 0,
        "records_already_ledgered": 0,
        "records_processed": 0,
        "candidates_evaluated": 0,
        "proposals_created": 0,
        "would_propose": 0,
        "suppressed_duplicate": 0,
        "no_furniture_candidate": 0,
        "record_errors": 0,
        "candidate_errors": 0,
    }


def _build_gemini_client():
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return None
    try:
        from google import genai
    except ImportError:
        logger.warning("[fact_extractor] google-genai not installed — cannot extract this run.")
        return None
    try:
        return genai.Client(api_key=api_key)
    except Exception as exc:
        logger.warning("[fact_extractor] genai.Client() construction failed: %s: %s", type(exc).__name__, exc)
        return None


def _process_record(memory_conn, gemini_client, source_type, source_record_id, text,
                     subject_user_id, author_user_id, summary, dry_run, log):
    content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()

    if hearth_furniture_proposals.is_source_processed(
        memory_conn, source_type, source_record_id, content_hash, EXTRACTOR_VERSION
    ):
        summary["records_already_ledgered"] += 1
        return

    candidates = _generate_candidates(memory_conn, text, subject_user_id)

    for candidate in candidates:
        summary["candidates_evaluated"] += 1
        try:
            result = _evaluate_candidate(gemini_client, candidate, text)
            if result is None:
                summary["no_furniture_candidate"] += 1
                continue

            fingerprint = fingerprint_fact(result["fact"])
            if _is_duplicate(memory_conn, gemini_client, candidate["id"], result["fact"], fingerprint):
                summary["suppressed_duplicate"] += 1
                continue

            if dry_run:
                summary["would_propose"] += 1
                log(
                    f"[fact_extractor] WOULD PROPOSE: entity={candidate['display_name']!r} "
                    f"fact={result['fact']!r} category={result['category']} "
                    f"confidence={result['confidence']:.2f} source={source_type}#{source_record_id}"
                )
                continue

            hearth_furniture_proposals.create_furniture_proposal(
                memory_conn,
                entity_id=candidate["id"],
                proposed_fact=result["fact"],
                fact_type=result["category"],
                confidence=result["confidence"],
                evidence_quote=result["evidence_quote"],
                source_type=source_type,
                source_record_id=source_record_id,
                source_author_user_id=author_user_id,
                semantic_fingerprint=fingerprint,
                extractor_version=EXTRACTOR_VERSION,
            )
            summary["proposals_created"] += 1
        except Exception as exc:
            summary["candidate_errors"] += 1
            log(
                f"[fact_extractor] candidate error for entity={candidate['id']} "
                f"source={source_type}#{source_record_id}: {type(exc).__name__}: {exc}"
            )
            continue

    if not dry_run:
        hearth_furniture_proposals.mark_source_processed(
            memory_conn, source_type, source_record_id, content_hash, EXTRACTOR_VERSION
        )
    summary["records_processed"] += 1


def run_batch(batch_limit_per_source=DEFAULT_BATCH_LIMIT_PER_SOURCE, dry_run=False, log=print):
    """Run one extraction pass across every approved source. Bounded batches
    per source; one record's or candidate's failure never aborts the rest.
    Returns a summary dict keyed by source_type plus a "totals" entry.
    """
    summary = {"totals": _empty_source_summary()}

    gemini_client = _build_gemini_client()
    if gemini_client is None:
        log("[fact_extractor] No usable Gemini client — skipping this run (nothing extracted).")
        return summary

    pathway_conn = _get_pathway_connection()
    if pathway_conn is None:
        return summary

    memory_conn = hearth_memory.get_memory_connection()

    try:
        for config in SOURCE_CONFIGS:
            source_type = config["source_type"]
            source_summary = _empty_source_summary()
            summary[source_type] = source_summary

            rows = _fetch_generic_records(pathway_conn, config, GO_LIVE_AT, batch_limit_per_source)
            source_summary["records_seen"] = len(rows)

            for row in rows:
                try:
                    text = _build_text(row, config)
                    if not text:
                        continue
                    if _is_hearth_authored(source_type, row):
                        continue
                    subject_user_id = (
                        row[config["subject_user_column"]]
                        if config["subject_user_column"] and config["subject_user_column"] in row.keys()
                        else None
                    )
                    author_user_id = (
                        row[config["author_user_column"]]
                        if config["author_user_column"] and config["author_user_column"] in row.keys()
                        else None
                    )
                    _process_record(
                        memory_conn, gemini_client, source_type, row[config["id_column"]], text,
                        subject_user_id, author_user_id, source_summary, dry_run, log,
                    )
                except Exception as exc:
                    source_summary["record_errors"] += 1
                    log(f"[fact_extractor] record error in {source_type}#{row[config['id_column']]}: "
                        f"{type(exc).__name__}: {exc}")
                    continue

        # checkin_answers: special-cased fetch (see _fetch_checkin_answers).
        source_summary = _empty_source_summary()
        summary[CHECKIN_ANSWERS_SOURCE_TYPE] = source_summary
        checkin_rows = _fetch_checkin_answers(pathway_conn, GO_LIVE_AT, batch_limit_per_source)
        source_summary["records_seen"] = len(checkin_rows)
        for row in checkin_rows:
            try:
                parts = []
                if row["question_text"]:
                    parts.append(f"Question: {row['question_text']}")
                parts.append(f"Answer: {row['creator_response']}")
                text = "\n".join(parts)
                if not text.strip():
                    continue
                subject_user_id = row["subject_user_id"]
                _process_record(
                    memory_conn, gemini_client, CHECKIN_ANSWERS_SOURCE_TYPE, row["id"], text,
                    subject_user_id, subject_user_id, source_summary, dry_run, log,
                )
            except Exception as exc:
                source_summary["record_errors"] += 1
                log(f"[fact_extractor] record error in checkin_answers#{row['id']}: "
                    f"{type(exc).__name__}: {exc}")
                continue

        for key in _empty_source_summary():
            summary["totals"][key] = sum(
                s[key] for name, s in summary.items() if name != "totals"
            )

    finally:
        pathway_conn.close()
        memory_conn.close()

    log(f"[fact_extractor] run complete: {summary['totals']}")
    return summary


if __name__ == "__main__":
    import sys
    dry = "--dry-run" in sys.argv
    run_batch(dry_run=dry)
