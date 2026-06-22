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
# Identity predicates — creator and staff are independent, overlapping
# identities. Pathway's actual model is:
#   users.role                          -> staff/access responsibility
#   users.is_pathway_creator/is_shop_creator -> creator identity
# A user can satisfy both. Centralized here so every watcher across Hearth
# (creator_quiet, new_creator_stuck, support_request_waiting,
# training_comment_waiting, etc.) agrees on what counts as staff.
# ---------------------------------------------------------------------------

# Union of every staff-like role found across Hearth/Pathway role checks
# (morning_briefing.py watcher exclusions, the comment-needs-response staff
# filter, and hearth_pulse.py's _STAFF_ROLES).
STAFF_ROLES = frozenset({
    "ceo",
    "manager",
    "coach",
    "navigator",
    "it",
    "admin",
})


def _get_field(row_or_dict, field, default=None):
    """Defensively read field from a sqlite3.Row, dict, or ORM-like object.

    sqlite3.Row raises IndexError (not KeyError) for an unknown column, and
    has no .get(), so it needs its own branch.
    """
    if row_or_dict is None:
        return default
    if isinstance(row_or_dict, sqlite3.Row):
        try:
            return row_or_dict[field]
        except IndexError:
            return default
    if isinstance(row_or_dict, dict):
        return row_or_dict.get(field, default)
    return getattr(row_or_dict, field, default)


def normalize_role(role):
    """Lowercase/trim a role string for comparison. None/non-string -> ""."""
    if role is None:
        return ""
    return str(role).strip().lower()


def is_creator_user(row_or_dict):
    """True if either creator flag is set — independent of role/staff status.

    A staff member with is_pathway_creator or is_shop_creator set is still
    a creator; being staff does not remove creator identity.
    """
    is_pathway_creator = _get_field(row_or_dict, "is_pathway_creator")
    is_shop_creator = _get_field(row_or_dict, "is_shop_creator")
    return bool(is_pathway_creator) or bool(is_shop_creator)


def is_staff_user(row_or_dict):
    """True if the user's normalized role is in the central STAFF_ROLES set.

    Independent of creator flags — a staff creator is both.
    """
    role = _get_field(row_or_dict, "role")
    return normalize_role(role) in STAFF_ROLES


def identity_flags(row_or_dict):
    """Return {"is_creator": bool, "is_staff": bool} for row_or_dict.

    The two flags are independent and can both be True for a hybrid
    staff/creator user.
    """
    return {
        "is_creator": is_creator_user(row_or_dict),
        "is_staff": is_staff_user(row_or_dict),
    }


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
