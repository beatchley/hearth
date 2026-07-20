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

Phase 5 addition: _construct_response() no longer asks Gemini to write the
final three-header prose directly. It now asks for a structured list of
individual assertions (category + text + evidence_quote), which
hearth_assertion_validation.py deterministically validates and corrects
before hearth_assertion_validation.render_assertions() builds the final
conversational text. See that module's docstring for the full validation
architecture (Grounded Assertions, Phase 5) — this module's tool-call
budget, eligibility gate, and retrieval planning are unchanged by it.

Phase 6 addition: this module is no longer authority-blind. Both
answer_manager_advice_question() and run_manager_advice_path() now require
an actor_role argument and refuse — before the eligibility gate, before any
tool call — unless it is one of AUTHORIZED_ACTOR_ROLES ("ceo", "manager",
"it"), the same literal role set /admin/hearth/ask already gates on at the
Flask route level. That route-level gate is unchanged and remains in place;
this is an additional, structural guarantee inside the cognitive path
itself, not a replacement for it. Coach/creator access is explicitly out of
scope. See the "1b. Authorization" section below and the Phase 6 completion
report.
"""

import json
import logging
import re
from dataclasses import dataclass
from typing import Optional

import hearth_assertion_validation
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
            + hearth_memory._exclude_evaluator_promoted_sql() +
            " ORDER BY observed_at DESC LIMIT ?;",
            (entity_id, *hearth_memory.EVALUATOR_PROMOTED_EPISODE_TYPES, limit),
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
# 1b. Authorization — Phase 6.
#
# /admin/hearth/ask (the only page that calls this path today) already gates
# access to ceo/manager/it at the Flask route level
# (pathway-portal/backend/app/routes/main.py: `if current_user.role not in
# ("ceo", "manager", "it")`). That gate is correct and unchanged by this
# phase. What Phase 6 closes is that this module itself — and
# run_manager_advice_path() specifically — trusted that check had already
# happened rather than ever confirming it, which meant any future caller
# (a new endpoint, a script, a scheduled job) could reach real Building
# evidence with zero authorization. This is now a structural guarantee
# enforced here, not an accident of "only one gated page currently calls
# this."
#
# AUTHORIZED_ACTOR_ROLES is the exact literal role set already used by that
# route — same three strings, same casing, deliberately not a new
# permission model, not pathway-portal's separate has_permission()/
# permissions.py granular-capability system (that system conflates this
# coarse ceo/manager/it check with arbitrary per-user granted permissions;
# see completion report). Coach and creator access are explicitly out of
# scope — do not add them here without a separate, deliberate policy
# decision.
#
# actor_role is checked before the eligibility gate and before any tool
# call — an unauthorized or missing actor_role never reaches
# check_entity_mentions(), classify_manager_advice_intent(), or any of the
# six tool wrappers. Omitting actor_role entirely fails closed (denied),
# the same as any other unrecognized role — this function has no implicit
# "trusted caller" path.
#
# Phase 7a: this authorization check is unaffected by, and runs before,
# any Attention Frame continuity input (fallback_entity_name/prior_evidence
# below) — a frame can help identify *who* a follow-up is about, never
# bypass whether the actor is allowed to ask at all.
# ===========================================================================

AUTHORIZED_ACTOR_ROLES = ("ceo", "manager", "it")

_NOT_AUTHORIZED_MESSAGE = "This isn't something Hearth can help with for this account."


def is_actor_authorized(actor_role: Optional[str]) -> bool:
    return actor_role in AUTHORIZED_ACTOR_ROLES


def _fail_closed_not_authorized() -> dict:
    return {
        "status": "not_authorized",
        "answer": _NOT_AUTHORIZED_MESSAGE,
        "source_summary": (
            "Manager-advice cognitive path — refused before any retrieval: "
            "actor is not authorized for advice-seeking questions."
        ),
        "entity_id": None,
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
    # True only when ineligibility was caused by classify_manager_advice_intent()
    # falling back on a genuine failure (missing client, call exception,
    # malformed JSON, invalid field types) rather than a real model response —
    # see that function's docstring. False whenever the classifier gave a
    # genuine answer (even a declining one) or never ran at all (e.g. the
    # entity-mention check itself failed). Lets a caller give an honest
    # "something went wrong" message instead of implying the question itself
    # is unsupported.
    classifier_failure: bool = False


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


def check_entity_mentions(memory_conn, text: str, fallback_entity_name: Optional[str] = None):
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

    fallback_entity_name (Phase 7a): the Attention Frame's currently-focused
    Building, if any — used only when the text names zero candidates at all
    ("none"). This is how a completely entity-less follow-up ("What do you
    think about that?") keeps referring to whichever Building the
    conversation already centers on, without the fixed-pattern regex routes
    or the capitalized-span heuristic needing to know anything about
    conversation history. It never overrides a name the text actually
    mentions — "one" and "multiple" are decided from the text alone, exactly
    as before this parameter existed.
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

    if fallback_entity_name:
        return "one", fallback_entity_name
    return "none", None


_ADVICE_CLASSIFIER_PROMPT = """You are classifying one manager message for Hearth, the organizational intelligence system used by Pathway Portal.
Your ONLY job is to decide whether this message is a manager seeking Hearth's advice, opinion, or judgment about a specific person ("{name}").

Answer "true" for a message asking Hearth to weigh in with its own judgment or opinion about {name}, including (but not limited to) phrasings like:
- "What do you think about {name}?"
- "What concerns you about {name}?"
- "What concerns you most about {name}?"
- "What stands out about {name}?"
- "How worried should I be about {name}?"
These are all judgment/opinion-seeking, not plain factual lookups — they ask Hearth to form and share a view, not just recite recorded facts.

Answer "false" if the message is instead:
- a request Hearth's existing fixed question types already handle, such as "tell me about {name}", "what's connected to {name}", or "who needs attention today"
- ordinary conversation, small talk, or a plain statement about {name} with no advice-seeking intent (e.g. praise, a simple factual note, a question about something unrelated)
- a plain factual question about {name} that has a factual answer, not a request for Hearth's judgment (e.g. "when did {name} last go live?")

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

    Never raises. Returns (is_advice_seeking: bool, confidence: float,
    genuine: bool); (False, 0.0, False) on any failure, missing client, or
    unparsable response — same conservative-default discipline as the
    comment classifier. `genuine` is True only when a real Gemini call
    returned a parseable, well-typed response; False for every
    fallback-on-failure path (missing client, call exception, malformed
    JSON, invalid field types). This is what lets a caller distinguish "the
    model genuinely said no" from "the classifier itself failed" — the
    (False, 0.0) pair alone cannot express that difference.

    Every call path logs exactly one result line (see _log below),
    including the successful ones — previously only failures were logged,
    which made it impossible to reconstruct why a past request was
    declined.

    Does NOT count against this path's tool-call budget — it is a gate, not
    retrieval; it reads no Hearth memory or organizational data at all.
    """
    def _log(is_seeking, confidence, genuine, reason):
        logger.info(
            "[manager_advice] classifier result: candidate=%r is_seeking=%s confidence=%.2f genuine=%s reason=%s",
            candidate_name, is_seeking, confidence, genuine, reason,
        )

    if gemini_client is None:
        _log(False, 0.0, False, "no_gemini_client")
        return False, 0.0, False

    prompt = _ADVICE_CLASSIFIER_PROMPT.format(name=candidate_name, text=text)
    try:
        response = gemini_client.models.generate_content(model=GEMINI_MODEL_NAME, contents=prompt)
        raw = response.text.strip() if response.text else ""
    except Exception as exc:
        logger.warning("[manager_advice] classifier Gemini call failed: %s", exc)
        _log(False, 0.0, False, "call_failed")
        return False, 0.0, False

    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        logger.warning("[manager_advice] classifier response not valid JSON: %r", raw[:200])
        _log(False, 0.0, False, "malformed_json")
        return False, 0.0, False

    is_seeking = parsed.get("is_manager_advice_seeking")
    confidence = parsed.get("confidence")
    if not isinstance(is_seeking, bool):
        _log(False, 0.0, False, "invalid_is_seeking_type")
        return False, 0.0, False
    try:
        confidence = float(confidence)
    except (TypeError, ValueError):
        _log(False, 0.0, False, "invalid_confidence_type")
        return False, 0.0, False
    confidence = max(0.0, min(1.0, confidence))
    _log(is_seeking, confidence, True, "model_classification")
    return is_seeking, confidence, True


def check_eligibility(
    memory_conn, text: str, gemini_client, fallback_entity_name: Optional[str] = None,
) -> EligibilityResult:
    """Run both halves of the eligibility gate. Both must pass.

    Consistent with HEARTH_CANONICAL_IDENTITY.md Section 10: when
    eligibility is unclear, do not proceed into the cognitive path.

    fallback_entity_name (Phase 7a): passed straight through to
    check_entity_mentions() — see that function's docstring. The
    classifier call below still runs, and still independently decides
    whether the (possibly frame-resolved) message is genuinely
    advice-seeking; continuity only helps identify *who*, never *whether*.
    """
    mention_status, name = check_entity_mentions(memory_conn, text, fallback_entity_name=fallback_entity_name)
    if mention_status != "one":
        return EligibilityResult(eligible=False, reason=f"entity_mention_check={mention_status}")

    is_seeking, confidence, genuine = classify_manager_advice_intent(text, name, gemini_client)
    if not is_seeking or confidence < CLASSIFIER_CONFIDENCE_THRESHOLD:
        return EligibilityResult(
            eligible=False, candidate_name=name, confidence=confidence,
            reason=f"classifier_declined(is_seeking={is_seeking}, confidence={confidence:.2f}, genuine={genuine})",
            classifier_failure=not genuine,
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
# Root cause (historical): several of hearth_soul.py's Worldview writers
# used to embed the raw numeric entity_id directly into stored free text
# instead of a resolved display name — e.g. _upsert_responsiveness_belief()'s
# `belief_text=f"Entity {entity} has shown episodes resolving..."`, and the
# same pattern in _upsert_entity_repeat_uncertainty() (uncertainty_text,
# possible_question), _upsert_creator_quiet_watch() (change_text), and
# _upsert_single_episode_uncertainty() (uncertainty_text, possible_question).
# This was inconsistent with _upsert_engagement_momentum_belief(), which
# already resolved to display_name correctly before writing. That
# inconsistency has since been fixed at the source in hearth_soul.py (all
# four writers now resolve display_name via _entity_display_name() before
# writing), so newly-written text no longer carries a raw id.
#
# This module keeps the rendering-time fix regardless: every tool's rendered
# text passes through _resolve_entity_id_mentions() before reaching a
# response, so any raw "Entity 31" reference already stored in older rows
# (written before the source-level fix) is still caught and resolved to
# "Ethan" wherever it appears — not just in belief text, in case the same
# leak class shows up
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


# Phase 5: initial attempt + one retry on a Category A (malformed structure)
# or Category B (ungrounded knowledge assertion) failure, per the Grounded
# Assertions build brief. Never more than one retry — see
# hearth_assertion_validation.close_out_ungrounded_knowledge()'s docstring
# for why a second failure converts rather than loops again.
_MAX_ASSERTION_ATTEMPTS = 2

_ASSERTION_PROMPT_TEMPLATE = """You are Hearth, forming an internal set of assertions to answer a manager's request for advice about {display_name}. Do not write your final prose yet — you are producing structured claims that a deterministic system will validate before any of this reaches the manager.

Manager's message: "{question_text}"

Your retrieval plan was:
- Goal: {goal}
- Already known: {known}
- Still to verify: {to_verify}

Retrieved evidence:
{evidence_block}

Known limitations for this answer:
{limitations_block}

Return ONLY a JSON object with one key, "assertions": a list of individual claims. Each item must have exactly these fields:
{{
  "category": "knowledge" | "reasoning" | "uncertainty",
  "text": "<one clear, natural sentence, the way a trusted colleague would say it>",
  "evidence_quote": "<exact substring copied character-for-character from the Retrieved Evidence above, or null>"
}}

Category rules:
- "knowledge": a claim you can point to directly in the Retrieved Evidence above. evidence_quote is REQUIRED and must be an exact, verbatim substring of the Retrieved Evidence — do not paraphrase it, do not fix typos or punctuation. If you cannot quote it exactly, do not use this category; use "uncertainty" instead.
- "reasoning": your own professional judgment or synthesis going beyond the raw evidence — e.g. what this pattern of evidence suggests, or advice about reaching out. This is where a real opinion belongs, since the manager asked "what do you think." evidence_quote must be null.
- "uncertainty": something you genuinely do not know or cannot verify given what's available, including anything from the Known limitations list above and the livestream-signal gap if it's listed. evidence_quote must be null.

Rules:
- Never invent or approximate an evidence_quote. A fabricated quote is worse than no knowledge claim at all — use "uncertainty" if you are not sure.
- Include at least one "reasoning" assertion and at least one "uncertainty" assertion. Include "knowledge" assertions only where the evidence above actually supports them.
- Do not blend categories within one assertion's text.
- Do not perform helpfulness with filler phrases ("it's worth noting", "I wanted to flag", "hope this helps"). Be warm, calm, and specific — the way a trusted colleague speaks, not a report.
- Return ONLY valid JSON, no markdown, no explanation.
"""


def _call_assertion_gemini(prompt: str, gemini_client):
    """Best-effort structured-JSON call for one assertion-generation attempt.
    Returns a parsed object, or None on any failure — validate_assertions()
    treats None the same as any other malformed shape (Category A)."""
    try:
        response = gemini_client.models.generate_content(model=GEMINI_MODEL_NAME, contents=prompt)
        raw = response.text.strip() if response.text else ""
    except Exception as exc:
        logger.warning("[manager_advice] assertion-generation Gemini call failed: %s", exc)
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        logger.warning("[manager_advice] assertion-generation response not valid JSON: %r", raw[:200])
        return None


def _construct_response(question_text, display_name, plan, evidence, limitations, gemini_client):
    """Phase 5 response construction: propose structured assertions, validate
    them deterministically, render only what survives validation. Returns
    (answer_text, ValidationOutcome) — the caller exposes the outcome on the
    result dict (mirroring how `plan` was already made inspectable in Phase 4)
    rather than only logging it.
    """
    evidence_pool = hearth_assertion_validation.build_evidence_pool(evidence, resolve_fn=_resolve_entity_id_mentions)

    if gemini_client is None:
        assertions = hearth_assertion_validation.deterministic_fallback_assertions(evidence_pool, limitations)
        outcome = hearth_assertion_validation.ValidationOutcome(
            ok=True, assertions=assertions, failure_category=None,
            notes=["gemini_unavailable: deterministic fallback assertions rendered"],
        )
        return hearth_assertion_validation.render_assertions(assertions), outcome

    prompt = _ASSERTION_PROMPT_TEMPLATE.format(
        display_name=display_name,
        question_text=question_text,
        goal=plan.get("goal", ""),
        known=plan.get("known", ""),
        to_verify=plan.get("to_verify", ""),
        evidence_block=_render_evidence_block(display_name, evidence),
        limitations_block="\n".join(f"- {item}" for item in limitations) if limitations else "- none",
    )

    outcome = None
    for attempt in range(_MAX_ASSERTION_ATTEMPTS):
        parsed = _call_assertion_gemini(prompt, gemini_client)
        outcome = hearth_assertion_validation.validate_assertions(parsed, evidence_pool)
        if outcome.failure_category not in ("A", "B"):
            break
        logger.info(
            "[manager_advice] assertion validation failure_category=%s on attempt %d: %s",
            outcome.failure_category, attempt + 1, outcome.notes,
        )

    if outcome.failure_category == "A":
        # Category A persisted through the retry: fall back to deterministic
        # assertions built straight from evidence, same as gemini_client=None
        # above — never render nothing, never render unvalidated model prose.
        assertions = hearth_assertion_validation.deterministic_fallback_assertions(evidence_pool, limitations)
        outcome = hearth_assertion_validation.ValidationOutcome(
            ok=True, assertions=assertions, failure_category=None,
            notes=outcome.notes + ["Category A persisted after retry: deterministic fallback assertions rendered instead."],
        )
    elif outcome.failure_category == "B":
        outcome = hearth_assertion_validation.close_out_ungrounded_knowledge(outcome)

    return hearth_assertion_validation.render_assertions(outcome.assertions), outcome


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

def run_manager_advice_path(
    question_text: str,
    candidate_name: str,
    gemini_client,
    actor_role: Optional[str] = None,
    prior_evidence: Optional[dict] = None,
    prior_entity_id: Optional[int] = None,
) -> dict:
    """Run the full cognitive path once eligibility has already been confirmed.

    Returns a dict with the same shape as hearth_ask.AskHearthResult's
    fields (status/answer/source_summary/entity_id), plus one extra key,
    "evidence", present only on a "success" return — the raw tool_name ->
    data dict this turn gathered/reused, made available so a caller can
    stash it in the Attention Frame for the *next* turn (hearth_ask.py
    strips this key back out before constructing AskHearthResult, which
    has no such field — this is an internal caller-to-caller signal, never
    rendered to a manager).

    Phase 6: re-checks authorization as its own first step, even though
    answer_manager_advice_question() (this function's only caller today)
    already checked it before running the eligibility gate. Nothing else
    calls this function directly today (confirmed during Phase 6's
    investigation), so this is deliberate defense-in-depth rather than a
    reachable gap being closed — see completion report. It keeps this
    function honest on its own terms rather than depending on every future
    caller to have gone through the one gate that currently exists.

    Phase 7a: prior_evidence/prior_entity_id let a caller offer up the
    Attention Frame's evidence from a previous turn about the same
    Building. Reused ONLY when prior_entity_id matches this turn's
    freshly-resolved entity_id (Step 1 below always re-resolves and
    re-authorizes from scratch — reuse never substitutes for either), and
    only for the four *optional* tools; resolve_building_by_name and
    get_building_context always run fresh every turn regardless, since
    they are this path's essential, deterministic, cheap grounding steps
    (see TOOL_REGISTRY comments) and the Building's existence/identity
    should never be assumed stale. Reuse reduces retrieval only — Step 6's
    deterministic Grounded Assertions validation still runs on whatever
    evidence (fresh or reused) ends up in play, every turn, unconditionally.
    """
    if not is_actor_authorized(actor_role):
        return _fail_closed_not_authorized()

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

    # Phase 7a: seed already-gathered optional-tool evidence from the
    # Attention Frame, but ONLY when it was gathered about this exact
    # Building — a subject change mid-conversation (prior_entity_id !=
    # entity_id) must never leak one Building's evidence into another's
    # answer. get_building_context is deliberately excluded from reuse
    # (see run_manager_advice_path docstring) — it was just re-fetched
    # fresh above unconditionally.
    reusable_evidence = {}
    if prior_evidence and prior_entity_id == entity_id:
        reusable_evidence = {
            k: v for k, v in prior_evidence.items() if k in OPTIONAL_TOOL_NAMES
        }

    # Step 4 (gather context, continued): execute the model's plan.
    # HEARTH_COGNITIVE_PROCESS.md Section 4's "stop earlier than the ceiling
    # whenever sufficient evidence has been gathered" is satisfied here by
    # the model's own one-shot selection (typically 1-2 of 4 optional tools,
    # i.e. 3-4 total calls) rather than by an iterative multi-round loop —
    # see the completion report for why this simplification was made.
    for tool_name in requested_tools:
        if tool_name in reusable_evidence:
            # Working Memory reducing unnecessary retrieval (Phase 7a): this
            # tool was already called for this same Building this session —
            # reuse its data instead of calling it again. Does not count
            # against tool_calls_made/the ceiling, since no call happened.
            # Grounded Assertions validation (Step 6) still runs on this
            # data exactly as if it had been freshly fetched.
            evidence[tool_name] = reusable_evidence[tool_name]
            trace.append({"tool": tool_name, "outcome": "reused_from_attention_frame"})
            continue
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

    # Step 6 (respond): propose assertions, validate deterministically
    # (Phase 5 — see hearth_assertion_validation.py), render only what
    # survives validation.
    answer_text, validation_outcome = _construct_response(
        question_text, display_name, plan, evidence, limitations, gemini_client,
    )

    checked = ", ".join(evidence.keys())
    source_summary = f"Manager-advice cognitive path ({tool_calls_made} tool call(s)) checked: {checked}."
    if len(limitations) > 1:  # index 0 is always the livestream-gap note
        source_summary += f" Limitations: {' '.join(limitations[1:])}"

    return {
        "status": "success",
        "answer": answer_text,
        "source_summary": source_summary,
        "entity_id": entity_id,
        # Phase 7a: the raw evidence dict this turn ended up with (fresh
        # and/or reused) — not part of AskHearthResult's shape, not
        # rendered to the manager; hearth_ask.py lifts this out to stash in
        # the Attention Frame for a possible next turn, then discards it
        # before constructing AskHearthResult.
        "evidence": evidence,
        # Made visible here, not just logged, so any caller — tests, a
        # future debug view, a human reviewing a past response — can see
        # what Hearth thought it needed to check, not just which tools it
        # ultimately called. Generation is unchanged; this only exposes the
        # already-generated plan object (see Step 3 above).
        "plan": plan,
        # Phase 5: internal validation record — never rendered to the
        # manager, but inspectable the same way `plan` already is. See
        # hearth_assertion_validation.ValidationOutcome.
        "validation": {
            "ok": validation_outcome.ok,
            "failure_category": validation_outcome.failure_category,
            "notes": validation_outcome.notes,
            "assertions": [
                {
                    "category": a.category, "text": a.text,
                    "evidence_quote": a.evidence_quote, "source": a.source,
                }
                for a in validation_outcome.assertions
            ],
        },
    }


def answer_manager_advice_question(
    question_text: str,
    memory_conn,
    gemini_client,
    actor_role: Optional[str] = None,
    fallback_entity_name: Optional[str] = None,
    prior_evidence: Optional[dict] = None,
    prior_entity_id: Optional[int] = None,
):
    """Top-level entry point hearth_ask.answer_question() calls whenever its
    three fixed-pattern routes did not match. Read-only; writes nothing.

    Returns (result_dict_or_None, EligibilityResult). result_dict is None
    when the eligibility gate did not pass — the caller must fall through to
    its own existing unsupported response unchanged in that case.

    Phase 6: authorization is checked first, before check_eligibility() —
    before the entity-mention scan, before the classifier call, before any
    of the six tool wrappers. An unauthorized (or omitted) actor_role
    returns a "not_authorized" result_dict immediately and None for the
    gate, since the eligibility gate never ran; this is distinct from every
    other status this function can return (success/not_found/ambiguous/
    error/unsupported-via-None) and must not be confused with any of them.

    Phase 7a: fallback_entity_name/prior_evidence/prior_entity_id are all
    optional Attention Frame continuity inputs, straight passthroughs to
    check_eligibility() and run_manager_advice_path() respectively — see
    those functions' docstrings. None of the three change what this
    function checks or in what order; a caller with no frame (all three
    None/absent) behaves identically to before this phase.
    """
    if not is_actor_authorized(actor_role):
        return _fail_closed_not_authorized(), None

    gate = check_eligibility(memory_conn, question_text, gemini_client, fallback_entity_name=fallback_entity_name)
    if not gate.eligible:
        return None, gate
    return run_manager_advice_path(
        question_text, gate.candidate_name, gemini_client, actor_role,
        prior_evidence=prior_evidence, prior_entity_id=prior_entity_id,
    ), gate
