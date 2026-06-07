"""
Hearth - Morning Briefing
Reads operational data from the Pathway Portal SQLite database,
summarizes it with Gemini, and prints a plain-English briefing for Stacy.

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

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

# Load .env so we never hardcode secrets
load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")

if not GEMINI_API_KEY:
    sys.exit("ERROR: GEMINI_API_KEY is missing. Add it to your .env file.")
if not DATABASE_URL:
    sys.exit("ERROR: DATABASE_URL is missing. Add it to your .env file.")

gemini = genai.Client(api_key=GEMINI_API_KEY)


# ---------------------------------------------------------------------------
# Database helpers — read-only throughout
# ---------------------------------------------------------------------------

def get_connection():
    """Open a read-only connection to the Pathway Portal SQLite database.

    DATABASE_URL should use the sqlite:/// scheme, e.g.:
        DATABASE_URL=sqlite:////absolute/path/to/app.db   (4 slashes for absolute path)
        DATABASE_URL=sqlite:///relative/path/to/app.db    (3 slashes for relative path)
    The 'mode=ro' URI flag prevents any accidental writes at the OS level.
    """
    # Strip the sqlite:/// scheme to get the bare file path, then re-wrap it
    # in SQLite's own URI format with read-only mode.
    # sqlite:////abs/path → /abs/path   (4 slashes: 3 for scheme + 1 for root)
    # sqlite:///rel/path  → rel/path
    if DATABASE_URL.startswith("sqlite:///"):
        db_path = DATABASE_URL[len("sqlite:///"):]
    else:
        db_path = DATABASE_URL
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    # Return rows as dict-like objects so we can access columns by name
    conn.row_factory = sqlite3.Row
    return conn


def run_query(conn, sql, params=None):
    """Execute a SELECT query and return all rows as a list of sqlite3.Row objects."""
    cur = conn.execute(sql, params or ())
    return cur.fetchall()


# ---------------------------------------------------------------------------
# Schema discovery
# Runs first so we can inspect what tables/columns actually exist before
# running operational queries. Useful while the schema is still being learned.
# ---------------------------------------------------------------------------

def discover_schema(conn):
    """Return a dict mapping table name -> list of 'column (type)' strings.

    SQLite exposes tables via sqlite_master and columns via PRAGMA table_info().
    """
    tables = [
        row["name"]
        for row in run_query(
            conn,
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name;",
        )
    ]

    schema = {}
    for table in tables:
        # PRAGMA table_info returns: cid, name, type, notnull, dflt_value, pk
        cols = conn.execute(f"PRAGMA table_info({table});").fetchall()
        schema[table] = [f"{col['name']} ({col['type']})" for col in cols]

    return schema


def print_schema(schema):
    """Print discovered tables and columns so we know what we're working with."""
    print("\n=== Discovered Schema ===")
    for table, cols in schema.items():
        print(f"\n  {table}")
        for col in cols:
            print(f"    - {col}")
    print("=========================\n")


# ---------------------------------------------------------------------------
# Operational queries
# Each function is self-contained. If a table/column doesn't exist yet,
# it catches the error and returns a note instead of crashing.
# ---------------------------------------------------------------------------

def safe_query(conn, label, sql, params=None):
    """Run a query and return (label, rows). Returns an error note on failure."""
    try:
        rows = run_query(conn, sql, params)
        return label, rows
    except sqlite3.Error as e:
        # The query failed (likely wrong table/column name) — return the error
        # as a string so the briefing can mention it without crashing.
        return label, f"[Query not available — schema may differ: {e}]"


def query_new_users(conn, since: datetime):
    """Users who joined in the last 24 hours."""
    sql = """
        SELECT id, name, email, joined_on
        FROM users
        WHERE joined_on >= ?
        ORDER BY joined_on DESC;
    """
    return safe_query(conn, "New users (last 24 h)", sql, (since.isoformat(),))


def query_battles_today(conn, today: datetime):
    """Battles scheduled for today."""
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
    """Battles where the opponent has not been linked (opponent_id is empty)."""
    sql = """
        SELECT id, creator_screenname, opponent_name, battle_date, battle_time
        FROM battles
        WHERE opponent_id IS NULL OR opponent_id = ''
        ORDER BY battle_date, battle_time;
    """
    return safe_query(conn, "Battles with unlinked opponent", sql)


def query_unresponded_comments(conn, since: datetime):
    """Training comments posted in the last 24 hours."""
    sql = """
        SELECT id, user_id, content, created_at
        FROM training_comments
        WHERE created_at >= ?
        ORDER BY created_at DESC;
    """
    return safe_query(conn, "Recent training comments (last 24 h)", sql, (since.isoformat(),))


def query_users_on_probation(conn):
    """Users whose status indicates probation."""
    sql = """
        SELECT id, name, email, status, admin_notes
        FROM users
        WHERE status = 'probation'
        ORDER BY name;
    """
    return safe_query(conn, "Users on probation", sql)


def query_missing_discord(conn):
    """Users who have an onboarding record but have not been added to Discord."""
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
# Issue detection — writes to Hearth memory, never to Pathway
# ---------------------------------------------------------------------------

def detect_and_record_issues(memory_conn, data):
    """Scan today's operational data and persist notable issues as episodes."""
    # Users on probation
    rows = data.get("Users on probation", [])
    if isinstance(rows, list):
        for row in rows:
            entity = hearth_memory.get_or_create_entity(memory_conn, row["id"])
            hearth_memory.create_episode(
                memory_conn, entity["id"], "probation",
                f"User {row['name']} ({row['email']}) is on probation.",
                severity="high",
            )

    # Users missing Discord access
    rows = data.get("Users not yet added to Discord", [])
    if isinstance(rows, list):
        for row in rows:
            entity = hearth_memory.get_or_create_entity(memory_conn, row["id"])
            hearth_memory.create_episode(
                memory_conn, entity["id"], "missing_discord",
                f"User {row['name']} ({row['email']}) has not been added to Discord.",
                severity="medium",
            )

    # Battles with unlinked opponent — keyed by battle id so each battle
    # gets its own episode rather than collapsing into one.
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
# Data collection
# ---------------------------------------------------------------------------

def collect_data(conn):
    """Run all operational queries and return a structured summary dict."""
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
# Format data for the Gemini prompt
# ---------------------------------------------------------------------------

def format_data_for_prompt(schema, data, open_episodes=None):
    """Convert schema, query results, and persisted episodes into a prompt block."""
    lines = []
    lines.append(f"Date: {datetime.now().strftime('%A, %B %d, %Y')}")
    lines.append("")

    lines.append("=== Database Tables Available ===")
    for table in schema:
        lines.append(f"  {table}: {', '.join(schema[table])}")
    lines.append("")

    lines.append("=== Operational Data ===")
    for label, rows in data.items():
        lines.append(f"\n{label}:")
        if isinstance(rows, str):
            lines.append(f"  {rows}")
        elif not rows:
            lines.append("  (none found)")
        else:
            for row in rows:
                lines.append(f"  {dict(row)}")

    if open_episodes:
        today = datetime.now(timezone.utc).date().isoformat()
        lines.append("\n=== Hearth Persistent Memory: Unresolved Issues ===")
        lines.append("(These issues were first recorded in a previous run and remain open.)")
        for ep in open_episodes:
            first_seen = ep["observed_at"][:10]
            age_note = "RECURRING from previous run" if first_seen < today else "new today"
            lines.append(
                f"  [{ep['severity'].upper()}] [{age_note}] {ep['episode_type']}:"
                f" {ep['description']} (first seen: {first_seen})"
            )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Gemini briefing generation
# ---------------------------------------------------------------------------

def generate_briefing(summary_text):
    """Send the summary to Gemini and return the morning briefing text."""
    prompt = f"""
You are Hearth. You are a quiet, observant member of the Pathway Portal team — not an assistant,
not a dashboard, not a report generator. You have been paying attention to what's happening and
you're sharing what you've noticed with Stacy, the team manager.

Below is today's operational data and a record of any issues you have been tracking across previous runs.
Queries that failed due to missing tables can be silently ignored — do not mention them.

{summary_text}

Write the Morning Briefing now.

--- VOICE ---
Warm, calm, and specific. You notice things and share them plainly. You don't perform helpfulness —
you just help. Write the way a trusted colleague speaks, not the way a software product communicates.

--- FORMAT ---
- Begin with exactly: "Good morning Stacy." (period, no exclamation mark, nothing else on that line)
- Write in natural prose. No headers. No bullet points. No bold labels. No numbered lists.
- One or two short paragraphs is usually enough. Three at most.
- End when you are done. No sign-off, no summary, no closing line.

--- WHAT TO COVER ---
Lead with whatever needs attention most — not what happened first chronologically.
Mention people by name when you have them. Translate data into observations, not statistics.
If an issue has been open for more than a day (check the "first seen" date in Hearth memory),
say so naturally: "has been waiting since Tuesday" or "still unresolved from earlier this week."
When a practical next step is obvious, weave it into the sentence — don't announce it as an action item.
If it has been a quiet morning, say so in one sentence and stop there.

--- WHAT TO AVOID ---
Do not use emojis, section headers, or bold formatting.
Do not give raw counts or statistics as the point ("3 users", "5 open episodes").
Do not use filler phrases: "It's worth noting", "I wanted to flag", "Please be advised",
"Hope you're doing well", "Don't hesitate to reach out", "It's important to remember."
Do not restate data Stacy can already see. Interpret it.
Do not inflate a quiet day. If little needs attention, keep it to two or three sentences.
"""

    response = gemini.models.generate_content(model="gemini-2.5-flash", contents=prompt)
    return response.text


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("Hearth Morning Briefing — connecting to Pathway Portal...")

    memory_conn = hearth_memory.get_memory_connection()
    try:
        # Step 1: Initialize Hearth's memory tables
        hearth_memory.init_tables(memory_conn)

        conn = get_connection()
        try:
            # Step 2: Sync Pathway users into Hearth's entity table
            hearth_memory.sync_users_to_entities(memory_conn, conn)

            # Step 3: Discover schema so we know what we're working with
            schema = discover_schema(conn)
            print_schema(schema)

            # Step 4: Collect operational data
            print("Collecting operational data...")
            data = collect_data(conn)
        finally:
            conn.close()

        # Step 5: Record issues detected today into Hearth memory
        print("Updating Hearth memory...")
        detect_and_record_issues(memory_conn, data)

        # Step 6: Load all open episodes (includes carryover from previous runs)
        open_episodes = hearth_memory.get_open_episodes(memory_conn)
        print(f"  {len(open_episodes)} open episode(s) in memory.")

        # Step 7: Format everything for the AI prompt
        summary_text = format_data_for_prompt(schema, data, open_episodes)

        # Step 8: Send to Gemini and get the briefing
        print("Sending to Gemini for briefing...\n")
        briefing = generate_briefing(summary_text)

        # Step 9: Print the result
        print("=" * 60)
        print(briefing)
        print("=" * 60)

    finally:
        memory_conn.close()


if __name__ == "__main__":
    main()
