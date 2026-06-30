"""
Hearth Pulse V2 — Interpretation V1.

Reads unprocessed hearth_events from the Pathway DB, classifies each one,
and writes the classification back. Pathway only ever emits trace-level
events into hearth_events; Hearth owns all interpretation of importance
and concern level.

Connects directly to the Pathway DB (DATABASE_URL) for both the read-write
update connection and the read-only lookup connection. Only opens
hearth_memory.db when a checkin_submitted event needs an episode lookup.

Since Session 6, Pulse also reads a small worldview snapshot (see
collect_pulse_worldview_context) once per run, before classification begins.
This is read-only and purely informational for now — classify_event() does
not receive or consult it, so classification output is unchanged. The
intent is future logic that understands what "normal" looks like; this
session only makes that data available to Pulse.
"""

import os
import sqlite3
from datetime import datetime

from dotenv import load_dotenv

import hearth_memory

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")

_STAFF_ROLES = ("manager", "coach", "ceo", "it", "navigator")

# Feature flag for reading worldview into Pulse — Session 6. Defaults on; set
# the HEARTH_WORLDVIEW_PULSE_ENABLED env var to "0" to make Pulse behave
# exactly as it did before Session 6 (no worldview read at all).
HEARTH_WORLDVIEW_PULSE_ENABLED = (
    os.environ.get("HEARTH_WORLDVIEW_PULSE_ENABLED", "1") == "1"
)

# Small per-category caps for the worldview snapshot Pulse carries per run.
_PULSE_WORLDVIEW_BELIEF_LIMIT = 10
_PULSE_WORLDVIEW_RELATIONSHIP_LIMIT = 10
_PULSE_WORLDVIEW_UNCERTAINTY_LIMIT = 10
_PULSE_WORLDVIEW_CHANGE_LIMIT = 10
_PULSE_WORLDVIEW_LESSON_LIMIT = 5


# ---------------------------------------------------------------------------
# Connections
# ---------------------------------------------------------------------------

def _resolve_db_path(database_url):
    if database_url.startswith("sqlite:///"):
        return database_url[len("sqlite:///"):]
    return database_url


def get_pathway_connection(db_path):
    """Read-write connection to the Pathway DB, used only for hearth_events updates."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def get_pathway_readonly_connection(db_path):
    """Read-only connection to the Pathway DB, used for classification lookups."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# Classification helpers
# ---------------------------------------------------------------------------

def _days_between(earlier_iso, later_iso):
    earlier = datetime.fromisoformat(earlier_iso)
    later = datetime.fromisoformat(later_iso)
    return (later - earlier).days


def _classify_user_signed_in(lookup_conn, event):
    prior = lookup_conn.execute(
        "SELECT occurred_at FROM hearth_events"
        " WHERE actor_user_id = ? AND event_type = 'user_signed_in'"
        " AND occurred_at < ? ORDER BY occurred_at DESC LIMIT 1;",
        (event["actor_user_id"], event["occurred_at"]),
    ).fetchone()

    if not prior:
        return "trace", 0.1, "First recorded sign-in event for this user."

    gap = _days_between(prior["occurred_at"], event["occurred_at"])
    if gap < 30:
        return "trace", 0.1, "Routine sign-in; no significance threshold crossed."
    if gap < 60:
        return "signal", 0.45, f"User signed in after {gap} days without recorded hub activity."
    return (
        "observation",
        0.75,
        f"User signed in after {gap} days without recorded hub activity. Extended absence.",
    )


def _classify_checkin_submitted(lookup_conn, memory_conn, event):
    prior_count = lookup_conn.execute(
        "SELECT COUNT(*) FROM hearth_events"
        " WHERE actor_user_id = ? AND event_type = 'checkin_submitted'"
        " AND occurred_at < ?;",
        (event["actor_user_id"], event["occurred_at"]),
    ).fetchone()[0]

    if prior_count == 0:
        level, score, reason = (
            "observation",
            0.75,
            "First recorded check-in submission from this creator.",
        )
    else:
        level, score, reason = (
            "signal",
            0.5,
            "Check-in submitted. May resolve pending coaching feedback.",
        )

    if memory_conn is not None:
        try:
            open_episode = memory_conn.execute(
                "SELECT e.id FROM hearth_episodes e"
                " JOIN hearth_entities en ON en.id = e.entity_id"
                " WHERE en.user_id = ? AND e.episode_type = 'checkin_feedback_waiting'"
                " AND e.resolved = 0 LIMIT 1;",
                (event["actor_user_id"],),
            ).fetchone()
            if open_episode:
                reason += " Open coaching feedback episode exists — potential resolution candidate."
        except Exception:
            pass  # episode lookup unclear — skip it, keep base classification

    return level, score, reason


def _classify_training_viewed(lookup_conn, event):
    prior = lookup_conn.execute(
        "SELECT occurred_at FROM hearth_events"
        " WHERE actor_user_id = ? AND event_type = 'training_viewed'"
        " AND occurred_at < ? ORDER BY occurred_at DESC LIMIT 1;",
        (event["actor_user_id"], event["occurred_at"]),
    ).fetchone()

    if not prior:
        return "signal", 0.40, "First recorded training view for this creator."

    gap = _days_between(prior["occurred_at"], event["occurred_at"])
    if gap >= 30:
        return "signal", 0.35, f"Creator returned to training after {gap} days."

    return "trace", 0.10, "Routine training view."


def _classify_onboarding_step_completed(lookup_conn, event):
    target_id = event["target_user_id"]

    prior = lookup_conn.execute(
        "SELECT 1 FROM hearth_events"
        " WHERE target_user_id = ? AND event_type = 'onboarding_step_completed'"
        " AND occurred_at < ? LIMIT 1;",
        (target_id, event["occurred_at"]),
    ).fetchone()

    if prior is None:
        return "signal", 0.50, "First onboarding step completed for this creator."

    try:
        record = lookup_conn.execute(
            "SELECT contract_sent, contract_accepted, added_tt_chat,"
            " added_discord, welcome_form_sent, welcome_form_returned"
            " FROM onboarding_records WHERE id = ?;",
            (event["reference_id"],),
        ).fetchone()
        if record is not None and all(record[k] == "completed" for k in record.keys()):
            return "observation", 0.80, "Onboarding completed for this creator."
    except sqlite3.Error:
        pass  # onboarding_records lookup unclear — skip it, keep base classification

    return "signal", 0.45, "Onboarding step completed."


def _classify_battle_requested(lookup_conn, event):
    prior = lookup_conn.execute(
        "SELECT 1 FROM hearth_events"
        " WHERE actor_user_id = ? AND event_type = 'battle_requested'"
        " AND occurred_at < ? LIMIT 1;",
        (event["actor_user_id"], event["occurred_at"]),
    ).fetchone()

    if prior is None:
        return "signal", 0.50, "First battle request submitted by this creator."

    return "trace", 0.20, "Battle request submitted."


def _classify_event_signup_created(lookup_conn, event):
    prior = lookup_conn.execute(
        "SELECT 1 FROM hearth_events"
        " WHERE actor_user_id = ? AND event_type = 'event_signup_created'"
        " AND occurred_at < ? LIMIT 1;",
        (event["actor_user_id"], event["occurred_at"]),
    ).fetchone()

    if prior is None:
        return "signal", 0.45, "First event signup recorded for this creator."

    return "trace", 0.15, "Event signup recorded."


def _classify_community_message_created(lookup_conn, event):
    actor_id = event["actor_user_id"]

    prior = lookup_conn.execute(
        "SELECT occurred_at FROM hearth_events"
        " WHERE actor_user_id = ? AND event_type = 'community_message_created'"
        " AND occurred_at < ? ORDER BY occurred_at DESC LIMIT 1;",
        (actor_id, event["occurred_at"]),
    ).fetchone()

    board = None
    reference_type = event["reference_type"] or ""
    if reference_type.startswith("post:"):
        board = reference_type.split(":", 1)[1]
    board_suffix = f" ({board} board)" if board else ""

    if prior is None:
        return "signal", 0.45, "First community post from this creator."

    gap = _days_between(prior["occurred_at"], event["occurred_at"])
    if gap >= 30:
        return (
            "signal",
            0.40,
            f"Creator posted to community after {gap} days of silence.",
        )

    return "trace", 0.10, f"Community post recorded{board_suffix}."


def classify_event(lookup_conn, memory_conn, event):
    """Return (experience_level, importance_score, importance_reason) for one event."""
    event_type = event["event_type"]
    if event_type == "user_signed_in":
        return _classify_user_signed_in(lookup_conn, event)
    if event_type == "checkin_submitted":
        return _classify_checkin_submitted(lookup_conn, memory_conn, event)
    if event_type == "training_viewed":
        return _classify_training_viewed(lookup_conn, event)
    if event_type == "onboarding_step_completed":
        return _classify_onboarding_step_completed(lookup_conn, event)
    if event_type == "battle_requested":
        return _classify_battle_requested(lookup_conn, event)
    if event_type == "event_signup_created":
        return _classify_event_signup_created(lookup_conn, event)
    if event_type == "community_message_created":
        return _classify_community_message_created(lookup_conn, event)
    # Hearth intentionally excludes private creator-to-creator conversations.
    # Organizational intelligence should observe organizational activity, not
    # private communication. This is a permanent architectural boundary.
    if event_type == "message_sent":
        return "trace", 0.0, "Private messages are excluded from Hearth's awareness."
    return "trace", 0.1, f"No classification rule for event_type '{event_type}'."


# ---------------------------------------------------------------------------
# Worldview context (Session 6 — read-only, not yet used for classification)
# ---------------------------------------------------------------------------

def collect_pulse_worldview_context(conn=None):
    """Read a small worldview snapshot for Pulse to carry alongside a run.

    Read-only and informational only: classify_event() never receives or
    consults this context, so it has no effect on classification this
    session. Returns {} without raising if disabled via
    HEARTH_WORLDVIEW_PULSE_ENABLED, if hearth_worldview is unavailable, if
    the worldview tables are missing or empty, or if any other error occurs
    while reading — Pulse must always be able to continue classifying events
    normally regardless of worldview's state. Never writes to worldview.

    Pass an existing hearth_memory.db connection via `conn` to reuse it;
    otherwise a short-lived connection is opened and closed internally.
    """
    if not HEARTH_WORLDVIEW_PULSE_ENABLED:
        return {}

    owns_conn = conn is None
    try:
        import hearth_worldview as _wv

        if owns_conn:
            conn = _wv.get_worldview_connection()

        snapshot = _wv.get_worldview_snapshot(
            conn,
            belief_limit=_PULSE_WORLDVIEW_BELIEF_LIMIT,
            relationship_limit=_PULSE_WORLDVIEW_RELATIONSHIP_LIMIT,
            uncertainty_limit=_PULSE_WORLDVIEW_UNCERTAINTY_LIMIT,
            change_limit=_PULSE_WORLDVIEW_CHANGE_LIMIT,
            lesson_limit=_PULSE_WORLDVIEW_LESSON_LIMIT,
        )
        total = sum(len(rows) for rows in snapshot.values())
        if total:
            print(
                "[hearth_pulse] worldview context read (informational only):"
                f" beliefs={len(snapshot['active_beliefs'])}"
                f" relationships={len(snapshot['active_relationships'])}"
                f" uncertainties={len(snapshot['open_uncertainties'])}"
                f" changes={len(snapshot['watched_changes'])}"
                f" lessons={len(snapshot['recent_lessons'])}"
            )
        return snapshot
    except Exception as exc:
        print(f"[hearth_pulse] worldview context unavailable this run, continuing without it: {exc}")
        return {}
    finally:
        if owns_conn and conn is not None:
            try:
                conn.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Processor
# ---------------------------------------------------------------------------

def _process_unprocessed_hearth_events_detailed(limit=100, include_worldview_context=False):
    """
    Classify up to `limit` unprocessed hearth_events rows and write the
    classification back to the Pathway DB. Returns a list of
    (id, event_type, experience_level, importance_score, importance_reason)
    tuples for the events processed.

    Opens its own read-write connection to the Pathway DB for updates and a
    separate read-only connection for classification lookups. Only opens
    hearth_memory.db when a checkin_submitted event needs an episode lookup.

    include_worldview_context: if True, returns (results, worldview_context)
    instead of just results. Defaults to False so existing callers keep the
    original return shape. worldview_context (see collect_pulse_worldview_context)
    is read once per run, before any event is classified, but classify_event()
    never receives or uses it — Session 6 only makes it available, it does
    not change classification output either way.
    """
    db_path = _resolve_db_path(DATABASE_URL)
    write_conn = get_pathway_connection(db_path)
    lookup_conn = get_pathway_readonly_connection(db_path)
    memory_conn = None

    worldview_context = collect_pulse_worldview_context()

    results = []
    try:
        rows = write_conn.execute(
            "SELECT * FROM hearth_events WHERE processed = 0"
            " ORDER BY occurred_at ASC LIMIT ?;",
            (limit,),
        ).fetchall()

        needs_memory = any(row["event_type"] == "checkin_submitted" for row in rows)
        if needs_memory:
            try:
                memory_conn = hearth_memory.get_memory_connection()
            except Exception as exc:
                print(f"[hearth_pulse] WARNING: could not open hearth_memory.db: {exc}")
                memory_conn = None

        for row in rows:
            try:
                level, score, reason = classify_event(lookup_conn, memory_conn, row)
            except Exception as exc:
                level, score, reason = "trace", 0.0, f"Processing error: {str(exc)[:100]}"
                print(f"[hearth_pulse] ERROR processing event id={row['id']}: {exc}")

            write_conn.execute(
                "UPDATE hearth_events"
                " SET processed = 1, experience_level = ?, importance_score = ?,"
                "     importance_reason = ? WHERE id = ?;",
                (level, score, reason, row["id"]),
            )
            results.append((row["id"], row["event_type"], level, score, reason))

        write_conn.commit()
        if include_worldview_context:
            return results, worldview_context
        return results
    finally:
        lookup_conn.close()
        write_conn.close()
        if memory_conn is not None:
            memory_conn.close()


def process_unprocessed_hearth_events(limit=100):
    """Classify and update up to `limit` unprocessed hearth_events rows.

    Returns the count of events processed.
    """
    return len(_process_unprocessed_hearth_events_detailed(limit=limit))


if __name__ == "__main__":
    if not DATABASE_URL:
        raise SystemExit("ERROR: DATABASE_URL is missing. Add it to your .env file.")

    print("Hearth Pulse — connecting to Pathway Portal...")
    results, worldview_context = _process_unprocessed_hearth_events_detailed(
        include_worldview_context=True
    )
    print(f"Processed {len(results)} events")
    for event_id, event_type, level, score, reason in results:
        print(f"  id={event_id} type={event_type} level={level} score={score} reason={reason}")
    if worldview_context:
        print(
            "Worldview context attached this run (read-only, not used for classification):"
            f" beliefs={len(worldview_context.get('active_beliefs', []))}"
            f" relationships={len(worldview_context.get('active_relationships', []))}"
            f" uncertainties={len(worldview_context.get('open_uncertainties', []))}"
            f" changes={len(worldview_context.get('watched_changes', []))}"
            f" lessons={len(worldview_context.get('recent_lessons', []))}"
        )
