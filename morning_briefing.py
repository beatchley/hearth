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
from datetime import datetime, timedelta, timezone

from google import genai
from dotenv import load_dotenv

import hearth_memory
import hearth_relationships
import hearth_context

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")


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


def query_battles_today(conn, today: datetime):
    today_str = today.strftime("%Y-%m-%d")
    sql = """
        SELECT id, creator_screenname, opponent_name, battle_date, battle_time,
               battle_format, opponent_id
        FROM battles
        WHERE battle_date = ?
        ORDER BY battle_time;
    """
    return safe_query(conn, "Battles scheduled today", sql, (today_str,))


def query_battles_missing_confirmation(conn):
    sql = """
        SELECT id, creator_screenname, opponent_name, battle_date, battle_time
        FROM battles
        WHERE opponent_id IS NULL OR opponent_id = ''
        ORDER BY battle_date, battle_time;
    """
    return safe_query(conn, "Battles with unlinked opponent", sql)


def query_unresponded_comments(conn, since: datetime):
    sql = """
        SELECT id, user_id, content, created_at
        FROM training_comments
        WHERE created_at >= ?
        ORDER BY created_at DESC;
    """
    return safe_query(conn, "Recent training comments (last 24 h)", sql, (since.isoformat(),))


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


# ---------------------------------------------------------------------------
# Data collection
# ---------------------------------------------------------------------------

def collect_data(conn):
    now = datetime.now(timezone.utc)
    today_midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    since_24h = now - timedelta(hours=24)

    results = {}
    queries = [
        lambda: query_new_users(conn, since_24h),
        lambda: query_battles_today(conn, today_midnight),
        lambda: query_battles_missing_confirmation(conn),
        lambda: query_unresponded_comments(conn, since_24h),
        lambda: query_users_on_probation(conn),
        lambda: query_missing_discord(conn),
    ]
    for q in queries:
        label, data = q()
        results[label] = data
    return results


# ---------------------------------------------------------------------------
# Issue detection — writes to Hearth memory, never to Pathway
# ---------------------------------------------------------------------------

def detect_and_record_issues(memory_conn, data):
    """Scan today's operational data and persist notable issues as episodes."""
    rows = data.get("Users on probation", [])
    if isinstance(rows, list):
        for row in rows:
            entity = hearth_memory.get_or_create_entity(memory_conn, row["id"])
            hearth_memory.create_episode(
                memory_conn, entity["id"], "probation",
                f"User {row['name']} ({row['email']}) is on probation.",
                severity="high",
            )

    rows = data.get("Users not yet added to Discord", [])
    if isinstance(rows, list):
        for row in rows:
            entity = hearth_memory.get_or_create_entity(memory_conn, row["id"])
            hearth_memory.create_episode(
                memory_conn, entity["id"], "missing_discord",
                f"User {row['name']} ({row['email']}) has not been added to Discord.",
                severity="medium",
            )

    rows = data.get("Battles with unlinked opponent", [])
    if isinstance(rows, list):
        for row in rows:
            hearth_memory.create_episode(
                memory_conn, None, "unlinked_battle",
                (
                    f"Battle on {row['battle_date']} at {row['battle_time']}"
                    f" (creator: {row['creator_screenname']},"
                    f" opponent: {row['opponent_name']}) has no linked opponent account."
                ),
                severity="medium",
                reference_key=f"battle_{row['id']}",
            )


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

        detect_and_record_issues(memory_conn, data)
        hearth_memory.process_all_entities(memory_conn)
        open_episodes = hearth_memory.get_open_episodes(memory_conn)
        awareness = hearth_context.build_context(data, open_episodes, memory_conn)
        return generate_hearth_message(awareness, gemini_client=gemini_client)
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

        # Step 6: Record issues into Hearth's memory
        print("Updating Hearth memory...")
        detect_and_record_issues(memory_conn, data)

        # Step 7: Update learned observations for all entities
        hearth_memory.process_all_entities(memory_conn)

        # Step 8: Load all open episodes from Hearth's memory
        open_episodes = hearth_memory.get_open_episodes(memory_conn)
        print(f"  {len(open_episodes)} open episode(s) in memory.")

        # Step 9: Build Hearth's awareness context
        print("Building Hearth awareness context...")
        awareness = hearth_context.build_context(data, open_episodes, memory_conn)

        # Step 10: Generate the Hearth message via Gemini
        gemini_client = genai.Client(api_key=GEMINI_API_KEY)
        print("Generating Hearth message...\n")
        message = generate_hearth_message(awareness, gemini_client=gemini_client)

        # Step 11: Print the result
        print("=" * 60)
        print(message)
        print("=" * 60)

    finally:
        memory_conn.close()


if __name__ == "__main__":
    main()
