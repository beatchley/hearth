#!/usr/bin/env python3
"""
Regression test: reusing an open new_creator_stuck episode must refresh its
stored description, mirroring the fix already applied to
detect_training_comment_waiting() (see test_training_comment_age.py).

Root cause: days_since_joining is baked into the episode description at
create time, but detect_new_creator_stuck() never called refresh_episode()
on reuse — so a creator who stayed stuck across multiple watcher runs would
keep displaying the "N days" figure from whenever the episode was first
created, even though the real duration kept growing. Cosmetic only; the
resolution logic itself was already correct.

Run directly:

    python3 test_new_creator_stuck_refresh.py
"""

import sqlite3
import sys

import hearth_memory
import hearth_principles
import hearth_relationships
import hearth_worldview
import morning_briefing

failures = []


def check(name, cond, msg=""):
    if not cond:
        failures.append(f"FAIL [{name}]" + (f": {msg}" if msg else ""))
        print(f"  FAIL: {name}" + (f" — {msg}" if msg else ""))
    else:
        print(f"  pass: {name}")


def _fresh_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON;")
    hearth_memory.init_tables(conn)
    hearth_relationships.init_relationship_tables(conn)
    hearth_worldview.ensure_worldview_tables(conn)
    hearth_principles.ensure_principles_table(conn)
    return conn


def _stuck_row(user_id, days_since_joining, display_name="Creator", joined_on="2026-01-01"):
    """Mirrors the shape query_new_creator_stuck() hands the watcher."""
    return {
        "user_id": user_id,
        "display_name": display_name,
        "days_since_joining": days_since_joining,
        "joined_on": joined_on,
        "cn_coach_display_name": None,
        "shop_coach_display_name": None,
        "coach_display_name": None,
    }


def _episode_row(conn, reference_key):
    return conn.execute(
        "SELECT * FROM hearth_episodes WHERE reference_key = ?;", (reference_key,)
    ).fetchone()


def run():
    print("=== Episode reuse calls refresh_episode() (no stale/frozen description) ===")
    conn = _fresh_conn()

    data_run1 = {"New creators stuck (14+ days)": [
        _stuck_row(user_id=404, days_since_joining=14, display_name="StuckCreator"),
    ]}
    morning_briefing.detect_new_creator_stuck(conn, data_run1)
    ep_after_run1 = _episode_row(conn, "new_creator_stuck_404")
    check("episode created on first run", ep_after_run1 is not None)
    check("description reflects 14-day age after first run",
          "14 days ago" in ep_after_run1["description"], ep_after_run1["description"])
    check("severity is medium at 14 days", ep_after_run1["severity"] == "medium",
          ep_after_run1["severity"])
    observed_at_after_run1 = ep_after_run1["observed_at"]

    # Second watcher run, same creator, still stuck — and now past the
    # medium/high severity threshold too, to confirm severity also refreshes.
    data_run2 = {"New creators stuck (14+ days)": [
        _stuck_row(user_id=404, days_since_joining=35, display_name="StuckCreator"),
    ]}
    morning_briefing.detect_new_creator_stuck(conn, data_run2)

    all_episodes = conn.execute(
        "SELECT * FROM hearth_episodes WHERE reference_key = 'new_creator_stuck_404';"
    ).fetchall()
    check("reuse does not create a duplicate episode row", len(all_episodes) == 1,
          f"got {len(all_episodes)} rows")

    ep_after_run2 = _episode_row(conn, "new_creator_stuck_404")
    check("description updated to reflect the new 35-day age on reuse",
          "35 days ago" in ep_after_run2["description"], ep_after_run2["description"])
    check("stale 14-day description does not persist across runs",
          "14 days ago" not in ep_after_run2["description"], ep_after_run2["description"])
    check("severity refreshed to high past the 30-day threshold",
          ep_after_run2["severity"] == "high", ep_after_run2["severity"])
    check("refresh_episode() does not reset the tracking clock (observed_at unchanged)",
          ep_after_run2["observed_at"] == observed_at_after_run1)

    conn.close()

    print("\n" + ("=" * 60))
    if failures:
        print(f"{len(failures)} FAILURE(S):")
        for f in failures:
            print(f"  {f}")
        sys.exit(1)
    else:
        print("All new_creator_stuck refresh-on-reuse assertions passed.")


if __name__ == "__main__":
    run()
