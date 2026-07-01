import argparse
import sqlite3
from datetime import datetime, timezone

from hearth_memory import MEMORY_DB_PATH


def get_connection():
    conn = sqlite3.connect(MEMORY_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def find_duplicate_groups(conn):
    return conn.execute("""
        SELECT subject_type, subject_id, belief_type, COUNT(*) AS cnt
        FROM hearth_worldview_beliefs
        WHERE status = 'active'
        GROUP BY subject_type, subject_id, belief_type
        HAVING COUNT(*) > 1
        ORDER BY cnt DESC, subject_type, subject_id, belief_type;
    """).fetchall()


def rows_for_group(conn, subject_type, subject_id, belief_type):
    return conn.execute("""
        SELECT *
        FROM hearth_worldview_beliefs
        WHERE status = 'active'
          AND subject_type = ?
          AND subject_id = ?
          AND belief_type = ?
        ORDER BY confidence DESC, updated_at DESC, id DESC;
    """, (subject_type, subject_id, belief_type)).fetchall()


def run(conn, apply=False):
    groups = find_duplicate_groups(conn)
    if not groups:
        print("No duplicate active beliefs found.")
        return

    archive_ids = []

    for group in groups:
        rows = rows_for_group(
            conn,
            group["subject_type"],
            group["subject_id"],
            group["belief_type"],
        )
        keeper = rows[0]
        duplicates = rows[1:]

        print(
            f"subject_type={group['subject_type']!r} "
            f"subject_id={group['subject_id']!r} "
            f"belief_type={group['belief_type']!r} "
            f"({len(rows)} active rows)"
        )
        print(
            f"  KEEP    id={keeper['id']} "
            f"confidence={keeper['confidence']} "
            f"updated_at={keeper['updated_at']}"
        )

        for row in duplicates:
            print(
                f"  ARCHIVE id={row['id']} "
                f"confidence={row['confidence']} "
                f"updated_at={row['updated_at']}"
            )
            archive_ids.append(row["id"])

        print()

    print(f"Total active duplicate rows to archive: {len(archive_ids)}")

    if not apply:
        print("Dry-run complete. Pass --apply to archive duplicates.")
        return

    now = datetime.now(timezone.utc).isoformat()
    note = f"dedup_cleanup: archived duplicate active belief at {now}"

    for belief_id in archive_ids:
        conn.execute("""
            UPDATE hearth_worldview_beliefs
            SET status = 'archived',
                updated_at = ?,
                notes = CASE
                    WHEN notes IS NULL OR notes = '' THEN ?
                    ELSE notes || char(10) || ?
                END
            WHERE id = ?
              AND status = 'active';
        """, (now, note, note, belief_id))

    conn.commit()
    print(f"Cleanup applied: archived {len(archive_ids)} duplicate active belief row(s).")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Hearth belief dedup report")
    parser.add_argument("--apply", action="store_true", help="Archive duplicates. Omit for dry-run.")
    args = parser.parse_args()

    conn = get_connection()
    try:
        run(conn, apply=args.apply)
    finally:
        conn.close()
