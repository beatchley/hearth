"""
Deactivated creators lingering in Daily Brief — end-to-end tests for the
three-part fix: Fix 1 (status filtering added to 9 previously-unprotected
morning_briefing.py queries), Fix 2 (general deactivation sweep in
resolve_stale_issues()), and Fix 3 (defense-in-depth filter in
hearth_context.build_context()).

Seeds real rows in Pathway's app.db (the same SQLite file the pathway-portal
dev server uses, located via DATABASE_URL) and in hearth_memory.db, under a
distinct MARKER-suffixed identity, then removes every row it created —
including from every child table it inserted into — in a finally block.

Uses raw sqlite3 (not the Flask app / SQLAlchemy) so this has no dependency
beyond what morning_briefing.py itself already requires.

Run: venv/bin/python3 test_deactivation_fixes.py
"""

import datetime as _dt
import os
import sqlite3
import sys

from dotenv import load_dotenv

load_dotenv()

import hearth_context
import hearth_memory
import morning_briefing as mb

MARKER = "DEACTFIX_TEST"


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


def _hours_ago(n):
    return _dt.datetime.utcnow() - _dt.timedelta(hours=n)


def main():
    db_path = _pathway_db_path()
    pconn = sqlite3.connect(db_path)
    pconn.row_factory = sqlite3.Row
    pconn.execute("PRAGMA foreign_keys = ON;")

    memory_conn = hearth_memory.get_memory_connection()
    hearth_memory.init_tables(memory_conn)

    created = {
        "users": [], "onboarding_records": [], "trainings": [], "training_comments": [],
        "checkins": [], "checkin_submissions": [], "support_threads": [], "battles": [],
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

    try:
        # =====================================================================
        # Seed two parallel users: one we'll deactivate, one that stays
        # approved throughout as a false-positive control.
        # =====================================================================
        today = _dt.date.today().isoformat()
        deact_id = insert(
            "users", ["email", "name", "role", "status", "is_pathway_creator", "joined_on", "created_at"],
            (f"deact_{MARKER}@example.invalid", f"DeactUser{MARKER}", "member", "inactive", 1, today, _iso(_dt.datetime.utcnow())),
        )
        active_id = insert(
            "users", ["email", "name", "role", "status", "is_pathway_creator", "joined_on", "created_at"],
            (f"active_{MARKER}@example.invalid", f"ActiveUser{MARKER}", "member", "approved", 1, today, _iso(_dt.datetime.utcnow())),
        )
        print(f"Seeded users: deact_id={deact_id} (status=inactive), active_id={active_id} (status=approved)")

        def seed_pair_for_both(fn):
            """Run fn(user_id) for both the deactivated and the active user."""
            fn(deact_id)
            fn(active_id)

        # --- query_missing_discord ------------------------------------------
        def seed_missing_discord(uid):
            insert(
                "onboarding_records", ["user_id", "created_by_id", "created_at", "added_discord"],
                (uid, uid, _iso(_dt.datetime.utcnow()), "needed"),
            )
        seed_pair_for_both(seed_missing_discord)

        # --- query_battles_today ----------------------------------------------
        def seed_battle_today(uid):
            insert(
                "battles",
                ["creator_user_id", "opponent_name", "battle_date", "battle_time", "timezone", "created_by", "created_at"],
                (uid, f"Opponent{MARKER}", today, "18:00:00", "CT", uid, _iso(_dt.datetime.utcnow())),
            )
        seed_pair_for_both(seed_battle_today)

        # --- query_training_comments_recent (recent, unacknowledged) --------
        def seed_recent_comment(uid):
            training_id = insert(
                "trainings",
                ["title", "training_type", "for_pathway_creators", "for_shop_creators", "created_by", "created_at"],
                (f"Training-Recent-{MARKER}-{uid}", "written", 1, 0, uid, _iso(_days_ago(2))),
            )
            insert(
                "training_comments", ["training_id", "user_id", "content", "created_at"],
                (training_id, uid, f"Recent comment {MARKER}", _iso(_hours_ago(5))),
            )
        seed_pair_for_both(seed_recent_comment)

        # --- query_trainings_no_engagement (own training, zero comments) ----
        def seed_training_no_engagement(uid):
            insert(
                "trainings",
                ["title", "training_type", "for_pathway_creators", "for_shop_creators", "created_by", "created_at"],
                (f"Training-NoEngagement-{MARKER}-{uid}", "written", 1, 0, uid, _iso(_days_ago(3))),
            )
        seed_pair_for_both(seed_training_no_engagement)

        # --- query_training_comment_waiting (old, classified, no reply) -----
        def seed_waiting_comment(uid):
            training_id = insert(
                "trainings",
                ["title", "training_type", "for_pathway_creators", "for_shop_creators", "created_by", "created_at"],
                (f"Training-Waiting-{MARKER}-{uid}", "written", 1, 0, uid, _iso(_days_ago(10))),
            )
            insert(
                "training_comments", ["training_id", "user_id", "content", "created_at", "comment_type"],
                (training_id, uid, f"Waiting comment {MARKER}", _iso(_days_ago(5)), "question"),
            )
        seed_pair_for_both(seed_waiting_comment)

        # --- query_checkins_not_submitted (Sent + Assigned, 7+ days) ---------
        def seed_checkin_not_submitted(uid):
            checkin_id = insert(
                "checkins", ["title", "status", "created_by_user_id", "created_at", "updated_at"],
                (f"CheckIn-NotSubmitted-{MARKER}-{uid}", "Sent", uid, _iso(_days_ago(10)), _iso(_days_ago(10))),
            )
            insert(
                "checkin_submissions", ["checkin_id", "user_id", "status"],
                (checkin_id, uid, "Assigned"),
            )
        seed_pair_for_both(seed_checkin_not_submitted)

        # --- query_checkin_feedback_waiting (Submitted, old) -----------------
        def seed_checkin_feedback_waiting(uid):
            checkin_id = insert(
                "checkins", ["title", "status", "created_by_user_id", "created_at", "updated_at"],
                (f"CheckIn-FeedbackWaiting-{MARKER}-{uid}", "Sent", uid, _iso(_days_ago(10)), _iso(_days_ago(10))),
            )
            insert(
                "checkin_submissions", ["checkin_id", "user_id", "status", "submitted_at"],
                (checkin_id, uid, "Submitted", _iso(_days_ago(5))),
            )
        seed_pair_for_both(seed_checkin_feedback_waiting)

        # --- query_support_request_waiting (open, old, no reply) ------------
        def seed_support_waiting(uid):
            insert(
                "support_threads", ["creator_id", "subject", "status", "created_at", "updated_at"],
                (uid, f"Support-{MARKER}", "open", _iso(_days_ago(5)), _iso(_days_ago(5))),
            )
        seed_pair_for_both(seed_support_waiting)

        pconn.commit()

        # =====================================================================
        # FIX 1 — confirm all 9 queries exclude the deactivated user, and
        # confirm none of them false-positive-exclude the active user.
        # =====================================================================
        print(f"\n{'#' * 78}\n# FIX 1 — 9-query status filtering\n{'#' * 78}")
        now_utc = _dt.datetime.now(_dt.timezone.utc)
        since_24h = now_utc - _dt.timedelta(hours=24)
        since_48h = now_utc - _dt.timedelta(hours=48)
        since_7d = now_utc - _dt.timedelta(days=7)
        fb_cut = now_utc - _dt.timedelta(days=mb.CHECKIN_FEEDBACK_WAITING_DAYS)
        tc_cut = now_utc - _dt.timedelta(days=mb.TRAINING_COMMENT_WAITING_DAYS)
        sp_cut = now_utc - _dt.timedelta(days=mb.SUPPORT_REQUEST_WAITING_DAYS)

        query_checks = [
            ("query_new_users", lambda: mb.query_new_users(pconn, since_24h), "id"),
            ("query_battles_today", lambda: mb.query_battles_today(pconn, today), "creator_user_id"),
            ("query_training_comments_recent", lambda: mb.query_training_comments_recent(pconn, since_48h), "user_id"),
            ("query_missing_discord", lambda: mb.query_missing_discord(pconn), "id"),
            ("query_trainings_no_engagement", lambda: mb.query_trainings_no_engagement(pconn, since_7d, since_24h), "created_by"),
            ("query_checkins_not_submitted", lambda: mb.query_checkins_not_submitted(pconn), "user_id"),
            ("query_checkin_feedback_waiting", lambda: mb.query_checkin_feedback_waiting(pconn, fb_cut), "user_id"),
            ("query_training_comment_waiting", lambda: mb.query_training_comment_waiting(pconn, tc_cut), "user_id"),
            ("query_support_request_waiting", lambda: mb.query_support_request_waiting(pconn, sp_cut), "creator_id"),
        ]

        fix1_failures = []
        for name, fn, id_field in query_checks:
            label, rows = fn()
            if not isinstance(rows, list):
                fix1_failures.append(f"{name}: query FAILED: {rows}")
                continue
            row_ids = {row[id_field] for row in rows if row[id_field] is not None}
            deact_present = deact_id in row_ids
            active_present = active_id in row_ids
            status = "OK" if (not deact_present and active_present) else "FAIL"
            if status == "FAIL":
                fix1_failures.append(
                    f"{name}: deact_present={deact_present} (want False), "
                    f"active_present={active_present} (want True)"
                )
            print(f"  [{status}] {name}: deact_present={deact_present}, active_present={active_present}")

        assert not fix1_failures, "Fix 1 failures:\n" + "\n".join(fix1_failures)
        print("Fix 1: all 9 queries correctly exclude the deactivated user"
              " and correctly still include the active user.")

        # =====================================================================
        # FIX 2 — general deactivation sweep resolves ALL open episode
        # types for the deactivated user, including a synthetic type not
        # in the 9-query list, with the distinct resolution_reason.
        # =====================================================================
        print(f"\n{'#' * 78}\n# FIX 2 — general deactivation sweep\n{'#' * 78}")

        deact_entity = hearth_memory.get_or_create_entity(memory_conn, deact_id)
        active_entity = hearth_memory.get_or_create_entity(memory_conn, active_id)
        created["hearth_entities"].extend([deact_entity["id"], active_entity["id"]])

        # >=3 real query-list-linked types, plus one synthetic type.
        seed_types = ["missing_discord", "checkin_not_submitted", "training_comment_waiting"]
        synthetic_type = f"synthetic_test_type_{MARKER}"

        deact_episode_ids = []
        for i, ep_type in enumerate(seed_types + [synthetic_type]):
            eid, _action = hearth_memory.create_episode(
                memory_conn, deact_entity["id"], ep_type,
                description=f"{MARKER} deact episode {ep_type}",
                reference_key=f"{MARKER}_deact_{ep_type}_{i}",
            )
            deact_episode_ids.append(eid)
        created["hearth_episodes"].extend(deact_episode_ids)

        # Control: the active user gets an open episode of one of the same
        # types — must NOT be touched by the sweep.
        active_episode_id, _action = hearth_memory.create_episode(
            memory_conn, active_entity["id"], "missing_discord",
            description=f"{MARKER} active episode missing_discord",
            reference_key=f"{MARKER}_active_missing_discord",
        )
        created["hearth_episodes"].append(active_episode_id)

        # Build `data` exactly as collect_data() would, using the real
        # queries above plus the new internal deactivated-ids query — so
        # the sweep and the (unchanged) per-type branches both see
        # realistic, consistent inputs, same as a real pipeline run.
        data = {label: rows for label, rows in (fn() for _, fn, _ in query_checks)}
        data["_internal_deactivated_user_ids"] = mb.query_deactivated_user_ids(pconn)[1]

        tracer = mb.hearth_trace.Tracer()
        mb.resolve_stale_issues(memory_conn, data, tracer)

        def _episode_row(eid):
            return memory_conn.execute("SELECT * FROM hearth_episodes WHERE id = ?;", (eid,)).fetchone()

        fix2_failures = []
        for eid, ep_type in zip(deact_episode_ids, seed_types + [synthetic_type]):
            row = _episode_row(eid)
            if not (row["resolved"] == 1 and row["resolution_reason"] == "creator_deactivated"):
                fix2_failures.append(
                    f"episode {eid} ({ep_type}): resolved={row['resolved']}, "
                    f"resolution_reason={row['resolution_reason']!r} (want resolved=1, reason='creator_deactivated')"
                )
            else:
                print(f"  [OK] {ep_type}: resolved=1, resolution_reason='creator_deactivated'")

        active_row = _episode_row(active_episode_id)
        if active_row["resolved"] != 0:
            fix2_failures.append(
                f"active user's episode {active_episode_id} was resolved unexpectedly "
                f"(resolved={active_row['resolved']}) — false positive"
            )
        else:
            print(f"  [OK] active user's control episode remains open (resolved=0)")

        assert not fix2_failures, "Fix 2 failures:\n" + "\n".join(fix2_failures)
        print("Fix 2: all deactivated-user episodes (3 real types + 1 synthetic type)"
              " resolved with resolution_reason='creator_deactivated'; active user's episode untouched.")

        # =====================================================================
        # FIX 3 — defense-in-depth filter in build_context(), tested in
        # isolation: fabricate open_episodes rows directly (as if Fix 1/2
        # both somehow missed this one — it's still open) and confirm
        # build_context() itself still excludes the deactivated user's
        # episode while keeping the active user's.
        # =====================================================================
        print(f"\n{'#' * 78}\n# FIX 3 — defense-in-depth filter in build_context()\n{'#' * 78}")

        still_open_deact_id, _action = hearth_memory.create_episode(
            memory_conn, deact_entity["id"], "creator_quiet",
            description=f"{MARKER} fix3 bypassed episode", severity="high",
            reference_key=f"{MARKER}_fix3_bypass",
        )
        created["hearth_episodes"].append(still_open_deact_id)
        still_open_active_id, _action = hearth_memory.create_episode(
            memory_conn, active_entity["id"], "creator_quiet",
            description=f"{MARKER} fix3 active episode", severity="high",
            reference_key=f"{MARKER}_fix3_active",
        )
        created["hearth_episodes"].append(still_open_active_id)

        open_episodes = hearth_memory.get_open_episodes(memory_conn)
        # Sanity: both synthetic episodes are genuinely present and open
        # going into build_context() — proving Fix 3 does the exclusion
        # itself, not that there was nothing to filter.
        open_ids = {ep["id"] for ep in open_episodes}
        assert still_open_deact_id in open_ids and still_open_active_id in open_ids

        context = hearth_context.build_context(data={}, open_episodes=open_episodes, memory_conn=None)
        briefed_user_ids = {p.user_id for p in context.person_contexts}

        assert deact_id not in briefed_user_ids, (
            f"Fix 3 FAILED: deactivated user {deact_id} still reached build_context()'s output"
        )
        assert active_id in briefed_user_ids, (
            f"Fix 3 false positive: active user {active_id} was excluded from build_context()'s output"
        )
        print(f"  [OK] deactivated user's still-open episode excluded from build_context() output")
        print(f"  [OK] active user's still-open episode correctly still included")
        print("Fix 3: defense-in-depth filter works in isolation, independent of Fix 1/2 having run.")

        print("\nAll deactivation-fix assertions passed (Fix 1, Fix 2, Fix 3).")

    finally:
        print("\nCleanup — removing all test-seeded rows")
        # Hearth memory side.
        for eid in created["hearth_episodes"]:
            memory_conn.execute("DELETE FROM hearth_episodes WHERE id = ?;", (eid,))
        for eid in created["hearth_entities"]:
            memory_conn.execute("DELETE FROM hearth_entities WHERE id = ?;", (eid,))
        memory_conn.commit()
        memory_conn.close()

        # Pathway side — delete children before parents.
        for table in ("checkin_submissions",):
            pconn.execute(
                f"DELETE FROM {table} WHERE checkin_id IN "
                f"(SELECT id FROM checkins WHERE title LIKE ?);", (f"%{MARKER}%",),
            )
        for table in ("checkins", "training_comments"):
            if table == "training_comments":
                pconn.execute(f"DELETE FROM {table} WHERE content LIKE ?;", (f"%{MARKER}%",))
            else:
                pconn.execute(f"DELETE FROM {table} WHERE title LIKE ?;", (f"%{MARKER}%",))
        pconn.execute("DELETE FROM trainings WHERE title LIKE ?;", (f"%{MARKER}%",))
        pconn.execute("DELETE FROM onboarding_records WHERE user_id IN "
                      "(SELECT id FROM users WHERE email LIKE ?);", (f"%{MARKER}%",))
        pconn.execute("DELETE FROM battles WHERE opponent_name LIKE ?;", (f"%{MARKER}%",))
        pconn.execute("DELETE FROM support_threads WHERE subject LIKE ?;", (f"%{MARKER}%",))
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
