"""
Hearth Identity — resolves Pathway user IDs to human-readable identity info.

Worldview and memory store IDs; presentation needs names. This module is the
single place that turns a Pathway users.id into the public identity fields
Hearth is allowed to show a human. Read-only against the Pathway database —
never writes, and never exposes password_hash.

Connects to the same Pathway database as morning_briefing.py and
hearth_pulse.py, via DATABASE_URL, using a read-only connection when it opens
one itself.
"""

import os
import sqlite3

from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")

# Explicit allowlist — never includes password_hash.
_IDENTITY_COLUMNS = (
    "id", "name", "tiktok_handle", "email", "role", "status",
    "is_pathway_creator", "is_shop_creator", "cn_level", "shop_level", "joined_on",
)


def _resolve_db_path(database_url):
    if database_url.startswith("sqlite:///"):
        return database_url[len("sqlite:///"):]
    return database_url


def get_pathway_connection():
    """Open a read-only connection to the Pathway database, or None if unavailable.

    Never raises — callers treat None the same as "identity lookup failed".
    """
    if not DATABASE_URL:
        return None
    try:
        db_path = _resolve_db_path(DATABASE_URL)
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error:
        return None


def get_user_identity(user_id, conn=None):
    """Return a dict of public identity fields for user_id, or None if unavailable.

    { "id", "name", "tiktok_handle", "email", "role", "status",
      "is_pathway_creator", "is_shop_creator", "cn_level", "shop_level", "joined_on" }

    Uses conn if provided (any existing Pathway connection). Otherwise opens
    and closes its own read-only connection. Never raises: returns None if
    user_id is missing, the connection is unavailable, the users table
    doesn't exist, or the user isn't found.
    """
    if user_id is None:
        return None

    owns_conn = conn is None
    if owns_conn:
        conn = get_pathway_connection()
    if conn is None:
        return None

    try:
        columns = ", ".join(_IDENTITY_COLUMNS)
        row = conn.execute(
            f"SELECT {columns} FROM users WHERE id = ?;", (user_id,)
        ).fetchone()
        if row is None:
            return None
        return dict(row)
    except sqlite3.Error:
        return None
    finally:
        if owns_conn:
            conn.close()


def get_user_display_name(user_id, conn=None):
    """Return the best available human-readable name for user_id.

    Fallback order: users.name -> users.tiktok_handle -> users.email ->
    "User {id}". Never raises.
    """
    identity = get_user_identity(user_id, conn=conn)
    if identity:
        for field in ("name", "tiktok_handle", "email"):
            value = identity.get(field)
            if value:
                return value
    return f"User {user_id}"


# ---------------------------------------------------------------------------
# Smoke test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    conn = get_pathway_connection()
    if conn is None:
        print("No Pathway connection available (DATABASE_URL missing or invalid) — "
              "skipping live checks, but defensive fallbacks still verified below.")
    else:
        print("Step 1: get_user_identity() for a real user")
        any_user = conn.execute("SELECT id FROM users LIMIT 1;").fetchone()
        if any_user:
            identity = get_user_identity(any_user["id"], conn=conn)
            assert identity is not None
            assert "password_hash" not in identity
            print(f"  id={identity['id']} name={identity['name']!r} "
                  f"email={identity['email']!r}")

            print("\nStep 2: get_user_display_name() prefers name")
            name = get_user_display_name(any_user["id"], conn=conn)
            print(f"  display_name={name!r}")
        else:
            print("  No users in this database — skipping live identity check.")

    print("\nStep 3: get_user_identity() for a missing user_id returns None")
    missing = get_user_identity(999999999, conn=conn)
    assert missing is None
    print("  OK")

    print("\nStep 4: get_user_display_name() falls back to 'User {id}' when missing")
    fallback_name = get_user_display_name(999999999, conn=conn)
    assert fallback_name == "User 999999999"
    print(f"  {fallback_name!r}")

    print("\nStep 5: get_user_identity(None) does not raise")
    assert get_user_identity(None, conn=conn) is None
    print("  OK")

    print("\nAll hearth_identity smoke test assertions passed.")
