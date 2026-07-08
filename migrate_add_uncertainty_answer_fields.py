"""
Migration: add answer fields to hearth_worldview_uncertainties.

Adds: answer_text, answered_by, answered_at, acknowledged_at, acknowledged_by

These fields support the Hearth communication loop — managers can answer
surfaced questions and Hearth can reference those answers later.

Safe to run multiple times; skips columns that already exist.
"""
import sqlite3

from hearth_memory import MEMORY_DB_PATH

_NEW_COLUMNS = [
    ("answer_text",     "TEXT"),
    ("answered_by",     "TEXT"),
    ("answered_at",     "TEXT"),
    ("acknowledged_at", "TEXT"),
    ("acknowledged_by", "TEXT"),
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
