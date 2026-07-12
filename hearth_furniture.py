"""
Hearth Furniture — single access layer for hearth_entity_furniture.

Furniture holds durable, stand-alone facts about a Building ("the because
test" — see hearth_furniture_taxonomy.py). This module owns the one shared
INSERT path onto that table, used by:
    - the manual Furniture UI (pathway-portal/backend/app/hearth_reader.py's
      add_furniture(), via the sys.path exception documented there)
    - Furniture proposal approval (hearth_furniture_proposals.py), in the
      same database transaction as marking the proposal approved

create_furniture() takes an existing connection and does not commit — the
caller controls the transaction boundary. This is what lets proposal
approval write Furniture and update the proposal row atomically instead of
as two separate commits.
"""

from datetime import datetime, timezone


def _now():
    return datetime.now(timezone.utc).isoformat()


def create_furniture(conn, entity_id, fact_text, fact_type, source, confidence,
                      first_observed_at=None, last_confirmed_at=None, status="active"):
    """Insert a new hearth_entity_furniture row and return its id.

    Does not commit — the caller owns the transaction. fact_text and
    fact_type are required (fact_type should be one of
    hearth_furniture_taxonomy.FURNITURE_CATEGORIES for new writers, but this
    function does not itself enforce that — validation is the caller's job,
    consistent with "no layer performs another layer's job"). source and
    confidence describe provenance (e.g. "manual", or
    "fact_extractor:<source_type>") — always required, never defaulted
    silently, so every Furniture row's origin is traceable.

    first_observed_at/last_confirmed_at default to now() if not given.
    """
    if not fact_text or not fact_text.strip():
        raise ValueError("fact_text is required")
    if not fact_type:
        raise ValueError("fact_type is required")
    if not source:
        raise ValueError("source is required")
    if confidence is None:
        raise ValueError("confidence is required")

    now = _now()
    cur = conn.execute(
        "INSERT INTO hearth_entity_furniture"
        " (entity_id, fact_text, fact_type, source, confidence,"
        "  first_observed_at, last_confirmed_at, status, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);",
        (entity_id, fact_text.strip(), fact_type, source, confidence,
         first_observed_at or now, last_confirmed_at or now, status, now, now),
    )
    return cur.lastrowid


def get_active_furniture(conn, entity_id):
    """Return active hearth_entity_furniture rows for one Building.

    Read helper used by the Fact Extractor's duplicate-suppression step 2
    (semantic comparison against what a Building already has on record).
    """
    return conn.execute(
        "SELECT * FROM hearth_entity_furniture WHERE entity_id = ? AND status = 'active'"
        " ORDER BY created_at DESC;",
        (entity_id,),
    ).fetchall()
