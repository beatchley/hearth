"""
Hearth Context Builder — assembles Hearth's awareness before language generation.

This module is the boundary between raw Pathway data and Hearth's perspective.
It translates database rows and memory episodes into structured observations that
describe what Hearth has noticed, in Hearth's own terms.

The language model receives this context — not raw database rows, table names,
column definitions, or query output.

Since Session 4, context begins with Hearth's current worldview understanding
(read via hearth_worldview.py) and supplements it with the existing supporting
evidence below — raw events, episodes, and memory remain in place unchanged.

Pipeline:
    Pathway Data  →  Hearth Memory  →  HearthAwarenessContext  →  LLM  →  Hearth Message
                      Worldview (Soul's interpretation)        ↗
"""

import os
from dataclasses import dataclass, field
from datetime import datetime, date, timezone
from typing import Optional

# Feature flag for reading worldview into context. Defaults on; set the
# HEARTH_WORLDVIEW_CONTEXT_ENABLED env var to "0" (e.g. via Render) to make
# context assembly behave exactly as it did before Session 4.
HEARTH_WORLDVIEW_CONTEXT_ENABLED = (
    os.environ.get("HEARTH_WORLDVIEW_CONTEXT_ENABLED", "1") == "1"
)

# Per-category cap when reading worldview into context — within the 10-20
# range recommended for briefing-sized output.
_WORLDVIEW_CONTEXT_LIMIT = 15


# ---------------------------------------------------------------------------
# Context types
# ---------------------------------------------------------------------------

@dataclass
class Observation:
    """Something Hearth has noticed today that is informational, not a tracked issue."""
    text: str
    person: Optional[str] = None


@dataclass
class OpenConcern:
    """An issue Hearth has been tracking in memory, new or recurring."""
    description: str
    first_seen: str        # ISO date string
    age_days: int
    severity: str          # low, medium, high
    episode_type: str
    is_recurring: bool     # True if first seen before today


@dataclass
class RecentResolution:
    """An issue Hearth had been tracking that is now resolved — progress worth noting."""
    description: str
    episode_type: str
    person_name: Optional[str]   # display_name of the linked person, if any
    resolved_at: str             # ISO timestamp
    days_open: int               # How long it was open before resolving


@dataclass
class PersonContext:
    """Hearth's accumulated knowledge about one person.

    Pathway owns the source-of-truth fields (name, email, role, status).
    Hearth owns what it has noticed over time: episodes, patterns, concerns.
    display_name is the only Pathway field cached here, purely for rendering.
    """
    user_id: int
    display_name: str
    open_concerns: list          # List[OpenConcern] for this person
    total_episode_count: int     # All episodes including resolved — depth of history
    hearth_summary: Optional[str] = None       # From entity.summary
    patterns_noticed: Optional[str] = None     # From entity.patterns_noticed (recurring patterns)
    coach_name: Optional[str] = None           # Display name of legacy assigned coach if known
    cn_coach_name: Optional[str] = None       # Display name of CN-program coach if known
    shop_coach_name: Optional[str] = None     # Display name of Shop-program coach if known
    recruiter_name: Optional[str] = None      # Display name of recruiter if known

    @property
    def has_multiple_issues(self):
        return len(self.open_concerns) > 1

    @property
    def has_history(self):
        """True if Hearth has seen this person in resolved episodes, not just open ones."""
        return self.total_episode_count > len(self.open_concerns)


@dataclass
class WorldviewSummary:
    """Hearth's current worldview understanding, read fresh at each context assembly.

    Populated from hearth_worldview.py's limited snapshot (Session 3). Empty by
    default — render_for_llm treats all-empty lists as nothing to add, so a
    briefing with no worldview data behaves exactly as it did before Session 4.
    Each list holds sqlite3.Row entries from the corresponding worldview table.
    """
    active_beliefs: list = field(default_factory=list)
    active_relationships: list = field(default_factory=list)
    open_uncertainties: list = field(default_factory=list)
    watched_changes: list = field(default_factory=list)
    recent_lessons: list = field(default_factory=list)

    @property
    def is_empty(self):
        return not (
            self.active_beliefs or self.active_relationships or self.open_uncertainties
            or self.watched_changes or self.recent_lessons
        )


@dataclass
class HearthAwarenessContext:
    """Hearth's complete awareness for a briefing moment."""
    date: str
    observations: list = field(default_factory=list)          # List[Observation]
    person_contexts: list = field(default_factory=list)       # List[PersonContext]
    unattached_concerns: list = field(default_factory=list)   # List[OpenConcern] — no linked person
    recent_resolutions: list = field(default_factory=list)    # List[RecentResolution]
    relevant_principles: list = field(default_factory=list)   # List[dict] from hearth_principles
    worldview: WorldviewSummary = field(default_factory=WorldviewSummary)  # Session 4

    @property
    def is_quiet(self):
        return (
            not self.observations
            and not self.person_contexts
            and not self.unattached_concerns
            and not self.recent_resolutions
        )


# ---------------------------------------------------------------------------
# Principles
# ---------------------------------------------------------------------------

_EPISODE_TAG_MAP = {
    "creator_quiet":            "creator_activity",
    "checkin_not_submitted":    "creator_activity",
    "new_creator_stuck":        "creator_activity",
    "checkin_feedback_waiting": "coaching",
    "training_comment_waiting": "engagement",
    "support_request_waiting":  "support",
    "probation":                "coaching",
    "missing_discord":          "onboarding",
    "training_no_engagement":   "engagement",
}


def collect_relevant_principles(memory_conn, active_episode_types, tracer=None):
    """Return principle dicts relevant to the current episode types, deduplicated.

    Queries hearth_principles by the domain tags that correspond to each active
    episode type. Marks each selected principle as used. Prints trace lines to
    stdout unconditionally — principles are developer-visible audit context.
    """
    import hearth_principles as _hp

    tags = set()
    for ep_type in active_episode_types:
        tag = _EPISODE_TAG_MAP.get(ep_type)
        if tag:
            tags.add(tag)

    if not tags:
        print("[HEARTH PRINCIPLES] No matching tags for current episode types — skipping.")
        return []

    seen_ids = set()
    principles = []
    for tag in sorted(tags):
        for row in _hp.get_principles_by_tag(memory_conn, tag):
            pid = row["principle_id"]
            if pid not in seen_ids:
                seen_ids.add(pid)
                principles.append(row)

    for p in principles:
        _hp.mark_principle_used(memory_conn, p["principle_id"])

    tags_str = ", ".join(sorted(tags))
    print(
        f"[HEARTH PRINCIPLES] Tags matched: {tags_str}"
        f" | Selected: {len(principles)} principle(s)"
    )
    for i, p in enumerate(principles, 1):
        snippet = p["content"][:80] + ("..." if len(p["content"]) > 80 else "")
        print(f"[HEARTH PRINCIPLES] #{i} (conf {p['confidence']:.2f}): {snippet}")

    return principles


# ---------------------------------------------------------------------------
# Worldview
# ---------------------------------------------------------------------------

def _resolve_worldview_subject_name(memory_conn, pathway_conn, subject_type, subject_id):
    """Best-effort resolution of one worldview subject to a human display name.

    subject_id is Hearth's own hearth_entities.id when subject_type == "entity"
    (not a Pathway users.id directly — see hearth_memory.get_or_create_entity),
    so resolution is a two-step hop: hearth_entities.id -> user_id -> identity.
    Returns None for any other subject_type, a missing/unresolvable id, or any
    lookup failure — callers must treat None as "render the raw row as before."
    """
    if subject_type != "entity" or not subject_id:
        return None
    try:
        entity_id = int(subject_id)
        entity_row = memory_conn.execute(
            "SELECT user_id FROM hearth_entities WHERE id = ?;", (entity_id,)
        ).fetchone()
        if not entity_row or entity_row["user_id"] is None:
            return None
        import hearth_identity
        return hearth_identity.get_user_display_name(entity_row["user_id"], conn=pathway_conn)
    except Exception:
        return None


def _with_subject_name(memory_conn, pathway_conn, row):
    """Return row (as a dict) with a resolved "_subject_name" key added."""
    row = dict(row)
    row["_subject_name"] = _resolve_worldview_subject_name(
        memory_conn, pathway_conn, row.get("subject_type"), row.get("subject_id")
    )
    return row


def _with_relationship_names(memory_conn, pathway_conn, row):
    """Return row (as a dict) with resolved "_entity_a_name"/"_entity_b_name" keys added."""
    row = dict(row)
    row["_entity_a_name"] = _resolve_worldview_subject_name(
        memory_conn, pathway_conn, row.get("entity_a_type"), row.get("entity_a_id")
    )
    row["_entity_b_name"] = _resolve_worldview_subject_name(
        memory_conn, pathway_conn, row.get("entity_b_type"), row.get("entity_b_id")
    )
    return row


def collect_worldview_summary(memory_conn) -> WorldviewSummary:
    """Read a limited worldview snapshot for context assembly (Session 4).

    Returns an empty WorldviewSummary — never raises — if disabled via
    HEARTH_WORLDVIEW_CONTEXT_ENABLED, if memory_conn is None, or if worldview
    retrieval fails for any reason. A worldview problem must never break
    briefing; legacy context generation always continues.

    Since the identity-resolution pass below, each row in active_beliefs,
    active_relationships, open_uncertainties, and watched_changes is a dict
    (not a sqlite3.Row) carrying an extra "_subject_name" (or
    "_entity_a_name"/"_entity_b_name") key for human-facing rendering. The
    underlying worldview tables and IDs are untouched — this is render-time
    enrichment only. recent_lessons has no subject id and is left as-is.
    """
    if not HEARTH_WORLDVIEW_CONTEXT_ENABLED or memory_conn is None:
        return WorldviewSummary()

    try:
        import hearth_worldview as _wv

        snapshot = _wv.get_worldview_snapshot(
            memory_conn,
            belief_limit=_WORLDVIEW_CONTEXT_LIMIT,
            relationship_limit=_WORLDVIEW_CONTEXT_LIMIT,
            uncertainty_limit=_WORLDVIEW_CONTEXT_LIMIT,
            change_limit=_WORLDVIEW_CONTEXT_LIMIT,
            lesson_limit=_WORLDVIEW_CONTEXT_LIMIT,
        )

        import hearth_identity
        pathway_conn = hearth_identity.get_pathway_connection()
        try:
            summary = WorldviewSummary(
                active_beliefs=[
                    _with_subject_name(memory_conn, pathway_conn, r)
                    for r in snapshot["active_beliefs"]
                ],
                active_relationships=[
                    _with_relationship_names(memory_conn, pathway_conn, r)
                    for r in snapshot["active_relationships"]
                ],
                open_uncertainties=[
                    _with_subject_name(memory_conn, pathway_conn, r)
                    for r in snapshot["open_uncertainties"]
                    if not _is_never_brief_worldview_row(r)
                ],
                watched_changes=[
                    _with_subject_name(memory_conn, pathway_conn, r)
                    for r in snapshot["watched_changes"]
                    if not _is_never_brief_worldview_row(r)
                ],
                recent_lessons=list(snapshot["recent_lessons"]),
            )
        finally:
            if pathway_conn:
                pathway_conn.close()
        if not summary.is_empty:
            print(
                "[HEARTH WORLDVIEW] context read:"
                f" beliefs={len(summary.active_beliefs)}"
                f" relationships={len(summary.active_relationships)}"
                f" uncertainties={len(summary.open_uncertainties)}"
                f" changes={len(summary.watched_changes)}"
                f" lessons={len(summary.recent_lessons)}"
            )
        return summary
    except Exception as exc:
        print(f"[HEARTH WORLDVIEW] context read skipped — falling back to legacy context: {exc}")
        return WorldviewSummary()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}

_PATTERN_COOLDOWN_DAYS = 3

# Daily Brief 2.0 (Attention layer): explicit episode_type overrides checked
# before briefing_category. missing_discord is a deliberate reversal of its
# 'awareness' categorization — always briefed while unresolved, not a
# restoration of prior behavior (that categorization was intentional when
# added). The _NEVER_BRIEF set fully excludes these from brief output even
# though their briefing_category ('pattern' or 'action_needed') would
# otherwise brief them on a cooldown or always — episodes keep being written
# and stay queryable in Hearth City/admin, only brief visibility changes.
_ALWAYS_BRIEF_EPISODE_TYPES = {"missing_discord"}
_NEVER_BRIEF_EPISODE_TYPES = {
    "creator_quiet",
    "new_creator_stuck",
    "probation",
    "training_comment_needs_response",  # legacy keyword-based detector; use training_comment_waiting instead
}


def _infer_worldview_episode_type(subject_type, subject_id):
    """Best-effort recovery of the episode_type a worldview row was derived from.

    hearth_worldview_changes/uncertainties have no type/category column — Soul
    (hearth_soul.py) encodes provenance into subject_type/subject_id by
    convention instead. Three conventions are known today:
      - subject_type="episode_type"   -> subject_id IS the episode_type
        (_upsert_episode_type_change: any spiking episode_type)
      - subject_type="entity_episode" -> subject_id is "<episode_type>:<entity>"
        (_upsert_single_episode_uncertainty: individually-significant episodes)
      - subject_type="<episode_type>_entity" -> strip the "_entity" suffix
        (_upsert_creator_quiet_watch: subject_type="creator_quiet_entity")
    Returns None if subject_type/subject_id don't match any known convention —
    callers must treat None as "not derived from an episode_type, render as-is."
    """
    if not subject_type:
        return None
    if subject_type == "episode_type":
        return subject_id or None
    if subject_type == "entity_episode" and subject_id and ":" in subject_id:
        return subject_id.split(":", 1)[0] or None
    if subject_type.endswith("_entity"):
        return subject_type[: -len("_entity")] or None
    return None


def _is_never_brief_worldview_row(row) -> bool:
    """True if this worldview row traces back to a _NEVER_BRIEF_EPISODE_TYPES episode.

    Extends the Daily Brief 2.0 Attention-layer suppression principle to
    Worldview watched-changes/uncertainties, which otherwise render
    unconditionally regardless of episode-level suppression. This is a
    blocklist check, not a safelist: a row with no recognized subject_type/
    subject_id convention (see _infer_worldview_episode_type) is treated as
    "not derived from a suppressed episode" and still renders. Any future
    Soul-side convention for a suppressed episode type that isn't recognized
    here will leak the same way this bug did — see hearth_context.py module
    notes / the Daily Brief worldview leak fix for this residual risk.
    """
    row = dict(row)
    inferred = _infer_worldview_episode_type(row.get("subject_type"), row.get("subject_id"))
    return inferred in _NEVER_BRIEF_EPISODE_TYPES


def _should_brief(ep, now_utc) -> bool:
    """Return True if this episode should appear in today's briefing.

    _ALWAYS_BRIEF_EPISODE_TYPES / _NEVER_BRIEF_EPISODE_TYPES override
    briefing_category entirely for those specific episode types.

    Otherwise, by briefing_category:
    awareness  → always False (tracked in memory, never surfaced in briefings)
    pattern    → True only if never briefed or last briefed 3+ days ago
    action_needed / critical → always True
    NULL / unrecognized      → always True (never silently drop unclassified)
    """
    ep_type = ep["episode_type"]
    if ep_type in _ALWAYS_BRIEF_EPISODE_TYPES:
        return True
    if ep_type in _NEVER_BRIEF_EPISODE_TYPES:
        return False

    cat = ep["briefing_category"]
    if cat == "awareness":
        return False
    if cat == "pattern":
        last = ep["last_briefed_at"]
        if last is None:
            return True
        try:
            last_dt = datetime.fromisoformat(last)
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            return True
        return (now_utc - last_dt).days >= _PATTERN_COOLDOWN_DAYS
    if cat in ("action_needed", "critical"):
        return True
    return True


def _make_concern(ep, today: date, today_str: str) -> OpenConcern:
    first_seen_str = ep["observed_at"][:10]
    try:
        age_days = (today - date.fromisoformat(first_seen_str)).days
    except (ValueError, TypeError):
        age_days = 0
    return OpenConcern(
        description=ep["description"],
        first_seen=first_seen_str,
        age_days=age_days,
        severity=ep["severity"],
        episode_type=ep["episode_type"],
        is_recurring=(first_seen_str < today_str),
    )


def _sort_concerns(concerns: list) -> list:
    return sorted(
        concerns,
        key=lambda c: (_SEVERITY_ORDER.get(c.severity, 1), -c.age_days),
    )


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------

def build_context(data: dict, open_episodes: list, memory_conn=None,
                  tracer=None) -> HearthAwarenessContext:
    """
    Transform Pathway operational data and Hearth memory episodes into
    a HearthAwarenessContext — Hearth's assembled awareness.

    Separation of concerns:
    - observations: informational items from today (new joins, scheduled battles,
      recent comments) that are not tracked as persistent episodes.
    - person_contexts: one PersonContext per person with open episodes, grouping
      all their concerns together so Hearth can speak about the whole person.
    - unattached_concerns: open episodes not tied to a specific person.

    memory_conn is optional. If provided, each PersonContext is enriched with
    the person's total historical episode count and any Hearth summary notes.
    Without it, the context is still person-aware — just less historically deep.
    """
    import hearth_identity     # local imports to avoid circular dependency
    import hearth_memory
    import hearth_relationships
    import hearth_trace as _ht

    if tracer is None:
        tracer = _ht.NULL_TRACER

    now_utc = datetime.now(timezone.utc)
    today = now_utc.date()
    today_str = today.isoformat()
    observations = []

    # New users joining is informational, not a "Needs Action Today" item —
    # Daily Brief 2.0 excludes it from brief output (data.get(...) itself is
    # unaffected; this is the only place it was ever surfaced).

    # Battles scheduled today — informational, not a "Needs Action Today" item.
    # Daily Brief 2.0 excludes it from brief output, but still traced (below)
    # for developer-audit visibility, same as before.
    battles_today = data.get("Battles scheduled today", [])
    if isinstance(battles_today, list):
        for row in battles_today:
            try:
                tracer.record(_ht.TraceEntry(
                    rule_name="battles_today_observation",
                    episode_type="battle_observation",
                    action_taken="excluded_from_briefing",
                    reason="battles.battle_date matches today — traced, but Daily Brief 2.0"
                           " excludes battles_today from brief output",
                    source_table="battles",
                    source_record_id=row["id"],
                    source_fields={
                        "battle_id": row["id"],
                        "battle_date": row["battle_date"],
                        "battle_time": row["battle_time"],
                        "timezone": "none in source — stored as text",
                        "creator_screenname": row["creator_screenname"],
                        "opponent_name": row["opponent_name"],
                        "opponent_id": row["opponent_id"],
                        "battle_format": row["battle_format"],
                        "linked_status": "linked" if row["opponent_id"] else "unlinked",
                        "considered_as": "today",
                    },
                    entity_display_name=row["creator_screenname"],
                    confidence="high",
                ))
            except Exception:
                pass

    # Recent training comment count is informational, not a "Needs Action
    # Today" item — Daily Brief 2.0 excludes it from brief output.

    # Defense-in-depth (Fix 3): even if morning_briefing.py's source-query
    # filtering and resolve_stale_issues() sweep both somehow missed a
    # case, never let an episode belonging to a currently-deactivated
    # Pathway user reach a briefing. This is a live, independent check —
    # it does not rely on `data` carrying deactivation info (some callers,
    # e.g. hearth_ask.py's needs_attention_today route, intentionally pass
    # data={} to avoid that path's write side effects) or on the sweep
    # having run recently (it only runs on morning_briefing.py's own
    # schedule, not on every request that reaches build_context()).
    # get_inactive_user_ids() never raises and degrades to "nothing
    # filtered" if Pathway is unreachable, matching this function's
    # existing never-crash-the-brief discipline.
    candidate_user_ids = {ep["user_id"] for ep in (open_episodes or []) if ep["user_id"] is not None}
    inactive_user_ids = hearth_identity.get_inactive_user_ids(candidate_user_ids) if candidate_user_ids else set()

    # Filter open episodes to only those that should appear in today's briefing.
    # awareness episodes are tracked in memory but never surfaced in briefings.
    # pattern episodes respect a 3-day cooldown via last_briefed_at.
    # Hearth City and hearth_reader receive the full open_episodes list — only
    # the context passed to Gemini gets the filtered briefable_episodes.
    briefable_episodes = [
        ep for ep in (open_episodes or [])
        if _should_brief(ep, now_utc) and ep["user_id"] not in inactive_user_ids
    ]

    # Group episodes by entity_id — None means no linked person
    by_entity = {}  # entity_id (int) -> list of episode rows
    unattached_eps = []

    for ep in briefable_episodes:
        eid = ep["entity_id"]
        if eid is None:
            unattached_eps.append(ep)
        else:
            by_entity.setdefault(eid, []).append(ep)

    # Build PersonContext for each entity group
    person_contexts = []
    for entity_id, episodes in by_entity.items():
        display_name = episodes[0]["display_name"] or "a team member"
        concerns = _sort_concerns([_make_concern(ep, today, today_str) for ep in episodes])

        total_count = len(episodes)
        hearth_summary = None
        patterns_noticed = None
        coach_name = None
        cn_coach_name = None
        shop_coach_name = None
        recruiter_name = None
        if memory_conn:
            ctx = hearth_memory.get_entity_context(memory_conn, entity_id)
            if ctx:
                total_count = ctx["total_episode_count"]
                entity_row = ctx["entity"]
                hearth_summary = entity_row["summary"] or None
                patterns_noticed = entity_row["patterns_noticed"] or None

            coaches = hearth_relationships.get_related_entities(
                memory_conn, entity_id, "coached_by"
            )
            if coaches:
                coach_name = coaches[0]["display_name"] or "a team member"

            cn_coaches = hearth_relationships.get_related_entities(
                memory_conn, entity_id, "coached_by_cn"
            )
            if cn_coaches:
                cn_coach_name = cn_coaches[0]["display_name"] or None

            shop_coaches = hearth_relationships.get_related_entities(
                memory_conn, entity_id, "coached_by_shop"
            )
            if shop_coaches:
                shop_coach_name = shop_coaches[0]["display_name"] or None

            recruiters = hearth_relationships.get_related_entities(
                memory_conn, entity_id, "recruited_by"
            )
            if recruiters:
                recruiter_name = recruiters[0]["display_name"] or None

        person_contexts.append(PersonContext(
            user_id=episodes[0]["user_id"],
            display_name=display_name,
            open_concerns=concerns,
            total_episode_count=total_count,
            hearth_summary=hearth_summary,
            patterns_noticed=patterns_noticed,
            coach_name=coach_name,
            cn_coach_name=cn_coach_name,
            shop_coach_name=shop_coach_name,
            recruiter_name=recruiter_name,
        ))

        # Trace each open episode being included for this person
        for ep in episodes:
            try:
                first_seen = ep["observed_at"][:10]
                try:
                    age_days = (today - date.fromisoformat(first_seen)).days
                except (ValueError, TypeError):
                    age_days = 0
                is_recurring = first_seen < today_str
                tracer.record(_ht.TraceEntry(
                    rule_name="context_inclusion",
                    episode_type=ep["episode_type"],
                    action_taken="included_in_briefing",
                    reason=(
                        f"open episode, severity={ep['severity']}, "
                        f"age={age_days}d, recurring={is_recurring}, "
                        f"entity={display_name}"
                    ),
                    reference_key=ep["reference_key"],
                    entity_user_id=ep["user_id"],
                    entity_display_name=display_name,
                    confidence="high",
                ))
            except Exception:
                pass

    # Sort people: multiple issues first (escalation signal), then by highest severity
    person_contexts.sort(key=lambda p: (
        -len(p.open_concerns),
        _SEVERITY_ORDER.get(p.open_concerns[0].severity if p.open_concerns else "low", 1),
    ))

    # Shared-coach group signal: if 2+ people with the same coach have open
    # concerns, this computes that pattern (kept for potential future use,
    # e.g. a Manager Dashboard view) but Daily Brief 2.0 does not append it
    # as an observation — it must not reach the brief-bound context.
    # Prefer program-specific coach names; fall back to legacy coach_name.
    coach_groups = {}
    for pc in person_contexts:
        for cname in filter(None, [pc.cn_coach_name, pc.shop_coach_name, pc.coach_name]):
            coach_groups.setdefault(cname, set()).add(pc.display_name)

    # Trace unattached concerns (no linked person)
    unattached_concerns = _sort_concerns(
        [_make_concern(ep, today, today_str) for ep in unattached_eps]
    )
    for ep in unattached_eps:
        try:
            tracer.record(_ht.TraceEntry(
                rule_name="context_inclusion_unattached",
                episode_type=ep["episode_type"],
                action_taken="included_in_briefing",
                reason=f"open episode, no linked entity, severity={ep['severity']}",
                reference_key=ep["reference_key"],
                entity_user_id=None,
                entity_display_name=None,
                confidence="high",
            ))
        except Exception:
            pass

    # Recent resolutions: issues Hearth was tracking that have since been fixed
    recent_resolutions = []
    if memory_conn:
        for ep in hearth_memory.get_recent_resolutions(memory_conn, hours=24):
            try:
                opened = date.fromisoformat(ep["observed_at"][:10])
                closed = date.fromisoformat(ep["resolved_at"][:10])
                days_open = max(0, (closed - opened).days)
            except (ValueError, TypeError):
                days_open = 0
            recent_resolutions.append(RecentResolution(
                description=ep["description"],
                episode_type=ep["episode_type"],
                person_name=ep["display_name"] or None,
                resolved_at=ep["resolved_at"],
                days_open=days_open,
            ))
            try:
                tracer.record(_ht.TraceEntry(
                    rule_name="recent_resolution_inclusion",
                    episode_type=ep["episode_type"],
                    action_taken="included_in_briefing",
                    reason=f"resolved in last 24h, was open {days_open}d",
                    reference_key=ep["reference_key"],
                    entity_user_id=ep["user_id"],
                    entity_display_name=ep["display_name"] or None,
                    confidence="high",
                ))
            except Exception:
                pass

    # Stamp last_briefed_at for pattern episodes that were just included so the
    # cooldown window starts from this run.
    if memory_conn:
        ts = now_utc.isoformat()
        for ep in briefable_episodes:
            if ep["briefing_category"] == "pattern":
                hearth_memory.update_last_briefed_at(memory_conn, ep["id"], ts)

    # Collect principles whose domain tags correspond to active episode types.
    relevant_principles = []
    if memory_conn:
        active_episode_types = [ep["episode_type"] for ep in briefable_episodes]
        principle_rows = collect_relevant_principles(memory_conn, active_episode_types, tracer)
        relevant_principles = [
            {
                "principle_id": p["principle_id"],
                "content": p["content"],
                "confidence": p["confidence"],
            }
            for p in principle_rows
        ]

    # Worldview (Session 4): Hearth's current understanding, read first and
    # supplemented by everything collected above. Never raises — falls back
    # to an empty summary on any failure so briefing always still generates.
    worldview = collect_worldview_summary(memory_conn)

    return HearthAwarenessContext(
        date=now_utc.strftime("%A, %B %d, %Y"),
        observations=observations,
        person_contexts=person_contexts,
        unattached_concerns=unattached_concerns,
        recent_resolutions=recent_resolutions,
        relevant_principles=relevant_principles,
        worldview=worldview,
    )


# ---------------------------------------------------------------------------
# Renderer
# ---------------------------------------------------------------------------

def _age_note(concern: OpenConcern) -> str:
    if not concern.is_recurring:
        return "new today"
    age = concern.age_days
    if age == 1:
        return "since yesterday"
    if age < 7:
        return f"for {age} days"
    return f"for {age} days — worth escalating"


# Episode types whose description already states the real-world source age
# (e.g. training_comment_waiting embeds the comment's actual unanswered age
# from training_comments.created_at). For these, _age_note's tracking-age
# suffix — derived from hearth_episodes.observed_at, i.e. how long Hearth has
# known about the episode, not how long the underlying condition has existed —
# must not be appended: a classifier change made old comments newly eligible
# for detection, giving them a small observed_at-based age alongside a much
# larger real age, and Gemini rendered the misleading small number. See the
# July 2026 training-comment brief bug.
_NO_TRACKING_AGE_SUFFIX_EPISODE_TYPES = frozenset({"training_comment_waiting"})


def _age_suffix(concern: OpenConcern) -> str:
    """Trailing ' (tracking age note)' for a concern line, or '' if suppressed."""
    if concern.episode_type in _NO_TRACKING_AGE_SUFFIX_EPISODE_TYPES:
        return ""
    return f" ({_age_note(concern)})"


def _subject_label(row) -> Optional[str]:
    """Return the resolved display name for a single-subject worldview row, if any."""
    try:
        return row.get("_subject_name")
    except AttributeError:
        return None


def _relationship_label(row) -> Optional[str]:
    """Return a combined display label for a relationship row, if either side resolved."""
    try:
        a = row.get("_entity_a_name")
        b = row.get("_entity_b_name")
    except AttributeError:
        return None
    if a and b:
        return f"{a} & {b}"
    return a or b


def _labeled(label, text) -> str:
    """Prefix text with "label: " when a display name resolved, else text unchanged."""
    return f"{label}: {text}" if label else text


def _render_worldview_section(worldview: WorldviewSummary) -> list:
    """Render the worldview section as a list of lines, or [] if empty.

    Kept separate so render_for_llm can place it ahead of legacy sections
    (Session 4 priority order) without disturbing any existing rendering.
    Where a row's subject resolved to a real person (see _subject_label /
    _relationship_label), the line is prefixed with their display name —
    e.g. "Denise Lewis: entity userE has shown responsiveness" — instead of
    showing only the stored worldview text. Unresolved subjects render
    exactly as before.
    """
    if worldview.is_empty:
        return []

    lines = ["Hearth's current understanding:"]
    if worldview.active_beliefs:
        lines.append("  Beliefs:")
        for b in worldview.active_beliefs:
            text = _labeled(_subject_label(b), b['belief_text'])
            lines.append(f"    - {text} (confidence: {b['confidence']:.1f})")
    if worldview.active_relationships:
        lines.append("  Relationship understandings:")
        for r in worldview.active_relationships:
            text = _labeled(_relationship_label(r), r['relationship_summary'])
            lines.append(f"    - {text} (confidence: {r['confidence']:.1f})")
    if worldview.open_uncertainties:
        lines.append("  Open uncertainties:")
        for u in worldview.open_uncertainties:
            text = _labeled(_subject_label(u), u['uncertainty_text'])
            lines.append(f"    - {text}")
    if worldview.watched_changes:
        lines.append("  Watched changes:")
        for c in worldview.watched_changes:
            text = _labeled(_subject_label(c), c['change_text'])
            lines.append(f"    - {text}")
    if worldview.recent_lessons:
        lines.append("  Recent lessons (provisional, not yet human-approved):")
        for lesson in worldview.recent_lessons:
            lines.append(f"    - {lesson['lesson_text']}")
    lines.append("")
    return lines


def render_for_llm(context: HearthAwarenessContext) -> str:
    """
    Render a HearthAwarenessContext into the text block the language model receives.

    Uses Hearth's language. No table names, column names, SQL dicts, row counts,
    or schema information appears here. Issues are grouped by person so Hearth
    can speak about the whole person rather than a list of independent incidents.

    Since Session 4: if context.worldview has any content, it is rendered first
    (Hearth's current understanding takes priority over supporting evidence). If
    worldview is empty, output is byte-for-byte identical to before Session 4.
    """
    lines = [f"Date: {context.date}", ""]

    if context.is_quiet and context.worldview.is_empty:
        lines.append("Nothing of particular concern observed today.")
        return "\n".join(lines)

    lines.extend(_render_worldview_section(context.worldview))

    if context.is_quiet:
        lines.append("Nothing of particular concern observed today.")
        return "\n".join(lines)

    if context.observations:
        lines.append("What I'm noticing today:")
        for obs in context.observations:
            lines.append(f"  - {obs.text}")
        lines.append("")

    # context.recent_resolutions is intentionally not rendered — Daily Brief
    # 2.0 excludes positive/resolved updates from brief output. Still
    # populated by build_context() in case a future non-brief consumer needs it.

    if context.person_contexts:
        lines.append("Who I'm watching:")
        for person in context.person_contexts:
            if person.has_multiple_issues:
                lines.append(f"\n  {person.display_name} — {len(person.open_concerns)} open concerns:")
            else:
                lines.append(f"\n  {person.display_name}:")

            for concern in person.open_concerns:
                lines.append(
                    f"    - [{concern.severity.upper()}] {concern.description}"
                    f"{_age_suffix(concern)}"
                )

            if person.cn_coach_name:
                lines.append(f"    [CN Coach: {person.cn_coach_name}]")
            if person.shop_coach_name:
                lines.append(f"    [Shop Coach: {person.shop_coach_name}]")
            if not person.cn_coach_name and not person.shop_coach_name and person.coach_name:
                lines.append(f"    [Assigned coach: {person.coach_name}]")
            if person.recruiter_name:
                lines.append(f"    [Recruited by: {person.recruiter_name}]")
            if person.patterns_noticed:
                lines.append(f"    [Recurring pattern: {person.patterns_noticed}]")
            if person.hearth_summary:
                lines.append(f"    [Hearth memory: {person.hearth_summary}]")
            elif person.has_history and not person.patterns_noticed:
                resolved = person.total_episode_count - len(person.open_concerns)
                lines.append(
                    f"    [Hearth has seen {resolved} resolved issue(s) for this person before]"
                )
        lines.append("")

    if context.unattached_concerns:
        lines.append("Other open concerns:")
        for concern in context.unattached_concerns:
            lines.append(
                f"  - [{concern.severity.upper()}] {concern.description}"
                f"{_age_suffix(concern)}"
            )
        lines.append("")

    qualifying_principles = [
        p for p in context.relevant_principles if p["confidence"] >= 0.5
    ]
    if qualifying_principles:
        lines.append("What I know about this organization:")
        for p in qualifying_principles:
            lines.append(f"  - {p['content']} (confidence: {p['confidence']:.1f})")
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Smoke test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import hearth_memory
    import hearth_worldview

    # NOTE: seeded via direct INSERT rather than hearth_worldview.record_change/
    # upsert_uncertainty, and identified by change_text/uncertainty_text content
    # rather than source_run — this local hearth_memory.db predates
    # migrate_add_worldview_source_run.py (a pre-existing, unrelated migration
    # gap), so the source_run column those functions write doesn't exist here
    # yet. Only columns present in both schema versions are used below.
    _QUIET_MARKER_TEXT = "SMOKE_TEST_CREATOR_QUIET_MARKER_never_brief_this"
    _STUCK_MARKER_TEXT = "SMOKE_TEST_NEW_CREATOR_STUCK_MARKER_never_brief_this"
    _BRIEFABLE_MARKER_TEXT = "SMOKE_TEST_BRIEFABLE_CHANGE_MARKER_should_still_brief"

    conn = hearth_memory.get_memory_connection()
    try:
        hearth_worldview.ensure_worldview_tables(conn)
        _now = datetime.now(timezone.utc).isoformat()

        print("Step 1: seed a creator_quiet-derived watched change (the reported leak)")
        conn.execute(
            "INSERT INTO hearth_worldview_changes"
            " (subject_type, subject_id, change_text, confidence, status, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?);",
            ("creator_quiet_entity", "smoke_test_entity_1",
             f"Entity smoke_test_entity_1 has gone quiet. {_QUIET_MARKER_TEXT}",
             0.5, "watching", _now, _now),
        )

        print("Step 2: seed a new_creator_stuck-derived uncertainty (the other leak found in Phase 1)")
        conn.execute(
            "INSERT INTO hearth_worldview_uncertainties"
            " (subject_type, subject_id, uncertainty_text, confidence, priority, status,"
            "  created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?);",
            ("entity_episode", "new_creator_stuck:smoke_test_entity_2",
             f"new_creator_stuck for smoke_test_entity_2. {_STUCK_MARKER_TEXT}",
             0.5, "normal", "open", _now, _now),
        )

        print("Step 3: seed a briefable watched change (not derived from a _NEVER_BRIEF episode type)")
        conn.execute(
            "INSERT INTO hearth_worldview_changes"
            " (subject_type, subject_id, change_text, confidence, status, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?);",
            ("episode_type", "checkin_feedback_waiting",
             f"checkin_feedback_waiting spiking. {_BRIEFABLE_MARKER_TEXT}",
             0.5, "watching", _now, _now),
        )
        conn.commit()

        print("Step 4: collect_worldview_summary() + render_for_llm(), alongside a Needs Action Today item")
        worldview = collect_worldview_summary(conn)
        needs_action = PersonContext(
            user_id=999999,
            display_name="Smoke Test Person",
            open_concerns=[OpenConcern(
                description="SMOKE_TEST_NEEDS_ACTION_TODAY_MARKER",
                first_seen=date.today().isoformat(), age_days=0,
                severity="high", episode_type="support_request_waiting",
                is_recurring=False,
            )],
            total_episode_count=1,
        )
        context = HearthAwarenessContext(
            date="Smoke Test Day", person_contexts=[needs_action], worldview=worldview,
        )
        rendered = render_for_llm(context)

        assert _QUIET_MARKER_TEXT not in rendered, \
            "LEAK: creator_quiet watched-change rendered into Daily Brief-bound output"
        assert _STUCK_MARKER_TEXT not in rendered, \
            "LEAK: new_creator_stuck uncertainty rendered into Daily Brief-bound output"
        assert _BRIEFABLE_MARKER_TEXT in rendered, \
            "REGRESSION: a briefable watched change (not derived from a suppressed episode" \
            " type) was incorrectly suppressed"
        assert "SMOKE_TEST_NEEDS_ACTION_TODAY_MARKER" in rendered, \
            "REGRESSION: Needs Action Today content did not render alongside worldview content"

        print("  Suppressed: creator_quiet watched change, new_creator_stuck uncertainty")
        print("  Preserved: checkin_feedback_waiting watched change, Needs Action Today item")
        print("\nAll smoke test assertions passed.")
    finally:
        print("\nCleanup — removing all smoke-test rows")
        conn.execute(
            "DELETE FROM hearth_worldview_changes WHERE change_text LIKE ?;",
            (f"%{_BRIEFABLE_MARKER_TEXT}%",),
        )
        conn.execute(
            "DELETE FROM hearth_worldview_changes WHERE change_text LIKE ?;",
            (f"%{_QUIET_MARKER_TEXT}%",),
        )
        conn.execute(
            "DELETE FROM hearth_worldview_uncertainties WHERE uncertainty_text LIKE ?;",
            (f"%{_STUCK_MARKER_TEXT}%",),
        )
        conn.commit()
        remaining = conn.execute(
            "SELECT"
            " (SELECT COUNT(*) FROM hearth_worldview_changes"
            "  WHERE change_text LIKE ? OR change_text LIKE ?)"
            " + (SELECT COUNT(*) FROM hearth_worldview_uncertainties WHERE uncertainty_text LIKE ?);",
            (f"%{_QUIET_MARKER_TEXT}%", f"%{_BRIEFABLE_MARKER_TEXT}%", f"%{_STUCK_MARKER_TEXT}%"),
        ).fetchone()[0]
        print(f"  Remaining smoke-test rows after cleanup: {remaining}")
        conn.close()
        print("\nSmoke test complete.")
