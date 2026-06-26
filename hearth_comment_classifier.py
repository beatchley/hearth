"""
Gemini-powered classification for training comments.

Called once at write time (flush → classify → commit).
Pulse reads the stored classification; it never calls Gemini.
"""

import json
import logging
import os

logger = logging.getLogger(__name__)

_ALLOWED_CATEGORIES = frozenset({
    "question",
    "help_request",
    "feedback_request",
    "discussion",
    "observation",
    "celebration",
    "appreciation",
    "other",
})

_SAFE_DEFAULT = ("other", 0.0)


def classify_training_comment(comment_text: str):
    """Return (category, confidence) for a training comment.

    Always returns a valid pair — never raises into the caller.
    Falls back to ("other", 0.0) on any error.
    """
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        logger.debug("[classifier] GEMINI_API_KEY not set — using safe default")
        return _SAFE_DEFAULT

    try:
        from google import genai
    except ImportError:
        logger.warning("[classifier] google-genai not installed — using safe default")
        return _SAFE_DEFAULT

    prompt = f"""You are classifying a single training comment for Hearth, the organizational intelligence system used by Pathway Portal.
Your task is ONLY to classify the type of the comment.
You are NOT deciding whether a manager should respond.
You are NOT answering the comment.
You are NOT summarizing the comment.

Choose exactly one of these categories:
- question
- help_request
- feedback_request
- discussion
- observation
- celebration
- appreciation
- other

Category guidance:
- question — The creator is asking for information or clarification.
- help_request — The creator is asking for assistance solving a problem or completing a task.
- feedback_request — The creator is asking someone to review, evaluate, or critique their work.
- discussion — The creator is expressing an opinion, disagreement, idea, or conversation starter that reasonably invites discussion.
- observation — The creator is sharing a thought, realization, experience, or update without requesting interaction.
- celebration — The creator is sharing a success, milestone, achievement, or positive accomplishment.
- appreciation — The creator is thanking, encouraging, complimenting, or acknowledging someone.
- other — Anything that does not clearly fit one of the categories above.

Rules:
- Choose exactly one category.
- Determine the creator's primary intent, not simply whether the text contains a question mark.
- If multiple categories seem possible, select the strongest primary intent.
- Never invent new categories.
- If uncertain, return "other".
- Confidence should be a decimal between 0.0 and 1.0 representing your confidence in the selected category.

Return ONLY valid JSON in this exact format:
{{
  "category": "question",
  "confidence": 0.97
}}

Do not include markdown.
Do not include explanations.
Do not include additional fields.
Return JSON only.

Training comment:
{comment_text}"""

    try:
        gemini_client = genai.Client(api_key=api_key)
        response = gemini_client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
        )
        raw = response.text.strip() if response.text else ""
    except Exception as exc:
        logger.warning("[classifier] Gemini call failed: %s", exc)
        return _SAFE_DEFAULT

    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        logger.warning("[classifier] Could not parse Gemini response as JSON: %r", raw[:200])
        return _SAFE_DEFAULT

    category = parsed.get("category")
    confidence = parsed.get("confidence")

    if category not in _ALLOWED_CATEGORIES:
        logger.warning("[classifier] Invalid category %r — using safe default", category)
        return _SAFE_DEFAULT

    try:
        confidence = float(confidence)
    except (TypeError, ValueError):
        logger.warning("[classifier] Invalid confidence %r — using safe default", confidence)
        return _SAFE_DEFAULT

    if not (0.0 <= confidence <= 1.0):
        logger.warning("[classifier] Confidence %r out of range — clamping", confidence)
        confidence = max(0.0, min(1.0, confidence))

    return (category, confidence)
