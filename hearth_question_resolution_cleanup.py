"""
One-time cleanup utility for the open-questions backlog.

Root cause (see hearth_soul.py's "Question auto-resolution" section): when
the episode(s) that triggered a surfaced question resolve, the question
itself never used to auto-close — it stayed open indefinitely until manually
answered or dismissed. hearth_soul.resolve_cleared_worldview_questions() now
runs every scan (wired into morning_briefing.run_pipeline()) to fix this
going forward. This script applies that exact same recompute-and-close logic
retroactively to the EXISTING backlog of open, worldview-sourced questions,
so previously-accumulated stale questions get the fix now instead of waiting
for some unrelated future scan to happen to touch them.

There is no second, parallel decision implementation here — this script
calls hearth_soul.resolve_cleared_worldview_questions(conn, dry_run=...)
directly, the identical function the standing per-scan pass uses. The ONLY
mutating call it can make is that function's own call to
hearth_questions.auto_resolve_question(), which only ever touches
hearth_questions / hearth_worldview_uncertainties rows already confirmed
open. Legacy (non-worldview) questions and any question whose subject_type/
subject_id can't be interpreted are left untouched and reported separately
— never guessed at, never deleted.

Dry-run by default. Never deletes a row. Backs up hearth_memory.db before
--apply — same safety pattern as hearth_experience_evaluator_cleanup.py.

Usage:
    # Dry run (default) — prints a full report, changes nothing:
    python3 hearth_question_resolution_cleanup.py

    # Apply (after reviewing the dry-run report):
    python3 hearth_question_resolution_cleanup.py --apply

Recommended: run a dry-run against a copy of the production database before
ever passing --apply against production, same as the Experience Evaluator
cleanup.
"""

import argparse
import json
import os
import shutil
import sqlite3
from datetime import datetime, timezone

from dotenv import load_dotenv

import hearth_memory
import hearth_questions
import hearth_soul

load_dotenv()

_LOG_PREFIX = "[HEARTH QUESTION CLEANUP]"

_ACTION_VERBS_DRY_RUN = {"resolve": "would close", "keep": "kept open", "skip": "SKIPPED"}
_ACTION_VERBS_APPLY = {"resolve": "closed", "keep": "kept open", "skip": "SKIPPED"}


# ---------------------------------------------------------------------------
# Backup
# ---------------------------------------------------------------------------

def backup_memory_db(db_path):
    backups_dir = os.path.join(os.path.dirname(os.path.abspath(db_path)) or ".", "backups")
    os.makedirs(backups_dir, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    backup_path = os.path.join(
        backups_dir, f"{os.path.basename(db_path)}.pre_question_resolution_cleanup_{ts}.bak"
    )
    shutil.copy2(db_path, backup_path)
    return backup_path


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run_cleanup(memory_conn, apply_changes):
    """Core cleanup logic, DB-connection-injectable for tests. Returns a
    report dict. Never opens/closes connections or touches env vars itself
    — see main() for the CLI wrapper that does.
    """
    hearth_questions.ensure_questions_table(memory_conn)

    results = hearth_soul.resolve_cleared_worldview_questions(memory_conn, dry_run=not apply_changes)

    verbs = _ACTION_VERBS_APPLY if apply_changes else _ACTION_VERBS_DRY_RUN
    counts = {"resolve": 0, "keep": 0, "skip": 0}
    for r in results:
        counts[r["action"]] += 1
        print(
            f"{_LOG_PREFIX} question_id={r['question_id']} subject_type={r['subject_type']!r}"
            f" subject_id={r['subject_id']!r}: {verbs[r['action']]} — {r['reason']}"
        )

    return {"apply": apply_changes, "counts": counts, "results": results}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", default=False,
                         help="Actually close cleared questions. Omit for dry-run (default).")
    parser.add_argument("--db-path", default=None, help="Override hearth_memory.db path.")
    parser.add_argument("--report-path", default=None, help="Optional path to write a JSON report.")
    args = parser.parse_args()

    db_path = args.db_path or hearth_memory.MEMORY_DB_PATH
    print(f"{_LOG_PREFIX} target database: {db_path}")
    print(f"{_LOG_PREFIX} mode: {'APPLY' if args.apply else 'DRY RUN (default — nothing will change)'}")

    if args.apply:
        backup_path = backup_memory_db(db_path)
        print(f"{_LOG_PREFIX} backup written: {backup_path}")

    memory_conn = sqlite3.connect(db_path)
    memory_conn.row_factory = sqlite3.Row
    memory_conn.execute("PRAGMA foreign_keys=ON;")
    try:
        report = run_cleanup(memory_conn, args.apply)
    finally:
        memory_conn.close()

    if args.report_path:
        with open(args.report_path, "w") as f:
            json.dump(report, f, indent=2, default=str)
        print(f"{_LOG_PREFIX} report written: {args.report_path}")

    c = report["counts"]
    print(f"{_LOG_PREFIX} done. resolve={c['resolve']} keep={c['keep']} skip={c['skip']}")
    return report


if __name__ == "__main__":
    main()
