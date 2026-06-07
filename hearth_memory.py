"""
Hearth Memory — persistent memory layer for the Hearth AI teammate.

Stores observations about Pathway Portal users across runs so Hearth
can reference recurring issues in future briefings. Uses a separate
SQLite file (hearth_memory.db) and never writes to Pathway tables.
"""

import os
import sqlite3
from datetime import datetime, timezone

MEMORY_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hearth_memory.db")


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
    # Migrate existing databases: add display_name if the column isn't there yet
    try:
        conn.execute("ALTER TABLE hearth_entities ADD COLUMN display_name TEXT;")
        conn.commit()
    except Exception:
        pass  # Column already exists


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
        users = pathway_conn.execute("SELECT id, name FROM users;").fetchall()
    except sqlite3.Error:
        return
    now = datetime.now(timezone.utc).isoformat()
    for user in users:
        memory_conn.execute(
            "INSERT INTO hearth_entities (user_id, display_name, created_at)"
            " VALUES (?, ?, ?)"
            " ON CONFLICT(user_id) DO UPDATE SET display_name = excluded.display_name;",
            (user["id"], user["name"], now),
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
        "INSERT INTO hearth_entities (user_id, created_at) VALUES (?, ?);",
        (user_id, now),
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
                   severity="medium", reference_key=None):
    """
    Record a new episode unless an identical open one already exists.

    Deduplication logic:
    - If reference_key is set: match on (episode_type, reference_key) globally.
    - Otherwise: match on (entity_id, episode_type) with no reference_key.

    Returns the episode id (existing or newly created).
    """
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
        return existing["id"]

    now = datetime.now(timezone.utc).isoformat()
    cur = memory_conn.execute(
        "INSERT INTO hearth_episodes"
        " (entity_id, episode_type, reference_key, description, severity, observed_at)"
        " VALUES (?, ?, ?, ?, ?, ?);",
        (entity_id, episode_type, reference_key, description, severity, now),
    )
    memory_conn.commit()
    return cur.lastrowid


def get_open_episodes(memory_conn, entity_id=None):
    """Return all unresolved episodes, optionally filtered to one entity.

    Each row includes user_id and display_name from hearth_entities so the
    context builder can group by person without extra queries.
    """
    if entity_id is not None:
        return memory_conn.execute(
            "SELECT e.*, en.user_id, en.display_name FROM hearth_episodes e"
            " LEFT JOIN hearth_entities en ON en.id = e.entity_id"
            " WHERE e.entity_id = ? AND e.resolved = 0"
            " ORDER BY e.observed_at;",
            (entity_id,),
        ).fetchall()
    return memory_conn.execute(
        "SELECT e.*, en.user_id, en.display_name FROM hearth_episodes e"
        " LEFT JOIN hearth_entities en ON en.id = e.entity_id"
        " WHERE e.resolved = 0"
        " ORDER BY e.observed_at;"
    ).fetchall()


def get_recent_episodes(memory_conn, limit=50):
    """Return the most recent episodes regardless of resolved status."""
    return memory_conn.execute(
        "SELECT e.*, en.user_id FROM hearth_episodes e"
        " LEFT JOIN hearth_entities en ON en.id = e.entity_id"
        " ORDER BY e.observed_at DESC LIMIT ?;",
        (limit,),
    ).fetchall()
