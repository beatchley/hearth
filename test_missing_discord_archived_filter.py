"""
Fix — query_missing_discord() must exclude archived onboarding_records.

An archived record (archived_at set) reflects onboarding state that no
longer applies to the creator's current status — even if its own
added_discord column still says 'needed' from before it was archived
(e.g. archived with a reason indicating the item was actually done). The
Pathway Portal dashboard already filters to archived_at IS NULL when
showing "Added Discord" status; the Hearth watcher must match that filter
exactly, or it treats stale archived rows as current and never stops
flagging creators who are actually fine (confirmed in production:
episodes 559 and 562).

Seeds real rows in Pathway's app.db (located via DATABASE_URL), runs the
real query_missing_discord(), and cleans up everything it created in a
finally block. Mirrors the pattern in test_deactivation_fixes.py.

Run: venv/bin/python3 test_missing_discord_archived_filter.py
"""

import datetime as _dt
import sqlite3
import sys

from dotenv import load_dotenv

load_dotenv()

import morning_briefing as mb

MARKER = "DISCARCHFIX_TEST"


def _pathway_db_path():
    raw = mb.DATABASE_URL or ""
    if not raw:
        print("DATABASE_URL not set — cannot run this test.")
        sys.exit(1)
    return raw[len("sqlite:///"):] if raw.startswith("sqlite:///") else raw


def _iso(dt):
    return dt.isoformat(sep=" ", timespec="seconds")


def main():
    db_path = _pathway_db_path()
    pconn = sqlite3.connect(db_path)
    pconn.row_factory = sqlite3.Row
    pconn.execute("PRAGMA foreign_keys = ON;")

    created = {"users": [], "onboarding_records": []}

    def insert(table, columns, values):
        placeholders = ", ".join("?" for _ in columns)
        cur = pconn.execute(
            f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders});", values,
        )
        pconn.commit()
        created[table].append(cur.lastrowid)
        return cur.lastrowid

    try:
        today = _dt.date.today().isoformat()
        now = _dt.datetime.utcnow()

        # User A: current, unarchived onboarding record, added_discord='needed'.
        # Real false-positive-free case — must still be flagged.
        current_id = insert(
            "users", ["email", "name", "role", "status", "is_pathway_creator", "joined_on", "created_at"],
            (f"current_{MARKER}@example.invalid", f"CurrentUser{MARKER}", "member", "approved", 1, today, _iso(now)),
        )
        insert(
            "onboarding_records", ["user_id", "created_by_id", "created_at", "added_discord"],
            (current_id, current_id, _iso(now), "needed"),
        )

        # User B: only an archived onboarding record remains, still tagged
        # added_discord='needed' from before archival, but archived with a
        # reason showing the item was actually completed. Reproduces the
        # elboridetampa/mrsnunez5 production false positive — must NOT be
        # flagged. (The query's `added_discord IS NULL` OR-branch is not
        # separately exercised here: the column is NOT NULL in the real
        # schema, so that branch can't occur with genuine data.)
        archived_id = insert(
            "users", ["email", "name", "role", "status", "is_pathway_creator", "joined_on", "created_at"],
            (f"archived_{MARKER}@example.invalid", f"ArchivedUser{MARKER}", "member", "approved", 1, today, _iso(now)),
        )
        insert(
            "onboarding_records",
            ["user_id", "created_by_id", "created_at", "added_discord", "archived_at", "archive_reason"],
            (archived_id, archived_id, _iso(now), "needed", _iso(now), f"{MARKER}: completed via other channel"),
        )

        label, rows = mb.query_missing_discord(pconn)
        assert isinstance(rows, list), f"query_missing_discord failed: {rows}"
        row_ids = {row["id"] for row in rows}

        failures = []
        if current_id not in row_ids:
            failures.append(f"current_id={current_id} (unarchived, needed) should be flagged but was not")
        if archived_id in row_ids:
            failures.append(f"archived_id={archived_id} (archived, needed) should NOT be flagged but was")

        assert not failures, "Fix failures:\n" + "\n".join(failures)
        print(f"[OK] {label}: current user flagged, archived user (elboridetampa/mrsnunez5-style) excluded.")

        # --- resolution-path sanity check -----------------------------------
        # An archived record must mean the user naturally drops out of the
        # result set (so an open episode resolves), same as any other
        # excluded user — no special-casing needed since it's the same
        # set-membership check resolve_stale_issues() already does.
        assert archived_id not in row_ids
        print("[OK] Archived users are absent from the result set, "
              "so resolve_stale_issues()'s set-difference check will "
              "resolve any open episode for them on the next pass.")

    finally:
        pconn.execute("DELETE FROM onboarding_records WHERE user_id IN "
                      "(SELECT id FROM users WHERE email LIKE ?);", (f"%{MARKER}%",))
        pconn.execute("DELETE FROM users WHERE email LIKE ?;", (f"%{MARKER}%",))
        pconn.commit()
        remaining = pconn.execute(
            "SELECT COUNT(*) FROM users WHERE email LIKE ?;", (f"%{MARKER}%",)
        ).fetchone()[0]
        print(f"Remaining test users after cleanup: {remaining}")
        pconn.close()


if __name__ == "__main__":
    main()
