# Hearth Mind Inventory — Traversal & Ask Hearth

Covers: `hearth_traversal.py`, `hearth_ask.py`. Both are read-only retrieval layers (by code discipline — see "What it can never do" notes below on how that boundary is, and isn't, enforced).

---

## Building Summary Reader (`hearth_traversal.get_building_summary`)

- **Purpose**: Produce a single, bounded "compact snapshot" of one Building across multiple rooms in one call, so callers never have to hand-assemble Identity+State+Furniture themselves.
- **What it knows**: A Furniture cap (`MAX_FURNITURE_FACTS = 5`), most-recently-observed-first ordering. Explicitly "not a full room dump" (:34).
- **What it can read**: `hearth_entities` (display_name, entity_type, summary, patterns_noticed, concerns, strengths, importance_score), `hearth_entity_state` (all rows, no cap), `hearth_entity_furniture` (active only, capped at 5).
- **What it can never do**: Read Roads, Episodes, or Reflection/Worldview content directly — those only surface as counts via context pointers, not content. Rank, generate brief text, or do drill-in/search (explicitly disclaimed).
- **Rooms touched**: Identity, State, Furniture (read-only).
- **Consumers**: `hearth_traversal.get_connected_context` only.

---

## Context Pointers (`hearth_traversal.get_context_pointers`)

- **Purpose**: Give callers a cheap, bulk "how much is there" signal per Building (counts, not content).
- **What it knows**: Deliberate bulk-query discipline — "one bulk query per room across all ids — never one query per room per entity" (:83-84).
- **What it can read**: `hearth_entity_furniture` (active count), `hearth_entity_state` (count), `hearth_relationships` (active count, both directions), `hearth_episodes` (count, all), `hearth_entity_reflection_refs` (count).
- **What it can never do**: Return content — counts only. Query `hearth_reflections` or `hearth_worldview_*` tables directly (uses the ref table as an indirection).
- **Note**: Its own comment claims `reflection_count` "will read 0 for nearly every Building today" because the ref table "has no write path yet" — this is stale. `hearth_worldview.create_entity_ref()` has been wired into 4 Soul creation branches since commit `c17f5ad`, and the live DB has non-zero rows. See conflicts document.
- **Consumers**: `hearth_traversal.get_connected_context` only.

---

## Road Reader (`hearth_traversal._get_active_roads`, private)

- **Purpose**: Fetch every active relationship touching a Building with full fidelity — richer than `hearth_relationships.get_relationships_for_entity()`/`get_related_entities()`, which "drop source/confidence/timestamps when building their result dicts" (:158-162).
- **What it can read**: `hearth_relationships` — all columns, `active=1` only, ordered by `last_observed_at DESC, relationship_type ASC, id ASC`.
- **What it can never do**: Traverse more than the entity's direct rows (no recursion, no relationship_type filtering).
- **Consumers**: `hearth_traversal.get_connected_context` only.

---

## Connected Context / Traversal V1 (`hearth_traversal.get_connected_context`) — the module's public API

- **Purpose**: Answer "what is connected to this Building, and what compact context exists for each connected Building" in one deterministic, one-hop call.
- **What it knows**: A hard neighbor cap (`max_neighbors=10` default) with explicit overflow accounting rather than silent truncation; per-neighbor failure isolation — one neighbor's summary-assembly failure becomes a placeholder, not a total failure.
- **What it can read**: Everything `get_building_summary`, `get_context_pointers`, and `_get_active_roads` read, combined.
- **What it can never do**: Rank/score connections, recurse past one hop, generate brief text, do search/drill-in, or write anything. **Important**: "read-only" here is a code-discipline claim (no write statement anywhere in the file), not a database-enforced boundary — the connection it's handed via `hearth_memory.get_memory_connection()` is a plain read-write SQLite connection, with no `PRAGMA query_only` or read-only URI mode. There is no structural guard preventing a future edit from adding a write.
- **Rooms touched**: Identity, State, Furniture, Roads (content); Episodes, Reflection (counts only).
- **Consumers**: `hearth_ask._answer_entity_question` (the sole in-repo caller); `pathway-portal/backend/app/hearth_reader.py:get_connected_context()` (a thin `sys.path`-injected wrapper, "no reshaping, no extra error handling"); a dedicated smoke test asserting the wrapper's output matches the direct call byte-for-byte.

---

## Deterministic Question Router (`hearth_ask.route_question`)

- **Purpose**: Classify a manager's free-text question into a fixed set of supported intents without ever asking an LLM to decide. "Routing is deterministic code. Gemini is the voice layer only." (:9-10)
- **What it knows**: Four branches — `needs_attention_today`, `connected_to_entity`, `tell_me_about_entity` (three phrasings), and `unsupported` as an explicit, by-design catch-all (not an omission). A casing-preservation trick for entity-name capture.
- **What it can never do**: Use an LLM for classification. Recognize any question shape outside the three matched patterns — e.g. `"What's Ethan's coach?"` routes to `unsupported` even though coach data is fully modeled and retrievable elsewhere in the codebase (a real, current capability gap, not a bug).
- **Rooms touched**: None (pre-retrieval).
- **Consumers**: `hearth_ask.answer_question` only.

Note: `connected_to_entity` and `tell_me_about_entity` converge on the *same* underlying retrieval (`get_connected_context`) and the same raw-text builder — the only difference is a boolean flag that changes prompt emphasis and heading text. There is no separate "connected-to" traversal implementation.

---

## Entity Resolution (imported from `hearth_entity_resolution.py` — full documentation in `HEARTH_MIND_08`)

Ask Hearth's entity-lookup step is `resolve_entity()`, shared with the Fact Extractor specifically "so the two never carry separate copies of the same matching rules." See `HEARTH_MIND_08_ENTITY_RESOLUTION_AND_RELATIONSHIPS.md` for full detail, including the documented (not accidental) asymmetry between how Ask Hearth handles same-name collisions (surfaces ambiguity, asks the human) versus how the Fact Extractor's mention-scan handles them (evaluates every same-named candidate independently, no disambiguation).

---

## "Who Needs Attention Today" Retrieval (`hearth_ask._answer_needs_attention_today`)

- **Purpose**: Answer "who needs attention today?" using exactly Daily Brief's own filtering logic, so Ask Hearth and Daily Brief can never silently diverge.
- **What it knows**: Reuses `hearth_context.build_context()` directly rather than reimplementing `_should_brief()` filtering.
- **What it can read**: All open episodes, fed into `build_context(data={}, open_episodes=..., memory_conn=None)`. Because `memory_conn=None`, `build_context` internally skips every branch gated on having a connection — both the two writes *and* several reads (coach-name lookups, entity enrichment, recent resolutions, relevant principles). The code comment justifying this only mentions the write-safety angle, not the read degradation — see conflicts document for why this is currently harmless but fragile.
- **What it can write**: Nothing.
- **What it can never do**: See anything Daily Brief itself would suppress — episodes in `_NEVER_BRIEF_EPISODE_TYPES` are filtered out before Ask Hearth ever sees them, verified by an explicit smoke-test assertion.
- **Consumers**: `hearth_ask.answer_question` only.

---

## Tell-Me-About / Connected-To Retrieval (`hearth_ask._answer_entity_question`)

- **Purpose**: Shared retrieval+rendering path for both `tell_me_about_entity` and `connected_to_entity` routes.
- **What it knows**: Honesty-over-implication rendering — explicitly says "Summary: none recorded" rather than omitting sections; a sparse-bio heuristic that injects a note when a Building has zero furniture and zero state, telling the reader Hearth has "stronger relationship and activity context than biographical detail" for it — a real signal-shaping decision baked into raw text, not just a prompt instruction.
- **What it can read**: Everything `get_connected_context` reads.
- **What it can never do**: Show more than 10 neighbors without explicitly flagging the overflow in text. Invent content Gemini wasn't given.
- **Consumers**: `hearth_ask.answer_question`, both entity-based routes.

---

## Gemini Voice Layer (`hearth_ask._call_gemini`)

- **Purpose**: Turn already-retrieved, grounded structured text into manager-readable prose — strictly a phrasing layer, never retrieval or judgment.
- **What it knows**: Two prompt templates with an explicit "use only the provided context — do not invent facts" instruction.
- **What it can never do**: Decide whether a question is supported, search data, or fill gaps with assumptions. On any exception it returns `None` and logs a warning rather than propagating — callers always fall back to the raw retrieved summary, "never to nothing," verified end-to-end by a smoke test using a deliberately-exploding Gemini client.
- **Dependencies**: `hearth_gemini_config.GEMINI_MODEL_NAME`.
- **Consumers**: Both entity-question and needs-attention retrieval paths.

---

## Ask Hearth Orchestrator (`hearth_ask.answer_question`) — the module's sole public entry point

- **Purpose**: One public function a Flask layer calls: text in, `AskHearthResult` out, with defined status semantics for every outcome (`success`, `unsupported`, `ambiguous`, `not_found`, `error`).
- **What it can write**: Nothing in production use — it only manages its own connection lifecycle (opens if none passed, closes in `finally`). The only writes anywhere in the file are inside its `__main__` smoke test, clearly prefixed and cleaned up in `finally`.
- **What it can never do**: Modify `hearth_traversal.py`, `hearth_context.py`, `hearth_soul.py`, `hearth_worldview.py`, or any watcher/detector code — an explicit non-goal in the module docstring, consistent with observed read-only behavior. Raise out to its caller — any unexpected exception becomes a `status="error"` result.
- **Rooms touched**: Identity, State, Furniture, Roads (content, entity routes); Episodes (open, needs-attention route); Reflection (counts only). Not Constitution, not Worldview content directly (Worldview only reaches Ask Hearth indirectly, through Daily Brief's suppression rules baked into `_should_brief`).
- **Consumers**: `pathway-portal/backend/app/hearth_reader.py:ask_hearth()` — a thin wrapper, `sys.path`-injected, that builds a best-effort Gemini client and calls straight through. That wrapper backs `/admin/hearth/ask`, a live, role-gated Flask route (`ceo`/`manager`/`it`) with a real template that renders `result.answer`, `result.source_summary`, and (when set) a link to the Building Inspector.

**Documentation note**: `hearth_ask.py`'s own module docstring states, present tense, that the Flask admin page for this "is a separate, later piece of work and is not built here." That page is already built, routed, role-gated, and live — the docstring predates a later commit and was never updated. See conflicts document.
