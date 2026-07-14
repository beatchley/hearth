"""
training_comment_waiting resolution — explicit terminal-reason check.

End-to-end tests for the fix that replaces resolve_stale_issues()'s lossy
set-difference resolution for training_comment_waiting with six explicit,
individually-checked resolution paths (see
_training_comment_waiting_resolution_reason() in morning_briefing.py).

Root-caused bug (June 22): a comment absent from
query_training_comment_waiting()'s result set for ANY reason — including an
unrelated eligibility-query change — was resolved with a generic reason, with
no way to tell a real resolution from an unrelated disappearance.

Seeds real rows in Pathway's app.db (the same SQLite file the pathway-portal
dev server uses, located via DATABASE_URL) and in hearth_memory.db, under a
distinct MARKER-suffixed identity, then removes every row it created —
including from every child table it inserted into — in a finally block.

Run: venv/bin/python3 test_training_comment_waiting_resolution.py
"""

import datetime as _dt
import sqlite3
import sys

from dotenv import load_dotenv

load_dotenv()

import hearth_memory
import hearth_trace
import morning_briefing as mb

MARKER = "TCWRES_TEST"


def _pathway_db_path():
    raw = mb.DATABASE_URL or ""
    if not raw:
        print("DATABASE_URL not set — cannot run this test.")
        sys.exit(1)
    return raw[len("sqlite:///"):] if raw.startswith("sqlite:///") else raw


def _iso(dt):
    return dt.isoformat(sep=" ", timespec="seconds")


def _days_ago(n):
    return _dt.datetime.utcnow() - _dt.timedelta(days=n)


def main():
    db_path = _pathway_db_path()
    pconn = sqlite3.connect(db_path)
    pconn.row_factory = sqlite3.Row
    pconn.execute("PRAGMA foreign_keys = ON;")

    memory_conn = hearth_memory.get_memory_connection()
    hearth_memory.init_tables(memory_conn)

    created = {
        "users": [], "trainings": [], "training_comments": [],
        "training_comment_replies": [],
        "hearth_entities": [], "hearth_episodes": [],
    }

    def insert(table, columns, values):
        placeholders = ", ".join("?" for _ in columns)
        cur = pconn.execute(
            f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders});", values,
        )
        pconn.commit()
        created[table].append(cur.lastrowid)
        return cur.lastrowid

    def seed_creator(suffix, status="approved"):
        today = _dt.date.today().isoformat()
        return insert(
            "users",
            ["email", "name", "role", "status", "is_pathway_creator", "joined_on", "created_at"],
            (f"{suffix}_{MARKER}@example.invalid", f"Creator{suffix}{MARKER}",
             "member", status, 1, today, _iso(_dt.datetime.utcnow())),
        )

    def seed_staff(suffix):
        today = _dt.date.today().isoformat()
        return insert(
            "users",
            ["email", "name", "role", "status", "is_pathway_creator", "joined_on", "created_at"],
            (f"stf{suffix}_{MARKER}@example.invalid", f"Staff{suffix}{MARKER}",
             "coach", "approved", 0, today, _iso(_dt.datetime.utcnow())),
        )

    def seed_training(creator_id, suffix):
        return insert(
            "trainings",
            ["title", "training_type", "for_pathway_creators", "for_shop_creators", "created_by", "created_at"],
            (f"Training-{suffix}-{MARKER}", "written", 1, 0, creator_id, _iso(_days_ago(10))),
        )

    def seed_comment(training_id, creator_id, suffix, comment_type="question",
                      created_days_ago=5, acknowledged_at=None):
        return insert(
            "training_comments",
            ["training_id", "user_id", "content", "created_at", "comment_type", "acknowledged_at"],
            (training_id, creator_id, f"Comment-{suffix}-{MARKER}",
             _iso(_days_ago(created_days_ago)), comment_type, acknowledged_at),
        )

    def make_episode(comment_id, entity_id):
        eid, _action = hearth_memory.create_episode(
            memory_conn, entity_id, "training_comment_waiting",
            description=f"{MARKER} waiting on comment {comment_id}",
            reference_key=f"training_comment_waiting_{comment_id}",
        )
        created["hearth_episodes"].append(eid)
        return eid

    def episode_row(eid):
        return memory_conn.execute(
            "SELECT * FROM hearth_episodes WHERE id = ?;", (eid,)
        ).fetchone()

    def fresh_data():
        return mb.collect_data(pconn)

    failures = []

    try:
        comment_cutoff = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(
            days=mb.TRAINING_COMMENT_WAITING_DAYS
        )

        # =====================================================================
        # Scenario 1 — the June 22 bug itself: a comment that meets every real
        # "still open" criterion, but is simulated as absent from the current
        # scan's result set (as if an unrelated eligibility-query change hid
        # it). Must be LEFT OPEN, not resolved, with the inconsistency logged.
        # =====================================================================
        print(f"\n{'#' * 78}\n# Scenario 1 — unrelated disappearance must NOT resolve\n{'#' * 78}")
        glitch_creator = seed_creator("Glitch")
        glitch_entity = hearth_memory.get_or_create_entity(memory_conn, glitch_creator)
        created["hearth_entities"].append(glitch_entity["id"])
        glitch_training = seed_training(glitch_creator, "Glitch")
        glitch_comment = seed_comment(glitch_training, glitch_creator, "Glitch")
        glitch_episode = make_episode(glitch_comment, glitch_entity["id"])

        # Confirm this comment genuinely qualifies as still-open right now.
        _, real_rows = mb.query_training_comment_waiting(pconn, comment_cutoff)
        assert any(r["comment_id"] == glitch_comment for r in real_rows), (
            "test setup error: seeded comment does not appear in a real query run"
        )

        data = fresh_data()
        # Simulate the glitch: strip this one legitimately-still-open comment
        # out of the result set, as an unrelated query/eligibility change would.
        data["Training comments awaiting staff response"] = [
            r for r in data["Training comments awaiting staff response"]
            if r["comment_id"] != glitch_comment
        ]

        tracer = hearth_trace.Tracer()
        mb.resolve_stale_issues(memory_conn, data, tracer, pathway_conn=pconn)

        row = episode_row(glitch_episode)
        if row["resolved"] != 0:
            failures.append(
                f"Scenario 1 FAILED: episode {glitch_episode} was resolved "
                f"(resolved={row['resolved']}, resolution_reason={row['resolution_reason']!r}) "
                "— should have been left open"
            )
        else:
            print(f"  [OK] episode {glitch_episode} correctly left open (resolved=0)")

        logged = any(
            e.rule_name == "training_comment_waiting_unexplained_disappearance"
            and e.reference_key == f"training_comment_waiting_{glitch_comment}"
            for e in tracer._entries
        )
        if not logged:
            failures.append("Scenario 1 FAILED: inconsistency was not logged via the tracer")
        else:
            print("  [OK] inconsistency logged via tracer")

        # =====================================================================
        # Scenario 2 — comment_deleted
        # =====================================================================
        print(f"\n{'#' * 78}\n# Scenario 2 — comment_deleted\n{'#' * 78}")
        del_creator = seed_creator("Deleted")
        del_entity = hearth_memory.get_or_create_entity(memory_conn, del_creator)
        created["hearth_entities"].append(del_entity["id"])
        del_training = seed_training(del_creator, "Deleted")
        del_comment = seed_comment(del_training, del_creator, "Deleted")
        del_episode = make_episode(del_comment, del_entity["id"])

        pconn.execute("DELETE FROM training_comments WHERE id = ?;", (del_comment,))
        pconn.commit()

        data = fresh_data()
        tracer = hearth_trace.Tracer()
        mb.resolve_stale_issues(memory_conn, data, tracer, pathway_conn=pconn)
        row = episode_row(del_episode)
        if not (row["resolved"] == 1 and row["resolution_reason"] == "comment_deleted"):
            failures.append(
                f"Scenario 2 FAILED: resolved={row['resolved']}, "
                f"resolution_reason={row['resolution_reason']!r} (want 1, 'comment_deleted')"
            )
        else:
            print("  [OK] resolved=1, resolution_reason='comment_deleted'")

        # =====================================================================
        # Scenario 3 — acknowledged_externally
        # =====================================================================
        print(f"\n{'#' * 78}\n# Scenario 3 — acknowledged_externally\n{'#' * 78}")
        ack_creator = seed_creator("Ack")
        ack_entity = hearth_memory.get_or_create_entity(memory_conn, ack_creator)
        created["hearth_entities"].append(ack_entity["id"])
        ack_training = seed_training(ack_creator, "Ack")
        ack_comment = seed_comment(ack_training, ack_creator, "Ack")
        ack_episode = make_episode(ack_comment, ack_entity["id"])

        pconn.execute(
            "UPDATE training_comments SET acknowledged_at = ? WHERE id = ?;",
            (_iso(_dt.datetime.utcnow()), ack_comment),
        )
        pconn.commit()

        data = fresh_data()
        tracer = hearth_trace.Tracer()
        mb.resolve_stale_issues(memory_conn, data, tracer, pathway_conn=pconn)
        row = episode_row(ack_episode)
        if not (row["resolved"] == 1 and row["resolution_reason"] == "acknowledged_externally"):
            failures.append(
                f"Scenario 3 FAILED: resolved={row['resolved']}, "
                f"resolution_reason={row['resolution_reason']!r} (want 1, 'acknowledged_externally')"
            )
        else:
            print("  [OK] resolved=1, resolution_reason='acknowledged_externally'")

        # =====================================================================
        # Scenario 4 — staff_reply_received
        # =====================================================================
        print(f"\n{'#' * 78}\n# Scenario 4 — staff_reply_received\n{'#' * 78}")
        reply_creator = seed_creator("Reply")
        reply_entity = hearth_memory.get_or_create_entity(memory_conn, reply_creator)
        created["hearth_entities"].append(reply_entity["id"])
        reply_staff = seed_staff("Reply")
        reply_training = seed_training(reply_creator, "Reply")
        reply_comment = seed_comment(reply_training, reply_creator, "Reply")
        reply_episode = make_episode(reply_comment, reply_entity["id"])

        insert(
            "training_comment_replies", ["comment_id", "user_id", "body", "created_at"],
            (reply_comment, reply_staff, f"Staff reply {MARKER}", _iso(_dt.datetime.utcnow())),
        )

        data = fresh_data()
        tracer = hearth_trace.Tracer()
        mb.resolve_stale_issues(memory_conn, data, tracer, pathway_conn=pconn)
        row = episode_row(reply_episode)
        if not (row["resolved"] == 1 and row["resolution_reason"] == "staff_reply_received"):
            failures.append(
                f"Scenario 4 FAILED: resolved={row['resolved']}, "
                f"resolution_reason={row['resolution_reason']!r} (want 1, 'staff_reply_received')"
            )
        else:
            print("  [OK] resolved=1, resolution_reason='staff_reply_received'")

        # =====================================================================
        # Scenario 5 — later_staff_comment_received
        # =====================================================================
        print(f"\n{'#' * 78}\n# Scenario 5 — later_staff_comment_received\n{'#' * 78}")
        later_creator = seed_creator("Later")
        later_entity = hearth_memory.get_or_create_entity(memory_conn, later_creator)
        created["hearth_entities"].append(later_entity["id"])
        later_staff = seed_staff("Later")
        later_training = seed_training(later_creator, "Later")
        later_comment = seed_comment(later_training, later_creator, "Later", created_days_ago=5)
        later_episode = make_episode(later_comment, later_entity["id"])

        seed_comment(later_training, later_staff, "LaterStaffReply", created_days_ago=1)

        data = fresh_data()
        tracer = hearth_trace.Tracer()
        mb.resolve_stale_issues(memory_conn, data, tracer, pathway_conn=pconn)
        row = episode_row(later_episode)
        if not (row["resolved"] == 1 and row["resolution_reason"] == "later_staff_comment_received"):
            failures.append(
                f"Scenario 5 FAILED: resolved={row['resolved']}, "
                f"resolution_reason={row['resolution_reason']!r} (want 1, 'later_staff_comment_received')"
            )
        else:
            print("  [OK] resolved=1, resolution_reason='later_staff_comment_received'")

        # =====================================================================
        # Scenario 6 — comment_non_actionable (reclassified after the episode
        # was opened)
        # =====================================================================
        print(f"\n{'#' * 78}\n# Scenario 6 — comment_non_actionable\n{'#' * 78}")
        nonact_creator = seed_creator("NonAct")
        nonact_entity = hearth_memory.get_or_create_entity(memory_conn, nonact_creator)
        created["hearth_entities"].append(nonact_entity["id"])
        nonact_training = seed_training(nonact_creator, "NonAct")
        nonact_comment = seed_comment(nonact_training, nonact_creator, "NonAct", comment_type="question")
        nonact_episode = make_episode(nonact_comment, nonact_entity["id"])

        pconn.execute(
            "UPDATE training_comments SET comment_type = 'praise' WHERE id = ?;",
            (nonact_comment,),
        )
        pconn.commit()

        data = fresh_data()
        tracer = hearth_trace.Tracer()
        mb.resolve_stale_issues(memory_conn, data, tracer, pathway_conn=pconn)
        row = episode_row(nonact_episode)
        if not (row["resolved"] == 1 and row["resolution_reason"] == "comment_non_actionable"):
            failures.append(
                f"Scenario 6 FAILED: resolved={row['resolved']}, "
                f"resolution_reason={row['resolution_reason']!r} (want 1, 'comment_non_actionable')"
            )
        else:
            print("  [OK] resolved=1, resolution_reason='comment_non_actionable'")

        # =====================================================================
        # Scenario 7 — creator_deactivated, exercised through the
        # training_comment_waiting six-path check itself (not the general Fix
        # 2 sweep) by simulating the sweep's own deactivation query having
        # failed this run (current_deactivated_user_ids is None) — the
        # per-comment check must still catch it independently.
        # =====================================================================
        print(f"\n{'#' * 78}\n# Scenario 7 — creator_deactivated (six-path check, sweep bypassed)\n{'#' * 78}")
        deact_creator = seed_creator("Deact", status="approved")
        deact_entity = hearth_memory.get_or_create_entity(memory_conn, deact_creator)
        created["hearth_entities"].append(deact_entity["id"])
        deact_training = seed_training(deact_creator, "Deact")
        deact_comment = seed_comment(deact_training, deact_creator, "Deact")
        deact_episode = make_episode(deact_comment, deact_entity["id"])

        pconn.execute("UPDATE users SET status = 'inactive' WHERE id = ?;", (deact_creator,))
        pconn.commit()

        data = fresh_data()
        # Simulate the general deactivation-sweep query having failed this run.
        data["_internal_deactivated_user_ids"] = "[Query not available: simulated failure]"

        tracer = hearth_trace.Tracer()
        mb.resolve_stale_issues(memory_conn, data, tracer, pathway_conn=pconn)
        row = episode_row(deact_episode)
        if not (row["resolved"] == 1 and row["resolution_reason"] == "creator_deactivated"):
            failures.append(
                f"Scenario 7 FAILED: resolved={row['resolved']}, "
                f"resolution_reason={row['resolution_reason']!r} (want 1, 'creator_deactivated')"
            )
        else:
            print("  [OK] resolved=1, resolution_reason='creator_deactivated' (via six-path check)")

        # =====================================================================
        # Scenario 8 — real, deactivated creator's open comment through the
        # NORMAL full pipeline (general Fix 2 sweep active) still correctly
        # resolves as creator_deactivated.
        # =====================================================================
        print(f"\n{'#' * 78}\n# Scenario 8 — creator_deactivated via normal full pipeline\n{'#' * 78}")
        deact2_creator = seed_creator("Deact2", status="approved")
        deact2_entity = hearth_memory.get_or_create_entity(memory_conn, deact2_creator)
        created["hearth_entities"].append(deact2_entity["id"])
        deact2_training = seed_training(deact2_creator, "Deact2")
        deact2_comment = seed_comment(deact2_training, deact2_creator, "Deact2")
        deact2_episode = make_episode(deact2_comment, deact2_entity["id"])

        pconn.execute("UPDATE users SET status = 'inactive' WHERE id = ?;", (deact2_creator,))
        pconn.commit()

        data = fresh_data()  # includes a real, successful _internal_deactivated_user_ids query
        tracer = hearth_trace.Tracer()
        mb.resolve_stale_issues(memory_conn, data, tracer, pathway_conn=pconn)
        row = episode_row(deact2_episode)
        if not (row["resolved"] == 1 and row["resolution_reason"] == "creator_deactivated"):
            failures.append(
                f"Scenario 8 FAILED: resolved={row['resolved']}, "
                f"resolution_reason={row['resolution_reason']!r} (want 1, 'creator_deactivated')"
            )
        else:
            print("  [OK] resolved=1, resolution_reason='creator_deactivated' (via full pipeline)")

        assert not failures, "Failures:\n" + "\n".join(failures)
        print("\nAll training_comment_waiting resolution assertions passed (8 scenarios).")

    finally:
        print("\nCleanup — removing all test-seeded rows")
        for eid in created["hearth_episodes"]:
            memory_conn.execute("DELETE FROM hearth_episodes WHERE id = ?;", (eid,))
        for eid in created["hearth_entities"]:
            memory_conn.execute("DELETE FROM hearth_entities WHERE id = ?;", (eid,))
        memory_conn.commit()
        memory_conn.close()

        pconn.execute(
            "DELETE FROM training_comment_replies WHERE body LIKE ?;", (f"%{MARKER}%",)
        )
        pconn.execute("DELETE FROM training_comments WHERE content LIKE ?;", (f"%{MARKER}%",))
        pconn.execute("DELETE FROM trainings WHERE title LIKE ?;", (f"%{MARKER}%",))
        pconn.execute("DELETE FROM users WHERE email LIKE ?;", (f"%{MARKER}%",))
        pconn.commit()

        remaining = pconn.execute(
            "SELECT COUNT(*) FROM users WHERE email LIKE ?;", (f"%{MARKER}%",)
        ).fetchone()[0]
        print(f"  Remaining test users after cleanup: {remaining}")
        pconn.close()
        print("Cleanup complete.")


if __name__ == "__main__":
    main()
