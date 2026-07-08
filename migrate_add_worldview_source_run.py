"""
Migration: add source_run to the four hearth_worldview_* tables that Soul
writes through (beliefs, uncertainties, changes, recent_lessons).

source_run holds scan-level provenance (e.g. "morning"/"midday"/"evening")
for beliefs/uncertainties/changes/lessons that are drawn from an aggregate
pattern across a scan rather than one specific episode. Before this
migration, six call sites in hearth_soul.py incorrectly wrote that scan
label into source_episode_id (which must hold a real hearth_episodes.id)
— see the June 14, 2026 regression (commit 129328e). This migration only
adds the correctly-named column for future writes; it does NOT touch or
clean up existing rows that already have a bad source_episode_id value.
That cleanup is a deliberately separate follow-up task.

Safe to run multiple times; skips any column that already exists.
"""

import sqlite3

from hearth_memory import MEMORY_DB_PATH

_TABLES = (
    "hearth_worldview_beliefs",
    "hearth_worldview_uncertainties",
    "hearth_worldview_changes",
    "hearth_worldview_recent_lessons",
)


def migrate(conn=None):
    owns_conn = conn is None
    if owns_conn:
        conn = sqlite3.connect(MEMORY_DB_PATH)
        conn.row_factory = sqlite3.Row
    try:
        for table in _TABLES:
            try:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN source_run TEXT;")
                conn.commit()
                print(f"{table}: added source_run column.")
            except sqlite3.OperationalError as exc:
                if "duplicate column name" in str(exc).lower():
                    print(f"{table}: source_run already exists — skipped.")
                else:
                    raise
    finally:
        if owns_conn:
            conn.close()


if __name__ == "__main__":
    migrate()
    print("Migration complete.")
