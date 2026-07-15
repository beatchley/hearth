"""
Hearth Conversation Ledger — Phase 7b.

Temporary staging for Ask Hearth conversation turns on their way into the
existing Furniture Fact Extractor (hearth_fact_extractor.py), which now
treats conversations as its eighth observational source. This module
introduces no new governance, no new proposal workflow, and no live
extraction — a staged turn only ever participates in the extractor's
existing daily batch (Conversation Ledger -> candidate generation -> Gemini
extraction -> validation -> proposal -> human approval -> Furniture), the
exact pipeline already proven for every other source.

Architectural separation from hearth_attention_frame.py (restated per the
Phase 7b build brief — the two must never be merged): the Attention Frame
exists solely to maintain conversational continuity within one active,
in-process session — it is ephemeral and disappears on idle timeout,
explicit end, or process restart. This module has a different reliability
requirement: a staged human message must survive a process restart, since
it may sit in staging for up to a day before the next Fact Extractor batch
runs. That is why this is a real, durable table in hearth_memory.db (see
migrate_add_conversation_ledger_schema.py) rather than another in-process
dict — the opposite operational tradeoff from the Attention Frame, made
deliberately, not by accident.

Eligibility (never decided in this module): only human-authored turns that
actually entered Ask Hearth's manager-advice cognitive path
(hearth_manager_advice.py) — as opposed to one of the three original
fixed-pattern routes — are ever staged. That distinction is made by
hearth_ask.py, from routing (a code-level fact), never by inspecting
message content here. Hearth's own responses are never staged; this
module is only ever called with a human's incoming message text.

Append-only while staged. Purged (deleted) by the Fact Extractor once an
entry has been evaluated (successfully processed or found already
processed) — see hearth_fact_extractor.py's conversation-ledger source
handling. The permanent artifact of a staged turn, if one exists, is a
Furniture proposal; the raw conversation text is never meant to
accumulate into a searchable history.
"""

from datetime import datetime, timezone

# The identity this source uses in hearth_processed_sources (source_type)
# and in Furniture proposal provenance — see hearth_fact_extractor.py.
SOURCE_TYPE = "ask_hearth_conversations"

DEFAULT_FETCH_LIMIT = 50


def _now():
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Write (staging side — called from hearth_ask.py)
# ---------------------------------------------------------------------------

def stage_conversation_turn(conn, message_text, session_id=None, author_user_id=None, actor_role=None):
    """Append one eligible human conversation turn to the ledger. Returns
    the new row's id.

    Called the moment a turn is known to be eligible (see hearth_ask.py) —
    never batched, never deferred to conversation end. This is what makes
    an abandoned conversation still capturable and a process interruption
    unable to lose an entire conversation's worth of turns.

    session_id/author_user_id/actor_role are optional provenance only (see
    module docstring) — message_text is the only field the extraction
    pipeline actually requires.
    """
    if not message_text or not message_text.strip():
        raise ValueError("message_text is required")

    cur = conn.execute(
        "INSERT INTO hearth_conversation_ledger"
        " (session_id, author_user_id, actor_role, message_text, created_at)"
        " VALUES (?, ?, ?, ?, ?);",
        (session_id, author_user_id, actor_role, message_text.strip(), _now()),
    )
    conn.commit()
    return cur.lastrowid


# ---------------------------------------------------------------------------
# Read / purge (extractor side — called from hearth_fact_extractor.py)
# ---------------------------------------------------------------------------

def get_pending_entries(conn, limit=DEFAULT_FETCH_LIMIT):
    """Every currently-staged entry, oldest first, up to limit.

    "Pending" here means "still present in the ledger" — this table holds
    nothing else, since a processed entry is purged (see delete_entry).
    A row can legitimately still exist here after already being marked
    processed only if a prior run crashed between marking it processed and
    purging it; the Fact Extractor's own per-record processed-source check
    (hearth_furniture_proposals.is_source_processed, shared with every
    other source) makes re-encountering that row safe without this
    function needing to know anything about that ledger itself.
    """
    return conn.execute(
        "SELECT * FROM hearth_conversation_ledger ORDER BY created_at ASC, id ASC LIMIT ?;",
        (limit,),
    ).fetchall()


def delete_entry(conn, ledger_id):
    """Purge one entry from staging. Safe to call on an id that no longer
    exists (no-op) — e.g. a retry after a crash where the delete already
    happened before the crash.
    """
    conn.execute("DELETE FROM hearth_conversation_ledger WHERE id = ?;", (ledger_id,))
    conn.commit()


def count_entries(conn):
    """Number of currently-staged entries — exposed for tests."""
    return conn.execute("SELECT COUNT(*) FROM hearth_conversation_ledger;").fetchone()[0]
