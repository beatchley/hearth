"""
Scheduler-repetition tests for hearth_experience_evaluator.py (V2): repeated
30-minute-tick runs don't create duplicates, overlapping/concurrent runs
don't create duplicates, and a crash between classification and the episode
action is replay-safe.

Run: venv/bin/python3 test_experience_evaluator_scheduler_repetition.py
"""

import sqlite3

import experience_evaluator_test_helpers as h
import hearth_memory
import hearth_experience_evaluator as ev


def test_repeated_scheduler_runs_do_not_duplicate():
    mconn, mpath = h.make_memory_db()
    pconn, ppath = h.make_pathway_db()
    try:
        entity_id = h.make_entity(mconn, user_id=1)
        watcher_id = h.make_watcher_episode(mconn, entity_id, "creator_quiet", reference_key="creator_quiet_1")
        h.make_quiet_change_target(mconn, entity_id, source_episode_id=watcher_id)
        h.insert_event(pconn, "training_viewed", actor_user_id=1)

        results = [
            ev.evaluate_recent_signals(pathway_conn=pconn, memory_conn=mconn, promote=True)
            for _ in range(5)  # simulate five consecutive 30-minute ticks
        ]
        assert results[0]["promoted"] == 1
        assert all(r["promoted"] == 0 for r in results[1:]), results

        promoted_count = mconn.execute(
            "SELECT COUNT(*) AS c FROM hearth_episodes WHERE episode_type='momentum';"
        ).fetchone()["c"]
        assert promoted_count == 1
        print("[OK] five repeated scheduler ticks produce exactly one promoted episode")
    finally:
        h.cleanup_db(mconn, mpath)
        h.cleanup_db(pconn, ppath)


def test_overlapping_runs_do_not_duplicate():
    """Approximates two overlapping scheduler runs: two independent
    connections to the same on-disk DBs both compute their eligible-event
    list before either has written a ledger row for it (the actual
    overlapping-read window a real concurrent run would have), then both
    proceed to classify and act. SQLite serializes the writes; the ledger's
    UNIQUE(source_event_id, evaluator_version) upsert and hearth_episodes'
    hardened (episode_type, reference_key) partial unique index must still
    converge to exactly one outcome.
    """
    mconn, mpath = h.make_memory_db()
    pconn, ppath = h.make_pathway_db()
    try:
        entity_id = h.make_entity(mconn, user_id=2)
        watcher_id = h.make_watcher_episode(mconn, entity_id, "creator_quiet", reference_key="creator_quiet_2")
        h.make_quiet_change_target(mconn, entity_id, source_episode_id=watcher_id)
        eid = h.insert_event(pconn, "training_viewed", actor_user_id=2)

        mconn_a = sqlite3.connect(mpath)
        mconn_a.row_factory = sqlite3.Row
        mconn_b = sqlite3.connect(mpath)
        mconn_b.row_factory = sqlite3.Row
        pconn_a = sqlite3.connect(f"file:{ppath}?mode=ro", uri=True)
        pconn_a.row_factory = sqlite3.Row
        pconn_b = sqlite3.connect(f"file:{ppath}?mode=ro", uri=True)
        pconn_b.row_factory = sqlite3.Row

        # Both "runs" see the event is eligible before either writes.
        events_a = ev._get_unevaluated_signal_events(pconn_a, mconn_a, 50)
        events_b = ev._get_unevaluated_signal_events(pconn_b, mconn_b, 50)
        assert len(events_a) == 1 and len(events_b) == 1

        r_a = ev.evaluate_recent_signals(pathway_conn=pconn_a, memory_conn=mconn_a, promote=True)
        r_b = ev.evaluate_recent_signals(pathway_conn=pconn_b, memory_conn=mconn_b, promote=True)

        for c in (mconn_a, mconn_b, pconn_a, pconn_b):
            c.close()

        mconn_check = sqlite3.connect(mpath)
        mconn_check.row_factory = sqlite3.Row
        promoted_count = mconn_check.execute(
            "SELECT COUNT(*) AS c FROM hearth_episodes WHERE episode_type='momentum';"
        ).fetchone()["c"]
        ledger_count = mconn_check.execute(
            "SELECT COUNT(*) AS c FROM hearth_experience_evaluations WHERE source_event_id=?;", (eid,)
        ).fetchone()["c"]
        mconn_check.close()

        assert promoted_count == 1, f"two overlapping runs must converge to one episode, got {promoted_count}"
        assert ledger_count == 1, f"the ledger must hold exactly one row per (event, version), got {ledger_count}"
        print(f"[OK] overlapping runs converge to one outcome (run_a promoted={r_a['promoted']}, run_b promoted={r_b['promoted']})")
    finally:
        h.cleanup_db(mconn, mpath)
        h.cleanup_db(pconn, ppath)


def test_crash_between_classification_and_action_is_replay_safe():
    """Simulates a process crash after classification succeeds but before
    the write action (create_episode) completes, by monkeypatching
    hearth_memory.create_episode to raise exactly once.
    """
    mconn, mpath = h.make_memory_db()
    pconn, ppath = h.make_pathway_db()
    try:
        entity_id = h.make_entity(mconn, user_id=3)
        watcher_id = h.make_watcher_episode(mconn, entity_id, "creator_quiet", reference_key="creator_quiet_3")
        h.make_quiet_change_target(mconn, entity_id, source_episode_id=watcher_id)
        eid = h.insert_event(pconn, "training_viewed", actor_user_id=3)

        real_create_episode = hearth_memory.create_episode
        call_count = {"n": 0}

        def _flaky_create_episode(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("simulated crash mid-action")
            return real_create_episode(*args, **kwargs)

        hearth_memory.create_episode = _flaky_create_episode
        try:
            r1 = ev.evaluate_recent_signals(pathway_conn=pconn, memory_conn=mconn, promote=True)
            assert r1["failed"] == 1 and r1["promoted"] == 0

            row = mconn.execute(
                "SELECT status FROM hearth_experience_evaluations WHERE source_event_id=?;", (eid,)
            ).fetchone()
            assert row["status"] == "failed_retryable"

            r2 = ev.evaluate_recent_signals(pathway_conn=pconn, memory_conn=mconn, promote=True)
            assert r2["promoted"] == 1, f"the crashed action must retry safely and succeed, got {r2}"
        finally:
            hearth_memory.create_episode = real_create_episode

        promoted_count = mconn.execute(
            "SELECT COUNT(*) AS c FROM hearth_episodes WHERE episode_type='momentum';"
        ).fetchone()["c"]
        assert promoted_count == 1, "exactly one episode must exist after the retried action succeeds"
        print("[OK] crash between classification and action is replay-safe, no duplicate episode")
    finally:
        h.cleanup_db(mconn, mpath)
        h.cleanup_db(pconn, ppath)


def main():
    tests = [
        test_repeated_scheduler_runs_do_not_duplicate,
        test_overlapping_runs_do_not_duplicate,
        test_crash_between_classification_and_action_is_replay_safe,
    ]
    for t in tests:
        t()
    print(f"\nAll {len(tests)} scheduler-repetition tests passed.")


if __name__ == "__main__":
    main()
