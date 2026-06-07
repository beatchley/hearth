"""
Hearth Context Builder — assembles Hearth's awareness before language generation.

This module is the boundary between raw Pathway data and Hearth's perspective.
It translates database rows and memory episodes into structured observations that
describe what Hearth has noticed, in Hearth's own terms.

The language model receives this context — not raw database rows, table names,
column definitions, or query output.

Pipeline:
    Pathway Data  →  Hearth Memory  →  HearthAwarenessContext  →  LLM  →  Hearth Message
"""

from dataclasses import dataclass, field
from datetime import datetime, date, timezone
from typing import Optional


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
    coach_name: Optional[str] = None           # Display name of assigned coach if known

    @property
    def has_multiple_issues(self):
        return len(self.open_concerns) > 1

    @property
    def has_history(self):
        """True if Hearth has seen this person in resolved episodes, not just open ones."""
        return self.total_episode_count > len(self.open_concerns)


@dataclass
class HearthAwarenessContext:
    """Hearth's complete awareness for a briefing moment."""
    date: str
    observations: list = field(default_factory=list)          # List[Observation]
    person_contexts: list = field(default_factory=list)       # List[PersonContext]
    unattached_concerns: list = field(default_factory=list)   # List[OpenConcern] — no linked person
    recent_resolutions: list = field(default_factory=list)    # List[RecentResolution]

    @property
    def is_quiet(self):
        return (
            not self.observations
            and not self.person_contexts
            and not self.unattached_concerns
            and not self.recent_resolutions
        )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}


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

def build_context(data: dict, open_episodes: list, memory_conn=None) -> HearthAwarenessContext:
    """
    Transform Pathway operational data and Hearth memory episodes into
    a HearthAwarenessContext — Hearth's assembled awareness.

    Separation of concerns:
    - observations: informational items from today (new joins, scheduled battles,
      recent comments) that are not tracked as persistent episodes.
    - person_contexts: one PersonContext per person with open episodes, grouping
      all their concerns together so Hearth can speak about the whole person.
    - unattached_concerns: open episodes not tied to a specific person (e.g.
      battles with unlinked opponents).

    memory_conn is optional. If provided, each PersonContext is enriched with
    the person's total historical episode count and any Hearth summary notes.
    Without it, the context is still person-aware — just less historically deep.
    """
    import hearth_memory       # local imports to avoid circular dependency
    import hearth_relationships

    today = datetime.now(timezone.utc).date()
    today_str = today.isoformat()
    observations = []

    # New users — informational; not tracked as episodes
    new_users = data.get("New users (last 24 h)", [])
    if isinstance(new_users, list):
        for row in new_users:
            observations.append(Observation(
                text=f"{row['name']} joined recently.",
                person=row["name"],
            ))

    # Battles scheduled today — informational
    battles_today = data.get("Battles scheduled today", [])
    if isinstance(battles_today, list):
        for row in battles_today:
            time_part = f" at {row['battle_time']}" if row.get("battle_time") else ""
            observations.append(Observation(
                text=(
                    f"{row['creator_screenname']} has a battle{time_part}"
                    f" against {row['opponent_name']}."
                ),
                person=row["creator_screenname"],
            ))

    # Recent training comments — informational
    comments = data.get("Recent training comments (last 24 h)", [])
    if isinstance(comments, list) and comments:
        count = len(comments)
        if count == 1:
            observations.append(Observation(text="One training comment came in over the last day."))
        else:
            observations.append(Observation(text=f"{count} training comments came in over the last day."))

    # Group episodes by entity_id — None means no linked person
    by_entity = {}  # entity_id (int) -> list of episode rows
    unattached_eps = []

    for ep in (open_episodes or []):
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

        person_contexts.append(PersonContext(
            user_id=episodes[0]["user_id"],
            display_name=display_name,
            open_concerns=concerns,
            total_episode_count=total_count,
            hearth_summary=hearth_summary,
            patterns_noticed=patterns_noticed,
            coach_name=coach_name,
        ))

    # Sort people: multiple issues first (escalation signal), then by highest severity
    person_contexts.sort(key=lambda p: (
        -len(p.open_concerns),
        _SEVERITY_ORDER.get(p.open_concerns[0].severity if p.open_concerns else "low", 1),
    ))

    # Shared-coach group signal: if 2+ people with the same coach have open concerns,
    # surface that as an observation so the briefing can mention the pattern.
    coach_groups = {}
    for pc in person_contexts:
        if pc.coach_name:
            coach_groups.setdefault(pc.coach_name, []).append(pc.display_name)
    for coach_name, members in coach_groups.items():
        if len(members) >= 2:
            names = ", ".join(members)
            observations.append(Observation(
                text=(
                    f"Multiple members connected to {coach_name} have open concerns: {names}."
                ),
                person=coach_name,
            ))

    unattached_concerns = _sort_concerns(
        [_make_concern(ep, today, today_str) for ep in unattached_eps]
    )

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

    return HearthAwarenessContext(
        date=datetime.now().strftime("%A, %B %d, %Y"),
        observations=observations,
        person_contexts=person_contexts,
        unattached_concerns=unattached_concerns,
        recent_resolutions=recent_resolutions,
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


def render_for_llm(context: HearthAwarenessContext) -> str:
    """
    Render a HearthAwarenessContext into the text block the language model receives.

    Uses Hearth's language. No table names, column names, SQL dicts, row counts,
    or schema information appears here. Issues are grouped by person so Hearth
    can speak about the whole person rather than a list of independent incidents.
    """
    lines = [f"Date: {context.date}", ""]

    if context.is_quiet:
        lines.append("Nothing of particular concern observed today.")
        return "\n".join(lines)

    if context.observations:
        lines.append("What I'm noticing today:")
        for obs in context.observations:
            lines.append(f"  - {obs.text}")
        lines.append("")

    if context.recent_resolutions:
        lines.append("What's been resolved:")
        for res in context.recent_resolutions:
            if res.days_open > 1:
                duration = f" (was open for {res.days_open} days)"
            elif res.days_open == 1:
                duration = " (was open since yesterday)"
            else:
                duration = ""
            lines.append(f"  - {res.description}{duration}")
        lines.append("")

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
                    f" ({_age_note(concern)})"
                )

            if person.coach_name:
                lines.append(f"    [Assigned coach: {person.coach_name}]")
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
                f" ({_age_note(concern)})"
            )
        lines.append("")

    return "\n".join(lines)
