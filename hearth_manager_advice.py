"""
Hearth Manager-Advice Cognitive Path — Phase 4.

The first bounded cognitive path inside Ask Hearth. Handles exactly one
class of request: a manager seeking advice/judgment about one identifiable
Building, when none of Ask Hearth's three fixed-pattern routes matched
(hearth_ask.route_question() returned "unsupported"). Proven end-to-end on
the Toxie/Ethan scenario:

    "I noticed Ethan has not been going live much lately and I was
    thinking about reaching out to him. What do you think?"

Built on top of, and consistent with:
  - docs/HEARTH_CANONICAL_IDENTITY.md (v1.1)      — who Hearth is
  - docs/HEARTH_COGNITIVE_PROCESS.md (v1.0)       — how Hearth thinks
  - docs/HEARTH_TOOLSET_MANAGER_ADVICE_SCENARIO.md — the six tools below

GOVERNING RULE (stated here per the Phase 4 build brief, and enforced
throughout this module): partial grounded advice is allowed; ungrounded
confidence is not.

Read-only. This module never writes to any Hearth memory table — not
Furniture, not Worldview, not State, not Episodes, not Reflection. It only
reads, reasons, and (via Gemini, best-effort) phrases. If Gemini is
unavailable, a deterministic, still three-part raw rendering is returned
instead — never nothing, consistent with hearth_ask.py's existing
Gemini-failure discipline.

Tool-call budget: target 3-4 calls, hard ceiling of 5 (TOOL_CALL_CEILING
below), enforced in code regardless of what any model response requests.
The eligibility-gate classification call (classify_manager_advice_intent)
does NOT count against this budget — it is a gate, not retrieval.

Naming-collision note: HEARTH_COGNITIVE_TOOLS.md documented two different,
non-interchangeable functions both historically called
get_relationships_for_entity() (hearth_relationships.py's one-directional
reader vs. hearth_reader.py's both-directional one).
HEARTH_TOOLSET_MANAGER_ADVICE_SCENARIO.md proposed the distinct names
get_roads_from_entity() / get_roads_touching_entity() for a future registry.
Neither function is part of this module's six-tool set (get_building_context
below already surfaces Roads via hearth_traversal.get_connected_context()),
so the collision cannot leak into this registry — see TOOL_REGISTRY.

Does not modify: hearth_ask.py's three existing fixed-pattern routes,
hearth_traversal.py, hearth_context.py, hearth_soul.py, hearth_worldview.py,
hearth_entity_resolution.py, or any watcher/detector code.
"""

import json
import logging
import re
from dataclasses import dataclass
from typing import Optional

import hearth_memory
import hearth_traversal
import hearth_worldview
from hearth_entity_resolution import find_entity_mentions, resolve_entity
from hearth_gemini_config import GEMINI_MODEL_NAME

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 0. Tool-call budget
# ---------------------------------------------------------------------------

TOOL_CALL_CEILING = 5           # hard ceiling — never exceeded, under any circumstance
TOOL_CALL_TARGET_MAX = 4        # soft target this path aims for, not a floor to reach for


# ===========================================================================
# 1. Tool wrappers — uniform contract over the six selected tools
#
# Contract (per HEARTH_TOOLSET_MANAGER_ADVICE_SCENARIO.md Task 3 / the Phase
# 4 build brief):
#   - Primitive, JSON-safe inputs only.
#   - Plain dict/list outputs only — no raw sqlite3.Row objects.
#   - No caller-supplied database connection — each wrapper opens and closes
#     its own hearth_memory.db connection.
#   - Explicit outcome states: "ok" / "not_found" / "ambiguous" / "error".
#   - Bounded output (no unbounded result sets).
#   - Permission checks happen outside this module (the Flask route layer
#     already gates /admin/hearth/ask to ceo/manager/it) — never inside a
#     wrapper here.
#   - No hidden writes anywhere in the call chain. collect_relevant_principles()
#     and build_context() are excluded from this tool set entirely because of
#     their documented write side effects (mark_principle_used(),
#     last_briefed_at stamping) — see the completion report.
# ===========================================================================

def resolve_building_by_name(query: str) -> dict:
    """Tool: Resolve Building by Name. Wraps hearth_entity_resolution.resolve_entity().

    The mandatory first retrieval step for this scenario — every other tool
    below needs the entity_id this produces.

    Returns:
      {"status": "ok", "entity_id": int, "display_name": str, "error": None}
      {"status": "not_found", "entity_id": None, "error": None}
      {"status": "ambiguous", "entity_id": None, "candidate_names": [str], "error": None}
      {"status": "error", "entity_id": None, "error": str}
    """
    conn = hearth_memory.get_memory_connection()
    try:
        result = resolve_entity(conn, query)
        if result.status == "resolved":
            return {
                "status": "ok",
                "entity_id": result.entity_id,
                "display_name": result.entity_row["display_name"],
                "error": None,
            }
        if result.status == "ambiguous":
            return {
                "status": "ambiguous",
                "entity_id": None,
                "candidate_names": result.candidate_names,
                "error": None,
            }
        return {"status": "not_found", "entity_id": None, "error": None}
    except Exception as exc:
        return {"status": "error", "entity_id": None, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        conn.close()


def get_building_context(entity_id: int) -> dict:
    """Tool: One-Hop Connected Context. Wraps hearth_traversal.get_connected_context().

    Usable as-is per the tool-set doc (Group 1) — already self-manages its
    own connection and returns plain dicts. This wrapper only adds the
    uniform status/error envelope every tool here shares.

    The primary evidence source (essential tool) for this scenario: Identity
    summary, current State, recent Furniture, and one-hop Roads (including
    any coach) in one bounded call. Absence contract: an empty state/
    furniture/connections section is an authoritative "none recorded" — the
    same honesty-over-implication discipline hearth_ask.py's own rendering
    already uses, not a sign anything went wrong.

    Returns {"status": "ok"/"not_found"/"error", "data": dict|None, "error": str|None}.
    """
    try:
        context = hearth_traversal.get_connected_context(entity_id, max_neighbors=10)
        if "error" in context:
            status = "not_found" if context["error"] == "entity_not_found" else "error"
            return {"status": status, "data": None, "error": context["error"]}
        return {"status": "ok", "data": context, "error": None}
    except Exception as exc:
        return {"status": "error", "data": None, "error": f"{type(exc).__name__}: {exc}"}


# Cap matches the existing "recent 20" convention used by the Hearth City
# Episode Timeline panel (pathway-portal/backend/app/hearth_reader.py's
# get_episodes_for_entity()). hearth_ask.py lives in hearth/ and does not
# (and must not, per the codebase's one-directional dependency — pathway
# imports hearth/, never the reverse) import pathway-portal code, so this is
# a same-table, same-query local read rather than a wrapper around that
# Pathway-side function — the same "duplicate implementation, not a wrapper"
# pattern the catalog already documents elsewhere for the reverse-direction
# equivalents of resolve_entity_by_user_id()/get_entity_by_user_id().
_RECENT_EPISODES_LIMIT = 20


def get_recent_episodes(entity_id: int, limit: int = _RECENT_EPISODES_LIMIT) -> dict:
    """Tool: Recent Episodes for a Building (any status), most recent first.

    Absence contract: an empty list is an authoritative, meaningful signal —
    "no evidence of this in Hearth's memory" — not proof the underlying
    thing didn't happen (Hearth has no livestream-specific signal at all;
    see the module-level scenario-gap note in run_manager_advice_path()).

    Returns {"status": "ok"/"error", "data": [dict, ...]|None, "error": str|None}.
    """
    conn = hearth_memory.get_memory_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM hearth_episodes WHERE entity_id = ?"
            " ORDER BY observed_at DESC LIMIT ?;",
            (entity_id, limit),
        ).fetchall()
        return {"status": "ok", "data": [dict(r) for r in rows], "error": None}
    except Exception as exc:
        return {"status": "error", "data": None, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        conn.close()


_WORLDVIEW_ROW_LIMIT = 10  # safety bound; a single Building rarely has more


def get_active_beliefs(entity_id: int) -> dict:
    """Tool: Active Beliefs About a Subject. Wraps hearth_worldview.get_active_beliefs().

    Absence contract: an empty list means Hearth has not formed a settled
    belief (engagement_momentum / responsiveness) about this Building —
    authoritative about Hearth's own state of mind, not about the Building.

    Returns {"status": "ok"/"error", "data": [dict, ...]|None, "error": str|None}.
    """
    conn = hearth_memory.get_memory_connection()
    try:
        rows = hearth_worldview.get_active_beliefs(
            conn, subject_type="entity", subject_id=entity_id, limit=_WORLDVIEW_ROW_LIMIT,
        )
        return {"status": "ok", "data": [dict(r) for r in rows], "error": None}
    except Exception as exc:
        return {"status": "error", "data": None, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        conn.close()


def get_watched_changes(entity_id: int) -> dict:
    """Tool: Watched Changes. Wraps hearth_worldview.get_watched_changes().

    Absence contract: a *soft* absence — an empty list means Hearth's own
    quiet-duration/spike detection (hearth_soul.py) hasn't independently
    flagged a trend for this Building. This must not be read as reassurance
    that nothing is happening — only that Hearth hasn't caught it yet.

    Returns {"status": "ok"/"error", "data": [dict, ...]|None, "error": str|None}.
    """
    conn = hearth_memory.get_memory_connection()
    try:
        rows = hearth_worldview.get_watched_changes(
            conn, subject_type="entity", subject_id=entity_id, limit=_WORLDVIEW_ROW_LIMIT,
        )
        return {"status": "ok", "data": [dict(r) for r in rows], "error": None}
    except Exception as exc:
        return {"status": "error", "data": None, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        conn.close()


def get_living_uncertainties(entity_id: int) -> dict:
    """Tool: Open / Living Uncertainties. Wraps hearth_worldview.get_living_uncertainties().

    Absence contract: also a *soft* absence — an empty list means Hearth
    isn't already privately wondering about something regarding this
    Building, not that everything about them is settled or fine.

    Returns {"status": "ok"/"error", "data": [dict, ...]|None, "error": str|None}.
    """
    conn = hearth_memory.get_memory_connection()
    try:
        rows = hearth_worldview.get_living_uncertainties(
            conn, subject_type="entity", subject_id=entity_id, limit=_WORLDVIEW_ROW_LIMIT,
        )
        return {"status": "ok", "data": [dict(r) for r in rows], "error": None}
    except Exception as exc:
        return {"status": "error", "data": None, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        conn.close()


# Registry — every tool this cognitive path may call, under a unique,
# scenario-safe name. resolve_building_by_name and get_building_context are
# not in OPTIONAL_TOOL_NAMES: both are called deterministically (see
# run_manager_advice_path) rather than left to model selection, because
# they are this scenario's two essential tools (see Fail-Closed Behavior in
# the module docstring / completion report) — the model's actual decision
# surface is narrowed to which *supplementary* evidence to gather, not
# whether identity or primary grounding happen at all.
TOOL_REGISTRY = {
    "resolve_building_by_name": {
        "fn": resolve_building_by_name,
        "essential": True,
        "description": "Resolve a free-text name to exactly one Building.",
    },
    "get_building_context": {
        "fn": get_building_context,
        "essential": True,
        "description": (
            "Primary evidence source: Identity summary, current State, recent "
            "Furniture, and one-hop Roads (incl. any coach) for the Building."
        ),
    },
    "get_recent_episodes": {
        "fn": get_recent_episodes,
        "essential": False,
        "description": (
            "Up to 20 of the Building's most recent recorded episodes (any "
            "status), most recent first — the concrete evidence trail of "
            "what Hearth has actually observed."
        ),
    },
    "get_active_beliefs": {
        "fn": get_active_beliefs,
        "essential": False,
        "description": (
            "Hearth's own settled, confidence-scored interpretations about "
            "the Building (e.g. engagement or responsiveness), if any exist."
        ),
    },
    "get_watched_changes": {
        "fn": get_watched_changes,
        "essential": False,
        "description": (
            "Any directional trend (improving/worsening) Hearth is "
            "currently tracking for the Building, if any."
        ),
    },
    "get_living_uncertainties": {
        "fn": get_living_uncertainties,
        "essential": False,
        "description": (
            "Anything Hearth is already privately uncertain about "
            "regarding the Building, if any."
        ),
    },
}

OPTIONAL_TOOL_NAMES = [
    "get_recent_episodes", "get_active_beliefs", "get_watched_changes", "get_living_uncertainties",
]

_TOOL_LABELS = {
    "get_recent_episodes": "recent episodes",
    "get_active_beliefs": "active beliefs",
    "get_watched_changes": "watched changes",
    "get_living_uncertainties": "open uncertainties",
}


# ===========================================================================
# 2. Eligibility gate
#
# Two parts, both must pass. Neither counts retrieval — the entity-mention
# check is a text scan (no DB writes, cheap reads only), and the
# classification call is explicitly excluded from the tool-call budget.
# ===========================================================================

@dataclass
class EligibilityResult:
    eligible: bool
    candidate_name: Optional[str] = None
    reason: str = ""
    confidence: float = 0.0


# Part 1 falls back to this heuristic only when find_entity_mentions() (which
# can only ever recognize *already-known* Buildings) finds zero matches. This
# lets a mention of a real person Hearth simply hasn't recorded yet still
# reach the retrieval step, where "not found" is the honest, correct place to
# discover that (Fail-Closed Behavior: "Entity not found... do not proceed
# into retrieval" — that determination belongs to resolve_building_by_name(),
# not to the gate). It does not resolve names against Hearth's memory in any
# way — that remains exclusively hearth_entity_resolution.py's job — it only
# decides whether a name-shaped span exists in the text at all. See the
# completion report for why this exists and its known limitations (it will
# also match other capitalized proper nouns, e.g. "Hearth" or "Pathway",
# which is an accepted, documented limitation of this bounded scenario).
_PROPER_NOUN_RE = re.compile(r"^[A-Z][a-zA-Z']+$")


_SENTENCE_END_RE = re.compile(r"[.!?][\"')]*$")


def _extract_capitalized_spans(text: str) -> list:
    """Naive proper-noun heuristic — see module docstring above this
    function's caller for why it exists and its scope. Excludes every
    sentence-initial word (the first word of the text, and the first word
    after any ./!/? ), not just the first word of the whole input — a
    message with more than one sentence (e.g. "...reaching out. What do you
    think?") would otherwise misread an ordinary sentence-initial word like
    "What" as a candidate name.
    """
    words = (text or "").split()
    spans = []
    at_sentence_start = True
    for word in words:
        stripped = word.strip(".,!?;:\"'()")
        skip = at_sentence_start
        at_sentence_start = bool(_SENTENCE_END_RE.search(word))
        if skip or not stripped:
            continue
        if _PROPER_NOUN_RE.match(stripped) and not stripped.isupper():
            spans.append(stripped)
    seen, deduped = set(), []
    for s in spans:
        if s not in seen:
            seen.add(s)
            deduped.append(s)
    return deduped


def check_entity_mentions(memory_conn, text: str):
    """Deterministic half 1 of the eligibility gate.

    Returns (status, name) where status is "one" / "none" / "multiple".
    "one" means exactly one distinct Building name is being talked about —
    it does NOT guarantee that Building exists (see _extract_capitalized_spans
    docstring above); existence is confirmed later, at retrieval.

    Two same-named existing Buildings mentioned by the same text collapse to
    a single distinct *name* here (status "one") — that is a data-quality
    condition (duplicate display_name), not evidence the input names two
    different people, so it is deliberately allowed to reach the cognitive
    path, where resolve_building_by_name() will correctly surface it as
    "ambiguous" and stop before any further retrieval. Two *different* names
    mentioned (e.g. "Ethan and Sarah") correctly fail this check as multiple
    candidate Buildings, per the Phase 4 build brief.
    """
    known_rows = find_entity_mentions(memory_conn, text)
    distinct_names = sorted({row["display_name"] for row in known_rows if row["display_name"]})
    if len(distinct_names) == 1:
        return "one", distinct_names[0]
    if len(distinct_names) > 1:
        return "multiple", None

    spans = _extract_capitalized_spans(text)
    if len(spans) == 1:
        return "one", spans[0]
    if len(spans) > 1:
        return "multiple", None
    return "none", None


_ADVICE_CLASSIFIER_PROMPT = """You are classifying one manager message for Hearth, the organizational intelligence system used by Pathway Portal.
Your ONLY job is to decide whether this message is a manager seeking Hearth's advice, opinion, or judgment about a specific person ("{name}").

Answer "false" if the message is instead:
- a request Hearth's existing fixed question types already handle, such as "tell me about {name}", "what's connected to {name}", or "who needs attention today"
- ordinary conversation, small talk, or a plain statement about {name} with no advice-seeking intent (e.g. praise, a simple factual note, a question about something unrelated)

You are NOT deciding whether the advice can actually be given. You are NOT answering the message. You are NOT extracting or verifying any facts about {name}.

Message: "{text}"

Return ONLY valid JSON in this exact format:
{{
  "is_manager_advice_seeking": true,
  "confidence": 0.85
}}

Rules:
- confidence is a decimal between 0.0 and 1.0.
- If uncertain, prefer is_manager_advice_seeking: false with a lower confidence rather than guessing true — a missed advice request can be asked again; a wrongly-triggered one spends effort investigating the wrong thing.
- Do not include markdown or explanation. Return JSON only.
"""

CLASSIFIER_CONFIDENCE_THRESHOLD = 0.6


def classify_manager_advice_intent(text: str, candidate_name: str, gemini_client):
    """Part 2 of the eligibility gate — narrow, single-purpose Gemini call,
    modeled directly on hearth_comment_classifier.classify_training_comment()
    (structured JSON output, conservative bias, confidence score).

    Never raises. Returns (is_advice_seeking: bool, confidence: float);
    (False, 0.0) on any failure, missing client, or unparsable response —
    same conservative-default discipline as the comment classifier.

    Does NOT count against this path's tool-call budget — it is a gate, not
    retrieval; it reads no Hearth memory or organizational data at all.
    """
    if gemini_client is None:
        return False, 0.0

    prompt = _ADVICE_CLASSIFIER_PROMPT.format(name=candidate_name, text=text)
    try:
        response = gemini_client.models.generate_content(model=GEMINI_MODEL_NAME, contents=prompt)
        raw = response.text.strip() if response.text else ""
    except Exception as exc:
        logger.warning("[manager_advice] classifier Gemini call failed: %s", exc)
        return False, 0.0

    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        logger.warning("[manager_advice] classifier response not valid JSON: %r", raw[:200])
        return False, 0.0

    is_seeking = parsed.get("is_manager_advice_seeking")
    confidence = parsed.get("confidence")
    if not isinstance(is_seeking, bool):
        return False, 0.0
    try:
        confidence = float(confidence)
    except (TypeError, ValueError):
        return False, 0.0
    confidence = max(0.0, min(1.0, confidence))
    return is_seeking, confidence


def check_eligibility(memory_conn, text: str, gemini_client) -> EligibilityResult:
    """Run both halves of the eligibility gate. Both must pass.

    Consistent with HEARTH_CANONICAL_IDENTITY.md Section 10: when
    eligibility is unclear, do not proceed into the cognitive path.
    """
    mention_status, name = check_entity_mentions(memory_conn, text)
    if mention_status != "one":
        return EligibilityResult(eligible=False, reason=f"entity_mention_check={mention_status}")

    is_seeking, confidence = classify_manager_advice_intent(text, name, gemini_client)
    if not is_seeking or confidence < CLASSIFIER_CONFIDENCE_THRESHOLD:
        return EligibilityResult(
            eligible=False, candidate_name=name, confidence=confidence,
            reason=f"classifier_declined(is_seeking={is_seeking}, confidence={confidence:.2f})",
        )

    return EligibilityResult(eligible=True, candidate_name=name, confidence=confidence, reason="eligible")


# ===========================================================================
# 3. Cognitive loop — retrieval planning, gathering, reasoning, validation,
#    response construction (HEARTH_COGNITIVE_PROCESS.md Sections 2-7).
# ===========================================================================

_PLAN_PROMPT_TEMPLATE = """You are Hearth's cognitive path for answering a manager's request for advice about one specific Building (person) in the organization. A gate has already confirmed this input names exactly this one Building and is a genuine request for your advice or judgment.

Building already identified: {display_name}
Manager's message: "{question_text}"

You already have this Building's core context (identity summary, current state, recent furniture, one-hop relationships). You may additionally request any of the following tools — request only what you genuinely need to ground a useful answer, not everything "just in case":

{tool_list_block}

Respond with ONLY valid JSON in this exact shape:
{{
  "plan": {{
    "goal": "<one sentence: what you are trying to determine>",
    "known": "<one sentence: what you already know without calling anything else>",
    "to_verify": "<one sentence: what you still need to check, if anything>"
  }},
  "tool_calls": ["<tool_name>", "<tool_name>"]
}}

Rules:
- tool_calls must be a JSON list of 0 to 3 tool names, drawn only from the names listed above, in the order you would want them called.
- Prefer the smallest set that would let you ground a genuinely useful answer.
- Do not repeat a tool name.
- No text outside the JSON.
"""


def _default_plan(display_name: str) -> dict:
    return {
        "goal": f"Determine whether Hearth has grounded evidence to help answer the manager's question about {display_name}.",
        "known": f"{display_name} has been identified and their core Building context has been retrieved.",
        "to_verify": "Whether Hearth's own recorded evidence (episodes, beliefs, watched changes) supports or complicates the manager's observation.",
    }


_DEFAULT_OPTIONAL_TOOLS = ["get_recent_episodes", "get_active_beliefs", "get_watched_changes"]


def _get_plan_and_tool_selection(question_text: str, display_name: str, gemini_client):
    """The first cognitive-loop response: plan + first requested tool call(s)
    in a single response, per HEARTH_COGNITIVE_PROCESS.md Section 4 — no
    separate, preceding planning-only call. If Gemini is unavailable or the
    call fails, falls back to a deterministic default plan and tool
    selection rather than stalling (never nothing).
    """
    default_plan, default_tools = _default_plan(display_name), _DEFAULT_OPTIONAL_TOOLS
    if gemini_client is None:
        return default_plan, default_tools, "gemini_unavailable"

    tool_list_block = "\n".join(
        f"- {name}: {TOOL_REGISTRY[name]['description']}" for name in OPTIONAL_TOOL_NAMES
    )
    prompt = _PLAN_PROMPT_TEMPLATE.format(
        display_name=display_name, question_text=question_text, tool_list_block=tool_list_block,
    )
    try:
        response = gemini_client.models.generate_content(model=GEMINI_MODEL_NAME, contents=prompt)
        raw = response.text.strip() if response.text else ""
        parsed = json.loads(raw)
        plan = parsed.get("plan")
        if not isinstance(plan, dict):
            plan = default_plan
        tools = parsed.get("tool_calls")
        if not isinstance(tools, list):
            tools = default_tools
        tools = [t for t in tools if isinstance(t, str)]
        return plan, tools, "model"
    except Exception as exc:
        logger.warning("[manager_advice] plan/tool-selection call failed, using deterministic default: %s", exc)
        return default_plan, default_tools, "fallback_after_error"


# --- Response construction -------------------------------------------------

def _render_building_context(display_name: str, context: dict) -> list:
    summary = (context or {}).get("summary") or {}
    lines = [f"Building: {display_name}"]
    for label, key in [
        ("Summary", "summary"), ("Patterns noticed", "patterns_noticed"),
        ("Concerns", "concerns"), ("Strengths", "strengths"),
    ]:
        value = summary.get(key)
        lines.append(f"{label}: {value}" if value else f"{label}: none recorded")

    state = summary.get("state") or {}
    lines.append(
        "Current state: " + ("; ".join(f"{k}={v}" for k, v in state.items()) if state else "none recorded")
    )

    furniture = summary.get("furniture") or []
    if furniture:
        lines.append("Recent furniture facts:")
        for fact in furniture:
            lines.append(f"  - {fact['fact_text']} ({fact['fact_type']})")
    else:
        lines.append("Furniture facts: none recorded")

    connections = (context or {}).get("connections") or []
    if connections:
        lines.append(f"One-hop connections ({context.get('total_neighbors', 0)} total):")
        for conn in connections:
            road, building = conn["road"], conn["building"]
            arrow = "->" if road["direction"] == "outgoing" else "<-"
            lines.append(f"  - {arrow} {building.get('name')} (relationship: {road.get('type') or 'unspecified'})")
    else:
        lines.append("One-hop connections: none recorded.")
    return lines


def _render_episodes(episodes: list) -> list:
    if not episodes:
        return ["Recent episodes: none recorded (no evidence in Hearth's memory of the described pattern — not proof it isn't happening)."]
    lines = [f"Recent episodes ({len(episodes)} shown, most recent first):"]
    for ep in episodes:
        status = "resolved" if ep.get("resolved") else "open"
        desc = ep.get("description") or ""
        lines.append(f"  - [{ep.get('episode_type')}] {status}, observed {ep.get('observed_at')}: {desc}".rstrip())
    return lines


def _render_beliefs(beliefs: list) -> list:
    if not beliefs:
        return ["Active beliefs about this Building: none — Hearth has not formed a settled belief here."]
    lines = ["Active beliefs Hearth already holds:"]
    for b in beliefs:
        lines.append(f"  - [{b.get('belief_type')}] {b.get('belief_text')} (confidence {b.get('confidence')})")
    return lines


def _render_watched_changes(changes: list) -> list:
    if not changes:
        return ["Watched changes: none currently tracked — this does not mean nothing is happening, only that Hearth's own detection hasn't flagged a trend yet."]
    lines = ["Changes Hearth is currently watching:"]
    for c in changes:
        lines.append(f"  - [{c.get('direction')}] {c.get('change_text')} (confidence {c.get('confidence')})")
    return lines


def _render_uncertainties(uncertainties: list) -> list:
    if not uncertainties:
        return ["Open uncertainties: none — Hearth was not already privately wondering about this."]
    lines = ["Uncertainties Hearth already holds:"]
    for u in uncertainties:
        lines.append(f"  - {u.get('uncertainty_text')} (priority {u.get('priority')})")
    return lines


_EVIDENCE_RENDERERS = {
    "get_recent_episodes": _render_episodes,
    "get_active_beliefs": _render_beliefs,
    "get_watched_changes": _render_watched_changes,
    "get_living_uncertainties": _render_uncertainties,
}


# --- Raw entity-ID leak fix ---------------------------------------------
#
# Root cause: several of hearth_soul.py's Worldview writers embed the raw
# numeric entity_id directly into stored free text instead of a resolved
# display name — e.g. _upsert_responsiveness_belief()'s
# `belief_text=f"Entity {entity} has shown episodes resolving..."`, and the
# same pattern in _upsert_entity_repeat_uncertainty() (uncertainty_text,
# possible_question) and _upsert_creator_quiet_watch() (change_text). This
# is inconsistent with _upsert_engagement_momentum_belief(), which already
# resolves to display_name correctly before writing. That inconsistency
# lives in hearth_soul.py's write path and is explicitly out of scope here
# (see module docstring / completion report) — this module never writes
# Worldview and does not touch how that text is stored.
#
# This is a rendering-time fix: every tool's rendered text passes through
# _resolve_entity_id_mentions() before reaching a response, so a raw
# "Entity 31" reference is caught and resolved to "Ethan" wherever it
# appears — not just in belief text, in case the same leak class shows up
# in any of the other five tools' output in the future.
_ENTITY_ID_MENTION_RE = re.compile(r"\bEntity\s+(\d+)\b", re.IGNORECASE)


def _resolve_entity_id_mentions(text: str) -> str:
    """Replace every raw 'Entity <id>' reference in text with its resolved
    display name. Leaves text untouched if it contains no such reference.
    A referenced id that no longer resolves (e.g. a deleted Building) is
    rendered as "an unresolved Building (id N)" rather than silently
    dropped or invented — consistent with never fabricating a name.
    """
    if not text or not _ENTITY_ID_MENTION_RE.search(text):
        return text

    cache = {}
    conn = hearth_memory.get_memory_connection()
    try:
        def _replace(match):
            raw_id = match.group(1)
            if raw_id not in cache:
                row = conn.execute(
                    "SELECT display_name FROM hearth_entities WHERE id = ?;", (int(raw_id),),
                ).fetchone()
                cache[raw_id] = row["display_name"] if row and row["display_name"] else None
            return cache[raw_id] or f"an unresolved Building (id {raw_id})"

        return _ENTITY_ID_MENTION_RE.sub(_replace, text)
    except Exception as exc:
        logger.warning("[manager_advice] entity-id-mention resolution failed, leaving text as-is: %s", exc)
        return text
    finally:
        conn.close()


def _render_evidence_block(display_name: str, evidence: dict) -> str:
    lines = _render_building_context(display_name, evidence.get("get_building_context") or {})
    for tool_name in OPTIONAL_TOOL_NAMES:
        if tool_name in evidence:
            lines += _EVIDENCE_RENDERERS[tool_name](evidence[tool_name])
    return _resolve_entity_id_mentions("\n".join(lines))


# This scenario-specific gap (HEARTH_TOOLSET_MANAGER_ADVICE_SCENARIO.md
# Task 4) applies to every answer this path gives, not just when episodes
# happen to be empty — it must always be disclosed, not just on absence.
_LIVESTREAM_GAP_NOTE = (
    "Hearth has no livestream-specific activity signal — only general "
    "quietness detectors (episodes, engagement belief, watched changes) "
    "that respond to any drop in tracked activity, not specifically to "
    "going live less often. Hearth cannot independently confirm or refute "
    "that specific part of the manager's observation."
)


def _render_raw_three_part_text(display_name: str, plan: dict, evidence: dict, limitations: list) -> str:
    """Deterministic fallback rendering — used verbatim if Gemini is
    unavailable or fails. Preserves the Hearth Knowledge / General Reasoning
    / Uncertainty separation even with zero model involvement, per
    HEARTH_COGNITIVE_PROCESS.md Section 7 (this separation is settled
    architecture, not an implementation detail Gemini's absence may collapse).
    """
    lines = ["Hearth Knowledge:", _render_evidence_block(display_name, evidence), ""]
    lines += [
        "General Reasoning:",
        "Hearth's reasoning layer wasn't available to synthesize an opinion this "
        "time — the evidence above is shown as retrieved, without added interpretation.",
        "",
    ]
    lines.append("Uncertainty:")
    for item in limitations:
        lines.append(f"- {item}")
    return "\n".join(lines)


_RESPONSE_PROMPT_TEMPLATE = """You are Hearth, answering a manager's request for advice about {display_name}. Use ONLY the retrieved evidence below — do not invent facts, and do not assume anything the evidence doesn't support.

Manager's message: "{question_text}"

Your retrieval plan was:
- Goal: {goal}
- Already known: {known}
- Still to verify: {to_verify}

Retrieved evidence:
{evidence_block}

Known limitations for this answer:
{limitations_block}

Write your answer in three clearly separated parts, in this order, using these exact three headers on their own line:

Hearth Knowledge:
Only what is directly grounded in the retrieved evidence above — cite specifics (episode types, dates, state values, beliefs, watched changes) rather than vague summary. If a category has no recorded signal, say so plainly rather than omitting it.

General Reasoning:
Your own judgment or synthesis going beyond the raw evidence above — e.g. what this pattern of evidence suggests, or general advice about reaching out. Make clear this is your own reasoning, not something retrieved from Hearth's memory. This is where a real opinion belongs, since the manager asked "what do you think."

Uncertainty:
What remains genuinely unclear or unverifiable given what's available, including anything in the limitations list above and the livestream-signal gap if it's listed.

Do not blend these three sections together. Do not perform helpfulness with filler phrases ("it's worth noting", "I wanted to flag", "hope this helps"). Be warm, calm, specific, and honest about limits — the way a trusted colleague speaks, not a report.
"""


def _construct_response(question_text, display_name, plan, evidence, limitations, gemini_client):
    raw_text = _render_raw_three_part_text(display_name, plan, evidence, limitations)
    if gemini_client is None:
        return raw_text

    prompt = _RESPONSE_PROMPT_TEMPLATE.format(
        display_name=display_name,
        question_text=question_text,
        goal=plan.get("goal", ""),
        known=plan.get("known", ""),
        to_verify=plan.get("to_verify", ""),
        evidence_block=_render_evidence_block(display_name, evidence),
        limitations_block="\n".join(f"- {item}" for item in limitations) if limitations else "- none",
    )
    try:
        response = gemini_client.models.generate_content(model=GEMINI_MODEL_NAME, contents=prompt)
        text = response.text.strip() if response.text else None
    except Exception as exc:
        logger.warning("[manager_advice] response-construction Gemini call failed, using raw text: %s", exc)
        text = None
    return text if text else raw_text


# --- Fail-closed builders ---------------------------------------------------

def _trace_summary(trace: list) -> str:
    parts = [f"{t['tool']}={t['outcome']}" for t in trace]
    return "Manager-advice cognitive path — tool calls: " + ", ".join(parts) + "."


def _fail_closed_not_found(name: str, trace: list) -> dict:
    return {
        "status": "not_found",
        "answer": (
            f"I couldn't find a Building matching '{name}', so I don't have anything grounded "
            "to base advice on. If they're a real person here, they may not be tracked in "
            "Hearth's memory yet."
        ),
        "source_summary": _trace_summary(trace),
        "entity_id": None,
    }


def _fail_closed_ambiguous(name: str, candidate_names: list, trace: list) -> dict:
    names = ", ".join(candidate_names)
    return {
        "status": "ambiguous",
        "answer": f"I found more than one Building matching '{name}': {names}. Which one did you mean?",
        "source_summary": _trace_summary(trace),
        "entity_id": None,
    }


def _fail_closed_essential_error(display_name: str, error: str, trace: list, entity_id=None) -> dict:
    return {
        "status": "error",
        "answer": (
            f"I found {display_name}, but couldn't retrieve enough verified context to "
            "responsibly offer a judgment right now. I don't want to guess."
        ),
        "source_summary": _trace_summary(trace) + f" Essential retrieval failed: {error}",
        "entity_id": entity_id,
    }


# --- Main entry point --------------------------------------------------------

def run_manager_advice_path(question_text: str, candidate_name: str, gemini_client) -> dict:
    """Run the full cognitive path once eligibility has already been confirmed.

    Returns a dict with the same shape as hearth_ask.AskHearthResult's
    fields (status/answer/source_summary/entity_id) — hearth_ask.py
    constructs the dataclass from this.
    """
    trace = []
    tool_calls_made = 0

    # Step 1 (mandatory, deterministic, essential): resolve the Building.
    resolve_result = resolve_building_by_name(candidate_name)
    tool_calls_made += 1
    trace.append({"tool": "resolve_building_by_name", "outcome": resolve_result["status"]})

    if resolve_result["status"] == "not_found":
        return _fail_closed_not_found(candidate_name, trace)
    if resolve_result["status"] == "ambiguous":
        return _fail_closed_ambiguous(candidate_name, resolve_result["candidate_names"], trace)
    if resolve_result["status"] == "error":
        return _fail_closed_essential_error(candidate_name, resolve_result["error"], trace)

    entity_id = resolve_result["entity_id"]
    display_name = resolve_result["display_name"]

    # Step 2 (mandatory, deterministic, essential/primary evidence source).
    context_result = get_building_context(entity_id)
    tool_calls_made += 1
    trace.append({"tool": "get_building_context", "outcome": context_result["status"]})

    if context_result["status"] != "ok":
        return _fail_closed_essential_error(
            display_name, context_result["error"] or "building context unavailable", trace, entity_id=entity_id,
        )

    # Step 3: retrieval planning + first requested tool call(s), one combined
    # response (HEARTH_COGNITIVE_PROCESS.md Section 4). Logged so the plan is
    # inspectable, not just internal.
    plan, requested_tools, plan_source = _get_plan_and_tool_selection(question_text, display_name, gemini_client)
    logger.info(
        "[manager_advice] plan for entity_id=%s (source=%s): %s | requested tools: %s",
        entity_id, plan_source, plan, requested_tools,
    )

    remaining_budget = TOOL_CALL_CEILING - tool_calls_made
    requested_tools = [t for t in requested_tools if t in OPTIONAL_TOOL_NAMES]
    seen, deduped = set(), []
    for t in requested_tools:
        if t not in seen:
            seen.add(t)
            deduped.append(t)
    requested_tools = deduped[: max(0, min(TOOL_CALL_TARGET_MAX - 2, remaining_budget))]

    evidence = {"get_building_context": context_result["data"]}
    limitations = [_LIVESTREAM_GAP_NOTE]

    # Step 4 (gather context, continued): execute the model's plan.
    # HEARTH_COGNITIVE_PROCESS.md Section 4's "stop earlier than the ceiling
    # whenever sufficient evidence has been gathered" is satisfied here by
    # the model's own one-shot selection (typically 1-2 of 4 optional tools,
    # i.e. 3-4 total calls) rather than by an iterative multi-round loop —
    # see the completion report for why this simplification was made.
    for tool_name in requested_tools:
        if tool_calls_made >= TOOL_CALL_CEILING:
            limitations.append("Stopped before calling every planned tool: hard ceiling of 5 tool calls reached.")
            break
        fn = TOOL_REGISTRY[tool_name]["fn"]
        result = fn(entity_id)
        tool_calls_made += 1
        trace.append({"tool": tool_name, "outcome": result["status"]})
        if result["status"] == "ok":
            evidence[tool_name] = result["data"]
        else:
            # Nonessential tool failure: continue with remaining evidence,
            # explicitly disclose the limitation — never silently drop it.
            limitations.append(
                f"Could not check {_TOOL_LABELS[tool_name]} ({result['error']}); "
                "continuing with the rest of the evidence gathered."
            )

    # Step 5 (validate): the essential checks already gated above (resolve,
    # building context); what remains here is confirming the budget was
    # respected and every limitation encountered is carried into the
    # response rather than smoothed over (HEARTH_COGNITIVE_PROCESS.md
    # Section 6 — an internal check, not a second model call).
    assert tool_calls_made <= TOOL_CALL_CEILING, "tool-call ceiling must never be exceeded"

    # Step 6 (respond): three-way separated construction.
    answer_text = _construct_response(question_text, display_name, plan, evidence, limitations, gemini_client)

    checked = ", ".join(evidence.keys())
    source_summary = f"Manager-advice cognitive path ({tool_calls_made} tool call(s)) checked: {checked}."
    if len(limitations) > 1:  # index 0 is always the livestream-gap note
        source_summary += f" Limitations: {' '.join(limitations[1:])}"

    return {
        "status": "success",
        "answer": answer_text,
        "source_summary": source_summary,
        "entity_id": entity_id,
        # Made visible here, not just logged, so any caller — tests, a
        # future debug view, a human reviewing a past response — can see
        # what Hearth thought it needed to check, not just which tools it
        # ultimately called. Generation is unchanged; this only exposes the
        # already-generated plan object (see Step 3 above).
        "plan": plan,
    }


def answer_manager_advice_question(question_text: str, memory_conn, gemini_client):
    """Top-level entry point hearth_ask.answer_question() calls whenever its
    three fixed-pattern routes did not match. Read-only; writes nothing.

    Returns (result_dict_or_None, EligibilityResult). result_dict is None
    when the eligibility gate did not pass — the caller must fall through to
    its own existing unsupported response unchanged in that case.
    """
    gate = check_eligibility(memory_conn, question_text, gemini_client)
    if not gate.eligible:
        return None, gate
    return run_manager_advice_path(question_text, gate.candidate_name, gemini_client), gate
