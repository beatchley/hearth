"""
Wiring test for hearth_questions.surface_worldview_questions().

Phase 0 finding #6: surface_worldview_questions() was fully built with a
passing smoke test, but nothing in production ever called it — 71 Worldview
uncertainties had accumulated with zero surfaced as manager-facing questions.
This test confirms morning_briefing.run_pipeline() now actually calls it
after hearth_soul.generate_reflection() (see call site in run_pipeline),
and that the HEARTH_WORLDVIEW_QUESTIONS_ENABLED flag still gates its effect
exactly as before this wiring change.

Seeds one real uncertainty under a MARKER-tagged subject_id, then removes it
(and anything it produced) in a finally block.

Run: venv/bin/python3 test_worldview_questions_wiring.py
"""

from dotenv import load_dotenv

load_dotenv()

import hearth_memory
import hearth_questions
import hearth_worldview
import morning_briefing as mb

MARKER = "WVQ_WIRING_TEST"


def main():
    memory_conn = hearth_memory.get_memory_connection()
    hearth_memory.init_tables(memory_conn)
    hearth_questions.ensure_questions_table(memory_conn)
    hearth_worldview.ensure_worldview_tables(memory_conn)

    failures = []
    seeded_uncertainty_id = None
    original_flag = hearth_questions.HEARTH_WORLDVIEW_QUESTIONS_ENABLED
    original_surface_fn = hearth_questions.surface_worldview_questions

    try:
        # =====================================================================
        # Scenario 1 — surface_worldview_questions() is actually invoked
        # during a full run_pipeline() call (the wiring itself).
        # =====================================================================
        print(f"\n{'#' * 78}\n# Scenario 1 — surface_worldview_questions() is called by run_pipeline()\n{'#' * 78}")
        call_count = {"n": 0}

        def _tracking_wrapper(conn, *args, **kwargs):
            call_count["n"] += 1
            return original_surface_fn(conn, *args, **kwargs)

        hearth_questions.surface_worldview_questions = _tracking_wrapper
        try:
            mb.run_pipeline(scan_mode="manual")
        finally:
            hearth_questions.surface_worldview_questions = original_surface_fn

        if call_count["n"] != 1:
            failures.append(f"Scenario 1 FAILED: expected 1 call, got {call_count['n']}")
        else:
            print("  [OK] surface_worldview_questions() called exactly once during run_pipeline()")

        # =====================================================================
        # Scenario 2 — seed a real uncertainty, flag disabled: the pipeline
        # must leave it untouched (existing default off-behavior preserved).
        # =====================================================================
        print(f"\n{'#' * 78}\n# Scenario 2 — flag disabled: seeded uncertainty is not surfaced\n{'#' * 78}")
        seeded_uncertainty_id = hearth_worldview.open_uncertainty(
            memory_conn, subject_type="creator", subject_id=MARKER,
            uncertainty_text=f"{MARKER}: is this creator still engaged with weekly check-ins?",
            why_it_matters="Wiring test coverage for surface_worldview_questions().",
            possible_question=f"{MARKER}: should we check in on this creator?",
            priority="high",
        )

        hearth_questions.HEARTH_WORLDVIEW_QUESTIONS_ENABLED = False
        mb.run_pipeline(scan_mode="manual")

        unc_rows = hearth_worldview.get_open_uncertainties(
            memory_conn, subject_type="creator", subject_id=MARKER,
        )
        question_count = memory_conn.execute(
            "SELECT COUNT(*) FROM hearth_questions WHERE worldview_uncertainty_id = ?;",
            (seeded_uncertainty_id,),
        ).fetchone()[0]
        if not (len(unc_rows) == 1 and unc_rows[0]["status"] == "open" and question_count == 0):
            failures.append(
                f"Scenario 2 FAILED: open_uncertainties={len(unc_rows)} "
                f"question_count={question_count} (expected 1 still-open uncertainty, 0 questions)"
            )
        else:
            print("  [OK] uncertainty remained open, no question created while flag disabled")

        # =====================================================================
        # Scenario 3 — flag enabled: the same seeded uncertainty is surfaced
        # as a real question end-to-end through the full pipeline.
        # =====================================================================
        print(f"\n{'#' * 78}\n# Scenario 3 — flag enabled: seeded uncertainty surfaces end-to-end\n{'#' * 78}")
        hearth_questions.HEARTH_WORLDVIEW_QUESTIONS_ENABLED = True
        mb.run_pipeline(scan_mode="manual")

        question_row = memory_conn.execute(
            "SELECT * FROM hearth_questions WHERE worldview_uncertainty_id = ? AND status = 'open';",
            (seeded_uncertainty_id,),
        ).fetchone()
        unc_row = memory_conn.execute(
            "SELECT status FROM hearth_worldview_uncertainties WHERE id = ?;",
            (seeded_uncertainty_id,),
        ).fetchone()
        expected_text = f"{MARKER}: should we check in on this creator?"
        if question_row is None or question_row["question_text"] != expected_text:
            failures.append(
                f"Scenario 3 FAILED: expected an open question with text {expected_text!r}, "
                f"got {dict(question_row) if question_row else None}"
            )
        elif unc_row["status"] != "question_surfaced":
            failures.append(
                f"Scenario 3 FAILED: expected uncertainty status='question_surfaced', "
                f"got {unc_row['status']!r}"
            )
        else:
            print(
                f"  [OK] question_id={question_row['question_id']} surfaced end-to-end via"
                " run_pipeline(); uncertainty advanced to 'question_surfaced'"
            )

        assert not failures, "Failures:\n" + "\n".join(failures)
        print("\nAll worldview-questions wiring assertions passed (3 scenarios).")

    finally:
        print("\nCleanup — removing all test-seeded rows")
        hearth_questions.surface_worldview_questions = original_surface_fn
        hearth_questions.HEARTH_WORLDVIEW_QUESTIONS_ENABLED = original_flag
        if seeded_uncertainty_id is not None:
            memory_conn.execute(
                "DELETE FROM hearth_questions WHERE worldview_uncertainty_id = ?;",
                (seeded_uncertainty_id,),
            )
            memory_conn.execute(
                "DELETE FROM hearth_worldview_uncertainties WHERE id = ?;",
                (seeded_uncertainty_id,),
            )
            memory_conn.commit()
        memory_conn.close()
        print("Cleanup complete.")


if __name__ == "__main__":
    main()
