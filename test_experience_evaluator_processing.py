"""
Event-processing / durable-ledger tests for hearth_experience_evaluator.py
(V2). Covers: same event repeated with unchanged Worldview, same event after
a living-uncertainty status change, restart safety, failed-evaluation retry,
no-match is not repeatedly reconsidered, and evaluator versioning.

Uses isolated temp-file SQLite databases (experience_evaluator_test_helpers)
— never touches the real dev hearth_memory.db / app.db.

Run: venv/bin/python3 test_experience_evaluator_processing.py
"""

import experience_evaluator_test_helpers as h
import hearth_experience_evaluator as ev


def _ledger_row(mconn, event_id, version=ev.EVALUATOR_VERSION):
    return mconn.execute(
        "SELECT * FROM hearth_experience_evaluations"
        " WHERE source_event_id = ? AND evaluator_version = ?;",
        (event_id, version),
    ).fetchone()


def test_same_event_repeated_unchanged_worldview_stays_stable():
    mconn, mpath = h.make_memory_db()
    pconn, ppath = h.make_pathway_db()
    try:
        entity_id = h.make_entity(mconn, user_id=1)
        # event type doesn't matter for no_match (triggered by missing targets,
        # checked before event_type) — must be a SAFE_HEARTH_EVENT_TYPES member
        # now that the source query filters on it (message_sent no longer
        # reaches evaluation at all; see test_hearth_experience_evaluator.py).
        eid = h.insert_event(pconn, "training_viewed", actor_user_id=1)  # no living targets -> no_match

        r1 = ev.evaluate_recent_signals(pathway_conn=pconn, memory_conn=mconn, promote=False)
        assert r1["evaluated"] == 1 and r1["no_match"] == 1

        r2 = ev.evaluate_recent_signals(pathway_conn=pconn, memory_conn=mconn, promote=False)
        assert r2["evaluated"] == 0, (
            f"a no_match event must never be rescanned once ledgered: {r2}"
        )
        print("[OK] same event, unchanged worldview: ledgered once, never rescanned")
    finally:
        h.cleanup_db(mconn, mpath)
        h.cleanup_db(pconn, ppath)


def test_same_event_after_worldview_status_change_no_reclassification():
    """The V1 bug: an uncertainty moving open -> question_surfaced could
    change classification of an already-pending event on the next run. V2
    fixes this by using get_living_uncertainties (both statuses are living)
    for target discovery — classification must be identical either way.

    creator_quiet_entity targets are "watched changes" (status='watching'),
    not uncertainties, so this scenario is built on the "new_creator_stuck"
    family, which is an entity_episode living uncertainty and does move
    between open/question_surfaced.
    """
    mconn, mpath = h.make_memory_db()
    pconn, ppath = h.make_pathway_db()
    try:
        entity_id = h.make_entity(mconn, user_id=3)
        watcher_id = h.make_watcher_episode(mconn, entity_id, "new_creator_stuck", reference_key="new_creator_stuck_3")
        uid = h.make_waiting_target(mconn, entity_id, "new_creator_stuck", source_episode_id=watcher_id)
        eid = h.insert_event(pconn, "training_viewed", actor_user_id=3)

        r1 = ev.evaluate_recent_signals(pathway_conn=pconn, memory_conn=mconn, promote=False)
        assert len(r1["candidates"]) == 1
        c1 = r1["candidates"][0]

        h.set_uncertainty_status(mconn, uid, "question_surfaced")

        r2 = ev.evaluate_recent_signals(pathway_conn=pconn, memory_conn=mconn, promote=False)
        assert len(r2["candidates"]) == 1, (
            "question_surfaced must still be a living target — event must still classify, "
            f"got {r2}"
        )
        c2 = r2["candidates"][0]
        assert c1["classification"] == c2["classification"] == "momentum"
        assert c1["target_id"] == c2["target_id"] == str(watcher_id)
        print("[OK] open -> question_surfaced does not change classification (root cause #1 fixed)")
    finally:
        h.cleanup_db(mconn, mpath)
        h.cleanup_db(pconn, ppath)


def test_restart_safety_fresh_connections_see_ledger():
    """Simulates a scheduler/process restart: fresh connections to the same
    on-disk DBs must see the same durable state a prior run left behind.
    """
    mconn, mpath = h.make_memory_db()
    pconn, ppath = h.make_pathway_db()
    try:
        entity_id = h.make_entity(mconn, user_id=4)
        watcher_id = h.make_watcher_episode(mconn, entity_id, "creator_quiet", reference_key="creator_quiet_4")
        h.make_quiet_change_target(mconn, entity_id, source_episode_id=watcher_id)
        eid = h.insert_event(pconn, "training_viewed", actor_user_id=4)

        r1 = ev.evaluate_recent_signals(pathway_conn=pconn, memory_conn=mconn, promote=True)
        assert r1["promoted"] == 1
        mconn.close()
        pconn.close()

        # "restart" — brand new connections, no shared Python state at all
        import sqlite3
        mconn2 = sqlite3.connect(mpath)
        mconn2.row_factory = sqlite3.Row
        pconn2 = sqlite3.connect(ppath)
        pconn2.row_factory = sqlite3.Row

        r2 = ev.evaluate_recent_signals(pathway_conn=pconn2, memory_conn=mconn2, promote=True)
        assert r2["evaluated"] == 0, f"already-ledgered event must not be reselected after restart: {r2}"

        episodes = mconn2.execute("SELECT COUNT(*) AS c FROM hearth_episodes WHERE episode_type='momentum';").fetchone()
        assert episodes["c"] == 1, "restart must not duplicate the promoted episode"
        mconn2.close()
        pconn2.close()
        print("[OK] restart safety: fresh connections respect prior ledger state")
    finally:
        h.cleanup_db(mconn, mpath)
        h.cleanup_db(pconn, ppath)


def test_failed_evaluation_retries_safely():
    mconn, mpath = h.make_memory_db()
    pconn, ppath = h.make_pathway_db()
    try:
        entity_id = h.make_entity(mconn, user_id=5)
        watcher_id = h.make_watcher_episode(mconn, entity_id, "creator_quiet", reference_key="creator_quiet_5")
        h.make_quiet_change_target(mconn, entity_id, source_episode_id=watcher_id)
        eid = h.insert_event(pconn, "training_viewed", actor_user_id=5)

        broken_rules = ({"rule_id": "broken_test_rule"},)  # missing required keys -> KeyError
        r1 = ev.evaluate_recent_signals(
            pathway_conn=pconn, memory_conn=mconn, promote=False, momentum_rules=broken_rules,
        )
        assert r1["failed"] == 1 and r1["evaluated"] == 1
        row = _ledger_row(mconn, eid)
        assert row["status"] == "failed_retryable", f"expected failed_retryable, got {dict(row)}"

        # Retry with the real (working) rule table — must pick the event back up.
        r2 = ev.evaluate_recent_signals(pathway_conn=pconn, memory_conn=mconn, promote=False)
        assert r2["evaluated"] == 1, f"a failed_retryable event must be reselected: {r2}"
        assert len(r2["candidates"]) == 1 and r2["candidates"][0]["classification"] == "momentum"
        print("[OK] failed evaluation is retryable and succeeds once the transient cause clears")
    finally:
        h.cleanup_db(mconn, mpath)
        h.cleanup_db(pconn, ppath)


def test_no_match_not_repeatedly_reconsidered():
    mconn, mpath = h.make_memory_db()
    pconn, ppath = h.make_pathway_db()
    try:
        eid = h.insert_event(pconn, "training_viewed", actor_user_id=999)  # no entity exists for user 999
        r1 = ev.evaluate_recent_signals(pathway_conn=pconn, memory_conn=mconn, promote=False)
        assert r1["no_match"] == 1
        row = _ledger_row(mconn, eid)
        assert row["status"] == "processed" and row["classification"] == "no_match"

        r2 = ev.evaluate_recent_signals(pathway_conn=pconn, memory_conn=mconn, promote=False)
        assert r2["evaluated"] == 0
        print("[OK] no_match is ledgered as terminal and never reconsidered")
    finally:
        h.cleanup_db(mconn, mpath)
        h.cleanup_db(pconn, ppath)


def test_version_bump_does_not_auto_replay_history():
    """Decision (follow-up to the V2 defect fix): changing EVALUATOR_VERSION
    must never cause normal event selection to automatically re-surface an
    event that already reached a terminal outcome under an older version.
    New logic applies only to new activity going forward; deliberate
    historical reprocessing (if ever needed) is a separate, explicitly-
    invoked tool a human runs — not something a version bump alone triggers.

    Process an event under version "1", then add a momentum-capable target
    for its entity (so if the event WERE reprocessed under new code/rules,
    it would very likely classify differently — proving the exclusion is
    real, not just incidental to nothing having changed) and bump to
    version "2". Normal selection must still exclude it.
    """
    mconn, mpath = h.make_memory_db()
    pconn, ppath = h.make_pathway_db()
    try:
        entity_id = h.make_entity(mconn, user_id=999)
        eid = h.insert_event(pconn, "training_viewed", actor_user_id=999)  # no target yet -> no_match

        r1 = ev.evaluate_recent_signals(pathway_conn=pconn, memory_conn=mconn, promote=False, evaluator_version="1")
        assert r1["evaluated"] == 1 and r1["no_match"] == 1
        ledger_rows_before = mconn.execute(
            "SELECT evaluator_version, classification FROM hearth_experience_evaluations WHERE source_event_id=?;",
            (eid,),
        ).fetchall()
        assert [(r["evaluator_version"], r["classification"]) for r in ledger_rows_before] == [("1", "no_match")]

        # Give the entity a momentum-capable target after the fact, so
        # worldview state has genuinely changed since the event was first
        # processed — the point is not that this event WOULD reclassify
        # differently, but that no reprocessing attempt happens at all
        # under the new version, confirmed directly against the selection
        # query below.
        watcher_id = h.make_watcher_episode(mconn, entity_id, "creator_quiet", reference_key="creator_quiet_999")
        h.make_quiet_change_target(mconn, entity_id, source_episode_id=watcher_id)

        candidates_under_new_version = ev._get_unevaluated_signal_events(pconn, mconn, 50)
        assert eid not in {e["id"] for e in candidates_under_new_version}, (
            "a version bump must not make an already-processed event newly eligible for selection"
        )

        r2 = ev.evaluate_recent_signals(pathway_conn=pconn, memory_conn=mconn, promote=False, evaluator_version="2")
        assert r2["evaluated"] == 0, (
            f"the version-1 event must NOT be re-selected or re-evaluated just because "
            f"EVALUATOR_VERSION changed, got {r2}"
        )

        ledger_rows_after = mconn.execute(
            "SELECT evaluator_version, classification FROM hearth_experience_evaluations WHERE source_event_id=?;",
            (eid,),
        ).fetchall()
        assert [(r["evaluator_version"], r["classification"]) for r in ledger_rows_after] == [("1", "no_match")], (
            "no new ledger row for the new version should have been written by normal selection"
        )
        print("[OK] a version bump does not automatically replay history — the event stays excluded")
    finally:
        h.cleanup_db(mconn, mpath)
        h.cleanup_db(pconn, ppath)


def main():
    tests = [
        test_same_event_repeated_unchanged_worldview_stays_stable,
        test_same_event_after_worldview_status_change_no_reclassification,
        test_restart_safety_fresh_connections_see_ledger,
        test_failed_evaluation_retries_safely,
        test_no_match_not_repeatedly_reconsidered,
        test_version_bump_does_not_auto_replay_history,
    ]
    for t in tests:
        t()
    print(f"\nAll {len(tests)} processing tests passed.")


if __name__ == "__main__":
    main()
