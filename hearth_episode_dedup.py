"""
Hearth Episode Dedup — one-time cleanup of duplicate episodes.

Inspects hearth_memory.db for open episodes that represent the same underlying
condition and resolves all but the oldest (canonical) one per group.

Run modes:
    python hearth_episode_dedup.py          # inspect only — prints report, writes nothing
    python hearth_episode_dedup.py --clean  # dry run showing what would be resolved
    python hearth_episode_dedup.py --execute # apply the cleanup (writes to hearth_memory.db)

Background
----------
The checkin_not_submitted watcher originally keyed episodes on
checkin_submission_{cs.id}. When Pathway creates multiple submission rows for
the same check-in (e.g. on resend), each submission produces a separate episode
even though they represent one unresolved condition. This script finds those
duplicates and collapses them to the oldest open episode per (entity, check-in).

Separate monthly check-in cycles (different checkin_id / different check-in
records) are NOT collapsed — each cycle is a distinct experience.

Cleanup strategy per episode type
----------------------------------
checkin_not_submitted
    Group by (entity_id, checkin_id).  checkin_id is recovered from the
    reference_key or from the Pathway Portal DB when needed.
    Keep: oldest open episode in each group.
    Resolve: all later duplicates in the same group.

All other episode types
    No automatic cleanup — each watcher already uses a stable reference key.
    The inspect report flags any unexpected duplicates for manual review.
"""

import argparse
import os
import sqlite3
from datetime import datetime, timezone

from dotenv import load_dotenv

import hearth_memory

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")

_CLEANUP_REASON = "dedup_cleanup: duplicate of earlier open episode for same check-in cycle"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resolve_db_path(database_url):
    if database_url and database_url.startswith("sqlite:///"):
        return database_url[len("sqlite:///"):]
    return database_url


def _open_pathway_readonly(db_path):
    """Read-only connection to Pathway Portal. Returns None if unavailable."""
    if not db_path:
        return None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        conn.execute("SELECT 1 FROM checkin_submissions LIMIT 1;")
        return conn
    except Exception as exc:
        print(f"[dedup] WARNING: Pathway DB unavailable ({exc}) — checkin_id lookup disabled.")
        return None


def _checkin_id_for_submission(pathway_conn, submission_id):
    """Look up the checkin_id (ci.id) for a given checkin_submission.id."""
    if pathway_conn is None:
        return None
    try:
        row = pathway_conn.execute(
            "SELECT checkin_id FROM checkin_submissions WHERE id = ?;",
            (submission_id,),
        ).fetchone()
        return row["checkin_id"] if row else None
    except Exception:
        return None


def _extract_group_key(ep, pathway_conn):
    """
    Return a dedup group key for a checkin_not_submitted episode.

    Priority:
    1. New-format reference_key  checkin_{ci_id}_user_{user_id}  → parse directly.
    2. Old-format reference_key  checkin_submission_{cs_id}      → look up ci_id in Pathway.
    3. Fallback: (entity_id, "unknown") — groups with unknown checkin won't be cleaned.
    """
    ref_key = ep["reference_key"] or ""
    entity_id = ep["entity_id"]

    if ref_key.startswith("checkin_") and "_user_" in ref_key:
        # New format: checkin_{ci_id}_user_{user_id}
        return (entity_id, ref_key)

    if ref_key.startswith("checkin_submission_"):
        sub_id_str = ref_key[len("checkin_submission_"):]
        try:
            sub_id = int(sub_id_str)
        except ValueError:
            return (entity_id, f"unknown:{ref_key}")
        ci_id = _checkin_id_for_submission(pathway_conn, sub_id)
        if ci_id is not None:
            user_id = ep["user_id"]
            return (entity_id, f"checkin_{ci_id}_user_{user_id}")
        # Couldn't look up checkin_id — use submission as a unique key (no dedup)
        return (entity_id, f"unknown_submission:{sub_id}")

    # No reference_key or unrecognised format
    return (entity_id, f"unknown:{ref_key}")


# ---------------------------------------------------------------------------
# Inspection
# ---------------------------------------------------------------------------

def inspect_duplicates(memory_conn, pathway_conn):
    """
    Scan all open episodes and report duplicates.

    Returns a dict:
        {
            episode_type: {
                group_key: [episode_row, ...],  # only groups with >1 episode
                ...
            },
            ...
        }
    """
    open_eps = hearth_memory.get_open_episodes(memory_conn)

    from collections import defaultdict
    by_type = defaultdict(lambda: defaultdict(list))

    for ep in open_eps:
        ep_type = ep["episode_type"]
        entity_id = ep["entity_id"]
        ref_key = ep["reference_key"]

        if ep_type == "checkin_not_submitted":
            group_key = _extract_group_key(ep, pathway_conn)
        elif ref_key is not None:
            # For typed episodes with reference_key, duplicates would be same (type, ref_key)
            group_key = (entity_id, ref_key)
        else:
            # No reference_key: dedup on (entity_id, episode_type)
            group_key = (entity_id, None)

        by_type[ep_type][group_key].append(ep)

    # Filter to groups with >1 episode
    duplicates = {}
    for ep_type, groups in by_type.items():
        dup_groups = {k: eps for k, eps in groups.items() if len(eps) > 1}
        if dup_groups:
            duplicates[ep_type] = dup_groups

    return duplicates


def print_report(duplicates, pathway_conn):
    if not duplicates:
        print("[dedup] No duplicate open episodes found.")
        return

    total_extra = 0
    for ep_type, groups in sorted(duplicates.items()):
        print(f"\n{ep_type}: {len(groups)} duplicate group(s)")
        for group_key, eps in sorted(groups.items(), key=lambda x: -len(x[1])):
            eps_sorted = sorted(eps, key=lambda e: e["observed_at"])
            keeper = eps_sorted[0]
            extras = eps_sorted[1:]
            total_extra += len(extras)
            print(f"  group {group_key}:")
            print(f"    KEEP    id={keeper['id']} observed={keeper['observed_at'][:16]} "
                  f"ref={keeper['reference_key']}")
            for ex in extras:
                print(f"    RESOLVE id={ex['id']} observed={ex['observed_at'][:16]} "
                      f"ref={ex['reference_key']}")

    print(f"\n[dedup] Total episodes that would be resolved: {total_extra}")


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------

def clean_duplicates(memory_conn, duplicates, execute=False):
    """
    Resolve duplicate episodes, keeping the oldest per group.

    Returns the count of episodes resolved.

    execute=False  → dry run (prints actions, writes nothing)
    execute=True   → applies the changes to hearth_memory.db
    """
    mode = "EXECUTE" if execute else "DRY RUN"
    resolved_count = 0
    now = datetime.now(timezone.utc).isoformat()

    if not duplicates:
        print(f"[dedup] {mode}: nothing to do.")
        return 0

    # Only handle checkin_not_submitted automatically — other types need manual review
    types_to_auto_clean = {"checkin_not_submitted"}
    types_needing_review = sorted(set(duplicates) - types_to_auto_clean)

    if types_needing_review:
        print(
            f"\n[dedup] {mode}: the following types have duplicates but are NOT auto-cleaned"
            f" (requires manual review): {', '.join(types_needing_review)}"
        )

    for ep_type in types_to_auto_clean:
        if ep_type not in duplicates:
            continue
        groups = duplicates[ep_type]
        for group_key, eps in groups.items():
            if "unknown" in str(group_key):
                print(
                    f"[dedup] {mode}: SKIP group {group_key} — could not resolve checkin_id,"
                    f" leaving {len(eps)} episodes untouched."
                )
                continue
            eps_sorted = sorted(eps, key=lambda e: e["observed_at"])
            keeper = eps_sorted[0]
            extras = eps_sorted[1:]
            for ex in extras:
                print(
                    f"[dedup] {mode}: resolve id={ex['id']} ({ep_type},"
                    f" entity={ex['entity_id']}) — duplicate of id={keeper['id']}"
                )
                if execute:
                    hearth_memory.resolve_episode(memory_conn, ex["id"], resolved_at=now)
                resolved_count += 1

    verb = "resolved" if execute else "would resolve"
    print(f"\n[dedup] {mode}: {verb} {resolved_count} duplicate episode(s).")
    return resolved_count


# ---------------------------------------------------------------------------
# Migrate old reference keys to new format (optional post-cleanup step)
# ---------------------------------------------------------------------------

def migrate_reference_keys(memory_conn, pathway_conn, execute=False):
    """
    Update surviving checkin_not_submitted episodes from old-format reference_keys
    (checkin_submission_{id}) to the new stable format (checkin_{ci_id}_user_{user_id}).

    This is separate from dedup cleanup — it prevents the morning_briefing resolution
    logic from auto-resolving episodes that had the old key format, which would close
    them prematurely and then immediately reopen them with the new key.

    Only migrates episodes where the Pathway lookup succeeds.
    Skips episodes that already use the new format.
    """
    mode = "EXECUTE" if execute else "DRY RUN"
    migrated = 0
    skipped = 0

    open_eps = memory_conn.execute(
        "SELECT e.id, e.entity_id, e.reference_key, en.user_id"
        " FROM hearth_episodes e"
        " LEFT JOIN hearth_entities en ON en.id = e.entity_id"
        " WHERE e.episode_type = 'checkin_not_submitted' AND e.resolved = 0;",
    ).fetchall()

    for ep in open_eps:
        ref_key = ep["reference_key"] or ""
        if not ref_key.startswith("checkin_submission_"):
            continue  # already in new format or no ref_key

        sub_id_str = ref_key[len("checkin_submission_"):]
        try:
            sub_id = int(sub_id_str)
        except ValueError:
            skipped += 1
            continue

        ci_id = _checkin_id_for_submission(pathway_conn, sub_id)
        if ci_id is None:
            skipped += 1
            print(f"[dedup] {mode}: SKIP key migration for episode id={ep['id']}"
                  f" (could not look up checkin_id for submission {sub_id})")
            continue

        user_id = ep["user_id"]
        new_key = f"checkin_{ci_id}_user_{user_id}"
        print(f"[dedup] {mode}: migrate episode id={ep['id']} ref_key"
              f" {ref_key} → {new_key}")
        if execute:
            memory_conn.execute(
                "UPDATE hearth_episodes SET reference_key = ? WHERE id = ? AND resolved = 0;",
                (new_key, ep["id"]),
            )
            migrated += 1
        else:
            migrated += 1

    if execute:
        memory_conn.commit()

    verb = "migrated" if execute else "would migrate"
    print(f"[dedup] {mode}: {verb} {migrated} reference key(s), skipped {skipped}.")
    return migrated


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Inspect and clean up duplicate Hearth episodes."
    )
    parser.add_argument(
        "--execute", action="store_true",
        help="Apply the cleanup. Without this flag the script is a dry run.",
    )
    parser.add_argument(
        "--migrate-keys", action="store_true",
        help="Also migrate old-format reference keys to the new stable format.",
    )
    args = parser.parse_args()

    print(f"[dedup] Hearth Episode Dedup — {'EXECUTE mode' if args.execute else 'inspect/dry-run mode'}")

    memory_conn = hearth_memory.get_memory_connection()
    hearth_memory.init_tables(memory_conn)

    db_path = _resolve_db_path(DATABASE_URL)
    pathway_conn = _open_pathway_readonly(db_path)

    print("\n--- Inspection ---")
    duplicates = inspect_duplicates(memory_conn, pathway_conn)
    print_report(duplicates, pathway_conn)

    print("\n--- Cleanup ---")
    clean_duplicates(memory_conn, duplicates, execute=args.execute)

    if args.migrate_keys and pathway_conn is not None:
        print("\n--- Reference key migration ---")
        migrate_reference_keys(memory_conn, pathway_conn, execute=args.execute)
    elif args.migrate_keys and pathway_conn is None:
        print("\n[dedup] WARNING: --migrate-keys requires a working Pathway DB connection.")

    memory_conn.close()
    if pathway_conn is not None:
        pathway_conn.close()

    if not args.execute:
        print(
            "\n[dedup] This was a dry run. Pass --execute to apply changes."
            " Pass --migrate-keys to also update old-format reference keys."
        )


if __name__ == "__main__":
    main()
