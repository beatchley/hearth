# Hearth Mind Inventory — Infrastructure

Covers: `hearth_trace.py`, `hearth_gemini_config.py`. Both are cross-cutting utilities, not memory/cognition capabilities in their own right — no room is touched by either.

---

## Observation Trace (`hearth_trace.py`)

- **Purpose**: A developer-only audit trail ("Hearth Observation Trace") for debugging the Hearth detection pipeline — which rules fired, what episodes were created/reused/resolved, and why. Exists so engineers can diagnose detection behavior without that reasoning ever reaching end users or the LLM.
- **What it knows**: The shape of one auditable pipeline event (`TraceEntry`: rule_name, episode_type, action_taken, reason, timestamp, reference_key, source_table/record_id/fields, entity info, confidence) and how to render it as a one-line log string or a full multi-entry report. No domain knowledge of Pathway Portal itself.
- **What it can read**: Nothing external — only the `TraceEntry` objects handed to it.
- **What it can write**: Only stdout. `record()` always prints a one-line summary; `print_report()` prints a full report only when explicitly invoked (gated by `HEARTH_TRACE=1`). **No database table, file, or persistent memory store** — entries live only in an in-process list for the run's duration and are gone once the process exits; whatever survives is only in log capture.
- **What it can never do**: Per its own docstring, "never passed to the LLM and never surfaces to managers" — explicitly excluded from the Gemini prompt/context path and any manager-facing UI. Never touches business/memory tables. Both `record()` and `print_report()` swallow all exceptions (bare `except: pass`) so tracing failures can never break the pipeline that calls it — deliberate, but it also means the audit trail itself can have silent gaps with no indication anywhere that an entry was lost.
- **Rooms touched**: None.
- **Dependencies**: None — stdlib only.
- **Consumers**: `morning_briefing.py` — the primary consumer, threaded through the whole detection pipeline (`Tracer()` instantiated once per `run_pipeline()`, `print_report()` gated by `HEARTH_TRACE`). `hearth_context.py` — imported *locally* inside `build_context()` (grouped with two other local imports under a comment about avoiding a circular dependency, though `hearth_trace.py` itself has zero dependencies of its own, so the reason it's specifically deferred here is unclear — possibly just swept in alongside the other two rather than needing deferral itself).

---

## Gemini Model Configuration (`hearth_gemini_config.py`)

- **Purpose**: Single source of truth for the Gemini model name string. Created in direct response to a production incident: the model name previously lived independently in three files; a July 2026 deprecation of `gemini-2.5-flash` required three separate edits, and one — `hearth_comment_classifier.py` — was missed and went unnoticed.
- **What it knows**: Exactly one fact — `GEMINI_MODEL_NAME = "gemini-3.5-flash"` (:10). No routing, no API-key handling, no retry/fallback logic.
- **What it can read/write**: Nothing — zero imports, not even stdlib. A pure constant.
- **What it can never do**: Call Gemini itself, handle API keys, or make the actual `generate_content()` call — it only supplies a name string.
- **Rooms touched**: None.
- **Consumers**: `hearth_comment_classifier.py`, `hearth_fact_extractor.py`, `morning_briefing.py`, `hearth_ask.py` — all four confirmed Gemini call sites in the codebase.

**Verification: the centralization fix is complete.** A repo-wide grep for any `gemini-` string or hardcoded `model=` literal found only `hearth_gemini_config.py` itself — every one of the four Gemini call sites imports `GEMINI_MODEL_NAME` rather than hardcoding a string, including the previously-missed `hearth_comment_classifier.py`. No lingering inconsistency remains as of this inventory.
