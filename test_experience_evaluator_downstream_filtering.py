"""
Downstream-containment tests: evaluator-promoted hearth_episodes rows
(episode_type IN {'resolution','momentum','concern'}, reference_key LIKE
'pulse_event_%') must stay excluded by default from every consumer that
treats hearth_episodes as trusted organizational evidence — and remain
retrievable via the explicit include_evaluator_promoted=True opt-in for
tools that deliberately need full visibility.

Covers hearth_memory.get_open_episodes, hearth_memory.get_recent_resolutions,
hearth_manager_advice.get_recent_episodes, and hearth_traversal's episode
count (get_context_pointers) — the consumers identified by the audit that
feed Daily Brief, Soul, and manager-facing surfaces.

Run: venv/bin/python3 test_experience_evaluator_downstream_filtering.py
"""

import experience_evaluator_test_helpers as h
import hearth_manager_advice
import hearth_memory
import hearth_traversal


def _seed(mconn):
    entity_id = h.make_entity(mconn, user_id=1, display_name="Filter Test")
    normal_open_id, _ = hearth_memory.create_episode(
        mconn, entity_id, "checkin_feedback_waiting", "normal open", reference_key="checkin_feedback_1",
    )
    normal_resolved_id, _ = hearth_memory.create_episode(
        mconn, entity_id, "training_comment_waiting", "normal resolved", reference_key="training_comment_waiting_1",
    )
    hearth_memory.resolve_episode(mconn, normal_resolved_id)
    promo_open_id, _ = hearth_memory.create_episode(
        mconn, entity_id, "concern", "promo open", reference_key="pulse_event_500",
        briefing_category="action_needed",
    )
    promo_resolved_id, _ = hearth_memory.create_episode(
        mconn, entity_id, "momentum", "promo resolved", reference_key="pulse_event_501",
        briefing_category="awareness",
    )
    hearth_memory.resolve_episode(mconn, promo_resolved_id)
    return entity_id, normal_open_id, normal_resolved_id, promo_open_id, promo_resolved_id


def test_get_open_episodes_default_excludes_evaluator_promoted():
    mconn, mpath = h.make_memory_db()
    try:
        entity_id, normal_open_id, _, promo_open_id, _ = _seed(mconn)
        default = hearth_memory.get_open_episodes(mconn, entity_id=entity_id)
        included = hearth_memory.get_open_episodes(mconn, entity_id=entity_id, include_evaluator_promoted=True)
        assert {e["id"] for e in default} == {normal_open_id}
        assert {e["id"] for e in included} == {normal_open_id, promo_open_id}
        print("[OK] get_open_episodes excludes evaluator-promoted rows by default, includes with opt-in")
    finally:
        h.cleanup_db(mconn, mpath)


def test_get_recent_resolutions_default_excludes_evaluator_promoted():
    mconn, mpath = h.make_memory_db()
    try:
        entity_id, _, normal_resolved_id, _, promo_resolved_id = _seed(mconn)
        default = hearth_memory.get_recent_resolutions(mconn, hours=999999)
        included = hearth_memory.get_recent_resolutions(mconn, hours=999999, include_evaluator_promoted=True)
        assert {e["id"] for e in default} == {normal_resolved_id}
        assert {e["id"] for e in included} == {normal_resolved_id, promo_resolved_id}
        print("[OK] get_recent_resolutions excludes evaluator-promoted rows by default (Soul responsiveness-belief path)")
    finally:
        h.cleanup_db(mconn, mpath)


def test_manager_advice_get_recent_episodes_excludes_evaluator_promoted():
    mconn, mpath = h.make_memory_db()
    try:
        entity_id, normal_open_id, normal_resolved_id, promo_open_id, promo_resolved_id = _seed(mconn)
        mconn.close()

        original_path = hearth_memory.MEMORY_DB_PATH
        hearth_memory.MEMORY_DB_PATH = mpath
        try:
            result = hearth_manager_advice.get_recent_episodes(entity_id)
        finally:
            hearth_memory.MEMORY_DB_PATH = original_path

        assert result["status"] == "ok"
        ids = {row["id"] for row in result["data"]}
        assert ids == {normal_open_id, normal_resolved_id}, ids
        print("[OK] hearth_manager_advice.get_recent_episodes excludes evaluator-promoted rows")
    finally:
        h.cleanup_db(mconn, mpath)


def test_traversal_episode_count_excludes_evaluator_promoted():
    mconn, mpath = h.make_memory_db()
    try:
        entity_id, normal_open_id, normal_resolved_id, promo_open_id, promo_resolved_id = _seed(mconn)
        pointers = hearth_traversal.get_context_pointers(mconn, [entity_id])
        assert pointers[entity_id]["episode_count"] == 2, (
            f"expected 2 (normal open + normal resolved), got {pointers[entity_id]}"
        )
        print("[OK] hearth_traversal episode_count excludes evaluator-promoted rows")
    finally:
        h.cleanup_db(mconn, mpath)


def main():
    tests = [
        test_get_open_episodes_default_excludes_evaluator_promoted,
        test_get_recent_resolutions_default_excludes_evaluator_promoted,
        test_manager_advice_get_recent_episodes_excludes_evaluator_promoted,
        test_traversal_episode_count_excludes_evaluator_promoted,
    ]
    for t in tests:
        t()
    print(f"\nAll {len(tests)} downstream-filtering tests passed.")


if __name__ == "__main__":
    main()
