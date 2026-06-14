"""
Hearth Principles — identity and wisdom layer for the Hearth AI teammate.

Stores durable beliefs about how Hearth should interpret creator behavior and
make judgments. Principles are distinct from episodic memory (hearth_memory.py)
and are never merged with Pathway tables.
"""

import os
import sqlite3
from datetime import datetime, timezone

from hearth_memory import MEMORY_DB_PATH

_VALID_STATUSES = {"active", "superseded", "under_review"}


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

def get_principles_connection():
    """Open a read-write connection to the shared Hearth memory database."""
    conn = sqlite3.connect(MEMORY_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def ensure_principles_table(conn):
    """Create hearth_principles if it doesn't exist. Safe to run on existing databases."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS hearth_principles (
            principle_id      INTEGER PRIMARY KEY AUTOINCREMENT,
            content           TEXT    NOT NULL,
            topic_tags        TEXT,
            source            TEXT,
            confidence        REAL    DEFAULT 0.5,
            created_at        TEXT    NOT NULL,
            last_confirmed_at TEXT,
            times_used        INTEGER DEFAULT 0,
            contradicted_by   TEXT,
            status            TEXT    DEFAULT 'active'
        );
    """)
    conn.commit()


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

def create_principle(conn, content, topic_tags, source, confidence=0.5):
    """Insert a new principle and return its principle_id.

    No-ops if a principle with identical content already exists, returning the
    existing id instead.  topic_tags should be a comma-separated string.
    """
    existing = conn.execute(
        "SELECT principle_id FROM hearth_principles WHERE content = ?;",
        (content,),
    ).fetchone()
    if existing:
        return existing["principle_id"]

    now = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        "INSERT INTO hearth_principles"
        " (content, topic_tags, source, confidence, created_at, status)"
        " VALUES (?, ?, ?, ?, ?, 'active');",
        (content, topic_tags, source, confidence, now),
    )
    conn.commit()
    return cur.lastrowid


def list_active_principles(conn):
    """Return all active principles ordered by confidence DESC, created_at ASC."""
    return conn.execute(
        "SELECT * FROM hearth_principles"
        " WHERE status = 'active'"
        " ORDER BY confidence DESC, created_at ASC;",
    ).fetchall()


def get_principles_by_tag(conn, tag):
    """Return active principles whose topic_tags contain tag (case-insensitive)."""
    pattern = f"%{tag.lower()}%"
    return conn.execute(
        "SELECT * FROM hearth_principles"
        " WHERE status = 'active'"
        "   AND LOWER(topic_tags) LIKE ?;",
        (pattern,),
    ).fetchall()


def mark_principle_used(conn, principle_id):
    """Increment times_used and update last_confirmed_at to now."""
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "UPDATE hearth_principles"
        " SET times_used = times_used + 1, last_confirmed_at = ?"
        " WHERE principle_id = ?;",
        (now, principle_id),
    )
    conn.commit()


def update_principle_status(conn, principle_id, new_status):
    """Update a principle's status. Raises ValueError for invalid status values."""
    if new_status not in _VALID_STATUSES:
        raise ValueError(
            f"Invalid status '{new_status}'. Must be one of: {', '.join(sorted(_VALID_STATUSES))}"
        )
    conn.execute(
        "UPDATE hearth_principles SET status = ? WHERE principle_id = ?;",
        (new_status, principle_id),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Smoke test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    conn = get_principles_connection()

    print("Step 1: ensure_principles_table()")
    ensure_principles_table(conn)
    print("  Table ready.")

    print("\nStep 2: create_principle() — seed principle")
    seed_content = (
        "Creator participation at Pathway is voluntary. "
        "Hearth should notice patterns without treating every missed action as a concern."
    )
    pid = create_principle(
        conn,
        content=seed_content,
        topic_tags="creator_activity,pathway_values,judgment",
        source="initial_identity_layer",
        confidence=0.9,
    )
    print(f"  principle_id={pid}")

    print("\nStep 3: list_active_principles()")
    principles = list_active_principles(conn)
    for p in principles:
        print(f"  [{p['principle_id']}] conf={p['confidence']} tags={p['topic_tags']}")
        print(f"       {p['content'][:80]}{'...' if len(p['content']) > 80 else ''}")

    print("\nStep 4: get_principles_by_tag('judgment')")
    tagged = get_principles_by_tag(conn, "judgment")
    print(f"  {len(tagged)} result(s)")
    for p in tagged:
        print(f"  [{p['principle_id']}] {p['content'][:80]}")

    print("\nStep 5: mark_principle_used()")
    mark_principle_used(conn, pid)
    updated = conn.execute(
        "SELECT times_used, last_confirmed_at FROM hearth_principles WHERE principle_id = ?;",
        (pid,),
    ).fetchone()
    print(f"  times_used={updated['times_used']}  last_confirmed_at={updated['last_confirmed_at']}")

    conn.close()
    print("\nSmoke test complete.")
