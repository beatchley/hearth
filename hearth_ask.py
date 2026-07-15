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

Phase 4 addition: when route_question() finds none of the three patterns
above match, this module now tries one more thing before giving up — the
bounded manager-advice cognitive path in hearth_manager_advice.py (see that
module's docstring, and docs/HEARTH_TOOLSET_MANAGER_ADVICE_SCENARIO.md /
docs/HEARTH_COGNITIVE_PROCESS.md). The three routes above, and route_question()
itself, are unchanged by this addition — see answer_question()'s
"unsupported" branch, the only place this is wired in.

Phase 7a addition: answer_question() now takes an optional attention_frame
(hearth_attention_frame.AttentionFrame) — Hearth's first session-scoped
conversational continuity. When present, a pronoun referring to a person
("he"/"him"/"his"/"she"/"her"/"they"/"them"/"their") is resolved to the
frame's currently-focused Building's name before routing, so a follow-up
question doesn't need to restate the name every turn; an entity-less
follow-up gets the same fallback applied one level deeper, inside the
manager-advice eligibility gate only (see hearth_manager_advice.py's
fallback_entity_name). Every turn still runs route_question() or the full
eligibility gate unchanged, still resolves/re-authorizes from scratch, and
still completes Grounded Assertions validation before anything is
returned — continuity can reduce retrieval (reusing a previous turn's
evidence for the same Building) but never bypasses validation,
authorization, or entity resolution. With attention_frame=None (the
default), behavior is byte-for-byte identical to before this phase.

Phase 7b addition: answer_question() now also takes an optional
actor_user_id, and the "unsupported" branch below stages every
routing-eligible human turn into the Conversation Ledger
(hearth_conversation_ledger.py) — conversations become the Furniture Fact
Extractor's eighth observational source. Eligibility is decided purely from
routed.route (== "unsupported", i.e. none of the three fixed patterns
below matched) and actor authorization, never from message content — see
_stage_eligible_conversation_turn() below. This does not change what
answer_question() returns, does not add a new proposal workflow, and does
not perform any extraction itself; it only appends to a durable staging
table that hearth_fact_extractor.py's existing daily batch now also reads.
"""

import logging
import re
from dataclasses import dataclass
from typing import Optional

import hearth_attention_frame
import hearth_context
import hearth_conversation_ledger
import hearth_manager_advice
import hearth_memory
import hearth_traversal
from hearth_entity_resolution import EntityResolution, resolve_entity
from hearth_gemini_config import GEMINI_MODEL_NAME

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class RoutedQuestion:
    """Deterministic routing decision for one question."""
    route: str  # tell_me_about_entity | connected_to_entity | needs_attention_today | unsupported
    entity_query: Optional[str] = None


@dataclass
class AskHearthResult:
    """The structured result the future Flask layer renders.

    status is one of: success, unsupported, ambiguous, not_found, error,
    not_authorized. not_authorized (Phase 6) is returned only by the
    manager-advice cognitive path when actor_role isn't one of
    hearth_manager_advice.AUTHORIZED_ACTOR_ROLES — distinct from
    unsupported (the question shape wasn't recognized) and from error (a
    retrieval failure): this specifically means the actor isn't allowed to
    ask this kind of question at all, regardless of content.
    entity_id is populated whenever a specific Building was resolved (success
    or a retrieval error after resolution), so a future Inspector link can be
    built from it — it is None for needs_attention_today, unsupported,
    ambiguous, not_found, and not_authorized.
    plan is populated only by the manager-advice cognitive path
    (hearth_manager_advice.run_manager_advice_path()) — the structured
    retrieval plan (goal/known/to_verify) it produced, made inspectable
    rather than only logged. None for every other route/status, unchanged.
    validation is populated only by that same path — the Phase 5 Grounded
    Assertions validation record (hearth_assertion_validation.py) for the
    assertions actually rendered into `answer`, made inspectable the same
    way `plan` already is. None for every other route/status.
    """
    status: str
    answer: str
    source_summary: str
    entity_id: Optional[int] = None
    plan: Optional[dict] = None
    validation: Optional[dict] = None


# Every field AskHearthResult actually declares — used to filter dicts
# returned by hearth_manager_advice.py before constructing the dataclass,
# so an internal-only key that module adds for its own purposes (Phase 7a:
# "evidence", stashed in the Attention Frame, never rendered to a manager)
# can never leak into, or break, this dataclass's fixed shape.
_ASKHEARTHRESULT_FIELDS = {"status", "answer", "source_summary", "entity_id", "plan", "validation"}


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

# Straight ASCII quotes plus curly/smart quotes — browsers and OS-level
# autocorrect frequently convert a typed " or ' into one of these, so a
# quote-wrapped question ("Who needs attention today?") can arrive in any
# of these forms.
_OUTER_QUOTE_PAIRS = (
    ('"', '"'),
    ("'", "'"),
    ("“", "”"),  # “ ”
    ("‘", "’"),  # ‘ ’
)


def _strip_outer_quote_pair(text: str) -> str:
    """Strip exactly one balanced pair of quote characters wrapping the
    ENTIRE string, if present — never a quote appearing anywhere else in
    the string. This is why the check is "does the string START and END
    with a matching quote pair," not "does it contain a quote character":
    text[1:-1] only ever removes the outermost two characters, so an
    internal apostrophe (e.g. "What's connected to Ethan") is untouched
    since neither of its ends is a quote.

    Single pass only, even for doubled/nested outer quotes — e.g. the
    literal input ""hello"" (two pairs) is stripped once, leaving "hello"
    (one pair remaining), not hello: a second pass risks eating into
    content that is genuinely part of the question (e.g. a Building whose
    display_name itself begins or ends with a quote character) rather than
    incidental wrapping. No entity in the current database has a
    display_name starting or ending with a quote character (checked
    directly), so this is a real but currently unobserved edge case, not a
    guessed-at one — documented rather than silently guarded against with
    more complexity than the data justifies.
    """
    if len(text) < 2:
        return text
    for open_q, close_q in _OUTER_QUOTE_PAIRS:
        if text[0] == open_q and text[-1] == close_q:
            return text[1:-1]
    return text


def _normalize_question_text(question_text: str) -> str:
    """Shared normalization — the single place every route in this module
    (the three fixed patterns below, and the Phase 4 manager-advice
    eligibility gate in hearth_manager_advice.py, via answer_question()'s
    call to it) sees text through, so a normalization fix like outer-quote
    stripping only needs to happen once, not per caller.

    Order matters: outer quotes are stripped only after trimming genuine
    leading/trailing whitespace (so a quote is actually the first/last
    character being checked, not preceded by stray spaces), and before the
    internal-whitespace collapse (so any whitespace left just inside the
    quotes — e.g. '" Who needs attention? "' — still gets collapsed).
    """
    stripped = (question_text or "").strip()
    stripped = _strip_outer_quote_pair(stripped)
    return " ".join(stripped.split())


# ---------------------------------------------------------------------------
# 1b. Attention Frame continuity (Phase 7a) — pronoun resolution only.
#
# Deliberately narrow, like every other piece of text handling in this
# module: only clear third-person-singular/plural pronouns referring to a
# person are substituted, and only when the frame has a focused Building.
# This runs on the SAME normalized text every downstream consumer sees
# (route_question()'s fixed patterns and hearth_manager_advice's
# entity-mention scan both see the substituted form), so a follow-up like
# "What is connected to him?" matches _CONNECTED_TO_RE exactly as if the
# manager had typed the Building's name. A question naming a different,
# explicit Building is never touched by this — pronoun substitution only
# ever fires on a pronoun, never on a proper noun.
# ---------------------------------------------------------------------------

_FOCUS_PRONOUN_RE = re.compile(r"\b(he|him|his|she|her|they|them|their)\b", re.IGNORECASE)


def _apply_attention_frame_pronouns(normalized_text: str, attention_frame) -> str:
    """Substitute person pronouns with the frame's focused Building name.

    No-op (returns normalized_text unchanged) when attention_frame is None,
    has no focused Building yet, or the text contains no matching pronoun.
    """
    if attention_frame is None or not attention_frame.focused_entity_name:
        return normalized_text
    if not _FOCUS_PRONOUN_RE.search(normalized_text):
        return normalized_text
    return _FOCUS_PRONOUN_RE.sub(attention_frame.focused_entity_name, normalized_text)


def route_question(question_text: str) -> RoutedQuestion:
    """Deterministically classify a free-text question. Never uses an LLM.

    entity_query preserves the original casing of the matched span (regex
    matching happens against a lowercased copy, but the capture group's
    start/end indices are sliced out of the original-case normalized text —
    safe because lowercasing never changes string length for the text this
    handles).
    """
    normalized = _normalize_question_text(question_text)
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
# 2. Entity resolution — see hearth_entity_resolution.py (EntityResolution,
# resolve_entity imported above). Shared with the Furniture Fact Extractor
# so the two never carry separate copies of the same matching rules.
# ---------------------------------------------------------------------------


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
        response = gemini_client.models.generate_content(model=GEMINI_MODEL_NAME, contents=prompt)
        text = getattr(response, "text", None)
        return text.strip() if text else None
    except Exception as exc:
        logger.warning(
            "[ask_hearth] Gemini generate_content() call failed — falling back to raw"
            " summary: %s: %s", type(exc).__name__, exc,
        )
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
# 6. Conversation Ledger staging (Phase 7b)
#
# Scope is enforced here by routing, not message content, per the Phase 7b
# build brief: eligible = routed.route == "unsupported" (none of the three
# fixed patterns above matched — the same routing decision that sends a
# message to hearth_manager_advice.py at all) AND the actor is authorized
# (the same ceo/manager/it check hearth_manager_advice.is_actor_authorized()
# applies, checked independently here so an unauthorized actor's text is
# never even written to staging, regardless of what hearth_manager_advice.py
# itself would go on to do with it).
#
# This deliberately does NOT additionally require hearth_manager_advice's
# own internal eligibility gate (entity-mention scan + advice-seeking
# classifier) to have passed. That gate decides whether Hearth will *answer*
# with advice; it does not decide whether a conversational turn is eligible
# Furniture-extraction material. A plain self-fact statement ("My favorite
# food is cheeseburgers.") names no Building and is not advice-seeking, so
# hearth_manager_advice's gate would reject it — but it is exactly the kind
# of open, conversational turn this phase exists to capture. Staging
# therefore runs before, and independently of, that inner gate.
# ---------------------------------------------------------------------------

def _stage_eligible_conversation_turn(
    memory_conn, attention_frame, actor_role: Optional[str], actor_user_id, resolved_text: str,
) -> None:
    """Append this turn to the Conversation Ledger if (and only if) it is
    eligible — see the scope note above. Best-effort: any failure here is
    logged and swallowed, never allowed to affect the manager's actual
    answer. Only ever called with the human's incoming message text, never
    with anything Hearth generates — Hearth's own responses are never
    staged, by construction (this function has no access to them).
    """
    if not hearth_manager_advice.is_actor_authorized(actor_role):
        return
    try:
        hearth_conversation_ledger.stage_conversation_turn(
            memory_conn,
            resolved_text,
            session_id=attention_frame.session_id if attention_frame else None,
            author_user_id=actor_user_id,
            actor_role=actor_role,
        )
    except Exception as exc:
        logger.warning(
            "[ask_hearth] failed to stage conversation turn for Fact Extractor"
            " staging (non-fatal — the manager's answer is unaffected): %s: %s",
            type(exc).__name__, exc,
        )


# ---------------------------------------------------------------------------
# Top-level entry point
# ---------------------------------------------------------------------------

def _update_attention_frame(attention_frame, memory_conn, question_text: str, result: AskHearthResult, evidence: Optional[dict] = None, plan: Optional[dict] = None) -> None:
    """Phase 7a: record this turn's outcome into the Attention Frame.

    A no-op whenever attention_frame is None — every call site above
    passes it through unconditionally so behavior with no frame is
    identical to before this phase existed.

    Only ever updates focused_entity_id/name when this turn actually
    resolved a Building (result.entity_id is set) — a turn that didn't
    resolve one (needs_attention_today, unsupported, not_found, ambiguous,
    not_authorized) leaves a still-relevant earlier focus untouched rather
    than clobbering it. The display name is looked up fresh from
    hearth_entities here (not threaded through AskHearthResult, which has
    no such field) — a single, cheap, read-only lookup.

    This function stores a pointer to what was retrieved, never the
    retrieved content as asserted fact — see module docstring's
    Foundational Principle. It does not validate anything and must never
    be treated as having done so.
    """
    if attention_frame is None:
        return

    attention_frame.turn_count += 1
    attention_frame.goal = question_text
    attention_frame.touch()

    if result.entity_id is not None:
        row = memory_conn.execute(
            "SELECT display_name FROM hearth_entities WHERE id = ?;", (result.entity_id,)
        ).fetchone()
        if row and row["display_name"]:
            attention_frame.focused_entity_id = result.entity_id
            attention_frame.focused_entity_name = row["display_name"]

    if evidence is not None:
        attention_frame.last_evidence = evidence
    if plan is not None:
        attention_frame.last_plan = plan


def answer_question(
    question_text: str,
    memory_conn=None,
    gemini_client=None,
    actor_role: Optional[str] = None,
    attention_frame: Optional[hearth_attention_frame.AttentionFrame] = None,
    actor_user_id=None,
) -> AskHearthResult:
    """Answer one free-text manager question. The only public entry point
    a future Flask layer should call.

    memory_conn is optional — if not provided, this opens and closes its own
    connection. gemini_client is optional — if not provided (or if it fails),
    the raw retrieved summary is returned instead of prose, never nothing.

    actor_role (Phase 6): the calling user's role (e.g. "ceo", "manager",
    "it", "coach"), passed straight through to the manager-advice cognitive
    path, which independently refuses to run unless it's one of
    hearth_manager_advice.AUTHORIZED_ACTOR_ROLES — the same role set
    /admin/hearth/ask already gates on at the Flask route level. This is an
    additional check, not a replacement for that route-level gate. The
    three fixed patterns below are unaffected by actor_role — that gap, if
    any, is out of this phase's scope (see completion report).

    attention_frame (Phase 7a, optional, default None): the caller's
    session-scoped hearth_attention_frame.AttentionFrame, if a conversation
    is active. See module docstring's "Phase 7a addition" for what this
    changes (pronoun resolution before routing, evidence reuse in the
    manager-advice path) and, just as importantly, what it never changes:
    routing itself, entity resolution, authorization, and Grounded
    Assertions validation all run exactly as they would with no frame at
    all. Every return path below updates the frame (a no-op if None) right
    before returning, so the frame always reflects the most recent turn's
    outcome by the time this function returns.

    actor_user_id (Phase 7b, optional, default None): the calling manager's
    Pathway users.id, passed straight through to Conversation Ledger
    staging as the turn's author/default subject candidate (the same role
    subject_user_column plays for every other Fact Extractor source — see
    hearth_fact_extractor.py). Omitting it does not disable staging, only
    the author-as-subject candidate for that staged turn; named-Building
    mentions in the text are still captured via the extractor's existing
    entity-mention scan.
    """
    owns_conn = memory_conn is None
    if owns_conn:
        memory_conn = hearth_memory.get_memory_connection()
    try:
        # Phase 7a: one shared normalization + pronoun-resolution pass, seen
        # identically by route_question() below and by the manager-advice
        # gate's entity-mention scan — see _apply_attention_frame_pronouns().
        normalized = _normalize_question_text(question_text)
        resolved_text = _apply_attention_frame_pronouns(normalized, attention_frame)

        routed = route_question(resolved_text)

        if routed.route == "unsupported":
            # Phase 4: before giving up, try the bounded manager-advice
            # cognitive path (docs/HEARTH_TOOLSET_MANAGER_ADVICE_SCENARIO.md).
            # This does not change routing for anything the three routes
            # above already handle — route_question()'s patterns are tried
            # first, unconditionally, and are untouched by this branch.
            # Passes the same pronoun-resolved, normalized text
            # route_question() itself matched against — the shared
            # normalization step (outer-quote stripping, whitespace
            # collapse, Phase 7a pronoun resolution) applies once, to every
            # route, not just the three fixed patterns.
            fallback_name = attention_frame.focused_entity_name if attention_frame else None
            prior_evidence = attention_frame.last_evidence if attention_frame else None
            prior_entity_id = attention_frame.focused_entity_id if attention_frame else None
            # Phase 7b: stage before running the cognitive path itself — see
            # the scope note above _stage_eligible_conversation_turn(). This
            # is what makes staging happen "immediately when received"
            # rather than only after (and conditional on) a successful
            # answer.
            _stage_eligible_conversation_turn(
                memory_conn, attention_frame, actor_role, actor_user_id, resolved_text,
            )
            advice_result, _gate = hearth_manager_advice.answer_manager_advice_question(
                resolved_text, memory_conn, gemini_client, actor_role,
                fallback_entity_name=fallback_name,
                prior_evidence=prior_evidence,
                prior_entity_id=prior_entity_id,
            )
            if advice_result is not None:
                evidence = advice_result.get("evidence")
                plan = advice_result.get("plan")
                filtered = {k: v for k, v in advice_result.items() if k in _ASKHEARTHRESULT_FIELDS}
                result = AskHearthResult(**filtered)
                _update_attention_frame(attention_frame, memory_conn, question_text, result, evidence=evidence, plan=plan)
                return result
            result = AskHearthResult(
                status="unsupported", answer=_UNSUPPORTED_MESSAGE, source_summary="", entity_id=None,
            )
            _update_attention_frame(attention_frame, memory_conn, question_text, result)
            return result

        if routed.route == "needs_attention_today":
            result = _answer_needs_attention_today(memory_conn, gemini_client)
            _update_attention_frame(attention_frame, memory_conn, question_text, result)
            return result

        # tell_me_about_entity / connected_to_entity — both need entity resolution
        resolution = resolve_entity(memory_conn, routed.entity_query)

        if resolution.status == "not_found":
            result = AskHearthResult(
                status="not_found",
                answer=f"I couldn't find a Building matching '{routed.entity_query}'.",
                source_summary="",
                entity_id=None,
            )
            _update_attention_frame(attention_frame, memory_conn, question_text, result)
            return result

        if resolution.status == "ambiguous":
            names = ", ".join(resolution.candidate_names)
            result = AskHearthResult(
                status="ambiguous",
                answer=(
                    f"I found more than one Building matching '{routed.entity_query}': "
                    f"{names}. Which one did you mean?"
                ),
                source_summary="",
                entity_id=None,
            )
            _update_attention_frame(attention_frame, memory_conn, question_text, result)
            return result

        result = _answer_entity_question(
            resolution.entity_id,
            emphasize_connections=(routed.route == "connected_to_entity"),
            gemini_client=gemini_client,
        )
        _update_attention_frame(attention_frame, memory_conn, question_text, result)
        return result
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
    print("  All routing assertions passed (unwrapped input, unchanged baseline).")

    print("Step 1b: route_question() — outer-quote-wrapped input (regression test)")
    # Straight ASCII double quotes, one per fixed pattern.
    assert route_question('"Who needs attention today?"').route == "needs_attention_today"
    assert route_question('"What is connected to Ethan?"').route == "connected_to_entity"
    assert route_question('"What is connected to Ethan?"').entity_query == "Ethan"
    assert route_question('"Tell me about Ethan"').route == "tell_me_about_entity"
    assert route_question('"Tell me about Ethan"').entity_query == "Ethan"
    # Straight ASCII single quotes.
    assert route_question("'Who needs attention today?'").route == "needs_attention_today"
    assert route_question("'Tell me about Ethan'").route == "tell_me_about_entity"
    # Curly/smart double and single quotes (common browser/OS autocorrect output).
    assert route_question("“Who needs attention today?”").route == "needs_attention_today"
    assert route_question("“What is connected to Ethan?”").route == "connected_to_entity"
    assert route_question("‘Tell me about Ethan’").route == "tell_me_about_entity"
    # Whitespace just inside the wrapping quotes must still be handled.
    assert route_question('"  Who needs attention today?  "').route == "needs_attention_today"
    # Internal apostrophe, no outer quotes at all — must remain unaffected,
    # matching exactly as before this fix (the case named explicitly in the
    # bug report).
    result = route_question("What's connected to Ethan")
    assert result.route == "connected_to_entity"
    assert result.entity_query == "Ethan"
    print("  All quote-wrapped routing assertions passed.")

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

        # actor_role="manager" (authorized): this question is meant to prove
        # the *genuinely unsupported* path (no fixed pattern matches, and the
        # manager-advice gate finds zero entity mentions) — not to
        # accidentally exercise Phase 6's authorization refusal instead. An
        # omitted/unauthorized actor_role would fail closed as
        # "not_authorized" before reaching that gate at all, which is a
        # different code path than this assertion is testing.
        result = answer_question("What's the weather like?", memory_conn=conn, gemini_client=None, actor_role="manager")
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
