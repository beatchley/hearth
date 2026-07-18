"""
Classification tests for hearth_experience_evaluator.py (momentum-only,
governed): one terminal classification per event, unrelated events are
rejected (not silently promoted), multiple candidate targets don't produce
arbitrary matching, target/rule identity is stored, the momentum rule fires
correctly, and concern/resolution detection is structurally impossible —
not just unused — per the permanent momentum-only architecture decision
(see hearth_experience_evaluator.py's module docstring).

Run: venv/bin/python3 test_experience_evaluator_classification.py
"""

import experience_evaluator_test_helpers as h
import hearth_experience_evaluator as ev


def test_one_terminal_classification_per_event_even_if_worldview_later_changes():
    mconn, mpath = h.make_memory_db()
    pconn, ppath = h.make_pathway_db()
    try:
        entity_id = h.make_entity(mconn, user_id=1)
        watcher_id = h.make_watcher_episode(mconn, entity_id, "creator_quiet", reference_key="creator_quiet_1")
        h.make_quiet_change_target(mconn, entity_id, source_episode_id=watcher_id)
        eid = h.insert_event(pconn, "training_viewed", actor_user_id=1)

        r1 = ev.evaluate_recent_signals(pathway_conn=pconn, memory_conn=mconn, promote=True)
        assert r1["promoted"] == 1
        episode_count_after_first = mconn.execute("SELECT COUNT(*) AS c FROM hearth_episodes;").fetchone()["c"]

        # Mutate worldview drastically after the fact — a second run must never
        # touch this already-terminal event again, regardless of what changed.
        mconn.execute("DELETE FROM hearth_worldview_changes;")
        mconn.commit()

        r2 = ev.evaluate_recent_signals(pathway_conn=pconn, memory_conn=mconn, promote=True)
        assert r2["evaluated"] == 0
        episode_count_after_second = mconn.execute("SELECT COUNT(*) AS c FROM hearth_episodes;").fetchone()["c"]
        assert episode_count_after_first == episode_count_after_second
        print("[OK] one terminal classification per event; later worldview changes can't reopen it")
    finally:
        h.cleanup_db(mconn, mpath)
        h.cleanup_db(pconn, ppath)


def test_unrelated_event_is_rejected_not_promoted():
    """Unit-level test of the classification rule, not an integration test
    through evaluate_recent_signals(). Previously this seeded a real
    message_sent event and ran it through the full pipeline — but Layer 3
    of the message_sent privacy fix (see hearth_event_types.py) added an
    event_type IN (SAFE_HEARTH_EVENT_TYPES) filter directly to
    _get_unevaluated_signal_events()'s SQL, which is the *same* 7-value
    allowlist _POSITIVE_ACTIVITY_EVENT_TYPES already used for rule matching
    below. That makes "a real event type that exists in hearth_events but
    isn't positive-activity" impossible to produce end-to-end any more —
    message_sent (previously the only such type ever written) is now
    excluded before evaluate_recent_signals() ever sees it, so rejected_unrelated
    is unreachable via the live pipeline with any real data. The
    classification rule itself is still real code (defense in depth for
    any future SAFE_HEARTH_EVENT_TYPES/_POSITIVE_ACTIVITY_EVENT_TYPES
    divergence), so it's still tested directly here, at the unit level.
    """
    mconn, mpath = h.make_memory_db()
    try:
        entity_id = h.make_entity(mconn, user_id=2)
        watcher_id = h.make_watcher_episode(mconn, entity_id, "creator_quiet", reference_key="creator_quiet_2")
        h.make_quiet_change_target(mconn, entity_id, source_episode_id=watcher_id)

        targets = ev._gather_entity_targets(mconn, entity_id)
        assert targets, "expected a living quiet-family target for this entity"

        fake_event = {"id": 999, "event_type": "some_unallowlisted_type"}
        result = ev._classify_event(fake_event, entity_id, targets, ev._MOMENTUM_RULES)
        assert result["classification"] == "rejected_unrelated"
        assert result["target_type"] is None and result["target_id"] is None
        print("[OK] unrelated event/target combination is rejected, not promoted")
    finally:
        h.cleanup_db(mconn, mpath)


def test_multiple_quiet_targets_resolve_deterministically():
    """An entity can have more than one living quiet-family target at once
    (a creator_quiet_entity watched change AND a new_creator_stuck
    entity_episode uncertainty). A positive-activity event must match the
    same one, every time, across repeated evaluations — never arbitrarily.
    """
    mconn, mpath = h.make_memory_db()
    pconn, ppath = h.make_pathway_db()
    try:
        entity_id = h.make_entity(mconn, user_id=3)
        stuck_watcher = h.make_watcher_episode(mconn, entity_id, "new_creator_stuck", reference_key="new_creator_stuck_3")
        h.make_waiting_target(mconn, entity_id, "new_creator_stuck", source_episode_id=stuck_watcher)
        quiet_watcher = h.make_watcher_episode(mconn, entity_id, "creator_quiet", reference_key="creator_quiet_3")
        h.make_quiet_change_target(mconn, entity_id, source_episode_id=quiet_watcher)

        eids = [h.insert_event(pconn, "training_viewed", actor_user_id=3) for _ in range(5)]

        r = ev.evaluate_recent_signals(pathway_conn=pconn, memory_conn=mconn, promote=False, limit=10)
        assert len(r["candidates"]) == 5
        target_ids = {c["target_id"] for c in r["candidates"]}
        for c in r["candidates"]:
            assert c["classification"] == "momentum"
        assert len(target_ids) == 1, (
            f"every candidate must deterministically match the same target, got {target_ids}"
        )
        print("[OK] multiple quiet-family targets resolve deterministically, never arbitrarily")
    finally:
        h.cleanup_db(mconn, mpath)
        h.cleanup_db(pconn, ppath)


def test_target_and_rule_identity_are_stored():
    mconn, mpath = h.make_memory_db()
    pconn, ppath = h.make_pathway_db()
    try:
        entity_id = h.make_entity(mconn, user_id=4)
        watcher_id = h.make_watcher_episode(mconn, entity_id, "creator_quiet", reference_key="creator_quiet_4")
        h.make_quiet_change_target(mconn, entity_id, source_episode_id=watcher_id)
        eid = h.insert_event(pconn, "training_viewed", actor_user_id=4)

        ev.evaluate_recent_signals(pathway_conn=pconn, memory_conn=mconn, promote=True)

        row = mconn.execute(
            "SELECT * FROM hearth_experience_evaluations WHERE source_event_id=?;", (eid,)
        ).fetchone()
        assert row["target_type"] == "hearth_episode"
        assert row["target_id"] == str(watcher_id)
        assert row["rule_id"] == "momentum_v1_quiet_reactivation"
        assert row["resulting_action"] == "created_episode"
        assert row["resulting_episode_id"] is not None
        print("[OK] exact target type/id and rule identity are durably stored")
    finally:
        h.cleanup_db(mconn, mpath)
        h.cleanup_db(pconn, ppath)


def test_momentum_rule_fires_for_positive_activity_against_quiet_target():
    mconn, mpath = h.make_memory_db()
    pconn, ppath = h.make_pathway_db()
    try:
        entity_id = h.make_entity(mconn, user_id=5)
        watcher_id = h.make_watcher_episode(mconn, entity_id, "new_creator_stuck", reference_key="new_creator_stuck_5")
        h.make_waiting_target(mconn, entity_id, "new_creator_stuck", source_episode_id=watcher_id)
        eid = h.insert_event(pconn, "event_signup_created", actor_user_id=5)

        r = ev.evaluate_recent_signals(pathway_conn=pconn, memory_conn=mconn, promote=False)
        assert len(r["candidates"]) == 1 and r["candidates"][0]["classification"] == "momentum"
        print("[OK] momentum rule fires for a positive-activity event against a quiet-family target")
    finally:
        h.cleanup_db(mconn, mpath)
        h.cleanup_db(pconn, ppath)


def test_resolution_and_concern_are_structurally_impossible():
    """Permanent architecture decision (see hearth_experience_evaluator.py's
    module docstring "Permanent scope" section): concern and resolution
    detection are not just unimplemented, they are structurally impossible
    — classify_event has no code path that can return either. This fires a
    battery of event types, including checkin_submitted (V1's false
    resolution trigger), against an entity with a live quiet-family target
    (so classification isn't trivially "no_match" for everything) and
    confirms every resulting classification, episode, and ledger row is
    limited to momentum/no_match/rejected_unrelated.

    message_sent deliberately removed from this battery: the source query
    now excludes it via SAFE_HEARTH_EVENT_TYPES (see hearth_event_types.py),
    so it's no longer real coverage of anything reaching classify_event —
    see test_unrelated_event_is_rejected_not_promoted for where that
    rejection path is still tested, at the unit level.
    """
    mconn, mpath = h.make_memory_db()
    pconn, ppath = h.make_pathway_db()
    try:
        entity_id = h.make_entity(mconn, user_id=6)
        watcher_id = h.make_watcher_episode(mconn, entity_id, "creator_quiet", reference_key="creator_quiet_6")
        h.make_quiet_change_target(mconn, entity_id, source_episode_id=watcher_id)

        for event_type in ("checkin_submitted", "training_viewed", "battle_requested"):
            h.insert_event(pconn, event_type, actor_user_id=6, reference_id=9001, reference_type="checkin_submission")

        r = ev.evaluate_recent_signals(pathway_conn=pconn, memory_conn=mconn, promote=True, limit=10)
        assert r["evaluated"] == 3

        for c in r["candidates"]:
            assert c["classification"] == "momentum"

        ledger_classifications = {
            row["classification"] for row in mconn.execute("SELECT classification FROM hearth_experience_evaluations;")
        }
        assert ledger_classifications <= {"momentum", "no_match", "rejected_unrelated"}
        assert "resolution" not in ledger_classifications and "concern" not in ledger_classifications

        episode_types = {row["episode_type"] for row in mconn.execute("SELECT episode_type FROM hearth_episodes;")}
        assert "resolution" not in episode_types and "concern" not in episode_types

        watcher_row = mconn.execute("SELECT resolved FROM hearth_episodes WHERE id=?;", (watcher_id,)).fetchone()
        assert watcher_row["resolved"] == 0, "nothing should have resolved the original watcher episode"
        print("[OK] resolution/concern are structurally impossible outcomes, not just empty rule tables")
    finally:
        h.cleanup_db(mconn, mpath)
        h.cleanup_db(pconn, ppath)


def main():
    tests = [
        test_one_terminal_classification_per_event_even_if_worldview_later_changes,
        test_unrelated_event_is_rejected_not_promoted,
        test_multiple_quiet_targets_resolve_deterministically,
        test_target_and_rule_identity_are_stored,
        test_momentum_rule_fires_for_positive_activity_against_quiet_target,
        test_resolution_and_concern_are_structurally_impossible,
    ]
    for t in tests:
        t()
    print(f"\nAll {len(tests)} classification tests passed.")


if __name__ == "__main__":
    main()
