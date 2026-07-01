"""
One-time maintenance: classify legacy training_comments rows written before
the comment classifier existed (comment_type IS NULL), and resolve any open
training_comment_waiting episodes that turn out to be false positives once
classified.

Part A — backfill_null_comment_types():
    Classifies every training_comments row with comment_type IS NULL using
    the same classify_training_comment() used at write time, and stores the
    result. Never touches rows that already have a comment_type.

Part B — resolve_stale_training_comment_waiting_episodes():
    Finds open training_comment_waiting Hearth episodes whose underlying
    comment is classified as non-actionable (anything other than
    ACTIONABLE_COMMENT_TYPES — including comments this script just
    backfilled) and resolves them. Episodes are never deleted, only marked
    resolved with a note appended to their description.

Safe to run more than once — both parts only touch rows/episodes that still
match their respective conditions.

Usage:
    python backfill_training_comment_classification.py
"""

import os
import sqlite3

from dotenv import load_dotenv

import hearth_memory
from hearth_comment_classifier import classify_training_comment
from morning_briefing import ACTIONABLE_COMMENT_TYPES

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")

RESOLUTION_REASON = "resolved_by_comment_classification_backfill"


def _resolve_pathway_db_path(database_url):
    if not database_url:
        raise SystemExit("ERROR: DATABASE_URL is missing. Add it to your .env file.")
    if database_url.startswith("sqlite:///"):
        return database_url[len("sqlite:///"):]
    return database_url


def _get_pathway_connection(db_path=None):
    """Read-write connection to the Pathway Portal database (training_comments lives here)."""
    if db_path is None:
        db_path = _resolve_pathway_db_path(DATABASE_URL)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# Part A — backfill legacy NULL comment_type rows
# ---------------------------------------------------------------------------

def backfill_null_comment_types(pathway_conn):
    """Classify every training_comments row with comment_type IS NULL.

    Returns the number of rows classified.
    """
    rows = pathway_conn.execute(
        "SELECT id, content FROM training_comments WHERE comment_type IS NULL ORDER BY id;"
    ).fetchall()

    print(f"[BACKFILL] {len(rows)} legacy comment(s) with comment_type IS NULL.")

    for row in rows:
        category, confidence = classify_training_comment(row["content"])
        pathway_conn.execute(
            "UPDATE training_comments SET comment_type = ?, comment_type_confidence = ?"
            " WHERE id = ? AND comment_type IS NULL;",
            (category, confidence, row["id"]),
        )
        pathway_conn.commit()
        print(
            f"[BACKFILL] comment_id={row['id']} classified"
            f" category={category} confidence={confidence}"
        )

    print(f"[BACKFILL] Done. {len(rows)} comment(s) classified.")
    return len(rows)


# ---------------------------------------------------------------------------
# Part B — resolve stale training_comment_waiting false positives
# ---------------------------------------------------------------------------

def resolve_stale_training_comment_waiting_episodes(memory_conn, pathway_conn):
    """Resolve open training_comment_waiting episodes whose comment is now
    classified as non-actionable (not in ACTIONABLE_COMMENT_TYPES).

    Covers both comments this run just backfilled and any comment that was
    already classified non-actionable before the watcher filtered on
    comment_type. Episodes are marked resolved, never deleted; a note is
    appended to the description so the reason is visible on the record.

    Returns the number of episodes resolved.
    """
    open_episodes = memory_conn.execute(
        "SELECT id, reference_key, description FROM hearth_episodes"
        " WHERE episode_type = 'training_comment_waiting' AND resolved = 0;"
    ).fetchall()

    resolved_count = 0
    for ep in open_episodes:
        ref_key = ep["reference_key"] or ""
        prefix = "training_comment_waiting_"
        if not ref_key.startswith(prefix):
            continue
        try:
            comment_id = int(ref_key[len(prefix):])
        except ValueError:
            continue

        comment = pathway_conn.execute(
            "SELECT comment_type FROM training_comments WHERE id = ?;", (comment_id,)
        ).fetchone()

        if comment is None:
            continue  # comment deleted — resolve_stale_issues already handles this case
        if comment["comment_type"] in ACTIONABLE_COMMENT_TYPES:
            continue  # still a legitimate concern — leave open

        new_description = f"{ep['description']} [{RESOLUTION_REASON}: comment_type={comment['comment_type']}]"
        hearth_memory.refresh_episode(memory_conn, ep["id"], new_description)
        hearth_memory.resolve_episode(memory_conn, ep["id"])
        resolved_count += 1
        print(
            f"[CLEANUP] episode_id={ep['id']} comment_id={comment_id}"
            f" comment_type={comment['comment_type']} -> resolved ({RESOLUTION_REASON})"
        )

    print(f"[CLEANUP] Done. {resolved_count} stale training_comment_waiting episode(s) resolved.")
    return resolved_count


def run():
    pathway_conn = _get_pathway_connection()
    memory_conn = hearth_memory.get_memory_connection()
    try:
        hearth_memory.init_tables(memory_conn)
        backfill_null_comment_types(pathway_conn)
        resolve_stale_training_comment_waiting_episodes(memory_conn, pathway_conn)
    finally:
        pathway_conn.close()
        memory_conn.close()


if __name__ == "__main__":
    run()
    print("Backfill complete.")
