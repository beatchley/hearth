"""
Ask Hearth — manager-facing question answering, built on top of existing
Hearth readers.

Pure logic only: no Flask, no routes, no templates. A Flask admin page in
pathway-portal calls into this module; that page is a separate, later piece
of work and is not built here.

Architecture rule:
    Routing is deterministic code. Gemini is the voice layer only.

Pipeline:
    Question text -> route_question() -> resolve_entity() (if needed) ->
    retrieval via hearth_traversal / hearth_context -> raw structured text ->
    Gemini phrasing (best-effort) -> AskHearthResult

Gemini never decides whether a question is supported, never searches data,
and never fills gaps with assumptions — it only turns already-retrieved,
grounded context into prose. If Gemini fails or is unavailable, the raw
retrieved summary is returned as-is rather than nothing.

Reuse discipline: "who needs attention today" calls hearth_context.build_context()
directly (memory_conn=None to skip its write side-effects — see that call
site below for why this is safe) rather than re-implementing Daily Brief 2.0's
_should_brief() filtering. Daily Brief and Ask Hearth must never be able to
silently diverge.

Does not modify: hearth_traversal.py, hearth_context.py, hearth_soul.py,
hearth_worldview.py, or any watcher/detector code.
"""

import re
from dataclasses import dataclass, field
from typing import Optional

import hearth_context
import hearth_memory
import hearth_traversal

# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class RoutedQuestion:
    """Deterministic routing decision for one question."""
    route: str  # tell_me_about_entity | connected_to_entity | needs_attention_today | unsupported
    entity_query: Optional[str] = None


@dataclass
class EntityResolution:
    """Result of resolving a free-text entity query against hearth_entities."""
    status: str  # resolved | ambiguous | not_found
    entity_id: Optional[int] = None
    entity_row: Optional[object] = None
    candidate_names: list = field(default_factory=list)  # populated only when ambiguous


@dataclass
class AskHearthResult:
    """The structured result the future Flask layer renders.

    status is one of: success, unsupported, ambiguous, not_found, error.
    entity_id is populated whenever a specific Building was resolved (success
    or a retrieval error after resolution), so a future Inspector link can be
    built from it — it is None for needs_attention_today, unsupported,
    ambiguous, and not_found.
    """
    status: str
    answer: str
    source_summary: str
    entity_id: Optional[int] = None


# ---------------------------------------------------------------------------
# 1. Deterministic routing
# ---------------------------------------------------------------------------

_UNSUPPORTED_MESSAGE = (
    "I can currently answer questions about a specific creator/building, or "
    "who needs attention today. I don't know how to answer that yet."
)

# Deliberately narrow, deterministic patterns — routing must never depend on
# LLM judgment. Anything not matched here is unsupported by design, not by
# omission; see module docstring.
_NEEDS_ATTENTION_RE = re.compile(
    r"^(?:who|what)\s+needs\s+(?:my\s+|our\s+|the\s+team'?s\s+)?attention(?:\s+today)?[\?\.!]*$"
)
_CONNECTED_TO_RE = re.compile(
    r"^(?:what|who)(?:'s|\s+is)\s+connected\s+to\s+(?:the\s+)?(.+?)[\?\.!]*$"
)
_TELL_ME_ABOUT_PATTERNS = [
    re.compile(r"^tell\s+me\s+about\s+(?:the\s+)?(.+?)[\?\.!]*$"),
    re.compile(r"^what\s+do\s+you\s+know\s+about\s+(?:the\s+)?(.+?)[\?\.!]*$"),
    re.compile(r"^what\s+can\s+you\s+tell\s+me\s+about\s+(?:the\s+)?(.+?)[\?\.!]*$"),
]


def route_question(question_text: str) -> RoutedQuestion:
    """Deterministically classify a free-text question. Never uses an LLM.

    entity_query preserves the original casing of the matched span (regex
    matching happens against a lowercased copy, but the capture group's
    start/end indices are sliced out of the original-case normalized text —
    safe because lowercasing never changes string length for the text this
    handles).
    """
    normalized = " ".join((question_text or "").strip().split())
    lowered = normalized.lower()

    if _NEEDS_ATTENTION_RE.match(lowered):
        return RoutedQuestion(route="needs_attention_today")

    m = _CONNECTED_TO_RE.match(lowered)
    if m:
        return RoutedQuestion(
            route="connected_to_entity",
            entity_query=normalized[m.start(1):m.end(1)].strip(),
        )

    for pattern in _TELL_ME_ABOUT_PATTERNS:
        m = pattern.match(lowered)
        if m:
            return RoutedQuestion(
                route="tell_me_about_entity",
                entity_query=normalized[m.start(1):m.end(1)].strip(),
            )

    return RoutedQuestion(route="unsupported")


# ---------------------------------------------------------------------------
# 2. Entity resolution
# ---------------------------------------------------------------------------

def _resolved(row) -> EntityResolution:
    return EntityResolution(status="resolved", entity_id=row["id"], entity_row=row)


def _ambiguous(rows) -> EntityResolution:
    names = sorted({row["display_name"] or f"Building {row['id']}" for row in rows})
    return EntityResolution(status="ambiguous", candidate_names=names)


def resolve_entity(memory_conn, query: str) -> EntityResolution:
    """Layered, deterministic resolution of a free-text query to one Building.

    Order: exact display_name match, case-insensitive display_name match,
    alias match (hearth_entities.aliases, comma-separated convention — this
    column has no write path anywhere today, so this layer is currently
    always a no-op; kept for forward compatibility rather than removed).
    Each layer only falls through to the next if it finds zero matches; two
    or more matches at any layer is ambiguous, not a guess.

    Fuzzy matching is deliberately not implemented — see module docstring /
    Ask Hearth investigation notes for the proposed future rule and why it
    was deferred.
    """
    query = (query or "").strip()
    if not query:
        return EntityResolution(status="not_found")

    rows = memory_conn.execute(
        "SELECT * FROM hearth_entities WHERE display_name = ?;", (query,)
    ).fetchall()
    if len(rows) == 1:
        return _resolved(rows[0])
    if len(rows) > 1:
        return _ambiguous(rows)

    rows = memory_conn.execute(
        "SELECT * FROM hearth_entities WHERE LOWER(display_name) = LOWER(?);", (query,)
    ).fetchall()
    if len(rows) == 1:
        return _resolved(rows[0])
    if len(rows) > 1:
        return _ambiguous(rows)

    alias_candidates = memory_conn.execute(
        "SELECT * FROM hearth_entities WHERE aliases IS NOT NULL AND aliases != '';"
    ).fetchall()
    query_lower = query.lower()
    matched = [
        row for row in alias_candidates
        if query_lower in [a.strip().lower() for a in row["aliases"].split(",") if a.strip()]
    ]
    if len(matched) == 1:
        return _resolved(matched[0])
    if len(matched) > 1:
        return _ambiguous(matched)

    return EntityResolution(status="not_found")


# ---------------------------------------------------------------------------
# 3. Tell-me-about / connected-to retrieval (shared retrieval, different emphasis)
# ---------------------------------------------------------------------------

_SPARSE_BIO_NOTE = (
    "Hearth has stronger relationship and activity context than biographical "
    "detail for this Building right now."
)


def _format_pointers(pointers) -> str:
    return (
        f"{pointers.get('furniture_count', 0)} furniture fact(s), "
        f"{pointers.get('current_state_count', 0)} state value(s), "
        f"{pointers.get('active_road_count', 0)} active road(s), "
        f"{pointers.get('episode_count', 0)} episode(s), "
        f"{pointers.get('reflection_count', 0)} reflection(s)"
    )


def _is_biographically_sparse(pointers) -> bool:
    return pointers.get("furniture_count", 0) == 0 and pointers.get("current_state_count", 0) == 0


def _build_raw_entity_text(context: dict, emphasize_connections: bool) -> str:
    """Render get_connected_context()'s output as honest, structured plain text.

    This is what gets shown verbatim if Gemini fails, and what Gemini is
    given as its only source of truth otherwise. Says plainly when
    biographical fields are empty rather than implying depth that isn't there.
    """
    source = context["source"]
    summary = source["summary"] or {}
    pointers = source["pointers"] or {}

    lines = [f"Building: {source['name']} (type: {source['type'] or 'unknown'})"]

    bio_fields = [
        ("Summary", summary.get("summary")),
        ("Patterns noticed", summary.get("patterns_noticed")),
        ("Concerns", summary.get("concerns")),
        ("Strengths", summary.get("strengths")),
    ]
    if any(value for _, value in bio_fields):
        for label, value in bio_fields:
            if value:
                lines.append(f"{label}: {value}")
    else:
        lines.append("Summary: none recorded")

    state = summary.get("state") or {}
    if state:
        lines.append("Current state: " + "; ".join(f"{k}={v}" for k, v in state.items()))
    else:
        lines.append("Current state: none recorded")

    furniture = summary.get("furniture") or []
    if furniture:
        lines.append("Recent furniture facts:")
        for fact in furniture:
            lines.append(f"  - {fact['fact_text']} ({fact['fact_type']})")
    else:
        lines.append("Furniture facts: none recorded")

    if _is_biographically_sparse(pointers):
        lines.append(_SPARSE_BIO_NOTE)

    lines.append(f"Context volume for this Building: {_format_pointers(pointers)}")

    connections = context.get("connections", [])
    total = context.get("total_neighbors", 0)
    overflow = context.get("overflow", 0)
    if connections:
        heading = "Connections" if not emphasize_connections else "What is connected to this Building"
        lines.append(f"{heading} — {total} total, showing {len(connections)}:")
        for conn in connections:
            road = conn["road"]
            building = conn["building"]
            arrow = "->" if road["direction"] == "outgoing" else "<-"
            lines.append(
                f"  - {arrow} {building['name']} "
                f"(relationship: {road['type'] or 'unspecified'}, confidence: {road.get('confidence')})"
            )
        if overflow:
            lines.append(f"  ...and {overflow} more connection(s) not shown (10-neighbor cap).")
    else:
        lines.append("Connections: none recorded.")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 4. Who-needs-attention-today retrieval
# ---------------------------------------------------------------------------

def _build_needs_attention_raw_text(context: hearth_context.HearthAwarenessContext) -> str:
    """Render only the Daily Brief 2.0-eligible concerns — nothing else.

    context comes from hearth_context.build_context(), the exact function
    Daily Brief itself calls, so this can never list something Daily Brief
    would suppress or omit something Daily Brief would show.
    """
    if not context.person_contexts and not context.unattached_concerns:
        return "Nobody currently meets Hearth's Daily Brief 2.0 bar for attention today."

    lines = []
    for person in context.person_contexts:
        lines.append(f"{person.display_name}:")
        for concern in person.open_concerns:
            lines.append(
                f"  - [{concern.severity.upper()}] {concern.description}"
                f" (first seen {concern.first_seen}, {concern.age_days}d old)"
            )
    for concern in context.unattached_concerns:
        lines.append(
            f"(unattached) [{concern.severity.upper()}] {concern.description}"
            f" (first seen {concern.first_seen}, {concern.age_days}d old)"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 5. Gemini usage — voice layer only, with a raw-summary fallback
# ---------------------------------------------------------------------------

_ASK_HEARTH_INSTRUCTION = (
    "You are summarizing Hearth's retrieved context for Pathway managers. "
    "Use only the provided context. If the context is sparse or missing, say "
    "that plainly. Do not invent facts."
)

_ENTITY_PROMPT_TEMPLATE = """\
{instruction}

{emphasis}

Retrieved context:
{raw_context}

Write a short, clear, team-member-style answer for a manager based only on the \
context above. If it is sparse, say so plainly instead of implying more depth \
than is there.
"""

_NEEDS_ATTENTION_PROMPT_TEMPLATE = """\
{instruction}

A Pathway manager asked who needs attention today. The list below is the \
exact set of people and concerns that meet Hearth's Daily Brief 2.0 bar for \
today — do not treat anything outside this list as needing attention, and do \
not soften or add caveats not present in the context.

Retrieved context:
{raw_context}

Write a short, clear answer for the manager. If the list says nobody meets \
the bar, say that plainly and stop there.
"""


def _entity_prompt(raw_context: str, emphasize_connections: bool) -> str:
    emphasis = (
        "The manager asked what is connected to this Building — lead with its "
        "connections, then add relevant background from the context."
        if emphasize_connections else
        "The manager asked to be told about this Building — lead with what "
        "Hearth knows about it directly, and mention connections as supporting context."
    )
    return _ENTITY_PROMPT_TEMPLATE.format(
        instruction=_ASK_HEARTH_INSTRUCTION, emphasis=emphasis, raw_context=raw_context,
    )


def _needs_attention_prompt(raw_context: str) -> str:
    return _NEEDS_ATTENTION_PROMPT_TEMPLATE.format(
        instruction=_ASK_HEARTH_INSTRUCTION, raw_context=raw_context,
    )


def _call_gemini(prompt: str, gemini_client) -> Optional[str]:
    """Best-effort Gemini call. Returns None on any failure, missing client,
    or empty response — callers must fall back to the raw summary, never to
    nothing. Gemini is never given anything but already-retrieved, grounded
    text; it cannot search data or add facts here.
    """
    if gemini_client is None:
        return None
    try:
        response = gemini_client.models.generate_content(model="gemini-2.5-flash", contents=prompt)
        text = getattr(response, "text", None)
        return text.strip() if text else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Per-route answer assembly
# ---------------------------------------------------------------------------

def _answer_entity_question(entity_id: int, emphasize_connections: bool, gemini_client) -> AskHearthResult:
    context = hearth_traversal.get_connected_context(entity_id, max_neighbors=10)
    if "error" in context:
        return AskHearthResult(
            status="error",
            answer="I found a matching Building but couldn't assemble its context right now.",
            source_summary=f"Traversal error: {context['error']}",
            entity_id=entity_id,
        )

    raw_text = _build_raw_entity_text(context, emphasize_connections)
    polished = _call_gemini(_entity_prompt(raw_text, emphasize_connections), gemini_client)

    pointers = context["source"]["pointers"] or {}
    source_summary = (
        f"Checked {context['source']['name']}'s Building summary, "
        f"{context.get('total_neighbors', 0)} active road(s), and context pointer counts "
        f"({_format_pointers(pointers)})"
    )
    if context.get("overflow"):
        source_summary += f"; {context['overflow']} connection(s) not shown (10-neighbor cap)"
    source_summary += "."

    return AskHearthResult(
        status="success",
        answer=polished if polished else raw_text,
        source_summary=source_summary,
        entity_id=entity_id,
    )


def _answer_needs_attention_today(memory_conn, gemini_client) -> AskHearthResult:
    open_episodes = hearth_memory.get_open_episodes(memory_conn)
    # memory_conn=None is deliberate: build_context()'s only two writes
    # (last_briefed_at cooldown stamping, principle-usage marking) are both
    # gated behind "if memory_conn:" internally, and every _should_brief()
    # filtering/grouping/sorting decision reads only from the already-fetched
    # episode rows above — so this reuses Daily Brief 2.0's exact filtering
    # with zero risk of Ask Hearth mutating Daily Brief's own state.
    context = hearth_context.build_context(data={}, open_episodes=open_episodes, memory_conn=None)

    raw_text = _build_needs_attention_raw_text(context)
    polished = _call_gemini(_needs_attention_prompt(raw_text), gemini_client)

    source_summary = (
        f"Checked open episodes against Daily Brief 2.0's _should_brief() filtering: "
        f"{len(context.person_contexts)} person(s) and {len(context.unattached_concerns)} "
        f"unattached concern(s) currently meet the bar."
    )

    return AskHearthResult(
        status="success",
        answer=polished if polished else raw_text,
        source_summary=source_summary,
        entity_id=None,
    )


# ---------------------------------------------------------------------------
# Top-level entry point
# ---------------------------------------------------------------------------

def answer_question(question_text: str, memory_conn=None, gemini_client=None) -> AskHearthResult:
    """Answer one free-text manager question. The only public entry point
    a future Flask layer should call.

    memory_conn is optional — if not provided, this opens and closes its own
    connection. gemini_client is optional — if not provided (or if it fails),
    the raw retrieved summary is returned instead of prose, never nothing.
    """
    owns_conn = memory_conn is None
    if owns_conn:
        memory_conn = hearth_memory.get_memory_connection()
    try:
        routed = route_question(question_text)

        if routed.route == "unsupported":
            return AskHearthResult(
                status="unsupported", answer=_UNSUPPORTED_MESSAGE, source_summary="", entity_id=None,
            )

        if routed.route == "needs_attention_today":
            return _answer_needs_attention_today(memory_conn, gemini_client)

        # tell_me_about_entity / connected_to_entity — both need entity resolution
        resolution = resolve_entity(memory_conn, routed.entity_query)

        if resolution.status == "not_found":
            return AskHearthResult(
                status="not_found",
                answer=f"I couldn't find a Building matching '{routed.entity_query}'.",
                source_summary="",
                entity_id=None,
            )

        if resolution.status == "ambiguous":
            names = ", ".join(resolution.candidate_names)
            return AskHearthResult(
                status="ambiguous",
                answer=(
                    f"I found more than one Building matching '{routed.entity_query}': "
                    f"{names}. Which one did you mean?"
                ),
                source_summary="",
                entity_id=None,
            )

        return _answer_entity_question(
            resolution.entity_id,
            emphasize_connections=(routed.route == "connected_to_entity"),
            gemini_client=gemini_client,
        )
    except Exception as exc:
        return AskHearthResult(
            status="error",
            answer="Something went wrong answering that question.",
            source_summary=f"Unexpected error: {exc}",
            entity_id=None,
        )
    finally:
        if owns_conn:
            memory_conn.close()


# ---------------------------------------------------------------------------
# Smoke test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    _MARKER = "ASK_HEARTH_SMOKE_TEST"

    print("Step 1: route_question() — deterministic routing")
    assert route_question("Tell me about Ethan").route == "tell_me_about_entity"
    assert route_question("Tell me about Ethan").entity_query == "Ethan"
    assert route_question("What do you know about Ethan?").route == "tell_me_about_entity"
    assert route_question("What is connected to Ethan?").route == "connected_to_entity"
    assert route_question("What is connected to Ethan?").entity_query == "Ethan"
    assert route_question("Who needs attention today?").route == "needs_attention_today"
    assert route_question("What needs attention?").route == "needs_attention_today"
    assert route_question("What's Ethan's coach?").route == "unsupported"
    assert route_question("").route == "unsupported"
    print("  All routing assertions passed.")

    conn = hearth_memory.get_memory_connection()
    hearth_memory.init_tables(conn)

    entity_a_name = f"{_MARKER}_ENTITY_A"
    entity_b_name = f"{_MARKER}_ENTITY_B"
    dup_name = f"{_MARKER}_DUPLICATE"

    try:
        now_placeholder = None  # created_at is required NOT NULL below
        import datetime as _dt
        now = _dt.datetime.now(_dt.timezone.utc).isoformat()

        cur_a = conn.execute(
            "INSERT INTO hearth_entities (display_name, entity_type, summary, created_at)"
            " VALUES (?, 'person', ?, ?);",
            (entity_a_name, f"Smoke test summary. {_MARKER}", now),
        )
        entity_a_id = cur_a.lastrowid

        cur_b = conn.execute(
            "INSERT INTO hearth_entities (display_name, entity_type, created_at)"
            " VALUES (?, 'person', ?);",
            (entity_b_name, now),
        )
        entity_b_id = cur_b.lastrowid

        conn.execute(
            "INSERT INTO hearth_relationships"
            " (entity_id_1, entity_id_2, relationship_type, active, confidence,"
            "  first_observed_at, last_observed_at, origin, source, status)"
            " VALUES (?, ?, 'smoke_test_link', 1, 0.9, ?, ?, 'smoke_test', 'smoke_test', 'active');",
            (entity_a_id, entity_b_id, now, now),
        )

        cur_dup1 = conn.execute(
            "INSERT INTO hearth_entities (display_name, entity_type, created_at)"
            " VALUES (?, 'person', ?);",
            (dup_name, now),
        )
        cur_dup2 = conn.execute(
            "INSERT INTO hearth_entities (display_name, entity_type, created_at)"
            " VALUES (?, 'person', ?);",
            (dup_name, now),
        )
        conn.commit()

        print("Step 2: resolve_entity() — exact, case-insensitive, ambiguous, not_found")
        res = resolve_entity(conn, entity_a_name)
        assert res.status == "resolved" and res.entity_id == entity_a_id
        res = resolve_entity(conn, entity_a_name.lower())
        assert res.status == "resolved" and res.entity_id == entity_a_id
        res = resolve_entity(conn, dup_name)
        assert res.status == "ambiguous" and res.candidate_names == [dup_name]
        res = resolve_entity(conn, f"{_MARKER}_NO_SUCH_ENTITY")
        assert res.status == "not_found"
        print("  All resolution assertions passed.")

        print("Step 3: needs_attention_today seed episodes (one qualifying, one _NEVER_BRIEF)")
        support_id, _ = hearth_memory.create_episode(
            conn, entity_a_id, "support_request_waiting",
            description=f"Support request waiting. {_MARKER}", severity="high",
        )
        quiet_id, _ = hearth_memory.create_episode(
            conn, entity_b_id, "creator_quiet",
            description=f"Creator quiet. {_MARKER}", severity="medium",
        )

        print("Step 4: answer_question() end to end, no Gemini client (fallback path)")
        result = answer_question(f"Tell me about {entity_a_name}", memory_conn=conn, gemini_client=None)
        assert result.status == "success"
        assert result.entity_id == entity_a_id
        assert "Smoke test summary" in result.answer
        assert entity_b_name in result.answer  # connection should appear

        result = answer_question(f"What is connected to {entity_a_name}", memory_conn=conn, gemini_client=None)
        assert result.status == "success"
        assert entity_b_name in result.answer

        result = answer_question("Who needs attention today?", memory_conn=conn, gemini_client=None)
        assert result.status == "success"
        assert _MARKER in result.answer  # support_request_waiting concern present
        assert "Creator quiet" not in result.answer  # _NEVER_BRIEF_EPISODE_TYPES suppressed

        result = answer_question("What's the weather like?", memory_conn=conn, gemini_client=None)
        assert result.status == "unsupported"
        assert result.answer == _UNSUPPORTED_MESSAGE

        result = answer_question(f"Tell me about {dup_name}", memory_conn=conn, gemini_client=None)
        assert result.status == "ambiguous"
        assert dup_name in result.answer

        result = answer_question(f"Tell me about {_MARKER}_GHOST", memory_conn=conn, gemini_client=None)
        assert result.status == "not_found"

        print("  All end-to-end assertions passed (no-Gemini fallback path).")

        print("Step 5: Gemini failure also falls back to raw summary, not nothing")
        class _ExplodingGeminiModels:
            def generate_content(self, model, contents):
                raise RuntimeError("simulated Gemini outage")

        class _ExplodingGeminiClient:
            models = _ExplodingGeminiModels()

        result = answer_question(
            f"Tell me about {entity_a_name}", memory_conn=conn, gemini_client=_ExplodingGeminiClient(),
        )
        assert result.status == "success"
        assert "Smoke test summary" in result.answer  # raw text, Gemini never reached the caller
        print("  Gemini-failure fallback assertion passed.")

        print("\nAll hearth_ask smoke test assertions passed.")
    finally:
        print("\nCleanup — removing all smoke-test rows")
        conn.execute(
            "DELETE FROM hearth_relationships WHERE source = 'smoke_test' AND origin = 'smoke_test';"
        )
        conn.execute("DELETE FROM hearth_episodes WHERE description LIKE ?;", (f"%{_MARKER}%",))
        conn.execute("DELETE FROM hearth_entities WHERE display_name LIKE ?;", (f"{_MARKER}%",))
        conn.commit()
        remaining = conn.execute(
            "SELECT COUNT(*) FROM hearth_entities WHERE display_name LIKE ?;", (f"{_MARKER}%",)
        ).fetchone()[0]
        print(f"  Remaining smoke-test entities after cleanup: {remaining}")
        conn.close()
        print("Smoke test complete.")
