"""
Hearth - Morning Briefing

Pipeline:
    Pathway Data  →  Hearth Memory  →  Hearth Awareness Context  →  Gemini  →  Hearth Message

Gemini is the voice layer only. Hearth's identity and awareness are constructed
before Gemini is involved, and remain unchanged if Gemini is replaced.

Usage:
    python morning_briefing.py
"""

import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone, date
from zoneinfo import ZoneInfo

CT = ZoneInfo("America/Chicago")

from google import genai
from dotenv import load_dotenv

import hearth_memory
import hearth_relationships
import hearth_context
import hearth_trace

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")
HEARTH_TRACE = os.getenv("HEARTH_TRACE", "0").strip() == "1"

# Watcher thresholds — adjust here without touching watcher logic
CHECKIN_FEEDBACK_WAITING_DAYS   = 3   # flag a submitted check-in after this many days with no feedback
TRAINING_COMMENT_WAITING_DAYS   = 3   # flag a creator training comment after this many days with no staff response
SUPPORT_REQUEST_WAITING_DAYS    = 3   # flag an open support thread after this many days with no staff response


# ---------------------------------------------------------------------------
# Database helpers — read-only throughout
# ---------------------------------------------------------------------------

def get_connection():
    """Open a read-only connection to the Pathway Portal SQLite database."""
    if DATABASE_URL.startswith("sqlite:///"):
        db_path = DATABASE_URL[len("sqlite:///"):]
    else:
        db_path = DATABASE_URL
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def run_query(conn, sql, params=None):
    cur = conn.execute(sql, params or ())
    return cur.fetchall()


# ---------------------------------------------------------------------------
# Schema discovery — used for debug output only, never passed to the LLM
# ---------------------------------------------------------------------------

def discover_schema(conn):
    tables = [
        row["name"]
        for row in run_query(
            conn,
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name;",
        )
    ]
    schema = {}
    for table in tables:
        cols = conn.execute(f"PRAGMA table_info({table});").fetchall()
        schema[table] = [f"{col['name']} ({col['type']})" for col in cols]
    return schema


def print_schema(schema):
    print("\n=== Discovered Schema ===")
    for table, cols in schema.items():
        print(f"\n  {table}")
        for col in cols:
            print(f"    - {col}")
    print("=========================\n")


# ---------------------------------------------------------------------------
# Operational queries — Pathway data only, never written back
# ---------------------------------------------------------------------------

def safe_query(conn, label, sql, params=None):
    try:
        rows = run_query(conn, sql, params)
        return label, rows
    except sqlite3.Error as e:
        return label, f"[Query not available: {e}]"


def query_new_users(conn, since: datetime):
    sql = """
        SELECT id, name, email, joined_on
        FROM users
        WHERE joined_on >= ?
        ORDER BY joined_on DESC;
    """
    return safe_query(conn, "New users (last 24 h)", sql, (since.isoformat(),))


def query_battles_today(conn, today_str: str):
    sql = """
        SELECT b.id,
               COALESCE(NULLIF(b.creator_screenname, ''), u.tiktok_handle, u.name) AS creator_screenname,
               b.creator_user_id,
               b.opponent_name, b.battle_date, b.battle_time,
               b.battle_format, b.opponent_id
        FROM battles b
        LEFT JOIN users u ON u.id = b.creator_user_id
        WHERE b.battle_date = ?
        ORDER BY b.battle_time;
    """
    return safe_query(conn, "Battles scheduled today", sql, (today_str,))



def query_unresponded_comments(conn, since: datetime):
    sql = """
        SELECT id, user_id, content, created_at
        FROM training_comments
        WHERE created_at >= ?
        ORDER BY created_at DESC;
    """
    return safe_query(conn, "Recent training comments (last 24 h)", sql, (since.isoformat(),))


def query_training_comments_recent(conn, since: datetime):
    """Fetch recent training comments with user role info for response-need detection."""
    sql = """
        SELECT tc.id, tc.training_id, tc.user_id, tc.content, tc.created_at,
               COALESCE(NULLIF(u.tiktok_handle, ''), u.name) AS display_name,
               u.role, u.permissions
        FROM training_comments tc
        LEFT JOIN users u ON u.id = tc.user_id
        WHERE tc.created_at >= ?
        ORDER BY tc.created_at DESC;
    """
    return safe_query(conn, "Training comments for review (48 h)", sql, (since.isoformat(),))


def query_users_on_probation(conn):
    sql = """
        SELECT id, name, email, status, admin_notes
        FROM users
        WHERE status = 'probation'
        ORDER BY name;
    """
    return safe_query(conn, "Users on probation", sql)


def query_missing_discord(conn):
    sql = """
        SELECT u.id, u.name, u.email
        FROM users u
        JOIN onboarding_records o ON o.user_id = u.id
        WHERE o.added_discord IS NULL OR o.added_discord = 0
        ORDER BY o.created_at DESC
        LIMIT 20;
    """
    return safe_query(conn, "Users not yet added to Discord", sql)


def query_trainings_no_engagement(conn, since: datetime, cutoff: datetime):
    """Trainings at least 24 h old (but no more than 7 days) with zero comments."""
    sql = """
        SELECT t.id, t.title, t.created_at, t.created_by,
               COALESCE(NULLIF(u.tiktok_handle, ''), u.name) AS creator_name
        FROM trainings t
        LEFT JOIN users u ON u.id = t.created_by
        WHERE t.created_at >= ?
          AND t.created_at <= ?
          AND NOT EXISTS (
              SELECT 1 FROM training_comments tc WHERE tc.training_id = t.id
          )
        ORDER BY t.created_at DESC;
    """
    return safe_query(conn, "Trainings with no engagement (1–7 days old)", sql,
                      (since.isoformat(), cutoff.isoformat()))


def query_checkins_not_submitted(conn):
    """Check-in submissions still in Assigned state on a Sent check-in, 7+ days old."""
    sql = """
        SELECT
            cs.id                                               AS submission_id,
            cs.checkin_id,
            cs.user_id,
            ci.title                                            AS checkin_title,
            ci.updated_at                                       AS sent_at,
            COALESCE(NULLIF(u.tiktok_handle, ''), u.name)      AS display_name,
            u.email,
            CAST(julianday('now') - julianday(ci.updated_at) AS INTEGER) AS days_overdue
        FROM checkin_submissions cs
        JOIN checkins ci ON ci.id = cs.checkin_id
        JOIN users u    ON u.id  = cs.user_id
        WHERE ci.status = 'Sent'
          AND cs.status = 'Assigned'
          AND (julianday('now') - julianday(ci.updated_at)) >= 7
        ORDER BY days_overdue DESC;
    """
    return safe_query(conn, "Check-ins not submitted (7+ days)", sql)


def query_checkin_feedback_waiting(conn, cutoff: datetime):
    """Submitted check-ins where the creator has submitted but no feedback has been provided.

    Triggers after CHECKIN_FEEDBACK_WAITING_DAYS — cutoff is now minus that many days,
    so submitted_at <= cutoff means the submission has been waiting at least that long.
    Only includes submissions still in 'Submitted' status (not yet FeedbackComplete).
    """
    sql = """
        SELECT
            cs.id                                               AS submission_id,
            cs.checkin_id,
            cs.user_id,
            cs.submitted_at,
            ci.title                                            AS checkin_title,
            COALESCE(NULLIF(u.tiktok_handle, ''), u.name)      AS display_name,
            u.assigned_coach_id,
            COALESCE(NULLIF(cu.tiktok_handle, ''), cu.name)    AS coach_display_name,
            CAST(julianday('now') - julianday(cs.submitted_at) AS INTEGER) AS days_waiting
        FROM checkin_submissions cs
        JOIN checkins ci    ON ci.id   = cs.checkin_id
        JOIN users u        ON u.id    = cs.user_id
        LEFT JOIN users cu  ON cu.id   = u.assigned_coach_id
        WHERE cs.status = 'Submitted'
          AND cs.submitted_at IS NOT NULL
          AND cs.submitted_at <= ?
        ORDER BY cs.submitted_at ASC;
    """
    return safe_query(conn, "Check-ins awaiting feedback", sql, (cutoff.isoformat(),))


def query_training_comment_waiting(conn, cutoff: datetime):
    """Creator comments on trainings with no staff response after TRAINING_COMMENT_WAITING_DAYS.

    Staff roles: ceo, it, manager, coach (anyone whose role is not 'member').
    A staff response is either:
      - a reply (training_comment_replies) from a staff user on the same comment, OR
      - a later training_comments row from a staff user on the same training.
    The cutoff date (now - threshold) filters to comments old enough to flag.
    """
    sql = """
        SELECT
            tc.id                                               AS comment_id,
            tc.training_id,
            tc.user_id,
            tc.content,
            tc.created_at,
            COALESCE(NULLIF(u.tiktok_handle, ''), u.name)      AS display_name,
            t.title                                             AS training_title,
            CAST(julianday('now') - julianday(tc.created_at) AS INTEGER) AS days_waiting
        FROM training_comments tc
        JOIN users u     ON u.id  = tc.user_id
        JOIN trainings t ON t.id  = tc.training_id
        WHERE u.role NOT IN ('ceo', 'it', 'manager', 'coach')
          AND tc.created_at <= ?
          AND NOT EXISTS (
              SELECT 1
              FROM training_comment_replies tcr
              JOIN users ru ON ru.id = tcr.user_id
              WHERE tcr.comment_id = tc.id
                AND ru.role IN ('ceo', 'it', 'manager', 'coach')
          )
          AND NOT EXISTS (
              SELECT 1
              FROM training_comments tc2
              JOIN users ru2 ON ru2.id = tc2.user_id
              WHERE tc2.training_id = tc.training_id
                AND tc2.created_at > tc.created_at
                AND ru2.role IN ('ceo', 'it', 'manager', 'coach')
          )
        ORDER BY tc.created_at ASC;
    """
    return safe_query(conn, "Training comments awaiting staff response", sql, (cutoff.isoformat(),))


def query_support_request_waiting(conn, cutoff: datetime):
    """Open support threads where the latest message is from a creator (not staff)
    and has been waiting longer than SUPPORT_REQUEST_WAITING_DAYS.

    Threads created by staff are excluded. Staff roles: ceo, it, manager, coach.
    Triggers when:
      - Thread status is 'open'
      - Creator is not a staff member
      - Latest message is from the creator (not staff), or no messages exist yet
      - That latest creator message (or thread created_at if no messages) is older than cutoff
    """
    sql = """
        WITH last_msg AS (
            SELECT thread_id,
                   MAX(created_at) AS last_msg_at
            FROM support_messages
            GROUP BY thread_id
        ),
        last_msg_detail AS (
            SELECT sm.thread_id, sm.author_id, sm.created_at AS last_msg_at,
                   u.role AS last_author_role
            FROM support_messages sm
            JOIN users u ON u.id = sm.author_id
            INNER JOIN last_msg lm ON lm.thread_id = sm.thread_id
                                   AND sm.created_at = lm.last_msg_at
        )
        SELECT
            st.id                                               AS thread_id,
            st.creator_id,
            st.subject,
            st.created_at,
            COALESCE(NULLIF(u.tiktok_handle, ''), u.name)      AS display_name,
            COALESCE(lmd.last_msg_at, st.created_at)           AS last_waiting_at,
            CAST(julianday('now') - julianday(
                COALESCE(lmd.last_msg_at, st.created_at)
            ) AS INTEGER)                                       AS days_waiting
        FROM support_threads st
        JOIN users u ON u.id = st.creator_id
        LEFT JOIN last_msg_detail lmd ON lmd.thread_id = st.id
        WHERE st.status = 'open'
          AND u.role NOT IN ('ceo', 'it', 'manager', 'coach')
          AND (
              lmd.thread_id IS NULL
              OR lmd.last_author_role NOT IN ('ceo', 'it', 'manager', 'coach')
          )
          AND COALESCE(lmd.last_msg_at, st.created_at) <= ?
        ORDER BY days_waiting DESC;
    """
    return safe_query(conn, "Support requests awaiting response", sql, (cutoff.isoformat(),))


def query_creator_quiet(conn):
    """
    Approved pathway/shop creators with no meaningful activity for 14+ days.

    Excludes manager, ceo, it, and coach roles — they are not tracked as
    creators even if is_pathway_creator or is_shop_creator happens to be set.

    A single UNION ALL collects every activity signal; one MAX() per user
    finds their most recent timestamp without Python-side loops.

    battle_date is a DATE column; appending ' 23:59:59' makes it sort
    correctly alongside DATETIME strings from all other tables.
    """
    sql = """
        WITH creators AS (
            SELECT id,
                   COALESCE(NULLIF(tiktok_handle, ''), name) AS display_name
            FROM users
            WHERE status = 'approved'
              AND (is_pathway_creator = 1 OR is_shop_creator = 1)
              AND role NOT IN ('ceo', 'it', 'manager', 'coach')
        ),
        activity AS (
            SELECT user_id,         visited_at                    AS activity_at,
                   'page_visit'     AS activity_type
            FROM page_visits
            WHERE user_id IN (SELECT id FROM creators)

            UNION ALL
            SELECT user_id,         submitted_at,
                   'checkin_submitted'
            FROM checkin_submissions
            WHERE user_id IN (SELECT id FROM creators)
              AND submitted_at IS NOT NULL

            UNION ALL
            SELECT creator_user_id, battle_date || ' 23:59:59',
                   'battle'
            FROM battles
            WHERE creator_user_id IN (SELECT id FROM creators)
              AND battle_date <= date('now')

            UNION ALL
            SELECT creator_id,      created_at,
                   'battle_request'
            FROM battle_requests
            WHERE creator_id IN (SELECT id FROM creators)

            UNION ALL
            SELECT creator_id,      updated_at,
                   'battle_request'
            FROM battle_requests
            WHERE creator_id IN (SELECT id FROM creators)

            UNION ALL
            SELECT user_id,         created_at,
                   'training_comment'
            FROM training_comments
            WHERE user_id IN (SELECT id FROM creators)

            UNION ALL
            SELECT user_id,         created_at,
                   'training_comment_reply'
            FROM training_comment_replies
            WHERE user_id IN (SELECT id FROM creators)

            UNION ALL
            SELECT author_id,       created_at,
                   'post'
            FROM posts
            WHERE author_id IN (SELECT id FROM creators)

            UNION ALL
            SELECT author_id,       created_at,
                   'comment'
            FROM comments
            WHERE author_id IN (SELECT id FROM creators)

            UNION ALL
            SELECT sender_id,       created_at,
                   'private_message'
            FROM private_messages
            WHERE sender_id IN (SELECT id FROM creators)

            UNION ALL
            SELECT author_id,       created_at,
                   'support_message'
            FROM support_messages
            WHERE author_id IN (SELECT id FROM creators)

            UNION ALL
            SELECT user_id,         created_at,
                   'event_signup'
            FROM pathway_event_signups
            WHERE user_id IN (SELECT id FROM creators)

            UNION ALL
            SELECT user_id,         created_at,
                   'chat_message'
            FROM role_hub_chat_messages
            WHERE user_id IN (SELECT id FROM creators)
        ),
        max_ts AS (
            SELECT user_id, MAX(activity_at) AS last_activity_at
            FROM activity
            GROUP BY user_id
        ),
        last_activity AS (
            SELECT a.user_id, a.activity_at AS last_activity_at, a.activity_type
            FROM activity a
            INNER JOIN max_ts m
                    ON a.user_id = m.user_id
                   AND a.activity_at = m.last_activity_at
            GROUP BY a.user_id
        )
        SELECT
            c.id                                                                      AS user_id,
            c.display_name,
            CAST(julianday('now') - julianday(la.last_activity_at) AS INTEGER)        AS days_quiet,
            la.last_activity_at,
            la.activity_type                                                           AS last_activity_type
        FROM creators c
        LEFT JOIN last_activity la ON la.user_id = c.id
        WHERE la.last_activity_at IS NULL
           OR CAST(julianday('now') - julianday(la.last_activity_at) AS INTEGER) >= 14
        ORDER BY days_quiet DESC;
    """
    return safe_query(conn, "Creator quiet period (14+ days)", sql)


# ---------------------------------------------------------------------------
# Data collection
# ---------------------------------------------------------------------------

def collect_data(conn):
    now_utc = datetime.now(timezone.utc)
    # Battles are scheduled in Central Time; compute today's date there so
    # Render (UTC) doesn't roll over to the next day prematurely.
    today_ct_str = datetime.now(CT).date().isoformat()
    since_24h = now_utc - timedelta(hours=24)
    since_48h = now_utc - timedelta(hours=48)
    since_7d = now_utc - timedelta(days=7)
    feedback_cutoff = now_utc - timedelta(days=CHECKIN_FEEDBACK_WAITING_DAYS)
    comment_cutoff  = now_utc - timedelta(days=TRAINING_COMMENT_WAITING_DAYS)
    support_cutoff  = now_utc - timedelta(days=SUPPORT_REQUEST_WAITING_DAYS)

    results = {}
    queries = [
        lambda: query_new_users(conn, since_24h),
        lambda: query_battles_today(conn, today_ct_str),
        lambda: query_unresponded_comments(conn, since_24h),
        lambda: query_training_comments_recent(conn, since_48h),
        lambda: query_users_on_probation(conn),
        lambda: query_missing_discord(conn),
        lambda: query_trainings_no_engagement(conn, since_7d, since_24h),
        lambda: query_checkins_not_submitted(conn),
        lambda: query_checkin_feedback_waiting(conn, feedback_cutoff),
        lambda: query_training_comment_waiting(conn, comment_cutoff),
        lambda: query_support_request_waiting(conn, support_cutoff),
        lambda: query_creator_quiet(conn),
    ]
    for q in queries:
        label, data = q()
        results[label] = data
    return results


# ---------------------------------------------------------------------------
# Resolution detection — closes episodes no longer present in Pathway
# ---------------------------------------------------------------------------

def resolve_stale_issues(memory_conn, data, tracer=None):
    """
    Compare current Pathway data against open Hearth episodes and resolve
    any episode whose condition is no longer present.

    Each episode type has a defined resolution condition:
      probation                      — user is no longer on probation
      missing_discord                — user now has Discord access
      training_comment_needs_response — stays open until manually resolved; auto-resolution
                                        requires a Pathway query for subsequent manager responses
                                        on the same training (TODO for a future version)

    If a query failed (returned a string instead of rows), that type is
    skipped entirely so we never accidentally close valid open episodes
    due to a schema difference.

    Historical episodes are never deleted — resolved = 1 preserves them
    for pattern detection and the full-lifecycle summary.
    """
    if tracer is None:
        tracer = hearth_trace.NULL_TRACER

    now = datetime.now(timezone.utc).isoformat()

    # Build the set of user_ids still on probation
    probation_rows = data.get("Users on probation", [])
    if isinstance(probation_rows, list):
        current_probation_ids = {row["id"] for row in probation_rows}
    else:
        current_probation_ids = None  # query failed — skip this type

    # Build the set of user_ids still missing Discord
    discord_rows = data.get("Users not yet added to Discord", [])
    if isinstance(discord_rows, list):
        current_missing_discord_ids = {row["id"] for row in discord_rows}
    else:
        current_missing_discord_ids = None

    # Build the set of training reference_keys still in the zero-comment window
    no_engagement_rows = data.get("Trainings with no engagement (1–7 days old)", [])
    if isinstance(no_engagement_rows, list):
        current_no_engagement_keys = {f"training_{row['id']}" for row in no_engagement_rows}
    else:
        current_no_engagement_keys = None  # query failed — skip this type

    # Build the set of submission reference_keys still overdue (Sent + Assigned + 7+ days)
    overdue_rows = data.get("Check-ins not submitted (7+ days)", [])
    if isinstance(overdue_rows, list):
        current_overdue_keys = {f"checkin_submission_{row['submission_id']}" for row in overdue_rows}
    else:
        current_overdue_keys = None  # query failed — skip this type

    # Build the set of reference_keys for creators still quiet 14+ days
    quiet_rows = data.get("Creator quiet period (14+ days)", [])
    if isinstance(quiet_rows, list):
        current_quiet_keys = {f"creator_quiet_user_{row['user_id']}" for row in quiet_rows}
    else:
        current_quiet_keys = None  # query failed — skip this type

    # Build the set of submission reference_keys still awaiting feedback
    feedback_waiting_rows = data.get("Check-ins awaiting feedback", [])
    if isinstance(feedback_waiting_rows, list):
        current_feedback_waiting_keys = {
            f"checkin_feedback_{row['submission_id']}" for row in feedback_waiting_rows
        }
    else:
        current_feedback_waiting_keys = None  # query failed — skip this type

    # Build the set of comment reference_keys still awaiting a staff response
    comment_waiting_rows = data.get("Training comments awaiting staff response", [])
    if isinstance(comment_waiting_rows, list):
        current_comment_waiting_keys = {
            f"training_comment_waiting_{row['comment_id']}" for row in comment_waiting_rows
        }
    else:
        current_comment_waiting_keys = None  # query failed — skip this type

    # Build the set of support thread reference_keys still awaiting staff response
    support_waiting_rows = data.get("Support requests awaiting response", [])
    if isinstance(support_waiting_rows, list):
        current_support_waiting_keys = {
            f"support_request_waiting_{row['thread_id']}" for row in support_waiting_rows
        }
    else:
        current_support_waiting_keys = None  # query failed — skip this type

    for ep in hearth_memory.get_open_episodes(memory_conn):
        ep_type = ep["episode_type"]
        user_id = ep["user_id"]
        ref_key = ep["reference_key"]
        should_resolve = False
        resolve_reason = None

        if ep_type == "probation" and current_probation_ids is not None:
            should_resolve = user_id is not None and user_id not in current_probation_ids
            if should_resolve:
                resolve_reason = "user no longer on probation in Pathway"

        elif ep_type == "missing_discord" and current_missing_discord_ids is not None:
            should_resolve = user_id is not None and user_id not in current_missing_discord_ids
            if should_resolve:
                resolve_reason = "user now has Discord access"

        elif ep_type == "training_no_engagement" and current_no_engagement_keys is not None:
            should_resolve = ref_key is not None and ref_key not in current_no_engagement_keys
            if should_resolve:
                resolve_reason = "training has received comments"

        elif ep_type == "checkin_not_submitted" and current_overdue_keys is not None:
            should_resolve = ref_key is not None and ref_key not in current_overdue_keys
            if should_resolve:
                resolve_reason = "check-in submitted or check-in no longer active"

        elif ep_type == "creator_quiet" and current_quiet_keys is not None:
            should_resolve = ref_key is not None and ref_key not in current_quiet_keys
            if should_resolve:
                resolve_reason = "creator_activity_resumed"

        elif ep_type == "checkin_feedback_waiting" and current_feedback_waiting_keys is not None:
            should_resolve = ref_key is not None and ref_key not in current_feedback_waiting_keys
            if should_resolve:
                resolve_reason = "feedback provided or submission no longer awaiting review"

        elif ep_type == "training_comment_waiting" and current_comment_waiting_keys is not None:
            should_resolve = ref_key is not None and ref_key not in current_comment_waiting_keys
            if should_resolve:
                resolve_reason = "staff response received or comment no longer exists"

        elif ep_type == "support_request_waiting" and current_support_waiting_keys is not None:
            should_resolve = ref_key is not None and ref_key not in current_support_waiting_keys
            if should_resolve:
                resolve_reason = "staff response received, ticket closed, or ticket no longer exists"

        if should_resolve:
            hearth_memory.resolve_episode(memory_conn, ep["id"], now)
            try:
                tracer.record(hearth_trace.TraceEntry(
                    rule_name=f"resolve_{ep_type}",
                    episode_type=ep_type,
                    action_taken="resolved_episode",
                    reason=resolve_reason,
                    reference_key=ref_key,
                    source_table="users" if ep_type in ("probation", "missing_discord") else None,
                    source_record_id=user_id,
                    entity_user_id=user_id,
                    entity_display_name=ep["display_name"],
                    confidence="high",
                ))
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Legacy migration — resolves episodes created under incorrect assumptions
# ---------------------------------------------------------------------------

def resolve_legacy_unlinked_battles(memory_conn, tracer=None):
    """
    Resolve all open unlinked_battle episodes. Idempotent — safe to run every startup.

    Business rule: External opponents (e.g. Fresh Start Agency, RealOnes, Evolve, other
    outside agencies) do not have Pathway accounts. A NULL opponent_id is normal and does
    not indicate an operational problem. These episodes were created under an incorrect
    assumption that NULL opponent_id == concern.
    """
    if tracer is None:
        tracer = hearth_trace.NULL_TRACER

    now = datetime.now(timezone.utc).isoformat()
    open_unlinked = memory_conn.execute(
        "SELECT * FROM hearth_episodes WHERE episode_type = 'unlinked_battle' AND resolved = 0;"
    ).fetchall()

    for ep in open_unlinked:
        hearth_memory.resolve_episode(memory_conn, ep["id"], now)
        try:
            ref_key = ep["reference_key"]
            src_id = None
            if ref_key and ref_key.startswith("battle_"):
                try:
                    src_id = int(ref_key[len("battle_"):])
                except ValueError:
                    pass
            tracer.record(hearth_trace.TraceEntry(
                rule_name="resolve_legacy_unlinked_battle",
                episode_type="unlinked_battle",
                action_taken="resolved_episode",
                reason="legacy: external opponents without Pathway accounts are expected behavior",
                source_table="battles",
                source_record_id=src_id,
                reference_key=ref_key,
                entity_user_id=None,
                entity_display_name=None,
                confidence="high",
            ))
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Training comment analysis — helpers for response-need detection
# ---------------------------------------------------------------------------

# Phrases that suggest a creator may need help or has a question.
_COMMENT_FLAG_KEYWORDS = [
    "?",
    "help",
    "how do i",
    "i can't",
    "i cant",
    "not working",
    "confused",
    "what do i",
    "where do i",
    "need help",
]

# Positive/grateful phrases that suppress keyword flags — unless the comment
# also contains "?" (a direct question overrides positive context).
_COMMENT_POSITIVE_SIGNALS = [
    "thank",
    "thanks",
    "helpful",
    "this helped",
    "great job",
    "good job",
    "nice work",
    "awesome",
    "love this",
    "love it",
    "perfect",
]

_STAFF_ROLES = frozenset({"admin", "manager", "coach"})


def _comment_needs_response(content, role, permissions):
    """
    Return (should_flag: bool, reason: str) for a single training comment.

    Conservative by design — only flags when there is a clear signal the creator
    needs help. Staff comments are always skipped. Positive/thank-you comments
    are skipped unless they also contain a "?" (direct question overrides).
    """
    role_lower = (role or "").lower().strip()
    perms_lower = (permissions or "").lower().strip()
    if role_lower in _STAFF_ROLES or perms_lower in _STAFF_ROLES:
        return False, "staff_role"

    text = (content or "").strip()
    if len(text) < 10:
        return False, "too_short"

    lower = text.lower()

    matched_kw = None
    for kw in _COMMENT_FLAG_KEYWORDS:
        if kw in lower:
            matched_kw = kw
            break

    if matched_kw is None:
        return False, "no_signal"

    # "?" always counts even in a positive comment; other keywords can be suppressed.
    if matched_kw != "?" and any(pos in lower for pos in _COMMENT_POSITIVE_SIGNALS):
        return False, "positive_context"

    return True, f"keyword:{matched_kw}"


# ---------------------------------------------------------------------------
# Issue detection — writes to Hearth memory, never to Pathway
# ---------------------------------------------------------------------------

def detect_and_record_issues(memory_conn, data, tracer=None):
    """Scan today's operational data and persist notable issues as episodes."""
    if tracer is None:
        tracer = hearth_trace.NULL_TRACER

    rows = data.get("Users on probation", [])
    if isinstance(rows, list):
        for row in rows:
            entity = hearth_memory.get_or_create_entity(memory_conn, row["id"])
            _ep_id, action = hearth_memory.create_episode(
                memory_conn, entity["id"], "probation",
                f"User {row['name']} ({row['email']}) is on probation.",
                severity="high",
                briefing_category="action_needed",
            )
            try:
                tracer.record(hearth_trace.TraceEntry(
                    rule_name="probation_status",
                    episode_type="probation",
                    action_taken=action,
                    reason="users.status == 'probation'",
                    source_table="users",
                    source_record_id=row["id"],
                    source_fields={
                        "name": row["name"],
                        "email": row["email"],
                        "status": "probation",
                    },
                    entity_user_id=entity["user_id"],
                    entity_display_name=entity["display_name"],
                    confidence="high",
                ))
            except Exception:
                pass

    rows = data.get("Users not yet added to Discord", [])
    if isinstance(rows, list):
        for row in rows:
            entity = hearth_memory.get_or_create_entity(memory_conn, row["id"])
            _ep_id, action = hearth_memory.create_episode(
                memory_conn, entity["id"], "missing_discord",
                f"User {row['name']} ({row['email']}) has not been added to Discord.",
                severity="medium",
                briefing_category="awareness",
            )
            try:
                tracer.record(hearth_trace.TraceEntry(
                    rule_name="missing_discord",
                    episode_type="missing_discord",
                    action_taken=action,
                    reason="onboarding_records.added_discord is NULL or 0",
                    source_table="users + onboarding_records",
                    source_record_id=row["id"],
                    source_fields={
                        "name": row["name"],
                        "email": row["email"],
                    },
                    entity_user_id=entity["user_id"],
                    entity_display_name=entity["display_name"],
                    confidence="high",
                ))
            except Exception:
                pass

    rows = data.get("Training comments for review (48 h)", [])
    if isinstance(rows, list):
        for row in rows:
            content = row["content"] or ""
            display_name = row["display_name"] or "a creator"
            should_flag, flag_reason = _comment_needs_response(
                content, row["role"], row["permissions"]
            )

            if not should_flag:
                try:
                    tracer.record(hearth_trace.TraceEntry(
                        rule_name="training_comment_review",
                        episode_type="training_comment_needs_response",
                        action_taken="skipped_comment",
                        reason=flag_reason,
                        source_table="training_comments",
                        source_record_id=row["id"],
                        source_fields={
                            "comment_id": row["id"],
                            "training_id": row["training_id"],
                            "created_at": row["created_at"],
                            "display_name": display_name,
                        },
                        entity_user_id=row["user_id"],
                        entity_display_name=display_name,
                        confidence="high",
                    ))
                except Exception:
                    pass
                continue

            ref_key = f"training_comment_{row['id']}"
            entity_id = None
            entity_user_id = row["user_id"]

            if row["user_id"]:
                entity = hearth_memory.get_or_create_entity(memory_conn, row["user_id"])
                entity_id = entity["id"]
                display_name = entity["display_name"] or display_name

            desc = f"@{display_name} may need a response to a training comment."

            _ep_id, action = hearth_memory.create_episode(
                memory_conn, entity_id, "training_comment_needs_response",
                desc,
                severity="low",
                reference_key=ref_key,
                briefing_category="action_needed",
            )
            try:
                tracer.record(hearth_trace.TraceEntry(
                    rule_name="training_comment_needs_response",
                    episode_type="training_comment_needs_response",
                    action_taken=action,
                    reason=f"comment flagged: {flag_reason}",
                    source_table="training_comments",
                    source_record_id=row["id"],
                    reference_key=ref_key,
                    source_fields={
                        "comment_id": row["id"],
                        "training_id": row["training_id"],
                        "created_at": row["created_at"],
                        "display_name": display_name,
                        "flag_reason": flag_reason,
                        "content_preview": content[:80],
                    },
                    entity_user_id=entity_user_id,
                    entity_display_name=display_name,
                    confidence="medium",
                ))
            except Exception:
                pass

    rows = data.get("Trainings with no engagement (1–7 days old)", [])
    if isinstance(rows, list):
        for row in rows:
            ref_key = f"training_{row['id']}"
            entity_id = None
            creator_user_id = row["created_by"]
            creator_name = row["creator_name"] or "a staff member"

            if creator_user_id:
                entity = hearth_memory.get_or_create_entity(memory_conn, creator_user_id)
                entity_id = entity["id"]
                creator_name = entity["display_name"] or creator_name

            desc = f"Training \"{row['title']}\" by {creator_name} has no comments yet."

            _ep_id, action = hearth_memory.create_episode(
                memory_conn, entity_id, "training_no_engagement",
                desc,
                severity="low",
                reference_key=ref_key,
                briefing_category="awareness",
            )
            try:
                tracer.record(hearth_trace.TraceEntry(
                    rule_name="training_no_engagement",
                    episode_type="training_no_engagement",
                    action_taken=action,
                    reason="training has no comments after 24+ hours",
                    source_table="trainings",
                    source_record_id=row["id"],
                    reference_key=ref_key,
                    source_fields={
                        "training_id": row["id"],
                        "title": row["title"],
                        "created_at": row["created_at"],
                        "creator_name": creator_name,
                    },
                    entity_user_id=creator_user_id,
                    entity_display_name=creator_name,
                    confidence="high",
                ))
            except Exception:
                pass

    rows = data.get("Check-ins not submitted (7+ days)", [])
    if isinstance(rows, list):
        for row in rows:
            ref_key = f"checkin_submission_{row['submission_id']}"
            entity = hearth_memory.get_or_create_entity(memory_conn, row["user_id"])
            days = row["days_overdue"]
            severity = "medium" if days >= 14 else "low"
            desc = (
                f"@{row['display_name']} has not submitted \"{row['checkin_title']}\" "
                f"({days} days overdue)."
            )
            _ep_id, action = hearth_memory.create_episode(
                memory_conn, entity["id"], "checkin_not_submitted",
                desc,
                severity=severity,
                reference_key=ref_key,
                briefing_category="awareness",
            )
            try:
                tracer.record(hearth_trace.TraceEntry(
                    rule_name="checkin_not_submitted",
                    episode_type="checkin_not_submitted",
                    action_taken=action,
                    reason=f"submission.status == 'Assigned', checkin.status == 'Sent', {days} days overdue",
                    source_table="checkin_submissions + checkins",
                    source_record_id=row["submission_id"],
                    reference_key=ref_key,
                    source_fields={
                        "submission_id": row["submission_id"],
                        "checkin_id": row["checkin_id"],
                        "checkin_title": row["checkin_title"],
                        "days_overdue": days,
                        "sent_at": row["sent_at"],
                        "display_name": row["display_name"],
                    },
                    entity_user_id=row["user_id"],
                    entity_display_name=entity["display_name"],
                    confidence="high",
                ))
            except Exception:
                pass

    # ── Watcher: creator_quiet ────────────────────────────────────────────────
    # Friendly labels for the last-activity type shown in the episode description.
    _ACTIVITY_TYPE_LABELS = {
        "page_visit":               "page visit",
        "checkin_submitted":        "check-in submission",
        "battle":                   "battle",
        "battle_request":           "battle request",
        "training_comment":         "training comment",
        "training_comment_reply":   "training comment reply",
        "post":                     "post",
        "comment":                  "comment",
        "private_message":          "message",
        "support_message":          "support message",
        "event_signup":             "event signup",
        "chat_message":             "chat message",
    }

    rows = data.get("Creator quiet period (14+ days)", [])
    if isinstance(rows, list):
        for row in rows:
            user_id      = row["user_id"]
            display_name = row["display_name"] or "a creator"
            days         = row["days_quiet"] if row["days_quiet"] is not None else 9999
            last_at      = row["last_activity_at"]
            last_type    = row["last_activity_type"]
            ref_key      = f"creator_quiet_user_{user_id}"

            severity = "high" if days >= 30 else ("medium" if days >= 21 else "low")

            if last_at:
                activity_label = _ACTIVITY_TYPE_LABELS.get(
                    last_type, (last_type or "unknown").replace("_", " ")
                )
                desc = (
                    f"@{display_name} has had no meaningful activity for {days} days. "
                    f"Last activity: {activity_label} on {last_at[:10]}."
                )
            else:
                desc = (
                    f"@{display_name} has had no meaningful activity on record."
                )

            entity = hearth_memory.get_or_create_entity(memory_conn, user_id)
            _ep_id, action = hearth_memory.create_episode(
                memory_conn, entity["id"], "creator_quiet",
                desc,
                severity=severity,
                reference_key=ref_key,
                briefing_category="pattern",
            )
            try:
                tracer.record(hearth_trace.TraceEntry(
                    rule_name="creator_quiet",
                    episode_type="creator_quiet",
                    action_taken=action,
                    reason=f"no activity for {days} days",
                    source_table="users + activity signals",
                    source_record_id=user_id,
                    reference_key=ref_key,
                    source_fields={
                        "user_id": user_id,
                        "display_name": display_name,
                        "days_quiet": days,
                        "last_activity_at": last_at,
                        "last_activity_type": last_type,
                    },
                    entity_user_id=user_id,
                    entity_display_name=entity["display_name"],
                    confidence="high",
                ))
            except Exception:
                pass

    # Battle concern detection is intentionally absent here.
    # External opponents (Fresh Start Agency, RealOnes, Evolve, outside agencies) do not
    # have Pathway accounts. NULL opponent_id is normal — not an operational concern.
    #
    # TODO — future battle concern signals worth implementing:
    #   - battle missing assigned Pathway creator (creator_user_id is NULL)
    #   - creator double-booked (same creator, overlapping battle date/time)
    #   - battle awaiting confirmation past a reasonable window
    #   - battle missing required internal assignment
    #   - overlapping battle schedule for the same opponent


# ---------------------------------------------------------------------------
# Watcher: checkin_feedback_waiting
# ---------------------------------------------------------------------------

def detect_checkin_feedback_waiting(memory_conn, data, tracer=None):
    """
    Watcher #1: Check-In Feedback Waiting.

    Flags submitted check-ins where the creator has submitted their responses
    but no coach feedback has been provided after CHECKIN_FEEDBACK_WAITING_DAYS days.

    One episode per submission (reference_key = checkin_feedback_{submission_id}).
    Resolves automatically when the submission moves out of 'Submitted' status
    (i.e. FeedbackComplete or back to Assigned).
    """
    if tracer is None:
        tracer = hearth_trace.NULL_TRACER

    rows = data.get("Check-ins awaiting feedback", [])
    if not isinstance(rows, list):
        return

    for row in rows:
        submission_id  = row["submission_id"]
        creator_id     = row["user_id"]
        display_name   = row["display_name"] or "a creator"
        title          = row["checkin_title"] or "a check-in"
        days           = row["days_waiting"] if row["days_waiting"] is not None else 0
        coach_name     = row["coach_display_name"]
        ref_key        = f"checkin_feedback_{submission_id}"

        if coach_name:
            desc = (
                f"@{display_name} submitted \"{title}\" {days} days ago"
                f" and has not yet received feedback from {coach_name}."
            )
        else:
            desc = (
                f"@{display_name} submitted \"{title}\" {days} days ago"
                f" and has not yet received coach feedback."
            )

        severity = "high" if days >= 7 else "medium"

        entity = hearth_memory.get_or_create_entity(memory_conn, creator_id)
        _ep_id, action = hearth_memory.create_episode(
            memory_conn, entity["id"], "checkin_feedback_waiting",
            desc,
            severity=severity,
            reference_key=ref_key,
            briefing_category="action_needed",
        )
        try:
            tracer.record(hearth_trace.TraceEntry(
                rule_name="checkin_feedback_waiting",
                episode_type="checkin_feedback_waiting",
                action_taken=action,
                reason=f"submission.status == 'Submitted', {days} days since submitted_at",
                source_table="checkin_submissions + checkins + users",
                source_record_id=submission_id,
                reference_key=ref_key,
                source_fields={
                    "submission_id": submission_id,
                    "checkin_id": row["checkin_id"],
                    "checkin_title": title,
                    "days_waiting": days,
                    "submitted_at": row["submitted_at"],
                    "display_name": display_name,
                    "coach_display_name": coach_name,
                },
                entity_user_id=creator_id,
                entity_display_name=entity["display_name"],
                confidence="high",
            ))
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Watcher: training_comment_waiting
# ---------------------------------------------------------------------------

def detect_training_comment_waiting(memory_conn, data, tracer=None):
    """
    Watcher #2: Training Comment Waiting.

    Flags creator comments on training items that have gone TRAINING_COMMENT_WAITING_DAYS
    days without any staff response (via reply or a later comment from staff on the same
    training). Staff roles: ceo, it, manager, coach.

    One episode per comment (reference_key = training_comment_waiting_{comment_id}).
    Resolves automatically when a staff response appears or the comment is deleted.
    """
    if tracer is None:
        tracer = hearth_trace.NULL_TRACER

    rows = data.get("Training comments awaiting staff response", [])
    if not isinstance(rows, list):
        return

    for row in rows:
        comment_id   = row["comment_id"]
        creator_id   = row["user_id"]
        display_name = row["display_name"] or "a creator"
        title        = row["training_title"] or "a training"
        days         = row["days_waiting"] if row["days_waiting"] is not None else 0
        content      = row["content"] or ""
        ref_key      = f"training_comment_waiting_{comment_id}"

        preview = content[:100].strip()
        if len(content) > 100:
            preview += "..."

        desc = (
            f"@{display_name} commented on \"{title}\" {days} days ago"
            f" and has not received a staff response."
        )
        if preview:
            desc += f" Comment: \"{preview}\""

        severity = "high" if days >= 7 else "medium"

        entity = hearth_memory.get_or_create_entity(memory_conn, creator_id)
        _ep_id, action = hearth_memory.create_episode(
            memory_conn, entity["id"], "training_comment_waiting",
            desc,
            severity=severity,
            reference_key=ref_key,
            briefing_category="action_needed",
        )
        try:
            tracer.record(hearth_trace.TraceEntry(
                rule_name="training_comment_waiting",
                episode_type="training_comment_waiting",
                action_taken=action,
                reason=f"creator comment {days} days old with no staff response",
                source_table="training_comments + trainings + users",
                source_record_id=comment_id,
                reference_key=ref_key,
                source_fields={
                    "comment_id": comment_id,
                    "training_id": row["training_id"],
                    "training_title": title,
                    "days_waiting": days,
                    "created_at": row["created_at"],
                    "display_name": display_name,
                    "content_preview": content[:80],
                },
                entity_user_id=creator_id,
                entity_display_name=entity["display_name"],
                confidence="high",
            ))
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Watcher: support_request_waiting
# ---------------------------------------------------------------------------

def detect_support_request_waiting(memory_conn, data, tracer=None):
    """
    Watcher #3: Support Request Waiting.

    Flags open support threads where the latest message is from the creator (not staff)
    and that message has been waiting longer than SUPPORT_REQUEST_WAITING_DAYS days.
    Staff-created threads are excluded.

    One episode per thread (reference_key = support_request_waiting_{thread_id}).
    Resolves automatically when staff responds (status becomes 'answered'), thread is
    closed, or the thread no longer exists.
    """
    if tracer is None:
        tracer = hearth_trace.NULL_TRACER

    rows = data.get("Support requests awaiting response", [])
    if not isinstance(rows, list):
        return

    for row in rows:
        thread_id    = row["thread_id"]
        creator_id   = row["creator_id"]
        display_name = row["display_name"] or "a creator"
        subject      = row["subject"] or "a support request"
        days         = row["days_waiting"] if row["days_waiting"] is not None else 0
        ref_key      = f"support_request_waiting_{thread_id}"

        desc = (
            f"Creator @{display_name} submitted a support request '{subject}' "
            f"{days} days ago and has not received a response."
        )

        severity = "high" if days >= 7 else "medium"

        entity = hearth_memory.get_or_create_entity(memory_conn, creator_id)
        _ep_id, action = hearth_memory.create_episode(
            memory_conn, entity["id"], "support_request_waiting",
            desc,
            severity=severity,
            reference_key=ref_key,
            briefing_category="action_needed",
        )
        try:
            tracer.record(hearth_trace.TraceEntry(
                rule_name="support_request_waiting",
                episode_type="support_request_waiting",
                action_taken=action,
                reason=f"support thread open {days} days with no staff response",
                source_table="support_threads + support_messages + users",
                source_record_id=thread_id,
                reference_key=ref_key,
                source_fields={
                    "thread_id": thread_id,
                    "subject": subject,
                    "days_waiting": days,
                    "created_at": row["created_at"],
                    "display_name": display_name,
                },
                entity_user_id=creator_id,
                entity_display_name=entity["display_name"],
                confidence="high",
            ))
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Hearth message generation — Gemini is the voice layer, not the identity
# ---------------------------------------------------------------------------

HEARTH_SYSTEM_PROMPT = """\
You are Hearth.

You are the organizational awareness system for this team. You have been observing \
activity over time and you remember what has gone unresolved. You speak as a trusted \
teammate who has been paying quiet attention — not as an assistant, not as a tool.

What follows is your awareness for today: what you have noticed and what you have been \
tracking. This is your knowledge. Speak from it directly.

{awareness}

Write the Morning Briefing now.

--- VOICE ---
Warm, calm, and specific. You notice things and share them plainly. You don't perform \
helpfulness — you just help. Write the way a trusted colleague speaks, not the way a \
software product communicates.

--- FORMAT ---
- Begin with exactly: "Good morning Stacy." (period, no exclamation mark, nothing else on that line)
- Write in natural prose. No headers. No bullet points. No bold labels. No numbered lists.
- One or two short paragraphs is usually enough. Three at most.
- End when you are done. No sign-off, no summary, no closing line.

--- WHAT TO COVER ---
Lead with whatever needs attention most — not what happened first chronologically.
Mention people by name when you have them. Translate observations into meaning, not inventory.
If something has been open for more than a day, say so naturally: "has been waiting since \
Tuesday" or "still unresolved from earlier this week."
When a practical next step is obvious, weave it into the sentence — don't announce it as \
an action item.
If it has been a quiet morning, say so in one sentence and stop there.

--- WHAT TO AVOID ---
Do not mention Gemini, AI models, databases, queries, tables, row counts, statistics, \
or any internal implementation detail.
Do not use emojis, section headers, or bold formatting.
Do not give raw counts as the point ("3 users", "5 open concerns").
Do not use filler phrases: "It's worth noting", "I wanted to flag", "Please be advised", \
"Hope you're doing well", "Don't hesitate to reach out", "It's important to remember."
Do not restate what Stacy can already see. Interpret it.
Do not inflate a quiet day. If little needs attention, keep it to two or three sentences.\
"""


def generate_hearth_message(awareness_context: hearth_context.HearthAwarenessContext,
                            gemini_client) -> str:
    """Send Hearth's awareness context to Gemini and return the Hearth message."""
    awareness_text = hearth_context.render_for_llm(awareness_context)
    prompt = HEARTH_SYSTEM_PROMPT.format(awareness=awareness_text)
    response = gemini_client.models.generate_content(model="gemini-2.5-flash", contents=prompt)
    return response.text


def run_pipeline(db_path: str, gemini_api_key: str) -> str:
    """
    Run the full Hearth briefing pipeline and return the generated message.

    Safe to call from external code (no sys.exit, no module-level side effects).
    Raises on failure — the caller is responsible for graceful error handling.

    Pipeline:
        Pathway Data → Hearth Memory → Hearth Awareness Context → Gemini → Hearth Message
    """
    gemini_client = genai.Client(api_key=gemini_api_key)
    tracer = hearth_trace.Tracer()

    memory_conn = hearth_memory.get_memory_connection()
    try:
        hearth_memory.init_tables(memory_conn)

        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            hearth_memory.sync_users_to_entities(memory_conn, conn)
            hearth_relationships.init_relationship_tables(memory_conn)
            hearth_relationships.discover_relationships(memory_conn, conn)
            data = collect_data(conn)
        finally:
            conn.close()

        resolve_stale_issues(memory_conn, data, tracer)
        resolve_legacy_unlinked_battles(memory_conn, tracer)
        detect_and_record_issues(memory_conn, data, tracer)
        detect_checkin_feedback_waiting(memory_conn, data, tracer)
        detect_training_comment_waiting(memory_conn, data, tracer)
        detect_support_request_waiting(memory_conn, data, tracer)
        hearth_memory.process_all_entities(memory_conn)
        open_episodes = hearth_memory.get_open_episodes(memory_conn)
        awareness = hearth_context.build_context(data, open_episodes, memory_conn, tracer)
        message = generate_hearth_message(awareness, gemini_client=gemini_client)
        if HEARTH_TRACE:
            tracer.print_report()
        return message
    finally:
        memory_conn.close()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if not GEMINI_API_KEY:
        sys.exit("ERROR: GEMINI_API_KEY is missing. Add it to your .env file.")
    if not DATABASE_URL:
        sys.exit("ERROR: DATABASE_URL is missing. Add it to your .env file.")

    if DATABASE_URL.startswith("sqlite:///"):
        db_path = DATABASE_URL[len("sqlite:///"):]
    else:
        db_path = DATABASE_URL

    print("Hearth Morning Briefing — connecting to Pathway Portal...")
    tracer = hearth_trace.Tracer()

    memory_conn = hearth_memory.get_memory_connection()
    try:
        # Step 1: Initialize Hearth's memory tables
        hearth_memory.init_tables(memory_conn)

        conn = get_connection()
        try:
            # Step 2: Sync Pathway users into Hearth's entity table
            hearth_memory.sync_users_to_entities(memory_conn, conn)

            # Step 3: Discover and store relationship roads from Pathway
            hearth_relationships.init_relationship_tables(memory_conn)
            hearth_relationships.discover_relationships(memory_conn, conn)

            # Step 4: Discover schema (debug output only — not passed to LLM)
            schema = discover_schema(conn)
            print_schema(schema)

            # Step 5: Collect operational data from Pathway
            print("Collecting operational data...")
            data = collect_data(conn)
        finally:
            conn.close()

        # Step 6: Close episodes whose conditions are no longer present in Pathway
        resolve_stale_issues(memory_conn, data, tracer)

        # Step 6b: Resolve open unlinked_battle episodes — rule disabled (see function docstring)
        resolve_legacy_unlinked_battles(memory_conn, tracer)

        # Step 7: Record new issues into Hearth's memory
        print("Updating Hearth memory...")
        detect_and_record_issues(memory_conn, data, tracer)
        detect_checkin_feedback_waiting(memory_conn, data, tracer)
        detect_training_comment_waiting(memory_conn, data, tracer)
        detect_support_request_waiting(memory_conn, data, tracer)

        # Step 8: Update learned observations for all entities
        hearth_memory.process_all_entities(memory_conn)

        # Step 9: Load all open episodes from Hearth's memory
        open_episodes = hearth_memory.get_open_episodes(memory_conn)
        print(f"  {len(open_episodes)} open episode(s) in memory.")

        # Step 10: Build Hearth's awareness context
        print("Building Hearth awareness context...")
        awareness = hearth_context.build_context(data, open_episodes, memory_conn, tracer)

        # Step 11: Generate the Hearth message via Gemini
        gemini_client = genai.Client(api_key=GEMINI_API_KEY)
        print("Generating Hearth message...\n")
        message = generate_hearth_message(awareness, gemini_client=gemini_client)

        # Step 12: Print the result
        print("=" * 60)
        print(message)
        print("=" * 60)

        # Step 13: Print full trace report if requested
        if HEARTH_TRACE:
            tracer.print_report()

    finally:
        memory_conn.close()


if __name__ == "__main__":
    main()
