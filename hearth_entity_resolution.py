"""
Hearth Entity Resolution — deterministic mapping from free text or a known
Pathway identifier to hearth_entities rows ("Buildings").

Originally lived inside hearth_ask.py as its single-name lookup path; moved
here so Ask Hearth and the Furniture Fact Extractor can share one resolver
instead of two copies drifting apart. Ask Hearth imports resolve_entity()
from here unchanged. The Fact Extractor additionally uses
resolve_entity_by_user_id() (author -> Building) and find_entity_mentions()
(scan a block of text for every known Building mentioned by name).

Matching rules, shared by every function in this module:
    1. exact display_name match
    2. case-insensitive display_name match
    3. alias match (hearth_entities.aliases, comma-separated convention)

Fuzzy matching is deliberately not implemented. The architecture allows it
later; V1 does not guess.
"""

import re
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class EntityResolution:
    """Result of resolving a free-text entity query against hearth_entities."""
    status: str  # resolved | ambiguous | not_found
    entity_id: Optional[int] = None
    entity_row: Optional[object] = None
    candidate_names: list = field(default_factory=list)  # populated only when ambiguous


def _resolved(row) -> EntityResolution:
    return EntityResolution(status="resolved", entity_id=row["id"], entity_row=row)


def _ambiguous(rows) -> EntityResolution:
    names = sorted({row["display_name"] or f"Building {row['id']}" for row in rows})
    return EntityResolution(status="ambiguous", candidate_names=names)


def resolve_entity(memory_conn, query: str) -> EntityResolution:
    """Layered, deterministic resolution of a free-text query to one Building.

    Order: exact display_name match, case-insensitive display_name match,
    alias match (hearth_entities.aliases, comma-separated convention — this
    column has no write path anywhere today, so this layer is currently
    always a no-op; kept for forward compatibility rather than removed).
    Each layer only falls through to the next if it finds zero matches; two
    or more matches at any layer is ambiguous, not a guess.

    Fuzzy matching is deliberately not implemented — see module docstring /
    Ask Hearth investigation notes for the proposed future rule and why it
    was deferred.
    """
    query = (query or "").strip()
    if not query:
        return EntityResolution(status="not_found")

    rows = memory_conn.execute(
        "SELECT * FROM hearth_entities WHERE display_name = ?;", (query,)
    ).fetchall()
    if len(rows) == 1:
        return _resolved(rows[0])
    if len(rows) > 1:
        return _ambiguous(rows)

    rows = memory_conn.execute(
        "SELECT * FROM hearth_entities WHERE LOWER(display_name) = LOWER(?);", (query,)
    ).fetchall()
    if len(rows) == 1:
        return _resolved(rows[0])
    if len(rows) > 1:
        return _ambiguous(rows)

    alias_candidates = memory_conn.execute(
        "SELECT * FROM hearth_entities WHERE aliases IS NOT NULL AND aliases != '';"
    ).fetchall()
    query_lower = query.lower()
    matched = [
        row for row in alias_candidates
        if query_lower in [a.strip().lower() for a in row["aliases"].split(",") if a.strip()]
    ]
    if len(matched) == 1:
        return _resolved(matched[0])
    if len(matched) > 1:
        return _ambiguous(matched)

    return EntityResolution(status="not_found")


# ---------------------------------------------------------------------------
# Author resolution (user_id -> Building)
# ---------------------------------------------------------------------------

def resolve_entity_by_user_id(memory_conn, user_id):
    """Return the hearth_entities row whose user_id equals user_id, or None.

    hearth_entities.user_id is UNIQUE, so this is always a single-row lookup
    — never ambiguous. Used to turn a Pathway source record's author column
    (a users.id) into its Building, when one exists. A Pathway user with no
    corresponding Hearth entity yet (never synced, or a non-person account)
    simply returns None — callers must treat that as "no author candidate,"
    not an error.
    """
    if user_id is None:
        return None
    return memory_conn.execute(
        "SELECT * FROM hearth_entities WHERE user_id = ?;", (user_id,)
    ).fetchone()


# ---------------------------------------------------------------------------
# Free-text candidate scanning (text -> every Building mentioned by name)
# ---------------------------------------------------------------------------

def _contains_name(text, name):
    """Whole-word, case-insensitive check for name inside text.

    Uses \\b word boundaries rather than a plain substring check so a short
    display_name like "Al" does not match inside "Always" — a naive
    substring scan would be a steady source of false-positive candidates,
    which conflicts with the conservative bias the Fact Extractor requires
    throughout (see hearth_fact_extractor.py module docstring).
    """
    if not name or not name.strip():
        return False
    return re.search(rf"\b{re.escape(name.strip())}\b", text, re.IGNORECASE) is not None


def find_entity_mentions(memory_conn, text, entity_type=None):
    """Return every distinct Building whose display_name or an alias appears
    literally in text, using the same matching primitives as resolve_entity()
    — applied as whole-word, case-insensitive scans across the whole text
    rather than as an exact lookup of a single query string.

    Unlike resolve_entity(), this never treats a same-name collision as
    "ambiguous" — if two different Buildings share a display_name and it
    appears in the text, both are returned as independent candidates; each
    one gets its own downstream evaluation rather than the scan guessing
    between them.

    Pronouns and other non-name references are never candidates — this is a
    literal name scan against known display_name/alias values only, no
    coreference resolution.

    entity_type, if given, restricts the candidate pool to that
    hearth_entities.entity_type (e.g. "person") before scanning — matching
    is otherwise unfiltered by type, same as resolve_entity().

    Returns a list of sqlite3.Row (deduplicated by id), in no particular
    order. Returns [] for empty/falsy text.
    """
    text = text or ""
    if not text.strip():
        return []

    if entity_type is not None:
        candidates = memory_conn.execute(
            "SELECT * FROM hearth_entities WHERE entity_type = ?;", (entity_type,)
        ).fetchall()
    else:
        candidates = memory_conn.execute("SELECT * FROM hearth_entities;").fetchall()

    matched = {}
    for row in candidates:
        if _contains_name(text, row["display_name"]):
            matched[row["id"]] = row
            continue

        aliases = row["aliases"] if "aliases" in row.keys() else None
        if aliases:
            for alias in aliases.split(","):
                if _contains_name(text, alias):
                    matched[row["id"]] = row
                    break

    return list(matched.values())
