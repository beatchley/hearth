"""
Migration: Hearth six-room schema foundation (Identity, Furniture, State,
Relationships/Roads, Experience, Reflection).

Schema-only, purely additive. This migration:
  - adds new columns to hearth_entities (Identity room)
  - adds new columns to hearth_relationships (Relationships/Roads room)
  - creates new empty tables for Furniture, State, Relationship Events,
    and Reflection refs

It does not add extraction, traversal, autonomous entity creation, or any
write logic. hearth_episodes (Experience room) is untouched. No existing
column is removed or renamed, and no existing row's existing columns are
modified.

Safe to run more than once: ALTER TABLE ADD COLUMN calls are guarded
against "duplicate column name" errors, and all CREATE TABLE/INDEX
statements use IF NOT EXISTS.

Usage:
    python3 migrate_add_six_room_schema.py
"""

import sqlite3

from hearth_memory import MEMORY_DB_PATH

NEW_TABLES = (
    "hearth_entity_furniture",
    "hearth_entity_state",
    "hearth_entity_state_history",
    "hearth_relationship_events",
    "hearth_entity_reflection_refs",
)

ENTITIES_NEW_COLUMNS = (
    ("entity_type", "TEXT DEFAULT 'person'"),
    ("source", "TEXT DEFAULT 'pathway_sync'"),
    ("canonical_key", "TEXT"),
    ("aliases", "TEXT"),
)

RELATIONSHIPS_NEW_COLUMNS = (
    ("origin", "TEXT DEFAULT 'structural'"),
    ("status", "TEXT DEFAULT 'active'"),
    ("activates_at", "TEXT"),
    ("expires_at", "TEXT"),
    ("transitioned_at", "TEXT"),
    ("transition_reason", "TEXT"),
)


def get_connection():
    """Open a read-write connection to Hearth's own memory database."""
    conn = sqlite3.connect(MEMORY_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def _add_column_if_missing(conn, table, column, coltype_and_default):
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype_and_default};")
        conn.commit()
        print(f"{table}: added {column} column.")
    except sqlite3.OperationalError as exc:
        if "duplicate column name" in str(exc).lower():
            print(f"{table}: {column} already exists — skipped.")
        else:
            raise


def migrate_entities(conn):
    """Identity room: additive columns on hearth_entities."""
    for column, coltype_and_default in ENTITIES_NEW_COLUMNS:
        _add_column_if_missing(conn, "hearth_entities", column, coltype_and_default)

    # Backfill canonical_key for existing person rows, following the
    # 'user:{user_id}' pattern that future non-person entities will extend
    # (e.g. 'project:goose', 'event:pathway_unmuted').
    conn.execute(
        "UPDATE hearth_entities"
        " SET canonical_key = 'user:' || user_id"
        " WHERE canonical_key IS NULL AND user_id IS NOT NULL;"
    )
    conn.commit()

    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_hearth_entities_canonical_key"
        " ON hearth_entities (canonical_key);"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_hearth_entities_entity_type"
        " ON hearth_entities (entity_type);"
    )
    conn.commit()


def migrate_relationships(conn):
    """Relationships/Roads room: additive columns on hearth_relationships."""
    for column, coltype_and_default in RELATIONSHIPS_NEW_COLUMNS:
        _add_column_if_missing(conn, "hearth_relationships", column, coltype_and_default)

    # Backfill any rows the column-level DEFAULT didn't already cover
    # (e.g. columns added by a prior partial run of this migration).
    conn.execute(
        "UPDATE hearth_relationships SET origin = 'structural' WHERE origin IS NULL;"
    )
    conn.execute(
        "UPDATE hearth_relationships SET status = 'active' WHERE status IS NULL AND active = 1;"
    )
    conn.commit()


def create_new_tables(conn):
    """Furniture, State, Relationship Events, and Reflection ref tables."""
    conn.executescript(
        """
        -- Furniture room: durable facts about an entity.
        CREATE TABLE IF NOT EXISTS hearth_entity_furniture (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            entity_id          INTEGER NOT NULL REFERENCES hearth_entities(id),
            fact_text          TEXT    NOT NULL,
            fact_type          TEXT,
            source             TEXT,
            confidence         REAL    DEFAULT 0.5,
            first_observed_at  TEXT,
            last_confirmed_at  TEXT,
            status             TEXT    DEFAULT 'active',
            created_at         TEXT    DEFAULT CURRENT_TIMESTAMP,
            updated_at         TEXT    DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_entity_furniture_entity
            ON hearth_entity_furniture (entity_id);
        CREATE INDEX IF NOT EXISTS idx_entity_furniture_fact_type
            ON hearth_entity_furniture (fact_type);
        CREATE INDEX IF NOT EXISTS idx_entity_furniture_status
            ON hearth_entity_furniture (status);

        -- State room: current value only, one row per entity_id + state_key.
        CREATE TABLE IF NOT EXISTS hearth_entity_state (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            entity_id     INTEGER NOT NULL REFERENCES hearth_entities(id),
            state_key     TEXT    NOT NULL,
            state_value   TEXT,
            source        TEXT,
            confidence    REAL    DEFAULT 0.5,
            set_at        TEXT    DEFAULT CURRENT_TIMESTAMP,
            updated_at    TEXT    DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (entity_id, state_key)
        );

        CREATE INDEX IF NOT EXISTS idx_entity_state_entity
            ON hearth_entity_state (entity_id);

        -- State room: append-only transition history.
        CREATE TABLE IF NOT EXISTS hearth_entity_state_history (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            entity_id      INTEGER NOT NULL REFERENCES hearth_entities(id),
            state_key      TEXT    NOT NULL,
            old_value      TEXT,
            new_value      TEXT,
            source         TEXT,
            confidence     REAL,
            changed_at     TEXT    DEFAULT CURRENT_TIMESTAMP,
            change_reason  TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_entity_state_history_entity_key
            ON hearth_entity_state_history (entity_id, state_key);
        CREATE INDEX IF NOT EXISTS idx_entity_state_history_changed_at
            ON hearth_entity_state_history (changed_at);

        -- Relationships/Roads room: append-only evidence/history log.
        CREATE TABLE IF NOT EXISTS hearth_relationship_events (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            relationship_id    INTEGER REFERENCES hearth_relationships(id),
            entity_id_1        INTEGER REFERENCES hearth_entities(id),
            entity_id_2        INTEGER REFERENCES hearth_entities(id),
            relationship_type  TEXT,
            event_type         TEXT,
            source             TEXT,
            source_table       TEXT,
            source_record_id   TEXT,
            confidence         REAL,
            observed_at        TEXT,
            notes              TEXT,
            created_at         TEXT    DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_relationship_events_relationship
            ON hearth_relationship_events (relationship_id);
        CREATE INDEX IF NOT EXISTS idx_relationship_events_entities
            ON hearth_relationship_events (entity_id_1, entity_id_2);
        CREATE INDEX IF NOT EXISTS idx_relationship_events_event_type
            ON hearth_relationship_events (event_type);
        CREATE INDEX IF NOT EXISTS idx_relationship_events_observed_at
            ON hearth_relationship_events (observed_at);

        -- Reflection room: lightweight lookup/index into existing worldview
        -- and question records. Does not duplicate their content.
        CREATE TABLE IF NOT EXISTS hearth_entity_reflection_refs (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            entity_id         INTEGER NOT NULL REFERENCES hearth_entities(id),
            reflection_type   TEXT    NOT NULL,
            reflection_id     INTEGER NOT NULL,
            source            TEXT,
            confidence        REAL,
            created_at        TEXT    DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (entity_id, reflection_type, reflection_id)
        );

        CREATE INDEX IF NOT EXISTS idx_entity_reflection_refs_entity
            ON hearth_entity_reflection_refs (entity_id);
        CREATE INDEX IF NOT EXISTS idx_entity_reflection_refs_type
            ON hearth_entity_reflection_refs (reflection_type);
        """
    )
    conn.commit()


def migrate(conn):
    migrate_entities(conn)
    migrate_relationships(conn)
    create_new_tables(conn)


def main():
    print(f"[MIGRATION] target database: {MEMORY_DB_PATH}")
    conn = get_connection()
    try:
        migrate(conn)

        entity_cols = {row["name"] for row in conn.execute("PRAGMA table_info(hearth_entities);")}
        for column, _ in ENTITIES_NEW_COLUMNS:
            print(f"[MIGRATION] hearth_entities.{column}: {'OK' if column in entity_cols else 'MISSING'}")

        rel_cols = {row["name"] for row in conn.execute("PRAGMA table_info(hearth_relationships);")}
        for column, _ in RELATIONSHIPS_NEW_COLUMNS:
            print(f"[MIGRATION] hearth_relationships.{column}: {'OK' if column in rel_cols else 'MISSING'}")

        existing_tables = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table';"
            )
        }
        for table in NEW_TABLES:
            print(f"[MIGRATION] {table}: {'OK' if table in existing_tables else 'MISSING'}")

        total_entities = conn.execute("SELECT COUNT(*) AS c FROM hearth_entities;").fetchone()["c"]
        defaulted_entities = conn.execute(
            "SELECT COUNT(*) AS c FROM hearth_entities"
            " WHERE entity_type = 'person' AND source = 'pathway_sync'"
            " AND canonical_key = 'user:' || user_id;"
        ).fetchone()["c"]
        print(f"[MIGRATION] hearth_entities defaulted correctly: {defaulted_entities}/{total_entities}")

        total_rels = conn.execute("SELECT COUNT(*) AS c FROM hearth_relationships;").fetchone()["c"]
        defaulted_rels = conn.execute(
            "SELECT COUNT(*) AS c FROM hearth_relationships"
            " WHERE origin = 'structural' AND status = 'active';"
        ).fetchone()["c"]
        print(f"[MIGRATION] hearth_relationships defaulted correctly: {defaulted_rels}/{total_rels}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
    print("Migration complete.")
