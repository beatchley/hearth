"""
Hearth Questions — open question tracker for the Hearth AI teammate.

Stores questions Hearth has surfaced about creator behavior, Pathway norms, or
its own judgment calls. Questions are distinct from principles (settled beliefs)
and episodes (observed events). They represent things Hearth is still uncertain
about and wants to surface for human review.
"""

import sqlite3
from datetime import datetime, timezone

from hearth_memory import MEMORY_DB_PATH

_VALID_STATUSES = {"open", "answered", "dismissed"}


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

def get_questions_connection():
    """Open a read-write connection to the shared Hearth memory database."""
    conn = sqlite3.connect(MEMORY_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def ensure_questions_table(conn):
    """Create hearth_questions if it doesn't exist. Safe to run on existing databases."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS hearth_questions (
            question_id   INTEGER PRIMARY KEY AUTOINCREMENT,
            question_text TEXT    NOT NULL,
            topic_tags    TEXT,
            triggered_by  TEXT,
            status        TEXT    DEFAULT 'open',
            created_at    TEXT    NOT NULL,
            answered_at   TEXT
        );
    """)
    conn.commit()


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

def create_question(conn, question_text, topic_tags=None, triggered_by=None):
    """Insert a new question and return its question_id.

    No-ops if an open question with identical question_text already exists,
    returning the existing id instead. topic_tags should be a comma-separated
    string or None.
    """
    existing = conn.execute(
        "SELECT question_id FROM hearth_questions"
        " WHERE question_text = ? AND status = 'open';",
        (question_text,),
    ).fetchone()
    if existing:
        return existing["question_id"]

    now = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        "INSERT INTO hearth_questions"
        " (question_text, topic_tags, triggered_by, status, created_at)"
        " VALUES (?, ?, ?, 'open', ?);",
        (question_text, topic_tags, triggered_by, now),
    )
    conn.commit()
    return cur.lastrowid


def list_open_questions(conn, limit=20):
    """Return open questions ordered by created_at ASC (oldest first)."""
    return conn.execute(
        "SELECT * FROM hearth_questions"
        " WHERE status = 'open'"
        " ORDER BY created_at ASC"
        " LIMIT ?;",
        (limit,),
    ).fetchall()


def get_question(conn, question_id):
    """Return one question row by question_id, or None if not found."""
    return conn.execute(
        "SELECT * FROM hearth_questions WHERE question_id = ?;",
        (question_id,),
    ).fetchone()


def mark_question_answered(conn, question_id):
    """Set status to 'answered' and record answered_at. Only acts on open questions."""
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "UPDATE hearth_questions"
        " SET status = 'answered', answered_at = ?"
        " WHERE question_id = ? AND status = 'open';",
        (now, question_id),
    )
    conn.commit()


def dismiss_question(conn, question_id):
    """Set status to 'dismissed'. Only acts on open questions."""
    conn.execute(
        "UPDATE hearth_questions SET status = 'dismissed'"
        " WHERE question_id = ? AND status = 'open';",
        (question_id,),
    )
    conn.commit()


def list_questions_by_tag(conn, tag, status="open"):
    """Return questions whose topic_tags contain tag (case-insensitive), filtered by status.

    Raises ValueError if status is not a recognized value.
    Ordered by created_at ASC.
    """
    if status not in _VALID_STATUSES:
        raise ValueError(
            f"Invalid status '{status}'. Must be one of: {', '.join(sorted(_VALID_STATUSES))}"
        )
    pattern = f"%{tag.lower()}%"
    return conn.execute(
        "SELECT * FROM hearth_questions"
        " WHERE status = ?"
        "   AND LOWER(topic_tags) LIKE ?"
        " ORDER BY created_at ASC;",
        (status, pattern),
    ).fetchall()


# ---------------------------------------------------------------------------
# Smoke test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    conn = get_questions_connection()

    print("Step 1: ensure_questions_table()")
    ensure_questions_table(conn)
    print("  Table ready.")

    print("\nStep 2: create_question() — seed question")
    seed_text = (
        "Should Hearth treat repeated skipped check-ins as meaningful inactivity,"
        " or only as weak signal?"
    )
    qid = create_question(
        conn,
        question_text=seed_text,
        topic_tags="checkins,creator_activity,judgment",
        triggered_by="step_3_smoke_test",
    )
    print(f"  question_id={qid}")

    print("\nStep 3: list_open_questions()")
    questions = list_open_questions(conn)
    print(f"  {len(questions)} open question(s)")
    for q in questions:
        print(f"  [{q['question_id']}] tags={q['topic_tags']}")
        print(f"       {q['question_text'][:80]}{'...' if len(q['question_text']) > 80 else ''}")

    print("\nStep 4: list_questions_by_tag('judgment')")
    tagged = list_questions_by_tag(conn, "judgment")
    print(f"  {len(tagged)} result(s)")
    for q in tagged:
        print(f"  [{q['question_id']}] {q['question_text'][:80]}")

    print("\nStep 5: get_question()")
    row = get_question(conn, qid)
    if row:
        print(f"  question_id={row['question_id']} status={row['status']}")
        print(f"  triggered_by={row['triggered_by']}")
        print(f"  created_at={row['created_at']}")

    print("\nStep 6: dismiss_question()")
    dismiss_question(conn, qid)
    dismissed = get_question(conn, qid)
    print(f"  status after dismiss: {dismissed['status']}")

    print("\nStep 7: confirm not in list_open_questions()")
    open_after = list_open_questions(conn)
    ids_open = [q["question_id"] for q in open_after]
    if qid not in ids_open:
        print(f"  Confirmed: question_id={qid} is no longer open.")
    else:
        print(f"  ERROR: question_id={qid} still appears as open.")

    conn.close()
    print("\nSmoke test complete.")
