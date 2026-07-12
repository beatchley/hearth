#!/usr/bin/env python3
"""
Regression tests for the training-comment source-age vs tracking-age bug.

Root cause (see build prompt / Codex investigation): the training-comment
watcher correctly computes a comment's real unanswered age from
training_comments.created_at, but hearth_context.py separately computed a
"tracking age" from hearth_episodes.observed_at (how long Hearth's own
memory has known about the episode) and rendered *that* number in
manager-facing brief text. A classifier/eligibility change around July 1-2
made old comments newly eligible for detection, so old comments got recent
observed_at timestamps — producing a small tracking age alongside a much
larger real age, and Gemini rendered the misleading small one.

Covers:
    1. Fix 1 — training_comment_waiting concern lines omit the
       observed_at-derived tracking-age suffix, while other episode types'
       rendering is provably unaffected.
    2. Fix 2 — reusing an open training_comment_waiting episode calls
       refresh_episode() so the description (and its embedded source age)
       does not go stale/frozen across watcher runs.
    3. End-to-end bug scenario — a comment created ~90 days ago whose
       episode was first observed only ~10 days ago renders the ~90-day
       source age, never the ~10-day tracking age.

Uses the same lightweight in-memory-DB + check() harness as
test_fact_extractor.py. Run directly:

    python3 test_training_comment_age.py
"""

import sqlite3
import sys
from datetime import datetime, timedelta, timezone

import hearth_context
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


def _comment_row(comment_id, user_id, days_waiting, display_name="Creator",
                  training_id=1, training_title="Intro Training",
                  content="Can someone help me with this?"):
    """Mirrors the shape query_training_comment_waiting() hands the watcher."""
    return {
        "comment_id": comment_id,
        "user_id": user_id,
        "display_name": display_name,
        "training_id": training_id,
        "training_title": training_title,
        "days_waiting": days_waiting,
        "content": content,
        "created_at": (datetime.now(timezone.utc) - timedelta(days=days_waiting)).isoformat(),
    }


def _episode_row(conn, reference_key):
    return conn.execute(
        "SELECT * FROM hearth_episodes WHERE reference_key = ?;", (reference_key,)
    ).fetchone()


def _set_observed_at(conn, episode_id, days_ago):
    ts = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    conn.execute("UPDATE hearth_episodes SET observed_at = ? WHERE id = ?;", (ts, episode_id))
    conn.commit()


def run():
    print("=== 1. Rendering: training_comment_waiting omits tracking-age suffix ===")
    conn = _fresh_conn()

    # A comment truly unanswered for 93 days (source age), whose episode was
    # only first observed 10 days ago (tracking age) — the exact shape of the
    # July bug: classifier rollout made an old comment newly eligible.
    data = {"Training comments awaiting staff response": [
        _comment_row(comment_id=1, user_id=101, days_waiting=93, display_name="OldCommenter"),
    ]}
    morning_briefing.detect_training_comment_waiting(conn, data)
    ep = _episode_row(conn, "training_comment_waiting_1")
    check("episode created", ep is not None)
    _set_observed_at(conn, ep["id"], days_ago=10)

    # Control: a different, unrelated episode type at the same 10-day
    # tracking age, to prove the generic renderer is untouched.
    control_entity = hearth_memory.get_or_create_entity(conn, 202)
    _ep_id, _action = hearth_memory.create_episode(
        conn, control_entity["id"], "checkin_not_submitted",
        "@ControlUser has not submitted a checkin.",
        severity="medium", reference_key="control_checkin_202",
        briefing_category="action_needed",
    )
    _set_observed_at(conn, _ep_id, days_ago=10)

    open_episodes = hearth_memory.get_open_episodes(conn)
    ctx = hearth_context.build_context({}, open_episodes, memory_conn=conn)
    rendered = hearth_context.render_for_llm(ctx)

    training_comment_line = next(
        (line for line in rendered.splitlines() if "OldCommenter" in line), None
    )
    control_line = next(
        (line for line in rendered.splitlines() if "ControlUser" in line), None
    )
    check("training-comment concern line found in rendered output",
          training_comment_line is not None, rendered)
    check("control concern line found in rendered output",
          control_line is not None, rendered)

    check("rendered text states the real ~93-day source age",
          training_comment_line is not None and "93 days ago" in training_comment_line,
          training_comment_line)
    check("training-comment concern line does NOT present the 10-day tracking"
          " age as a duration (no '(for N days ...)' suffix at all)",
          training_comment_line is not None and "(for " not in training_comment_line,
          training_comment_line)
    check("training-comment concern line does NOT say 'worth escalating'"
          " (that phrase is driven by the suppressed 10-day tracking age)",
          training_comment_line is not None and "worth escalating" not in training_comment_line,
          training_comment_line)
    check("control (non-training-comment) episode at the same 10-day tracking"
          " age STILL renders the tracking-age suffix — proving the generic"
          " renderer's behavior for other episode types is unchanged",
          control_line is not None and "(for 10 days — worth escalating)" in control_line,
          control_line)

    conn.close()

    print("\n=== 2. Episode reuse calls refresh_episode() (no stale/frozen description) ===")
    conn = _fresh_conn()

    data_run1 = {"Training comments awaiting staff response": [
        _comment_row(comment_id=2, user_id=303, days_waiting=90, display_name="AgingCommenter"),
    ]}
    morning_briefing.detect_training_comment_waiting(conn, data_run1)
    ep_after_run1 = _episode_row(conn, "training_comment_waiting_2")
    check("episode created on first run", ep_after_run1 is not None)
    check("description reflects 90-day age after first run",
          "90 days ago" in ep_after_run1["description"], ep_after_run1["description"])
    observed_at_after_run1 = ep_after_run1["observed_at"]

    # Second watcher run, same comment, now further along (simulates a later
    # day's briefing run against the same still-unanswered comment).
    data_run2 = {"Training comments awaiting staff response": [
        _comment_row(comment_id=2, user_id=303, days_waiting=95, display_name="AgingCommenter"),
    ]}
    morning_briefing.detect_training_comment_waiting(conn, data_run2)

    all_episodes = conn.execute(
        "SELECT * FROM hearth_episodes WHERE reference_key = 'training_comment_waiting_2';"
    ).fetchall()
    check("reuse does not create a duplicate episode row", len(all_episodes) == 1,
          f"got {len(all_episodes)} rows")

    ep_after_run2 = _episode_row(conn, "training_comment_waiting_2")
    check("description updated to reflect the new 95-day age on reuse",
          "95 days ago" in ep_after_run2["description"], ep_after_run2["description"])
    check("stale 90-day description does not persist across runs",
          "90 days ago" not in ep_after_run2["description"], ep_after_run2["description"])
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
        print("All training-comment age regression assertions passed.")


if __name__ == "__main__":
    run()
