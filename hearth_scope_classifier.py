"""
Hearth Scope Classifier — Phase 8.

Decides, for a question that none of Ask Hearth's three fixed-pattern
routes matched (hearth_ask.route_question() returned "unsupported"), which
of three lanes it belongs to:

    organizational      — may require Pathway/Hearth data, a Building, or
                           manager judgment grounded in internal evidence.
                           Routed to the existing, unchanged manager-advice
                           cognitive path (hearth_manager_advice.py).
    general_knowledge   — confidently answerable from stable, widely-known
                           information alone, with no organizational
                           reference at all. Routed to the new direct-model
                           general-knowledge lane (hearth_general_knowledge.py).
    uncertain_or_mixed  — anything else: low confidence, mixed signals, a
                           malformed/failed classification, or a question
                           whose meaning depends on organizational context
                           this classifier can't rule out. Never routed to
                           general knowledge; falls through to a
                           conservative unsupported/clarification response.

This is a separate, narrower gate from hearth_manager_advice.py's own
advice-seeking eligibility classifier (classify_manager_advice_intent) —
that one decides whether Hearth should attempt *advice*; this one decides
whether the question needs organizational context at all. A question can be
"organizational" here and still be declined by that inner gate (e.g. a
plain statement naming a Building with no advice-seeking intent) — this
module does not change that gate's behavior in any way.

Conservative failure rule (per the Phase 8 build brief): this module must
never resolve to general_knowledge except on a confident, well-formed model
verdict. A missing/failed/malformed classification, or a low-confidence
general_knowledge verdict, always falls back to organizational (when this
message directly mentions a known Building — "safer failure direction...
organizational handling when clearly appropriate") or uncertain_or_mixed
(otherwise) — never to general_knowledge. The Attention Frame's currently
focused Building is given to the model as context (see _build_signal_note)
but deliberately never feeds this deterministic fallback: a stale or
unrelated focus must never force an unconnected question into the
organizational lane merely because a classification call failed. A plain
statement or observation about a specific person (including the manager's
own self-disclosure, e.g. "My favorite food is cheeseburgers") is
deliberately treated as organizational, not general knowledge, per the
classifier prompt below — it is not a knowledge question at all, and Ask
Hearth's existing Conversation Ledger staging (Phase 7b) depends on this
kind of message continuing to reach the organizational branch.
"""

import json
import logging
from dataclasses import dataclass
from typing import Optional

from hearth_entity_resolution import find_entity_mentions
from hearth_gemini_config import GEMINI_MODEL_NAME

logger = logging.getLogger(__name__)

SCOPE_ORGANIZATIONAL = "organizational"
SCOPE_GENERAL_KNOWLEDGE = "general_knowledge"
SCOPE_UNCERTAIN_OR_MIXED = "uncertain_or_mixed"

_VALID_SCOPES = {SCOPE_ORGANIZATIONAL, SCOPE_GENERAL_KNOWLEDGE, SCOPE_UNCERTAIN_OR_MIXED}

# Same convention/value as hearth_manager_advice.CLASSIFIER_CONFIDENCE_THRESHOLD.
CONFIDENCE_THRESHOLD = 0.6


@dataclass
class ScopeClassification:
    scope: str
    confidence: float = 0.0
    reason: str = ""


_SCOPE_CLASSIFIER_PROMPT = """You are a routing classifier for Hearth, the organizational intelligence system used by Pathway Portal. Your ONLY job is to decide whether a message a Pathway manager typed into Ask Hearth needs Pathway/Hearth organizational knowledge to handle correctly, is pure general knowledge Hearth could answer the same way for anyone, or is genuinely uncertain/mixed.

{signal_note}

Message: "{text}"

Classify into exactly one of:
- "organizational": the message may require, or is about, a specific Building (person/creator/staff member), Pathway data, Hearth's own memory, organizational history, internal status, or a manager seeking judgment grounded in internal evidence. This also includes plain statements, observations, or self-disclosures about a specific person (even the manager themselves, e.g. "My favorite food is cheeseburgers") — those are organizational, not general knowledge, because they are not questions asking for pre-existing world knowledge at all.
- "general_knowledge": the message is confidently answerable using only stable, widely-known information (or an honest "I don't have current/live data for that"), with NO reference to Pathway, Hearth, a Building, the organization, or private context. This includes plain factual questions, unit conversions, definitions, general advice/suggestions, AND questions about live/current information (weather, sports results, news, prices, today's date) that have no organizational reference at all — Hearth will honestly decline the current-information part of those, but the question itself still doesn't need Pathway/Hearth data, so it is general_knowledge, not uncertain_or_mixed.
- "uncertain_or_mixed": you are not confident which of the above applies, the message combines a known organizational reference with a general concept, its meaning depends on context you don't have, or classifying it as general knowledge risks discarding relevant organizational context.

Bias toward "uncertain_or_mixed" over "general_knowledge" only when the message itself plausibly refers to the organization, a Building, or ongoing conversation context — not merely because Hearth can't verify a live fact. A missed general-knowledge answer costs nothing (the manager can ask again), but a wrongly general-knowledge answer to something organizational could ignore real Pathway/Hearth context — that is the risk to weigh against, not whether the fact itself is verifiable.

An active conversation may currently be focused on a specific Building (noted below, if so) — that is relevant ONLY if this message plausibly continues talking about that Building (e.g. a pronoun, or an unqualified follow-up). If this message is a self-contained question with no plausible connection to that Building or to the organization, the existing focus does not make it organizational.

Return ONLY valid JSON in this exact format:
{{
  "scope": "organizational" | "general_knowledge" | "uncertain_or_mixed",
  "confidence": 0.85
}}

Rules:
- confidence is a decimal between 0.0 and 1.0.
- Do not include markdown or explanation. Return JSON only.
"""


def _text_mention_names(memory_conn, text) -> list:
    """Deterministic organizational-signal check — a literal known-Building
    name scan against the message text itself (never fuzzy, never session
    history). This is the ONLY signal used for the conservative fallback
    scope below; the Attention Frame's current focus (see
    _frame_focus_note()) is informational context for the model only and
    deliberately never drives the fallback — a stale or unrelated focused
    Building must never make an unrelated question (e.g. general trivia
    asked mid-conversation about someone else) default to organizational
    when the classifier itself is unavailable.
    """
    try:
        known_rows = find_entity_mentions(memory_conn, text) if memory_conn is not None else []
    except Exception as exc:
        logger.warning("[scope_classifier] find_entity_mentions() failed, treating as no signal: %s", exc)
        known_rows = []
    return sorted({row["display_name"] for row in known_rows if row["display_name"]})


def _build_signal_note(text_mentions: list, attention_frame) -> str:
    lines = []
    if text_mentions:
        lines.append(f"Known Building name(s) mentioned directly in this message: {', '.join(text_mentions)}.")
    else:
        lines.append("No known Building name is mentioned directly in this message.")
    if attention_frame is not None and attention_frame.focused_entity_name:
        lines.append(
            f"The ongoing conversation is currently focused on Building: "
            f"{attention_frame.focused_entity_name} (from an earlier turn) — only treat this message as "
            "about them if it plausibly continues that topic (see instructions above)."
        )
    return " ".join(lines)


def classify_question_scope(memory_conn, text: str, gemini_client, attention_frame=None) -> ScopeClassification:
    """Classify one unmatched, already-authorized question's scope.

    Never raises. Every failure mode (no client, call failure, malformed
    JSON, invalid scope value, invalid confidence) falls back to
    organizational (if this message directly mentions a known Building) or
    uncertain_or_mixed (if not) — never to general_knowledge. See module
    docstring's Conservative failure rule. The Attention Frame's current
    focus is passed to the model as context but never drives this
    deterministic fallback — see _text_mention_names().
    """
    text_mentions = _text_mention_names(memory_conn, text)
    has_signal = bool(text_mentions)
    fallback_scope = SCOPE_ORGANIZATIONAL if has_signal else SCOPE_UNCERTAIN_OR_MIXED

    if gemini_client is None:
        return ScopeClassification(scope=fallback_scope, confidence=0.0, reason="gemini_unavailable")

    signal_note = _build_signal_note(text_mentions, attention_frame)
    prompt = _SCOPE_CLASSIFIER_PROMPT.format(signal_note=signal_note, text=text)

    try:
        response = gemini_client.models.generate_content(model=GEMINI_MODEL_NAME, contents=prompt)
        raw = response.text.strip() if response.text else ""
    except Exception as exc:
        logger.warning("[scope_classifier] Gemini call failed: %s: %s", type(exc).__name__, exc)
        return ScopeClassification(scope=fallback_scope, confidence=0.0, reason="gemini_call_failed")

    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        logger.warning("[scope_classifier] response not valid JSON: %r", raw[:200])
        return ScopeClassification(scope=fallback_scope, confidence=0.0, reason="malformed_json")

    scope = parsed.get("scope") if isinstance(parsed, dict) else None
    confidence = parsed.get("confidence") if isinstance(parsed, dict) else None

    if scope not in _VALID_SCOPES:
        return ScopeClassification(scope=fallback_scope, confidence=0.0, reason="invalid_scope_value")
    try:
        confidence = float(confidence)
    except (TypeError, ValueError):
        return ScopeClassification(scope=fallback_scope, confidence=0.0, reason="invalid_confidence_value")
    confidence = max(0.0, min(1.0, confidence))

    if scope == SCOPE_GENERAL_KNOWLEDGE and confidence < CONFIDENCE_THRESHOLD:
        return ScopeClassification(
            scope=SCOPE_UNCERTAIN_OR_MIXED, confidence=confidence,
            reason="low_confidence_general_knowledge_demoted",
        )

    if scope == SCOPE_ORGANIZATIONAL and confidence < CONFIDENCE_THRESHOLD:
        return ScopeClassification(
            scope=fallback_scope, confidence=confidence,
            reason="low_confidence_organizational_demoted_to_signal_default",
        )

    return ScopeClassification(scope=scope, confidence=confidence, reason="model")
