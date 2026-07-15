"""
Shared fixture helpers for the test_experience_evaluator_*.py suite.

Not itself a test — does not start with "test_" so it is never swept up by
tooling that runs all test_*.py files directly. Each of the six suite files
uses these helpers to build fully isolated, temp-file SQLite databases (one
Pathway-shaped, one hearth_memory-shaped) so none of them touch the real
dev hearth_memory.db / app.db. Mirrors the schema-bootstrapping every other
test_*.py in this repo hand-rolls inline, but factored out once since six
files need the identical ~80 lines of setup.
"""

import os
import sqlite3
import tempfile
from datetime import datetime, timezone

import hearth_memory
import hearth_relationships
import hearth_worldview
import migrate_add_experience_evaluator_governance as gov_migration
import migrate_add_six_room_schema as six_room_migration


def make_memory_db():
    """Create a fresh, fully-migrated hearth_memory-shaped sqlite db in a
    temp file. Returns (conn, path). Caller is responsible for closing conn
    and removing path (see cleanup_db).
    """
    fd, path = tempfile.mkstemp(suffix="_hearth_memory_test.db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON;")
    hearth_memory.init_tables(conn)
    hearth_worldview.ensure_worldview_tables(conn)
    hearth_relationships.init_relationship_tables(conn)
    six_room_migration.create_new_tables(conn)
    gov_migration.create_tables(conn)
    gov_migration.create_dedup_hardening_index(conn)
    return conn, path


def make_pathway_db():
    """Create a minimal hearth_events-only sqlite db (mirrors the real
    Pathway app.db schema for just this table) in a temp file. Returns
    (conn, path).
    """
    fd, path = tempfile.mkstemp(suffix="_pathway_events_test.db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE hearth_events (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type        TEXT NOT NULL,
            actor_user_id     INTEGER,
            target_user_id    INTEGER,
            reference_id      INTEGER,
            reference_type    TEXT,
            occurred_at       TEXT NOT NULL,
            processed         INTEGER NOT NULL DEFAULT 0,
            experience_level  TEXT NOT NULL DEFAULT 'trace',
            importance_score  REAL,
            importance_reason TEXT
        );
        """
    )
    conn.commit()
    return conn, path


def cleanup_db(conn, path):
    try:
        conn.close()
    except Exception:
        pass
    for suffix in ("", "-wal", "-shm"):
        candidate = path + suffix
        if os.path.exists(candidate):
            os.remove(candidate)


def insert_event(conn, event_type, actor_user_id=None, target_user_id=None,
                  reference_id=None, reference_type=None, occurred_at=None,
                  processed=1, experience_level="signal"):
    """Insert one hearth_events row and return its id."""
    if occurred_at is None:
        occurred_at = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        "INSERT INTO hearth_events"
        " (event_type, actor_user_id, target_user_id, reference_id, reference_type,"
        "  occurred_at, processed, experience_level)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?);",
        (event_type, actor_user_id, target_user_id, reference_id, reference_type,
         occurred_at, processed, experience_level),
    )
    conn.commit()
    return cur.lastrowid


def make_entity(memory_conn, user_id, display_name="Test Creator"):
    """Create (or fetch) a hearth_entities row and set its display_name."""
    entity = hearth_memory.get_or_create_entity(memory_conn, user_id)
    memory_conn.execute(
        "UPDATE hearth_entities SET display_name = ? WHERE id = ?;",
        (display_name, entity["id"]),
    )
    memory_conn.commit()
    return entity["id"]


def make_watcher_episode(memory_conn, entity_id, episode_type, reference_key=None,
                          description="synthetic watcher episode", severity="medium"):
    """Create an original (non-evaluator) watcher episode, as morning_briefing.py's
    watchers do — e.g. checkin_feedback_waiting, training_comment_waiting,
    creator_quiet. Returns the episode id.
    """
    episode_id, _action = hearth_memory.create_episode(
        memory_conn, entity_id, episode_type, description,
        severity=severity, reference_key=reference_key,
    )
    return episode_id


def make_waiting_target(memory_conn, entity_id, episode_type, source_episode_id=None):
    """Create a hearth_soul-shaped 'entity_episode' living uncertainty for
    {episode_type}:{entity_id}, as _upsert_single_episode_uncertainty does.
    Pass source_episode_id=<a real make_watcher_episode() id> for a
    resolvable target, or omit for a legacy-shaped (unresolvable) one.
    Returns the uncertainty id.
    """
    subject_id = f"{episode_type}:{entity_id}"
    uid, _created = hearth_worldview.upsert_uncertainty(
        memory_conn, subject_type="entity_episode", subject_id=subject_id,
        uncertainty_text=f"synthetic {episode_type} uncertainty for entity {entity_id}",
        source_episode_id=source_episode_id,
    )
    return uid


def make_quiet_change_target(memory_conn, entity_id, source_episode_id=None):
    """Create a hearth_soul-shaped 'creator_quiet_entity' watched change, as
    _upsert_creator_quiet_watch does. Returns the change id.
    """
    return hearth_worldview.record_change(
        memory_conn, subject_type="creator_quiet_entity", subject_id=str(entity_id),
        change_text=f"synthetic creator_quiet change for entity {entity_id}",
        source_episode_id=source_episode_id,
    )


def set_uncertainty_status(memory_conn, uncertainty_id, status):
    hearth_worldview.update_uncertainty(memory_conn, uncertainty_id, status=status)
    memory_conn.commit()
