"""
One-time maintenance: backfill question_family/question_version on existing
hearth_worldview_uncertainties rows that predate — or were created before a
mapping existed for — the stamping registry in
hearth_soul.py:_QUESTION_FAMILIES_BY_EPISODE_TYPE /
_RECENT_CONCERN_VOLUME_FAMILY.

Only two shapes are ever unambiguously mappable, both derived from the exact
same trusted identifiers the generator itself used to write the row (never
inferred from rendered question/uncertainty text):

  1. subject_type == 'entity_episode', subject_id == "{episode_type}:{entity}"
     (written by hearth_soul._upsert_single_episode_uncertainty). The
     episode_type prefix before the first ':' is looked up directly in
     hearth_soul._QUESTION_FAMILIES_BY_EPISODE_TYPE — the SAME dict the live
     generator now uses, so this script can never drift out of sync with it.

  2. subject_type == 'entity', subject_id is a bare digit string (written by
     hearth_soul._upsert_entity_repeat_uncertainty — the only producer of
     subject_type='entity' *uncertainties* as of this writing; beliefs also
     use subject_type='entity' but live in a different table entirely). This
     is stamped hearth_soul._RECENT_CONCERN_VOLUME_FAMILY.

Anything else (subject_type='entity_episode' with an episode_type prefix not
in the registry, subject_type='entity' with a non-digit subject_id, any
other subject_type, or a NULL subject_id) is reported and skipped rather
than guessed at.

Scope: only living/current rows — status IN ('open', 'question_surfaced',
'answered') — so existing manager answers are not left permanently
unstamped/uninterpretable, without touching resolved/archived/dismissed
history. Never touches a row that already has a non-null question_family or
question_version.

Idempotent: only ever targets question_family IS NULL rows, so a second run
(dry or applied) after a successful apply finds zero rows left to change.
Transactional: --apply wraps every UPDATE in one transaction and rolls back
on any error; nothing is partially applied.

Usage:
    # Local (dry run — default, makes no changes):
    cd hearth && ../backend/venv/bin/python3 backfill_question_family_stamps.py

    # Local (apply):
    cd hearth && ../backend/venv/bin/python3 backfill_question_family_stamps.py --apply

    # Production (Render shell, same script, same DATABASE via
    # hearth_memory.MEMORY_DB_PATH which already resolves the persistent-disk
    # path in that environment — no separate prod-only invocation needed):
    python3 hearth/backfill_question_family_stamps.py            # dry run first
    python3 hearth/backfill_question_family_stamps.py --apply    # then apply
"""

import argparse
import sqlite3
from collections import Counter

from hearth_memory import MEMORY_DB_PATH
from hearth_soul import _QUESTION_FAMILIES_BY_EPISODE_TYPE, _RECENT_CONCERN_VOLUME_FAMILY

_LIVING_STATUSES = ("open", "question_surfaced", "answered")


def get_connection():
    conn = sqlite3.connect(MEMORY_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def _plan_for_row(row):
    """Return (question_family, question_version) or None if unmappable."""
    subject_type = row["subject_type"]
    subject_id = row["subject_id"] or ""

    if subject_type == "entity_episode" and ":" in subject_id:
        episode_type = subject_id.split(":", 1)[0]
        mapped = _QUESTION_FAMILIES_BY_EPISODE_TYPE.get(episode_type)
        if mapped:
            return mapped, episode_type
        return None, episode_type

    if subject_type == "entity" and subject_id.strip().isdigit():
        return _RECENT_CONCERN_VOLUME_FAMILY, "entity_repeat_concern"

    return None, subject_type or "unknown"


def plan_backfill(conn):
    """Read-only: compute what WOULD change. Returns a report dict."""
    rows = conn.execute(
        "SELECT id, subject_type, subject_id, status FROM hearth_worldview_uncertainties"
        " WHERE question_family IS NULL"
        f"   AND status IN ({','.join('?' for _ in _LIVING_STATUSES)});",
        _LIVING_STATUSES,
    ).fetchall()

    to_update = []   # [(uncertainty_id, family, version, episode_type_label)]
    skipped = []      # [(uncertainty_id, subject_type, subject_id, reason_label)]

    for row in rows:
        mapped, label = _plan_for_row(row)
        if mapped:
            family, version = mapped
            to_update.append((row["id"], family, version, label))
        else:
            skipped.append((row["id"], row["subject_type"], row["subject_id"], label))

    counts_by_family = Counter((u[1], u[2]) for u in to_update)
    counts_by_episode_type = Counter(u[3] for u in to_update)
    skipped_by_label = Counter(s[3] for s in skipped)

    return {
        "total_candidates": len(rows),
        "to_update": to_update,
        "skipped": skipped,
        "counts_by_family_version": dict(counts_by_family),
        "counts_by_episode_type": dict(counts_by_episode_type),
        "skipped_by_label": dict(skipped_by_label),
    }


def apply_backfill(conn, plan):
    """Apply plan['to_update'] inside one transaction. Rolls back on error."""
    try:
        for uncertainty_id, family, version, _label in plan["to_update"]:
            conn.execute(
                "UPDATE hearth_worldview_uncertainties"
                " SET question_family = ?, question_version = ?"
                " WHERE id = ? AND question_family IS NULL;",
                (family, version, uncertainty_id),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _print_report(plan, applied):
    print(f"[BACKFILL] candidates scanned (question_family IS NULL, living status): "
          f"{plan['total_candidates']}")
    print(f"[BACKFILL] {'applied' if applied else 'would update'}: {len(plan['to_update'])} row(s)")
    for (family, version), count in sorted(plan["counts_by_family_version"].items()):
        print(f"    -> question_family={family!r} question_version={version}: {count} row(s)")
    print("[BACKFILL] breakdown by source:")
    for label, count in sorted(plan["counts_by_episode_type"].items()):
        print(f"    {label}: {count} row(s)")

    print(f"[BACKFILL] skipped (ambiguous/unknown): {len(plan['skipped'])} row(s)")
    for label, count in sorted(plan["skipped_by_label"].items()):
        print(f"    subject_type/episode_type={label!r}: {count} row(s) skipped")
    if plan["skipped"]:
        print("[BACKFILL] skipped uncertainty ids (first 20):")
        for uid, subject_type, subject_id, label in plan["skipped"][:20]:
            print(f"    id={uid} subject_type={subject_type!r} subject_id={subject_id!r} ({label})")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                         help="Actually write the backfill (default is dry-run — report only).")
    args = parser.parse_args()

    print(f"[BACKFILL] target database: {MEMORY_DB_PATH}")
    conn = get_connection()
    try:
        plan = plan_backfill(conn)
        if args.apply:
            apply_backfill(conn, plan)
            _print_report(plan, applied=True)
        else:
            _print_report(plan, applied=False)
            print("[BACKFILL] dry run only — no changes written. Re-run with --apply to commit.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
