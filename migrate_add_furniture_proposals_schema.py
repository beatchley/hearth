"""
Migration: hearth_furniture_proposals and hearth_processed_sources tables —
the review queue and replay-safety ledger for the Furniture Fact Extractor.

Schema-only, purely additive. Creates two new tables. Does not touch
hearth_entity_furniture or any other existing table. No existing column is
removed or renamed, and no existing row is modified.

hearth_furniture_proposals is a dedicated table for proposed Furniture facts
only — it does not reuse hearth_state_proposals (a different Watcher's
review queue for hearth_entity_state, not Furniture) and does not implement
a 'superseded' status; the Fact Extractor never merges, updates, or
strengthens a proposal once written, so there is nothing to supersede.

hearth_processed_sources makes extraction replay-safe: a source record is
only ever evaluated once per (source_type, source_record_id, content_hash,
extractor_version) identity, so re-running a batch (or a later extractor
version) never re-proposes from a source record whose exact content was
already looked at.

Safe to run more than once: CREATE TABLE/INDEX statements use IF NOT EXISTS.

Usage:
    python3 migrate_add_furniture_proposals_schema.py
"""

import sqlite3

from hearth_memory import MEMORY_DB_PATH


def get_connection():
    """Open a read-write connection to Hearth's own memory database."""
    conn = sqlite3.connect(MEMORY_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def create_tables(conn):
    conn.executescript(
        """
        -- Extractor-proposed Furniture facts, awaiting human approval or
        -- dismissal. The extractor never writes hearth_entity_furniture
        -- directly; approving a proposal is the only path from here into
        -- Furniture (see hearth_furniture.create_furniture,
        -- hearth_furniture_proposals.approve_furniture_proposal).
        CREATE TABLE IF NOT EXISTS hearth_furniture_proposals (
            id                     INTEGER PRIMARY KEY AUTOINCREMENT,
            entity_id              INTEGER NOT NULL REFERENCES hearth_entities(id),
            proposed_fact          TEXT    NOT NULL,
            fact_type              TEXT    NOT NULL,
            confidence             REAL    NOT NULL,
            evidence_quote         TEXT    NOT NULL,
            source_type            TEXT    NOT NULL,
            source_record_id       TEXT    NOT NULL,
            source_author_user_id  INTEGER,
            semantic_fingerprint   TEXT    NOT NULL,
            extractor_version      TEXT    NOT NULL,
            status                 TEXT    NOT NULL DEFAULT 'pending',
            reviewed_by            TEXT,
            reviewed_at            TEXT,
            applied_furniture_id   INTEGER,
            created_at             TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_furniture_proposals_entity_status
            ON hearth_furniture_proposals (entity_id, status);
        CREATE INDEX IF NOT EXISTS idx_furniture_proposals_status
            ON hearth_furniture_proposals (status);
        CREATE INDEX IF NOT EXISTS idx_furniture_proposals_dedup
            ON hearth_furniture_proposals (entity_id, semantic_fingerprint);
        CREATE INDEX IF NOT EXISTS idx_furniture_proposals_source
            ON hearth_furniture_proposals (source_type, source_record_id);

        -- Replay-safety ledger: one row per (source_type, source_record_id,
        -- content_hash, extractor_version) the extractor has ever evaluated,
        -- regardless of whether that evaluation produced a proposal. Marked
        -- after all candidate Buildings for a source record have been
        -- evaluated. A changed content_hash (the source record was edited)
        -- is a new identity and will be evaluated again — deliberate, not a
        -- bug: see hearth_fact_extractor.py's Source Edits note.
        CREATE TABLE IF NOT EXISTS hearth_processed_sources (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            source_type        TEXT    NOT NULL,
            source_record_id   TEXT    NOT NULL,
            content_hash       TEXT    NOT NULL,
            extractor_version  TEXT    NOT NULL,
            processed_at       TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (source_type, source_record_id, content_hash, extractor_version)
        );

        CREATE INDEX IF NOT EXISTS idx_processed_sources_lookup
            ON hearth_processed_sources (source_type, source_record_id, extractor_version);
        """
    )
    conn.commit()


def main():
    print(f"[MIGRATION] target database: {MEMORY_DB_PATH}")
    conn = get_connection()
    try:
        create_tables(conn)
        existing_tables = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table';"
            )
        }
        for name in ("hearth_furniture_proposals", "hearth_processed_sources"):
            print(f"[MIGRATION] {name}: {'OK' if name in existing_tables else 'MISSING'}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
    print("Migration complete.")
