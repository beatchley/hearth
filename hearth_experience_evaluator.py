"""
Hearth Experience Evaluator — V1, observation mode only.

Bridges Pulse signals and Hearth episodes. Pulse notices activity and
classifies hearth_events as trace/signal/observation. This module asks the
next question: "Does this signal meaningfully change understanding relative
to worldview?" If yes, it may eventually be promoted into a Hearth episode.

V1 never creates episodes and never writes to worldview. It only reads
Pulse-classified events and Hearth's worldview, classifies each event into
a candidate type, and reports what it would have done.

Architecture:
    Pulse (hearth_events) -> Experience Evaluator -> episode candidate
        -> [future] episode -> Soul -> worldview

The rule is not "training_viewed becomes an episode." The rule is "a signal
becomes an episode when it changes understanding relative to worldview."
Worldview is the filter that keeps most signals as no_action.

Generic by design — entity resolution and worldview lookups go through
hearth_memory/hearth_worldview, so this should extend to non-Pathway sources
(e.g. Goose) without rework, as long as they can resolve to a hearth_entities
row and write events into a compatible table.
"""

import os
import sqlite3

from dotenv import load_dotenv

import hearth_memory
import hearth_worldview
from hearth_identity import get_user_identity, is_staff_user

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")

_LOG_PREFIX = "[HEARTH EXPERIENCE]"

# Feature flags — see module docstring. Promotion stays off by default;
# enabling it in V1 only prints/returns that promotion is not implemented.
HEARTH_EXPERIENCE_EVALUATOR_ENABLED = (
    os.environ.get("HEARTH_EXPERIENCE_EVALUATOR_ENABLED", "1") == "1"
)
HEARTH_EXPERIENCE_EVALUATOR_PROMOTE = (
    os.environ.get("HEARTH_EXPERIENCE_EVALUATOR_PROMOTE", "0") == "1"
)

_DEFAULT_LIMIT = 50

# Only these Pulse experience levels are evaluated. Trace events are noise
# by Pulse's own classification and are never read here.
_EVALUABLE_EXPERIENCE_LEVELS = ("signal", "observation")

# Event types that represent a creator doing something positive/re-engaging.
_POSITIVE_ACTIVITY_EVENT_TYPES = frozenset({
    "training_viewed",
    "user_signed_in",
    "message_sent",
    "community_message_created",
    "battle_requested",
    "event_signup_created",
    "checkin_submitted",
    "onboarding_step_completed",
})

# Worldview text containing any of these suggests the entity is currently
# being watched for quiet/stuck/disengaged behavior.
_QUIET_STUCK_KEYWORDS = (
    "quiet", "stuck", "inactive", "onboarding", "no engagement",
    "disengag", "creator_quiet", "new_creator_stuck",
)

# Worldview text containing any of these suggests Hearth/staff is waiting on
# the creator for a response (the "concern" family of watchers).
_WAITING_KEYWORDS = (
    "waiting", "support_request_waiting", "checkin_feedback_waiting",
    "training_comment_waiting",
)

# Conservative mapping: an event of this type only counts as resolving a
# waiting-type worldview entry if the entry's text also matches one of
# these more specific keywords. Anything else stays a momentum/concern call.
_RESOLUTION_KEYWORDS_BY_EVENT_TYPE = {
    "checkin_submitted": ("checkin", "feedback"),
    "message_sent": ("support", "training comment", "message"),
}

# subject_types under hearth_worldview that are scoped to a single entity.
# "episode_type" is deliberately excluded — that subject_type tracks
# cross-entity volume spikes, not this entity's situation.
_ENTITY_SUBJECT_TYPE = "entity"
_ENTITY_EPISODE_SUBJECT_TYPE = "entity_episode"
_CREATOR_QUIET_SUBJECT_TYPE = "creator_quiet_entity"

_WORLDVIEW_SCAN_LIMIT = 500  # safety cap when scanning entity_episode rows


# ---------------------------------------------------------------------------
# Connections
# ---------------------------------------------------------------------------

def _resolve_db_path(database_url):
    if database_url.startswith("sqlite:///"):
        return database_url[len("sqlite:///"):]
    return database_url


def get_pathway_readonly_connection(db_path):
    """Read-only connection to the Pathway DB — this module never writes to it."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# Worldview gathering
# ---------------------------------------------------------------------------

def _row_text(row, *fields):
    parts = [str(row[f]) for f in fields if row[f]]
    return " ".join(parts).lower()


def _gather_entity_worldview(memory_conn, entity_id):
    """Return a list of {"subject_type", "text"} dicts for worldview entries
    scoped to this entity: active beliefs, open uncertainties, and watched
    changes. Read-only — never writes.
    """
    subject_id = str(entity_id)
    entries = []

    for row in hearth_worldview.get_active_beliefs(
        memory_conn, subject_type=_ENTITY_SUBJECT_TYPE, subject_id=subject_id
    ):
        entries.append({
            "subject_type": row["subject_type"],
            "subject_id": row["subject_id"],
            "text": _row_text(row, "belief_type", "belief_text"),
        })

    for row in hearth_worldview.get_open_uncertainties(
        memory_conn, subject_type=_ENTITY_SUBJECT_TYPE, subject_id=subject_id
    ):
        entries.append({
            "subject_type": row["subject_type"],
            "subject_id": row["subject_id"],
            "text": _row_text(row, "uncertainty_text", "why_it_matters", "possible_question"),
        })

    # entity_episode uncertainties are keyed "{episode_type}:{entity_id}" — no
    # direct subject_id filter for the suffix, so scan a capped window and
    # match in Python. Worldview tables are small in V1; cap keeps this safe.
    suffix = f":{entity_id}"
    for row in hearth_worldview.get_open_uncertainties(
        memory_conn, subject_type=_ENTITY_EPISODE_SUBJECT_TYPE, limit=_WORLDVIEW_SCAN_LIMIT
    ):
        if row["subject_id"] and row["subject_id"].endswith(suffix):
            entries.append({
                "subject_type": row["subject_type"],
                "subject_id": row["subject_id"],
                "text": _row_text(row, "subject_id", "uncertainty_text", "why_it_matters"),
            })

    for row in hearth_worldview.get_watched_changes(
        memory_conn, subject_type=_CREATOR_QUIET_SUBJECT_TYPE, subject_id=subject_id
    ):
        entries.append({
            "subject_type": row["subject_type"],
            "subject_id": row["subject_id"],
            "text": _row_text(row, "subject_type", "change_text", "previous_state", "current_state"),
        })

    for row in hearth_worldview.get_watched_changes(
        memory_conn, subject_type=_ENTITY_SUBJECT_TYPE, subject_id=subject_id
    ):
        entries.append({
            "subject_type": row["subject_type"],
            "subject_id": row["subject_id"],
            "text": _row_text(row, "change_text", "previous_state", "current_state"),
        })

    return entries


def _matches_any(text, keywords):
    return any(keyword in text for keyword in keywords)


# ---------------------------------------------------------------------------
# Per-event evaluation
# ---------------------------------------------------------------------------

def _result(event, candidate_type, entity_id=None, reason=""):
    return {
        "event_id": event["id"],
        "event_type": event["event_type"],
        "candidate_type": candidate_type,
        "entity_id": str(entity_id) if entity_id is not None else None,
        "reason": reason,
    }


def _evaluate_event(event, memory_conn):
    """Classify one Pulse-classified event against worldview. Read-only.

    Returns a result dict with candidate_type one of:
    "momentum", "resolution", "concern", "no_action".
    """
    actor_id = event["actor_user_id"] if event["actor_user_id"] is not None else event["target_user_id"]
    if actor_id is None:
        return _result(event, "no_action", reason="Event has no actor or target user to map to an entity.")

    entity_row = hearth_memory.get_entity_by_user_id(memory_conn, actor_id)
    if entity_row is None:
        return _result(
            event, "no_action",
            reason=f"No Hearth entity exists yet for user_id={actor_id}.",
        )
    entity_id = entity_row["id"]

    worldview_entries = _gather_entity_worldview(memory_conn, entity_id)
    if not worldview_entries:
        return _result(
            event, "no_action", entity_id,
            reason="No relevant worldview entry exists for this creator.",
        )

    quiet_hits = [e for e in worldview_entries if _matches_any(e["text"], _QUIET_STUCK_KEYWORDS)]
    waiting_hits = [e for e in worldview_entries if _matches_any(e["text"], _WAITING_KEYWORDS)]

    event_type = event["event_type"]
    resolution_keywords = _RESOLUTION_KEYWORDS_BY_EVENT_TYPE.get(event_type, ())

    if waiting_hits and resolution_keywords:
        resolving_hit = next(
            (e for e in waiting_hits if _matches_any(e["text"], resolution_keywords)), None
        )
        if resolving_hit is not None:
            reason = (
                f"Creator had an open worldview entry ({resolving_hit['subject_type']}:"
                f" {resolving_hit['text'][:80]}) and then took an action ('{event_type}')"
                " that appears to directly answer it."
            )
            return _result(event, "resolution", entity_id, reason)

    if quiet_hits and event_type in _POSITIVE_ACTIVITY_EVENT_TYPES:
        hit = quiet_hits[0]
        reason = (
            f"Creator had an open quiet/stuck-related worldview entry"
            f" ({hit['subject_type']}: {hit['text'][:80]}) and then showed"
            f" positive activity ('{event_type}')."
        )
        return _result(event, "momentum", entity_id, reason)

    if waiting_hits:
        hit = waiting_hits[0]
        reason = (
            f"Creator already has an open waiting/concern-related worldview entry"
            f" ({hit['subject_type']}: {hit['text'][:80]}); this signal ('{event_type}')"
            " touches the same topic without clearly resolving it."
        )
        return _result(event, "concern", entity_id, reason)

    return _result(
        event, "no_action", entity_id,
        reason=f"Entity {entity_id} has worldview entries, but none relate to this signal type ('{event_type}').",
    )


# ---------------------------------------------------------------------------
# Processor
# ---------------------------------------------------------------------------

def _get_recent_signal_events(pathway_conn, limit):
    placeholders = ", ".join("?" for _ in _EVALUABLE_EXPERIENCE_LEVELS)
    return pathway_conn.execute(
        "SELECT * FROM hearth_events"
        f" WHERE processed = 1 AND experience_level IN ({placeholders})"
        " ORDER BY occurred_at DESC LIMIT ?;",
        (*_EVALUABLE_EXPERIENCE_LEVELS, limit),
    ).fetchall()


def evaluate_recent_signals(limit=_DEFAULT_LIMIT):
    """Evaluate up to `limit` recent Pulse-classified signal/observation events.

    Observation mode only: never creates a hearth_episode, never writes to
    worldview, never updates hearth_events. Returns:

        {
            "evaluated": int,
            "candidates": [
                {"event_id", "event_type", "candidate_type", "entity_id", "reason"},
                ...
            ],
            "no_action": int,
        }

    candidates only includes momentum/resolution/concern results — no_action
    results are counted but not included in detail, to keep logs/output
    readable.
    """
    if not HEARTH_EXPERIENCE_EVALUATOR_ENABLED:
        print(f"{_LOG_PREFIX} disabled via HEARTH_EXPERIENCE_EVALUATOR_ENABLED — skipping run.")
        return {"evaluated": 0, "candidates": [], "no_action": 0}

    if not DATABASE_URL:
        print(f"{_LOG_PREFIX} DATABASE_URL is not set — cannot read hearth_events. Skipping run.")
        return {"evaluated": 0, "candidates": [], "no_action": 0}

    db_path = _resolve_db_path(DATABASE_URL)
    pathway_conn = get_pathway_readonly_connection(db_path)
    memory_conn = hearth_memory.get_memory_connection()

    candidates = []
    no_action_count = 0

    try:
        events = _get_recent_signal_events(pathway_conn, limit)

        for event in events:
            result = _evaluate_event(event, memory_conn)
            if result["candidate_type"] == "no_action":
                no_action_count += 1
                print(
                    f"{_LOG_PREFIX} no_action — event_id={result['event_id']}"
                    f" type={result['event_type']}: {result['reason']}"
                )
            else:
                candidates.append(result)
                print(
                    f"{_LOG_PREFIX} would promote signal {result['event_id']} as"
                    f" {result['candidate_type']} — {result['reason']}"
                )

        if HEARTH_EXPERIENCE_EVALUATOR_PROMOTE and candidates:
            print(
                f"{_LOG_PREFIX} HEARTH_EXPERIENCE_EVALUATOR_PROMOTE is enabled, but"
                f" promotion is not implemented in V1 — no episodes created for"
                f" {len(candidates)} candidate(s)."
            )

        print(
            f"{_LOG_PREFIX} evaluated {len(events)} event(s):"
            f" {len(candidates)} candidate(s), {no_action_count} no_action."
        )

        return {
            "evaluated": len(events),
            "candidates": candidates,
            "no_action": no_action_count,
        }
    finally:
        pathway_conn.close()
        memory_conn.close()


def run_experience_evaluator(limit=_DEFAULT_LIMIT):
    """Callable entry point for the scheduler (and manual runs).

    See evaluate_recent_signals for behavior and return shape. This is the
    function to call from a scheduler job — see module docstring / session
    notes for exactly where it should be wired alongside Pulse.
    """
    return evaluate_recent_signals(limit=limit)


if __name__ == "__main__":
    if not DATABASE_URL:
        raise SystemExit("ERROR: DATABASE_URL is missing. Add it to your .env file.")

    print(f"{_LOG_PREFIX} Hearth Experience Evaluator — observation mode only.")
    print(
        f"{_LOG_PREFIX} flags: ENABLED={HEARTH_EXPERIENCE_EVALUATOR_ENABLED}"
        f" PROMOTE={HEARTH_EXPERIENCE_EVALUATOR_PROMOTE}"
    )
    output = run_experience_evaluator()
    print(f"{_LOG_PREFIX} done: {output['evaluated']} evaluated,"
          f" {len(output['candidates'])} candidate(s), {output['no_action']} no_action.")
