"""
Tests for the open-questions auto-resolution fix (hearth_soul.py's "Question
auto-resolution" section, wired into morning_briefing.run_pipeline() and
into hearth_question_resolution_cleanup.py for the one-time backlog sweep).

Root cause under test: a question surfaced from a worldview uncertainty used
to stay open forever once created, even after the episode(s) that triggered
it resolved. Neither question type has reliable per-episode provenance
(entity_episode uncertainties only ever store the FIRST triggering episode's
id; entity/repeat-volume uncertainties have no per-episode link at all), so
resolution recomputes the current qualifying condition fresh from live open
episodes rather than trusting historical provenance.

Covers the exact real-world scenario found in production (entity 11 /
magicathomemama: two checkin_feedback_waiting episodes shared one question;
one resolved, one didn't — a question must stay open until ALL matching
open episodes for that entity+type clear), the entity/repeat-volume
threshold-crossing case, human-answered questions being left untouched,
unparseable/unknown subjects being skipped without crashing, and the
cleanup utility's dry-run/apply/idempotent/backup behavior.

Never touches the real dev hearth_memory.db — uses isolated temp-file
databases via experience_evaluator_test_helpers.py.

Run: venv/bin/python3 test_question_auto_resolution.py
"""

import os
import tempfile

import experience_evaluator_test_helpers as h
import hearth_memory
import hearth_question_resolution_cleanup as cleanup
import hearth_questions
import hearth_soul
import hearth_worldview


def _make_db():
    """make_memory_db() plus hearth_questions' own table, which the shared
    fixture helper doesn't create (hearth_questions.py isn't part of the
    Experience Evaluator suite it was built for).
    """
    mconn, mpath = h.make_memory_db()
    hearth_questions.ensure_questions_table(mconn)
    return mconn, mpath


def _make_entity_episode_question(mconn, entity_id, episode_type, reference_keys):
    """Create N open episodes of episode_type for entity_id (one per
    reference_key, so they're distinct rows — mirrors real watchers like
    detect_checkin_feedback_waiting, which key on submission_id), an
    entity_episode uncertainty for them, and the question surfaced from it.
    Returns (question_id, uncertainty_id, [episode_ids]).
    """
    episode_ids = [
        h.make_watcher_episode(mconn, entity_id, episode_type, reference_key=ref)
        for ref in reference_keys
    ]
    subject_id = f"{episode_type}:{entity_id}"
    uncertainty_id, _created = hearth_worldview.upsert_uncertainty(
        mconn, subject_type="entity_episode", subject_id=subject_id,
        uncertainty_text=f"synthetic {episode_type} uncertainty for entity {entity_id}",
        possible_question=f"Is the {episode_type} episode for entity {entity_id} a pattern?",
    )
    question_id = hearth_questions.create_or_update_worldview_question(
        mconn, uncertainty_id, f"Is the {episode_type} episode for entity {entity_id} a pattern?",
    )
    return question_id, uncertainty_id, episode_ids


def _make_entity_volume_question(mconn, entity_id, episode_types):
    """Create one open episode per episode_type for entity_id (distinct
    types so create_episode's no-reference_key dedup doesn't collapse them),
    an entity (repeat-volume) uncertainty, and its surfaced question.
    Returns (question_id, uncertainty_id, [episode_ids]).
    """
    episode_ids = [
        h.make_watcher_episode(mconn, entity_id, et) for et in episode_types
    ]
    subject_id = str(entity_id)
    uncertainty_id, _created = hearth_worldview.upsert_uncertainty(
        mconn, subject_type="entity", subject_id=subject_id,
        uncertainty_text=f"synthetic repeat-concern uncertainty for entity {entity_id}",
        possible_question=f"Is entity {entity_id}'s recent concern volume expected?",
    )
    question_id = hearth_questions.create_or_update_worldview_question(
        mconn, uncertainty_id, f"Is entity {entity_id}'s recent concern volume expected?",
    )
    return question_id, uncertainty_id, episode_ids


def _question_status(mconn, question_id):
    row = mconn.execute(
        "SELECT status, resolution_reason, resolved_at FROM hearth_questions WHERE question_id = ?;",
        (question_id,),
    ).fetchone()
    return dict(row)


def _result_for(results, question_id):
    matches = [r for r in results if r["question_id"] == question_id]
    assert len(matches) == 1, f"expected exactly one result for question_id={question_id}, got {matches}"
    return matches[0]


# ---------------------------------------------------------------------------
# entity_episode — magicathomemama-style partial resolution
# ---------------------------------------------------------------------------

def test_entity_episode_partial_resolution_stays_open():
    mconn, mpath = _make_db()
    try:
        entity_id = h.make_entity(mconn, user_id=11, display_name="magicathomemama-analog")
        question_id, uncertainty_id, episode_ids = _make_entity_episode_question(
            mconn, entity_id, "checkin_feedback_waiting", ["checkin_feedback_101", "checkin_feedback_102"],
        )

        hearth_memory.resolve_episode(mconn, episode_ids[0])

        results = hearth_soul.resolve_cleared_worldview_questions(mconn)
        result = _result_for(results, question_id)
        assert result["action"] == "keep", f"expected 'keep' with one episode still open, got {result}"

        status = _question_status(mconn, question_id)
        assert status["status"] == "open", "question must stay open while one matching episode is still open"
        assert status["resolution_reason"] is None
        print("[OK] entity_episode question stays open when one of two matching episodes is still open")
    finally:
        h.cleanup_db(mconn, mpath)


def test_entity_episode_full_resolution_auto_resolves():
    mconn, mpath = _make_db()
    try:
        entity_id = h.make_entity(mconn, user_id=11, display_name="magicathomemama-analog")
        question_id, uncertainty_id, episode_ids = _make_entity_episode_question(
            mconn, entity_id, "checkin_feedback_waiting", ["checkin_feedback_201", "checkin_feedback_202"],
        )

        hearth_memory.resolve_episode(mconn, episode_ids[0])
        hearth_soul.resolve_cleared_worldview_questions(mconn)
        assert _question_status(mconn, question_id)["status"] == "open", "sanity: still open after first resolve"

        hearth_memory.resolve_episode(mconn, episode_ids[1])
        results = hearth_soul.resolve_cleared_worldview_questions(mconn)
        result = _result_for(results, question_id)
        assert result["action"] == "resolve", f"expected 'resolve' once both episodes are closed, got {result}"

        status = _question_status(mconn, question_id)
        assert status["status"] == "dismissed"
        assert status["resolution_reason"] == hearth_soul.QUESTION_AUTO_RESOLVE_REASON
        assert status["resolved_at"] is not None

        uncertainty = hearth_worldview.get_uncertainty(mconn, uncertainty_id)
        assert uncertainty["status"] == "resolved", "linked uncertainty should also be resolved"
        print("[OK] entity_episode question auto-resolves once both matching episodes are closed")
    finally:
        h.cleanup_db(mconn, mpath)


# ---------------------------------------------------------------------------
# entity — repeat-volume threshold crossing/clearing
# ---------------------------------------------------------------------------

def test_entity_volume_threshold_stays_open_at_threshold():
    mconn, mpath = _make_db()
    try:
        entity_id = h.make_entity(mconn, user_id=21, display_name="volume-analog")
        question_id, uncertainty_id, episode_ids = _make_entity_volume_question(
            mconn, entity_id, ["training_comment_waiting", "support_request_waiting"],
        )
        assert len(episode_ids) == 2, "sanity: two distinct open episodes for this entity"

        results = hearth_soul.resolve_cleared_worldview_questions(mconn)
        result = _result_for(results, question_id)
        assert result["action"] == "keep", f"expected 'keep' at count==threshold(2), got {result}"
        assert _question_status(mconn, question_id)["status"] == "open"
        print("[OK] entity-volume question stays open while count is still at/above threshold")
    finally:
        h.cleanup_db(mconn, mpath)


def test_entity_volume_auto_resolves_below_threshold():
    mconn, mpath = _make_db()
    try:
        entity_id = h.make_entity(mconn, user_id=22, display_name="volume-analog-2")
        question_id, uncertainty_id, episode_ids = _make_entity_volume_question(
            mconn, entity_id, ["training_comment_waiting", "support_request_waiting"],
        )

        hearth_memory.resolve_episode(mconn, episode_ids[0])

        results = hearth_soul.resolve_cleared_worldview_questions(mconn)
        result = _result_for(results, question_id)
        assert result["action"] == "resolve", f"expected 'resolve' once count drops below threshold, got {result}"

        status = _question_status(mconn, question_id)
        assert status["status"] == "dismissed"
        assert status["resolution_reason"] == hearth_soul.QUESTION_AUTO_RESOLVE_REASON
        print("[OK] entity-volume question auto-resolves once count drops below threshold (2)")
    finally:
        h.cleanup_db(mconn, mpath)


# ---------------------------------------------------------------------------
# Safety: human-answered questions and unparseable subjects
# ---------------------------------------------------------------------------

def test_human_answered_question_never_touched():
    mconn, mpath = _make_db()
    try:
        entity_id = h.make_entity(mconn, user_id=31, display_name="answered-analog")
        question_id, uncertainty_id, episode_ids = _make_entity_episode_question(
            mconn, entity_id, "checkin_feedback_waiting", ["checkin_feedback_301"],
        )
        hearth_questions.mark_question_answered(mconn, question_id)
        before = _question_status(mconn, question_id)
        assert before["status"] == "answered"

        # Even though the episode never resolved (condition would still hold
        # anyway), the real point of this test is that an answered question
        # must never be reconsidered at all.
        results = hearth_soul.resolve_cleared_worldview_questions(mconn)
        touched = [r for r in results if r["question_id"] == question_id]
        assert touched == [], "an answered question must not appear in the auto-resolution pass at all"

        after = _question_status(mconn, question_id)
        assert after == before, "answered question row must be completely unchanged"
        print("[OK] a question a human already answered is never reconsidered or touched")
    finally:
        h.cleanup_db(mconn, mpath)


def test_unparseable_subject_id_skipped_not_crashed():
    mconn, mpath = _make_db()
    try:
        # entity_episode subject_id missing the "type:entity_id" shape entirely.
        uncertainty_id = hearth_worldview.upsert_uncertainty(
            mconn, subject_type="entity_episode", subject_id="not_a_valid_shape",
            uncertainty_text="malformed subject_id uncertainty",
        )[0]
        question_id = hearth_questions.create_or_update_worldview_question(
            mconn, uncertainty_id, "malformed subject_id question",
        )

        results = hearth_soul.resolve_cleared_worldview_questions(mconn)
        result = _result_for(results, question_id)
        assert result["action"] == "skip", f"expected 'skip' for unparseable subject_id, got {result}"
        assert _question_status(mconn, question_id)["status"] == "open", "must be left untouched, not closed"
        print("[OK] unparseable entity_episode subject_id is skipped, not crashed on or closed")
    finally:
        h.cleanup_db(mconn, mpath)


def test_unknown_subject_type_skipped_not_crashed():
    mconn, mpath = _make_db()
    try:
        # subject_type='creator' is a real shape used elsewhere in this codebase
        # (see hearth_questions.py's own smoke test) but has no recomputable
        # condition defined for it — must be left alone, not guessed at.
        uncertainty_id = hearth_worldview.upsert_uncertainty(
            mconn, subject_type="creator", subject_id="some_creator_key",
            uncertainty_text="unknown subject_type uncertainty",
        )[0]
        question_id = hearth_questions.create_or_update_worldview_question(
            mconn, uncertainty_id, "unknown subject_type question",
        )

        results = hearth_soul.resolve_cleared_worldview_questions(mconn)
        result = _result_for(results, question_id)
        assert result["action"] == "skip", f"expected 'skip' for unknown subject_type, got {result}"
        assert _question_status(mconn, question_id)["status"] == "open"
        print("[OK] unknown subject_type is skipped, not crashed on or closed")
    finally:
        h.cleanup_db(mconn, mpath)


# ---------------------------------------------------------------------------
# Cleanup utility
# ---------------------------------------------------------------------------

def _seed_mixed_backlog(mconn):
    """One question that should resolve, one that should stay open, one
    that should be skipped as unparseable. Returns dict of question_ids.
    """
    resolve_entity = h.make_entity(mconn, user_id=41, display_name="cleanup-resolve")
    resolve_qid, _u, resolve_eps = _make_entity_episode_question(
        mconn, resolve_entity, "checkin_feedback_waiting", ["checkin_feedback_401"],
    )
    hearth_memory.resolve_episode(mconn, resolve_eps[0])

    keep_entity = h.make_entity(mconn, user_id=42, display_name="cleanup-keep")
    keep_qid, _u2, _eps2 = _make_entity_episode_question(
        mconn, keep_entity, "checkin_feedback_waiting", ["checkin_feedback_402"],
    )

    skip_uncertainty_id = hearth_worldview.upsert_uncertainty(
        mconn, subject_type="entity_episode", subject_id="garbage",
        uncertainty_text="cleanup skip case",
    )[0]
    skip_qid = hearth_questions.create_or_update_worldview_question(
        mconn, skip_uncertainty_id, "cleanup skip question",
    )

    return {"resolve": resolve_qid, "keep": keep_qid, "skip": skip_qid}


def test_cleanup_dry_run_changes_nothing():
    mconn, mpath = _make_db()
    try:
        qids = _seed_mixed_backlog(mconn)
        before = {r["question_id"]: dict(r) for r in mconn.execute("SELECT * FROM hearth_questions;")}

        report = cleanup.run_cleanup(mconn, apply_changes=False)

        after = {r["question_id"]: dict(r) for r in mconn.execute("SELECT * FROM hearth_questions;")}
        assert before == after, "dry-run must not change any hearth_questions row"
        assert report["counts"] == {"resolve": 1, "keep": 1, "skip": 1}
        print("[OK] cleanup dry-run changes nothing")
    finally:
        h.cleanup_db(mconn, mpath)


def test_cleanup_apply_closes_only_qualifying_rows():
    mconn, mpath = _make_db()
    try:
        qids = _seed_mixed_backlog(mconn)

        report = cleanup.run_cleanup(mconn, apply_changes=True)
        assert report["counts"] == {"resolve": 1, "keep": 1, "skip": 1}

        resolved = _question_status(mconn, qids["resolve"])
        kept = _question_status(mconn, qids["keep"])
        skipped = _question_status(mconn, qids["skip"])

        assert resolved["status"] == "dismissed" and resolved["resolution_reason"] == hearth_soul.QUESTION_AUTO_RESOLVE_REASON
        assert kept["status"] == "open"
        assert skipped["status"] == "open"
        print("[OK] cleanup --apply closes only the qualifying row; keep/skip rows untouched")
    finally:
        h.cleanup_db(mconn, mpath)


def test_cleanup_rerun_is_idempotent():
    mconn, mpath = _make_db()
    try:
        qids = _seed_mixed_backlog(mconn)
        cleanup.run_cleanup(mconn, apply_changes=True)
        after_first = {r["question_id"]: dict(r) for r in mconn.execute("SELECT * FROM hearth_questions;")}

        report_second = cleanup.run_cleanup(mconn, apply_changes=True)
        after_second = {r["question_id"]: dict(r) for r in mconn.execute("SELECT * FROM hearth_questions;")}

        assert after_first == after_second, "rerunning apply must be a no-op the second time"
        # Second pass: the resolved question is no longer 'open' so it's not even
        # considered; only keep/skip remain in the open-worldview-question backlog.
        assert report_second["counts"] == {"resolve": 0, "keep": 1, "skip": 1}
        print("[OK] cleanup rerun is idempotent")
    finally:
        h.cleanup_db(mconn, mpath)


def test_cleanup_backup_created_before_apply():
    mconn, mpath = _make_db()
    mconn.close()  # backup copies the file directly; release our handle first
    try:
        backup_path = cleanup.backup_memory_db(mpath)
        assert os.path.exists(backup_path), "backup file must be created"
        assert os.path.dirname(backup_path).endswith("backups")
        with open(mpath, "rb") as f_orig, open(backup_path, "rb") as f_backup:
            assert f_orig.read() == f_backup.read(), "backup must be a byte-identical copy"
        print("[OK] cleanup creates a byte-identical backup before apply")
    finally:
        backups_dir = os.path.join(os.path.dirname(os.path.abspath(mpath)), "backups")
        if os.path.exists(backup_path):
            os.remove(backup_path)
        if os.path.isdir(backups_dir) and not os.listdir(backups_dir):
            os.rmdir(backups_dir)
        for suffix in ("", "-wal", "-shm"):
            candidate = mpath + suffix
            if os.path.exists(candidate):
                os.remove(candidate)


def main():
    tests = [
        test_entity_episode_partial_resolution_stays_open,
        test_entity_episode_full_resolution_auto_resolves,
        test_entity_volume_threshold_stays_open_at_threshold,
        test_entity_volume_auto_resolves_below_threshold,
        test_human_answered_question_never_touched,
        test_unparseable_subject_id_skipped_not_crashed,
        test_unknown_subject_type_skipped_not_crashed,
        test_cleanup_dry_run_changes_nothing,
        test_cleanup_apply_closes_only_qualifying_rows,
        test_cleanup_rerun_is_idempotent,
        test_cleanup_backup_created_before_apply,
    ]
    for t in tests:
        t()
    print(f"\nAll {len(tests)} question auto-resolution tests passed.")


if __name__ == "__main__":
    main()
