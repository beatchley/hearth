"""
Hearth General-Knowledge Lane — Phase 8.

Answers a question hearth_scope_classifier.py has confidently classified as
pure general knowledge (SCOPE_GENERAL_KNOWLEDGE): one direct model call, in
Hearth's voice, with no organizational retrieval of any kind. This module
never touches hearth_memory.db, hearth_traversal.py, hearth_context.py, or
any Building/entity data — it receives only the already-normalized question
text and a Gemini client.

Architecture rule (same as hearth_ask.py's entity/needs-attention routes,
and hearth_manager_advice.py's cognitive path): the model is the voice
layer only, never a source of organizational fact — but unlike those two,
there is no retrieved organizational context to phrase here at all. The
model's own general knowledge *is* the content, by design, for this lane
only, because hearth_scope_classifier.py already confirmed the question
needs none of Hearth's organizational grounding.

Time-sensitive/current-information questions (weather, live scores,
today's news, current prices, etc.) are still routed here — they don't
need organizational data either — but the voice instruction below
explicitly tells the model to say plainly that it doesn't have verified
current information rather than answer as if its training knowledge were
live. See docs test coverage for "What's the weather in Dallas today?" and
"Who won last night's game?".

Model failure (no client, call failure, empty response) never fabricates a
deterministic factual fallback — it returns an honest, plain inability
message instead. See answer_general_knowledge_question()'s `ok` field.
"""

import logging
from dataclasses import dataclass

from hearth_gemini_config import GEMINI_MODEL_NAME

logger = logging.getLogger(__name__)

UNAVAILABLE_MESSAGE = (
    "I can't put together a reliable answer to that right now — try again in a moment."
)

# Deliberately minimal — no "Hearth Knowledge" / "General Reasoning" /
# "Uncertainty" headings (those belong to the manager-advice assertion
# contract, hearth_assertion_validation.py, and would make a one-line
# answer sound like a report). No mention of Gemini, models, prompts,
# lanes, classifiers, or retrieval — see Canonical Identity Section 8
# (Communication Philosophy): transparent about what Hearth is, never
# about the mechanism behind a specific message.
_VOICE_INSTRUCTION = (
    "You are Hearth, speaking directly to a Pathway manager who just asked you a plain "
    "question. Answer using your own general knowledge only — this question does not "
    "involve Pathway, Hearth's memory, or any specific Building/creator, so do not "
    "reference any of those or imply you checked any records.\n\n"
    "Speak the way a trusted, calm colleague would: direct, natural, and concise by "
    "default — confident when the answer is stable and well known, honest when you're "
    "genuinely not sure. Never perform helpfulness with filler phrases (\"it's worth "
    "noting\", \"I hope this helps\", \"please let me know\"). Never mention that you are "
    "an AI, a model, a system, or how you produced the answer — just answer, the way a "
    "person would.\n\n"
    "If the question asks for information that changes over time and you cannot verify "
    "it right now — current weather, live scores or results, today's news, current "
    "prices, today's date, or anything else that requires up-to-the-moment data — say "
    "plainly that you don't have verified current information for that, rather than "
    "guessing or presenting older training knowledge as if it were current.\n\n"
    "Do not use headings, labels, or bullet formatting unless the question specifically "
    "calls for a list. Keep it as short as it can be while still being genuinely useful "
    "— often one or two sentences is enough."
)

_PROMPT_TEMPLATE = """{instruction}

Manager's question: "{question_text}"

Answer now, in Hearth's voice, as described above.
"""


@dataclass
class GeneralKnowledgeAnswer:
    ok: bool
    answer: str


def answer_general_knowledge_question(question_text: str, gemini_client) -> GeneralKnowledgeAnswer:
    """One direct, best-effort model call. Never raises.

    ok=False (with UNAVAILABLE_MESSAGE) whenever the model is missing,
    unreachable, or returns nothing usable — the caller (hearth_ask.py)
    uses this to set provenance="none" rather than
    "general_model_knowledge", and never invents a deterministic factual
    answer in its place.
    """
    if gemini_client is None:
        return GeneralKnowledgeAnswer(ok=False, answer=UNAVAILABLE_MESSAGE)

    prompt = _PROMPT_TEMPLATE.format(instruction=_VOICE_INSTRUCTION, question_text=question_text)
    try:
        response = gemini_client.models.generate_content(model=GEMINI_MODEL_NAME, contents=prompt)
        text = getattr(response, "text", None)
        text = text.strip() if text else ""
    except Exception as exc:
        logger.warning("[general_knowledge] Gemini call failed: %s: %s", type(exc).__name__, exc)
        return GeneralKnowledgeAnswer(ok=False, answer=UNAVAILABLE_MESSAGE)

    if not text:
        return GeneralKnowledgeAnswer(ok=False, answer=UNAVAILABLE_MESSAGE)
    return GeneralKnowledgeAnswer(ok=True, answer=text)
