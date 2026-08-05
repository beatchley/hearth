"""
Migration: universal interpretation status/claims schema (Build 2 of the
manager-answer learning loop).

Adds to hearth_answer_interpretations:
    universal_status TEXT   -- 'supported' | 'insufficient_information' | 'unrelated_or_unclear'
    raw_status       TEXT   -- model's raw proposed universal status, pre-validation
    raw_claims_json  TEXT   -- JSON array of the model's raw proposed claims, pre-validation

These are additive and NULL for every historical Build 1 row — Build 1 wrote
raw_conclusion/conclusion (its own closed pattern taxonomy) instead, and those
columns are untouched. See hearth_answer_interpreter.py for how the two
generations of rows are told apart at read time (build_generation()).

Adds two new tables:
    hearth_answer_interpretation_claims — one row per structured claim
        proposed for a given interpretation attempt (0-3 per attempt),
        accepted and rejected claims both stored for audit, with per-claim
        semantic-check provenance.
    hearth_learning_candidates / hearth_learning_candidate_members — durable,
        human-reviewable rollups of repeated structured claims across
        independent answers/subjects. Never written to except by
        hearth_answer_interpreter.compute_learning_candidates(); never
        auto-promoted into Worldview.

Safe to run more than once: every statement uses CREATE TABLE/INDEX IF NOT
EXISTS or catches "duplicate column name".

Usage:
    python3 migrate_add_universal_claims_schema.py
"""

import sqlite3

from hearth_memory import MEMORY_DB_PATH

_NEW_INTERPRETATION_COLUMNS = [
    ("universal_status", "TEXT"),
    ("raw_status", "TEXT"),
    ("raw_claims_json", "TEXT"),
]


def get_connection():
    conn = sqlite3.connect(MEMORY_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def migrate(conn=None):
    owns_conn = conn is None
    if owns_conn:
        conn = get_connection()
    try:
        for col, typ in _NEW_INTERPRETATION_COLUMNS:
            try:
                conn.execute(
                    f"ALTER TABLE hearth_answer_interpretations ADD COLUMN {col} {typ};"
                )
                conn.commit()
                print(f"hearth_answer_interpretations: added {col} column.")
            except sqlite3.OperationalError as exc:
                if "duplicate column name" in str(exc).lower():
                    print(f"hearth_answer_interpretations: {col} already exists — skipped.")
                else:
                    raise

        conn.executescript("""
            CREATE TABLE IF NOT EXISTS hearth_answer_interpretation_claims (
                id                      INTEGER PRIMARY KEY AUTOINCREMENT,
                interpretation_id       INTEGER NOT NULL,
                uncertainty_id          INTEGER NOT NULL,
                question_family         TEXT    NOT NULL,
                question_version        INTEGER NOT NULL,
                claim_index             INTEGER NOT NULL,
                subject                 TEXT,
                predicate               TEXT,
                value                   TEXT,
                polarity                TEXT,
                scope                   TEXT,
                temporal_status         TEXT,
                conclusion_text         TEXT,
                evidence_quote          TEXT,
                accepted                INTEGER NOT NULL DEFAULT 0,
                rejection_reason        TEXT,
                semantic_check_provider TEXT,
                semantic_check_model    TEXT,
                semantic_check_result   TEXT,
                semantic_check_reason   TEXT,
                created_at              TEXT    NOT NULL,
                FOREIGN KEY (interpretation_id) REFERENCES hearth_answer_interpretations(id),
                FOREIGN KEY (uncertainty_id) REFERENCES hearth_worldview_uncertainties(id)
            );
            CREATE INDEX IF NOT EXISTS idx_claims_interpretation
                ON hearth_answer_interpretation_claims (interpretation_id);
            CREATE INDEX IF NOT EXISTS idx_claims_uncertainty
                ON hearth_answer_interpretation_claims (uncertainty_id);
            CREATE INDEX IF NOT EXISTS idx_claims_family_version
                ON hearth_answer_interpretation_claims (question_family, question_version);
            CREATE INDEX IF NOT EXISTS idx_claims_predicate_value
                ON hearth_answer_interpretation_claims (predicate, value);
            CREATE INDEX IF NOT EXISTS idx_claims_accepted
                ON hearth_answer_interpretation_claims (accepted);

            CREATE TABLE IF NOT EXISTS hearth_learning_candidates (
                id                      INTEGER PRIMARY KEY AUTOINCREMENT,
                question_family         TEXT    NOT NULL,
                question_version        INTEGER NOT NULL,
                predicate_key           TEXT    NOT NULL,
                value_key               TEXT    NOT NULL,
                polarity                TEXT    NOT NULL,
                scope_key               TEXT    NOT NULL,
                interpretation_count    INTEGER NOT NULL DEFAULT 0,
                distinct_subject_count  INTEGER NOT NULL DEFAULT 0,
                first_observed_at       TEXT,
                last_observed_at        TEXT,
                sample_conclusion_text  TEXT,
                sample_evidence_quote   TEXT,
                status                  TEXT    NOT NULL DEFAULT 'pending',
                created_at              TEXT    NOT NULL,
                updated_at              TEXT    NOT NULL,
                UNIQUE (question_family, question_version, predicate_key, value_key, polarity, scope_key)
            );
            CREATE INDEX IF NOT EXISTS idx_candidates_family_version
                ON hearth_learning_candidates (question_family, question_version);
            CREATE INDEX IF NOT EXISTS idx_candidates_status
                ON hearth_learning_candidates (status);

            CREATE TABLE IF NOT EXISTS hearth_learning_candidate_members (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                candidate_id  INTEGER NOT NULL,
                claim_id      INTEGER NOT NULL,
                created_at    TEXT    NOT NULL,
                FOREIGN KEY (candidate_id) REFERENCES hearth_learning_candidates(id),
                FOREIGN KEY (claim_id) REFERENCES hearth_answer_interpretation_claims(id),
                UNIQUE (candidate_id, claim_id)
            );
            CREATE INDEX IF NOT EXISTS idx_candidate_members_candidate
                ON hearth_learning_candidate_members (candidate_id);
        """)
        conn.commit()
    finally:
        if owns_conn:
            conn.close()


def main():
    print(f"[MIGRATION] target database: {MEMORY_DB_PATH}")
    conn = get_connection()
    try:
        migrate(conn)
        for table in (
            "hearth_answer_interpretation_claims",
            "hearth_learning_candidates",
            "hearth_learning_candidate_members",
        ):
            exists = conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?;",
                (table,),
            ).fetchone()
            print(f"[MIGRATION] {table}: {'OK' if exists else 'MISSING'}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
