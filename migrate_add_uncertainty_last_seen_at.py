"""
Migration: add last_seen_at to hearth_worldview_uncertainties.

Tracks when an uncertainty was last observed — repeated sightings of the
same issue update last_seen_at in place rather than creating a new row.

Safe to run multiple times; skips if the column already exists.
"""

import sqlite3

from hearth_memory import MEMORY_DB_PATH


def migrate(conn=None):
    owns_conn = conn is None
    if owns_conn:
        conn = sqlite3.connect(MEMORY_DB_PATH)
        conn.row_factory = sqlite3.Row
    try:
        try:
            conn.execute(
                "ALTER TABLE hearth_worldview_uncertainties"
                " ADD COLUMN last_seen_at TEXT;"
            )
            conn.commit()
            print("hearth_worldview_uncertainties: added last_seen_at column.")
        except sqlite3.OperationalError as exc:
            if "duplicate column name" in str(exc).lower():
                print("hearth_worldview_uncertainties: last_seen_at already exists — skipped.")
            else:
                raise

        # Backfill: set last_seen_at = updated_at for existing rows
        conn.execute(
            "UPDATE hearth_worldview_uncertainties"
            " SET last_seen_at = updated_at"
            " WHERE last_seen_at IS NULL;"
        )
        conn.commit()
        backfilled = conn.execute(
            "SELECT COUNT(*) FROM hearth_worldview_uncertainties WHERE last_seen_at IS NOT NULL;"
        ).fetchone()[0]
        print(f"hearth_worldview_uncertainties: {backfilled} row(s) have last_seen_at set.")
    finally:
        if owns_conn:
            conn.close()


if __name__ == "__main__":
    migrate()
    print("Migration complete.")
