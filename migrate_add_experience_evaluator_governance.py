"""
Migration: Experience Evaluator governance table — durable per-event ledger
— plus a defensive hardening index on hearth_episodes.

Schema-only, purely additive except for one explicit, safe drop (see
"Dropped table" below). Does not touch hearth_events (Pathway's own
database) and does not modify any existing hearth_episodes/hearth_entities
row.

Background (see hearth_experience_evaluator.py's module docstring for the
full defect writeup): the evaluator used to rescan the latest N Pathway
events on every run and dedup promoted episodes on (episode_type,
reference_key). Because the same source event could classify differently
across runs as Worldview changed, that dedup key was ineffective — the same
event could produce multiple, contradictory promoted episodes. This
migration adds the table the corrected evaluator needs to guarantee "one
terminal classification per source event", enforced by a database UNIQUE
constraint, not an application-level lookup alone.

hearth_experience_evaluations:
    One durable row per (source_event_id, evaluator_version). Records the
    terminal (or currently-retryable) outcome of evaluating one Pathway
    hearth_events row, including the exact matched target (target_type +
    target_id) and the relationship rule that matched, if any. This is the
    structured provenance record — hearth_episodes itself is not modified to
    carry these fields. Legacy evaluator-promoted hearth_episodes rows (from
    before this migration existed) have no corresponding row here; that
    absence is itself how they're distinguished as legacy/ungoverned.
    evaluator_version on each row is an audit stamp of which code version
    produced it — normal event selection is version-agnostic (a version
    bump never re-queues an event that already has a 'processed' row here
    under an older version); see hearth_experience_evaluator.EVALUATOR_VERSION.
    classification is one of 'momentum'|'no_match'|'rejected_unrelated' —
    concern/resolution detection is permanently out of scope for this
    module (see hearth_experience_evaluator.py's module docstring), so
    those values are never written.

Dropped table — hearth_experience_target_resolutions:
    An earlier version of this migration also created a target-level
    idempotency lock table, needed only for resolving an original watcher
    episode from a matched "resolution" rule. That mechanism has since been
    removed permanently (concern/resolution detection was decided to be
    out of scope for this module by architecture, not a temporary gap —
    see hearth_experience_evaluator.py's module docstring), so this table
    has no remaining reader or writer anywhere in the codebase. This
    migration drops it if present. Safe: nothing has been deployed or
    committed since it was introduced, no production data ever depended on
    it, and hearth_memory.create_episode's own dedup plus the hardening
    index below are unaffected by its removal.

Defensive hardening:
    hearth_episodes' existing dedup (in hearth_memory.create_episode) checks
    "does an open row with this (episode_type, reference_key) already
    exist?" before inserting, but that check-then-insert is not atomic
    against a concurrent writer. This migration adds a partial UNIQUE index
    enforcing it at the database layer for all open rows, for every watcher
    that uses reference_key (not just the evaluator) — but only if no
    pre-existing violating duplicates are found first. If duplicates already
    exist (e.g. as a symptom of the very defect this build fixes), index
    creation is skipped and a warning is printed instead of failing the
    migration. This keeps the migration safe to run against a dirty
    production database without deleting or altering any row.

Safe to run more than once: every statement uses IF NOT EXISTS (or IF
EXISTS, for the drop), and the index-duplicate check is re-evaluated (and
re-skipped, harmlessly) on every run until the underlying duplicates are
cleaned up separately.

Usage:
    python3 migrate_add_experience_evaluator_governance.py
"""

import sqlite3

from hearth_memory import MEMORY_DB_PATH

_LEDGER_TABLE = "hearth_experience_evaluations"
_DROPPED_TARGET_LOCK_TABLE = "hearth_experience_target_resolutions"
_DEDUP_INDEX = "idx_episodes_open_type_reference_unique"


def get_connection():
    """Open a read-write connection to Hearth's own memory database."""
    conn = sqlite3.connect(MEMORY_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def create_tables(conn):
    conn.executescript(
        f"""
        -- One durable row per (source_event_id, evaluator_version). See
        -- module docstring for the full field-by-field rationale.
        CREATE TABLE IF NOT EXISTS {_LEDGER_TABLE} (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            source_event_id     INTEGER NOT NULL,
            evaluator_version   TEXT    NOT NULL,
            status              TEXT    NOT NULL,  -- 'processed' | 'failed_retryable'
            classification      TEXT,               -- 'momentum'|'no_match'|'rejected_unrelated'
            entity_id           INTEGER,
            target_type         TEXT,
            target_id           TEXT,
            rule_id             TEXT,
            resulting_episode_id INTEGER,
            resulting_action    TEXT,   -- 'created_episode'|'reused_open_episode'|'no_action'
            reason              TEXT,
            error_detail        TEXT,
            evaluated_at        TEXT    NOT NULL,
            created_at          TEXT    NOT NULL,
            updated_at          TEXT    NOT NULL
        );

        CREATE UNIQUE INDEX IF NOT EXISTS idx_experience_evaluations_event_version
            ON {_LEDGER_TABLE} (source_event_id, evaluator_version);

        CREATE INDEX IF NOT EXISTS idx_experience_evaluations_status
            ON {_LEDGER_TABLE} (evaluator_version, status);
        """
    )
    conn.commit()


def drop_target_lock_table(conn):
    """Drop hearth_experience_target_resolutions if present — see module
    docstring's "Dropped table" section. Idempotent; safe against a
    database that never had it.
    """
    conn.execute(f"DROP TABLE IF EXISTS {_DROPPED_TARGET_LOCK_TABLE};")
    conn.commit()


def _find_dedup_index_violations(conn):
    """Return open (episode_type, reference_key) pairs that appear more than
    once — these would violate the partial unique index below. Read-only.
    """
    return conn.execute(
        """
        SELECT episode_type, reference_key, COUNT(*) AS n
        FROM hearth_episodes
        WHERE reference_key IS NOT NULL AND resolved = 0
        GROUP BY episode_type, reference_key
        HAVING COUNT(*) > 1;
        """
    ).fetchall()


def create_dedup_hardening_index(conn):
    """Create the partial unique index hardening hearth_episodes'
    (episode_type, reference_key) open-row dedup at the database layer,
    unless pre-existing violating duplicates make that impossible.

    Returns (created: bool, violations: list[sqlite3.Row]). Never raises for
    a dirty database — reports and skips instead, per module docstring.
    """
    violations = _find_dedup_index_violations(conn)
    if violations:
        return False, violations

    existing = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'index' AND name = ?;",
        (_DEDUP_INDEX,),
    ).fetchone()
    if existing:
        return True, []

    try:
        conn.execute(
            f"CREATE UNIQUE INDEX {_DEDUP_INDEX} "
            "ON hearth_episodes (episode_type, reference_key) "
            "WHERE reference_key IS NOT NULL AND resolved = 0;"
        )
        conn.commit()
        return True, []
    except sqlite3.OperationalError as exc:
        if "already exists" in str(exc).lower():
            return True, []
        raise


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
        print(f"[MIGRATION] {_LEDGER_TABLE}: {'OK' if _LEDGER_TABLE in existing_tables else 'MISSING'}")

        had_target_lock_table = _DROPPED_TARGET_LOCK_TABLE in existing_tables
        drop_target_lock_table(conn)
        if had_target_lock_table:
            print(f"[MIGRATION] {_DROPPED_TARGET_LOCK_TABLE}: DROPPED (no longer used — momentum-only architecture)")
        else:
            print(f"[MIGRATION] {_DROPPED_TARGET_LOCK_TABLE}: not present, nothing to drop")

        created, violations = create_dedup_hardening_index(conn)
        if created:
            print(f"[MIGRATION] {_DEDUP_INDEX}: OK")
        else:
            print(
                f"[MIGRATION] {_DEDUP_INDEX}: SKIPPED — found "
                f"{len(violations)} pre-existing open (episode_type, reference_key) "
                "duplicate group(s) that would violate this index. No index was "
                "created and no existing row was changed. Resolve the duplicates "
                "(e.g. via hearth_experience_evaluator_cleanup.py for evaluator-"
                "promoted rows) and re-run this migration."
            )
            for v in violations:
                print(
                    f"[MIGRATION]   duplicate: episode_type={v['episode_type']!r} "
                    f"reference_key={v['reference_key']!r} count={v['n']}"
                )
    finally:
        conn.close()


if __name__ == "__main__":
    main()
    print("Migration complete.")
