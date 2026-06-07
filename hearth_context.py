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
class HearthAwarenessContext:
    """Hearth's complete awareness for a briefing moment."""
    date: str
    observations: list = field(default_factory=list)   # List[Observation]
    open_concerns: list = field(default_factory=list)  # List[OpenConcern]

    @property
    def is_quiet(self):
        return not self.observations and not self.open_concerns


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------

def build_context(data: dict, open_episodes: list) -> HearthAwarenessContext:
    """
    Transform Pathway operational data and Hearth memory episodes into
    a HearthAwarenessContext — Hearth's assembled awareness.

    Separation of concerns:
    - observations: informational items from today (new joins, scheduled battles,
      recent comments) — things that are not tracked as persistent episodes.
    - open_concerns: everything recorded in Hearth's memory (probation, missing
      Discord, unlinked battles), both new today and carrying over from prior runs.
      These are never duplicated in observations.
    """
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
            observations.append(Observation(
                text="One training comment came in over the last day."
            ))
        else:
            observations.append(Observation(
                text=f"{count} training comments came in over the last day."
            ))

    # Open concerns — from Hearth's memory only (episodes handle deduplication)
    open_concerns = []
    for ep in (open_episodes or []):
        first_seen_str = ep["observed_at"][:10]
        try:
            first_seen_date = date.fromisoformat(first_seen_str)
            age_days = (today - first_seen_date).days
        except (ValueError, TypeError):
            age_days = 0

        open_concerns.append(OpenConcern(
            description=ep["description"],
            first_seen=first_seen_str,
            age_days=age_days,
            severity=ep["severity"],
            episode_type=ep["episode_type"],
            is_recurring=(first_seen_str < today_str),
        ))

    # Sort: high severity first, then by age descending (oldest = most urgent)
    open_concerns.sort(key=lambda c: (
        {"high": 0, "medium": 1, "low": 2}.get(c.severity, 1),
        -c.age_days,
    ))

    return HearthAwarenessContext(
        date=datetime.now().strftime("%A, %B %d, %Y"),
        observations=observations,
        open_concerns=open_concerns,
    )


# ---------------------------------------------------------------------------
# Renderer
# ---------------------------------------------------------------------------

def render_for_llm(context: HearthAwarenessContext) -> str:
    """
    Render a HearthAwarenessContext into the text block the language model receives.

    Uses Hearth's language. No table names, column names, SQL dicts, row counts,
    or schema information appears here.
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

    if context.open_concerns:
        lines.append("What I've been tracking:")
        for concern in context.open_concerns:
            if concern.is_recurring:
                age = concern.age_days
                if age == 1:
                    age_note = "since yesterday"
                elif age < 7:
                    age_note = f"for {age} days"
                else:
                    age_note = f"for {age} days — worth escalating"
                lines.append(
                    f"  - [{concern.severity.upper()}] {concern.description}"
                    f" (open {age_note})"
                )
            else:
                lines.append(
                    f"  - [{concern.severity.upper()}] {concern.description} (new today)"
                )
        lines.append("")

    return "\n".join(lines)
