"""
Hearth Uncertainty Dedup Report

Identifies groups of living uncertainties (status IN 'open', 'question_surfaced')
that share the same (subject_type, subject_id) — i.e. duplicates that should be
one row. Reports which row to keep and which to drop.

Usage:
    python hearth_uncertainty_dedup_report.py           # dry-run report only
    python hearth_uncertainty_dedup_report.py --apply   # apply the cleanup
"""

import argparse
import sqlite3

from hearth_memory import MEMORY_DB_PATH

LIVING_STATUSES = ("open", "question_surfaced")


def get_connection():
    conn = sqlite3.connect(MEMORY_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def find_duplicate_groups(conn):
    """Return groups where more than one living row shares (subject_type, subject_id)."""
    placeholders = ", ".join("?" for _ in LIVING_STATUSES)
    sql = f"""
        SELECT subject_type, subject_id, COUNT(*) AS cnt
        FROM hearth_worldview_uncertainties
        WHERE status IN ({placeholders})
        GROUP BY subject_type, subject_id
        HAVING COUNT(*) > 1
        ORDER BY cnt DESC, subject_type, subject_id;
    """
    return conn.execute(sql, LIVING_STATUSES).fetchall()


def get_rows_for_group(conn, subject_type, subject_id):
    """Return all living rows for a (subject_type, subject_id) pair."""
    placeholders = ", ".join("?" for _ in LIVING_STATUSES)
    sql = f"""
        SELECT *
        FROM hearth_worldview_uncertainties
        WHERE subject_type = ? AND subject_id = ? AND status IN ({placeholders})
        ORDER BY
            CASE status WHEN 'question_surfaced' THEN 0 ELSE 1 END,
            updated_at DESC;
    """
    return conn.execute(sql, (subject_type, subject_id) + LIVING_STATUSES).fetchall()


def pick_keeper(rows):
    """Choose which row to keep.

    Prefer question_surfaced over open (further along in the lifecycle).
    Among equal statuses, prefer the most recently updated row.
    """
    for row in rows:
        if row["status"] == "question_surfaced":
            return row
    return rows[0]  # already sorted by updated_at DESC


def run_report(conn, apply=False):
    groups = find_duplicate_groups(conn)

    if not groups:
        print("No duplicate living uncertainties found.")
        return

    print(f"Found {len(groups)} duplicate group(s) among living uncertainties.\n")

    drop_ids = []
    for group in groups:
        subject_type = group["subject_type"]
        subject_id = group["subject_id"]
        rows = get_rows_for_group(conn, subject_type, subject_id)

        keeper = pick_keeper(rows)
        to_drop = [r for r in rows if r["id"] != keeper["id"]]

        print(f"  subject_type={subject_type!r}  subject_id={subject_id!r}  ({len(rows)} rows)")
        print(f"    KEEP  id={keeper['id']} status={keeper['status']} updated_at={keeper['updated_at']}")
        print(f"          uncertainty_text: {keeper['uncertainty_text'][:80]}")
        for row in to_drop:
            print(f"    DROP  id={row['id']} status={row['status']} updated_at={row['updated_at']}")
            print(f"          uncertainty_text: {row['uncertainty_text'][:80]}")
            drop_ids.append(row["id"])
        print()

    print(f"Total rows to drop: {len(drop_ids)}")

    if not apply:
        print("\nDry-run complete. Pass --apply to execute the cleanup.")
        return

    if not drop_ids:
        print("Nothing to apply.")
        return

    placeholders = ", ".join("?" for _ in drop_ids)
    conn.execute(
        f"DELETE FROM hearth_worldview_uncertainties WHERE id IN ({placeholders});",
        drop_ids,
    )
    conn.commit()
    print(f"\nCleanup applied: {len(drop_ids)} duplicate row(s) deleted.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Hearth uncertainty dedup report")
    parser.add_argument(
        "--apply",
        action="store_true",
        default=False,
        help="Apply the cleanup (delete duplicate rows). Omit for dry-run.",
    )
    args = parser.parse_args()

    conn = get_connection()
    try:
        run_report(conn, apply=args.apply)
    finally:
        conn.close()
