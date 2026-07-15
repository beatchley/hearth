"""
Hearth Memory — persistent memory layer for the Hearth AI teammate.

Stores observations about Pathway Portal users across runs so Hearth
can reference recurring issues in future briefings. Uses a separate
SQLite file (hearth_memory.db) and never writes to Pathway tables.
"""

import os
import sqlite3
from datetime import datetime, timedelta, timezone

_LOCAL_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hearth_memory.db")
MEMORY_DB_PATH = os.environ.get("HEARTH_DB_PATH", _LOCAL_DB_PATH)

# episode_type values written by hearth_experience_evaluator.py's promotion
# path, and the reference_key prefix it uses. Until evaluator promotion has
# been independently verified and manually re-enabled (see
# HEARTH_EXPERIENCE_EVALUATOR_PROMOTE), rows matching this shape must stay
# out of every consumer that treats hearth_episodes as trusted
# organizational evidence — Daily Brief, Soul, Worldview reflection,
# belief-confidence updates, manager-facing summaries, recurring-pattern
# calculations. This is the single shared definition other modules
# (hearth_traversal.py, hearth_manager_advice.py, and pathway-portal's
# hearth_reader.py) should import rather than re-deriving the same check.
EVALUATOR_PROMOTED_EPISODE_TYPES = frozenset({"resolution", "momentum", "concern"})
EVALUATOR_REFERENCE_KEY_PREFIX = "pulse_event_"


def is_evaluator_promoted(episode_type, reference_key):
    """True if a hearth_episodes row was written by the Experience Evaluator's
    promotion path (see hearth_experience_evaluator.py), based on the same
    (episode_type, reference_key-prefix) shape hearth_reader.py's
    _exclude_promoted_evaluator_sql() checks in raw SQL.
    """
    return (
        episode_type in EVALUATOR_PROMOTED_EPISODE_TYPES
        and bool(reference_key)
        and reference_key.startswith(EVALUATOR_REFERENCE_KEY_PREFIX)
    )


def _exclude_evaluator_promoted_sql(alias=""):
    """SQL fragment excluding evaluator-promoted rows, for raw-SQL callers
    that can't filter in Python (e.g. aggregate COUNT queries). Mirrors
    is_evaluator_promoted's logic exactly — keep the two in sync.
    """
    prefix = f"{alias}." if alias else ""
    placeholders = ", ".join("?" for _ in EVALUATOR_PROMOTED_EPISODE_TYPES)
    return (
        f" AND {prefix}episode_type NOT IN ({placeholders})"
        f" AND ({prefix}reference_key IS NULL OR {prefix}reference_key NOT LIKE"
        f" '{EVALUATOR_REFERENCE_KEY_PREFIX}%')"
    )


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

def get_memory_connection():
    """Open a read-write connection to Hearth's own memory database."""
    conn = sqlite3.connect(MEMORY_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def init_tables(conn):
    """Create Hearth's memory tables if they don't already exist."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS hearth_entities (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id          INTEGER UNIQUE,
            display_name     TEXT,
            summary          TEXT,
            patterns_noticed TEXT,
            concerns         TEXT,
            strengths        TEXT,
            importance_score REAL    DEFAULT 0.5,
            first_observed_at TEXT,
            last_observed_at TEXT,
            created_at       TEXT    NOT NULL
        );

        CREATE TABLE IF NOT EXISTS hearth_episodes (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            entity_id     INTEGER REFERENCES hearth_entities(id),
            episode_type  TEXT    NOT NULL,
            reference_key TEXT,
            description   TEXT    NOT NULL,
            severity      TEXT    NOT NULL DEFAULT 'medium',
            observed_at   TEXT    NOT NULL,
            resolved      INTEGER NOT NULL DEFAULT 0,
            resolved_at   TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_episodes_open
            ON hearth_episodes (episode_type, resolved);
    """)
    # Migrate existing databases — add columns introduced after initial release
    for migration in (
        "ALTER TABLE hearth_entities ADD COLUMN display_name TEXT;",
        "ALTER TABLE hearth_entities ADD COLUMN first_observed_at TEXT;",
        "ALTER TABLE hearth_episodes ADD COLUMN briefing_category TEXT;",
        "ALTER TABLE hearth_episodes ADD COLUMN last_briefed_at TEXT;",
        "ALTER TABLE hearth_entities ADD COLUMN entity_type TEXT DEFAULT 'person';",
        "ALTER TABLE hearth_entities ADD COLUMN source TEXT DEFAULT 'pathway_sync';",
        "ALTER TABLE hearth_entities ADD COLUMN canonical_key TEXT;",
        "ALTER TABLE hearth_entities ADD COLUMN aliases TEXT;",
        "ALTER TABLE hearth_episodes ADD COLUMN resolution_reason TEXT;",
    ):
        try:
            conn.execute(migration)
            conn.commit()
        except Exception:
            pass  # Column already exists
    conn.execute(
        "UPDATE hearth_entities SET canonical_key = 'user:' || user_id"
        " WHERE canonical_key IS NULL AND user_id IS NOT NULL;"
    )
    conn.commit()
    conn.execute("""
        UPDATE hearth_episodes SET briefing_category =
            CASE episode_type
                WHEN 'checkin_not_submitted'           THEN 'awareness'
                WHEN 'creator_quiet'                   THEN 'pattern'
                WHEN 'training_comment_needs_response' THEN 'action_needed'
                WHEN 'probation'                       THEN 'action_needed'
                WHEN 'missing_discord'                 THEN 'awareness'
                WHEN 'training_no_engagement'          THEN 'awareness'
                WHEN 'unlinked_battle'                 THEN 'awareness'
                WHEN 'checkin_feedback_waiting'        THEN 'action_needed'
                WHEN 'training_comment_waiting'        THEN 'action_needed'
                WHEN 'new_creator_stuck'               THEN 'pattern'
            END
        WHERE briefing_category IS NULL;
    """)
    conn.commit()


# ---------------------------------------------------------------------------
# Entity helpers
# ---------------------------------------------------------------------------

def sync_users_to_entities(memory_conn, pathway_conn):
    """Ensure every current Pathway user has a hearth_entities row.

    Caches display_name from Pathway as a convenience for rendering. This is
    the only Pathway field stored here — everything else in hearth_entities is
    Hearth's own learned memory, not a copy of Pathway's source of truth.
    """
    try:
        users = pathway_conn.execute(
            "SELECT id, name, tiktok_handle FROM users WHERE status = 'approved';"
        ).fetchall()
    except sqlite3.Error:
        return
    now = datetime.now(timezone.utc).isoformat()
    for user in users:
        display_name = user["tiktok_handle"] or user["name"] or "a team member"
        memory_conn.execute(
            "INSERT INTO hearth_entities"
            " (user_id, display_name, entity_type, source, canonical_key, created_at)"
            " VALUES (?, ?, 'person', 'pathway_sync', ?, ?)"
            " ON CONFLICT(user_id) DO UPDATE SET display_name = excluded.display_name;",
            (user["id"], display_name, f"user:{user['id']}", now),
        )
    memory_conn.commit()


def get_or_create_entity(memory_conn, user_id):
    """Return the hearth_entities row for this user, creating it if needed."""
    row = memory_conn.execute(
        "SELECT * FROM hearth_entities WHERE user_id = ?;", (user_id,)
    ).fetchone()
    if row:
        return row
    now = datetime.now(timezone.utc).isoformat()
    cur = memory_conn.execute(
        "INSERT INTO hearth_entities"
        " (user_id, entity_type, source, canonical_key, created_at)"
        " VALUES (?, 'person', 'pathway_sync', ?, ?);",
        (user_id, f"user:{user_id}", now),
    )
    memory_conn.commit()
    return memory_conn.execute(
        "SELECT * FROM hearth_entities WHERE id = ?;", (cur.lastrowid,)
    ).fetchone()


def get_entity_by_user_id(memory_conn, user_id):
    """Return the hearth_entities row for a Pathway user_id, or None."""
    return memory_conn.execute(
        "SELECT * FROM hearth_entities WHERE user_id = ?;", (user_id,)
    ).fetchone()


def get_entity_context(memory_conn, entity_id):
    """Return a dict with the entity row, its open episodes, and total episode count.

    Used by the context builder to enrich PersonContext beyond what the open_episodes
    query already carries. Returns None if the entity doesn't exist.
    """
    entity = memory_conn.execute(
        "SELECT * FROM hearth_entities WHERE id = ?;", (entity_id,)
    ).fetchone()
    if not entity:
        return None
    open_episodes = get_open_episodes(memory_conn, entity_id=entity_id)
    total_count = memory_conn.execute(
        "SELECT COUNT(*) FROM hearth_episodes WHERE entity_id = ?;", (entity_id,)
    ).fetchone()[0]
    return {
        "entity": entity,
        "open_episodes": open_episodes,
        "total_episode_count": total_count,
    }


# ---------------------------------------------------------------------------
# Episode helpers
# ---------------------------------------------------------------------------

def create_episode(memory_conn, entity_id, episode_type, description,
                   severity="medium", reference_key=None, briefing_category=None):
    """
    Record a new episode unless an identical open one already exists.

    Deduplication logic:
    - If reference_key is set: match on (episode_type, reference_key) globally.
    - Otherwise: match on (entity_id, episode_type) with no reference_key.

    Returns the episode id (existing or newly created).
    """
    if briefing_category is None:
        briefing_category = _BRIEFING_CATEGORIES.get(episode_type)

    if reference_key:
        existing = memory_conn.execute(
            "SELECT id FROM hearth_episodes"
            " WHERE episode_type = ? AND reference_key = ? AND resolved = 0;",
            (episode_type, reference_key),
        ).fetchone()
    else:
        existing = memory_conn.execute(
            "SELECT id FROM hearth_episodes"
            " WHERE entity_id IS ? AND episode_type = ?"
            " AND reference_key IS NULL AND resolved = 0;",
            (entity_id, episode_type),
        ).fetchone()

    if existing:
        return existing["id"], "reused_open_episode"

    now = datetime.now(timezone.utc).isoformat()
    cur = memory_conn.execute(
        "INSERT INTO hearth_episodes"
        " (entity_id, episode_type, reference_key, description, severity, observed_at,"
        "  briefing_category)"
        " VALUES (?, ?, ?, ?, ?, ?, ?);",
        (entity_id, episode_type, reference_key, description, severity, now, briefing_category),
    )
    memory_conn.commit()
    return cur.lastrowid, "created_episode"


def get_open_episodes(memory_conn, entity_id=None, include_evaluator_promoted=False):
    """Return all unresolved episodes, optionally filtered to one entity.

    Each row includes user_id and display_name from hearth_entities so the
    context builder can group by person without extra queries.

    Excludes Experience-Evaluator-promoted rows (see
    EVALUATOR_PROMOTED_EPISODE_TYPES) by default — they remain untrusted
    until promotion is independently verified and manually re-enabled. Pass
    include_evaluator_promoted=True for tools that deliberately need full
    visibility (e.g. hearth_episode_dedup.py's diagnostic scan, or the
    Experience Evaluator cleanup utility itself).
    """
    exclude_sql = "" if include_evaluator_promoted else _exclude_evaluator_promoted_sql("e")
    exclude_params = () if include_evaluator_promoted else tuple(EVALUATOR_PROMOTED_EPISODE_TYPES)
    if entity_id is not None:
        return memory_conn.execute(
            "SELECT e.*, en.user_id, en.display_name FROM hearth_episodes e"
            " LEFT JOIN hearth_entities en ON en.id = e.entity_id"
            f" WHERE e.entity_id = ? AND e.resolved = 0{exclude_sql}"
            " ORDER BY e.observed_at;",
            (entity_id, *exclude_params),
        ).fetchall()
    return memory_conn.execute(
        "SELECT e.*, en.user_id, en.display_name FROM hearth_episodes e"
        " LEFT JOIN hearth_entities en ON en.id = e.entity_id"
        f" WHERE e.resolved = 0{exclude_sql}"
        " ORDER BY e.observed_at;",
        exclude_params,
    ).fetchall()


def update_last_briefed_at(memory_conn, episode_id, ts=None):
    """Record that this episode was included in a briefing. Used to enforce pattern cooldown."""
    if ts is None:
        ts = datetime.now(timezone.utc).isoformat()
    memory_conn.execute(
        "UPDATE hearth_episodes SET last_briefed_at = ? WHERE id = ?;",
        (ts, episode_id),
    )
    memory_conn.commit()


def refresh_episode(memory_conn, episode_id, description, severity=None):
    """Refresh the description (and optionally severity) of an existing open episode.

    Called when a watcher reuses an existing episode but the condition has
    evolved — e.g. days_overdue has increased since the episode was first opened.
    Does not change observed_at, entity, or type.
    """
    if severity is not None:
        memory_conn.execute(
            "UPDATE hearth_episodes SET description = ?, severity = ?"
            " WHERE id = ? AND resolved = 0;",
            (description, severity, episode_id),
        )
    else:
        memory_conn.execute(
            "UPDATE hearth_episodes SET description = ?"
            " WHERE id = ? AND resolved = 0;",
            (description, episode_id),
        )
    memory_conn.commit()


def get_recent_episodes(memory_conn, limit=50):
    """Return the most recent episodes regardless of resolved status."""
    return memory_conn.execute(
        "SELECT e.*, en.user_id FROM hearth_episodes e"
        " LEFT JOIN hearth_entities en ON en.id = e.entity_id"
        " ORDER BY e.observed_at DESC LIMIT ?;",
        (limit,),
    ).fetchall()


def resolve_episode(memory_conn, episode_id, resolved_at=None, resolution_reason=None):
    """Mark a single episode as resolved. Idempotent — safe to call twice.

    resolution_reason (optional): a short, distinct machine-readable tag
    (e.g. "creator_deactivated") stored in hearth_episodes.resolution_reason,
    separate from the human-readable text hearth_trace.py already logs for
    every resolution. Existing callers that omit it are unaffected — the
    column stays NULL, identical to this function's behavior before this
    parameter existed. Added so a resolution driven by something other than
    a normal per-type "condition no longer present" check (e.g. Fix 2's
    general deactivation sweep in morning_briefing.resolve_stale_issues())
    is distinguishable on the episode row itself later, not only in an
    ephemeral trace log.
    """
    if resolved_at is None:
        resolved_at = datetime.now(timezone.utc).isoformat()
    if resolution_reason is not None:
        memory_conn.execute(
            "UPDATE hearth_episodes SET resolved = 1, resolved_at = ?, resolution_reason = ?"
            " WHERE id = ? AND resolved = 0;",
            (resolved_at, resolution_reason, episode_id),
        )
    else:
        memory_conn.execute(
            "UPDATE hearth_episodes SET resolved = 1, resolved_at = ?"
            " WHERE id = ? AND resolved = 0;",
            (resolved_at, episode_id),
        )
    memory_conn.commit()


def get_recent_resolutions(memory_conn, hours=24, include_evaluator_promoted=False):
    """Return episodes resolved in the last N hours, joined to entity display_name.

    Used by the context builder so Hearth can mention positive progress alongside
    open concerns rather than only ever surfacing problems, and by Soul to
    confirm the responsiveness belief — so this excludes Experience-Evaluator-
    promoted rows by default for the same reason get_open_episodes does.
    """
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    exclude_sql = "" if include_evaluator_promoted else _exclude_evaluator_promoted_sql("e")
    exclude_params = () if include_evaluator_promoted else tuple(EVALUATOR_PROMOTED_EPISODE_TYPES)
    return memory_conn.execute(
        "SELECT e.*, en.user_id, en.display_name FROM hearth_episodes e"
        " LEFT JOIN hearth_entities en ON en.id = e.entity_id"
        f" WHERE e.resolved = 1 AND e.resolved_at >= ?{exclude_sql}"
        " ORDER BY e.resolved_at DESC;",
        (since, *exclude_params),
    ).fetchall()


# ---------------------------------------------------------------------------
# Observation processor — builds learned memory from episode history
# ---------------------------------------------------------------------------

# Human-readable labels for episode types used in summaries and pattern text.
# Never includes IDs, table names, or internal implementation language.
_EPISODE_TYPE_LABELS = {
    "probation":                        "account on probation",
    "missing_discord":                  "Discord onboarding delay",
    "training_no_engagement":           "training posted with no engagement",
    "training_comment_needs_response":  "training comment may need a response",
    "creator_quiet":                    "creator quiet period",
    "checkin_feedback_waiting":         "check-in feedback waiting",
    "training_comment_waiting":         "training comment awaiting staff response",
    "support_request_waiting":          "support request awaiting staff response",
    "new_creator_stuck":                "new creator not yet engaged with Pathway",
}

_BRIEFING_CATEGORIES = {
    "checkin_not_submitted":            "awareness",
    "creator_quiet":                    "pattern",
    "training_comment_needs_response":  "action_needed",
    "probation":                        "action_needed",
    "missing_discord":                  "awareness",
    "training_no_engagement":           "awareness",
    "unlinked_battle":                  "awareness",
    "checkin_feedback_waiting":         "action_needed",
    "training_comment_waiting":         "action_needed",
    "support_request_waiting":          "action_needed",
    "new_creator_stuck":                "pattern",
}


def _episode_label(episode_type: str) -> str:
    return _EPISODE_TYPE_LABELS.get(episode_type, episode_type.replace("_", " "))


def _build_entity_summary(all_episodes, open_episodes, type_counts, patterns):
    """
    Return a short, factual summary of what Hearth has observed about this entity.

    Only states what the episode record supports. Never asserts character,
    motivation, or anything not directly derivable from observed events.
    Includes resolved history so the summary reflects the full lifecycle.
    """
    parts = []

    first_date = all_episodes[0]["observed_at"][:10]
    parts.append(f"First observed {first_date}.")

    if patterns:
        parts.append("Recurring: " + "; ".join(patterns) + ".")

    resolved_episodes = [e for e in all_episodes if e["resolved"]]
    open_count = len(open_episodes)

    if open_count == 0:
        if resolved_episodes:
            resolved_labels = sorted({_episode_label(e["episode_type"]) for e in resolved_episodes})
            parts.append(
                f"Previously resolved: {'; '.join(resolved_labels)}. No current open concerns."
            )
        else:
            parts.append("No current open concerns.")
    elif open_count == 1:
        parts.append("1 open concern.")
        if resolved_episodes:
            parts.append(f"{len(resolved_episodes)} previously resolved.")
    else:
        parts.append(f"{open_count} open concerns.")
        if resolved_episodes:
            parts.append(f"{len(resolved_episodes)} previously resolved.")

    return " ".join(parts)


def process_entity_observations(memory_conn, entity_id):
    """
    Examine all episodes for one entity and update its learned memory fields.

    Patterns are only recorded when the same episode type has appeared more than
    once — never from a single data point. Concerns reflect currently open
    episodes. Strengths are left for future phases when positive signal types
    exist. Summary is factual and evidence-backed.

    Experience-Evaluator-promoted rows (see EVALUATOR_PROMOTED_EPISODE_TYPES)
    are excluded from this computation entirely — not just from the
    "awareness" carve-out below — so an invalid promotion can never surface
    in an entity's summary/patterns_noticed/concerns/first_observed_at/
    last_observed_at until promotion is independently verified.
    """
    all_episodes = memory_conn.execute(
        "SELECT episode_type, severity, resolved, observed_at, briefing_category"
        " FROM hearth_episodes WHERE entity_id = ?" + _exclude_evaluator_promoted_sql() +
        " ORDER BY observed_at;",
        (entity_id, *EVALUATOR_PROMOTED_EPISODE_TYPES),
    ).fetchall()

    if not all_episodes:
        return

    open_episodes = [e for e in all_episodes if not e["resolved"]]

    # Timestamps span the full episode history
    first_observed_at = all_episodes[0]["observed_at"]
    last_observed_at = all_episodes[-1]["observed_at"]

    # Awareness episodes track state but don't count toward recurring patterns
    # or concerns surfaced in briefings — exclude them from these learned-memory fields
    briefable_eps = [e for e in all_episodes if e["briefing_category"] != "awareness"]
    open_briefable_eps = [e for e in open_episodes if e["briefing_category"] != "awareness"]

    # Count all-time occurrences per episode type (briefable only)
    type_counts = {}
    for ep in briefable_eps:
        type_counts[ep["episode_type"]] = type_counts.get(ep["episode_type"], 0) + 1

    # Patterns: only types seen more than once (repeated evidence rule)
    patterns = []
    for ep_type, count in sorted(type_counts.items()):
        if count > 1:
            patterns.append(f"{_episode_label(ep_type)} ({count} times)")
    patterns_noticed = "; ".join(patterns) if patterns else None

    # Concerns: open briefable episodes with actionable severity
    concern_labels = [
        _episode_label(ep["episode_type"])
        for ep in open_briefable_eps
        if ep["severity"] in ("high", "medium")
    ]
    concerns = "; ".join(concern_labels) if concern_labels else None

    summary = _build_entity_summary(all_episodes, open_briefable_eps, type_counts, patterns)

    memory_conn.execute(
        "UPDATE hearth_entities"
        " SET summary = ?, patterns_noticed = ?, concerns = ?,"
        "     first_observed_at = ?, last_observed_at = ?"
        " WHERE id = ?;",
        (summary, patterns_noticed, concerns, first_observed_at, last_observed_at, entity_id),
    )
    memory_conn.commit()


def process_all_entities(memory_conn):
    """Update learned memory fields for every entity that has at least one episode."""
    entity_ids = memory_conn.execute(
        "SELECT DISTINCT entity_id FROM hearth_episodes WHERE entity_id IS NOT NULL;"
    ).fetchall()
    for row in entity_ids:
        process_entity_observations(memory_conn, row["entity_id"])
