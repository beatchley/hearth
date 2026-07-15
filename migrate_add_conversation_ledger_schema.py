"""
Migration: hearth_conversation_ledger table — Phase 7b.

Schema-only, purely additive. Creates one new table. Does not touch any
existing table.

hearth_conversation_ledger is temporary staging for Ask Hearth conversation
turns on their way into the existing Furniture Fact Extractor pipeline (see
hearth_conversation_ledger.py, hearth_fact_extractor.py). It is deliberately
NOT the same mechanism as hearth_attention_frame.py's in-process Attention
Frame — that store exists only for conversational continuity within one
session and is never durable; this table exists so a staged human message
survives a process restart until the next Fact Extractor batch run picks it
up. Append-only while staged; rows are deleted (purged) by the Fact
Extractor once evaluated — the permanent artifact of a staged turn, if any,
is a Furniture proposal, never the raw conversation text itself.

session_id/author_user_id/actor_role are nullable: they are provenance/
debugging metadata only, never joined on by the extraction pipeline (which
reads message_text and author_user_id for subject-candidate resolution, the
same way every other source's subject_user_column already works — see
hearth_fact_extractor._generate_candidates). A caller with no active
Attention Frame session (e.g. a direct call, or a future non-Flask caller)
can still stage a turn with session_id left NULL.

Safe to run more than once: CREATE TABLE/INDEX statements use IF NOT EXISTS.

Usage:
    python3 migrate_add_conversation_ledger_schema.py
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
        -- One row per eligible human conversation turn, staged the moment
        -- it is received (see hearth_ask.py's Phase 7b staging call).
        -- Eligibility is enforced entirely by the caller (routing decision
        -- + authorization check) — this table has no opinion on what
        -- belongs in it, only how it is stored.
        CREATE TABLE IF NOT EXISTS hearth_conversation_ledger (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id       TEXT,
            author_user_id   INTEGER,
            actor_role       TEXT,
            message_text     TEXT    NOT NULL,
            created_at       TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_conversation_ledger_created_at
            ON hearth_conversation_ledger (created_at);
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
        for name in ("hearth_conversation_ledger",):
            print(f"[MIGRATION] {name}: {'OK' if name in existing_tables else 'MISSING'}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
    print("Migration complete.")
