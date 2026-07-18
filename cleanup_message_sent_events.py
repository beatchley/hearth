"""
ONE-TIME DATA CLEANUP — not a migration, not meant to be run on a schedule
or wired into any startup path. Run once against production, confirm the
summary count looks right, and retire this script.

Background: a Codex investigation confirmed hearth_pulse.py's
_process_unprocessed_hearth_events_detailed() ran `SELECT * FROM
hearth_events WHERE processed = 0`, loading every column of every row —
including message_sent events, which carry actor_user_id, target_user_id,
a private_message reference, and a timestamp — before classifying them as
"trace" and discarding them. This is a confirmed HEARTH_SENSORY_POLICY.md
Category B violation: Hearth must never observe private message metadata,
even transiently, even if nothing downstream ever surfaced it to a human.

This is Layer 2 of a three-layer fix:
  Layer 1 — the DM-send route (backend/app/routes/main.py) no longer calls
            record_hearth_event(event_type="message_sent", ...) at all.
            Confirmed the only call site; grepped thoroughly for others.
  Layer 2 — THIS SCRIPT: removes message_sent rows that already exist in
            hearth_events from before the Layer 1 fix.
  Layer 3 — Pulse and the Experience Evaluator now also apply an explicit
            event_type allowlist (hearth_event_types.SAFE_HEARTH_EVENT_TYPES)
            directly in their SQL, so no future private event type is ever
            loaded into either process even if a write path forgets this
            boundary again.

Why DELETE, not mark-resolved (unlike this week's other cleanup scripts):
cleanup_worldview_bad_source_episode_id.py and
hearth_question_resolution_cleanup.py both preserve every row they touch —
those rows are legitimate operational history with one bad field value, and
morning_briefing.py's own resolve_stale_issues() docstring states the
general principle: "Historical episodes are never deleted — resolved = 1
preserves them for pattern detection." That principle assumes the row's
EXISTENCE is legitimate. Here it is not: per policy, a message_sent row
should never have entered hearth_events at all, in any state. Marking it
processed/resolved would leave the actual prohibited data (actor_user_id,
target_user_id, the private_message reference) sitting in a
Hearth-readable table indefinitely — exactly the exposure this fix exists
to close. There is no "resolved" state for a row whose existence is the
violation. Deletion is the only outcome that actually satisfies the
policy, so it's the correct exception to this week's general
delete-avoidance pattern, not a departure from carefulness.

Report minimalism: per the investigation's own finding that Pulse's prior
report already printed only "a message_sent event exists (ID + generic
label), not who/what/when," this script's report follows the same
convention deliberately — it prints row ids and a count, never
actor_user_id, target_user_id, reference_id, or occurred_at. There is no
value in this cleanup tool re-exposing the exact private metadata it
exists to remove, even to an authorized operator's own terminal/log.

Safety:
  - Dry-run by default — prints exactly what would be deleted, deletes
    nothing.
  - --apply backs up the Pathway DB (app.db, resolved from DATABASE_URL) to
    hearth/backups/ before writing anything, then deletes.
  - Idempotent — a second run (dry-run or --apply) finds zero matching
    rows, since after the first run no row has event_type='message_sent'.
  - Only ever touches rows with event_type = 'message_sent', exact match.
    No other event type is read, reported, or deleted.

Usage:
    python3 cleanup_message_sent_events.py             # dry-run report only
    python3 cleanup_message_sent_events.py --apply      # back up + apply for real

Recommended: run a dry-run against a copy of the production database
before ever passing --apply against production, same as this week's other
cleanup scripts.
"""

import argparse
import os
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")

_TARGET_EVENT_TYPE = "message_sent"


def _resolve_db_path(database_url):
    if database_url.startswith("sqlite:///"):
        return database_url[len("sqlite:///"):]
    return database_url


def get_connection(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def find_message_sent_rows(conn):
    """Return every hearth_events row with event_type = 'message_sent',
    exact match — nothing else can match this query, so nothing else can
    ever be deleted by this script. Only id is selected: see the module
    docstring's "Report minimalism" note on why actor/target/reference/
    timestamp are deliberately never read here.
    """
    return conn.execute(
        "SELECT id FROM hearth_events WHERE event_type = ? ORDER BY id;",
        (_TARGET_EVENT_TYPE,),
    ).fetchall()


def backup_database(db_path):
    src = Path(db_path)
    backups_dir = Path(__file__).resolve().parent / "backups"
    backups_dir.mkdir(exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    dest = backups_dir / f"{src.name}.bak_{ts}_pre_message_sent_cleanup"
    shutil.copy2(src, dest)
    print(f"Backed up {src} -> {dest}")
    return dest


def delete_rows(conn, row_ids):
    placeholders = ", ".join("?" for _ in row_ids)
    conn.execute(
        f"DELETE FROM hearth_events WHERE id IN ({placeholders});",
        tuple(row_ids),
    )
    conn.commit()


def run(conn, db_path, apply=False):
    print("=" * 72)
    print(
        "message_sent hearth_events cleanup — "
        + ("LIVE RUN" if apply else "DRY RUN (no writes)")
    )
    print(f"Pathway DB path: {db_path}")
    print("=" * 72)

    rows = find_message_sent_rows(conn)
    row_ids = [row["id"] for row in rows]

    print(f"\nmessage_sent rows found: {len(row_ids)}")
    if row_ids:
        print(f"  ids: {row_ids}")

    print("\n" + "=" * 72)
    print("Summary")
    print("=" * 72)
    print(f"  hearth_events (event_type='{_TARGET_EVENT_TYPE}')  "
          f"{len(row_ids)} row(s) {'deleted' if apply else 'would be deleted'}")
    print("=" * 72)

    if not apply:
        if not row_ids:
            print("\nDry-run complete. No message_sent rows found — clean already.")
        else:
            print(f"\nDry-run complete. {len(row_ids)} row(s) would be deleted. "
                  "Pass --apply to back up the database and apply this for real.")
        return row_ids

    if not row_ids:
        print("\nNothing to apply — already clean (idempotent no-op). No backup taken.")
        return row_ids

    backup_database(db_path)
    delete_rows(conn, row_ids)
    print(f"\nCleanup applied: {len(row_ids)} row(s) deleted.")
    return row_ids


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description=(
            "One-time cleanup: permanently delete every hearth_events row with "
            "event_type='message_sent' — private message metadata that should "
            "never have entered Hearth's intake table (HEARTH_SENSORY_POLICY.md "
            "Category B)."
        )
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        default=False,
        help="Back up the database and apply the cleanup for real. Omit for a dry-run report only.",
    )
    args = parser.parse_args()

    if not DATABASE_URL:
        raise SystemExit("ERROR: DATABASE_URL is missing. Add it to your .env file.")

    db_path = _resolve_db_path(DATABASE_URL)
    conn = get_connection(db_path)
    try:
        run(conn, db_path, apply=args.apply)
    finally:
        conn.close()
