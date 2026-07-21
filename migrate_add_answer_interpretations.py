"""
Migration: add the hearth_answer_interpretations ledger table.

This is the durable, auditable record of every attempt to interpret a
manager's answer to a Worldview question (Build 1 of the manager-answer
learning loop — see hearth_answer_interpreter.py). It is a separate table
from hearth_worldview_uncertainties: the uncertainty row remains the sole
authoritative record of the question/subject/answer; this table stores only
*derived* interpretation attempts and their provenance.

Invariants enforced at the schema level:
  - At most one row per uncertainty_id may have is_current_accepted = 1
    (partial unique index) — a durable version of "at most one current
    accepted interpretation per uncertainty."
  - Every attempt is an independent row; nothing here is ever overwritten by
    a later attempt (retries/new interpreter versions just add new rows,
    linked via retry_of_attempt_id for auditability).

Safe to run more than once: every statement uses CREATE TABLE/INDEX IF NOT
EXISTS.

Usage:
    python3 migrate_add_answer_interpretations.py
"""

import sqlite3

from hearth_memory import MEMORY_DB_PATH


def get_connection():
    conn = sqlite3.connect(MEMORY_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def migrate(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS hearth_answer_interpretations (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            uncertainty_id      INTEGER NOT NULL,
            question_family     TEXT    NOT NULL,
            question_version    INTEGER NOT NULL,
            attempt_number      INTEGER NOT NULL,
            retry_of_attempt_id INTEGER,
            status              TEXT    NOT NULL DEFAULT 'pending',
            interpreter_version TEXT    NOT NULL,
            schema_version      TEXT    NOT NULL,
            provider            TEXT,
            model_name          TEXT,
            model_identifier    TEXT,
            raw_conclusion      TEXT,
            conclusion          TEXT,
            model_confidence    REAL,
            model_reason        TEXT,
            validation_result   TEXT,
            validation_reason   TEXT,
            is_current_accepted INTEGER NOT NULL DEFAULT 0,
            failure_category    TEXT,
            failure_detail      TEXT,
            attempted_at        TEXT    NOT NULL,
            completed_at        TEXT,
            created_at          TEXT    NOT NULL,
            FOREIGN KEY (uncertainty_id) REFERENCES hearth_worldview_uncertainties(id)
        );

        CREATE INDEX IF NOT EXISTS idx_answer_interp_uncertainty
            ON hearth_answer_interpretations (uncertainty_id);
        CREATE INDEX IF NOT EXISTS idx_answer_interp_family_version
            ON hearth_answer_interpretations (question_family, question_version);
        CREATE INDEX IF NOT EXISTS idx_answer_interp_status
            ON hearth_answer_interpretations (status);
        CREATE INDEX IF NOT EXISTS idx_answer_interp_interpreter_version
            ON hearth_answer_interpretations (interpreter_version);

        -- At most one current accepted interpretation per uncertainty,
        -- enforced at the schema level (not just in application code).
        CREATE UNIQUE INDEX IF NOT EXISTS uq_answer_interp_current_accepted
            ON hearth_answer_interpretations (uncertainty_id)
            WHERE is_current_accepted = 1;
    """)
    conn.commit()


def main():
    print(f"[MIGRATION] target database: {MEMORY_DB_PATH}")
    conn = get_connection()
    try:
        migrate(conn)
        exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
            " AND name = 'hearth_answer_interpretations';"
        ).fetchone()
        print(f"[MIGRATION] hearth_answer_interpretations: {'OK' if exists else 'MISSING'}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
