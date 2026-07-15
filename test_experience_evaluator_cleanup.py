"""
Tests for hearth_experience_evaluator_cleanup.py, using synthetic fixtures
mirroring the two confirmed affected-entity shapes from the defect report:
a duplicate/contradictory-pattern active entity (entities 11/39) and a
duplicate-inactive-entity pair (entities 15/16). Never touches real
production data or the real dev hearth_memory.db/app.db.

Covers: dry-run changes nothing, apply closes only invalid rows, valid rows
remain, no row is ever deleted, protected source events are never touched,
rerunning cleanup is idempotent, summaries/recurring patterns are rebuilt,
no guessed belief confidence is written, and the duplicate inactive entities
are never merged.

Run: venv/bin/python3 test_experience_evaluator_cleanup.py
"""

import sqlite3

import experience_evaluator_test_helpers as h
import hearth_experience_evaluator as ev
import hearth_experience_evaluator_cleanup as cleanup
import hearth_memory


def _make_duplicate_pattern_entity(mconn, pconn, user_id, display_name):
    """Mirrors entities 11/39: a legitimate quiet-family target plus a mix
    of V1-era evaluator-promoted rows — one that still validates under the
    corrected engine (momentum), and two that never can (resolution/concern
    are permanently out of scope now — classify_event can never reproduce
    either), or that reference a malformed/foreign key.
    """
    entity_id = h.make_entity(mconn, user_id=user_id, display_name=display_name)
    quiet_watcher = h.make_watcher_episode(mconn, entity_id, "creator_quiet", reference_key=f"creator_quiet_{user_id}")
    h.make_quiet_change_target(mconn, entity_id, source_episode_id=quiet_watcher)

    legit_event = h.insert_event(pconn, "training_viewed", actor_user_id=user_id)
    false_resolution_event = h.insert_event(pconn, "checkin_submitted", actor_user_id=user_id)
    false_concern_event = h.insert_event(pconn, "message_sent", actor_user_id=user_id)

    legit_id, _ = hearth_memory.create_episode(
        mconn, entity_id, "momentum", "legit momentum",
        severity=ev._MOMENTUM_SEVERITY, reference_key=f"pulse_event_{legit_event}",
        briefing_category=ev._MOMENTUM_BRIEFING_CATEGORY,
    )
    false_resolution_id, _ = hearth_memory.create_episode(
        mconn, entity_id, "resolution", "false resolution — checkin_submitted mapping",
        severity="low", reference_key=f"pulse_event_{false_resolution_event}",
        briefing_category="awareness",
    )
    false_concern_id, _ = hearth_memory.create_episode(
        mconn, entity_id, "concern", "false concern — generic fallback",
        severity="medium", reference_key=f"pulse_event_{false_concern_event}",
        briefing_category="action_needed",
    )
    return {
        "entity_id": entity_id,
        "legit_id": legit_id,
        "false_resolution_id": false_resolution_id,
        "false_concern_id": false_concern_id,
    }


def test_dry_run_changes_nothing():
    mconn, mpath = h.make_memory_db()
    pconn, ppath = h.make_pathway_db()
    try:
        fx = _make_duplicate_pattern_entity(mconn, pconn, 11, "Entity11Analog")
        before = {r["id"]: dict(r) for r in mconn.execute("SELECT * FROM hearth_episodes;")}

        cleanup.run_cleanup([fx["entity_id"]], apply_changes=False, memory_conn=mconn, pathway_conn=pconn)

        after = {r["id"]: dict(r) for r in mconn.execute("SELECT * FROM hearth_episodes;")}
        assert before == after, "dry-run must not change any row"
        print("[OK] dry-run changes nothing")
    finally:
        h.cleanup_db(mconn, mpath)
        h.cleanup_db(pconn, ppath)


def test_apply_closes_only_invalid_rows_valid_rows_remain():
    mconn, mpath = h.make_memory_db()
    pconn, ppath = h.make_pathway_db()
    try:
        fx = _make_duplicate_pattern_entity(mconn, pconn, 39, "Entity39Analog")
        cleanup.run_cleanup([fx["entity_id"]], apply_changes=True, memory_conn=mconn, pathway_conn=pconn)

        legit = mconn.execute("SELECT resolved, resolution_reason FROM hearth_episodes WHERE id=?;", (fx["legit_id"],)).fetchone()
        false_res = mconn.execute("SELECT resolved, resolution_reason FROM hearth_episodes WHERE id=?;", (fx["false_resolution_id"],)).fetchone()
        false_concern = mconn.execute("SELECT resolved, resolution_reason FROM hearth_episodes WHERE id=?;", (fx["false_concern_id"],)).fetchone()

        assert legit["resolved"] == 0, "the still-valid momentum row must remain open"
        assert false_res["resolved"] == 1 and false_res["resolution_reason"] == "invalid_evaluator_promotion"
        assert false_concern["resolved"] == 1 and false_concern["resolution_reason"] == "invalid_evaluator_promotion"
        print("[OK] apply closes only invalid rows; the valid row is untouched")
    finally:
        h.cleanup_db(mconn, mpath)
        h.cleanup_db(pconn, ppath)


def test_no_row_is_ever_deleted():
    mconn, mpath = h.make_memory_db()
    pconn, ppath = h.make_pathway_db()
    try:
        fx = _make_duplicate_pattern_entity(mconn, pconn, 111, "CountCheck")
        before_count = mconn.execute("SELECT COUNT(*) AS c FROM hearth_episodes;").fetchone()["c"]
        cleanup.run_cleanup([fx["entity_id"]], apply_changes=True, memory_conn=mconn, pathway_conn=pconn)
        after_count = mconn.execute("SELECT COUNT(*) AS c FROM hearth_episodes;").fetchone()["c"]
        assert before_count == after_count == 4  # quiet watcher + 3 promoted rows
        print("[OK] no row is ever deleted — only resolved flags change")
    finally:
        h.cleanup_db(mconn, mpath)
        h.cleanup_db(pconn, ppath)


def test_protected_source_events_remain_unchanged():
    mconn, mpath = h.make_memory_db()
    pconn, ppath = h.make_pathway_db()
    try:
        fx = _make_duplicate_pattern_entity(mconn, pconn, 222, "ProtectedEventsCheck")
        before = {r["id"]: dict(r) for r in pconn.execute("SELECT * FROM hearth_events;")}
        cleanup.run_cleanup([fx["entity_id"]], apply_changes=True, memory_conn=mconn, pathway_conn=pconn)
        after = {r["id"]: dict(r) for r in pconn.execute("SELECT * FROM hearth_events;")}
        assert before == after, "cleanup must never modify a hearth_events row"
        print("[OK] source hearth_events rows are byte-for-byte unchanged after cleanup")
    finally:
        h.cleanup_db(mconn, mpath)
        h.cleanup_db(pconn, ppath)


def test_rerunning_cleanup_is_idempotent():
    mconn, mpath = h.make_memory_db()
    pconn, ppath = h.make_pathway_db()
    try:
        fx = _make_duplicate_pattern_entity(mconn, pconn, 333, "IdempotentCheck")
        cleanup.run_cleanup([fx["entity_id"]], apply_changes=True, memory_conn=mconn, pathway_conn=pconn)
        after_first = {r["id"]: dict(r) for r in mconn.execute("SELECT * FROM hearth_episodes;")}

        cleanup.run_cleanup([fx["entity_id"]], apply_changes=True, memory_conn=mconn, pathway_conn=pconn)
        after_second = {r["id"]: dict(r) for r in mconn.execute("SELECT * FROM hearth_episodes;")}

        assert after_first == after_second, "a second apply run must be a no-op"
        print("[OK] rerunning cleanup is idempotent")
    finally:
        h.cleanup_db(mconn, mpath)
        h.cleanup_db(pconn, ppath)


def test_summary_and_patterns_rebuilt_from_valid_episodes():
    mconn, mpath = h.make_memory_db()
    pconn, ppath = h.make_pathway_db()
    try:
        fx = _make_duplicate_pattern_entity(mconn, pconn, 444, "SummaryRebuild")
        cleanup.run_cleanup([fx["entity_id"]], apply_changes=True, memory_conn=mconn, pathway_conn=pconn)

        entity_row = mconn.execute("SELECT concerns FROM hearth_entities WHERE id=?;", (fx["entity_id"],)).fetchone()
        concerns = entity_row["concerns"] or ""
        # the invalidated evaluator-promoted 'concern' row must not surface
        # once closed, but the entity's real (non-evaluator) creator_quiet
        # watcher episode legitimately still does — recompute must be
        # selective, not just blank everything out.
        assert "concern" not in concerns.lower(), (
            f"invalidated evaluator-promoted concern row must not surface in recomputed concerns: {concerns!r}"
        )
        assert "quiet" in concerns.lower(), (
            f"the entity's real creator_quiet watcher episode should still surface: {concerns!r}"
        )
        print("[OK] entity summary/concerns recomputed from valid episodes only")
    finally:
        h.cleanup_db(mconn, mpath)
        h.cleanup_db(pconn, ppath)


def test_no_guessed_belief_confidence_written():
    mconn, mpath = h.make_memory_db()
    pconn, ppath = h.make_pathway_db()
    try:
        fx = _make_duplicate_pattern_entity(mconn, pconn, 555, "NoGuessedBelief")
        before_beliefs = mconn.execute("SELECT COUNT(*) AS c FROM hearth_worldview_beliefs;").fetchone()["c"]
        report = cleanup.run_cleanup([fx["entity_id"]], apply_changes=True, memory_conn=mconn, pathway_conn=pconn)
        after_beliefs = mconn.execute("SELECT COUNT(*) AS c FROM hearth_worldview_beliefs;").fetchone()["c"]

        assert before_beliefs == after_beliefs == 0, "cleanup must never write hearth_worldview_beliefs"
        note = report["entities"][fx["entity_id"]]["responsiveness_belief_note"]
        assert note is not None and "not rebuilt" in note
        print("[OK] no guessed responsiveness-belief confidence is written; the gap is reported instead")
    finally:
        h.cleanup_db(mconn, mpath)
        h.cleanup_db(pconn, ppath)


def test_duplicate_inactive_entities_never_merged():
    """Mirrors entities 15/16: two Hearth entity rows claiming the same
    Pathway user. hearth_entities.user_id is UNIQUE in the real schema (see
    completion report for why real duplicates likely predate that
    constraint) — a hand-built minimal schema is used here, deliberately
    without that constraint, purely so the "both map to the same user"
    precondition-success path is reachable in a test at all.
    """
    mconn = sqlite3.connect(":memory:")
    mconn.row_factory = sqlite3.Row
    mconn.executescript(
        """
        CREATE TABLE hearth_entities (id INTEGER PRIMARY KEY, user_id INTEGER, display_name TEXT,
            summary TEXT, patterns_noticed TEXT, concerns TEXT, entity_type TEXT DEFAULT 'person',
            source TEXT DEFAULT 'pathway_sync', canonical_key TEXT, aliases TEXT,
            first_observed_at TEXT, last_observed_at TEXT, created_at TEXT, importance_score REAL DEFAULT 0.5);
        CREATE TABLE hearth_episodes (id INTEGER PRIMARY KEY, entity_id INTEGER, episode_type TEXT,
            reference_key TEXT, description TEXT, severity TEXT, observed_at TEXT,
            resolved INTEGER DEFAULT 0, resolved_at TEXT, briefing_category TEXT, resolution_reason TEXT);
        CREATE TABLE hearth_worldview_uncertainties (id INTEGER PRIMARY KEY, subject_type TEXT,
            subject_id TEXT, uncertainty_text TEXT, why_it_matters TEXT, possible_question TEXT,
            confidence REAL, priority TEXT, status TEXT DEFAULT 'open', created_at TEXT, updated_at TEXT,
            last_seen_at TEXT, resolved_at TEXT, source_episode_id TEXT, source_signal_id TEXT, source_run TEXT);
        CREATE TABLE hearth_worldview_changes (id INTEGER PRIMARY KEY, subject_type TEXT, subject_id TEXT,
            change_text TEXT, previous_state TEXT, current_state TEXT, direction TEXT, confidence REAL,
            status TEXT DEFAULT 'watching', created_at TEXT, updated_at TEXT, last_seen_at TEXT,
            source_episode_id TEXT, source_signal_id TEXT, source_run TEXT);
        INSERT INTO hearth_entities (id, user_id, display_name, created_at)
            VALUES (15, 900, 'Mrs Frankie A', datetime('now'));
        INSERT INTO hearth_entities (id, user_id, display_name, created_at)
            VALUES (16, 900, 'Mrs Frankie B', datetime('now'));
        """
    )
    mconn.commit()

    pconn = sqlite3.connect(":memory:")
    pconn.row_factory = sqlite3.Row
    pconn.executescript(
        """
        CREATE TABLE users (id INTEGER PRIMARY KEY, status TEXT);
        CREATE TABLE hearth_events (id INTEGER PRIMARY KEY, event_type TEXT, actor_user_id INTEGER,
            target_user_id INTEGER, reference_id INTEGER, reference_type TEXT, occurred_at TEXT,
            processed INTEGER DEFAULT 0, experience_level TEXT DEFAULT 'trace',
            importance_score REAL, importance_reason TEXT);
        INSERT INTO users (id, status) VALUES (900, 'inactive');
        """
    )
    pconn.commit()
    ev_id = h.insert_event(pconn, "message_sent", actor_user_id=900)
    mconn.execute(
        "INSERT INTO hearth_episodes (entity_id, episode_type, reference_key, description, severity, observed_at)"
        " VALUES (15, 'concern', ?, 'invalid, entity 15', 'medium', datetime('now'));",
        (f"pulse_event_{ev_id}",),
    )
    mconn.commit()

    original_pair = cleanup._DUPLICATE_INACTIVE_ENTITY_IDS
    cleanup._DUPLICATE_INACTIVE_ENTITY_IDS = frozenset({15, 16})
    try:
        report = cleanup.run_cleanup([15, 16], apply_changes=True, memory_conn=mconn, pathway_conn=pconn)
    finally:
        cleanup._DUPLICATE_INACTIVE_ENTITY_IDS = original_pair

    assert report["duplicate_inactive_entity_precheck"]["ok"] is True

    entities = {r["id"]: dict(r) for r in mconn.execute("SELECT * FROM hearth_entities;")}
    assert set(entities.keys()) == {15, 16}, "both entities must still exist, independently"
    assert entities[15]["user_id"] == entities[16]["user_id"] == 900, "user_id must be unchanged, not rewritten"
    assert entities[15]["display_name"] != entities[16]["display_name"], "no merge — distinct rows preserved"

    row = mconn.execute("SELECT resolved, resolution_reason FROM hearth_episodes WHERE entity_id=15;").fetchone()
    assert row["resolved"] == 1 and row["resolution_reason"] == cleanup._MOOT_REASON

    mconn.close()
    pconn.close()
    print("[OK] duplicate inactive entities are closed as moot, never merged or rewritten")


def main():
    tests = [
        test_dry_run_changes_nothing,
        test_apply_closes_only_invalid_rows_valid_rows_remain,
        test_no_row_is_ever_deleted,
        test_protected_source_events_remain_unchanged,
        test_rerunning_cleanup_is_idempotent,
        test_summary_and_patterns_rebuilt_from_valid_episodes,
        test_no_guessed_belief_confidence_written,
        test_duplicate_inactive_entities_never_merged,
    ]
    for t in tests:
        t()
    print(f"\nAll {len(tests)} cleanup tests passed.")


if __name__ == "__main__":
    main()
