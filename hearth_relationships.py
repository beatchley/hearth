"""
Hearth Relationships — relationship roads between Hearth entities.

Hearth reads Pathway's existing data to discover how people connect.
Pathway owns the truth; Hearth remembers the roads.

Architecture rule:
- Pathway is never modified.
- Hearth relationship roads are derived, not invented.
- Only concrete Pathway data produces relationships (assigned_coach_id, role).
- No emotional, personal, or subjective relationships are ever created.

Roads discovered in v0.1:
  coach_of / coached_by  — from users.assigned_coach_id (confidence 1.0)
  creator_role_peer      — users sharing the same coach and role, groups of 2–10
                           (confidence 0.7, source: structural inference)
"""

import sqlite3
from collections import defaultdict
from datetime import datetime, timezone


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def init_relationship_tables(conn):
    """Create the hearth_relationships table and indexes if they don't exist."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS hearth_relationships (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            entity_id_1       INTEGER NOT NULL REFERENCES hearth_entities(id),
            entity_id_2       INTEGER NOT NULL REFERENCES hearth_entities(id),
            relationship_type TEXT    NOT NULL,
            source            TEXT    NOT NULL,
            confidence        REAL    NOT NULL DEFAULT 1.0,
            first_observed_at TEXT    NOT NULL,
            last_observed_at  TEXT    NOT NULL,
            active            INTEGER NOT NULL DEFAULT 1,
            UNIQUE (entity_id_1, entity_id_2, relationship_type)
        );

        CREATE INDEX IF NOT EXISTS idx_relationships_entity1
            ON hearth_relationships (entity_id_1, active);

        CREATE INDEX IF NOT EXISTS idx_relationships_type
            ON hearth_relationships (entity_id_1, relationship_type, active);
    """)


# ---------------------------------------------------------------------------
# Upsert
# ---------------------------------------------------------------------------

def upsert_relationship(conn, entity_id_1, entity_id_2, relationship_type,
                        source, confidence, now):
    """
    Insert or update a relationship road. Idempotent — running repeatedly
    updates last_observed_at and re-activates the road without duplicates.
    """
    conn.execute(
        "INSERT INTO hearth_relationships"
        " (entity_id_1, entity_id_2, relationship_type, source, confidence,"
        "  first_observed_at, last_observed_at, active)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, 1)"
        " ON CONFLICT(entity_id_1, entity_id_2, relationship_type)"
        " DO UPDATE SET last_observed_at = excluded.last_observed_at, active = 1;",
        (entity_id_1, entity_id_2, relationship_type, source, confidence, now, now),
    )


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def discover_relationships(memory_conn, pathway_conn):
    """
    Read Pathway user data and write relationship roads into Hearth memory.

    Safe to call on every pipeline run — all writes are idempotent.
    If Pathway columns don't exist (schema differences), the function returns
    gracefully without error.

    Rule 1 — coach_of / coached_by
        Source: users.assigned_coach_id
        Confidence: 1.0 (explicit Pathway assignment)

    Rule 2 — creator_role_peer
        Source: users sharing the same assigned_coach_id and role
        Confidence: 0.7 (structural inference from grouping)
        Only created for groups of 2–10 to stay bounded and meaningful.
    """
    try:
        users = pathway_conn.execute(
            "SELECT id, role, assigned_coach_id FROM users;"
        ).fetchall()
    except sqlite3.Error:
        return

    if not users:
        return

    now = datetime.now(timezone.utc).isoformat()

    # user_id → hearth entity_id lookup
    entity_map = {
        row["user_id"]: row["id"]
        for row in memory_conn.execute(
            "SELECT id, user_id FROM hearth_entities WHERE user_id IS NOT NULL;"
        ).fetchall()
    }

    # Rule 1: Coach assignment roads
    for user in users:
        coach_user_id = user["assigned_coach_id"]
        if not coach_user_id:
            continue
        creator_eid = entity_map.get(user["id"])
        coach_eid = entity_map.get(coach_user_id)
        if not creator_eid or not coach_eid:
            continue
        upsert_relationship(memory_conn, coach_eid, creator_eid,
                            "coach_of", "pathway_assigned_coach", 1.0, now)
        upsert_relationship(memory_conn, creator_eid, coach_eid,
                            "coached_by", "pathway_assigned_coach", 1.0, now)

    # Rule 2: Shared-coach peer roads
    # Group users by (assigned_coach_id, role) — only meaningful for small groups
    peer_groups = defaultdict(list)
    for user in users:
        if user["assigned_coach_id"] and user["role"]:
            peer_groups[(user["assigned_coach_id"], user["role"])].append(user["id"])

    for group_members in peer_groups.values():
        if not (2 <= len(group_members) <= 10):
            continue
        for i in range(len(group_members)):
            for j in range(i + 1, len(group_members)):
                ea = entity_map.get(group_members[i])
                eb = entity_map.get(group_members[j])
                if not ea or not eb:
                    continue
                upsert_relationship(memory_conn, ea, eb,
                                    "creator_role_peer", "pathway_shared_coach", 0.7, now)
                upsert_relationship(memory_conn, eb, ea,
                                    "creator_role_peer", "pathway_shared_coach", 0.7, now)

    memory_conn.commit()


def discover_program_coach_relationships(memory_conn, pathway_conn):
    """
    Read creator_coach_assignments and write program-specific coach roads.

    Roads created:
      cn_coach_of / coached_by_cn    — from active cn assignments (confidence 1.0)
      shop_coach_of / coached_by_shop — from active shop assignments (confidence 1.0)

    Safe to call on every pipeline run — all writes are idempotent.
    If the table does not exist yet, returns gracefully.
    """
    try:
        rows = pathway_conn.execute(
            "SELECT creator_user_id, coach_user_id, program"
            " FROM creator_coach_assignments"
            " WHERE active = 1;"
        ).fetchall()
    except sqlite3.Error:
        return

    if not rows:
        return

    now = datetime.now(timezone.utc).isoformat()

    entity_map = {
        row["user_id"]: row["id"]
        for row in memory_conn.execute(
            "SELECT id, user_id FROM hearth_entities WHERE user_id IS NOT NULL;"
        ).fetchall()
    }

    for row in rows:
        creator_eid = entity_map.get(row["creator_user_id"])
        coach_eid = entity_map.get(row["coach_user_id"])
        if not creator_eid or not coach_eid:
            continue
        program = row["program"]  # 'cn' or 'shop'
        coach_of_type = f"{program}_coach_of"
        coached_by_type = f"coached_by_{program}"
        upsert_relationship(memory_conn, coach_eid, creator_eid,
                            coach_of_type, "pathway_program_coach", 1.0, now)
        upsert_relationship(memory_conn, creator_eid, coach_eid,
                            coached_by_type, "pathway_program_coach", 1.0, now)

    memory_conn.commit()


def discover_recruiter_relationships(memory_conn, pathway_conn):
    """
    Read Pathway recruiter data and write recruited_by / recruiter_of roads.

    Rule: recruited_by / recruiter_of
        Source: users.recruited_by_user_id
        Confidence: 1.0 (explicit Pathway assignment)
        Only approved creators with a non-null recruited_by_user_id are processed.
        If the recruiter has no Hearth entity, the relationship is skipped silently.
    """
    try:
        rows = pathway_conn.execute(
            "SELECT id, recruited_by_user_id"
            " FROM users"
            " WHERE recruited_by_user_id IS NOT NULL AND status = 'approved';"
        ).fetchall()
    except sqlite3.Error:
        return

    if not rows:
        return

    now = datetime.now(timezone.utc).isoformat()

    entity_map = {
        row["user_id"]: row["id"]
        for row in memory_conn.execute(
            "SELECT id, user_id FROM hearth_entities WHERE user_id IS NOT NULL;"
        ).fetchall()
    }

    for row in rows:
        creator_eid = entity_map.get(row["id"])
        recruiter_eid = entity_map.get(row["recruited_by_user_id"])
        if not creator_eid or not recruiter_eid:
            continue
        upsert_relationship(memory_conn, creator_eid, recruiter_eid,
                            "recruited_by", "pathway_recruited_by", 1.0, now)
        upsert_relationship(memory_conn, recruiter_eid, creator_eid,
                            "recruiter_of", "pathway_recruited_by", 1.0, now)

    memory_conn.commit()


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------

def get_relationships_for_entity(memory_conn, entity_id, active_only=True):
    """Return all relationship roads where this entity is entity_id_1."""
    sql = (
        "SELECT r.*, en.display_name AS related_display_name,"
        "       en.user_id AS related_user_id"
        " FROM hearth_relationships r"
        " LEFT JOIN hearth_entities en ON en.id = r.entity_id_2"
        " WHERE r.entity_id_1 = ?"
    )
    params = [entity_id]
    if active_only:
        sql += " AND r.active = 1"
    sql += " ORDER BY r.relationship_type;"
    return memory_conn.execute(sql, params).fetchall()


def get_related_entities(memory_conn, entity_id, relationship_type=None, active_only=True):
    """
    Return entities connected to this entity.

    If relationship_type is given, filters to only that road type.
    Returns entity rows enriched with relationship_type and confidence.
    """
    sql = (
        "SELECT en.*, r.relationship_type, r.confidence"
        " FROM hearth_relationships r"
        " JOIN hearth_entities en ON en.id = r.entity_id_2"
        " WHERE r.entity_id_1 = ?"
    )
    params = [entity_id]
    if active_only:
        sql += " AND r.active = 1"
    if relationship_type:
        sql += " AND r.relationship_type = ?"
        params.append(relationship_type)
    sql += " ORDER BY r.relationship_type;"
    return memory_conn.execute(sql, params).fetchall()
