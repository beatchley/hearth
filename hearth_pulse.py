"""
Hearth Pulse V1 — processor stub.

Reads unprocessed hearth_events from the Pathway DB and marks them processed.
Does not yet classify or act on events; full classification logic is a V1.1 build.

Not wired into the scheduler or run_pipeline() yet. Pathway's morning_briefing.py
connections are opened read-only (mode=ro); this stub expects a read-write
connection to be passed in by whatever future caller wires it up.
"""


def process_unprocessed_hearth_events(pathway_conn, limit=100):
    rows = pathway_conn.execute(
        "SELECT * FROM hearth_events WHERE processed=0 ORDER BY occurred_at ASC LIMIT ?",
        (limit,)
    ).fetchall()

    for row in rows:
        # V1: mark processed only — classification in next build
        pathway_conn.execute(
            "UPDATE hearth_events SET processed=1 WHERE id=?",
            (row["id"],)
        )

    pathway_conn.commit()
    return len(rows)
