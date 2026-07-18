"""
training_comment_needs_response resolution — wiring regression test.

Confirms the legacy training_comment_needs_response watcher, which
previously had no resolution path at all (stayed open until manually
resolved — see resolve_stale_issues()'s docstring in morning_briefing.py),
now reuses the same six-condition explicit-reason check already proven for
training_comment_waiting (test_training_comment_waiting_resolution.py).

The six conditions themselves are exercised in depth against
_training_comment_waiting_resolution_reason() by that existing suite; this
test only covers the NEW wiring specific to training_comment_needs_response:
  1. A comment still present in the current scan's flagged results stays open.
  2. A comment absent from the flagged results, with no pathway_conn
     available, is left open (cannot confirm resolution) rather than guessed at.
  3. A comment absent from the flagged results, confirmed deleted in Pathway
     (row gone from training_comments), resolves with resolution_reason
     'comment_deleted' — proving the ref_key ("training_comment_<id>", not
     "training_comment_waiting_<id>") is parsed correctly and dispatched
     through the shared resolution-reason helper.

Uses isolated in-memory/temp-file databases only — never touches the real
dev hearth_memory.db or app.db.

Run: venv/bin/python3 test_training_comment_needs_response_resolution.py
"""

import sqlite3
import sys

import hearth_memory
import morning_briefing as mb

failures = []


def check(name, cond, msg=""):
    if not cond:
        failures.append(f"FAIL [{name}]" + (f": {msg}" if msg else ""))
        print(f"  FAIL: {name}" + (f" — {msg}" if msg else ""))
    else:
        print(f"  pass: {name}")


def _memory_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    hearth_memory.init_tables(conn)
    return conn


def _pathway_conn_without_comment():
    """Minimal Pathway-shaped DB where the comment row has been deleted."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE training_comments (id INTEGER PRIMARY KEY, training_id INTEGER,"
                 " user_id INTEGER, content TEXT, created_at TEXT, comment_type TEXT,"
                 " acknowledged_at TEXT);")
    conn.execute("CREATE TABLE training_comment_replies (id INTEGER PRIMARY KEY,"
                 " comment_id INTEGER, user_id INTEGER);")
    conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, role TEXT, status TEXT);")
    return conn


def _make_episode(memory_conn, comment_id, entity_id):
    eid, _action = hearth_memory.create_episode(
        memory_conn, entity_id, "training_comment_needs_response",
        description=f"needs-response test comment {comment_id}",
        reference_key=f"training_comment_{comment_id}",
    )
    return eid


def _episode_row(memory_conn, eid):
    return memory_conn.execute(
        "SELECT * FROM hearth_episodes WHERE id = ?;", (eid,)
    ).fetchone()


def run():
    print("=== 1. Still-flagged comment stays open ===")
    memory_conn = _memory_conn()
    entity = hearth_memory.get_or_create_entity(memory_conn, 501)
    eid = _make_episode(memory_conn, comment_id=1, entity_id=entity["id"])

    data = {"Training comments for review (48 h)": [
        {"id": 1, "content": "How do I fix this, still stuck?", "role": "member", "permissions": None},
    ]}
    mb.resolve_stale_issues(memory_conn, data, pathway_conn=None)
    ep = _episode_row(memory_conn, eid)
    check("episode still open while comment is present in flagged results",
          ep["resolved"] == 0, dict(ep))
    memory_conn.close()

    print("\n=== 2. Absent from flagged results, no pathway_conn — left open ===")
    memory_conn = _memory_conn()
    entity = hearth_memory.get_or_create_entity(memory_conn, 502)
    eid = _make_episode(memory_conn, comment_id=2, entity_id=entity["id"])

    data = {"Training comments for review (48 h)": []}
    mb.resolve_stale_issues(memory_conn, data, pathway_conn=None)
    ep = _episode_row(memory_conn, eid)
    check("episode left open when pathway_conn is unavailable to confirm a reason",
          ep["resolved"] == 0, dict(ep))
    memory_conn.close()

    print("\n=== 3. Absent from flagged results, comment deleted — resolves ===")
    memory_conn = _memory_conn()
    entity = hearth_memory.get_or_create_entity(memory_conn, 503)
    eid = _make_episode(memory_conn, comment_id=3, entity_id=entity["id"])
    pconn = _pathway_conn_without_comment()  # comment_id 3 does not exist here

    data = {"Training comments for review (48 h)": []}
    mb.resolve_stale_issues(memory_conn, data, pathway_conn=pconn)
    ep = _episode_row(memory_conn, eid)
    check("episode resolves once absence is confirmed via a live pathway_conn",
          ep["resolved"] == 1, dict(ep))
    check("resolution_reason is 'comment_deleted' for a comment no longer in training_comments",
          ep["resolution_reason"] == "comment_deleted", ep["resolution_reason"])
    memory_conn.close()
    pconn.close()

    print("\n" + ("=" * 60))
    if failures:
        print(f"{len(failures)} FAILURE(S):")
        for f in failures:
            print(f"  {f}")
        sys.exit(1)
    else:
        print("All training_comment_needs_response resolution wiring assertions passed.")


if __name__ == "__main__":
    run()
