"""
Hearth Soul — reflective state recorder and worldview reflection engine for
the Hearth AI teammate.

Records operational observations after each pipeline run (a black-box log,
not a journal) and, since Session 3, is the only writer to Hearth's
worldview: it reads the current worldview and the same episode data already
flowing into reflection, weighs it conservatively against existing principles,
and writes belief/uncertainty/change/lesson updates through hearth_worldview.py.

Architecture rule: Pulse filters. Soul interprets. Worldview updates.
Artifacts surface. Soul may suggest lessons; only a human promotes a lesson
into hearth_principles.
"""

import os
import sqlite3
import traceback
from datetime import datetime, timedelta, timezone

import hearth_memory
import hearth_principles
import hearth_questions
import hearth_worldview
from hearth_memory import MEMORY_DB_PATH

# Feature flag for Soul's worldview reflection pass. Defaults on; set the
# HEARTH_WORLDVIEW_ENABLED env var to "0" (e.g. via Render) to disable
# worldview writes without a deploy.
HEARTH_WORLDVIEW_ENABLED = os.environ.get("HEARTH_WORLDVIEW_ENABLED", "1") == "1"


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

def get_reflections_connection():
    """Open a read-write connection to the shared Hearth memory database."""
    conn = sqlite3.connect(MEMORY_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def ensure_reflections_table(conn):
    """Create hearth_reflections if it does not exist. Safe on existing databases."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS hearth_reflections (
            reflection_id              INTEGER PRIMARY KEY AUTOINCREMENT,
            reflection_time            TEXT    NOT NULL,
            what_changed               TEXT,
            what_surprised_me          TEXT,
            what_i_am_uncertain_about  TEXT,
            what_i_should_ask          TEXT,
            source_run                 TEXT
        );
    """)
    conn.commit()


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

def create_reflection(conn, what_changed="", what_surprised_me="",
                      what_i_am_uncertain_about="", what_i_should_ask="",
                      source_run=None):
    """Insert one reflection row and return its reflection_id."""
    now = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        "INSERT INTO hearth_reflections"
        " (reflection_time, what_changed, what_surprised_me,"
        "  what_i_am_uncertain_about, what_i_should_ask, source_run)"
        " VALUES (?, ?, ?, ?, ?, ?);",
        (
            now,
            what_changed or None,
            what_surprised_me or None,
            what_i_am_uncertain_about or None,
            what_i_should_ask or None,
            source_run,
        ),
    )
    conn.commit()
    return cur.lastrowid


def get_recent_reflections(conn, limit=20):
    """Return up to limit reflections ordered by reflection_time DESC."""
    return conn.execute(
        "SELECT * FROM hearth_reflections"
        " ORDER BY reflection_time DESC"
        " LIMIT ?;",
        (limit,),
    ).fetchall()


def get_latest_reflection(conn):
    """Return the most recent reflection row, or None if the table is empty."""
    return conn.execute(
        "SELECT * FROM hearth_reflections"
        " ORDER BY reflection_time DESC"
        " LIMIT 1;",
    ).fetchone()


def summarize_recent_reflections(conn, limit=10):
    """
    Return a short plain-text summary of recent reflections (3-6 bullet lines).
    No AI. Pure string assembly from stored fields. Returns empty string if
    no reflections exist.
    """
    rows = get_recent_reflections(conn, limit=limit)
    if not rows:
        return ""

    seen = set()
    lines = []
    fields = ("what_changed", "what_surprised_me", "what_i_am_uncertain_about")
    for row in rows:
        for field in fields:
            val = row[field]
            if val and val.strip() and val not in seen:
                seen.add(val)
                lines.append(f"- {val.strip()}")
            if len(lines) >= 6:
                break
        if len(lines) >= 6:
            break

    if not lines:
        return ""
    return "Recent Hearth observations:\n" + "\n".join(lines)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _episode_entity(ep):
    """Extract an entity identifier from an episode dict or Row."""
    for key in ("entity_id", "entity", "display_name"):
        try:
            val = ep[key]
            if val is not None:
                return val
        except (KeyError, IndexError, TypeError):
            pass
    return None


def _episode_id(ep):
    """Extract the real hearth_episodes.id from an episode dict or Row, if present.

    Only used where a worldview write is triggered by one specific episode
    (not an aggregate over several) — that id is the correct value for
    source_episode_id. See hearth_worldview._validate_source_episode_id.
    """
    try:
        val = ep["id"]
        return val if val is not None else None
    except (KeyError, IndexError, TypeError):
        return None


def _episode_type(ep):
    """Extract an episode type string from an episode dict or Row."""
    for key in ("episode_type", "type"):
        try:
            val = ep[key]
            if val is not None:
                return str(val)
        except (KeyError, IndexError, TypeError):
            pass
    return None


def _episode_severity(ep):
    """Extract a severity string from an episode dict or Row, or None if absent."""
    try:
        val = ep["severity"]
        return str(val) if val is not None else None
    except (KeyError, IndexError, TypeError):
        return None


def _list_len(val):
    """Return len if val is a list, treat as integer count otherwise."""
    if val is None:
        return 0
    if isinstance(val, list):
        return len(val)
    try:
        return int(val)
    except (TypeError, ValueError):
        return 0


# ---------------------------------------------------------------------------
# Worldview reflection
#
# Soul is the only writer to worldview. These functions read the same
# episode data already passed into generate_reflection() (no new event
# pipeline), compare it against current worldview state, weigh changes using
# existing principles where available, and write conservative updates through
# hearth_worldview.py. Lessons are always written as provisional — only a
# human promotes a lesson into hearth_principles.
# ---------------------------------------------------------------------------

# Thresholds mirror the surprise-detection thresholds already used above in
# generate_reflection's what_surprised_me logic.
_REPEAT_CONCERN_THRESHOLD = 2   # same entity appears this many times in one run
_TYPE_SPIKE_THRESHOLD = 3       # same episode_type appears this many times in one run

_NEW_BELIEF_CONFIDENCE = 0.5
_NEW_UNCERTAINTY_CONFIDENCE = 0.5
_NEW_CHANGE_CONFIDENCE = 0.5
_GROUNDED_DELTA = 0.05          # confidence shift when a supporting principle exists
_UNGROUNDED_DELTA = 0.03        # smaller, more conservative shift otherwise

# Episode types significant enough that a single occurrence still earns a
# cautious worldview entry, separate from the repeat/spike thresholds above
# (those exist for stronger, higher-volume signals and are unchanged). One
# meaningful episode may open a cautious uncertainty; it is never enough on
# its own to create a belief.
_SINGLE_SIGNIFICANCE_TYPES = frozenset({
    "support_request_waiting",
    "checkin_feedback_waiting",
    "training_comment_waiting",
    "new_creator_stuck",
    "onboarding_engagement",
    "missing_discord",
})

# creator_quiet only earns single-episode significance once the watcher in
# morning_briefing.py has already flagged it past its lowest severity band
# (severity is "low" for the first 14-20 quiet days, "medium"/"high" beyond),
# so a creator who just crossed the 14-day line doesn't immediately trigger.
_CREATOR_QUIET_SIGNIFICANT_SEVERITIES = frozenset({"medium", "high"})

_SINGLE_SIGNIFICANCE_CONFIDENCE = 0.5


def _group_episode_counts(episodes):
    """Return (entity_counts, type_counts) dicts from a list of episode dicts/Rows."""
    entity_counts = {}
    type_counts = {}
    for ep in episodes or []:
        entity = _episode_entity(ep)
        if entity is not None:
            entity_counts[entity] = entity_counts.get(entity, 0) + 1
        ep_type = _episode_type(ep)
        if ep_type is not None:
            type_counts[ep_type] = type_counts.get(ep_type, 0) + 1
    return entity_counts, type_counts


def _confidence_delta(conn, topic_tag):
    """Use existing principles to weigh how much a confidence change should move.

    Returns the standard delta if an active principle tags this topic,
    otherwise a smaller, more conservative delta.
    """
    if hearth_principles.get_principles_by_tag(conn, topic_tag):
        return _GROUNDED_DELTA
    return _UNGROUNDED_DELTA


def _entity_display_name(conn, entity_id):
    """Resolve an entity_id to its display_name for use in stored worldview text.

    Falls back to "a team member" (matching hearth_memory.py's convention for
    a missing display_name) rather than leaking the raw numeric id into text
    that becomes a belief/uncertainty/change record.
    """
    row = conn.execute(
        "SELECT display_name FROM hearth_entities WHERE id = ?;", (entity_id,),
    ).fetchone()
    return (row["display_name"] if row else None) or "a team member"


def _upsert_entity_repeat_uncertainty(conn, entity, count, source_run):
    """Open or refresh a living uncertainty about repeated same-run concerns.

    Uses upsert_uncertainty() so existing rows survive a status change to
    question_surfaced — re-observing the same concern refreshes the row
    rather than creating a duplicate.

    This reasons over multiple episodes for the entity (count >= threshold),
    not one specific episode, so there is no single hearth_episodes.id to
    attach — source_run carries the scan-level provenance instead.
    """
    subject_id = str(entity)
    display_name = _entity_display_name(conn, entity)
    text = (
        f"{display_name} had {count} new concern episode(s) in a single run —"
        " unclear if this is a meaningful pattern or coincidence."
    )
    result_id, created = hearth_worldview.upsert_uncertainty(
        conn,
        subject_type="entity",
        subject_id=subject_id,
        uncertainty_text=text,
        why_it_matters=(
            "Repeated same-run concerns may indicate an emerging issue, but a"
            " single run is not enough evidence on its own."
        ),
        possible_question=f"Is {display_name}'s recent concern volume expected or unusual?",
        confidence=_NEW_UNCERTAINTY_CONFIDENCE,
        source_run=source_run,
    )
    if created:
        hearth_worldview.create_entity_ref(
            conn, entity_id=entity, reflection_type="worldview_uncertainty",
            reflection_id=result_id, source="_upsert_entity_repeat_uncertainty",
            confidence=1.0,
        )
    return result_id, created


def _reinforce_recurrence_lesson(conn, episode_type, source_run):
    """Add or confirm a provisional lesson when an episode-type spike recurs across runs.

    Only called when the watched change for episode_type already existed
    (i.e. this is at least the second run it has been seen), so a single
    spike never becomes a lesson by itself. Stays provisional — Soul may
    suggest lessons; only a human may promote one into hearth_principles.
    """
    lesson_text = (
        f"Episode type '{episode_type}' recurring at elevated volume across multiple"
        " scans may indicate a systemic pattern worth reviewing."
    )
    existing = [
        row for row in hearth_worldview.get_recent_lessons(conn, limit=None)
        if row["lesson_text"] == lesson_text
    ]
    if existing:
        hearth_worldview.confirm_recent_lesson(conn, existing[0]["id"])
        return existing[0]["id"], False

    lid = hearth_worldview.add_recent_lesson(
        conn, lesson_text=lesson_text,
        topic_tags=f"episode_type,{episode_type},pattern",
        confidence=_NEW_BELIEF_CONFIDENCE,
        source_run=source_run,
    )
    return lid, True


def _upsert_episode_type_change(conn, episode_type, count, source_run):
    """Record or refresh a watched change for an episode_type volume spike.

    Duplicate protection: at most one watching change per episode_type,
    refreshed (last_seen_at) in place rather than re-created each run. A
    recurrence (the change already existed) also reinforces a provisional
    recent lesson — see _reinforce_recurrence_lesson.
    """
    existing = hearth_worldview.get_watched_changes(
        conn, subject_type="episode_type", subject_id=episode_type, limit=1,
    )
    if existing:
        row = existing[0]
        hearth_worldview.update_change(
            conn, row["id"],
            current_state=f"{count} occurrence(s) in latest run",
            direction="recurring",
            last_seen=True,
        )
        lesson_result = _reinforce_recurrence_lesson(conn, episode_type, source_run)
        return row["id"], False, lesson_result

    cid = hearth_worldview.record_change(
        conn, subject_type="episode_type", subject_id=episode_type,
        change_text=f"Episode type '{episode_type}' is appearing at elevated volume.",
        previous_state="not previously elevated",
        current_state=f"{count} occurrence(s) in latest run",
        direction="increasing",
        confidence=_NEW_CHANGE_CONFIDENCE,
        source_run=source_run,
    )
    return cid, True, None


def _is_individually_significant(ep, episode_type):
    """Whether a single occurrence of this episode is worth a cautious worldview entry.

    Most types in _SINGLE_SIGNIFICANCE_TYPES qualify unconditionally; creator_quiet
    qualifies only once its severity has moved past the lowest band (see
    _CREATOR_QUIET_SIGNIFICANT_SEVERITIES).
    """
    if episode_type in _SINGLE_SIGNIFICANCE_TYPES:
        return True
    if episode_type == "creator_quiet":
        return _episode_severity(ep) in _CREATOR_QUIET_SIGNIFICANT_SEVERITIES
    return False


def _upsert_single_episode_uncertainty(conn, episode_type, entity, source_run, episode_id=None):
    """Open or refresh a cautious living uncertainty for one individually meaningful episode.

    This is deliberately separate from _upsert_entity_repeat_uncertainty (keyed on
    entity alone) and _upsert_episode_type_change (keyed on episode_type alone) — it
    uses subject_type="entity_episode" so the three never collide or overwrite each
    other.

    Uses upsert_uncertainty() so a row that was promoted to question_surfaced
    (i.e. the question was asked of Stacy) is still found and refreshed rather
    than duplicated on the next scan. Confidence stays in the cautious 0.45-0.55
    band — a single concern is worth watching, not a belief.

    Unlike the aggregate helpers, this is triggered by one specific episode —
    episode_id (the triggering episode's real hearth_episodes.id, if known) is
    passed through as source_episode_id; source_run still carries which scan
    produced the write.
    """
    subject_id = f"{episode_type}:{entity}"
    label = episode_type.replace("_", " ")
    display_name = _entity_display_name(conn, entity)
    text = (
        f"It is unclear whether the {label} episode for {display_name} reflects"
        " a meaningful pattern or an isolated event — worth watching."
    )
    result_id, created = hearth_worldview.upsert_uncertainty(
        conn,
        subject_type="entity_episode",
        subject_id=subject_id,
        uncertainty_text=text,
        why_it_matters=(
            f"A single {label} episode may or may not indicate something worth"
            " acting on — Hearth is unsure without more data."
        ),
        possible_question=f"Is the {label} episode for {display_name} part of a larger pattern?",
        confidence=_SINGLE_SIGNIFICANCE_CONFIDENCE,
        source_episode_id=episode_id,
        source_run=source_run,
    )
    if created:
        hearth_worldview.create_entity_ref(
            conn, entity_id=entity, reflection_type="worldview_uncertainty",
            reflection_id=result_id, source="_upsert_single_episode_uncertainty",
            confidence=1.0,
        )
    return result_id, created


def _upsert_creator_quiet_watch(conn, entity, source_run, episode_id=None):
    """Open or refresh a cautious watched change for a creator_quiet episode that has
    already crossed into the medium/high severity band (see
    _CREATOR_QUIET_SIGNIFICANT_SEVERITIES) — quiet duration is motion, not a fixed
    state, so this uses record_change rather than open_uncertainty.

    Duplicate protection: at most one watching change per entity (subject_type=
    "creator_quiet_entity"), refreshed in place rather than re-created each run.

    Triggered by one specific creator_quiet episode — episode_id (its real
    hearth_episodes.id, if known) is passed through as source_episode_id on
    creation; source_run still carries which scan produced the write.
    """
    subject_id = str(entity)
    existing = hearth_worldview.get_watched_changes(
        conn, subject_type="creator_quiet_entity", subject_id=subject_id, limit=1,
    )
    if existing:
        hearth_worldview.update_change(
            conn, existing[0]["id"],
            current_state="still quiet as of latest run",
            direction="unclear",
            last_seen=True,
        )
        return existing[0]["id"], False

    cid = hearth_worldview.record_change(
        conn, subject_type="creator_quiet_entity", subject_id=subject_id,
        change_text=(
            f"{_entity_display_name(conn, entity)} has gone quiet for an extended period."
            " This may indicate disengagement, or it may be a temporary lull —"
            " it is unclear yet."
        ),
        previous_state="not previously flagged at this severity",
        current_state="quiet as of latest run",
        direction="unclear",
        confidence=_SINGLE_SIGNIFICANCE_CONFIDENCE,
        source_episode_id=episode_id,
        source_run=source_run,
    )
    hearth_worldview.create_entity_ref(
        conn, entity_id=entity, reflection_type="worldview_change",
        reflection_id=cid, source="_upsert_creator_quiet_watch",
        confidence=1.0,
    )
    return cid, True


def _upsert_responsiveness_belief(conn, entity, source_run, confirm):
    """Add, confirm, or softly challenge a belief about one entity's responsiveness.

    confirm=True is evidence the entity resolved a concern (supports or
    creates the belief). confirm=False is evidence of a new concern despite
    an existing belief (challenges it) — a single negative event never
    creates a belief on its own, since optional participation is not failure.
    Duplicate protection: at most one active responsiveness belief per entity.

    Reasons over an entity's resolved/new episode counts for the run, not
    one specific episode, so there is no single hearth_episodes.id to
    attach — source_run carries the scan-level provenance instead.
    """
    subject_id = str(entity)
    existing = hearth_worldview.get_active_beliefs(
        conn, subject_type="entity", subject_id=subject_id,
        belief_type="responsiveness", limit=1,
    )
    if existing:
        belief = existing[0]
        delta = _confidence_delta(conn, "creator_activity")
        new_confidence = belief["confidence"] + delta if confirm else belief["confidence"] - delta
        hearth_worldview.update_belief(
            conn, belief["id"], confidence=new_confidence,
            last_confirmed=confirm, last_challenged=not confirm,
        )
        return belief["id"], False

    if not confirm:
        return None, False  # don't manufacture a belief out of a single negative event

    bid = hearth_worldview.add_belief(
        conn, subject_type="entity", subject_id=subject_id, belief_type="responsiveness",
        belief_text=(
            f"{_entity_display_name(conn, entity)} has shown episodes resolving,"
            " suggesting responsiveness to outreach."
        ),
        confidence=_NEW_BELIEF_CONFIDENCE,
        source_run=source_run,
    )
    hearth_worldview.create_entity_ref(
        conn, entity_id=entity, reflection_type="worldview_belief",
        reflection_id=bid, source="_upsert_responsiveness_belief",
        confidence=1.0,
    )
    return bid, True


# ---------------------------------------------------------------------------
# Engagement momentum belief
#
# Hearth's second belief type. Emerges from a pattern of diverse
# organizational activity over time — not from episode resolution, but from
# Pulse-classified hearth_events in the Pathway DB. Private DM event types
# (message_sent, private_messages) are never included; this exclusion is
# permanent per HEARTH_SENSORY_POLICY.md Category B. This module's query
# below already applies that exclusion as an explicit allowlist (event_type
# IN (...), not a denylist) — the proven pattern hearth_event_types.py's
# shared SAFE_HEARTH_EVENT_TYPES now also applies to Pulse and the
# Experience Evaluator. This set is intentionally NOT the same constant:
# it's a narrower business-logic subset (which activity types count toward
# a "diverse engagement" momentum belief — notably excluding
# user_signed_in), not itself a security boundary, so it stays independent.
# ---------------------------------------------------------------------------

_MOMENTUM_ELIGIBLE_EVENT_TYPES = frozenset({
    "training_viewed",
    "checkin_submitted",
    "battle_requested",
    "event_signup_created",
    "community_message_created",
    "onboarding_step_completed",
})

_MOMENTUM_TYPE_LABELS = {
    "training_viewed": "training",
    "checkin_submitted": "check-ins",
    "battle_requested": "battles",
    "event_signup_created": "events",
    "community_message_created": "community",
    "onboarding_step_completed": "onboarding",
}

_MOMENTUM_WINDOW_DAYS = 14       # rolling activity window
_MOMENTUM_STALE_DAYS = 21        # no activity after this → begin decay
_MOMENTUM_DECAY_PER_CYCLE = 0.08 # confidence lost per Soul cycle while stale
_MOMENTUM_ARCHIVE_THRESHOLD = 0.10  # archive once confidence drops below this
_MOMENTUM_CONFIDENCE_CAP = 0.85  # never exceed this
_MOMENTUM_MIN_DISTINCT_TYPES = 4 # threshold to form or sustain a belief


def _momentum_confidence(distinct_count):
    """Map distinct eligible activity type count to belief confidence.

    4→0.18, 6→0.33, 8→0.48, 10+→0.65 growing to 0.85 cap.
    """
    if distinct_count < _MOMENTUM_MIN_DISTINCT_TYPES:
        return 0.0
    if distinct_count >= 10:
        return min(_MOMENTUM_CONFIDENCE_CAP, 0.65 + (distinct_count - 10) * 0.05)
    return min(_MOMENTUM_CONFIDENCE_CAP, 0.18 + (distinct_count - 4) * 0.075)


def _collect_momentum_activity(window_days=_MOMENTUM_WINDOW_DAYS):
    """Read signal-level hearth_events per user for the last window_days days.

    Opens its own read-only connection to the Pathway DB (DATABASE_URL).
    Returns {str(actor_user_id): {"types": set, "display_name": str}}.
    Returns {} silently if DATABASE_URL is absent or the query fails —
    a missing Pathway DB must never break Soul's reflection cycle.
    """
    db_url = os.environ.get("DATABASE_URL", "")
    if not db_url:
        return {}
    db_path = db_url[len("sqlite:///"):] if db_url.startswith("sqlite:///") else db_url

    window_start = (
        datetime.now(timezone.utc) - timedelta(days=window_days)
    ).isoformat()
    eligible = sorted(_MOMENTUM_ELIGIBLE_EVENT_TYPES)
    placeholders = ",".join("?" * len(eligible))

    try:
        path_conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        path_conn.row_factory = sqlite3.Row
        try:
            rows = path_conn.execute(
                f"SELECT he.actor_user_id, he.event_type,"
                f" COALESCE(NULLIF(u.tiktok_handle, ''), u.name) AS display_name"
                f" FROM hearth_events he"
                f" LEFT JOIN users u ON u.id = he.actor_user_id"
                f" WHERE he.event_type IN ({placeholders})"
                f"   AND he.experience_level IN ('signal', 'observation')"
                f"   AND he.occurred_at >= ?"
                f"   AND he.actor_user_id IS NOT NULL;",
                tuple(eligible) + (window_start,),
            ).fetchall()
        finally:
            path_conn.close()
    except Exception as exc:
        print(
            f"[hearth_soul] momentum: pathway query unavailable this run"
            f" ({type(exc).__name__}: {exc})"
        )
        return {}

    result = {}
    for row in rows:
        uid = str(row["actor_user_id"])
        if uid not in result:
            result[uid] = {
                "types": set(),
                "display_name": row["display_name"] or f"User {uid}",
            }
        result[uid]["types"].add(row["event_type"])
    return result


def _upsert_momentum_belief(conn, entity_id, distinct_count, activity_types,
                            display_name, source_run):
    """Add or confirm an engagement_momentum belief for one entity.

    On confirm: updates belief_text and confidence (never lowers), stamps
    last_confirmed_at. On creation: inserts with calculated confidence.
    Returns (belief_id, created: bool).

    Momentum is computed from hearth_events (Pathway DB), not hearth_episodes
    — there is never a hearth_episodes.id here, so source_episode_id must
    stay None. distinct_count/activity_types are themselves aggregated over
    many events in the rolling window, so there is no single event id to use
    as source_signal_id either; source_run carries the scan-level provenance.
    """
    subject_id = str(entity_id)
    existing = hearth_worldview.get_active_beliefs(
        conn, subject_type="entity", subject_id=subject_id,
        belief_type="engagement_momentum", limit=1,
    )

    confidence = _momentum_confidence(distinct_count)
    labels = sorted(
        _MOMENTUM_TYPE_LABELS.get(t, t.replace("_", " ")) for t in activity_types
    )
    belief_text = (
        f"{display_name} has shown engagement momentum across {distinct_count} distinct"
        f" activity types in the last {_MOMENTUM_WINDOW_DAYS} days"
        f" ({', '.join(labels)})."
    )

    if existing:
        belief = existing[0]
        new_conf = max(belief["confidence"], confidence)
        hearth_worldview.update_belief(
            conn, belief["id"],
            confidence=new_conf,
            belief_text=belief_text,
            last_confirmed=True,
        )
        return belief["id"], False

    bid = hearth_worldview.add_belief(
        conn, subject_type="entity", subject_id=subject_id,
        belief_type="engagement_momentum",
        belief_text=belief_text,
        confidence=confidence,
        source_run=source_run,
    )
    return bid, True


def _decay_stale_momentum_beliefs(conn, active_entity_ids):
    """Decay confidence on engagement_momentum beliefs inactive for _MOMENTUM_STALE_DAYS.

    active_entity_ids: set of entity_id strings confirmed this cycle (skipped).
    Uses last_confirmed_at if set, else created_at, as the staleness timestamp.
    Beliefs that fall below _MOMENTUM_ARCHIVE_THRESHOLD are archived rather
    than deleted — momentum fades gradually, never flips in one cycle.
    """
    stale_cutoff = (
        datetime.now(timezone.utc) - timedelta(days=_MOMENTUM_STALE_DAYS)
    ).isoformat()

    all_momentum = hearth_worldview.get_active_beliefs(
        conn, subject_type="entity", belief_type="engagement_momentum",
    )

    for belief in all_momentum:
        if belief["subject_id"] in active_entity_ids:
            continue
        last_active = belief["last_confirmed_at"] or belief["created_at"] or ""
        if last_active >= stale_cutoff:
            continue  # still within the grace window; not yet stale

        new_conf = max(0.0, belief["confidence"] - _MOMENTUM_DECAY_PER_CYCLE)
        if new_conf < _MOMENTUM_ARCHIVE_THRESHOLD:
            hearth_worldview.update_belief(
                conn, belief["id"], confidence=new_conf,
                status="archived", last_challenged=True,
            )
        else:
            hearth_worldview.update_belief(
                conn, belief["id"], confidence=new_conf, last_challenged=True,
            )


def reflect_on_worldview(conn, new_episodes=None, resolved_episodes=None, source_run=None,
                         snapshot_limit=25):
    """
    Soul's worldview reflection pass — the only code path that writes to
    hearth_worldview_* tables, and only ever through hearth_worldview.py.

    Reads a limited worldview snapshot (step 1), then reuses the same
    new_episodes/resolved_episodes already flowing into generate_reflection
    (step 2) to conservatively add or refresh beliefs, uncertainties, watched
    changes, and provisional recent lessons (steps 3-5). Duplicate protection
    keeps each (subject, type) to at most one active/open row per category,
    refreshed in place rather than re-inserted on repeat runs.

    NOTE — new_episodes naming: despite the parameter name, callers pass all
    currently *open* episodes (not only newly-created ones). The name is a
    historical artifact; treat it as "open_episodes_for_reflection".

    Returns a dict: {"worldview_before": <snapshot>, "beliefs": [...],
    "uncertainties": [...], "changes": [...], "lessons": [...]}, where each
    list holds (row_id, created) tuples for whatever Soul wrote this run.
    """
    hearth_worldview.ensure_worldview_tables(conn)

    worldview_before = hearth_worldview.get_worldview_snapshot(
        conn,
        belief_limit=snapshot_limit,
        relationship_limit=snapshot_limit,
        uncertainty_limit=snapshot_limit,
        change_limit=snapshot_limit,
        lesson_limit=snapshot_limit,
    )

    written = {"worldview_before": worldview_before, "beliefs": [], "uncertainties": [],
               "changes": [], "lessons": []}

    entity_counts, type_counts = _group_episode_counts(new_episodes)
    resolved_entity_counts, _ = _group_episode_counts(resolved_episodes)

    for entity, count in entity_counts.items():
        if count >= _REPEAT_CONCERN_THRESHOLD:
            result = _upsert_entity_repeat_uncertainty(conn, entity, count, source_run)
            written["uncertainties"].append(result)

    for episode_type, count in type_counts.items():
        if count >= _TYPE_SPIKE_THRESHOLD:
            cid, created, lesson_result = _upsert_episode_type_change(
                conn, episode_type, count, source_run
            )
            written["changes"].append((cid, created))
            if lesson_result is not None:
                written["lessons"].append(lesson_result)

    # Individual significance: some episode types are worth a cautious entry
    # even as a single occurrence, independent of the repeat/spike thresholds
    # above. Never creates a belief — see _upsert_single_episode_uncertainty
    # and _upsert_creator_quiet_watch.
    for ep in new_episodes or []:
        entity = _episode_entity(ep)
        ep_type = _episode_type(ep)
        if entity is None or ep_type is None:
            continue
        if not _is_individually_significant(ep, ep_type):
            continue
        episode_id = _episode_id(ep)
        if ep_type == "creator_quiet":
            written["changes"].append(
                _upsert_creator_quiet_watch(conn, entity, source_run, episode_id=episode_id)
            )
        else:
            written["uncertainties"].append(
                _upsert_single_episode_uncertainty(
                    conn, ep_type, entity, source_run, episode_id=episode_id
                )
            )

    for entity in resolved_entity_counts:
        result = _upsert_responsiveness_belief(conn, entity, source_run, confirm=True)
        if result[0] is not None:
            written["beliefs"].append(result)

    # An entity with an existing responsiveness belief that still shows a new
    # concern this run softly challenges that belief. One event alone is
    # never enough to create a belief (handled inside the helper).
    for entity in entity_counts:
        if entity in resolved_entity_counts:
            continue
        result = _upsert_responsiveness_belief(conn, entity, source_run, confirm=False)
        if result[0] is not None:
            written["beliefs"].append(result)

    # Engagement momentum belief — reads Pathway hearth_events directly.
    # Runs after responsiveness logic; isolated in a try/except so a Pathway
    # DB outage never breaks the rest of the reflection cycle.
    try:
        momentum_activity = _collect_momentum_activity()
        active_momentum_ids = set()
        for entity_id, activity_data in momentum_activity.items():
            distinct_types = activity_data["types"]
            if len(distinct_types) < _MOMENTUM_MIN_DISTINCT_TYPES:
                continue
            result = _upsert_momentum_belief(
                conn, entity_id, len(distinct_types), distinct_types,
                activity_data["display_name"], source_run,
            )
            active_momentum_ids.add(entity_id)
            if result[0] is not None:
                written["beliefs"].append(result)
        _decay_stale_momentum_beliefs(conn, active_momentum_ids)
    except Exception as exc:
        print(
            f"[HEARTH WORLDVIEW ERROR] momentum source_run={source_run!r} "
            f"{type(exc).__name__}: {exc}"
        )
        traceback.print_exc()

    return written


# ---------------------------------------------------------------------------
# Question auto-resolution
#
# Neither question type has reliable per-episode provenance: entity_episode
# uncertainties only ever store the FIRST triggering episode's id (never
# updated on refresh), and entity (repeat-volume) uncertainties have no
# per-episode link at all. So resolution never looks at historical
# provenance — it recomputes the current qualifying condition fresh, using
# the exact same source query (hearth_memory.get_open_episodes) and counting
# logic (_group_episode_counts) the trigger path above uses, and closes the
# question only if that condition no longer holds right now.
# ---------------------------------------------------------------------------

QUESTION_AUTO_RESOLVE_REASON = "condition_cleared"


def _parse_entity_episode_subject(subject_id):
    """Parse an entity_episode subject_id ("<episode_type>:<entity_id>") into
    (episode_type, entity_id). Returns (None, None) if subject_id doesn't
    match that shape — callers must treat that as "cannot recompute", not as
    entity_id=None being a valid value.
    """
    if not subject_id or ":" not in subject_id:
        return None, None
    episode_type, _, entity_id_str = subject_id.partition(":")
    if not episode_type or not entity_id_str:
        return None, None
    try:
        entity_id = int(entity_id_str)
    except (TypeError, ValueError):
        return None, None
    return episode_type, entity_id


def _entity_open_episode_count(conn, entity_id):
    """Current count of open episodes for entity_id, across all types — the
    same source query (hearth_memory.get_open_episodes) and grouping
    (_group_episode_counts) _upsert_entity_repeat_uncertainty's trigger path
    counts against, so resolution can never drift from the trigger condition.
    """
    open_for_entity = hearth_memory.get_open_episodes(conn, entity_id=entity_id)
    entity_counts, _ = _group_episode_counts(open_for_entity)
    return entity_counts.get(entity_id, 0)


def _entity_episode_type_open_count(conn, entity_id, episode_type):
    """Current count of open episodes of episode_type for entity_id — same
    source query and grouping as above, just reading the type side of the
    same _group_episode_counts() call instead of the entity side.
    """
    open_for_entity = hearth_memory.get_open_episodes(conn, entity_id=entity_id)
    _, type_counts = _group_episode_counts(open_for_entity)
    return type_counts.get(episode_type, 0)


def question_condition_cleared(conn, subject_type, subject_id):
    """Whether the condition a worldview-sourced question describes has
    stopped holding, recomputed fresh from current open-episode data.

    Returns True (condition cleared, question should resolve), False
    (condition still holds, question should stay open), or None if
    subject_type/subject_id can't be interpreted — callers must leave those
    rows untouched and log them for manual review rather than guessing.
    """
    if subject_type == "entity_episode":
        episode_type, entity_id = _parse_entity_episode_subject(subject_id)
        if entity_id is None:
            return None
        return _entity_episode_type_open_count(conn, entity_id, episode_type) == 0

    if subject_type == "entity":
        try:
            entity_id = int(subject_id)
        except (TypeError, ValueError):
            return None
        return _entity_open_episode_count(conn, entity_id) < _REPEAT_CONCERN_THRESHOLD

    return None


def resolve_cleared_worldview_questions(conn, dry_run=False):
    """Standing auto-resolution pass over the open worldview-question backlog.

    For every open question sourced from a worldview uncertainty (see
    hearth_questions.list_open_worldview_questions), recomputes whether its
    underlying condition still holds via question_condition_cleared() and
    closes it (hearth_questions.auto_resolve_question(),
    resolution_reason=QUESTION_AUTO_RESOLVE_REASON) if not. dry_run=True
    recomputes and reports without mutating anything — used by the one-time
    backlog cleanup utility (hearth_question_resolution_cleanup.py) so the
    exact same decision logic drives both the standing per-scan pass and the
    cleanup script; there is no second, parallel implementation to drift.

    Returns a list of dicts, one per question considered:
        {"question_id", "subject_type", "subject_id", "action", "reason"}
    where action is one of:
        "resolve" — condition cleared (closed unless dry_run)
        "keep"    — condition still holds, left open
        "skip"    — unrecognized/unparseable subject, or a dangling link to
                    a worldview uncertainty row that no longer exists; never
                    touched
    """
    results = []
    for question in hearth_questions.list_open_worldview_questions(conn):
        uncertainty = hearth_worldview.get_uncertainty(conn, question["worldview_uncertainty_id"])
        if uncertainty is None:
            results.append({
                "question_id": question["question_id"], "subject_type": None, "subject_id": None,
                "action": "skip",
                "reason": "linked worldview uncertainty not found (dangling reference)",
            })
            continue

        subject_type = uncertainty["subject_type"]
        subject_id = uncertainty["subject_id"]
        cleared = question_condition_cleared(conn, subject_type, subject_id)

        if cleared is None:
            results.append({
                "question_id": question["question_id"], "subject_type": subject_type,
                "subject_id": subject_id, "action": "skip",
                "reason": f"unrecognized/unparseable subject_type={subject_type!r} subject_id={subject_id!r}",
            })
        elif cleared:
            if not dry_run:
                hearth_questions.auto_resolve_question(
                    conn, question["question_id"], resolution_reason=QUESTION_AUTO_RESOLVE_REASON,
                )
            results.append({
                "question_id": question["question_id"], "subject_type": subject_type,
                "subject_id": subject_id, "action": "resolve",
                "reason": "underlying condition no longer holds",
            })
        else:
            results.append({
                "question_id": question["question_id"], "subject_type": subject_type,
                "subject_id": subject_id, "action": "keep",
                "reason": "underlying condition still holds",
            })

    return results


# ---------------------------------------------------------------------------
# Reflection generator
# ---------------------------------------------------------------------------

def generate_reflection(conn, new_episodes=None, resolved_episodes=None,
                        open_concerns=None, open_questions=None, source_run=None,
                        auto_question=True, update_worldview=True):
    """
    Derive a reflection from pipeline run data and persist it.

    Episode parameters accept lists of dicts or sqlite3.Row objects.
    Concern and question parameters accept lists or integer counts.
    Returns the reflection_id of the created row.

    auto_question: if True (default) and what_i_should_ask is non-empty, a
    question is created via hearth_questions so it surfaces for human review.
    Pass False to store the reflection without touching hearth_questions.

    update_worldview: if True (default) and HEARTH_WORLDVIEW_ENABLED is also
    True, also runs reflect_on_worldview() on the same new_episodes/
    resolved_episodes so Soul's worldview reflection happens automatically on
    every call. A failure in that pass is logged and swallowed so it can
    never break reflection generation; pass False to disable it for this call
    only (e.g. for isolated testing), or set HEARTH_WORLDVIEW_ENABLED=0 to
    disable it everywhere without a deploy.
    """
    # --- what_changed ---
    parts = []
    if new_episodes:
        parts.append(f"{len(new_episodes)} new episode(s) recorded.")
    if resolved_episodes:
        parts.append(f"{len(resolved_episodes)} episode(s) resolved.")
    what_changed = " ".join(parts) if parts else "No episode changes detected."

    # --- what_surprised_me ---
    surprises = []
    if new_episodes:
        entity_counts = {}
        type_counts = {}
        for ep in new_episodes:
            e = _episode_entity(ep)
            if e is not None:
                entity_counts[e] = entity_counts.get(e, 0) + 1
            t = _episode_type(ep)
            if t is not None:
                type_counts[t] = type_counts.get(t, 0) + 1

        if any(c >= 2 for c in entity_counts.values()):
            surprises.append("Multiple new concerns detected under the same coach.")

        for ep_type, count in type_counts.items():
            if count >= 3:
                surprises.append(
                    f"Episode type '{ep_type}' appeared {count} times in this run."
                )
                break

    what_surprised_me = " ".join(surprises)

    # --- what_i_am_uncertain_about ---
    n_questions = _list_len(open_questions)
    n_concerns = _list_len(open_concerns)
    uncertainties = []
    if n_questions > 0:
        uncertainties.append(f"{n_questions} open question(s) remain unanswered.")
    if n_concerns > 5:
        uncertainties.append(
            "High concern volume may indicate systemic issue or data artifact."
        )
    what_i_am_uncertain_about = " ".join(uncertainties)

    # --- what_i_should_ask ---
    if "High concern volume" in what_i_am_uncertain_about:
        what_i_should_ask = (
            "Is the current concern volume expected or does it indicate a detection error?"
        )
    elif n_questions > 0:
        what_i_should_ask = (
            f"Are the {n_questions} open question(s) under active review?"
        )
    else:
        what_i_should_ask = ""

    # --- persist ---
    reflection_id = create_reflection(
        conn,
        what_changed=what_changed,
        what_surprised_me=what_surprised_me,
        what_i_am_uncertain_about=what_i_am_uncertain_about,
        what_i_should_ask=what_i_should_ask,
        source_run=source_run,
    )

    if auto_question and what_i_should_ask:
        hearth_questions.create_question(
            conn,
            question_text=what_i_should_ask,
            topic_tags="soul_reflection,auto_generated",
            triggered_by="hearth_soul",
        )

    if update_worldview and HEARTH_WORLDVIEW_ENABLED:
        try:
            reflect_on_worldview(
                conn, new_episodes=new_episodes, resolved_episodes=resolved_episodes,
                source_run=source_run,
            )
        except Exception as exc:
            print(
                f"[HEARTH WORLDVIEW ERROR] source_run={source_run!r} "
                f"{type(exc).__name__}: {exc}"
            )
            traceback.print_exc()

    return reflection_id


# ---------------------------------------------------------------------------
# Smoke test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    conn = get_reflections_connection()

    print("Step 1: ensure_reflections_table()")
    ensure_reflections_table(conn)
    hearth_questions.ensure_questions_table(conn)
    print("  Tables ready.")

    print("\nStep 2: generate_reflection() with sample data")
    new_eps = [
        {"episode_type": "creator_quiet", "entity": "userA"},
        {"episode_type": "creator_quiet", "entity": "userB"},
        {"episode_type": "creator_quiet", "entity": "userC"},
        {"episode_type": "training_comment_waiting", "entity": "userD"},
    ]
    resolved_eps = [
        {"episode_type": "checkin_feedback_waiting", "entity": "userE"},
    ]
    open_concerns = [{}] * 7
    open_questions_sample = [{}] * 2

    rid = generate_reflection(
        conn,
        new_episodes=new_eps,
        resolved_episodes=resolved_eps,
        open_concerns=open_concerns,
        open_questions=open_questions_sample,
        source_run="smoke_test",
    )
    print(f"  reflection_id={rid}")

    print("\nStep 3: get_latest_reflection()")
    row = get_latest_reflection(conn)
    if row:
        print(f"  reflection_id:             {row['reflection_id']}")
        print(f"  reflection_time:           {row['reflection_time']}")
        print(f"  what_changed:              {row['what_changed']}")
        print(f"  what_surprised_me:         {row['what_surprised_me']}")
        print(f"  what_i_am_uncertain_about: {row['what_i_am_uncertain_about']}")
        print(f"  what_i_should_ask:         {row['what_i_should_ask']}")
        print(f"  source_run:                {row['source_run']}")

    print("\nStep 4: summarize_recent_reflections()")
    summary = summarize_recent_reflections(conn, limit=10)
    print(summary if summary else "  (no summary)")

    print("\nStep 5: confirm Soul's automatic worldview write from Step 2")
    creator_quiet_change = hearth_worldview.get_watched_changes(
        conn, subject_type="episode_type", subject_id="creator_quiet", limit=1,
    )
    userE_belief = hearth_worldview.get_active_beliefs(
        conn, subject_type="entity", subject_id="userE", belief_type="responsiveness", limit=1,
    )
    assert creator_quiet_change, "Expected a watched change for episode_type='creator_quiet'"
    assert userE_belief, "Expected a responsiveness belief for entity='userE'"
    change_id_first = creator_quiet_change[0]["id"]
    belief_id_first = userE_belief[0]["id"]
    belief_confidence_first = userE_belief[0]["confidence"]
    print(f"  watched_change id={change_id_first} current_state={creator_quiet_change[0]['current_state']!r}")
    print(f"  belief id={belief_id_first} confidence={belief_confidence_first}")

    print("\nStep 6: generate_reflection() again with identical input — duplicate protection")
    rid2 = generate_reflection(
        conn,
        new_episodes=new_eps,
        resolved_episodes=resolved_eps,
        open_concerns=open_concerns,
        open_questions=open_questions_sample,
        source_run="smoke_test",
    )
    print(f"  reflection_id={rid2} (a new reflection log row is expected — each call logs one)")

    all_creator_quiet_changes = hearth_worldview.get_watched_changes(
        conn, subject_type="episode_type", subject_id="creator_quiet",
    )
    all_userE_beliefs = hearth_worldview.get_active_beliefs(
        conn, subject_type="entity", subject_id="userE", belief_type="responsiveness",
    )
    assert len(all_creator_quiet_changes) == 1, "Duplicate watched change rows created!"
    assert len(all_userE_beliefs) == 1, "Duplicate belief rows created!"
    assert all_creator_quiet_changes[0]["id"] == change_id_first, "change id should be stable across runs"
    assert all_userE_beliefs[0]["id"] == belief_id_first, "belief id should be stable across runs"
    assert all_userE_beliefs[0]["confidence"] > belief_confidence_first, "confirmed belief should gain confidence"
    print(f"  watched_change id unchanged: {all_creator_quiet_changes[0]['id'] == change_id_first}")
    print(f"  belief id unchanged: {all_userE_beliefs[0]['id'] == belief_id_first},"
          f" confidence {belief_confidence_first} -> {all_userE_beliefs[0]['confidence']}")

    recurrence_lessons = [
        row for row in hearth_worldview.get_recent_lessons(conn)
        if row["lesson_text"].startswith("Episode type 'creator_quiet' recurring")
    ]
    assert recurrence_lessons, "Expected a provisional recurrence lesson to appear on the second run"
    print(f"  recurrence lesson created: id={recurrence_lessons[0]['id']}"
          f" status={recurrence_lessons[0]['status']} confidence={recurrence_lessons[0]['confidence']}")

    print("\nStep 7: reflect_on_worldview() directly — entity repeat-concern uncertainty + dedup")
    repeat_eps = [
        {"episode_type": "missing_discord", "entity": "session_3_smoketest_creator_F"},
        {"episode_type": "probation", "entity": "session_3_smoketest_creator_F"},
    ]
    result_a = reflect_on_worldview(conn, new_episodes=repeat_eps, source_run="session_3_smoke_test")
    result_b = reflect_on_worldview(conn, new_episodes=repeat_eps, source_run="session_3_smoke_test")
    repeat_uncertainties = hearth_worldview.get_open_uncertainties(
        conn, subject_type="entity", subject_id="session_3_smoketest_creator_F",
    )
    assert len(repeat_uncertainties) == 1, "Duplicate uncertainty rows created!"
    assert result_a["uncertainties"] and result_a["uncertainties"][0][1] is True, \
        "Expected the first call to create a new uncertainty"
    assert result_b["uncertainties"] and result_b["uncertainties"][0][1] is False, \
        "Expected the second call to update, not create, the uncertainty"
    print(f"  uncertainty id={repeat_uncertainties[0]['id']}")
    print(f"  text: {repeat_uncertainties[0]['uncertainty_text']}")
    print(f"  first call created={result_a['uncertainties'][0][1]},"
          f" second call created={result_b['uncertainties'][0][1]}")

    print("\nAll Session 3 smoke test assertions passed.")

    print("\nStep 8: get_worldview_snapshot() and full review dump")
    snapshot = hearth_worldview.get_worldview_snapshot(conn)
    for key in ("identity", "active_beliefs", "active_relationships", "open_uncertainties",
                "watched_changes", "recent_lessons"):
        print(f"  {key}: {len(snapshot[key])} row(s)")

    print("\n=== Full worldview table contents (for human review before Session 4) ===")

    print("\n--- hearth_worldview_beliefs ---")
    belief_rows = conn.execute("SELECT * FROM hearth_worldview_beliefs ORDER BY id;").fetchall()
    for row in belief_rows:
        print(f"  id={row['id']} subject={row['subject_type']}:{row['subject_id']}"
              f" type={row['belief_type']} status={row['status']} confidence={row['confidence']}"
              f" source_episode_id={row['source_episode_id']} source_run={row['source_run']}")
        print(f"    belief_text: {row['belief_text']}")
    if not belief_rows:
        print("  (no rows)")

    print("\n--- hearth_worldview_uncertainties ---")
    uncertainty_rows = conn.execute("SELECT * FROM hearth_worldview_uncertainties ORDER BY id;").fetchall()
    for row in uncertainty_rows:
        print(f"  id={row['id']} subject={row['subject_type']}:{row['subject_id']}"
              f" status={row['status']} confidence={row['confidence']}"
              f" source_episode_id={row['source_episode_id']} source_run={row['source_run']}")
        print(f"    uncertainty_text: {row['uncertainty_text']}")
    if not uncertainty_rows:
        print("  (no rows)")

    print("\n--- hearth_worldview_changes ---")
    change_rows = conn.execute("SELECT * FROM hearth_worldview_changes ORDER BY id;").fetchall()
    for row in change_rows:
        print(f"  id={row['id']} subject={row['subject_type']}:{row['subject_id']}"
              f" status={row['status']} direction={row['direction']} confidence={row['confidence']}"
              f" source_episode_id={row['source_episode_id']} source_run={row['source_run']}")
        print(f"    change_text: {row['change_text']}")
        print(f"    previous_state={row['previous_state']!r} current_state={row['current_state']!r}")
    if not change_rows:
        print("  (no rows)")

    print("\n--- hearth_worldview_recent_lessons ---")
    lesson_rows = conn.execute("SELECT * FROM hearth_worldview_recent_lessons ORDER BY id;").fetchall()
    for row in lesson_rows:
        print(f"  id={row['id']} status={row['status']} confidence={row['confidence']}"
              f" times_confirmed={row['times_confirmed']} times_challenged={row['times_challenged']}"
              f" candidate_for_principle={row['candidate_for_principle']}"
              f" source_episode_id={row['source_episode_id']} source_run={row['source_run']}")
        print(f"    lesson_text: {row['lesson_text']}")
    if not lesson_rows:
        print("  (no rows)")

    print("\n--- hearth_worldview_relationships ---")
    relationship_rows = conn.execute("SELECT * FROM hearth_worldview_relationships ORDER BY id;").fetchall()
    for row in relationship_rows:
        print(f"  id={row['id']} status={row['status']} confidence={row['confidence']}")
        print(f"    relationship_summary: {row['relationship_summary']}")
    if not relationship_rows:
        print("  (no rows — this session does not write relationships)")

    print("\n--- hearth_worldview_identity ---")
    identity_rows = conn.execute("SELECT * FROM hearth_worldview_identity ORDER BY id;").fetchall()
    for row in identity_rows:
        print(f"  id={row['id']} key={row['identity_key']} value={row['identity_value']!r} status={row['status']}")
    if not identity_rows:
        print("  (no rows — this session does not write identity)")

    print(
        "\nNOTE: worldview rows above were written by Soul during this smoke test and"
        " intentionally left in hearth_memory.db for review before Session 4, rather"
        " than being deleted like Session 2's cleanup."
    )

    conn.close()
    print("\nSmoke test complete.")
