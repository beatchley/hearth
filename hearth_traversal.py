"""
Hearth Traversal — one-hop connected-context retrieval for a Building.

Answers only "what is connected to this Building, and what compact context
exists for each connected Building?" Read-only. Does not rank relevance,
generate brief text, or perform ask-Hearth drill-in/search — that's Daily
Brief 2.0 and the ask-Hearth surface, not this module. Not wired into the
reflection loop.

Terminology: a Building is a hearth_entities row; a Road is a
hearth_relationships row.
"""

import sqlite3

from hearth_memory import get_memory_connection

# Furniture facts are a compact snapshot, not a dossier — cap what
# get_building_summary surfaces per Building.
MAX_FURNITURE_FACTS = 5


# ---------------------------------------------------------------------------
# Building Summary
# ---------------------------------------------------------------------------

def get_building_summary(memory_conn, entity_id):
    """Return a compact six-room snapshot for one Building, or None if the
    Building doesn't exist.

    Pulls Identity fields (display_name, entity_type, summary,
    patterns_noticed, concerns, strengths, importance_score), all current
    State values, and up to MAX_FURNITURE_FACTS active Furniture facts
    (most recently observed first). Deliberately not a full room dump.
    """
    entity = memory_conn.execute(
        "SELECT display_name, entity_type, summary, patterns_noticed,"
        " concerns, strengths, importance_score"
        " FROM hearth_entities WHERE id = ?;",
        (entity_id,),
    ).fetchone()
    if not entity:
        return None

    state_rows = memory_conn.execute(
        "SELECT state_key, state_value FROM hearth_entity_state"
        " WHERE entity_id = ? ORDER BY state_key;",
        (entity_id,),
    ).fetchall()

    furniture_rows = memory_conn.execute(
        "SELECT fact_text, fact_type FROM hearth_entity_furniture"
        " WHERE entity_id = ? AND status = 'active'"
        " ORDER BY first_observed_at DESC, id DESC LIMIT ?;",
        (entity_id, MAX_FURNITURE_FACTS),
    ).fetchall()

    return {
        "display_name": entity["display_name"],
        "entity_type": entity["entity_type"],
        "summary": entity["summary"],
        "patterns_noticed": entity["patterns_noticed"],
        "concerns": entity["concerns"],
        "strengths": entity["strengths"],
        "importance_score": entity["importance_score"],
        "state": {row["state_key"]: row["state_value"] for row in state_rows},
        "furniture": [
            {"fact_text": row["fact_text"], "fact_type": row["fact_type"]}
            for row in furniture_rows
        ],
    }


# ---------------------------------------------------------------------------
# Context Pointers
# ---------------------------------------------------------------------------

def get_context_pointers(memory_conn, entity_ids):
    """Return {entity_id: {furniture_count, current_state_count,
    active_road_count, episode_count, reflection_count}} for every id in
    entity_ids, defaulting to 0 for any room with no rows.

    One bulk query per room across all ids — never one query per room per
    entity. reflection_count uses Option A: only hearth_entity_reflection_refs,
    not worldview beliefs/uncertainties/changes/lessons directly. That ref
    table has no write path yet, so this will read 0 for nearly every
    Building today — expected, not a bug.
    """
    unique_ids = list({eid for eid in entity_ids if eid is not None})
    pointers = {
        eid: {
            "furniture_count": 0,
            "current_state_count": 0,
            "active_road_count": 0,
            "episode_count": 0,
            "reflection_count": 0,
        }
        for eid in unique_ids
    }
    if not unique_ids:
        return pointers

    placeholders = ",".join("?" for _ in unique_ids)

    for row in memory_conn.execute(
        "SELECT entity_id, COUNT(*) AS c FROM hearth_entity_furniture"
        f" WHERE status = 'active' AND entity_id IN ({placeholders})"
        " GROUP BY entity_id;",
        unique_ids,
    ).fetchall():
        pointers[row["entity_id"]]["furniture_count"] = row["c"]

    for row in memory_conn.execute(
        "SELECT entity_id, COUNT(*) AS c FROM hearth_entity_state"
        f" WHERE entity_id IN ({placeholders})"
        " GROUP BY entity_id;",
        unique_ids,
    ).fetchall():
        pointers[row["entity_id"]]["current_state_count"] = row["c"]

    for row in memory_conn.execute(
        "WITH counts AS ("
        f"  SELECT entity_id_1 AS entity_id FROM hearth_relationships"
        f"  WHERE active = 1 AND entity_id_1 IN ({placeholders})"
        "   UNION ALL"
        f"  SELECT entity_id_2 AS entity_id FROM hearth_relationships"
        f"  WHERE active = 1 AND entity_id_2 IN ({placeholders})"
        ")"
        " SELECT entity_id, COUNT(*) AS c FROM counts GROUP BY entity_id;",
        unique_ids + unique_ids,
    ).fetchall():
        pointers[row["entity_id"]]["active_road_count"] = row["c"]

    for row in memory_conn.execute(
        "SELECT entity_id, COUNT(*) AS c FROM hearth_episodes"
        f" WHERE entity_id IN ({placeholders})"
        " GROUP BY entity_id;",
        unique_ids,
    ).fetchall():
        pointers[row["entity_id"]]["episode_count"] = row["c"]

    for row in memory_conn.execute(
        "SELECT entity_id, COUNT(*) AS c FROM hearth_entity_reflection_refs"
        f" WHERE entity_id IN ({placeholders})"
        " GROUP BY entity_id;",
        unique_ids,
    ).fetchall():
        pointers[row["entity_id"]]["reflection_count"] = row["c"]

    return pointers


# ---------------------------------------------------------------------------
# Road reader
# ---------------------------------------------------------------------------

def _get_active_roads(memory_conn, entity_id):
    """Return every active Road touching entity_id, richer than
    hearth_relationships.get_relationships_for_entity()/get_related_entities()
    — those drop source/confidence/timestamps when building their result
    dicts. Preserves every Road column plus an inferred direction and the
    neighbor's id.

    Ordered most-recently-observed first, then relationship_type, then id —
    the deterministic order get_connected_context truncates against.
    """
    rows = memory_conn.execute(
        "SELECT r.* FROM hearth_relationships r"
        " WHERE (r.entity_id_1 = ? OR r.entity_id_2 = ?) AND r.active = 1"
        " ORDER BY r.last_observed_at DESC, r.relationship_type ASC, r.id ASC;",
        (entity_id, entity_id),
    ).fetchall()

    roads = []
    for row in rows:
        if row["entity_id_1"] == entity_id:
            neighbor_id, direction = row["entity_id_2"], "outgoing"
        else:
            neighbor_id, direction = row["entity_id_1"], "incoming"
        roads.append({
            "id": row["id"],
            "neighbor_entity_id": neighbor_id,
            "relationship_type": row["relationship_type"],
            "direction": direction,
            "origin": row["origin"],
            "source": row["source"],
            "confidence": row["confidence"],
            "first_observed_at": row["first_observed_at"],
            "last_observed_at": row["last_observed_at"],
            "status": row["status"],
        })
    return roads


# ---------------------------------------------------------------------------
# Connected Context
# ---------------------------------------------------------------------------

def _unavailable_building(entity_id):
    return {
        "id": entity_id,
        "name": "Unknown / unavailable",
        "type": None,
        "summary": {},
        "pointers": {},
        "error": "summary_assembly_failed",
    }


def get_connected_context(entity_id, max_neighbors=10):
    """Return the source Building plus its one-hop active-Road connections.

    Read-only. Does not rank, truncate silently, or recurse past one hop.
    See module docstring for full scope.
    """
    conn = get_memory_connection()
    try:
        try:
            source_summary = get_building_summary(conn, entity_id)
        except sqlite3.Error:
            return {"error": "source_assembly_failed", "entity_id": entity_id}

        if source_summary is None:
            return {"error": "entity_not_found", "entity_id": entity_id}

        roads = _get_active_roads(conn, entity_id)
        total_neighbors = len(roads)
        shown_roads = roads[:max_neighbors]
        overflow = max(0, total_neighbors - max_neighbors)

        neighbor_ids = [r["neighbor_entity_id"] for r in shown_roads]
        pointers = get_context_pointers(conn, [entity_id] + neighbor_ids)

        source = {
            "id": entity_id,
            "name": source_summary["display_name"],
            "type": source_summary["entity_type"],
            "summary": source_summary,
            "pointers": pointers.get(entity_id, {}),
        }

        connections = []
        for road in shown_roads:
            neighbor_id = road["neighbor_entity_id"]
            try:
                neighbor_summary = get_building_summary(conn, neighbor_id)
                if neighbor_summary is None:
                    raise ValueError(f"entity_id {neighbor_id} not found")
                building = {
                    "id": neighbor_id,
                    "name": neighbor_summary["display_name"],
                    "type": neighbor_summary["entity_type"],
                    "summary": neighbor_summary,
                    "pointers": pointers.get(neighbor_id, {}),
                }
            except Exception:
                building = _unavailable_building(neighbor_id)

            connections.append({
                "road": {
                    "id": road["id"],
                    "type": road["relationship_type"],
                    "direction": road["direction"],
                    "origin": road["origin"],
                    "source": road["source"],
                    "confidence": road["confidence"],
                    "first_observed_at": road["first_observed_at"],
                    "last_observed_at": road["last_observed_at"],
                    "status": road["status"],
                },
                "building": building,
            })

        return {
            "source": source,
            "connections": connections,
            "overflow": overflow,
            "total_neighbors": total_neighbors,
            "max_neighbors": max_neighbors,
        }
    finally:
        conn.close()
