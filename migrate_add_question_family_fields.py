"""
Migration: add question_family/question_version to hearth_worldview_uncertainties.

Adds: question_family (TEXT), question_version (INTEGER)

These are deterministic identity fields stamped only by the generator that
already knows why it's opening a given uncertainty (see hearth_soul.py's
_QUESTION_FAMILIES_BY_EPISODE_TYPE). Build 1 of the manager-answer learning
loop stamps only question_family="checkin_feedback_waiting",
question_version=1 — every other row, historical or new, stays NULL for both
columns. No backfill of any kind is performed by this migration.

Safe to run multiple times; skips columns that already exist.
"""
import sqlite3

from hearth_memory import MEMORY_DB_PATH

_NEW_COLUMNS = [
    ("question_family",  "TEXT"),
    ("question_version", "INTEGER"),
]


def migrate(conn=None):
    owns_conn = conn is None
    if owns_conn:
        conn = sqlite3.connect(MEMORY_DB_PATH)
        conn.row_factory = sqlite3.Row
    try:
        for col, typ in _NEW_COLUMNS:
            try:
                conn.execute(
                    f"ALTER TABLE hearth_worldview_uncertainties"
                    f" ADD COLUMN {col} {typ};"
                )
                conn.commit()
                print(f"hearth_worldview_uncertainties: added {col} column.")
            except sqlite3.OperationalError as exc:
                if "duplicate column name" in str(exc).lower():
                    print(f"hearth_worldview_uncertainties: {col} already exists — skipped.")
                else:
                    raise
    finally:
        if owns_conn:
            conn.close()


if __name__ == "__main__":
    migrate()
    print("Migration complete.")
