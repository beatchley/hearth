"""
Hearth Furniture Proposals — single access layer for
hearth_furniture_proposals and the hearth_processed_sources ledger.

The Furniture Fact Extractor never writes hearth_entity_furniture directly.
It writes proposals here; a human approves or dismisses each one from the
Furniture Proposals review page. Approving a proposal is the only path from
here into Furniture (see approve_furniture_proposal, which writes Furniture
and marks the proposal approved in one transaction).

Status values: pending, approved, dismissed. There is no "superseded" —
the extractor never merges, updates, or strengthens a proposal once
written; duplicate suppression (see hearth_fact_extractor.py) happens
before a proposal is created, not after.

Convention: functions here raise on programmer error (missing required
field, unknown proposal id) rather than returning (row, error) tuples —
matching hearth_worldview.py's core-module style. The Fact Extractor's
own per-record loop is responsible for catching exceptions so one bad
record never aborts a batch (see hearth_fact_extractor.py). The two
UI-facing actions (approve/dismiss) are the exception: they return
(row, error) tuples, matching hearth_reader.py's Flask-facing convention,
since pathway-portal's approve/dismiss routes call these directly and
expect that shape (mirroring approve_state_proposal/dismiss_state_proposal).
"""

from datetime import datetime, timezone

import hearth_furniture

VALID_STATUSES = ("pending", "approved", "dismissed")


def _now():
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Proposal writes (extractor side)
# ---------------------------------------------------------------------------

def create_furniture_proposal(conn, entity_id, proposed_fact, fact_type, confidence,
                               evidence_quote, source_type, source_record_id,
                               source_author_user_id, semantic_fingerprint,
                               extractor_version):
    """Insert a new pending Furniture proposal and return its id.

    Every field is required except source_author_user_id (a source record
    may have no resolvable author entity). Commits immediately — proposal
    creation is not part of any larger transaction.
    """
    if not proposed_fact or not proposed_fact.strip():
        raise ValueError("proposed_fact is required")
    if not fact_type:
        raise ValueError("fact_type is required")
    if confidence is None:
        raise ValueError("confidence is required")
    if not evidence_quote:
        raise ValueError("evidence_quote is required")
    if not source_type or not source_record_id:
        raise ValueError("source_type and source_record_id are required")
    if not semantic_fingerprint:
        raise ValueError("semantic_fingerprint is required")
    if not extractor_version:
        raise ValueError("extractor_version is required")

    cur = conn.execute(
        "INSERT INTO hearth_furniture_proposals"
        " (entity_id, proposed_fact, fact_type, confidence, evidence_quote,"
        "  source_type, source_record_id, source_author_user_id,"
        "  semantic_fingerprint, extractor_version, status, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?);",
        (entity_id, proposed_fact.strip(), fact_type, confidence, evidence_quote,
         source_type, str(source_record_id), source_author_user_id,
         semantic_fingerprint, extractor_version, _now()),
    )
    conn.commit()
    return cur.lastrowid


# ---------------------------------------------------------------------------
# Duplicate-suppression lookups (step 1: exact fingerprint match)
# ---------------------------------------------------------------------------

def find_pending_by_fingerprint(conn, entity_id, semantic_fingerprint):
    return conn.execute(
        "SELECT * FROM hearth_furniture_proposals"
        " WHERE entity_id = ? AND semantic_fingerprint = ? AND status = 'pending'"
        " LIMIT 1;",
        (entity_id, semantic_fingerprint),
    ).fetchone()


def find_dismissed_by_fingerprint(conn, entity_id, semantic_fingerprint):
    return conn.execute(
        "SELECT * FROM hearth_furniture_proposals"
        " WHERE entity_id = ? AND semantic_fingerprint = ? AND status = 'dismissed'"
        " LIMIT 1;",
        (entity_id, semantic_fingerprint),
    ).fetchone()


def find_approved_by_fingerprint(conn, entity_id, semantic_fingerprint):
    return conn.execute(
        "SELECT * FROM hearth_furniture_proposals"
        " WHERE entity_id = ? AND semantic_fingerprint = ? AND status = 'approved'"
        " LIMIT 1;",
        (entity_id, semantic_fingerprint),
    ).fetchone()


def get_pending_proposals_for_entity(conn, entity_id):
    """All pending proposals for one Building — used for duplicate-suppression
    step 2 (semantic comparison against what's already queued, not just what's
    already Furniture)."""
    return conn.execute(
        "SELECT * FROM hearth_furniture_proposals WHERE entity_id = ? AND status = 'pending';",
        (entity_id,),
    ).fetchall()


# ---------------------------------------------------------------------------
# Reads (UI side)
# ---------------------------------------------------------------------------

def get_furniture_proposal(conn, proposal_id):
    return conn.execute(
        "SELECT * FROM hearth_furniture_proposals WHERE id = ?;", (proposal_id,)
    ).fetchone()


def get_furniture_proposals(conn, status="pending", limit=100):
    """Return proposals for the review page, most recent first.

    status: one of VALID_STATUSES, or "all". Each row is enriched with the
    Building's display_name/entity_type so the UI never needs a second query
    per row.
    """
    sql = (
        "SELECT p.*, e.display_name AS building_display_name,"
        " e.entity_type AS building_entity_type"
        " FROM hearth_furniture_proposals p"
        " JOIN hearth_entities e ON e.id = p.entity_id"
    )
    params = []
    if status != "all":
        if status not in VALID_STATUSES:
            raise ValueError(f"status must be one of {VALID_STATUSES} or 'all', got {status!r}")
        sql += " WHERE p.status = ?"
        params.append(status)
    sql += " ORDER BY p.created_at DESC"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    return conn.execute(sql + ";", params).fetchall()


# ---------------------------------------------------------------------------
# Approve / dismiss (UI-facing actions — (row, error) tuple convention)
# ---------------------------------------------------------------------------

def approve_furniture_proposal(conn, proposal_id, reviewed_by):
    """Approve a pending proposal: write Furniture and mark the proposal
    approved in one transaction. Returns (row, error).

    Atomic by construction — both writes happen on the same connection with
    no intermediate commit, so a crash between them is impossible; either
    both land or neither does. This deliberately differs from
    hearth_state_proposals' approve path (two separate committed
    connections), which the Fact Extractor investigation flagged as a real
    (if narrow) risk window — this function closes that window rather than
    repeating it, per the build spec's explicit instruction.
    """
    proposal = get_furniture_proposal(conn, proposal_id)
    if proposal is None:
        return None, f"No Furniture proposal with id {proposal_id}."
    if proposal["status"] != "pending":
        return None, f"Proposal {proposal_id} is not pending (status={proposal['status']})."

    try:
        furniture_id = hearth_furniture.create_furniture(
            conn,
            entity_id=proposal["entity_id"],
            fact_text=proposal["proposed_fact"],
            fact_type=proposal["fact_type"],
            source=f"fact_extractor:{proposal['source_type']}",
            confidence=proposal["confidence"],
        )
        now = _now()
        conn.execute(
            "UPDATE hearth_furniture_proposals"
            " SET status = 'approved', reviewed_by = ?, reviewed_at = ?,"
            "     applied_furniture_id = ?"
            " WHERE id = ?;",
            (reviewed_by, now, furniture_id, proposal_id),
        )
        conn.commit()
    except Exception as exc:
        conn.rollback()
        return None, str(exc)

    return get_furniture_proposal(conn, proposal_id), None


def dismiss_furniture_proposal(conn, proposal_id, reviewed_by):
    """Dismiss a pending proposal. Writes nothing to Furniture. Returns (row, error)."""
    proposal = get_furniture_proposal(conn, proposal_id)
    if proposal is None:
        return None, f"No Furniture proposal with id {proposal_id}."
    if proposal["status"] != "pending":
        return None, f"Proposal {proposal_id} is not pending (status={proposal['status']})."

    try:
        now = _now()
        conn.execute(
            "UPDATE hearth_furniture_proposals"
            " SET status = 'dismissed', reviewed_by = ?, reviewed_at = ?"
            " WHERE id = ?;",
            (reviewed_by, now, proposal_id),
        )
        conn.commit()
    except Exception as exc:
        conn.rollback()
        return None, str(exc)

    return get_furniture_proposal(conn, proposal_id), None


# ---------------------------------------------------------------------------
# Processed-source ledger
# ---------------------------------------------------------------------------

def is_source_processed(conn, source_type, source_record_id, content_hash, extractor_version):
    row = conn.execute(
        "SELECT 1 FROM hearth_processed_sources"
        " WHERE source_type = ? AND source_record_id = ? AND content_hash = ?"
        "   AND extractor_version = ?;",
        (source_type, str(source_record_id), content_hash, extractor_version),
    ).fetchone()
    return row is not None


def mark_source_processed(conn, source_type, source_record_id, content_hash, extractor_version):
    """Record that this exact (source, content, version) identity has been
    evaluated. Conflict-safe: replaying the same identity is a no-op, not an
    error, thanks to the table's UNIQUE constraint."""
    conn.execute(
        "INSERT INTO hearth_processed_sources"
        " (source_type, source_record_id, content_hash, extractor_version, processed_at)"
        " VALUES (?, ?, ?, ?, ?)"
        " ON CONFLICT(source_type, source_record_id, content_hash, extractor_version)"
        " DO NOTHING;",
        (source_type, str(source_record_id), content_hash, extractor_version, _now()),
    )
    conn.commit()
