"""
Migration: add Hearth worldview tables.

Adds the hearth_worldview_* tables that back Hearth's new worldview layer
(Pulse filters -> Soul interprets -> Worldview updates -> Artifacts surface).
This migration only creates tables/indexes; it does not wire any reader or
writer into the new tables.

Safe to run more than once: every statement uses CREATE TABLE/INDEX IF NOT
EXISTS, and no existing table, column, or row is touched.

Usage:
    python3 migrate_add_hearth_worldview.py
"""

import sqlite3

from hearth_memory import MEMORY_DB_PATH

WORLDVIEW_TABLES = (
    "hearth_worldview_identity",
    "hearth_worldview_beliefs",
    "hearth_worldview_relationships",
    "hearth_worldview_uncertainties",
    "hearth_worldview_changes",
    "hearth_worldview_recent_lessons",
)


def get_connection():
    """Open a read-write connection to Hearth's own memory database."""
    conn = sqlite3.connect(MEMORY_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def migrate(conn):
    """Create the hearth_worldview_* tables and their indexes if missing."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS hearth_worldview_identity (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            identity_key  TEXT    NOT NULL,
            identity_value TEXT   NOT NULL,
            source        TEXT,
            status        TEXT    DEFAULT 'active',
            created_at    TEXT    DEFAULT CURRENT_TIMESTAMP,
            updated_at    TEXT    DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_worldview_identity_status
            ON hearth_worldview_identity (status);
        CREATE INDEX IF NOT EXISTS idx_worldview_identity_key
            ON hearth_worldview_identity (identity_key);
        CREATE INDEX IF NOT EXISTS idx_worldview_identity_created_at
            ON hearth_worldview_identity (created_at);
        CREATE INDEX IF NOT EXISTS idx_worldview_identity_updated_at
            ON hearth_worldview_identity (updated_at);

        CREATE TABLE IF NOT EXISTS hearth_worldview_beliefs (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            subject_type      TEXT,
            subject_id        TEXT,
            belief_type       TEXT,
            belief_text       TEXT    NOT NULL,
            confidence        REAL    DEFAULT 0.5,
            status            TEXT    DEFAULT 'active',
            created_at        TEXT    DEFAULT CURRENT_TIMESTAMP,
            updated_at        TEXT    DEFAULT CURRENT_TIMESTAMP,
            last_confirmed_at TEXT,
            last_challenged_at TEXT,
            source_episode_id TEXT,
            source_signal_id TEXT,
            notes             TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_worldview_beliefs_status
            ON hearth_worldview_beliefs (status);
        CREATE INDEX IF NOT EXISTS idx_worldview_beliefs_subject
            ON hearth_worldview_beliefs (subject_type, subject_id);
        CREATE INDEX IF NOT EXISTS idx_worldview_beliefs_created_at
            ON hearth_worldview_beliefs (created_at);
        CREATE INDEX IF NOT EXISTS idx_worldview_beliefs_updated_at
            ON hearth_worldview_beliefs (updated_at);
        CREATE INDEX IF NOT EXISTS idx_worldview_beliefs_confidence
            ON hearth_worldview_beliefs (confidence);
        CREATE INDEX IF NOT EXISTS idx_worldview_beliefs_source_episode
            ON hearth_worldview_beliefs (source_episode_id);
        CREATE INDEX IF NOT EXISTS idx_worldview_beliefs_source_signal
            ON hearth_worldview_beliefs (source_signal_id);

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

        CREATE INDEX IF NOT EXISTS idx_worldview_relationships_status
            ON hearth_worldview_relationships (status);
        CREATE INDEX IF NOT EXISTS idx_worldview_relationships_entity_a
            ON hearth_worldview_relationships (entity_a_type, entity_a_id);
        CREATE INDEX IF NOT EXISTS idx_worldview_relationships_entity_b
            ON hearth_worldview_relationships (entity_b_type, entity_b_id);
        CREATE INDEX IF NOT EXISTS idx_worldview_relationships_created_at
            ON hearth_worldview_relationships (created_at);
        CREATE INDEX IF NOT EXISTS idx_worldview_relationships_updated_at
            ON hearth_worldview_relationships (updated_at);
        CREATE INDEX IF NOT EXISTS idx_worldview_relationships_confidence
            ON hearth_worldview_relationships (confidence);
        CREATE INDEX IF NOT EXISTS idx_worldview_relationships_source_episode
            ON hearth_worldview_relationships (source_episode_id);
        CREATE INDEX IF NOT EXISTS idx_worldview_relationships_source_signal
            ON hearth_worldview_relationships (source_signal_id);

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
            resolved_at       TEXT,
            source_episode_id TEXT,
            source_signal_id TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_worldview_uncertainties_status
            ON hearth_worldview_uncertainties (status);
        CREATE INDEX IF NOT EXISTS idx_worldview_uncertainties_subject
            ON hearth_worldview_uncertainties (subject_type, subject_id);
        CREATE INDEX IF NOT EXISTS idx_worldview_uncertainties_created_at
            ON hearth_worldview_uncertainties (created_at);
        CREATE INDEX IF NOT EXISTS idx_worldview_uncertainties_updated_at
            ON hearth_worldview_uncertainties (updated_at);
        CREATE INDEX IF NOT EXISTS idx_worldview_uncertainties_confidence
            ON hearth_worldview_uncertainties (confidence);
        CREATE INDEX IF NOT EXISTS idx_worldview_uncertainties_source_episode
            ON hearth_worldview_uncertainties (source_episode_id);
        CREATE INDEX IF NOT EXISTS idx_worldview_uncertainties_source_signal
            ON hearth_worldview_uncertainties (source_signal_id);

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
            source_signal_id TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_worldview_changes_status
            ON hearth_worldview_changes (status);
        CREATE INDEX IF NOT EXISTS idx_worldview_changes_subject
            ON hearth_worldview_changes (subject_type, subject_id);
        CREATE INDEX IF NOT EXISTS idx_worldview_changes_created_at
            ON hearth_worldview_changes (created_at);
        CREATE INDEX IF NOT EXISTS idx_worldview_changes_updated_at
            ON hearth_worldview_changes (updated_at);
        CREATE INDEX IF NOT EXISTS idx_worldview_changes_confidence
            ON hearth_worldview_changes (confidence);
        CREATE INDEX IF NOT EXISTS idx_worldview_changes_source_episode
            ON hearth_worldview_changes (source_episode_id);
        CREATE INDEX IF NOT EXISTS idx_worldview_changes_source_signal
            ON hearth_worldview_changes (source_signal_id);

        CREATE TABLE IF NOT EXISTS hearth_worldview_recent_lessons (
            id                       INTEGER PRIMARY KEY AUTOINCREMENT,
            lesson_text              TEXT    NOT NULL,
            topic_tags               TEXT,
            confidence               REAL    DEFAULT 0.5,
            status                   TEXT    DEFAULT 'provisional',
            created_at               TEXT    DEFAULT CURRENT_TIMESTAMP,
            updated_at               TEXT    DEFAULT CURRENT_TIMESTAMP,
            last_confirmed_at        TEXT,
            times_confirmed          INTEGER DEFAULT 0,
            times_challenged         INTEGER DEFAULT 0,
            candidate_for_principle  INTEGER DEFAULT 0,
            source_episode_id        TEXT,
            source_signal_id         TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_worldview_lessons_status
            ON hearth_worldview_recent_lessons (status);
        CREATE INDEX IF NOT EXISTS idx_worldview_lessons_created_at
            ON hearth_worldview_recent_lessons (created_at);
        CREATE INDEX IF NOT EXISTS idx_worldview_lessons_updated_at
            ON hearth_worldview_recent_lessons (updated_at);
        CREATE INDEX IF NOT EXISTS idx_worldview_lessons_confidence
            ON hearth_worldview_recent_lessons (confidence);
        CREATE INDEX IF NOT EXISTS idx_worldview_lessons_source_episode
            ON hearth_worldview_recent_lessons (source_episode_id);
        CREATE INDEX IF NOT EXISTS idx_worldview_lessons_source_signal
            ON hearth_worldview_recent_lessons (source_signal_id);
    """)
    conn.commit()


def main():
    print(f"[MIGRATION] target database: {MEMORY_DB_PATH}")
    conn = get_connection()
    try:
        migrate(conn)
        existing = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE 'hearth_worldview_%';"
            )
        }
        for table in WORLDVIEW_TABLES:
            status = "OK" if table in existing else "MISSING"
            print(f"[MIGRATION] {table}: {status}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
