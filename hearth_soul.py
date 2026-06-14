"""
Hearth Soul — reflective state recorder for the Hearth AI teammate.

Records operational observations after each pipeline run. Entries are factual
and short — a black-box log, not a journal.
"""

import sqlite3
from datetime import datetime, timezone

import hearth_questions
from hearth_memory import MEMORY_DB_PATH


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

def get_reflections_connection():
    """Open a read-write connection to the shared Hearth memory database."""
    conn = sqlite3.connect(MEMORY_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def ensure_reflections_table(conn):
    """Create hearth_reflections if it does not exist. Safe on existing databases."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS hearth_reflections (
            reflection_id              INTEGER PRIMARY KEY AUTOINCREMENT,
            reflection_time            TEXT    NOT NULL,
            what_changed               TEXT,
            what_surprised_me          TEXT,
            what_i_am_uncertain_about  TEXT,
            what_i_should_ask          TEXT,
            source_run                 TEXT
        );
    """)
    conn.commit()


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

def create_reflection(conn, what_changed="", what_surprised_me="",
                      what_i_am_uncertain_about="", what_i_should_ask="",
                      source_run=None):
    """Insert one reflection row and return its reflection_id."""
    now = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        "INSERT INTO hearth_reflections"
        " (reflection_time, what_changed, what_surprised_me,"
        "  what_i_am_uncertain_about, what_i_should_ask, source_run)"
        " VALUES (?, ?, ?, ?, ?, ?);",
        (
            now,
            what_changed or None,
            what_surprised_me or None,
            what_i_am_uncertain_about or None,
            what_i_should_ask or None,
            source_run,
        ),
    )
    conn.commit()
    return cur.lastrowid


def get_recent_reflections(conn, limit=20):
    """Return up to limit reflections ordered by reflection_time DESC."""
    return conn.execute(
        "SELECT * FROM hearth_reflections"
        " ORDER BY reflection_time DESC"
        " LIMIT ?;",
        (limit,),
    ).fetchall()


def get_latest_reflection(conn):
    """Return the most recent reflection row, or None if the table is empty."""
    return conn.execute(
        "SELECT * FROM hearth_reflections"
        " ORDER BY reflection_time DESC"
        " LIMIT 1;",
    ).fetchone()


def summarize_recent_reflections(conn, limit=10):
    """
    Return a short plain-text summary of recent reflections (3-6 bullet lines).
    No AI. Pure string assembly from stored fields. Returns empty string if
    no reflections exist.
    """
    rows = get_recent_reflections(conn, limit=limit)
    if not rows:
        return ""

    seen = set()
    lines = []
    fields = ("what_changed", "what_surprised_me", "what_i_am_uncertain_about")
    for row in rows:
        for field in fields:
            val = row[field]
            if val and val.strip() and val not in seen:
                seen.add(val)
                lines.append(f"- {val.strip()}")
            if len(lines) >= 6:
                break
        if len(lines) >= 6:
            break

    if not lines:
        return ""
    return "Recent Hearth observations:\n" + "\n".join(lines)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _episode_entity(ep):
    """Extract an entity identifier from an episode dict or Row."""
    for key in ("entity_id", "entity", "display_name"):
        try:
            val = ep[key]
            if val is not None:
                return val
        except (KeyError, IndexError, TypeError):
            pass
    return None


def _episode_type(ep):
    """Extract an episode type string from an episode dict or Row."""
    for key in ("episode_type", "type"):
        try:
            val = ep[key]
            if val is not None:
                return str(val)
        except (KeyError, IndexError, TypeError):
            pass
    return None


def _list_len(val):
    """Return len if val is a list, treat as integer count otherwise."""
    if val is None:
        return 0
    if isinstance(val, list):
        return len(val)
    try:
        return int(val)
    except (TypeError, ValueError):
        return 0


# ---------------------------------------------------------------------------
# Reflection generator
# ---------------------------------------------------------------------------

def generate_reflection(conn, new_episodes=None, resolved_episodes=None,
                        open_concerns=None, open_questions=None, source_run=None,
                        auto_question=True):
    """
    Derive a reflection from pipeline run data and persist it.

    Episode parameters accept lists of dicts or sqlite3.Row objects.
    Concern and question parameters accept lists or integer counts.
    Returns the reflection_id of the created row.

    auto_question: if True (default) and what_i_should_ask is non-empty, a
    question is created via hearth_questions so it surfaces for human review.
    Pass False to store the reflection without touching hearth_questions.
    """
    # --- what_changed ---
    parts = []
    if new_episodes:
        parts.append(f"{len(new_episodes)} new episode(s) recorded.")
    if resolved_episodes:
        parts.append(f"{len(resolved_episodes)} episode(s) resolved.")
    what_changed = " ".join(parts) if parts else "No episode changes detected."

    # --- what_surprised_me ---
    surprises = []
    if new_episodes:
        entity_counts = {}
        type_counts = {}
        for ep in new_episodes:
            e = _episode_entity(ep)
            if e is not None:
                entity_counts[e] = entity_counts.get(e, 0) + 1
            t = _episode_type(ep)
            if t is not None:
                type_counts[t] = type_counts.get(t, 0) + 1

        if any(c >= 2 for c in entity_counts.values()):
            surprises.append("Multiple new concerns detected under the same coach.")

        for ep_type, count in type_counts.items():
            if count >= 3:
                surprises.append(
                    f"Episode type '{ep_type}' appeared {count} times in this run."
                )
                break

    what_surprised_me = " ".join(surprises)

    # --- what_i_am_uncertain_about ---
    n_questions = _list_len(open_questions)
    n_concerns = _list_len(open_concerns)
    uncertainties = []
    if n_questions > 0:
        uncertainties.append(f"{n_questions} open question(s) remain unanswered.")
    if n_concerns > 5:
        uncertainties.append(
            "High concern volume may indicate systemic issue or data artifact."
        )
    what_i_am_uncertain_about = " ".join(uncertainties)

    # --- what_i_should_ask ---
    if "High concern volume" in what_i_am_uncertain_about:
        what_i_should_ask = (
            "Is the current concern volume expected or does it indicate a detection error?"
        )
    elif n_questions > 0:
        what_i_should_ask = (
            f"Are the {n_questions} open question(s) under active review?"
        )
    else:
        what_i_should_ask = ""

    # --- persist ---
    reflection_id = create_reflection(
        conn,
        what_changed=what_changed,
        what_surprised_me=what_surprised_me,
        what_i_am_uncertain_about=what_i_am_uncertain_about,
        what_i_should_ask=what_i_should_ask,
        source_run=source_run,
    )

    if auto_question and what_i_should_ask:
        hearth_questions.create_question(
            conn,
            question_text=what_i_should_ask,
            topic_tags="soul_reflection,auto_generated",
            triggered_by="hearth_soul",
        )

    return reflection_id


# ---------------------------------------------------------------------------
# Smoke test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    conn = get_reflections_connection()

    print("Step 1: ensure_reflections_table()")
    ensure_reflections_table(conn)
    hearth_questions.ensure_questions_table(conn)
    print("  Tables ready.")

    print("\nStep 2: generate_reflection() with sample data")
    new_eps = [
        {"episode_type": "creator_quiet", "entity": "userA"},
        {"episode_type": "creator_quiet", "entity": "userB"},
        {"episode_type": "creator_quiet", "entity": "userC"},
        {"episode_type": "training_comment_waiting", "entity": "userD"},
    ]
    resolved_eps = [
        {"episode_type": "checkin_feedback_waiting", "entity": "userE"},
    ]
    open_concerns = [{}] * 7
    open_questions_sample = [{}] * 2

    rid = generate_reflection(
        conn,
        new_episodes=new_eps,
        resolved_episodes=resolved_eps,
        open_concerns=open_concerns,
        open_questions=open_questions_sample,
        source_run="smoke_test",
    )
    print(f"  reflection_id={rid}")

    print("\nStep 3: get_latest_reflection()")
    row = get_latest_reflection(conn)
    if row:
        print(f"  reflection_id:             {row['reflection_id']}")
        print(f"  reflection_time:           {row['reflection_time']}")
        print(f"  what_changed:              {row['what_changed']}")
        print(f"  what_surprised_me:         {row['what_surprised_me']}")
        print(f"  what_i_am_uncertain_about: {row['what_i_am_uncertain_about']}")
        print(f"  what_i_should_ask:         {row['what_i_should_ask']}")
        print(f"  source_run:                {row['source_run']}")

    print("\nStep 4: summarize_recent_reflections()")
    summary = summarize_recent_reflections(conn, limit=10)
    print(summary if summary else "  (no summary)")

    conn.close()
    print("\nSmoke test complete.")
