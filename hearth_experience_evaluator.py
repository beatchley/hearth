"""
Hearth Experience Evaluator — V3 (governed, momentum-only).

Bridges Pulse signals and Hearth episodes. Pulse notices activity and
classifies hearth_events as trace/signal/observation. This module asks the
next question: "Does this signal meaningfully change understanding relative
to worldview?" If yes, it is promoted into a Hearth episode.

Default behavior is observation only: it reads Pulse-classified events and
Hearth's worldview, classifies each event into a candidate type, and reports
what it would do. Setting HEARTH_EXPERIENCE_EVALUATOR_PROMOTE=1 enables the
write path, which turns momentum candidates into Hearth episodes. It never
writes to worldview and never updates hearth_events.

Architecture:
    Pulse (hearth_events) -> Experience Evaluator -> episode candidate
        -> [future] episode -> Soul -> worldview

The rule is not "training_viewed becomes an episode." The rule is "a signal
becomes an episode when it changes understanding relative to worldview, via
an explicit, allowlisted relationship to a specific target." Worldview is
the filter that keeps most signals as no_match.

--- Permanent scope: momentum only -------------------------------------

This module detects momentum/trend patterns from general Pathway activity
— nothing else. Concern and resolution detection are permanently out of
scope here, by architecture decision, not because the rules haven't been
written yet. Concerns and resolutions belong exclusively to purpose-built
Watchers (morning_briefing.py's detect_*/resolve_* functions), which have
real, specific knowledge of the condition they check — an unanswered
training comment, a missing Discord invite, a check-in still awaiting
feedback. This module only ever sees Pulse's general activity stream; it
has no comparable specific knowledge, and guessing at it from general
activity is exactly what caused the V1 defect this file was rewritten to
fix (duplicate/contradictory episodes affecting entities 11, 39, and the
15/16 duplicate-inactive-entity pair — see
migrate_add_experience_evaluator_governance.py and
hearth_experience_evaluator_cleanup.py for the full incident and repair).

Concretely, this means:
  - There is no resolution-rule table, no concern-rule table, and no code
    path that resolves an original watcher episode or creates a "concern"/
    "resolution"-type episode. That machinery existed briefly after the V1
    fix (proven correct via an injected synthetic test rule) but has been
    removed entirely now that the scope decision is permanent — an empty
    rule table would still invite "not filled in yet"; removing the code
    is the honest way to say "this will never happen here."
  - Target discovery (_gather_entity_targets) only looks for quiet/stuck
    situations (the "quiet" family below) — the only thing momentum ever
    reacts to. It no longer looks for "waiting" (staff-response-pending)
    targets at all, since nothing in this module ever matched against
    them once the resolution/concern paths were removed.
  - classify_event's only positive outcome is "momentum". Every other
    outcome (no_match, rejected_unrelated, failed_retryable) is a
    non-action, exactly as before.

--- V2 defect fix (historical — see migrate_add_experience_evaluator_governance.py) ---

V1 had three confirmed defects, all fixed in V2 and still true here:

1. No durable event-processing boundary. V1 rescanned the latest 50 events
   on every run and re-derived worldview context independently per branch,
   so the same source event could classify differently across runs as
   Worldview changed underneath it (e.g. an uncertainty moving open ->
   question_surfaced). V2+ evaluates each eligible event against one
   in-run-memoized snapshot (_RunSnapshot) and records a durable ledger row
   (hearth_experience_evaluations) — see _get_unevaluated_signal_events. An
   event that already has a 'processed' ledger row is never reselected
   (see EVALUATOR_VERSION below for exactly what "already processed" means
   across a version change).

2. Dedup was keyed on (episode_type, reference_key), not the source event,
   so a reclassified event evaded dedup entirely by producing a different
   episode_type. The ledger's uniqueness is keyed on the source event
   itself, and hearth_episodes' own (episode_type, reference_key) dedup is
   hardened with a database-level partial unique index (see the migration).

3. Two invalid semantic rules existed: `checkin_submitted` was treated as
   resolving `checkin_feedback_waiting`, and a generic "any event + any
   open waiting item = concern" fallback existed. Both were deleted in V2.
   The permanent-scope decision above supersedes V2's original framing
   (which left the door open to a future resolution/concern rule if
   Pathway ever emitted the right events) — concern/resolution detection
   is not coming back to this module regardless of what Pathway emits.

Target discovery is structural, not keyword-based: it only considers
worldview rows shaped like a specific watcher's output (hearth_worldview
"entity_episode" uncertainties keyed "{episode_type}:{entity_id}", and
"creator_quiet_entity" watched changes), because those rows are the ones
hearth_soul.py populates with source_episode_id — the real hearth_episodes.id
of the original watcher episode (see hearth_soul._upsert_single_episode_uncertainty
and _upsert_creator_quiet_watch). Generic beliefs and entity-level watched
changes are not scanned — they carry no such id and were only ever a source
of false positives.

Generic by design — entity resolution and worldview lookups go through
hearth_memory/hearth_worldview, so this should extend to non-Pathway sources
(e.g. Goose) without rework, as long as they can resolve to a hearth_entities
row and write events into a compatible table.
"""

import os
import sqlite3
from datetime import datetime, timezone

from dotenv import load_dotenv

import hearth_memory
import hearth_worldview

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")

_LOG_PREFIX = "[HEARTH EXPERIENCE]"

# EVALUATOR_VERSION identifies which code version produced a given ledger
# row — an audit/history stamp, NOT a re-processing trigger. Bumping this
# constant does NOT cause previously-processed events to become eligible
# for normal selection again: _get_unevaluated_signal_events
# (via _already_processed_event_ids) excludes any source event with a
# 'processed' ledger row under ANY version, not just the current one. "New
# logic applies only to new activity going forward" is a deliberate policy,
# not an oversight — this module's own history (the V1 defect) already
# showed what happens when reclassification under changed state is allowed
# to happen silently across runs; the same principle applies across a
# version change. If historical events genuinely need re-evaluation under
# corrected rules some day, that must be a separate, explicitly-invoked
# tool a human runs deliberately — following the same reuse-not-reimplement
# pattern hearth_experience_evaluator_cleanup.py already established for
# the entities-11/39/15/16 cleanup — never something that happens
# automatically just because this constant changed. See
# test_experience_evaluator_processing.py:test_version_bump_does_not_auto_replay_history.
#
# Bumped to "3" for the momentum-only permanent-scope change above (target
# discovery and classification genuinely changed) — safe to bump precisely
# because of the policy this comment describes: it changes the audit stamp
# on future ledger rows, nothing else.
EVALUATOR_VERSION = "3"

# Feature flags — see module docstring. Promotion stays off by default;
# enabling it turns momentum candidates into Hearth episodes.
HEARTH_EXPERIENCE_EVALUATOR_ENABLED = (
    os.environ.get("HEARTH_EXPERIENCE_EVALUATOR_ENABLED", "1") == "1"
)
HEARTH_EXPERIENCE_EVALUATOR_PROMOTE = (
    os.environ.get("HEARTH_EXPERIENCE_EVALUATOR_PROMOTE", "0") == "1"
)

_DEFAULT_LIMIT = 50
# How many candidate hearth_events rows (oldest-eligible-first) to consider
# per run before filtering out already-ledgered ones. Bounded, not
# unbounded — but unlike V1's "latest 50" this makes real forward progress
# across runs on a backlog, since retired events are never rescanned.
_SCAN_WINDOW_MULTIPLIER = 10
_MIN_SCAN_WINDOW = 500

# Only these Pulse experience levels are evaluated. Trace events are noise
# by Pulse's own classification and are never read here.
_EVALUABLE_EXPERIENCE_LEVELS = ("signal", "observation")

# Event types that represent a creator doing something positive/re-engaging.
# Hearth intentionally excludes private creator-to-creator conversations.
# Organizational intelligence should observe organizational activity, not
# private communication. This is a permanent architectural boundary.
_POSITIVE_ACTIVITY_EVENT_TYPES = frozenset({
    "training_viewed",
    "user_signed_in",
    "community_message_created",
    "battle_requested",
    "event_signup_created",
    "checkin_submitted",
    "onboarding_step_completed",
})

# hearth_soul._SINGLE_SIGNIFICANCE_TYPES episode_type that earns a
# structural "entity_episode" worldview uncertainty carrying a real
# source_episode_id, for the one target family this module still looks
# for: the creator hasn't engaged at all. creator_quiet itself is tracked
# separately as a watched change, not an entity_episode uncertainty — see
# _QUIET_CHANGE_SUBJECT_TYPE below.
_QUIET_ENTITY_EPISODE_TYPES = frozenset({"new_creator_stuck"})

_ENTITY_EPISODE_SUBJECT_TYPE = "entity_episode"
_QUIET_CHANGE_SUBJECT_TYPE = "creator_quiet_entity"

_WORLDVIEW_SCAN_LIMIT = 500  # safety cap when scanning entity_episode rows

# severity / briefing_category for promoted momentum episodes. "momentum" is
# new to hearth_memory's maps, so it's passed explicitly rather than relying
# on the default (which would leave briefing_category NULL).
_MOMENTUM_SEVERITY = "low"
_MOMENTUM_BRIEFING_CATEGORY = "awareness"

_LEDGER_TABLE = "hearth_experience_evaluations"

# ---------------------------------------------------------------------------
# Relationship rules (explicit allowlist)
# ---------------------------------------------------------------------------
#
# A rule is a dict: {"rule_id": str, "target_families": frozenset[str],
# "event_types": frozenset[str]}. A rule matches one (event, target) pair
# when the event's type is in event_types and the target's family is in
# target_families. This is the only rule table this module has, or will
# have — see the module docstring's "Permanent scope" section. Passed as a
# parameter through the call chain so tests can inject a rule table without
# touching production behavior (e.g. to simulate a rule-evaluation failure).

_MOMENTUM_RULES = (
    {
        "rule_id": "momentum_v1_quiet_reactivation",
        "target_families": frozenset({"quiet"}),
        "event_types": _POSITIVE_ACTIVITY_EVENT_TYPES,
    },
)


# ---------------------------------------------------------------------------
# Connections
# ---------------------------------------------------------------------------

def _resolve_db_path(database_url):
    if database_url.startswith("sqlite:///"):
        return database_url[len("sqlite:///"):]
    return database_url


def get_pathway_readonly_connection(db_path):
    """Read-only connection to the Pathway DB — this module never writes to it."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _governance_tables_present(memory_conn):
    """True if the migration this evaluator depends on has been run.

    Fail-closed guard: the evaluator refuses to run against a database
    missing its durable ledger rather than silently falling back to V1's
    rescan-and-guess behavior.
    """
    existing = {
        row["name"]
        for row in memory_conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table';"
        )
    }
    return _LEDGER_TABLE in existing


# ---------------------------------------------------------------------------
# Target discovery — structural, not keyword-based
# ---------------------------------------------------------------------------

def _gather_entity_targets(memory_conn, entity_id):
    """Return living, structurally-identified quiet/stuck targets for one
    entity — the only kind of target this module ever reacts to.

    Each target is a dict:
        {
            "family": "quiet",
            "episode_type": the original watcher's episode_type,
            "target_type": "hearth_episode" | "worldview_uncertainty" | "worldview_change",
            "target_id": str,
        }

    "hearth_episode" targets carry a real hearth_episodes.id (the original
    watcher episode) via the worldview row's source_episode_id — this is
    the exact target a momentum episode's provenance points back to. Rows
    without a source_episode_id (legacy, predating that field) fall back
    to referencing the worldview row itself — still a legitimate "explicit
    operational record" for provenance purposes.

    Only two structurally-exact worldview shapes are considered:
    hearth_soul-authored "entity_episode" uncertainties (subject_id =
    "{episode_type}:{entity_id}", filtered to the "new_creator_stuck"
    episode_type) and "creator_quiet_entity" watched changes (subject_id =
    str(entity_id)). Both are populated exclusively by hearth_soul.py from
    a specific triggering episode — unlike generic beliefs or entity-level
    changes, they cannot be triggered by unrelated free text, which is what
    makes an allowlisted rule match on them a real semantic relationship
    rather than a keyword coincidence.

    Uses get_living_uncertainties (open + question_surfaced) rather than
    get_open_uncertainties, so a UI-surfaced (question_surfaced) uncertainty
    is not invisible to evaluation.
    """
    targets = []
    suffix = f":{entity_id}"

    for row in hearth_worldview.get_living_uncertainties(
        memory_conn, subject_type=_ENTITY_EPISODE_SUBJECT_TYPE, limit=_WORLDVIEW_SCAN_LIMIT,
    ):
        subject_id = row["subject_id"] or ""
        if not subject_id.endswith(suffix):
            continue
        episode_type = subject_id[: -len(suffix)]
        if episode_type not in _QUIET_ENTITY_EPISODE_TYPES:
            continue
        source_episode_id = row["source_episode_id"]
        if source_episode_id:
            target_type, target_id = "hearth_episode", str(source_episode_id)
        else:
            target_type, target_id = "worldview_uncertainty", str(row["id"])
        targets.append({
            "family": "quiet",
            "episode_type": episode_type,
            "target_type": target_type,
            "target_id": target_id,
        })

    for row in hearth_worldview.get_watched_changes(
        memory_conn, subject_type=_QUIET_CHANGE_SUBJECT_TYPE, subject_id=str(entity_id),
    ):
        source_episode_id = row["source_episode_id"]
        if source_episode_id:
            target_type, target_id = "hearth_episode", str(source_episode_id)
        else:
            target_type, target_id = "worldview_change", str(row["id"])
        targets.append({
            "family": "quiet",
            "episode_type": "creator_quiet",
            "target_type": target_type,
            "target_id": target_id,
        })

    return targets


class _RunSnapshot:
    """Per-run memoized worldview target cache.

    Snapshot boundary: one instance per call to evaluate_recent_signals. The
    first time an entity is touched during a run, its living targets are
    fetched once via _gather_entity_targets and cached here; every
    subsequent classification for that entity in the same run reads the
    cached list, so a single run can never see two different, conflicting
    views of the same entity's worldview state. The next scheduler tick
    constructs a brand new _RunSnapshot and re-fetches fresh state.
    """

    def __init__(self, memory_conn):
        self._memory_conn = memory_conn
        self._cache = {}

    def targets_for_entity(self, entity_id):
        if entity_id not in self._cache:
            self._cache[entity_id] = _gather_entity_targets(self._memory_conn, entity_id)
        return self._cache[entity_id]


# ---------------------------------------------------------------------------
# Per-event classification
# ---------------------------------------------------------------------------

def _match_rule(event_type, targets, rules):
    """Return (rule, target) for the first rule/target pair that matches, or
    (None, None). Rules are checked in table order; targets in discovery
    order — both are stable, so this is deterministic given a fixed
    snapshot and rule table (no "arbitrary matching" across repeated runs).
    """
    for rule in rules:
        if event_type not in rule["event_types"]:
            continue
        for target in targets:
            if target["family"] in rule["target_families"]:
                return rule, target
    return None, None


def _result(event, classification, entity_id=None, target_type=None, target_id=None,
            rule_id=None, reason=""):
    return {
        "event_id": event["id"],
        "event_type": event["event_type"],
        "classification": classification,
        "entity_id": str(entity_id) if entity_id is not None else None,
        "target_type": target_type,
        "target_id": target_id,
        "rule_id": rule_id,
        "reason": reason,
    }


def _classify_event(event, entity_id, targets, momentum_rules):
    """Classify one Pulse-classified event against one entity's living
    quiet/stuck targets. Read-only — never writes. Deterministic given a
    fixed snapshot.

    Returns a result dict (see _result) with classification one of:
    "momentum", "no_match", "rejected_unrelated". There is no "resolution"
    or "concern" outcome — permanently out of scope for this module, see
    the module docstring's "Permanent scope" section.
    """
    event_type = event["event_type"]

    if not targets:
        return _result(
            event, "no_match", entity_id,
            reason="No living worldview target exists for this entity.",
        )

    rule, target = _match_rule(event_type, targets, momentum_rules)
    if rule is not None:
        return _result(
            event, "momentum", entity_id,
            target_type=target["target_type"], target_id=target["target_id"],
            rule_id=rule["rule_id"],
            reason=f"Event type '{event_type}' matched momentum rule '{rule['rule_id']}'.",
        )

    families = sorted({t["family"] for t in targets})
    return _result(
        event, "rejected_unrelated", entity_id,
        reason=(
            f"Entity {entity_id} has living target(s) in family/families {families}, "
            f"but no allowlisted momentum rule connects event type '{event_type}' to any of them."
        ),
    )


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------

def _record_evaluation(memory_conn, *, source_event_id, evaluator_version, status,
                        classification=None, entity_id=None, target_type=None, target_id=None,
                        rule_id=None, resulting_episode_id=None, resulting_action=None,
                        reason=None, error_detail=None):
    """Durable upsert of one (source_event_id, evaluator_version) ledger row.

    evaluator_version here is always the CURRENT EVALUATOR_VERSION — an
    audit stamp of which code version produced this row, not a selection
    scope (see EVALUATOR_VERSION's docstring and _already_processed_event_ids).

    Returns the ledger row id. Safe under a concurrent writer: SQLite
    serializes the ON CONFLICT DO UPDATE, so two overlapping runs converge
    to one consistent row rather than producing two.
    """
    now = datetime.now(timezone.utc).isoformat()
    memory_conn.execute(
        f"""
        INSERT INTO {_LEDGER_TABLE}
            (source_event_id, evaluator_version, status, classification, entity_id,
             target_type, target_id, rule_id, resulting_episode_id, resulting_action,
             reason, error_detail, evaluated_at, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source_event_id, evaluator_version) DO UPDATE SET
            status = excluded.status,
            classification = excluded.classification,
            entity_id = excluded.entity_id,
            target_type = excluded.target_type,
            target_id = excluded.target_id,
            rule_id = excluded.rule_id,
            resulting_episode_id = excluded.resulting_episode_id,
            resulting_action = excluded.resulting_action,
            reason = excluded.reason,
            error_detail = excluded.error_detail,
            evaluated_at = excluded.evaluated_at,
            updated_at = excluded.updated_at;
        """,
        (source_event_id, evaluator_version, status, classification, entity_id,
         target_type, target_id, rule_id, resulting_episode_id, resulting_action,
         reason, error_detail, now, now, now),
    )
    memory_conn.commit()
    row = memory_conn.execute(
        f"SELECT id FROM {_LEDGER_TABLE} WHERE source_event_id = ? AND evaluator_version = ?;",
        (source_event_id, evaluator_version),
    ).fetchone()
    return row["id"]


def _already_processed_event_ids(memory_conn):
    """Source event ids with a 'processed' (terminal) ledger row under ANY
    evaluator_version.

    Deliberately version-agnostic — see EVALUATOR_VERSION's docstring: a
    version bump must never cause normal selection to re-surface an event
    that already reached a terminal outcome under an older version. A row
    that only ever reached 'failed_retryable' does NOT appear here, so it
    remains normally selectable (and retryable) regardless of version —
    it was never actually terminal in the first place.
    """
    rows = memory_conn.execute(
        f"SELECT DISTINCT source_event_id FROM {_LEDGER_TABLE} WHERE status = 'processed';"
    ).fetchall()
    return {row["source_event_id"] for row in rows}


# ---------------------------------------------------------------------------
# Actions (write path — only reached when promote=True)
# ---------------------------------------------------------------------------

def _promotion_reference_key(event):
    """Stable provenance key tying a promoted episode back to its Pulse event."""
    return f"pulse_event_{event['id']}"


def _promotion_description(event, result, display_name):
    """Human-readable episode body that preserves where the episode came from."""
    who = f"@{display_name}" if display_name else f"user_id={event['actor_user_id'] or event['target_user_id']}"
    return (
        f"Promoted from Pulse signal: {who} — {event['event_type']}"
        f" at {event['occurred_at']} (category={result['classification']}, "
        f"rule={result['rule_id']}). {result['reason']} [source: hearth_events#{event['id']}]"
    )


def _promote_candidate(result, event, memory_conn):
    """Write one momentum candidate into a Hearth episode.

    Reuses hearth_memory.create_episode (the same helper every watcher
    uses). Returns the (episode_id, action) tuple from create_episode.
    """
    classification = result["classification"]  # always "momentum" — see module docstring
    entity_id = int(result["entity_id"])

    entity_row = memory_conn.execute(
        "SELECT display_name FROM hearth_entities WHERE id = ?;", (entity_id,)
    ).fetchone()
    display_name = entity_row["display_name"] if entity_row else None

    return hearth_memory.create_episode(
        memory_conn,
        entity_id,
        classification,
        _promotion_description(event, result, display_name),
        severity=_MOMENTUM_SEVERITY,
        reference_key=_promotion_reference_key(event),
        briefing_category=_MOMENTUM_BRIEFING_CATEGORY,
    )


# ---------------------------------------------------------------------------
# Processor
# ---------------------------------------------------------------------------

def _get_unevaluated_signal_events(pathway_conn, memory_conn, limit):
    """Return up to `limit` eligible events not yet terminally processed,
    oldest-eligible first.

    Scans a bounded window (not the whole table, not "latest 50" forever) —
    ordering ASC by id means a backlog is cleared in order and retired
    events are never rescanned, so, unlike V1, repeated runs make real
    forward progress instead of perpetually reconsidering the same tail.

    "Not yet terminally processed" is checked across ALL evaluator
    versions (see _already_processed_event_ids) — a version bump never
    re-queues history here; see EVALUATOR_VERSION's docstring.
    """
    scan_window = max(limit * _SCAN_WINDOW_MULTIPLIER, _MIN_SCAN_WINDOW)
    placeholders = ", ".join("?" for _ in _EVALUABLE_EXPERIENCE_LEVELS)
    candidates = pathway_conn.execute(
        "SELECT * FROM hearth_events"
        f" WHERE processed = 1 AND experience_level IN ({placeholders})"
        " ORDER BY id ASC LIMIT ?;",
        (*_EVALUABLE_EXPERIENCE_LEVELS, scan_window),
    ).fetchall()

    already_processed = _already_processed_event_ids(memory_conn)
    unevaluated = [e for e in candidates if e["id"] not in already_processed]
    return unevaluated[:limit]


def evaluate_recent_signals(limit=_DEFAULT_LIMIT, pathway_conn=None, memory_conn=None,
                             promote=None, evaluator_version=EVALUATOR_VERSION,
                             momentum_rules=_MOMENTUM_RULES):
    """Evaluate up to `limit` not-yet-terminally-processed Pulse events.

    Never writes to worldview or updates hearth_events. When promote is off
    (default: HEARTH_EXPERIENCE_EVALUATOR_PROMOTE), momentum candidates are
    reported but not acted on or ledgered as terminal — see module
    docstring for why. no_match/rejected_unrelated/failed_retryable
    outcomes are always ledgered regardless of promote, since they don't
    depend on it and permanently retiring them is what stops the endless
    rescan.

    pathway_conn/memory_conn/promote/evaluator_version/momentum_rules are
    injectable for tests; production callers (run_experience_evaluator, the
    __main__ block, the scheduler) omit them and get the real env-driven
    connections and the production rule table.

    Returns:
        {
            "evaluated": int,
            "candidates": [...],   # momentum candidates this run
            "no_match": int,
            "rejected": int,
            "promoted": int,      # episodes created this run
            "duplicates": int,    # candidates already promoted, skipped
            "failed": int,        # evaluations that errored (retryable)
        }
    """
    _empty = {"evaluated": 0, "candidates": [], "no_match": 0, "rejected": 0,
              "promoted": 0, "duplicates": 0, "failed": 0}
    if not HEARTH_EXPERIENCE_EVALUATOR_ENABLED:
        print(f"{_LOG_PREFIX} disabled via HEARTH_EXPERIENCE_EVALUATOR_ENABLED — skipping run.")
        return dict(_empty)

    if promote is None:
        promote = HEARTH_EXPERIENCE_EVALUATOR_PROMOTE

    owns_pathway_conn = pathway_conn is None
    owns_memory_conn = memory_conn is None

    if owns_pathway_conn:
        if not DATABASE_URL:
            print(f"{_LOG_PREFIX} DATABASE_URL is not set — cannot read hearth_events. Skipping run.")
            return dict(_empty)
        db_path = _resolve_db_path(DATABASE_URL)
        pathway_conn = get_pathway_readonly_connection(db_path)
    if owns_memory_conn:
        memory_conn = hearth_memory.get_memory_connection()

    try:
        if not _governance_tables_present(memory_conn):
            print(
                f"{_LOG_PREFIX} governance tables missing — run "
                "migrate_add_experience_evaluator_governance.py first. Skipping run "
                "(fail closed rather than falling back to unsafe V1 behavior)."
            )
            return dict(_empty)

        events = _get_unevaluated_signal_events(pathway_conn, memory_conn, limit)
        snapshot = _RunSnapshot(memory_conn)

        candidates = []
        no_match_count = 0
        rejected_count = 0
        promoted_count = 0
        duplicate_count = 0
        failed_count = 0

        for event in events:
            actor_id = event["actor_user_id"] if event["actor_user_id"] is not None else event["target_user_id"]
            entity_id = None
            try:
                if actor_id is None:
                    result = _result(event, "no_match", reason="Event has no actor or target user to map to an entity.")
                else:
                    entity_row = hearth_memory.get_entity_by_user_id(memory_conn, actor_id)
                    if entity_row is None:
                        result = _result(
                            event, "no_match",
                            reason=f"No Hearth entity exists yet for user_id={actor_id}.",
                        )
                    else:
                        entity_id = entity_row["id"]
                        targets = snapshot.targets_for_entity(entity_id)
                        result = _classify_event(event, entity_id, targets, momentum_rules)
            except Exception as exc:  # never let one failure abort the scan
                _record_evaluation(
                    memory_conn, source_event_id=event["id"], evaluator_version=evaluator_version,
                    status="failed_retryable", error_detail=str(exc),
                )
                failed_count += 1
                print(f"{_LOG_PREFIX} evaluation FAILED (retryable) — event_id={event['id']}: {exc}")
                continue

            classification = result["classification"]

            if classification in ("no_match", "rejected_unrelated"):
                if classification == "no_match":
                    no_match_count += 1
                else:
                    rejected_count += 1
                _record_evaluation(
                    memory_conn, source_event_id=event["id"], evaluator_version=evaluator_version,
                    status="processed", classification=classification, entity_id=entity_id,
                    target_type=result["target_type"], target_id=result["target_id"],
                    rule_id=result["rule_id"], resulting_action="no_action", reason=result["reason"],
                )
                print(f"{_LOG_PREFIX} {classification} — event_id={result['event_id']}"
                      f" type={result['event_type']}: {result['reason']}")
                continue

            candidates.append(result)
            print(f"{_LOG_PREFIX} candidate — event {result['event_id']} as"
                  f" {classification} (rule={result['rule_id']}) — {result['reason']}")

            if not promote:
                continue  # observation only — stays pending for a future run

            try:
                episode_id, action = _promote_candidate(result, event, memory_conn)
                _record_evaluation(
                    memory_conn, source_event_id=event["id"], evaluator_version=evaluator_version,
                    status="processed", classification=classification, entity_id=entity_id,
                    target_type=result["target_type"], target_id=result["target_id"],
                    rule_id=result["rule_id"], resulting_episode_id=episode_id,
                    resulting_action=action, reason=result["reason"],
                )
                if action == "created_episode":
                    promoted_count += 1
                    print(f"{_LOG_PREFIX} promoted event_type={result['event_type']}"
                          f" entity_id={result['entity_id']} classification={classification}"
                          f" (episode_id={episode_id})")
                else:
                    duplicate_count += 1
                    print(f"{_LOG_PREFIX} duplicate skipped — event_id={result['event_id']}"
                          f" already promoted (episode_id={episode_id}).")
            except Exception as exc:  # never let one failure abort the scan
                _record_evaluation(
                    memory_conn, source_event_id=event["id"], evaluator_version=evaluator_version,
                    status="failed_retryable", classification=classification, entity_id=entity_id,
                    target_type=result["target_type"], target_id=result["target_id"],
                    rule_id=result["rule_id"], reason=result["reason"], error_detail=str(exc),
                )
                failed_count += 1
                print(f"{_LOG_PREFIX} action FAILED (retryable) — event_id={result['event_id']}: {exc}")

        print(
            f"{_LOG_PREFIX} evaluated {len(events)} event(s):"
            f" {len(candidates)} candidate(s), {no_match_count} no_match, {rejected_count} rejected."
            + (
                f" Promotion ON — {promoted_count} promoted,"
                f" {duplicate_count} duplicate(s), {failed_count} failed."
                if promote
                else " Promotion OFF (observation only)."
            )
        )

        return {
            "evaluated": len(events),
            "candidates": candidates,
            "no_match": no_match_count,
            "rejected": rejected_count,
            "promoted": promoted_count,
            "duplicates": duplicate_count,
            "failed": failed_count,
        }
    finally:
        if owns_pathway_conn:
            pathway_conn.close()
        if owns_memory_conn:
            memory_conn.close()


def run_experience_evaluator(limit=_DEFAULT_LIMIT):
    """Callable entry point for the scheduler (and manual runs).

    See evaluate_recent_signals for behavior and return shape. This is the
    function to call from a scheduler job — see module docstring / session
    notes for exactly where it should be wired alongside Pulse.
    """
    return evaluate_recent_signals(limit=limit)


if __name__ == "__main__":
    if not DATABASE_URL:
        raise SystemExit("ERROR: DATABASE_URL is missing. Add it to your .env file.")

    print(f"{_LOG_PREFIX} Hearth Experience Evaluator V3 (momentum-only).")
    print(
        f"{_LOG_PREFIX} flags: ENABLED={HEARTH_EXPERIENCE_EVALUATOR_ENABLED}"
        f" PROMOTE={HEARTH_EXPERIENCE_EVALUATOR_PROMOTE} VERSION={EVALUATOR_VERSION}"
    )
    output = run_experience_evaluator()
    print(f"{_LOG_PREFIX} done: {output['evaluated']} evaluated,"
          f" {len(output['candidates'])} candidate(s), {output['no_match']} no_match,"
          f" {output['rejected']} rejected.")
