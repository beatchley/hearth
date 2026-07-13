"""
Hearth Grounded Assertions — Phase 5.

Deterministically validates every manager-facing assertion Hearth's
manager-advice cognitive path (hearth_manager_advice.py) proposes, before any
of it is rendered. This is the same architectural pattern as the Furniture
Fact Extractor (hearth_fact_extractor.py): the model proposes structured
candidates; deterministic code verifies them; only validated output reaches
a human. It is also a direct, code-level enforcement of
HEARTH_CANONICAL_IDENTITY.md Section 7's "What Hearth may never do":
"Let its voice layer (the model) decide what is true, what is supported, or
what to say beyond phrasing already-grounded content. Routing, retrieval,
and validation are deterministic code; the model is never the source of a
decision, only of prose."

This module governs speech, not memory. It never reads or writes any Hearth
memory table itself — hearth_manager_advice.py already did all retrieval
before calling in here (docs/HEARTH_COGNITIVE_PROCESS.md Section 4). This
module's only inputs are the evidence already gathered and the model's
proposed assertions about that evidence.

An assertion is one individual claim, belonging to exactly one category
(Phase 5 build brief):
  - "knowledge"   — traceable to evidence retrieved this cognitive loop.
  - "reasoning"   — professional judgment/synthesis; evidence not required.
  - "uncertainty" — genuine "I don't know" / "cannot verify"; also valid and
                    expected, not a failure mode (HEARTH_CANONICAL_IDENTITY.md
                    Section 10).

Validation is deterministic throughout — no second LLM call ever asks
"was the first model right?" Grounding is checked the same way the Furniture
Fact Extractor checks its own evidence_quote: an exact, character-for-
character substring match against the evidence actually retrieved. Anything
that cannot be matched this way is treated as unsupported, never as "probably
fine."

Failure categories (Phase 5 build brief), and how this module resolves each:
  A. Formatting failure     — malformed JSON / missing required categories.
                               Caller retries generation once; see
                               deterministic_fallback_assertions() for the
                               backstop if a retry still fails.
  B. Grounding failure       — a "knowledge" assertion has no matching
                               evidence. Caller retries generation once;
                               close_out_ungrounded_knowledge() is this
                               module's deterministic backstop if the retry
                               still leaves an assertion ungrounded (see that
                               function's docstring for why this exists —
                               the build brief defines the first retry but
                               not what happens if it also fails).
  C. Reasoning failure       — an assertion cites an evidence_quote that does
                               not actually appear in the retrieved evidence
                               (a fabricated or approximate quote). Converted
                               to "uncertainty" in place, here, without a
                               retry — a fabricated quote is not something
                               asking the same model again is likely to fix.
  D. Evidence unavailable    — retrieval genuinely found nothing, or Gemini
                               itself is unavailable. Not a validation
                               failure at all; deterministic_fallback_assertions()
                               renders an honest response from whatever
                               evidence exists, never blocking a response.

Does not modify: hearth_manager_advice.py's tool-call budget, eligibility
gate, or retrieval planning — this module only ever runs after all of that,
on the model's proposed assertions.
"""

import logging
from dataclasses import dataclass, field
from typing import Callable, Optional

logger = logging.getLogger(__name__)

VALID_CATEGORIES = ("knowledge", "reasoning", "uncertainty")

# Response construction always requires at least one reasoning assertion (the
# manager asked "what do you think" — Hearth must have an opinion) and at
# least one uncertainty assertion (HEARTH_CANONICAL_IDENTITY.md Section 10 —
# uncertainty is expected, not exceptional). "knowledge" has no such floor:
# a Building with genuinely no recorded evidence is Category D, not a
# formatting failure.
_REQUIRED_NONEMPTY_CATEGORIES = ("reasoning", "uncertainty")

CATEGORY_HEADERS = {
    "knowledge": "Hearth Knowledge:",
    "reasoning": "General Reasoning:",
    "uncertainty": "Uncertainty:",
}
_CATEGORY_ORDER = ("knowledge", "reasoning", "uncertainty")

# Lexical proxy for "does this uncertainty assertion actually read as
# uncertain?" — deliberately a keyword heuristic, not semantic fact-checking
# (the build brief explicitly forbids asking another LLM to judge the first).
# Same class of known limitation as hearth_manager_advice.py's own
# _PROPER_NOUN_RE: cheap, mostly right, not exhaustive. See
# _looks_like_genuine_uncertainty()'s only caller for what happens when it
# misses — logged, not blocked.
_UNCERTAINTY_HEDGE_MARKERS = (
    "don't know", "do not know", "not sure", "unsure", "unclear", "uncertain",
    "cannot confirm", "can't confirm", "cannot verify", "can't verify",
    "no evidence", "not been recorded", "hasn't been recorded",
    "has not been recorded", "unknown", "no way to know",
    "unable to determine", "can't say", "cannot say", "don't have enough",
    "not confirmed", "no signal", "hasn't flagged", "not clear", "not certain",
    "cannot independently confirm", "cannot independently confirm or refute",
)


@dataclass
class Assertion:
    """One validated (or corrected) claim, ready for rendering.

    source is populated only for "knowledge" assertions whose evidence_quote
    was matched against the evidence pool — this is the Assertion -> Evidence
    -> Source provenance chain the build brief requires internally. Managers
    never see `source` directly; it exists for logs, tests, and any future
    debug view, the same inspectability precedent hearth_manager_advice.py
    already set by exposing `plan` on the result dict.
    """
    category: str
    text: str
    evidence_quote: Optional[str] = None
    source: Optional[str] = None


@dataclass
class ValidationOutcome:
    ok: bool
    assertions: list = field(default_factory=list)
    failure_category: Optional[str] = None  # "A" | "B" | None (C is corrected in place, never surfaced as a blocking failure)
    notes: list = field(default_factory=list)  # internal validation log — never rendered to the manager


# ---------------------------------------------------------------------------
# Evidence pool — the provenance-tagged text a "knowledge" assertion's
# evidence_quote must be an exact substring of.
# ---------------------------------------------------------------------------

def build_evidence_pool(evidence: dict, resolve_fn: Optional[Callable[[str], str]] = None) -> list:
    """Flatten hearth_manager_advice.py's `evidence` dict (keyed by tool name,
    shaped per each tool's own return contract) into a flat list of
    {"source": <tool name>, "text": <raw field text>} units.

    resolve_fn, if given, is applied to each unit's text before it's added —
    hearth_manager_advice.py passes its own _resolve_entity_id_mentions() so
    a stored "Entity 31" reference resolves to "Ethan" here too, matching
    what the model was actually shown in its prompt's evidence block (which
    passes through the same resolver). Without this, a model quoting the
    resolved name verbatim (the only form it ever sees) would never match a
    pool built from unresolved raw text.

    Deliberately does not import anything from hearth_manager_advice.py —
    that module imports this one; a reverse import would be circular. This
    keeps evidence-shape knowledge here, resolution-behavior injection there.
    """
    pool = []

    def _add(source, text):
        if resolve_fn is not None:
            text = resolve_fn(text)
        text = (text or "").strip()
        if text:
            pool.append({"source": source, "text": text})

    context = evidence.get("get_building_context") or {}
    summary = context.get("summary") or {}
    for key in ("summary", "patterns_noticed", "concerns", "strengths"):
        _add("get_building_context.summary", summary.get(key))
    for k, v in (summary.get("state") or {}).items():
        _add("get_building_context.state", f"{k}={v}")
    for fact in summary.get("furniture") or []:
        _add("get_building_context.furniture", fact.get("fact_text"))
    for conn in context.get("connections") or []:
        _add("get_building_context.connections", (conn.get("building") or {}).get("name"))
        _add("get_building_context.connections", (conn.get("road") or {}).get("type"))

    for ep in evidence.get("get_recent_episodes") or []:
        _add("get_recent_episodes", ep.get("description"))
        _add("get_recent_episodes", ep.get("episode_type"))

    for belief in evidence.get("get_active_beliefs") or []:
        _add("get_active_beliefs", belief.get("belief_text"))

    for change in evidence.get("get_watched_changes") or []:
        _add("get_watched_changes", change.get("change_text"))

    for unc in evidence.get("get_living_uncertainties") or []:
        _add("get_living_uncertainties", unc.get("uncertainty_text"))

    return pool


def _find_source(quote: Optional[str], evidence_pool: list) -> Optional[str]:
    """Exact, character-for-character substring match only — same standard
    hearth_fact_extractor._validate_extraction() already holds evidence_quote
    to. No fuzzy or semantic matching: a paraphrase is not grounding."""
    if not quote:
        return None
    for entry in evidence_pool:
        if quote in entry["text"]:
            return entry["source"]
    return None


def _looks_like_genuine_uncertainty(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in _UNCERTAINTY_HEDGE_MARKERS)


# ---------------------------------------------------------------------------
# Structural validation — Category A
# ---------------------------------------------------------------------------

def _structural_validate(parsed):
    """Returns (ok, cleaned_items, notes). cleaned_items survives even when
    ok is False, so a caller that chooses to salvage a partially-malformed
    response still can — today's caller (hearth_manager_advice._construct_response)
    always retries or falls back instead, per the build brief's Category A
    policy, but this function itself doesn't force that choice."""
    notes = []
    if not isinstance(parsed, dict):
        return False, [], ["Category A: model output was not a JSON object (or Gemini call failed)."]

    items = parsed.get("assertions")
    if not isinstance(items, list) or not items:
        return False, [], ["Category A: 'assertions' key missing, not a list, or empty."]

    cleaned = []
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            notes.append(f"Category A: assertion #{i} is not an object — dropped.")
            continue
        category = item.get("category")
        text = (item.get("text") or "").strip()
        quote = item.get("evidence_quote")
        quote = quote.strip() if isinstance(quote, str) and quote.strip() else None
        if category not in VALID_CATEGORIES or not text:
            notes.append(f"Category A: assertion #{i} malformed (category={category!r}) — dropped.")
            continue
        cleaned.append({"category": category, "text": text, "evidence_quote": quote})

    if not cleaned:
        return False, [], notes + ["Category A: no well-formed assertions survived structural validation."]

    present = {c["category"] for c in cleaned}
    missing = [c for c in _REQUIRED_NONEMPTY_CATEGORIES if c not in present]
    if missing:
        notes.append(f"Category A: response is missing required non-empty categories: {missing}.")
        return False, cleaned, notes

    return True, cleaned, notes


# ---------------------------------------------------------------------------
# Full validation — Categories B and C
# ---------------------------------------------------------------------------

def validate_assertions(parsed, evidence_pool: list) -> ValidationOutcome:
    """The single entry point a caller needs. Runs structural validation
    (Category A) first; if that fails, returns immediately with
    failure_category="A" and no assertions — there is nothing safe to reason
    about further until the shape itself is right.

    Otherwise, validates every assertion's relationship to its category and
    the evidence pool:
      - "knowledge" without a matching evidence_quote -> Category B: kept in
        the output (so a caller inspecting `assertions` sees the model's
        full intent) but tallied, and failure_category is set to "B" so the
        caller knows to retry.
      - "reasoning"/"uncertainty" WITH a quote that DOES match -> the
        assertion demonstrably has grounding it wasn't labeled for;
        reclassified to "knowledge" in place. This is a safe upgrade, not a
        failure — it only makes the label more accurate.
      - any assertion WITH a quote that does NOT match -> Category C: the
        quote is fabricated. The quote is stripped and the assertion becomes
        (or remains) "uncertainty" — converting toward the most conservative
        category is always deterministically possible for a text string, so
        Category C's "retry only if conversion is impossible" branch never
        triggers here.
      - "uncertainty" with no quote passes through a lexical genuineness
        check (see module docstring) — a miss is logged, not blocked.
    """
    ok, cleaned, notes = _structural_validate(parsed)
    if not ok:
        return ValidationOutcome(ok=False, assertions=[], failure_category="A", notes=notes)

    result = []
    ungrounded_knowledge = 0

    for item in cleaned:
        category, text, quote = item["category"], item["text"], item["evidence_quote"]
        source = _find_source(quote, evidence_pool)

        if category == "knowledge":
            if source is None:
                ungrounded_knowledge += 1
                notes.append(f"Category B: knowledge assertion has no matching evidence — {text!r}")
                result.append(Assertion("knowledge", text, quote, None))
            else:
                result.append(Assertion("knowledge", text, quote, source))
            continue

        if quote and source:
            notes.append(f"Reclassified {category}->knowledge: verifiable evidence_quote found — {text!r}")
            result.append(Assertion("knowledge", text, quote, source))
            continue

        if quote and source is None:
            notes.append(f"Category C: {category} assertion cited an unverifiable evidence_quote, converted to uncertainty — {text!r}")
            converted_text = text if category == "uncertainty" else f"Not confirmed by Hearth's records: {text}"
            result.append(Assertion("uncertainty", converted_text, None, None))
            continue

        if category == "uncertainty" and not _looks_like_genuine_uncertainty(text):
            notes.append(f"Uncertainty genuineness unconfirmed by lexical heuristic (kept, not retried) — {text!r}")

        result.append(Assertion(category, text, None, None))

    failure_category = "B" if ungrounded_knowledge else None
    return ValidationOutcome(ok=(failure_category is None), assertions=result, failure_category=failure_category, notes=notes)


def close_out_ungrounded_knowledge(outcome: ValidationOutcome) -> ValidationOutcome:
    """Category B's deterministic backstop.

    The build brief defines Category B's policy as "retry generation" but
    does not say what happens if the retry still leaves a knowledge
    assertion ungrounded. Looping indefinitely would violate the same
    "retrieval must stop" discipline HEARTH_COGNITIVE_PROCESS.md Section 4
    already requires of retrieval; converting the still-unsupported claim to
    Uncertainty (the same deterministic conversion Category C already uses)
    is the honest, terminating choice — never retry forever, never render an
    ungrounded claim as fact. Flagged in the completion report as an
    implementation decision, since the build brief leaves this case open.
    """
    fixed = []
    converted = 0
    for a in outcome.assertions:
        if a.category == "knowledge" and a.source is None:
            fixed.append(Assertion("uncertainty", f"Not confirmed by Hearth's records: {a.text}", None, None))
            converted += 1
        else:
            fixed.append(a)
    notes = list(outcome.notes)
    if converted:
        notes.append(
            f"Category B closure: {converted} knowledge assertion(s) still ungrounded after retry — "
            "converted to uncertainty rather than retrying again."
        )
    return ValidationOutcome(ok=True, assertions=fixed, failure_category=None, notes=notes)


# ---------------------------------------------------------------------------
# Category D — deterministic fallback when no usable model output exists at
# all (Gemini unavailable, or Category A persisted through the retry).
# ---------------------------------------------------------------------------

def deterministic_fallback_assertions(evidence_pool: list, limitations: list) -> list:
    """Every evidence-pool unit becomes its own self-evidencing knowledge
    assertion (evidence_quote == the unit's own text, which trivially matches
    itself — this is retrieved evidence being reported, not a model claim
    being verified). Preserves the three-part separation even with zero
    model involvement, consistent with hearth_manager_advice.py's existing
    Gemini-failure discipline: never nothing."""
    assertions = [Assertion("knowledge", e["text"], e["text"], e["source"]) for e in evidence_pool]
    if not assertions:
        assertions.append(Assertion("knowledge", "No organizational evidence was retrieved for this Building."))
    assertions.append(Assertion(
        "reasoning",
        "Hearth's reasoning layer wasn't available to synthesize an opinion this time — "
        "the evidence above is shown as retrieved, without added interpretation.",
    ))
    for item in limitations:
        assertions.append(Assertion("uncertainty", item))
    return assertions


# ---------------------------------------------------------------------------
# Rendering — deterministic, from validated assertions only.
# ---------------------------------------------------------------------------

def render_assertions(assertions: list) -> str:
    """Builds the conversational three-header response purely from already-
    validated Assertion objects. The headers are never model-authored text —
    this is precisely what closes Phase 5's gap: a response's labels and its
    assertions' actual categories cannot drift apart, because the label is
    applied here, by code, after validation, not requested of the model."""
    grouped = {c: [] for c in _CATEGORY_ORDER}
    for a in assertions:
        grouped.setdefault(a.category, []).append(a.text)

    blocks = []
    for category in _CATEGORY_ORDER:
        items = grouped.get(category) or []
        blocks.append(CATEGORY_HEADERS[category])
        blocks.append(" ".join(items) if items else "Nothing recorded in this category for this answer.")
        blocks.append("")
    return "\n".join(blocks).rstrip()
