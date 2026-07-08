"""
Hearth Questions — open question tracker for the Hearth AI teammate.

Stores questions Hearth has surfaced about creator behavior, Pathway norms, or
its own judgment calls. Questions are distinct from principles (settled beliefs)
and episodes (observed events). They represent things Hearth is still uncertain
about and wants to surface for human review.

Questions come from two sources:
  - Legacy generation: hearth_soul.py's generate_reflection() creates a
    question directly from its own reflection logic. Unchanged by this module.
  - Worldview-sourced: surface_worldview_questions() below turns open
    hearth_worldview_uncertainties rows into questions, so a question is an
    artifact of Soul's uncertainty rather than a separately generated idea.
    Falls back to a no-op (relying solely on legacy generation) if worldview
    is disabled, unavailable, or has no open uncertainties.
"""

import os
import sqlite3
from datetime import datetime, timezone

import hearth_worldview
from hearth_memory import MEMORY_DB_PATH

_VALID_STATUSES = {"open", "answered", "dismissed"}

HEARTH_WORLDVIEW_QUESTIONS_ENABLED = (
    os.environ.get("HEARTH_WORLDVIEW_QUESTIONS_ENABLED", "1") == "1"
)

_PRIORITY_RANK = {"high": 0, "normal": 1, "low": 2}


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

def get_questions_connection():
    """Open a read-write connection to the shared Hearth memory database."""
    conn = sqlite3.connect(MEMORY_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def ensure_questions_table(conn):
    """Create hearth_questions if it doesn't exist. Safe to run on existing databases."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS hearth_questions (
            question_id   INTEGER PRIMARY KEY AUTOINCREMENT,
            question_text TEXT    NOT NULL,
            topic_tags    TEXT,
            triggered_by  TEXT,
            status        TEXT    DEFAULT 'open',
            created_at    TEXT    NOT NULL,
            answered_at   TEXT
        );
    """)
    _ensure_column(conn, "hearth_questions", "source_type", "TEXT")
    _ensure_column(conn, "hearth_questions", "worldview_uncertainty_id", "INTEGER")
    conn.commit()


def _ensure_column(conn, table, column, column_type):
    """Add column to table if it doesn't already exist. Safe to call repeatedly."""
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table});")}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_type};")


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

def create_question(conn, question_text, topic_tags=None, triggered_by=None,
                     source_type=None, worldview_uncertainty_id=None):
    """Insert a new question and return its question_id.

    No-ops if an open question with identical question_text already exists,
    returning the existing id instead. topic_tags should be a comma-separated
    string or None. source_type and worldview_uncertainty_id are optional
    traceability fields for questions sourced from worldview uncertainties
    (see surface_worldview_questions); legacy callers can omit them.
    """
    existing = conn.execute(
        "SELECT question_id FROM hearth_questions"
        " WHERE question_text = ? AND status = 'open';",
        (question_text,),
    ).fetchone()
    if existing:
        return existing["question_id"]

    now = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        "INSERT INTO hearth_questions"
        " (question_text, topic_tags, triggered_by, status, created_at,"
        "  source_type, worldview_uncertainty_id)"
        " VALUES (?, ?, ?, 'open', ?, ?, ?);",
        (question_text, topic_tags, triggered_by, now, source_type, worldview_uncertainty_id),
    )
    conn.commit()
    return cur.lastrowid


def list_open_questions(conn, limit=20):
    """Return open questions ordered by created_at ASC (oldest first)."""
    return conn.execute(
        "SELECT * FROM hearth_questions"
        " WHERE status = 'open'"
        " ORDER BY created_at ASC"
        " LIMIT ?;",
        (limit,),
    ).fetchall()


def get_question(conn, question_id):
    """Return one question row by question_id, or None if not found."""
    return conn.execute(
        "SELECT * FROM hearth_questions WHERE question_id = ?;",
        (question_id,),
    ).fetchone()


def _resolve_linked_uncertainty(conn, question_id):
    """If this question came from a worldview uncertainty, resolve that uncertainty."""
    row = conn.execute(
        "SELECT worldview_uncertainty_id FROM hearth_questions WHERE question_id = ?;",
        (question_id,),
    ).fetchone()
    if row and row["worldview_uncertainty_id"]:
        try:
            hearth_worldview.resolve_uncertainty(conn, row["worldview_uncertainty_id"])
        except Exception:
            pass


def mark_question_answered(conn, question_id):
    """Set status to 'answered' and record answered_at. Only acts on open questions.

    If the question was sourced from a worldview uncertainty, also resolves
    that uncertainty so it does not resurface.
    """
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "UPDATE hearth_questions"
        " SET status = 'answered', answered_at = ?"
        " WHERE question_id = ? AND status = 'open';",
        (now, question_id),
    )
    conn.commit()
    _resolve_linked_uncertainty(conn, question_id)


def dismiss_question(conn, question_id):
    """Set status to 'dismissed'. Only acts on open questions.

    If the question was sourced from a worldview uncertainty, also resolves
    that uncertainty so it does not resurface.
    """
    conn.execute(
        "UPDATE hearth_questions SET status = 'dismissed'"
        " WHERE question_id = ? AND status = 'open';",
        (question_id,),
    )
    conn.commit()
    _resolve_linked_uncertainty(conn, question_id)


def list_questions_by_tag(conn, tag, status="open"):
    """Return questions whose topic_tags contain tag (case-insensitive), filtered by status.

    Raises ValueError if status is not a recognized value.
    Ordered by created_at ASC.
    """
    if status not in _VALID_STATUSES:
        raise ValueError(
            f"Invalid status '{status}'. Must be one of: {', '.join(sorted(_VALID_STATUSES))}"
        )
    pattern = f"%{tag.lower()}%"
    return conn.execute(
        "SELECT * FROM hearth_questions"
        " WHERE status = ?"
        "   AND LOWER(topic_tags) LIKE ?"
        " ORDER BY created_at ASC;",
        (status, pattern),
    ).fetchall()


# ---------------------------------------------------------------------------
# Worldview-sourced questions
# ---------------------------------------------------------------------------

def _priority_rank(priority):
    """Lower rank surfaces first. Unrecognized/missing priority sorts as 'normal'."""
    return _PRIORITY_RANK.get((priority or "normal").lower(), _PRIORITY_RANK["normal"])


def _generate_simple_question(uncertainty):
    """Build a lightweight question from an uncertainty row when possible_question is empty.

    No LLM involved by design — this is meant to be a plain restatement, not
    a polished question. possible_question (set by Soul) is always preferred
    when present.
    """
    question = f"What's the latest on: {uncertainty['uncertainty_text']}?"
    why = uncertainty["why_it_matters"]
    if why:
        question += f" (Matters because: {why})"
    return question


def _find_open_question_for_uncertainty(conn, uncertainty_id):
    """Return the open question already linked to uncertainty_id, or None."""
    return conn.execute(
        "SELECT * FROM hearth_questions"
        " WHERE worldview_uncertainty_id = ? AND status = 'open'"
        " ORDER BY created_at DESC LIMIT 1;",
        (uncertainty_id,),
    ).fetchone()


def create_or_update_worldview_question(conn, uncertainty_id, question_text,
                                         topic_tags="worldview"):
    """Create a question sourced from a worldview uncertainty, or update an existing one.

    Reuses the existing open question already linked to uncertainty_id rather
    than creating a duplicate (one uncertainty -> one living question);
    refreshes its text if the uncertainty's wording has changed since it was
    last surfaced. Returns the question_id either way.
    """
    existing = _find_open_question_for_uncertainty(conn, uncertainty_id)
    if existing:
        if existing["question_text"] != question_text:
            conn.execute(
                "UPDATE hearth_questions SET question_text = ? WHERE question_id = ?;",
                (question_text, existing["question_id"]),
            )
            conn.commit()
        return existing["question_id"]

    now = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        "INSERT INTO hearth_questions"
        " (question_text, topic_tags, triggered_by, status, created_at,"
        "  source_type, worldview_uncertainty_id)"
        " VALUES (?, ?, 'hearth_worldview', 'open', ?, 'worldview_uncertainty', ?);",
        (question_text, topic_tags, now, uncertainty_id),
    )
    conn.commit()
    return cur.lastrowid


def surface_worldview_questions(conn, limit=10):
    """Create/update questions from open hearth_worldview_uncertainties rows.

    For each open uncertainty (highest priority first, ties broken by newest
    created_at first, capped at limit): uses possible_question directly if
    present, otherwise generates a simple question from uncertainty_text and
    why_it_matters. Reuses/updates an existing open question for the same
    uncertainty rather than duplicating it. After a question is
    created/updated, the uncertainty's status is advanced to
    'question_surfaced' via hearth_worldview.update_uncertainty() so it drops
    out of get_open_uncertainties() and isn't reprocessed on the next run.

    Returns the list of question_ids touched. Returns [] without raising if
    HEARTH_WORLDVIEW_QUESTIONS_ENABLED is false, worldview is unavailable
    (e.g. tables missing), or there are no open uncertainties — legacy
    question generation in hearth_soul.py is unaffected either way.
    """
    if not HEARTH_WORLDVIEW_QUESTIONS_ENABLED:
        return []

    try:
        uncertainties = hearth_worldview.get_open_uncertainties(conn)
    except sqlite3.OperationalError as exc:
        print(f"[HEARTH QUESTIONS] worldview uncertainties unavailable, skipping: {exc}")
        return []

    if not uncertainties:
        return []

    # Stable sorts compose: newest-first within each priority tier.
    uncertainties = sorted(uncertainties, key=lambda r: r["created_at"] or "", reverse=True)
    uncertainties = sorted(uncertainties, key=lambda r: _priority_rank(r["priority"]))
    uncertainties = uncertainties[:limit]

    question_ids = []
    for uncertainty in uncertainties:
        question_text = uncertainty["possible_question"] or _generate_simple_question(uncertainty)
        question_id = create_or_update_worldview_question(conn, uncertainty["id"], question_text)
        hearth_worldview.update_uncertainty(conn, uncertainty["id"], status="question_surfaced")
        question_ids.append(question_id)

    return question_ids


# ---------------------------------------------------------------------------
# Smoke test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    conn = get_questions_connection()

    print("Step 1: ensure_questions_table()")
    ensure_questions_table(conn)
    print("  Table ready.")

    print("\nStep 2: create_question() — seed question")
    seed_text = (
        "Should Hearth treat repeated skipped check-ins as meaningful inactivity,"
        " or only as weak signal?"
    )
    qid = create_question(
        conn,
        question_text=seed_text,
        topic_tags="checkins,creator_activity,judgment",
        triggered_by="step_3_smoke_test",
    )
    print(f"  question_id={qid}")

    print("\nStep 3: list_open_questions()")
    questions = list_open_questions(conn)
    print(f"  {len(questions)} open question(s)")
    for q in questions:
        print(f"  [{q['question_id']}] tags={q['topic_tags']}")
        print(f"       {q['question_text'][:80]}{'...' if len(q['question_text']) > 80 else ''}")

    print("\nStep 4: list_questions_by_tag('judgment')")
    tagged = list_questions_by_tag(conn, "judgment")
    print(f"  {len(tagged)} result(s)")
    for q in tagged:
        print(f"  [{q['question_id']}] {q['question_text'][:80]}")

    print("\nStep 5: get_question()")
    row = get_question(conn, qid)
    if row:
        print(f"  question_id={row['question_id']} status={row['status']}")
        print(f"  triggered_by={row['triggered_by']}")
        print(f"  created_at={row['created_at']}")

    print("\nStep 6: dismiss_question()")
    dismiss_question(conn, qid)
    dismissed = get_question(conn, qid)
    print(f"  status after dismiss: {dismissed['status']}")

    print("\nStep 7: confirm not in list_open_questions()")
    open_after = list_open_questions(conn)
    ids_open = [q["question_id"] for q in open_after]
    if qid not in ids_open:
        print(f"  Confirmed: question_id={qid} is no longer open.")
    else:
        print(f"  ERROR: question_id={qid} still appears as open.")

    print("\nStep 8: HEARTH_WORLDVIEW_QUESTIONS_ENABLED default")
    print(f"  HEARTH_WORLDVIEW_QUESTIONS_ENABLED={HEARTH_WORLDVIEW_QUESTIONS_ENABLED}")

    _TEST_MARKER = "session_5_smoke_test"
    hearth_worldview.ensure_worldview_tables(conn)
    test_uncertainty_ids = []

    try:
        print("\nStep 9: open_uncertainty() — one with possible_question, one without")
        unc_with_q = hearth_worldview.open_uncertainty(
            conn, subject_type="creator", subject_id="session_5_test_creator",
            uncertainty_text="SESSION 5 SMOKE TEST: is creator still engaged with weekly check-ins?",
            why_it_matters="Determines whether to escalate to coach.",
            possible_question="Is the creator still doing weekly check-ins, or have they quietly stopped?",
            priority="high", source_episode_id=_TEST_MARKER,
        )
        unc_without_q = hearth_worldview.open_uncertainty(
            conn, subject_type="creator", subject_id="session_5_test_creator_2",
            uncertainty_text="SESSION 5 SMOKE TEST: unclear why response time has slowed",
            why_it_matters="Could indicate burnout or just a busy week.",
            priority="normal", source_episode_id=_TEST_MARKER,
        )
        test_uncertainty_ids += [unc_with_q, unc_without_q]
        print(f"  unc_with_q={unc_with_q}, unc_without_q={unc_without_q}")

        print("\nStep 10: surface_worldview_questions() — first run")
        qids = surface_worldview_questions(conn)
        print(f"  {len(qids)} question_id(s) touched: {qids}")
        # Both test uncertainties must be surfaced; pre-existing open uncertainties
        # from other smoke tests may also appear — check by uncertainty_id, not count.
        assert unc_with_q in [
            conn.execute(
                "SELECT worldview_uncertainty_id FROM hearth_questions WHERE question_id = ?;",
                (qid,),
            ).fetchone()[0]
            for qid in qids
        ], "Expected unc_with_q to be surfaced"
        assert unc_without_q in [
            conn.execute(
                "SELECT worldview_uncertainty_id FROM hearth_questions WHERE question_id = ?;",
                (qid,),
            ).fetchone()[0]
            for qid in qids
        ], "Expected unc_without_q to be surfaced"

        q_with = _find_open_question_for_uncertainty(conn, unc_with_q)
        q_without = _find_open_question_for_uncertainty(conn, unc_without_q)
        assert q_with is not None and q_without is not None
        assert q_with["question_text"] == (
            "Is the creator still doing weekly check-ins, or have they quietly stopped?"
        ), "Expected possible_question to be used directly"
        assert "unclear why response time has slowed" in q_without["question_text"], (
            "Expected generated question to reference uncertainty_text"
        )
        assert q_with["source_type"] == "worldview_uncertainty"
        assert q_with["worldview_uncertainty_id"] == unc_with_q
        print(f"  possible_question used directly: {q_with['question_text'][:70]}...")
        print(f"  generated question: {q_without['question_text'][:70]}...")

        print("\nStep 11: uncertainty status advanced to 'question_surfaced'")
        open_after_surface = hearth_worldview.get_open_uncertainties(conn, subject_type="creator")
        open_ids = {r["id"] for r in open_after_surface}
        assert unc_with_q not in open_ids and unc_without_q not in open_ids, (
            "Surfaced uncertainties should no longer be open"
        )
        print("  Confirmed: both uncertainties left the open set after surfacing.")

        print("\nStep 12: surface_worldview_questions() — second run, no duplicates")
        qids_again = surface_worldview_questions(conn)
        assert qids_again == [], f"Expected no new questions on rerun, got {qids_again}"
        print("  Confirmed: rerun with no remaining open uncertainties produced no new questions.")

        print("\nStep 13: reopen an uncertainty whose question is still open — reuse, not duplicate")
        hearth_worldview.update_uncertainty(conn, unc_with_q, status="open")
        before_count = conn.execute(
            "SELECT COUNT(*) FROM hearth_questions WHERE worldview_uncertainty_id = ?;",
            (unc_with_q,),
        ).fetchone()[0]
        qids_reuse = surface_worldview_questions(conn)
        after_count = conn.execute(
            "SELECT COUNT(*) FROM hearth_questions WHERE worldview_uncertainty_id = ?;",
            (unc_with_q,),
        ).fetchone()[0]
        assert before_count == after_count == 1, (
            "Reopened uncertainty should reuse its existing question, not duplicate"
        )
        assert qids_reuse == [q_with["question_id"]], "Expected the same question_id to be reused"
        print(f"  Confirmed: reused question_id={qids_reuse[0]} instead of creating a duplicate.")

        print("\nStep 14: resolved uncertainties stop resurfacing")
        unc_for_resolution = hearth_worldview.open_uncertainty(
            conn, uncertainty_text="SESSION 5 SMOKE TEST: will resolve before ever surfacing",
            priority="low", source_episode_id=_TEST_MARKER,
        )
        test_uncertainty_ids.append(unc_for_resolution)
        hearth_worldview.resolve_uncertainty(conn, unc_for_resolution)
        surface_worldview_questions(conn)
        linked = conn.execute(
            "SELECT COUNT(*) FROM hearth_questions WHERE worldview_uncertainty_id = ?;",
            (unc_for_resolution,),
        ).fetchone()[0]
        assert linked == 0, "Resolved-before-surfacing uncertainty should never produce a question"
        print("  Confirmed: an uncertainty resolved before surfacing never produces a question.")

        print("\nStep 15: feature flag disabled — behaves as before Session 5")
        unc_flag_test = hearth_worldview.open_uncertainty(
            conn, uncertainty_text="SESSION 5 SMOKE TEST: should not surface while flag disabled",
            priority="high", source_episode_id=_TEST_MARKER,
        )
        test_uncertainty_ids.append(unc_flag_test)
        HEARTH_WORLDVIEW_QUESTIONS_ENABLED = False
        try:
            qids_disabled = surface_worldview_questions(conn)
            assert qids_disabled == [], f"Expected [] while flag disabled, got {qids_disabled}"
            print("  Confirmed: surface_worldview_questions() is a no-op while disabled.")
        finally:
            HEARTH_WORLDVIEW_QUESTIONS_ENABLED = True

        print("\nStep 16: fallback when no open uncertainties remain")
        hearth_worldview.resolve_uncertainty(conn, unc_flag_test)
        qids_empty = surface_worldview_questions(conn)
        assert qids_empty == [], f"Expected [] with no open uncertainties, got {qids_empty}"
        print("  Confirmed: with worldview empty, surface_worldview_questions() is a no-op"
              " — legacy generation in hearth_soul.py is untouched by this module either way.")

    finally:
        print("\nStep 17: cleanup — removing all session_5_smoke_test rows")
        for unc_id in test_uncertainty_ids:
            conn.execute("DELETE FROM hearth_questions WHERE worldview_uncertainty_id = ?;", (unc_id,))
        conn.execute(
            "DELETE FROM hearth_worldview_uncertainties WHERE source_episode_id = ?;",
            (_TEST_MARKER,),
        )
        conn.commit()
        remaining_unc = conn.execute(
            "SELECT COUNT(*) FROM hearth_worldview_uncertainties WHERE source_episode_id = ?;",
            (_TEST_MARKER,),
        ).fetchone()[0]
        remaining_q = sum(
            conn.execute(
                "SELECT COUNT(*) FROM hearth_questions WHERE worldview_uncertainty_id = ?;",
                (unc_id,),
            ).fetchone()[0]
            for unc_id in test_uncertainty_ids
        )
        print(f"  Remaining test uncertainties: {remaining_unc}, remaining linked questions: {remaining_q}")

    conn.close()
    print("\nSmoke test complete.")
