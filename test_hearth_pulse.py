"""
Regression test: Pulse's source query excludes message_sent (and any
non-allowlisted event type) at the SQL level — it must never even reach
classify_event(), not merely be classified as "trace" once loaded.

Root cause under test: hearth_pulse.py's
_process_unprocessed_hearth_events_detailed() used to run
`SELECT * FROM hearth_events WHERE processed = 0`, loading every column of
every row — including message_sent's private actor_user_id/target_user_id/
reference metadata — before an explicit classify_event() branch discarded
it as "trace". The fix applies hearth_event_types.SAFE_HEARTH_EVENT_TYPES
directly in the WHERE clause, so a message_sent row is excluded from the
result set entirely.

Uses experience_evaluator_test_helpers.make_pathway_db() — an isolated,
temp-file sqlite db shaped like the real Pathway app.db's hearth_events
table — and points hearth_pulse.DATABASE_URL at it for the duration of the
test, never touching the real dev app.db or hearth_memory.db.

Run: venv/bin/python3 test_hearth_pulse.py
"""

import sys

import experience_evaluator_test_helpers as h
import hearth_pulse

failures = []


def check(name, cond, msg=""):
    if not cond:
        failures.append(f"FAIL [{name}]" + (f": {msg}" if msg else ""))
        print(f"  FAIL: {name}" + (f" — {msg}" if msg else ""))
    else:
        print(f"  pass: {name}")


def run():
    print("=== message_sent is excluded from Pulse's source query ===")
    pconn, ppath = h.make_pathway_db()

    safe_id_1 = h.insert_event(
        pconn, "training_viewed", actor_user_id=101, processed=0, experience_level="trace",
    )
    private_id = h.insert_event(
        pconn, "message_sent", actor_user_id=101, target_user_id=202,
        reference_id=999, reference_type="private_message", processed=0,
        experience_level="trace",
    )
    safe_id_2 = h.insert_event(
        pconn, "user_signed_in", actor_user_id=101, processed=0, experience_level="trace",
    )
    pconn.close()

    orig_db_url = hearth_pulse.DATABASE_URL
    orig_worldview_flag = hearth_pulse.HEARTH_WORLDVIEW_PULSE_ENABLED
    hearth_pulse.DATABASE_URL = f"sqlite:///{ppath}"
    hearth_pulse.HEARTH_WORLDVIEW_PULSE_ENABLED = False  # avoid touching real hearth_memory.db
    try:
        results = hearth_pulse._process_unprocessed_hearth_events_detailed(limit=100)
    finally:
        hearth_pulse.DATABASE_URL = orig_db_url
        hearth_pulse.HEARTH_WORLDVIEW_PULSE_ENABLED = orig_worldview_flag

    processed_ids = {row[0] for row in results}
    check("both safe events were processed", {safe_id_1, safe_id_2} <= processed_ids,
          f"processed_ids={processed_ids}")
    check("message_sent event was NOT processed (never selected at all)",
          private_id not in processed_ids, f"processed_ids={processed_ids}")
    check("results only contain the two safe events (no extras)",
          processed_ids == {safe_id_1, safe_id_2}, f"processed_ids={processed_ids}")

    # Re-open and confirm the message_sent row was never touched — proves
    # exclusion happened at SELECT time, not just "processed but then
    # ignored downstream."
    verify_conn = hearth_pulse.get_pathway_connection(ppath)
    private_row = verify_conn.execute(
        "SELECT processed, experience_level, importance_score FROM hearth_events WHERE id = ?;",
        (private_id,),
    ).fetchone()
    check("message_sent row still has processed=0 (untouched)",
          private_row["processed"] == 0, dict(private_row))
    check("message_sent row still has default experience_level='trace' (never written by Pulse)",
          private_row["experience_level"] == "trace", dict(private_row))
    check("message_sent row still has importance_score=NULL (never classified)",
          private_row["importance_score"] is None, dict(private_row))

    safe_row = verify_conn.execute(
        "SELECT processed, experience_level FROM hearth_events WHERE id = ?;",
        (safe_id_1,),
    ).fetchone()
    check("safe event WAS marked processed", safe_row["processed"] == 1, dict(safe_row))
    verify_conn.close()

    import os
    for suffix in ("", "-wal", "-shm"):
        candidate = ppath + suffix
        if os.path.exists(candidate):
            os.remove(candidate)

    print("\n" + ("=" * 60))
    if failures:
        print(f"{len(failures)} FAILURE(S):")
        for f in failures:
            print(f"  {f}")
        sys.exit(1)
    else:
        print("All Pulse message_sent-exclusion assertions passed.")


if __name__ == "__main__":
    run()
