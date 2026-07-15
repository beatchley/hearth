"""
One-time cleanup utility for hearth_episodes rows invalidly promoted by
hearth_experience_evaluator.py V1 (see that module's docstring for the full
defect writeup).

Dry-run by default. Never deletes a row — every invalidated row is closed
via hearth_memory.resolve_episode(..., resolution_reason="invalid_evaluator_promotion"),
exactly like any other episode resolution. The ONLY mutating call this
script ever makes against hearth_episodes is that one; it has no other
write path into hearth_memory.db, and it never writes to hearth_events
(Pathway's database) at all — read-only there, always.

For each entity's evaluator-promoted rows (reference_key LIKE 'pulse_event_%',
any status), this re-derives the source hearth_events row and re-classifies
it under the corrected engine in hearth_experience_evaluator.py — the exact
same _gather_entity_targets/_classify_event functions the live evaluator
uses, imported and reused rather than reimplemented. A row is retained if
re-classification reproduces the same episode_type it was originally
promoted as; otherwise it is invalidated. Concern and resolution detection
are permanently out of scope for hearth_experience_evaluator.py by
architecture decision (see that module's docstring) — classify_event can
never return 'resolution' or 'concern' — so every historical row promoted
as either of those two types is invalidated by construction; only
'momentum' rows are individually revalidated against current worldview
data.

Entities 15/16 (the known duplicate-inactive-entity pair) get an additional
mandatory pre-check — see _check_entities_15_16_precondition — and this
script has no code path capable of merging entities, rewriting user
mappings, or touching Roads/Furniture/State/Reflection; its only mutation
anywhere is resolve_episode() on a hearth_episodes row.

Usage:
    # Dry run (default) — prints a full report, changes nothing:
    python3 hearth_experience_evaluator_cleanup.py --entity-id 11 --entity-id 39

    # Apply (after reviewing the dry-run report):
    python3 hearth_experience_evaluator_cleanup.py --entity-id 11 --entity-id 39 --apply

Refuses to --apply unless HEARTH_EXPERIENCE_EVALUATOR_PROMOTE=0, and always
backs up hearth_memory.db before applying.
"""

import argparse
import json
import os
import shutil
import sqlite3
from datetime import datetime, timezone

from dotenv import load_dotenv

import hearth_experience_evaluator as evaluator
import hearth_identity
import hearth_memory
import hearth_worldview

load_dotenv()

_LOG_PREFIX = "[HEARTH CLEANUP]"
_INVALID_REASON = "invalid_evaluator_promotion"
_MOOT_REASON = "invalid_evaluator_promotion_moot_inactive_entity"

# Entities 15/16 get the extra duplicate-inactive-entity precondition check.
_DUPLICATE_INACTIVE_ENTITY_IDS = frozenset({15, 16})


# ---------------------------------------------------------------------------
# Data gathering (read-only)
# ---------------------------------------------------------------------------

def _extract_source_event_id(reference_key):
    if not reference_key or not reference_key.startswith(hearth_memory.EVALUATOR_REFERENCE_KEY_PREFIX):
        return None
    tail = reference_key[len(hearth_memory.EVALUATOR_REFERENCE_KEY_PREFIX):]
    return int(tail) if tail.isdigit() else None


def _load_promoted_rows(memory_conn, entity_id):
    """Every evaluator-promoted row for one entity, any status, oldest first."""
    return memory_conn.execute(
        "SELECT * FROM hearth_episodes WHERE entity_id = ? AND reference_key LIKE ?"
        " ORDER BY observed_at ASC, id ASC;",
        (entity_id, f"{hearth_memory.EVALUATOR_REFERENCE_KEY_PREFIX}%"),
    ).fetchall()


def _fetch_source_event(pathway_conn, event_id):
    if pathway_conn is None:
        return None
    return pathway_conn.execute(
        "SELECT * FROM hearth_events WHERE id = ?;", (event_id,)
    ).fetchone()


# ---------------------------------------------------------------------------
# Re-evaluation under corrected rules
# ---------------------------------------------------------------------------

def evaluate_entity_rows(memory_conn, pathway_conn, entity_id):
    """Re-evaluate every evaluator-promoted row for one entity. Read-only —
    does not resolve anything itself; returns a list of decision dicts for
    the caller to act on (or not, in dry-run).

    Each decision dict:
        {
            "episode_id", "episode_type", "reference_key", "resolved" (bool),
            "source_event_id", "action" ("retain"|"invalidate"|"cannot_verify"),
            "reason", "new_classification", "new_target_type", "new_target_id",
        }
    """
    rows = _load_promoted_rows(memory_conn, entity_id)
    targets = evaluator._gather_entity_targets(memory_conn, entity_id)
    decisions = []

    for row in rows:
        event_id = _extract_source_event_id(row["reference_key"])
        decision = {
            "episode_id": row["id"],
            "episode_type": row["episode_type"],
            "reference_key": row["reference_key"],
            "resolved": bool(row["resolved"]),
            "source_event_id": event_id,
            "new_classification": None,
            "new_target_type": None,
            "new_target_id": None,
        }

        if event_id is None:
            decision["action"] = "cannot_verify"
            decision["reason"] = (
                "reference_key does not match the pulse_event_<id> shape — "
                "cannot re-derive the source event; left untouched."
            )
            decisions.append(decision)
            continue

        event = _fetch_source_event(pathway_conn, event_id)
        if event is None:
            decision["action"] = "cannot_verify"
            decision["reason"] = (
                f"source hearth_events#{event_id} not found or unreadable — "
                "cannot re-evaluate; left untouched."
            )
            decisions.append(decision)
            continue

        actor_id = event["actor_user_id"] if event["actor_user_id"] is not None else event["target_user_id"]
        entity_row = hearth_memory.get_entity_by_user_id(memory_conn, actor_id) if actor_id is not None else None

        if entity_row is None or entity_row["id"] != entity_id:
            decision["action"] = "invalidate"
            decision["new_classification"] = "no_match"
            decision["reason"] = (
                f"source hearth_events#{event_id} no longer maps to entity {entity_id} "
                "under current data."
            )
            decisions.append(decision)
            continue

        result = evaluator._classify_event(event, entity_id, targets, evaluator._MOMENTUM_RULES)
        decision["new_classification"] = result["classification"]
        decision["new_target_type"] = result["target_type"]
        decision["new_target_id"] = result["target_id"]

        if result["classification"] != row["episode_type"]:
            decision["action"] = "invalidate"
            decision["reason"] = (
                f"re-evaluated under corrected rules as '{result['classification']}'"
                f" (was promoted as '{row['episode_type']}'): {result['reason']}"
            )
        else:
            decision["action"] = "retain"
            decision["reason"] = "revalidated under corrected rules."

        decisions.append(decision)

    return decisions


# ---------------------------------------------------------------------------
# Entities 15/16 precondition
# ---------------------------------------------------------------------------

def check_duplicate_inactive_entity_precondition(memory_conn, pathway_conn, entity_ids):
    """For the known duplicate-inactive-entity pair (15, 16): confirm both
    map to the same Pathway user_id, that user is inactive, and that the
    existing deactivation defense-in-depth filter (hearth_identity.
    get_inactive_user_ids — the same function hearth_context.build_context's
    Fix 3 uses) would exclude that user's episodes from Daily Brief. Returns
    (ok: bool, message: str). Never merges, rewrites, or migrates anything —
    read-only.
    """
    pair = sorted(eid for eid in entity_ids if eid in _DUPLICATE_INACTIVE_ENTITY_IDS)
    if not pair:
        return True, "no duplicate-inactive-entity ids in this run — precondition not applicable."
    if set(pair) != _DUPLICATE_INACTIVE_ENTITY_IDS:
        return False, (
            f"entities {pair} include a duplicate-inactive-entity id but not both "
            f"{sorted(_DUPLICATE_INACTIVE_ENTITY_IDS)} — refusing to process either without "
            "the full known pair present, since the precondition checks their relationship."
        )

    rows = {}
    for eid in pair:
        row = memory_conn.execute("SELECT id, user_id FROM hearth_entities WHERE id = ?;", (eid,)).fetchone()
        if row is None:
            return False, f"entity {eid} does not exist in hearth_entities — refusing."
        rows[eid] = row

    user_ids = {rows[eid]["user_id"] for eid in pair}
    if len(user_ids) != 1 or None in user_ids:
        return False, (
            f"entities {pair} do not map to the same non-null Pathway user_id "
            f"({ {eid: rows[eid]['user_id'] for eid in pair} }) — refusing; this build does not "
            "repair the duplicate-entity defect itself."
        )
    user_id = next(iter(user_ids))

    if pathway_conn is None:
        return False, "no Pathway connection available — cannot confirm user inactivity; refusing."

    user_row = pathway_conn.execute("SELECT id, status FROM users WHERE id = ?;", (user_id,)).fetchone()
    if user_row is None:
        return False, f"Pathway user_id={user_id} not found — refusing."
    if user_row["status"] == "inactive":
        status_ok = True
    else:
        status_ok = False

    inactive_ids = hearth_identity.get_inactive_user_ids({user_id}, conn=pathway_conn)
    filter_ok = user_id in inactive_ids

    if not (status_ok and filter_ok):
        return False, (
            f"Pathway user_id={user_id} status={user_row['status']!r} (status_ok={status_ok}), "
            f"and the existing deactivation filter reports "
            f"in_inactive_set={filter_ok} — both must hold before this script will touch "
            "entities 15/16; refusing."
        )

    return True, (
        f"entities {pair} both map to Pathway user_id={user_id}, confirmed status='inactive', "
        "and the existing deactivation defense-in-depth filter "
        "(hearth_identity.get_inactive_user_ids, the same function hearth_context.build_context "
        "Fix 3 uses) confirms that user's episodes are already excluded from Daily Brief. "
        "Proceeding to close invalid evaluator-promoted rows as moot."
    )


# ---------------------------------------------------------------------------
# Backup
# ---------------------------------------------------------------------------

def backup_memory_db(db_path):
    backups_dir = os.path.join(os.path.dirname(os.path.abspath(db_path)) or ".", "backups")
    os.makedirs(backups_dir, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    backup_path = os.path.join(
        backups_dir, f"{os.path.basename(db_path)}.pre_evaluator_cleanup_{ts}.bak"
    )
    shutil.copy2(db_path, backup_path)
    return backup_path


# ---------------------------------------------------------------------------
# Entity-volume uncertainty — report-only, never auto-resolved
# ---------------------------------------------------------------------------

_VOLUME_KEYWORDS = ("volume", "episode", "pattern", "many", "number of", "duplicate")


def find_entity_volume_uncertainty_candidates(memory_conn, entity_id):
    """Read-only search for a living worldview uncertainty/change that might
    be the "entity-volume uncertainty" that surfaced this defect for this
    entity. Never auto-resolved by this script — reported for Brian's
    manual review only, per the build's instruction not to guess.
    """
    candidates = []
    for row in hearth_worldview.get_living_uncertainties(memory_conn, limit=500):
        text = " ".join(filter(None, [
            row["subject_id"], row["uncertainty_text"], row["why_it_matters"],
        ])).lower()
        if str(entity_id) in (row["subject_id"] or "") and any(k in text for k in _VOLUME_KEYWORDS):
            candidates.append({"table": "hearth_worldview_uncertainties", "id": row["id"],
                                "subject_id": row["subject_id"], "text": row["uncertainty_text"]})
    for row in hearth_worldview.get_watched_changes(memory_conn, limit=500):
        text = " ".join(filter(None, [row["subject_id"], row["change_text"]])).lower()
        if str(entity_id) in (row["subject_id"] or "") and any(k in text for k in _VOLUME_KEYWORDS):
            candidates.append({"table": "hearth_worldview_changes", "id": row["id"],
                                "subject_id": row["subject_id"], "text": row["change_text"]})
    return candidates


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _deterministic_confidence_rebuild_available():
    """hearth_soul.py's responsiveness belief only ever nudges confidence up
    or down from whatever it currently is (_upsert_responsiveness_belief) —
    there is no function that recomputes it from scratch given a set of
    valid episodes. Returns False; kept as a named check (not a bare
    False literal in main()) so a future hearth_soul.py addition of a real
    rebuild function has one obvious place to wire in.
    """
    return False


def run_cleanup(entity_ids, apply_changes, memory_conn, pathway_conn):
    """Core cleanup logic, DB-connection-injectable for tests. Returns a
    report dict. Never opens/closes connections or touches env vars itself
    — see main() for the CLI wrapper that does.
    """
    report = {"apply": apply_changes, "entities": {}, "skipped_entities": {}}

    precheck_ok, precheck_message = check_duplicate_inactive_entity_precondition(
        memory_conn, pathway_conn, entity_ids,
    )
    duplicate_pair_present = any(eid in _DUPLICATE_INACTIVE_ENTITY_IDS for eid in entity_ids)
    if duplicate_pair_present:
        report["duplicate_inactive_entity_precheck"] = {"ok": precheck_ok, "message": precheck_message}
        print(f"{_LOG_PREFIX} entities 15/16 precondition: {'OK' if precheck_ok else 'FAILED'} — {precheck_message}")

    for entity_id in entity_ids:
        is_duplicate_pair_member = entity_id in _DUPLICATE_INACTIVE_ENTITY_IDS
        if is_duplicate_pair_member and not precheck_ok:
            report["skipped_entities"][entity_id] = precheck_message
            print(f"{_LOG_PREFIX} entity {entity_id}: SKIPPED — {precheck_message}")
            continue

        decisions = evaluate_entity_rows(memory_conn, pathway_conn, entity_id)
        reason = _MOOT_REASON if is_duplicate_pair_member else _INVALID_REASON

        changed_any = False
        for decision in decisions:
            if decision["action"] == "invalidate" and not decision["resolved"]:
                print(
                    f"{_LOG_PREFIX} entity {entity_id} episode {decision['episode_id']}"
                    f" ({decision['episode_type']}): {'would close' if not apply_changes else 'closing'}"
                    f" — {decision['reason']}"
                )
                if apply_changes:
                    hearth_memory.resolve_episode(
                        memory_conn, decision["episode_id"], resolution_reason=reason,
                    )
                    changed_any = True
            elif decision["action"] == "invalidate" and decision["resolved"]:
                decision["note"] = "already resolved — idempotent no-op."
            elif decision["action"] == "retain":
                print(
                    f"{_LOG_PREFIX} entity {entity_id} episode {decision['episode_id']}"
                    f" ({decision['episode_type']}): RETAINED — {decision['reason']}"
                )
            else:
                print(
                    f"{_LOG_PREFIX} entity {entity_id} episode {decision['episode_id']}"
                    f" ({decision['episode_type']}): CANNOT VERIFY — {decision['reason']}"
                )

        if apply_changes and changed_any:
            hearth_memory.process_entity_observations(memory_conn, entity_id)

        volume_candidates = find_entity_volume_uncertainty_candidates(memory_conn, entity_id)

        report["entities"][entity_id] = {
            "decisions": decisions,
            "resolution_reason_used": reason,
            "entity_volume_uncertainty_candidates": volume_candidates,
            "responsiveness_belief_note": (
                "not rebuilt — hearth_soul.py has no deterministic rebuild-from-evidence "
                "function for the responsiveness belief (only incremental confirm/challenge "
                "nudges); manual review recommended for this entity's current confidence value."
                if not _deterministic_confidence_rebuild_available() else None
            ),
        }

    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--entity-id", type=int, action="append", required=True, dest="entity_ids",
                         help="Hearth entity id to clean up. Repeatable.")
    parser.add_argument("--apply", action="store_true", default=False,
                         help="Actually close invalid rows. Omit for dry-run (default).")
    parser.add_argument("--db-path", default=None, help="Override hearth_memory.db path.")
    parser.add_argument("--report-path", default=None, help="Optional path to write a JSON report.")
    args = parser.parse_args()

    if args.apply and evaluator.HEARTH_EXPERIENCE_EVALUATOR_PROMOTE:
        raise SystemExit(
            f"{_LOG_PREFIX} REFUSING to --apply: HEARTH_EXPERIENCE_EVALUATOR_PROMOTE=1. "
            "Promotion must stay disabled during cleanup. Set it to 0 and re-run."
        )

    db_path = args.db_path or hearth_memory.MEMORY_DB_PATH
    print(f"{_LOG_PREFIX} target database: {db_path}")
    print(f"{_LOG_PREFIX} mode: {'APPLY' if args.apply else 'DRY RUN (default — nothing will change)'}")

    if args.apply:
        backup_path = backup_memory_db(db_path)
        print(f"{_LOG_PREFIX} backup written: {backup_path}")

    memory_conn = sqlite3.connect(db_path)
    memory_conn.row_factory = sqlite3.Row
    memory_conn.execute("PRAGMA foreign_keys=ON;")

    pathway_conn = None
    if evaluator.DATABASE_URL:
        pathway_db_path = evaluator._resolve_db_path(evaluator.DATABASE_URL)
        pathway_conn = evaluator.get_pathway_readonly_connection(pathway_db_path)
    else:
        print(f"{_LOG_PREFIX} WARNING: DATABASE_URL not set — cannot read hearth_events;"
              " all rows will be reported as cannot_verify.")

    try:
        report = run_cleanup(args.entity_ids, args.apply, memory_conn, pathway_conn)
    finally:
        memory_conn.close()
        if pathway_conn is not None:
            pathway_conn.close()

    if args.report_path:
        with open(args.report_path, "w") as f:
            json.dump(report, f, indent=2, default=str)
        print(f"{_LOG_PREFIX} report written: {args.report_path}")

    print(f"{_LOG_PREFIX} done.")
    return report


if __name__ == "__main__":
    main()
