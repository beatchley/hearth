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


def increase_principle_confidence(conn, principle_id, amount=0.02):
    """Nudge confidence upward by amount, capped at 1.0. Active principles only.

    Returns new confidence value, or None if principle not found or not active.
    """
    row = conn.execute(
        "SELECT confidence FROM hearth_principles"
        " WHERE principle_id = ? AND status = 'active';",
        (principle_id,),
    ).fetchone()
    if row is None:
        return None
    new_conf = min(row["confidence"] + amount, 1.0)
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "UPDATE hearth_principles"
        " SET confidence = ?, last_confirmed_at = ?"
        " WHERE principle_id = ?;",
        (new_conf, now, principle_id),
    )
    conn.commit()
    return new_conf


def mark_principle_used(conn, principle_id):
    """Increment times_used and nudge confidence up by 0.02."""
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "UPDATE hearth_principles"
        " SET times_used = times_used + 1, last_confirmed_at = ?"
        " WHERE principle_id = ?;",
        (now, principle_id),
    )
    conn.commit()
    increase_principle_confidence(conn, principle_id, amount=0.02)


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


def flag_principle_for_review(conn, principle_id, reason=""):
    """Mark a principle under_review and reduce confidence by 0.15 (floor 0.1).

    Appends reason to contradicted_by. Returns updated confidence, or None if
    principle not found.
    """
    row = conn.execute(
        "SELECT confidence, contradicted_by FROM hearth_principles"
        " WHERE principle_id = ?;",
        (principle_id,),
    ).fetchone()
    if row is None:
        return None
    new_conf = max(row["confidence"] - 0.15, 0.1)
    existing = row["contradicted_by"] or ""
    new_contradicted = f"{existing} | {reason}" if existing else reason
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "UPDATE hearth_principles"
        " SET status = 'under_review', confidence = ?, contradicted_by = ?,"
        "     last_confirmed_at = ?"
        " WHERE principle_id = ?;",
        (new_conf, new_contradicted, now, principle_id),
    )
    conn.commit()
    return new_conf


def supersede_principle(conn, old_principle_id, replacement_principle_id):
    """Mark old_principle as superseded by replacement. Preserves history.

    Returns True if successful, False if old principle not found.
    """
    row = conn.execute(
        "SELECT contradicted_by FROM hearth_principles WHERE principle_id = ?;",
        (old_principle_id,),
    ).fetchone()
    if row is None:
        return False
    existing = row["contradicted_by"] or ""
    if existing:
        new_contradicted = f"{existing} | superseded_by:{replacement_principle_id}"
    else:
        new_contradicted = str(replacement_principle_id)
    conn.execute(
        "UPDATE hearth_principles"
        " SET status = 'superseded', contradicted_by = ?"
        " WHERE principle_id = ?;",
        (new_contradicted, old_principle_id),
    )
    conn.commit()
    return True


def get_principles_under_review(conn):
    """Return all principles with status='under_review', lowest confidence first."""
    return conn.execute(
        "SELECT * FROM hearth_principles"
        " WHERE status = 'under_review'"
        " ORDER BY confidence ASC;",
    ).fetchall()


# ---------------------------------------------------------------------------
# Smoke test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    conn = get_principles_connection()
    ensure_principles_table(conn)

    stamp = datetime.now(timezone.utc).isoformat()

    print("Step 1: create test principle (confidence=0.70)")
    test_content = f"[SMOKE TEST {stamp}] Creator voluntary participation principle."
    pid = create_principle(
        conn,
        content=test_content,
        topic_tags="test,smoke",
        source="smoke_test",
        confidence=0.70,
    )
    print(f"  principle_id={pid}")

    print("\nStep 2: mark_principle_used() x3")
    for _ in range(3):
        mark_principle_used(conn, pid)
        row = conn.execute(
            "SELECT confidence FROM hearth_principles WHERE principle_id = ?;", (pid,)
        ).fetchone()
        print(f"  confidence={round(row['confidence'], 4)}")

    print("\nStep 3: flag_principle_for_review(reason='test correction')")
    flag_principle_for_review(conn, pid, reason="test correction")
    row = conn.execute(
        "SELECT confidence, status, contradicted_by FROM hearth_principles WHERE principle_id = ?;",
        (pid,),
    ).fetchone()
    print(f"  new_confidence={round(row['confidence'], 4)}")
    print(f"  status={row['status']}")
    print(f"  contradicted_by={row['contradicted_by']}")

    print("\nStep 4: create replacement principle")
    replacement_content = f"[SMOKE TEST REPLACEMENT {stamp}] Updated participation principle."
    new_pid = create_principle(
        conn,
        content=replacement_content,
        topic_tags="test,smoke",
        source="smoke_test",
        confidence=0.7,
    )
    print(f"  replacement principle_id={new_pid}")

    print("\nStep 5: supersede_principle(old_id, new_id)")
    result = supersede_principle(conn, pid, new_pid)
    row = conn.execute(
        "SELECT status, contradicted_by FROM hearth_principles WHERE principle_id = ?;",
        (pid,),
    ).fetchone()
    print(f"  supersede returned: {result}")
    print(f"  old status={row['status']}")
    print(f"  old contradicted_by={row['contradicted_by']}")
    confirm_exists = conn.execute(
        "SELECT principle_id FROM hearth_principles WHERE principle_id = ?;", (pid,)
    ).fetchone()
    print(f"  old principle still exists: {confirm_exists is not None}")

    print("\nStep 6: get_principles_under_review() — expect empty (old is now superseded)")
    under_review = get_principles_under_review(conn)
    print(f"  count under_review={len(under_review)}")

    print("\nStep 7: list_active_principles() — confirm old NOT in list")
    active = list_active_principles(conn)
    active_ids = [p["principle_id"] for p in active]
    print(f"  old_id={pid} in active list: {pid in active_ids}")

    conn.close()
    print("\nSmoke test complete.")
