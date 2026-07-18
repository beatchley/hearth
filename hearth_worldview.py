"""
Hearth Worldview — single access layer for Hearth's worldview tables.

Worldview holds Hearth's interpreted understanding of people and situations:
identity facts, beliefs, relationship dynamics, open uncertainties, observed
changes, and recent lessons. It is distinct from episodic memory
(hearth_memory.py), which records raw events.

Architecture rule:
    Pulse filters. Soul interprets. Worldview updates. Artifacts surface.

Soul will eventually be the only writer to worldview. This module provides
the read/write functions Soul will call — it is not wired into hearth_soul.py,
hearth_context.py, hearth_questions.py, or hearth_pulse.py yet.

Tables (created by migrate_add_hearth_worldview.py):
    hearth_worldview_identity
    hearth_worldview_beliefs
    hearth_worldview_relationships
    hearth_worldview_uncertainties
    hearth_worldview_changes
    hearth_worldview_recent_lessons
"""

import sqlite3
from datetime import datetime, timezone

from hearth_memory import MEMORY_DB_PATH

_CONFIRM_BUMP = 0.05
_CHALLENGE_BUMP = 0.05


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

def get_worldview_connection():
    """Open a read-write connection to the shared Hearth memory database."""
    conn = sqlite3.connect(MEMORY_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def ensure_worldview_tables(conn=None):
    """Verify the six worldview tables exist, creating them if missing.

    Mirrors the schema from migrate_add_hearth_worldview.py as a backup in
    case this module is used before that migration has run. Safe to call
    repeatedly. If conn is not given, opens and closes its own connection.
    """
    owns_conn = conn is None
    if owns_conn:
        conn = get_worldview_connection()
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS hearth_worldview_identity (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                identity_key   TEXT    NOT NULL,
                identity_value TEXT    NOT NULL,
                source         TEXT,
                status         TEXT    DEFAULT 'active',
                created_at     TEXT    DEFAULT CURRENT_TIMESTAMP,
                updated_at     TEXT    DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS hearth_worldview_beliefs (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                subject_type        TEXT,
                subject_id          TEXT,
                belief_type         TEXT,
                belief_text         TEXT    NOT NULL,
                confidence          REAL    DEFAULT 0.5,
                status              TEXT    DEFAULT 'active',
                created_at          TEXT    DEFAULT CURRENT_TIMESTAMP,
                updated_at          TEXT    DEFAULT CURRENT_TIMESTAMP,
                last_confirmed_at   TEXT,
                last_challenged_at  TEXT,
                source_episode_id   TEXT,
                source_signal_id    TEXT,
                source_run          TEXT,
                notes               TEXT
            );

            CREATE TABLE IF NOT EXISTS hearth_worldview_relationships (
                id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                entity_a_type        TEXT,
                entity_a_id          TEXT,
                entity_b_type        TEXT,
                entity_b_id          TEXT,
                relationship_type    TEXT,
                relationship_summary TEXT    NOT NULL,
                confidence           REAL    DEFAULT 0.5,
                status               TEXT    DEFAULT 'active',
                created_at           TEXT    DEFAULT CURRENT_TIMESTAMP,
                updated_at           TEXT    DEFAULT CURRENT_TIMESTAMP,
                last_confirmed_at    TEXT,
                source_episode_id    TEXT,
                source_signal_id     TEXT
            );

            CREATE TABLE IF NOT EXISTS hearth_worldview_uncertainties (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                subject_type      TEXT,
                subject_id        TEXT,
                uncertainty_text  TEXT    NOT NULL,
                why_it_matters    TEXT,
                possible_question TEXT,
                confidence        REAL    DEFAULT 0.5,
                priority          TEXT    DEFAULT 'normal',
                status            TEXT    DEFAULT 'open',
                created_at        TEXT    DEFAULT CURRENT_TIMESTAMP,
                updated_at        TEXT    DEFAULT CURRENT_TIMESTAMP,
                last_seen_at      TEXT,
                resolved_at       TEXT,
                source_episode_id TEXT,
                source_signal_id  TEXT,
                source_run        TEXT
            );

            CREATE TABLE IF NOT EXISTS hearth_worldview_changes (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                subject_type      TEXT,
                subject_id        TEXT,
                change_text       TEXT    NOT NULL,
                previous_state    TEXT,
                current_state     TEXT,
                direction         TEXT,
                confidence        REAL    DEFAULT 0.5,
                status            TEXT    DEFAULT 'watching',
                created_at        TEXT    DEFAULT CURRENT_TIMESTAMP,
                updated_at        TEXT    DEFAULT CURRENT_TIMESTAMP,
                last_seen_at      TEXT,
                source_episode_id TEXT,
                source_signal_id  TEXT,
                source_run        TEXT
            );

            CREATE TABLE IF NOT EXISTS hearth_worldview_recent_lessons (
                id                      INTEGER PRIMARY KEY AUTOINCREMENT,
                lesson_text             TEXT    NOT NULL,
                topic_tags              TEXT,
                confidence              REAL    DEFAULT 0.5,
                status                  TEXT    DEFAULT 'provisional',
                created_at              TEXT    DEFAULT CURRENT_TIMESTAMP,
                updated_at              TEXT    DEFAULT CURRENT_TIMESTAMP,
                last_confirmed_at       TEXT,
                times_confirmed         INTEGER DEFAULT 0,
                times_challenged        INTEGER DEFAULT 0,
                candidate_for_principle INTEGER DEFAULT 0,
                source_episode_id       TEXT,
                source_signal_id        TEXT,
                source_run              TEXT
            );
        """)
        conn.commit()
    finally:
        if owns_conn:
            conn.close()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _now():
    return datetime.now(timezone.utc).isoformat()


def _validate_source_episode_id(source_episode_id):
    """Guard against writing non-numeric values into source_episode_id.

    source_episode_id must point at a real hearth_episodes.id (an integer,
    or a string of digits), or be None. Scan-level provenance (e.g. the
    "morning"/"midday"/"evening" scan label) belongs in source_run, not
    here — see the June 2026 bug where six hearth_soul.py call sites wrote
    the scan label into this field, producing 192 rows with unresolvable
    "episode ids". Raises loudly instead of allowing that to happen again.
    """
    if source_episode_id is None:
        return
    if isinstance(source_episode_id, bool):
        raise ValueError(
            f"source_episode_id must be a real hearth_episodes.id (int) or None,"
            f" got bool {source_episode_id!r}"
        )
    if isinstance(source_episode_id, int):
        return
    if isinstance(source_episode_id, str) and source_episode_id.strip().isdigit():
        return
    raise ValueError(
        f"source_episode_id must be a real hearth_episodes.id (int, or a string of"
        f" digits) or None — got {source_episode_id!r}. Scan-level provenance (e.g."
        f" a scan label like 'morning') belongs in source_run, not source_episode_id."
    )


def _clamp_confidence(value):
    return max(0.0, min(1.0, value))


def _build_filters(filters):
    """Turn a dict of column -> value into (clauses, params), skipping None values."""
    clauses = []
    params = []
    for column, value in filters.items():
        if value is not None:
            clauses.append(f"{column} = ?")
            params.append(value)
    return clauses, params


def _update_row(conn, table, row_id, fields):
    """Apply an UPDATE ... SET ... WHERE id = ? from a dict of column -> value."""
    if not fields:
        return
    set_clause = ", ".join(f"{column} = ?" for column in fields)
    params = list(fields.values()) + [row_id]
    conn.execute(f"UPDATE {table} SET {set_clause} WHERE id = ?;", params)
    conn.commit()


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------

def upsert_identity(conn, identity_key, identity_value, source=None, status="active"):
    """Insert or update the active identity row for identity_key.

    If an active row already exists for identity_key, updates its
    identity_value, source, status, and updated_at in place. Otherwise inserts
    a new row. Does not touch any pre-existing inactive/archived rows.
    """
    now = _now()
    existing = conn.execute(
        "SELECT id FROM hearth_worldview_identity"
        " WHERE identity_key = ? AND status = 'active'"
        " ORDER BY updated_at DESC LIMIT 1;",
        (identity_key,),
    ).fetchone()
    if existing:
        conn.execute(
            "UPDATE hearth_worldview_identity"
            " SET identity_value = ?, source = ?, status = ?, updated_at = ?"
            " WHERE id = ?;",
            (identity_value, source, status, now, existing["id"]),
        )
        conn.commit()
        return existing["id"]

    cur = conn.execute(
        "INSERT INTO hearth_worldview_identity"
        " (identity_key, identity_value, source, status, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?);",
        (identity_key, identity_value, source, status, now, now),
    )
    conn.commit()
    return cur.lastrowid


def get_identity(conn, status="active"):
    """Return identity rows with the given status, ordered by identity_key."""
    return conn.execute(
        "SELECT * FROM hearth_worldview_identity"
        " WHERE status = ?"
        " ORDER BY identity_key ASC;",
        (status,),
    ).fetchall()


def get_identity_value(conn, identity_key, default=None):
    """Return the active identity_value for identity_key, or default if none."""
    row = conn.execute(
        "SELECT identity_value FROM hearth_worldview_identity"
        " WHERE identity_key = ? AND status = 'active'"
        " ORDER BY updated_at DESC LIMIT 1;",
        (identity_key,),
    ).fetchone()
    return row["identity_value"] if row else default


# ---------------------------------------------------------------------------
# Beliefs
# ---------------------------------------------------------------------------

def add_belief(conn, subject_type=None, subject_id=None, belief_type=None, belief_text=None,
               confidence=0.5, status="active", source_episode_id=None, source_signal_id=None,
               source_run=None, notes=None):
    """Insert a new belief and return its id. Raises ValueError if belief_text is empty.

    source_episode_id must be a real hearth_episodes.id (or None) — use
    source_run for scan-level provenance (e.g. "morning"/"midday"/"evening")
    when the belief is drawn from an aggregate pattern rather than one
    specific episode. See _validate_source_episode_id.
    """
    if not belief_text:
        raise ValueError("belief_text is required")
    _validate_source_episode_id(source_episode_id)
    now = _now()
    cur = conn.execute(
        "INSERT INTO hearth_worldview_beliefs"
        " (subject_type, subject_id, belief_type, belief_text, confidence, status,"
        "  created_at, updated_at, source_episode_id, source_signal_id, source_run, notes)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);",
        (subject_type, subject_id, belief_type, belief_text, _clamp_confidence(confidence),
         status, now, now, source_episode_id, source_signal_id, source_run, notes),
    )
    conn.commit()
    return cur.lastrowid


def update_belief(conn, belief_id, belief_text=None, confidence=None, status=None,
                   last_confirmed=False, last_challenged=False, notes=None):
    """Update a belief in place. updated_at is always refreshed."""
    fields = {}
    if belief_text is not None:
        fields["belief_text"] = belief_text
    if confidence is not None:
        fields["confidence"] = _clamp_confidence(confidence)
    if status is not None:
        fields["status"] = status
    if notes is not None:
        fields["notes"] = notes
    now = _now()
    fields["updated_at"] = now
    if last_confirmed:
        fields["last_confirmed_at"] = now
    if last_challenged:
        fields["last_challenged_at"] = now
    _update_row(conn, "hearth_worldview_beliefs", belief_id, fields)


def get_active_beliefs(conn, subject_type=None, subject_id=None, belief_type=None, limit=None):
    """Return active beliefs, optionally filtered, ordered by updated_at DESC.

    Pass limit to cap the number of rows returned (most recently updated first).
    """
    clauses, params = _build_filters({
        "subject_type": subject_type,
        "subject_id": subject_id,
        "belief_type": belief_type,
    })
    sql = "SELECT * FROM hearth_worldview_beliefs WHERE status = 'active'"
    if clauses:
        sql += " AND " + " AND ".join(clauses)
    sql += " ORDER BY updated_at DESC"
    if limit is not None:
        sql += " LIMIT ?"
        params = params + [limit]
    return conn.execute(sql + ";", params).fetchall()


def get_belief_by_id(conn, belief_id):
    """Return one belief row by id, or None if not found."""
    return conn.execute(
        "SELECT * FROM hearth_worldview_beliefs WHERE id = ?;",
        (belief_id,),
    ).fetchone()


# ---------------------------------------------------------------------------
# Relationships
# ---------------------------------------------------------------------------

def add_relationship_understanding(conn, entity_a_type=None, entity_a_id=None,
                                    entity_b_type=None, entity_b_id=None, relationship_type=None,
                                    relationship_summary=None, confidence=0.5, status="active",
                                    source_episode_id=None, source_signal_id=None):
    """Insert an interpreted relationship dynamic and return its id.

    This stores Hearth's interpretation of a relationship, not a raw
    assignment record (those live in hearth_relationships.py).
    """
    if not relationship_summary:
        raise ValueError("relationship_summary is required")
    _validate_source_episode_id(source_episode_id)
    now = _now()
    cur = conn.execute(
        "INSERT INTO hearth_worldview_relationships"
        " (entity_a_type, entity_a_id, entity_b_type, entity_b_id, relationship_type,"
        "  relationship_summary, confidence, status, created_at, updated_at,"
        "  source_episode_id, source_signal_id)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);",
        (entity_a_type, entity_a_id, entity_b_type, entity_b_id, relationship_type,
         relationship_summary, _clamp_confidence(confidence), status, now, now,
         source_episode_id, source_signal_id),
    )
    conn.commit()
    return cur.lastrowid


def get_active_relationship_understandings(conn, entity_a_type=None, entity_a_id=None,
                                            entity_b_type=None, entity_b_id=None,
                                            relationship_type=None, limit=None):
    """Return active relationship understandings, optionally filtered.

    Pass limit to cap the number of rows returned (most recently updated first).
    """
    clauses, params = _build_filters({
        "entity_a_type": entity_a_type,
        "entity_a_id": entity_a_id,
        "entity_b_type": entity_b_type,
        "entity_b_id": entity_b_id,
        "relationship_type": relationship_type,
    })
    sql = "SELECT * FROM hearth_worldview_relationships WHERE status = 'active'"
    if clauses:
        sql += " AND " + " AND ".join(clauses)
    sql += " ORDER BY updated_at DESC"
    if limit is not None:
        sql += " LIMIT ?"
        params = params + [limit]
    return conn.execute(sql + ";", params).fetchall()


def update_relationship_understanding(conn, relationship_id, relationship_summary=None,
                                       confidence=None, status=None, last_confirmed=False):
    """Update a relationship understanding in place. updated_at is always refreshed."""
    fields = {}
    if relationship_summary is not None:
        fields["relationship_summary"] = relationship_summary
    if confidence is not None:
        fields["confidence"] = _clamp_confidence(confidence)
    if status is not None:
        fields["status"] = status
    now = _now()
    fields["updated_at"] = now
    if last_confirmed:
        fields["last_confirmed_at"] = now
    _update_row(conn, "hearth_worldview_relationships", relationship_id, fields)


# ---------------------------------------------------------------------------
# Uncertainties
# ---------------------------------------------------------------------------

def open_uncertainty(conn, subject_type=None, subject_id=None, uncertainty_text=None,
                      why_it_matters=None, possible_question=None, confidence=0.5,
                      priority="normal", source_episode_id=None, source_signal_id=None):
    """Insert a new open uncertainty and return its id."""
    if not uncertainty_text:
        raise ValueError("uncertainty_text is required")
    _validate_source_episode_id(source_episode_id)
    now = _now()
    cur = conn.execute(
        "INSERT INTO hearth_worldview_uncertainties"
        " (subject_type, subject_id, uncertainty_text, why_it_matters, possible_question,"
        "  confidence, priority, status, created_at, updated_at,"
        "  source_episode_id, source_signal_id)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?);",
        (subject_type, subject_id, uncertainty_text, why_it_matters, possible_question,
         _clamp_confidence(confidence), priority, now, now, source_episode_id, source_signal_id),
    )
    conn.commit()
    return cur.lastrowid


def get_uncertainty(conn, uncertainty_id):
    """Return one uncertainty row by id, or None if not found."""
    return conn.execute(
        "SELECT * FROM hearth_worldview_uncertainties WHERE id = ?;",
        (uncertainty_id,),
    ).fetchone()


def get_open_uncertainties(conn, subject_type=None, subject_id=None, priority=None, limit=None):
    """Return open uncertainties (status='open'), optionally filtered, ordered by updated_at DESC.

    Pass limit to cap the number of rows returned (most recently updated first).
    Use get_living_uncertainties() to include question_surfaced rows as well.
    """
    clauses, params = _build_filters({
        "subject_type": subject_type,
        "subject_id": subject_id,
        "priority": priority,
    })
    sql = "SELECT * FROM hearth_worldview_uncertainties WHERE status = 'open'"
    if clauses:
        sql += " AND " + " AND ".join(clauses)
    sql += " ORDER BY updated_at DESC"
    if limit is not None:
        sql += " LIMIT ?"
        params = params + [limit]
    return conn.execute(sql + ";", params).fetchall()


def get_living_uncertainties(conn, subject_type=None, subject_id=None, priority=None, limit=None):
    """Return uncertainties with living statuses (open, question_surfaced), optionally filtered.

    'Living' means the uncertainty is still active — either not yet surfaced
    (open) or surfaced but awaiting resolution (question_surfaced). Terminal
    statuses (resolved, archived, dismissed) are excluded.

    Pass limit to cap the number of rows returned (most recently updated first).
    """
    clauses, params = _build_filters({
        "subject_type": subject_type,
        "subject_id": subject_id,
        "priority": priority,
    })
    sql = (
        "SELECT * FROM hearth_worldview_uncertainties"
        " WHERE status IN ('open', 'question_surfaced')"
    )
    if clauses:
        sql += " AND " + " AND ".join(clauses)
    sql += " ORDER BY updated_at DESC"
    if limit is not None:
        sql += " LIMIT ?"
        params = params + [limit]
    return conn.execute(sql + ";", params).fetchall()


def upsert_uncertainty(conn, subject_type=None, subject_id=None, uncertainty_text=None,
                       why_it_matters=None, possible_question=None, confidence=0.5,
                       priority="normal", source_episode_id=None, source_signal_id=None,
                       source_run=None):
    """Find or create a living uncertainty for (subject_type, subject_id).

    Searches for an existing row with status IN ('open', 'question_surfaced').
    - If found: updates uncertainty_text, updated_at, last_seen_at, and
      source_run (which scan most recently re-observed this) in place;
      preserves status (including question_surfaced) and does not overwrite
      source_episode_id. Returns (id, False).
    - If not found: inserts a new row with status='open'. Returns (id, True).

    This makes uncertainties behave as living objects — repeated detection of
    the same condition refreshes the existing row rather than creating duplicates.
    Terminal statuses (resolved, archived, dismissed) are never reused.

    source_episode_id must be a real hearth_episodes.id (or None) — use
    source_run for scan-level provenance (e.g. "morning"/"midday"/"evening")
    when the uncertainty is drawn from an aggregate pattern rather than one
    specific episode. See _validate_source_episode_id.
    """
    if not uncertainty_text:
        raise ValueError("uncertainty_text is required")
    _validate_source_episode_id(source_episode_id)
    now = _now()
    existing = get_living_uncertainties(
        conn, subject_type=subject_type, subject_id=subject_id, limit=1,
    )
    if existing:
        row = existing[0]
        refresh_fields = {
            "uncertainty_text": uncertainty_text,
            "updated_at": now,
            "last_seen_at": now,
        }
        if source_run is not None:
            refresh_fields["source_run"] = source_run
        _update_row(conn, "hearth_worldview_uncertainties", row["id"], refresh_fields)
        return row["id"], False

    cur = conn.execute(
        "INSERT INTO hearth_worldview_uncertainties"
        " (subject_type, subject_id, uncertainty_text, why_it_matters, possible_question,"
        "  confidence, priority, status, created_at, updated_at, last_seen_at,"
        "  source_episode_id, source_signal_id, source_run)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?, ?, ?);",
        (subject_type, subject_id, uncertainty_text, why_it_matters, possible_question,
         _clamp_confidence(confidence), priority, now, now, now,
         source_episode_id, source_signal_id, source_run),
    )
    conn.commit()
    return cur.lastrowid, True


def update_uncertainty(conn, uncertainty_id, uncertainty_text=None, why_it_matters=None,
                        possible_question=None, confidence=None, priority=None, status=None,
                        last_seen=False):
    """Update an uncertainty in place. updated_at is always refreshed.

    Pass last_seen=True to also stamp last_seen_at (use when re-observing the
    same condition without changing status).
    """
    fields = {}
    if uncertainty_text is not None:
        fields["uncertainty_text"] = uncertainty_text
    if why_it_matters is not None:
        fields["why_it_matters"] = why_it_matters
    if possible_question is not None:
        fields["possible_question"] = possible_question
    if confidence is not None:
        fields["confidence"] = _clamp_confidence(confidence)
    if priority is not None:
        fields["priority"] = priority
    if status is not None:
        fields["status"] = status
    now = _now()
    fields["updated_at"] = now
    if last_seen:
        fields["last_seen_at"] = now
    _update_row(conn, "hearth_worldview_uncertainties", uncertainty_id, fields)


def resolve_uncertainty(conn, uncertainty_id):
    """Mark an uncertainty resolved and stamp resolved_at and updated_at."""
    now = _now()
    _update_row(conn, "hearth_worldview_uncertainties", uncertainty_id, {
        "status": "resolved",
        "resolved_at": now,
        "updated_at": now,
    })


# ---------------------------------------------------------------------------
# Changes
# ---------------------------------------------------------------------------

def record_change(conn, subject_type=None, subject_id=None, change_text=None,
                   previous_state=None, current_state=None, direction=None, confidence=0.5,
                   status="watching", source_episode_id=None, source_signal_id=None,
                   source_run=None):
    """Insert a new watched change and return its id.

    source_episode_id must be a real hearth_episodes.id (or None) — use
    source_run for scan-level provenance (e.g. "morning"/"midday"/"evening")
    when the change is drawn from an aggregate pattern rather than one
    specific episode. See _validate_source_episode_id.
    """
    if not change_text:
        raise ValueError("change_text is required")
    _validate_source_episode_id(source_episode_id)
    now = _now()
    cur = conn.execute(
        "INSERT INTO hearth_worldview_changes"
        " (subject_type, subject_id, change_text, previous_state, current_state, direction,"
        "  confidence, status, created_at, updated_at, source_episode_id, source_signal_id,"
        "  source_run)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);",
        (subject_type, subject_id, change_text, previous_state, current_state, direction,
         _clamp_confidence(confidence), status, now, now, source_episode_id, source_signal_id,
         source_run),
    )
    conn.commit()
    return cur.lastrowid


def get_watched_changes(conn, subject_type=None, subject_id=None, direction=None, limit=None):
    """Return changes with status 'watching', optionally filtered.

    Pass limit to cap the number of rows returned (most recently updated first).
    """
    clauses, params = _build_filters({
        "subject_type": subject_type,
        "subject_id": subject_id,
        "direction": direction,
    })
    sql = "SELECT * FROM hearth_worldview_changes WHERE status = 'watching'"
    if clauses:
        sql += " AND " + " AND ".join(clauses)
    sql += " ORDER BY updated_at DESC"
    if limit is not None:
        sql += " LIMIT ?"
        params = params + [limit]
    return conn.execute(sql + ";", params).fetchall()


def update_change(conn, change_id, change_text=None, previous_state=None, current_state=None,
                   direction=None, confidence=None, status=None, last_seen=False):
    """Update a change in place. updated_at is always refreshed."""
    fields = {}
    if change_text is not None:
        fields["change_text"] = change_text
    if previous_state is not None:
        fields["previous_state"] = previous_state
    if current_state is not None:
        fields["current_state"] = current_state
    if direction is not None:
        fields["direction"] = direction
    if confidence is not None:
        fields["confidence"] = _clamp_confidence(confidence)
    if status is not None:
        fields["status"] = status
    now = _now()
    fields["updated_at"] = now
    if last_seen:
        fields["last_seen_at"] = now
    _update_row(conn, "hearth_worldview_changes", change_id, fields)


# ---------------------------------------------------------------------------
# Entity reflection refs
# ---------------------------------------------------------------------------

_VALID_REFLECTION_TYPES = ("worldview_belief", "worldview_uncertainty", "worldview_change")


def create_entity_ref(conn, entity_id, reflection_type, reflection_id, source=None, confidence=None):
    """Leave a breadcrumb linking a Building to a worldview artifact genuinely created for it.

    This is Hearth's evidence trail: every time a belief, uncertainty, or
    watched change is created (not merely refreshed) for a specific Building,
    a ref records that link. This is what will eventually let Ask Hearth
    answer "why does Hearth believe this" by following the breadcrumbs left
    when Hearth learned something, rather than re-deriving the answer from
    scratch. Deciding whether an artifact deserves a ref is the caller's job
    (only on genuine creation, never on update/refresh of an existing
    artifact) — this function only performs the write.

    entity_id is the Hearth Building (hearth_entities.id) this artifact
    belongs to. reflection_type is one of "worldview_belief",
    "worldview_uncertainty", or "worldview_change" — no other values in V1.
    reflection_id is the id of the actual belief/uncertainty/change row.

    source and confidence describe the association itself — why this
    artifact belongs to this Building, and how confident Hearth is in that
    association — not the underlying artifact's own confidence field.

    Idempotent via the existing UNIQUE(entity_id, reflection_type,
    reflection_id) constraint: calling this twice for the same identity never
    raises and always returns the same row id. Does not rely on
    cursor.lastrowid after a conflict — SQLite's INSERT ... ON CONFLICT DO
    NOTHING does not report a pre-existing row's id that way — so the row is
    always looked up by its unique identity afterward, regardless of whether
    this call created it or found it already there.
    """
    if reflection_type not in _VALID_REFLECTION_TYPES:
        raise ValueError(
            f"reflection_type must be one of {_VALID_REFLECTION_TYPES}, got {reflection_type!r}"
        )
    conn.execute(
        "INSERT INTO hearth_entity_reflection_refs"
        " (entity_id, reflection_type, reflection_id, source, confidence)"
        " VALUES (?, ?, ?, ?, ?)"
        " ON CONFLICT(entity_id, reflection_type, reflection_id) DO NOTHING;",
        (entity_id, reflection_type, reflection_id, source, confidence),
    )
    conn.commit()
    row = conn.execute(
        "SELECT id FROM hearth_entity_reflection_refs"
        " WHERE entity_id = ? AND reflection_type = ? AND reflection_id = ?;",
        (entity_id, reflection_type, reflection_id),
    ).fetchone()
    return row["id"]


# ---------------------------------------------------------------------------
# Recent lessons
# ---------------------------------------------------------------------------

def add_recent_lesson(conn, lesson_text=None, topic_tags=None, confidence=0.5,
                       status="provisional", candidate_for_principle=0,
                       source_episode_id=None, source_signal_id=None, source_run=None):
    """Insert a new recent lesson and return its id.

    source_episode_id must be a real hearth_episodes.id (or None) — use
    source_run for scan-level provenance (e.g. "morning"/"midday"/"evening")
    when the lesson is drawn from an aggregate pattern rather than one
    specific episode. See _validate_source_episode_id.
    """
    if not lesson_text:
        raise ValueError("lesson_text is required")
    _validate_source_episode_id(source_episode_id)
    now = _now()
    cur = conn.execute(
        "INSERT INTO hearth_worldview_recent_lessons"
        " (lesson_text, topic_tags, confidence, status, created_at, updated_at,"
        "  candidate_for_principle, source_episode_id, source_signal_id, source_run)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);",
        (lesson_text, topic_tags, _clamp_confidence(confidence), status, now, now,
         candidate_for_principle, source_episode_id, source_signal_id, source_run),
    )
    conn.commit()
    return cur.lastrowid


def get_recent_lessons(conn, status=None, candidate_for_principle=None, limit=None):
    """Return recent lessons, optionally filtered, ordered by created_at DESC.

    Pass limit to cap the number of rows returned (most recent first).
    """
    clauses, params = _build_filters({
        "status": status,
        "candidate_for_principle": candidate_for_principle,
    })
    sql = "SELECT * FROM hearth_worldview_recent_lessons"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY created_at DESC"
    if limit is not None:
        sql += " LIMIT ?"
        params = params + [limit]
    return conn.execute(sql + ";", params).fetchall()


def update_recent_lesson(conn, lesson_id, lesson_text=None, topic_tags=None, confidence=None,
                          status=None, candidate_for_principle=None):
    """Update a recent lesson in place. updated_at is always refreshed."""
    fields = {}
    if lesson_text is not None:
        fields["lesson_text"] = lesson_text
    if topic_tags is not None:
        fields["topic_tags"] = topic_tags
    if confidence is not None:
        fields["confidence"] = _clamp_confidence(confidence)
    if status is not None:
        fields["status"] = status
    if candidate_for_principle is not None:
        fields["candidate_for_principle"] = candidate_for_principle
    fields["updated_at"] = _now()
    _update_row(conn, "hearth_worldview_recent_lessons", lesson_id, fields)


def confirm_recent_lesson(conn, lesson_id, bump=_CONFIRM_BUMP):
    """Increment times_confirmed, stamp last_confirmed_at, and nudge confidence up."""
    row = conn.execute(
        "SELECT confidence, times_confirmed FROM hearth_worldview_recent_lessons WHERE id = ?;",
        (lesson_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"No recent lesson with id={lesson_id}")
    now = _now()
    _update_row(conn, "hearth_worldview_recent_lessons", lesson_id, {
        "confidence": _clamp_confidence(row["confidence"] + bump),
        "times_confirmed": row["times_confirmed"] + 1,
        "last_confirmed_at": now,
        "updated_at": now,
    })


def challenge_recent_lesson(conn, lesson_id, bump=_CHALLENGE_BUMP):
    """Increment times_challenged and nudge confidence down."""
    row = conn.execute(
        "SELECT confidence, times_challenged FROM hearth_worldview_recent_lessons WHERE id = ?;",
        (lesson_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"No recent lesson with id={lesson_id}")
    now = _now()
    _update_row(conn, "hearth_worldview_recent_lessons", lesson_id, {
        "confidence": _clamp_confidence(row["confidence"] - bump),
        "times_challenged": row["times_challenged"] + 1,
        "updated_at": now,
    })


# ---------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------

def get_worldview_snapshot(conn, belief_limit=25, relationship_limit=25, uncertainty_limit=25,
                            change_limit=25, lesson_limit=25):
    """Return a dict snapshot of worldview state for Soul, Context, and Questions to read.

    Each category except identity (which stays small) is capped by its limit
    parameter, most recently updated/created first, so callers don't pull
    unbounded data as the worldview grows. Pass limit=None for a category to
    fetch everything; the defaults preserve existing callers' behavior for
    small tables and avoid surprises as tables grow.
    """
    return {
        "identity": get_identity(conn),
        "active_beliefs": get_active_beliefs(conn, limit=belief_limit),
        "active_relationships": get_active_relationship_understandings(conn, limit=relationship_limit),
        "open_uncertainties": get_living_uncertainties(conn, limit=uncertainty_limit),
        "watched_changes": get_watched_changes(conn, limit=change_limit),
        "recent_lessons": get_recent_lessons(conn, limit=lesson_limit),
    }


# ---------------------------------------------------------------------------
# Smoke test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    _TEST_MARKER = "session_2_smoke_test"

    conn = get_worldview_connection()
    try:
        print("Step 1: ensure_worldview_tables()")
        ensure_worldview_tables(conn)
        tables = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE 'hearth_worldview_%';"
            )
        }
        for name in (
            "hearth_worldview_identity", "hearth_worldview_beliefs",
            "hearth_worldview_relationships", "hearth_worldview_uncertainties",
            "hearth_worldview_changes", "hearth_worldview_recent_lessons",
        ):
            assert name in tables, f"Missing table: {name}"
        print(f"  All six worldview tables present: {sorted(tables)}")

        print("\nStep 2: upsert_identity() twice — confirm no duplicate active rows")
        test_key = "session_2_smoke_test_identity_key"
        id1 = upsert_identity(conn, test_key, "first value", source=_TEST_MARKER)
        id2 = upsert_identity(conn, test_key, "second value", source=_TEST_MARKER)
        assert id1 == id2, f"Expected same row id, got {id1} and {id2}"
        active_for_key = [
            r for r in get_identity(conn, status="active") if r["identity_key"] == test_key
        ]
        assert len(active_for_key) == 1, f"Expected 1 active row, found {len(active_for_key)}"
        assert get_identity_value(conn, test_key) == "second value"
        print(f"  identity_id={id1}, identity_value={get_identity_value(conn, test_key)!r}")

        print("\nStep 3: add_belief() / update_belief() / get_belief_by_id()")
        belief_id = add_belief(
            conn, subject_type="creator", subject_id="test_creator_1",
            belief_type="engagement", belief_text="SESSION 2 SMOKE TEST belief text",
            confidence=0.6, source_episode_id=_TEST_MARKER,
        )
        update_belief(conn, belief_id, confidence=0.9, last_confirmed=True,
                      notes="SESSION 2 SMOKE TEST note")
        belief = get_belief_by_id(conn, belief_id)
        assert belief["confidence"] == 0.9
        assert belief["last_confirmed_at"] is not None
        print(f"  belief_id={belief_id}, confidence={belief['confidence']}, "
              f"last_confirmed_at={belief['last_confirmed_at']}")

        print("\nStep 4: add_relationship_understanding() / update_relationship_understanding()")
        rel_id = add_relationship_understanding(
            conn, entity_a_type="creator", entity_a_id="test_creator_1",
            entity_b_type="coach", entity_b_id="test_coach_1",
            relationship_type="coach_dynamic",
            relationship_summary="SESSION 2 SMOKE TEST relationship summary",
            source_episode_id=_TEST_MARKER,
        )
        update_relationship_understanding(conn, rel_id, confidence=0.8, last_confirmed=True)
        rel_rows = get_active_relationship_understandings(conn, entity_a_id="test_creator_1")
        assert any(r["id"] == rel_id for r in rel_rows)
        print(f"  relationship_id={rel_id}, active_count={len(rel_rows)}")

        print("\nStep 5: open_uncertainty() / resolve_uncertainty()")
        unc_id = open_uncertainty(
            conn, subject_type="creator", subject_id="test_creator_1",
            uncertainty_text="SESSION 2 SMOKE TEST uncertainty text",
            why_it_matters="testing", source_episode_id=_TEST_MARKER,
        )
        resolve_uncertainty(conn, unc_id)
        unc = conn.execute(
            "SELECT * FROM hearth_worldview_uncertainties WHERE id = ?;", (unc_id,)
        ).fetchone()
        assert unc["status"] == "resolved"
        assert unc["resolved_at"] is not None
        print(f"  uncertainty_id={unc_id}, status={unc['status']}, resolved_at={unc['resolved_at']}")

        print("\nStep 6: record_change() / update_change()")
        change_id = record_change(
            conn, subject_type="creator", subject_id="test_creator_1",
            change_text="SESSION 2 SMOKE TEST change text",
            previous_state="quiet", current_state="active",
            direction="improving", source_episode_id=_TEST_MARKER,
        )
        update_change(conn, change_id, current_state="very active", last_seen=True)
        change = conn.execute(
            "SELECT * FROM hearth_worldview_changes WHERE id = ?;", (change_id,)
        ).fetchone()
        assert change["current_state"] == "very active"
        assert change["last_seen_at"] is not None
        print(f"  change_id={change_id}, current_state={change['current_state']}, "
              f"last_seen_at={change['last_seen_at']}")

        print("\nStep 7: add_recent_lesson() / confirm_recent_lesson() / challenge_recent_lesson()")
        lesson_id = add_recent_lesson(
            conn, lesson_text="SESSION 2 SMOKE TEST lesson text",
            topic_tags="testing,smoke_test", confidence=0.5,
            source_episode_id=_TEST_MARKER,
        )
        confirm_recent_lesson(conn, lesson_id)
        lesson = get_recent_lessons(conn, status="provisional")
        lesson = next(r for r in lesson if r["id"] == lesson_id)
        assert lesson["times_confirmed"] == 1
        assert lesson["confidence"] > 0.5
        challenge_recent_lesson(conn, lesson_id)
        lesson = next(r for r in get_recent_lessons(conn) if r["id"] == lesson_id)
        assert lesson["times_challenged"] == 1
        print(f"  lesson_id={lesson_id}, confidence={lesson['confidence']}, "
              f"times_confirmed={lesson['times_confirmed']}, times_challenged={lesson['times_challenged']}")

        print("\nStep 8: get_worldview_snapshot()")
        snapshot = get_worldview_snapshot(conn)
        for key, rows in snapshot.items():
            print(f"  {key}: {len(rows)} row(s)")
        assert any(r["id"] == belief_id for r in snapshot["active_beliefs"])
        assert any(r["id"] == lesson_id for r in snapshot["recent_lessons"])

        print("\nAll smoke test assertions passed.")
    finally:
        print("\nStep 9: cleanup — removing all session_2_smoke_test rows")
        conn.execute("DELETE FROM hearth_worldview_identity WHERE source = ?;", (_TEST_MARKER,))
        conn.execute("DELETE FROM hearth_worldview_beliefs WHERE source_episode_id = ?;", (_TEST_MARKER,))
        conn.execute("DELETE FROM hearth_worldview_relationships WHERE source_episode_id = ?;", (_TEST_MARKER,))
        conn.execute("DELETE FROM hearth_worldview_uncertainties WHERE source_episode_id = ?;", (_TEST_MARKER,))
        conn.execute("DELETE FROM hearth_worldview_changes WHERE source_episode_id = ?;", (_TEST_MARKER,))
        conn.execute("DELETE FROM hearth_worldview_recent_lessons WHERE source_episode_id = ?;", (_TEST_MARKER,))
        conn.commit()
        remaining = sum(
            conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE "
                f"{'source' if table == 'hearth_worldview_identity' else 'source_episode_id'} = ?;",
                (_TEST_MARKER,),
            ).fetchone()[0]
            for table in (
                "hearth_worldview_identity", "hearth_worldview_beliefs",
                "hearth_worldview_relationships", "hearth_worldview_uncertainties",
                "hearth_worldview_changes", "hearth_worldview_recent_lessons",
            )
        )
        print(f"  Remaining smoke-test rows after cleanup: {remaining}")
        conn.close()
        print("\nSmoke test complete.")
