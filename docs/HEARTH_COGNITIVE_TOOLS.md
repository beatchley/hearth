# Hearth Cognitive Tools Catalog

**Phase 2 — Cognitive Tools (documentation only).** This document catalogs every existing read/retrieval capability in Hearth and Pathway Portal that could plausibly serve as a callable tool for a future reasoning layer to investigate a Building or situation before answering. It builds directly on the Phase 0 inventory (`HEARTH_MIND_00` through `12`, `99`) and Phase 1's Canonical Identity — no new capability is created here, nothing is wired into a calling loop, and no existing function is modified. Where a capability's current usage or a known gap is architecturally significant, it is cross-referenced back to the relevant Phase 0 document.

**Scope.** Only read-only capabilities are cataloged. Write functions (`create_*`, `add_*`, `update_*`, `resolve_*`, proposal-creation, etc.) are excluded per the phase brief, even when they sit in the same module as a documented read tool. Two categories of read-only code were deliberately left out of the catalog (see the end of this document's introduction, and the completion report, for why):

- **Worldview Audit's bulk diagnostic helpers** (`pathway-portal/backend/app/hearth_reader.py`'s `find_duplicate_*`, `find_orphaned_roads`, `find_road_lifecycle_inconsistencies`, `find_roads_inconsistent_with_state`, `find_empty_buildings`, `find_duplicate_canonical_keys`, `find_repeated_proposal_dismissals`, `get_proposal_activity_by_watcher`, `get_furniture_coverage_by_entity`, `get_watcher_ambiguity`, `list_all_furniture`, `list_all_state`, `list_all_entities_basic`, `get_road_counts_by_entity`, `get_last_activity_by_entity`, `get_active_beliefs_with_episode_status`, `get_living_uncertainties_with_episode_status`, `list_active_roads_with_names`). These are read-only and technically callable, but their sole current caller is `hearth_worldview_audit.py`, a self-diagnostic report, not something Ask Hearth or Traversal calls to investigate a question. Treated the same way the brief treats Watchers: infrastructure serving one autonomous process, not an on-demand tool today.
- **`hearth_ask.route_question()`** — pure text classification, reads no memory or organizational data at all, so it falls outside "reads Hearth's memory or Pathway's organizational data."

Tools are grouped by room/subsystem, matching the Phase 0 document boundaries.

---

## Entity Resolution & Building Lookup

### Resolve Building by Name
**Actual function / location**: `resolve_entity(memory_conn, query)` — `hearth/hearth_entity_resolution.py`
**Purpose**: Deterministically map a free-text name (typed by a manager, or captured from a question) to exactly one `hearth_entities` row, without ever guessing between plausible candidates.
**Inputs**: `memory_conn` (open sqlite3 connection to `hearth_memory.db`); `query: str` (free text, e.g. a manager-typed name).
**Outputs**: `EntityResolution` dataclass — `status` (`resolved`/`ambiguous`/`not_found`), `entity_id` (int, only if resolved), `entity_row` (sqlite3.Row, only if resolved), `candidate_names` (list of display names, only if ambiguous).
**Permissions**: No role/permission gate in the function itself — any caller with a `memory_conn` can call it. The one production consumer (Ask Hearth, via `/admin/hearth/ask`) is role-gated to `ceo`/`manager`/`it` at the Flask layer, one level up.
**Failure behavior**: Empty/whitespace query returns `status="not_found"` immediately. Zero matches at any layer falls through to the next layer; zero matches at all layers returns `not_found`. Two or more matches at any single layer returns `ambiguous` with `candidate_names` populated — never guesses. Never raises on a normal query.
**Cost**: Up to three sequential indexed/table-scan `SELECT`s against `hearth_entities` (exact match, case-insensitive match, then an unfiltered alias scan) — the alias layer is currently always a no-op in production since `hearth_entities.aliases` has no write path (see `HEARTH_MIND_08`).
**When it should be used**: The sole entity-lookup step in Ask Hearth (`hearth_ask.answer_question`) for both `tell_me_about_entity` and `connected_to_entity` routes — call this first whenever a reasoning process has a free-text name and needs a specific Building id before it can call any other tool in this catalog.

---

### Resolve Building by Pathway User ID
**Actual function / location**: `resolve_entity_by_user_id(memory_conn, user_id)` — `hearth/hearth_entity_resolution.py`
**Purpose**: Turn a known Pathway `users.id` (e.g. the author of a record) into its Building, when one exists.
**Inputs**: `memory_conn`; `user_id: int`.
**Outputs**: One `sqlite3.Row` from `hearth_entities`, or `None`.
**Permissions**: None.
**Failure behavior**: `user_id is None` returns `None` immediately. No match returns `None` — callers must treat this as "no author candidate," not an error. `hearth_entities.user_id` is `UNIQUE`, so this is never ambiguous.
**Cost**: Single indexed lookup on `hearth_entities.user_id`.
**When it should be used**: The Furniture Fact Extractor's self-subject step (turning a source record's author column into a Building). Useful to any reasoning process that already has a Pathway user id and needs the corresponding Building id.

---

### Scan Text for Known Buildings
**Actual function / location**: `find_entity_mentions(memory_conn, text, entity_type=None)` — `hearth/hearth_entity_resolution.py`
**Purpose**: Find every Building whose `display_name` or alias is literally, whole-word present in a block of text — e.g. to discover which Buildings a piece of free text is talking about.
**Inputs**: `memory_conn`; `text: str`; `entity_type: str | None` (restricts the candidate pool, e.g. `"person"`).
**Outputs**: `list[sqlite3.Row]` from `hearth_entities`, deduplicated by id, in no particular order. `[]` for empty/falsy text.
**Permissions**: None.
**Failure behavior**: Never raises; empty text returns `[]`. Unlike `resolve_entity()`, same-name collisions are never treated as ambiguous — both same-named Buildings are returned as independent candidates (a deliberate, documented asymmetry — see `HEARTH_MIND_08`).
**Cost**: One full-table scan of `hearth_entities` (or one filtered by `entity_type`) plus a whole-word regex check per candidate per call — scales with total Building count, not with text length.
**When it should be used**: The Furniture Fact Extractor's mention-scan step. Useful to a reasoning process that has a block of unstructured text (a note, a transcript) and wants every Building it might be about, accepting that each candidate needs independent downstream verification rather than a single confident match.

---

### Search Buildings by Name Fragment
**Actual function / location**: `find_entities_by_display_name(fragment, entity_type=None)` — `pathway-portal/backend/app/hearth_reader.py:986`
**Purpose**: Case-insensitive substring search of `display_name` — a looser fallback than `resolve_entity()`'s exact/alias-only matching, for when a caller only has a partial name.
**Inputs**: `fragment: str`; `entity_type: str | None`.
**Outputs**: `list[dict]` of full `hearth_entities` rows, ordered by `display_name`. `[]` for empty fragment or on any DB error.
**Permissions**: None in the function; its only current caller is a manual seed script, not a role-gated route.
**Failure behavior**: Wrapped in try/except — returns `[]` on any exception, never raises. Unlike `resolve_entity()`, this can return many matches; it does not distinguish "ambiguous" from "list of options" — the caller decides what to do with more than one result.
**Cost**: One `LIKE '%fragment%'` scan of `hearth_entities` — not indexed (leading wildcard), so cost grows with total Building count.
**When it should be used**: Currently only used by idempotent seed scripts trying to locate a known Building by partial name. A future reasoning layer wanting fuzzy/partial name search (something `resolve_entity()` explicitly refuses to do) would use this rather than inventing new fuzzy logic — but note it returns raw dicts with no ambiguity signaling, unlike `resolve_entity()`.

---

### Look Up Building by ID
**Actual function / location**: `get_entity_by_id(entity_id)` — `pathway-portal/backend/app/hearth_reader.py:931`
**Purpose**: Fetch one Building's full `hearth_entities` row when the id is already known.
**Inputs**: `entity_id: int`.
**Outputs**: `dict` of the full row, or `None`.
**Permissions**: None in the function; consumed only behind `@login_required` + `ceo`/`manager`/`it` role checks at the Flask layer (consistent across every `/admin/hearth/*` route).
**Failure behavior**: Returns `None` on no match or any exception — never raises.
**Cost**: Single indexed primary-key lookup.
**When it should be used**: Any time a reasoning process already has an `entity_id` (e.g. from `resolve_entity()` or a Road) and needs the raw Identity row rather than the curated summary `get_building_summary()` produces.

---

### Look Up Building by Canonical Key
**Actual function / location**: `get_entity_by_canonical_key(canonical_key)` — `pathway-portal/backend/app/hearth_reader.py:945`
**Purpose**: Look up a manually-created, non-person Building (organization/recurring_event/program) by its stable slug, so idempotent seed logic can reuse rather than duplicate.
**Inputs**: `canonical_key: str` (e.g. `"organization:pathway"`).
**Outputs**: `dict` of the full `hearth_entities` row, or `None`.
**Permissions**: None.
**Failure behavior**: Returns `None` on no match or exception.
**Cost**: Single indexed lookup (unique key).
**When it should be used**: Resolving a known non-person Building (an org, a recurring event, a program) when its canonical key is known — the person-entity equivalent is `resolve_entity_by_user_id()`/`get_entity_by_user_id()`.

---

### Look Up Building by Pathway User ID (Pathway-side)
**Actual function / location**: `get_entity_by_user_id(user_id)` — `pathway-portal/backend/app/hearth_reader.py:966`
**Purpose**: The Pathway-side counterpart of `hearth_entity_resolution.resolve_entity_by_user_id()` — same query, reached without importing `hearth/` code.
**Inputs**: `user_id: int`.
**Outputs**: `dict` of the full row, or `None`.
**Permissions**: None directly; reached only through role-gated admin routes in practice.
**Failure behavior**: Returns `None` on no match or exception.
**Cost**: Single indexed lookup.
**When it should be used**: Same use case as the `hearth/`-side version, for callers already on the Pathway side of the boundary (avoids the `sys.path` exception). This is a duplicate implementation, not a wrapper — see the completion report.

---

### List All Other Buildings
**Actual function / location**: `list_other_buildings(exclude_entity_id)` — `pathway-portal/backend/app/hearth_reader.py:1730`
**Purpose**: Return every Building except one — built for a manual-Road-creation "other side" picker UI.
**Inputs**: `exclude_entity_id: int`.
**Outputs**: `list[dict]` with `id`, `display_name`, `entity_type` only (not full rows), ordered by name.
**Permissions**: None directly; reached through role-gated admin routes.
**Failure behavior**: Returns `[]` on exception.
**Cost**: Full-table scan of `hearth_entities` minus one row — cost grows with total Building count; has no practical cap.
**When it should be used**: Today, only the manual-Road admin form. Would only suit a reasoning layer that genuinely needs "every Building" — most investigative questions should use a targeted resolver instead.

---

## Traversal

### Building Compact Snapshot
**Actual function / location**: `get_building_summary(memory_conn, entity_id)` — `hearth/hearth_traversal.py:27`
**Purpose**: Produce a single, bounded snapshot of one Building across Identity, State, and Furniture in one call, so callers never hand-assemble those three rooms themselves.
**Inputs**: `memory_conn`; `entity_id: int`.
**Outputs**: `dict` — `display_name`, `entity_type`, `summary`, `patterns_noticed`, `concerns`, `strengths`, `importance_score` (Identity fields); `state: {state_key: state_value}` (all current State rows, no cap); `furniture: [{fact_text, fact_type}, ...]` (active only, capped at `MAX_FURNITURE_FACTS = 5`, most-recently-observed first). Returns `None` if the Building doesn't exist.
**Permissions**: None in the function.
**Failure behavior**: Returns `None` (not an exception) if `entity_id` doesn't exist. Any underlying `sqlite3.Error` propagates to the caller uncaught — the one caller (`get_connected_context`) is the layer that catches it.
**Cost**: Three indexed `SELECT`s (`hearth_entities` by id, `hearth_entity_state` by `entity_id`, `hearth_entity_furniture` by `entity_id` + `status` with a `LIMIT`) — cheap, bounded, no joins.
**When it should be used**: The sole consumer today is `get_connected_context()`. Explicitly not a full room dump, not ranked, and cannot render brief text — a reasoning process wanting a quick "what does Hearth know about this Building" snapshot without traversing Roads should call this directly rather than the heavier `get_connected_context()`.

---

### Building Context-Volume Pointers
**Actual function / location**: `get_context_pointers(memory_conn, entity_ids)` — `hearth/hearth_traversal.py:78`
**Purpose**: Cheap, bulk "how much is there" signal per Building — counts only, never content — so a caller can decide whether a Building is worth a deeper look before paying for one.
**Inputs**: `memory_conn`; `entity_ids: list[int]` (any size; `None`s and duplicates are filtered).
**Outputs**: `dict[entity_id, {furniture_count, current_state_count, active_road_count, episode_count, reflection_count}]`, defaulting every field to `0` for ids with no rows in a given room.
**Permissions**: None.
**Failure behavior**: `entity_ids=[]` (or all-`None`) returns an empty dict without querying. Never raises under normal use — pure `SELECT ... GROUP BY` bulk queries.
**Cost**: Exactly five bulk queries total, regardless of how many entity_ids are passed — one per room (Furniture, State, Roads via a `UNION ALL`, Episodes, Reflection refs) — deliberately "one bulk query per room across all ids, never one query per room per entity."
**When it should be used**: Whenever a reasoning process has a set of candidate Buildings (e.g. Traversal neighbors) and wants a cheap signal for which ones actually have enough recorded content to be worth a full read, before calling `get_building_summary()` or deeper per-room tools on each one. Note: `reflection_count` reflects only `hearth_entity_reflection_refs` rows, not the underlying belief/uncertainty/change counts themselves — see the Reflection section below and Gaps Noticed.

---

### Full-Fidelity Road Reader — not currently callable in isolation
**Actual function / location**: `_get_active_roads(memory_conn, entity_id)` — `hearth/hearth_traversal.py:157` (module-private, leading underscore)
**Purpose**: Fetch every active Road touching a Building with full fidelity — every column, direction, and timestamp — richer than `hearth_relationships.get_relationships_for_entity()`/`get_related_entities()`, which "drop source/confidence/timestamps when building their result dicts."
**Inputs**: `memory_conn`; `entity_id: int`.
**Outputs**: `list[dict]` — each with `id`, `neighbor_entity_id`, `relationship_type`, `direction` (`incoming`/`outgoing`), `origin`, `source`, `confidence`, `first_observed_at`, `last_observed_at`, `status`. Ordered most-recently-observed first.
**Permissions**: None.
**Failure behavior**: Empty list if no active Roads. Underlying `sqlite3.Error` is not caught here.
**Cost**: Single indexed query against `hearth_relationships` (`entity_id_1 = ? OR entity_id_2 = ?`, `active = 1`).
**When it should be used**: This is genuinely the richest Road reader in the codebase, but it is a private function (leading underscore) with one caller, `get_connected_context()`, and is not exported or intended to be called standalone today. Flagged here because it is a real, useful capability that a future tool registry would likely want to expose as its own callable — a future phase should decide whether to promote it to a public function rather than reimplement its query.

---

### One-Hop Connected Context (Traversal V1)
**Actual function / location**: `get_connected_context(entity_id, max_neighbors=10)` — `hearth/hearth_traversal.py:210`; Pathway-side wrapper `get_connected_context(entity_id, max_neighbors=10)` — `pathway-portal/backend/app/hearth_reader.py:2469` (thin, "no reshaping, no extra error handling," `sys.path`-injected, one of `hearth_reader.py`'s three documented crossing-point exceptions)
**Purpose**: Answer "what is connected to this Building, and what compact context exists for each connection?" in one deterministic call — the module's public API and the retrieval backbone of Ask Hearth's entity-based routes.
**Inputs**: `entity_id: int`; `max_neighbors: int = 10`.
**Outputs**: `dict` — `source` (`id`, `name`, `type`, `summary` [= `get_building_summary()`'s shape], `pointers`); `connections` (list of `{road: {...}, building: {...}}`, one per shown neighbor, `building` becomes a placeholder `{"error": "summary_assembly_failed", ...}` if that one neighbor's summary assembly fails — per-neighbor failure isolation); `overflow` (int, neighbors beyond `max_neighbors`); `total_neighbors`; `max_neighbors`. On failure: `{"error": "entity_not_found" | "source_assembly_failed", "entity_id": ...}`.
**Permissions**: None in the `hearth/`-side function. The Pathway wrapper is reached only via `hearth_ask.answer_question()` behind the `/admin/hearth/ask` route's `ceo`/`manager`/`it` gate, or called directly by any Pathway code with the `sys.path` exception.
**Failure behavior**: Never raises to its caller — a missing source Building returns a structured error dict, not an exception; a `sqlite3.Error` assembling the source also returns a structured error dict; a single neighbor's assembly failure degrades to a placeholder building rather than failing the whole call. A dedicated smoke test asserts the Pathway wrapper's output matches the direct call byte-for-byte.
**Cost**: One `get_building_summary()` call for the source, one Road-fetch (`_get_active_roads`), one `get_context_pointers()` bulk call for source + up to 10 neighbors, then up to 10 more `get_building_summary()` calls (one per shown neighbor) — bounded, roughly O(max_neighbors) queries, never unbounded.
**When it should be used**: The sole in-repo caller of the underlying retrieval is `hearth_ask._answer_entity_question()`, for both `tell_me_about_entity` and `connected_to_entity` questions. This is the tool a reasoning layer should call whenever a question is fundamentally about one specific, already-resolved Building and its immediate neighborhood — it does not recurse past one hop, rank, or search, so a multi-hop question needs repeated calls, one per hop, orchestrated by the caller.

---

## Ask Hearth (composite retrieval + phrasing)

### Ask Hearth (Full Question Answering)
**Actual function / location**: `answer_question(question_text, memory_conn=None, gemini_client=None)` — `hearth/hearth_ask.py:402`, the module's sole public entry point; Pathway-side wrapper `ask_hearth(question_text)` — `pathway-portal/backend/app/hearth_reader.py:2507` (one of the three documented crossing-point exceptions; builds a best-effort Gemini client itself)
**Purpose**: One call that takes a manager's free-text question, deterministically classifies it, resolves any named Building, retrieves grounded context via the tools above, and returns a manager-readable answer (or the raw retrieved text if Gemini is unavailable/fails). This is the highest-level "investigate and answer" composite tool in the codebase today.
**Inputs**: `question_text: str`; `memory_conn` (optional, opens/closes its own if omitted); `gemini_client` (optional; `None` is a fully supported "no phrasing" mode).
**Outputs**: `AskHearthResult` dataclass — `status` (`success`/`unsupported`/`ambiguous`/`not_found`/`error`), `answer: str`, `source_summary: str` (a plain-text account of what was checked), `entity_id: int | None` (populated whenever a specific Building was resolved).
**Permissions**: None in `hearth/`'s function itself. The Pathway wrapper backs `/admin/hearth/ask`, gated to `ceo`/`manager`/`it` via an in-view-function role check (not a decorator) — `pathway-portal/backend/app/routes/main.py:2589-2608`.
**Failure behavior**: Never raises out — any unexpected exception inside becomes `status="error"` with the exception text in `source_summary`. Unsupported question shapes return `status="unsupported"` with a fixed message, by design (routing is a small, fixed pattern set — see `route_question()`, not itself a cognitive tool since it reads nothing). Ambiguous entity names return `status="ambiguous"` with the candidate names listed in `answer`, asking the human to disambiguate rather than guessing. Gemini failures fall back to the raw retrieved summary, "never to nothing."
**Cost**: Deterministic regex routing (cheap) → `resolve_entity()` (cheap) → either `get_connected_context()` (bounded, see above) or `hearth_context.build_context()` called with `memory_conn=None` (skips several reads and both writes — see the composite-tools section below) → one best-effort Gemini call (`gemini-*` model via `hearth_gemini_config.GEMINI_MODEL_NAME`).
**When it should be used**: The natural top-level tool for "answer this manager's question," already proven against three supported shapes (`tell_me_about_entity`, `connected_to_entity`, `needs_attention_today`). For anything routing would mark `unsupported`, a future reasoning layer needs to call the finer-grained tools in this catalog directly rather than through this entry point.

---

### Tell-Me-About / Connected-To Retrieval — not currently callable in isolation
**Actual function / location**: `_answer_entity_question(entity_id, emphasize_connections, gemini_client)` — `hearth/hearth_ask.py:340` (module-private)
**Purpose**: The shared retrieval-plus-rendering step behind both `tell_me_about_entity` and `connected_to_entity` — calls `get_connected_context()`, renders it to honest structured text (explicitly says "Summary: none recorded" rather than omitting sections; flags a Building as biographically sparse when it has zero Furniture and zero State), then best-effort polishes with Gemini.
**Inputs**: `entity_id: int` (already resolved); `emphasize_connections: bool`; `gemini_client`.
**Outputs**: `AskHearthResult` (see above).
**Permissions**: None directly.
**Failure behavior**: Same fallback discipline as `answer_question()` — a Traversal error becomes `status="error"` with a plain explanation; Gemini failure falls back to raw text.
**Cost**: Same as `get_connected_context()` plus one Gemini call.
**When it should be used**: Already the retrieval step for two of Ask Hearth's three supported question shapes. Documented separately because its raw-text rendering logic (the sparse-bio heuristic, the overflow note) is a real, reusable capability a future tool might want on its own — but it is private and not designed to be imported standalone today.

---

### Who-Needs-Attention-Today Retrieval — not currently callable in isolation
**Actual function / location**: `_answer_needs_attention_today(memory_conn, gemini_client)` — `hearth/hearth_ask.py:371` (module-private)
**Purpose**: Answer "who needs attention today?" using exactly Daily Brief's own filtering logic (`hearth_context.build_context()` / `_should_brief()`), so Ask Hearth and Daily Brief can never silently diverge on what counts as urgent.
**Inputs**: `memory_conn`; `gemini_client`.
**Outputs**: `AskHearthResult`, `entity_id=None` always (this route is never about one Building).
**Permissions**: None directly.
**Failure behavior**: Same fallback discipline as the module overall.
**Cost**: `hearth_memory.get_open_episodes(memory_conn)` (see Episodes section) plus one `build_context()` call with `memory_conn=None` — which, per its own internal guard, skips several enrichment reads (coach names, recent resolutions, relevant principles) as well as both of its writes; see `HEARTH_MIND_99` finding #17 for the documented-but-incomplete safety rationale.
**When it should be used**: Reuses Daily Brief's exact filtering rather than reimplementing it — any future "what needs attention" tool should call `hearth_context.build_context()` the same way rather than re-deriving `_should_brief()`'s rules independently.

---

## Furniture

### Active Furniture Facts for a Building
**Actual function / location**: `get_active_furniture(conn, entity_id)` — `hearth/hearth_furniture.py:61`
**Purpose**: Return a Building's current, active Furniture facts — the durable "because test" facts describing skills, interests, content, preferences, roles, and traits.
**Inputs**: `conn`; `entity_id: int`.
**Outputs**: `list[sqlite3.Row]` — full `hearth_entity_furniture` rows where `status='active'`, newest (`created_at`) first.
**Permissions**: None.
**Failure behavior**: Empty list if none. Not wrapped in try/except — a `sqlite3.Error` propagates.
**Cost**: Single indexed query (`entity_id`, `status`).
**When it should be used**: Currently used by the Fact Extractor for duplicate-suppression (checking what a Building already has on record before proposing a new fact) — not currently used for Ask-Hearth-style retrieval, which instead goes through `get_building_summary()`'s capped Furniture slice. A reasoning layer wanting the *uncapped* full active Furniture list (rather than the 5-fact snapshot) should call this directly.

---

### Furniture Facts by Status (Pathway-side)
**Actual function / location**: `get_furniture(entity_id, status="active")` — `pathway-portal/backend/app/hearth_reader.py:1020`
**Purpose**: The Building Inspector's Furniture-room read — same table as `get_active_furniture()`, but with a `status` parameter (pass `None` for full history including superseded/retracted rows).
**Inputs**: `entity_id: int`; `status: str | None = "active"`.
**Outputs**: `list[dict]`, most-recently-observed first.
**Permissions**: None directly; reached through role-gated admin routes.
**Failure behavior**: Returns `[]` on any exception — never raises.
**Cost**: Single query, indexed by `entity_id` (+ `status` when given).
**When it should be used**: The Building Inspector page's Furniture room, and the manual Furniture admin UI. Prefer this over `get_active_furniture()` when Pathway-side and either the full history or a non-active status is needed.

---

### Furniture Category Vocabulary
**Actual function / location**: `get_furniture_categories()` — `pathway-portal/backend/app/hearth_reader.py:2572` (wraps `hearth/hearth_furniture_taxonomy.FURNITURE_CATEGORIES`)
**Purpose**: Return the controlled vocabulary of valid Furniture `fact_type` values, so a caller can validate or present category options.
**Inputs**: None.
**Outputs**: `list[str]` — the seven categories (`skill`, `interest`, `content`, `preference`, `role`, `trait`, `other`; deliberately excludes `"relationship"` — "Roads own relationships, not Furniture").
**Permissions**: None.
**Failure behavior**: No error path documented — a static list wrapped from a module constant.
**Cost**: Negligible (no DB access).
**When it should be used**: Any tool proposing or validating a new Furniture fact_type. Note this taxonomy is enforced only for Fact-Extractor-sourced proposals today, not for manually-entered Furniture (see `HEARTH_MIND_03`) — a reasoning layer should not assume every existing Furniture row's `fact_type` is drawn from this list.

---

### Pending Furniture Proposals Queue
**Actual function / location**: `get_furniture_proposals_data(status="pending", limit=100)` — `pathway-portal/backend/app/hearth_reader.py:2592` (wraps `hearth/hearth_furniture_proposals.get_furniture_proposals()`, one of `hearth_reader.py`'s three documented crossing-point exceptions)
**Purpose**: List Furniture proposals awaiting (or having received) human review — the human-in-the-loop gate between the Fact Extractor and actual Furniture writes.
**Inputs**: `status: str = "pending"` (or `"all"`); `limit: int = 100`.
**Outputs**: `{"proposals": [dict, ...]}` — each row enriched with the proposing Building's `display_name`/`entity_type`, most recent first. `{"proposals": [], "error": str}` on failure.
**Permissions**: None directly; the admin review page is gated `ceo`/`manager`/`it`.
**Failure behavior**: Never raises to its caller — returns the error-shaped dict instead. The underlying `hearth_furniture_proposals.get_furniture_proposals()` raises `ValueError` for an invalid `status`, which this wrapper's try/except converts into the error dict.
**Cost**: Single indexed query with a `JOIN` to `hearth_entities` for display enrichment.
**When it should be used**: A reasoning layer investigating "what does Hearth think it's learned about this Building recently, but hasn't confirmed yet" would find this useful alongside `get_furniture()` — it's the only place to see facts Hearth has inferred but a human hasn't yet approved.

---

## State

### Current State Values for a Building
**Actual function / location**: `get_state(entity_id)` — `pathway-portal/backend/app/hearth_reader.py:1172`
**Purpose**: Return every current State key/value for a Building — the "what's true right now" room (e.g. `next_session`, `event_status`).
**Inputs**: `entity_id: int`.
**Outputs**: `list[dict]` — full `hearth_entity_state` rows, ordered by `state_key`.
**Permissions**: None directly; reached through role-gated admin routes.
**Failure behavior**: Returns `[]` on any exception.
**Cost**: Single indexed query.
**When it should be used**: The Building Inspector's State room, and any tool needing the full current-state picture rather than one key. `get_building_summary()`'s `state` dict is the Traversal-side equivalent (same table, dict-shaped instead of list-of-rows).

---

### Single Current State Value
**Actual function / location**: `get_current_state_value(entity_id, state_key)` — `pathway-portal/backend/app/hearth_reader.py:1189`
**Purpose**: Fetch exactly one State value, when the caller already knows which key it wants (e.g. "what's this Building's `next_session`?").
**Inputs**: `entity_id: int`; `state_key: str`.
**Outputs**: `str | None` (the raw `state_value`, or `None` if unset).
**Permissions**: None.
**Failure behavior**: Returns `None` on no match or any exception — "never raises," per its own docstring.
**Cost**: Single indexed point lookup — the cheapest read in this catalog for a targeted question.
**When it should be used**: Whenever a reasoning process needs one specific, known State fact rather than the whole State room — cheaper than `get_state()` for that case.

---

### State Change History
**Actual function / location**: `get_state_history(entity_id, state_key=None)` — `pathway-portal/backend/app/hearth_reader.py:1208`
**Purpose**: Read the append-only audit trail of how a State value changed over time (`hearth_entity_state_history`), distinct from the single current value in `hearth_entity_state`.
**Inputs**: `entity_id: int`; `state_key: str | None` (omit for every key's history).
**Outputs**: `list[dict]`, most recent change first.
**Permissions**: None directly; reached through role-gated admin routes.
**Failure behavior**: Returns `[]` on any exception.
**Cost**: Single indexed query (`entity_id` [+ `state_key`]), ordered.
**When it should be used**: A reasoning process asking "how has this changed over time" (e.g. "when did this Building's `event_status` last change, and from what?") rather than just "what is it now."

---

## Episodes

### Open Episodes
**Actual function / location**: `get_open_episodes(memory_conn, entity_id=None)` — `hearth/hearth_memory.py:232`
**Purpose**: Return every currently-unresolved episode, optionally scoped to one Building — the raw evidence layer behind Daily Brief and Ask Hearth's needs-attention route.
**Inputs**: `memory_conn`; `entity_id: int | None`.
**Outputs**: `list[sqlite3.Row]`, each enriched with `user_id`/`display_name` from a `LEFT JOIN` to `hearth_entities`, ordered by `observed_at`.
**Permissions**: None.
**Failure behavior**: Empty list if none open. Not wrapped in try/except.
**Cost**: Single indexed query (`resolved = 0` [+ `entity_id`]) with a cheap `LEFT JOIN`.
**When it should be used**: `hearth_ask._answer_needs_attention_today()`'s first step, and the general starting point for "what's currently unresolved" — note this returns *every* open episode, including types Daily Brief itself would suppress (`_NEVER_BRIEF_EPISODE_TYPES`); filtering for brief-worthiness is a separate step in `hearth_context.build_context()`, not part of this function.

---

### Recent Episodes (Any Status)
**Actual function / location**: `get_recent_episodes(memory_conn, limit=50)` — `hearth/hearth_memory.py:287`
**Purpose**: Return the most recent episodes regardless of resolved/open status — a general activity feed rather than an "outstanding issues" view.
**Inputs**: `memory_conn`; `limit: int = 50`.
**Outputs**: `list[sqlite3.Row]`, joined to `user_id`, ordered by `observed_at DESC`.
**Permissions**: None.
**Failure behavior**: Not wrapped in try/except.
**Cost**: Single indexed query with `LIMIT`.
**When it should be used**: No current in-repo caller was found beyond its own module context — a genuinely available, unused-today capability for "show recent activity across all Buildings," useful for an investigative "what's been happening lately" question that isn't scoped to open issues.

---

### Recently Resolved Episodes
**Actual function / location**: `get_recent_resolutions(memory_conn, hours=24)` — `hearth/hearth_memory.py:309`
**Purpose**: Surface positive progress (episodes resolved recently) so a caller isn't limited to only ever mentioning problems.
**Inputs**: `memory_conn`; `hours: int = 24`.
**Outputs**: `list[sqlite3.Row]`, joined to `display_name`, ordered by `resolved_at DESC`.
**Permissions**: None.
**Failure behavior**: Not wrapped in try/except.
**Cost**: Single indexed query (`resolved = 1 AND resolved_at >= ?`).
**When it should be used**: Computed by `hearth_context.build_context()` on every call but, per that function's own design, explicitly excluded from brief output. A reasoning layer building a "what got better recently" answer should call this directly rather than expecting it in rendered brief text.

---

### Building + Open Episodes + Total Count
**Actual function / location**: `get_entity_context(memory_conn, entity_id)` — `hearth/hearth_memory.py:163`
**Purpose**: A small composite read — the Identity row, its open episodes, and a total lifetime episode count — used to enrich Daily Brief's per-person context beyond what the open-episodes query alone carries.
**Inputs**: `memory_conn`; `entity_id: int`.
**Outputs**: `{"entity": sqlite3.Row, "open_episodes": list[sqlite3.Row], "total_episode_count": int}`, or `None` if the Building doesn't exist.
**Permissions**: None.
**Failure behavior**: Returns `None` (not an exception) for a missing entity.
**Cost**: One point lookup + one `get_open_episodes()` call + one `COUNT(*)` — three small queries.
**When it should be used**: The context builder's per-person enrichment step. Useful whenever a reasoning process wants "this Building's open episodes plus a sense of its total history depth" in one call rather than composing `get_open_episodes()` and a manual count.

---

### Recent Episodes for a Building (Pathway-side)
**Actual function / location**: `get_episodes_for_entity(entity_id, limit=20)` — `pathway-portal/backend/app/hearth_reader.py:1984`
**Purpose**: The Building Inspector's Episode Timeline panel read — recent episodes for one Building, any status, capped at 20 to match that panel's existing convention.
**Inputs**: `entity_id: int`; `limit: int = 20`.
**Outputs**: `list[dict]`, most-recently-observed first.
**Permissions**: None directly; reached through role-gated admin routes.
**Failure behavior**: Returns `[]` on exception.
**Cost**: Single indexed query with `LIMIT`.
**When it should be used**: When a reasoning process is already scoped to one Building (unlike `get_recent_episodes()`, which is global) and wants its event history without going through `get_entity_context()`'s open-only view.

---

## Worldview

### Seeded Organizational Identity Facts
**Actual function / location**: `get_identity(conn, status="active")` / `get_identity_value(conn, identity_key, default=None)` — `hearth/hearth_worldview.py:267,277`
**Purpose**: Read static, human-seeded organizational facts Hearth should simply know (key people, their roles) — as opposed to anything inferred from behavior.
**Inputs**: `get_identity`: `conn`, `status: str = "active"`. `get_identity_value`: `conn`, `identity_key: str`, `default=None`.
**Outputs**: `get_identity` → `list[sqlite3.Row]` ordered by `identity_key`. `get_identity_value` → `str | default`.
**Permissions**: None.
**Failure behavior**: `get_identity_value` returns `default` (not an exception) if the key isn't set.
**Cost**: Single indexed query each.
**When it should be used**: In principle, whenever a reasoning process needs a stable organizational fact (e.g. "who is the CEO"). In practice, note the gap: `hearth_context.collect_worldview_summary()` fetches Identity via `get_worldview_snapshot()` but the `WorldviewSummary` dataclass it renders into has no `identity` field, so these rows never reach Daily-Brief-bound text today (`HEARTH_MIND_99` #18) — a reasoning layer calling this tool directly would see data that current rendered output does not surface.

---

### Active Beliefs About a Subject
**Actual function / location**: `get_active_beliefs(conn, subject_type=None, subject_id=None, belief_type=None, limit=None)` / `get_belief_by_id(conn, belief_id)` — `hearth/hearth_worldview.py:339,359`
**Purpose**: Read Hearth's settled(-ish), confidence-scored interpretations about a subject (e.g. "this creator is responsive") — the primary Worldview-room read.
**Inputs**: `get_active_beliefs`: `conn` + optional filters (`subject_type`, `subject_id`, `belief_type`, `limit`). `get_belief_by_id`: `conn`, `belief_id: int`.
**Outputs**: `get_active_beliefs` → `list[sqlite3.Row]`, `status='active'` only, most-recently-updated first, capped if `limit` given. `get_belief_by_id` → one row or `None`.
**Permissions**: None.
**Failure behavior**: Empty list / `None` for no match, never raises.
**Cost**: Single indexed/filtered query each.
**When it should be used**: The two production belief types are `responsiveness` and `engagement_momentum`, written only by `hearth_soul.py` with deliberately conservative rules (never from a single negative event). A reasoning layer asking "what does Hearth currently believe about this Building" should call `get_active_beliefs(subject_type="entity", subject_id=<id>)` — this is read by `hearth_experience_evaluator.py` and `hearth_pulse.py` today, but not by Ask Hearth or Traversal directly (Worldview only reaches Ask Hearth indirectly, through Daily Brief's suppression logic — `HEARTH_MIND_05`).

---

### Interpreted Relationship Dynamics — currently a dead capability
**Actual function / location**: `get_active_relationship_understandings(conn, entity_a_type=None, entity_a_id=None, entity_b_type=None, entity_b_id=None, relationship_type=None, limit=None)` — `hearth/hearth_worldview.py:398`
**Purpose**: Read Hearth's *interpreted* understanding of a relationship dynamic between two Buildings — explicitly distinct from the raw structural assignment in `hearth_relationships.py`.
**Inputs**: `conn` + optional filters.
**Outputs**: `list[sqlite3.Row]`, `status='active'` only.
**Permissions**: None.
**Failure behavior**: Empty list — and today, always empty in production, since no writer ever populates this table (the live DB has 0 rows; see `HEARTH_MIND_04` and `HEARTH_MIND_99` #7).
**Cost**: Single indexed/filtered query.
**When it should be used**: Fully built and read by `hearth_context.collect_worldview_summary()` today, but a genuinely dead capability — calling this will always return `[]` until a writer exists. Cataloged for completeness and because a future phase may build the writer this table is waiting for.

---

### Open / Living Uncertainties
**Actual function / location**: `get_open_uncertainties(conn, subject_type=None, subject_id=None, priority=None, limit=None)` / `get_living_uncertainties(conn, subject_type=None, subject_id=None, priority=None, limit=None)` — `hearth/hearth_worldview.py:464,485`
**Purpose**: Read things Hearth is unsure about and is watching or wants to eventually ask a human — the mechanism behind surfaced questions.
**Inputs**: `conn` + optional filters.
**Outputs**: `list[sqlite3.Row]`. `get_open_uncertainties` → `status='open'` only. `get_living_uncertainties` → `status IN ('open','question_surfaced')`, i.e. anything not yet terminal (excludes `resolved`/`archived`/`dismissed`).
**Permissions**: None.
**Failure behavior**: Empty list, never raises.
**Cost**: Single indexed/filtered query each.
**When it should be used**: `get_living_uncertainties` is the one used internally by `upsert_uncertainty()`'s find-or-refresh logic and is the more complete "what is Hearth currently uncertain about" view; prefer it over `get_open_uncertainties` unless specifically excluding surfaced-but-unanswered items. Read by `hearth_context.py`, `hearth_experience_evaluator.py`, `hearth_pulse.py` (informational only for the latter two).

---

### Watched Changes
**Actual function / location**: `get_watched_changes(conn, subject_type=None, subject_id=None, direction=None, limit=None)` — `hearth/hearth_worldview.py:638`
**Purpose**: Read directional motion Hearth is tracking for a subject ("is this getting better or worse") — distinct from a belief (settled) or an uncertainty (an open question).
**Inputs**: `conn` + optional filters (`direction` e.g. `"improving"`/`"worsening"`).
**Outputs**: `list[sqlite3.Row]`, `status='watching'` only (no terminal status exists for this table — see Gaps Noticed).
**Permissions**: None.
**Failure behavior**: Empty list, never raises.
**Cost**: Single indexed/filtered query.
**When it should be used**: Written by two production paths in `hearth_soul.py` (cross-entity episode-type spikes, per-entity quiet-duration escalation). A reasoning layer asking "is this situation trending in a direction" should read this rather than trying to infer trend from raw Episodes.

---

### Recent Provisional Lessons
**Actual function / location**: `get_recent_lessons(conn, status=None, candidate_for_principle=None, limit=None)` — `hearth/hearth_worldview.py:767`
**Purpose**: Read provisional, cross-cutting patterns Soul has noticed that are candidates for eventual promotion into a settled Principle — explicitly not settled knowledge themselves.
**Inputs**: `conn` + optional filters.
**Outputs**: `list[sqlite3.Row]`, most recent first.
**Permissions**: None.
**Failure behavior**: Empty list, never raises.
**Cost**: Single indexed/filtered query.
**When it should be used**: The only production writer fires on the *second* occurrence of an already-watched change — a single spike never becomes a lesson. Useful for "has Hearth noticed this pattern before, more than once" questions. Note the promotion path to `hearth_principles` is asserted in comments but has no implementing code anywhere (`HEARTH_MIND_99` #8) — `candidate_for_principle` filtering will never actually find a promoted row today.

---

### Full Worldview Snapshot
**Actual function / location**: `get_worldview_snapshot(conn, belief_limit=25, relationship_limit=25, uncertainty_limit=25, change_limit=25, lesson_limit=25)` — `hearth/hearth_worldview.py:841`
**Purpose**: One call returning all six Worldview sub-concepts at once, for Soul, Context, and Questions to read without composing six separate calls.
**Inputs**: `conn` + per-category limits (each independently overridable, `None` for unbounded).
**Outputs**: `dict` with keys `identity`, `active_beliefs`, `active_relationships`, `open_uncertainties`, `watched_changes`, `recent_lessons` — each a `list[sqlite3.Row]` from the corresponding function above (`open_uncertainties` here calls `get_living_uncertainties`, not `get_open_uncertainties`).
**Permissions**: None.
**Failure behavior**: Composes six independently-safe reads; no additional failure mode of its own.
**Cost**: Six queries total (one per sub-concept) — cheap and bounded by the default 25-row-per-category caps.
**When it should be used**: Whenever a reasoning process wants the whole Worldview picture for a subject rather than one sub-concept — this is what `hearth_context.collect_worldview_summary()` (below) further filters and name-resolves for briefing use.

---

### Name-Resolved Worldview Summary for Briefing
**Actual function / location**: `collect_worldview_summary(memory_conn)` — `hearth/hearth_context.py:251`
**Purpose**: A capped, human-name-resolved worldview snapshot suitable for direct inclusion in briefing text — resolves each row's `subject_id`/`entity_a_id`/`entity_b_id` (Hearth entity ids) to a real display name via a two-hop lookup through Pathway.
**Inputs**: `memory_conn` (if falsy, returns an empty summary immediately).
**Outputs**: `WorldviewSummary` dataclass — `active_beliefs`, `active_relationships`, `open_uncertainties`, `watched_changes`, `recent_lessons` (each a list of dicts with a resolved `_subject_name`/`_entity_a_name`/`_entity_b_name` key added) — **no `identity` field**, even though the underlying snapshot includes it (see Gaps Noticed / `HEARTH_MIND_99` #18). Fixed per-category cap of 15 rows.
**Permissions**: None.
**Failure behavior**: Explicitly designed to "never raise" — any internal exception (including a broken Pathway connection for name resolution) falls back to an empty `WorldviewSummary`, so a worldview problem can never break briefing. Feature-flaggable off entirely via `HEARTH_WORLDVIEW_CONTEXT_ENABLED=0`.
**Cost**: `get_worldview_snapshot()` (6 queries) plus up to one Pathway-side name lookup per row (each a `hearth_entities` hop + `hearth_identity.get_user_display_name()` call) — the most expensive Worldview read in this catalog because of the per-row name resolution, though still bounded by the 15-row-per-category cap.
**When it should be used**: This is the version a reasoning layer should prefer over raw `get_worldview_snapshot()` whenever the output needs to be human-readable (names, not ids) — it is what Daily Brief itself calls.

---

## Constitution / Principles

### Active Principles List
**Actual function / location**: `list_active_principles(conn)` — `hearth/hearth_principles.py:82`
**Purpose**: Read every currently-active durable organizational belief Hearth holds about how to interpret creator behavior.
**Inputs**: `conn`.
**Outputs**: `list[sqlite3.Row]`, `status='active'` only, ordered by `confidence DESC, created_at ASC`.
**Permissions**: None.
**Failure behavior**: Empty list, never raises.
**Cost**: Single indexed query.
**When it should be used**: Whenever a reasoning process wants the full current set of organizational principles rather than ones filtered to specific episode types (see `collect_relevant_principles()` below for the filtered version).

---

### Principles by Topic Tag
**Actual function / location**: `get_principles_by_tag(conn, tag)` — `hearth/hearth_principles.py:91`
**Purpose**: Read active principles whose `topic_tags` contain a given tag — the building block `collect_relevant_principles()` uses per episode-type domain.
**Inputs**: `conn`; `tag: str`.
**Outputs**: `list[sqlite3.Row]`, `status='active'` only.
**Permissions**: None.
**Failure behavior**: Empty list, never raises.
**Cost**: Single `LIKE`-filtered query (case-insensitive) on `topic_tags`.
**When it should be used**: A pure read with no side effect — prefer this over `collect_relevant_principles()` (below) when a reasoning layer wants principles for a tag without also marking them "used" and nudging their confidence.

---

### Principles Under Review
**Actual function / location**: `get_principles_under_review(conn)` — `hearth/hearth_principles.py:206`
**Purpose**: Read principles that have been flagged as contradicted by later evidence, lowest-confidence first.
**Inputs**: `conn`.
**Outputs**: `list[sqlite3.Row]`, `status='under_review'` only.
**Permissions**: None.
**Failure behavior**: Empty list, never raises.
**Cost**: Single indexed query.
**When it should be used**: A reasoning process auditing "what does Hearth currently doubt about its own rules" — no current production caller was found outside `flag_principle_for_review()`'s own module context; a genuinely available, presently-unused-elsewhere read.

---

### Principles Relevant to Active Episode Types — has a write side effect
**Actual function / location**: `collect_relevant_principles(memory_conn, active_episode_types, tracer=None)` — `hearth/hearth_context.py:159`
**Purpose**: Surface principles relevant to whatever episode types are currently active, via a fixed episode-type → domain-tag map.
**Inputs**: `memory_conn`; `active_episode_types: iterable[str]`; `tracer` (unused parameter today).
**Outputs**: `list[sqlite3.Row]`, deduplicated by `principle_id`.
**Permissions**: None.
**Failure behavior**: No tags matched → empty list (with a stdout trace line, not an exception).
**Cost**: One `get_principles_by_tag()` call per matched domain tag, plus **one `mark_principle_used()` write per selected principle** (increments `times_used`, bumps confidence by +0.02, each individually committed).
**Note — not a pure read**: Despite living in a "collect"-named context-assembly function, every call to this function writes to `hearth_principles` as a side effect. A future tool registry treating catalog entries as safe, repeatable reads should either exclude this one or explicitly model it as read-with-side-effect (calling it twice for the same episode types is not idempotent-free — confidence keeps climbing).
**When it should be used**: Today, only `hearth_context.build_context()` calls this, once per full-context build. A reasoning layer wanting principles without the write side effect should call `get_principles_by_tag()` directly instead.

---

## Questions

### Open Surfaced Questions
**Actual function / location**: `list_open_questions(conn, limit=20)` — `hearth/hearth_questions.py:111`
**Purpose**: Read questions that have been surfaced for human review and are awaiting an answer.
**Inputs**: `conn`; `limit: int = 20`.
**Outputs**: `list[sqlite3.Row]`, `status='open'` only, oldest first.
**Permissions**: None in the function; the admin review page (`/admin/hearth/questions`) is gated `ceo`/`manager`/`it`.
**Failure behavior**: Empty list, never raises.
**Cost**: Single indexed query with `LIMIT`.
**When it should be used**: A reasoning process wanting "what is Hearth currently waiting to ask a human" — the surfaced subset of Worldview's uncertainties (see `get_living_uncertainties()` for the pre-surfacing view).

---

### One Question by ID
**Actual function / location**: `get_question(conn, question_id)` — `hearth/hearth_questions.py:122`
**Purpose**: Fetch a single question row when the id is already known.
**Inputs**: `conn`; `question_id: int`.
**Outputs**: One `sqlite3.Row`, or `None`.
**Permissions**: None.
**Failure behavior**: `None` on no match.
**Cost**: Single indexed lookup.
**When it should be used**: Following up on a specific question already referenced elsewhere (e.g. from a Worldview uncertainty's linkage).

---

### Questions by Topic Tag
**Actual function / location**: `list_questions_by_tag(conn, tag, status="open")` — `hearth/hearth_questions.py:175`
**Purpose**: Read questions filtered by topic tag and status.
**Inputs**: `conn`; `tag: str`; `status: str = "open"` (must be a recognized status).
**Outputs**: `list[sqlite3.Row]`, oldest first.
**Permissions**: None.
**Failure behavior**: Raises `ValueError` for an unrecognized `status` — the one function in this cluster that does not silently degrade.
**Cost**: Single `LIKE`-filtered query.
**When it should be used**: Scoping "what has Hearth asked (or wanted to ask) about topic X" — no current production caller found beyond the module's own context; available but presently unused elsewhere.

---

## Relationships / Roads

### Raw Roads for a Building (`hearth/`-side)
**Actual function / location**: `get_relationships_for_entity(memory_conn, entity_id, active_only=True)` — `hearth/hearth_relationships.py:336`
**Purpose**: Read Roads where the given Building is the *first* side (`entity_id_1`) only — a lighter, one-directional read than Traversal's `_get_active_roads()`.
**Inputs**: `memory_conn`; `entity_id: int`; `active_only: bool = True`.
**Outputs**: `list[sqlite3.Row]`, each enriched via `LEFT JOIN` with the connected Building's `display_name`/`user_id`, ordered by `relationship_type`.
**Permissions**: None.
**Failure behavior**: Empty list, never raises.
**Cost**: Single indexed query with a `LEFT JOIN`.
**When it should be used**: Only reads roads in the `entity_id_1` direction — since Roads are written in both directions for symmetric relationship pairs (e.g. `coach_of`/`coached_by`), this alone can miss the other direction. Prefer `_get_active_roads()` (Traversal, both-directions, richer columns) or the Pathway-side `get_relationships_for_entity()` below for a complete picture of one Building's Roads.

---

### Related Buildings with Relationship Metadata
**Actual function / location**: `get_related_entities(memory_conn, entity_id, relationship_type=None, active_only=True)` — `hearth/hearth_relationships.py:352`
**Purpose**: Read full Building rows (not just ids) connected to a given Building, enriched with the relationship type and confidence — used to pull coach/recruiter names into briefing context.
**Inputs**: `memory_conn`; `entity_id: int`; `relationship_type: str | None`; `active_only: bool = True`.
**Outputs**: `list[sqlite3.Row]` — full `hearth_entities.*` plus `relationship_type`, `confidence`.
**Permissions**: None.
**Failure behavior**: Empty list, never raises. Same one-directional (`entity_id_1`) limitation as above.
**Cost**: Single indexed query with a `JOIN`.
**When it should be used**: `hearth_context.py`'s read-only path for pulling coach/cn_coach/shop_coach/recruiter names into briefing context — pass `relationship_type="coach_of"` (etc.) to scope to one road type. Note this and `_get_active_roads()` both "drop source/confidence/timestamps" differently — see `HEARTH_MIND_08` for the documented gap between this and Traversal's richer reader.

---

### Roads Connected to a Building, Either Side (Pathway-side)
**Actual function / location**: `get_relationships_for_entity(entity_id, active_only=True)` — `pathway-portal/backend/app/hearth_reader.py:1911` — **note: same function name as the `hearth/`-side function above, different module, different behavior**
**Purpose**: The Building Inspector's Relationships-room read — unlike the `hearth/`-side function of the same name, this checks *both* `entity_id_1` and `entity_id_2`, so it never misses the "other direction" of an asymmetric-looking pair.
**Inputs**: `entity_id: int`; `active_only: bool = True` (pass `False` to include historical Roads too).
**Outputs**: `list[dict]` — `id`, `connected_entity_id`, `connected_display_name`, `connected_entity_type` (defaults to `"person"` if null), `relationship_type`, `origin`, `status`, `active`, `direction` (`incoming`/`outgoing`).
**Permissions**: None directly; reached through role-gated admin routes.
**Failure behavior**: Returns `[]` on any exception.
**Cost**: Single query with two `JOIN`s (both entity sides), filtered by either-side match.
**When it should be used**: This is the correct choice, not the `hearth/`-side function of the same name, whenever a complete both-directions Road list for one Building is needed and Traversal's `_get_active_roads()` isn't available/appropriate. The name collision between this and `hearth_relationships.get_relationships_for_entity()` is worth flagging to any future tool-registry author — they are not interchangeable.

---

### Historical Road Count
**Actual function / location**: `get_historical_relationship_count(entity_id)` — `pathway-portal/backend/app/hearth_reader.py:1968`
**Purpose**: Count non-active (historical/transitioned) Roads touching a Building, without fetching their content.
**Inputs**: `entity_id: int`.
**Outputs**: `int`.
**Permissions**: None directly; reached through role-gated admin routes.
**Failure behavior**: Returns `0` on any exception (indistinguishable from a genuine zero count).
**Cost**: Single indexed `COUNT(*)`.
**When it should be used**: A cheap existence check before deciding whether to pull historical Road content — none of the tools above surface historical Roads by default (`_get_active_roads()` and both `get_relationships_for_entity()`s default to active-only).

---

### Specific Road Lookup
**Actual function / location**: `get_road(from_entity_id, to_entity_id, relationship_type)` — `pathway-portal/backend/app/hearth_reader.py:1747`
**Purpose**: Check whether an exact (from, to, type) Road exists, active or historical — a read-only preview of what `add_manual_road()` would do, without writing.
**Inputs**: `from_entity_id: int`; `to_entity_id: int`; `relationship_type: str`.
**Outputs**: `dict` (full row) or `None`.
**Permissions**: None directly; reached through role-gated admin routes.
**Failure behavior**: Returns `None` on no match or exception.
**Cost**: Single indexed lookup on the `(entity_id_1, entity_id_2, relationship_type)` unique constraint.
**When it should be used**: A reasoning process asking a precise yes/no relationship question ("is Building A currently `coach_of` Building B?") rather than enumerating all of one Building's Roads.

---

## Identity (Pathway user identity resolution)

### Pathway User Identity Fields
**Actual function / location**: `get_user_identity(user_id, conn=None)` — `hearth/hearth_identity.py:52`
**Purpose**: The single, allowlisted place that turns a Pathway `users.id` into public identity fields Hearth is allowed to show a human — never exposes `password_hash`.
**Inputs**: `user_id: int`; `conn` (optional existing Pathway connection; opens/closes its own read-only one if omitted).
**Outputs**: `dict` with exactly `id`, `name`, `tiktok_handle`, `email`, `role`, `status`, `is_pathway_creator`, `is_shop_creator`, `cn_level`, `shop_level`, `joined_on` — or `None`.
**Permissions**: None in the function; connects read-only to Pathway's DB (`?mode=ro`) when opening its own connection.
**Failure behavior**: Never raises — returns `None` for a missing `user_id`, an unavailable connection, a missing `users` table, or no match.
**Cost**: Single query against Pathway's `users` table with an explicit column allowlist (not `SELECT *`).
**When it should be used**: Whenever a Pathway user's public profile fields are needed by id — the base primitive `get_user_display_name()` (below) and `collect_worldview_summary()`'s name resolution both build on this.

---

### Human-Readable Display Name for a User
**Actual function / location**: `get_user_display_name(user_id, conn=None)` — `hearth/hearth_identity.py:87`
**Purpose**: Resolve the single best human-readable name for a Pathway user, with a defined fallback order.
**Inputs**: `user_id: int`; `conn` (optional).
**Outputs**: `str` — `name` → `tiktok_handle` → `email`, in that order, or `"User {id}"` if all are empty/unavailable. Never `None`.
**Permissions**: None.
**Failure behavior**: Never raises, always returns a usable string.
**Cost**: One `get_user_identity()` call.
**When it should be used**: Anywhere a display string is needed rather than raw identity fields — this is what `collect_worldview_summary()` uses for its `_subject_name` resolution.

---

### Creator/Staff Identity Flags
**Actual function / location**: `identity_flags(row_or_dict)` (plus its components `is_creator_user()`, `is_staff_user()`) — `hearth/hearth_identity.py:170,150,161`
**Purpose**: Centrally answer "is this user a creator, staff, or both" — independent, overlapping flags, not mutually exclusive — so every watcher/reasoning process agrees on what counts as staff.
**Inputs**: `row_or_dict` — a `sqlite3.Row`, `dict`, or ORM-like object with `is_pathway_creator`/`is_shop_creator`/`role` fields.
**Outputs**: `{"is_creator": bool, "is_staff": bool}`.
**Permissions**: None. Pure in-memory computation — no DB access of its own (operates on a row already fetched, e.g. via `get_user_identity()`).
**Failure behavior**: Defensive field access (`_get_field`) never raises even for an unexpected row shape; missing fields default to falsy.
**Cost**: Negligible — no I/O.
**When it should be used**: Filtering staff-authored activity out of creator-focused analysis. **Caveat**: `hearth_identity.STAFF_ROLES` is documented as "centralized... so every watcher agrees," but `HEARTH_MIND_99` finding #3 confirms two other files (`hearth_pulse.py`, `morning_briefing.py`'s legacy path) define their own, silently different `_STAFF_ROLES` sets rather than importing this one — a reasoning layer should use this canonical version, not assume every existing caller already does.

---

## Composite / Building Inspector-level reads

### Per-Creator Hearth Context (Intelligence Record)
**Actual function / location**: `get_creator_hearth_context(user_id)` — `pathway-portal/backend/app/hearth_reader.py:661`
**Purpose**: The richest single-call "what does Hearth know about this creator right now" read — assembles active concerns, recent awareness-only observations, recent patterns, and coach/recruiter relationship info in one call, for the per-creator Intelligence Record surface.
**Inputs**: `user_id: int` (a Pathway user id, not a `hearth_entities.id`).
**Outputs**: `dict` — `active_concerns` (up to 5, `action_needed`/`critical`/`pattern` category, unresolved, each with `episode_type`, human `label`, `description`, `observed_at`, `briefing_category`, `severity`, `age_days`); `recent_observations` (up to 5, `awareness` category only); `recent_patterns` (up to 3, `pattern` category only); `relationship_insights` (`cn_coach`/`shop_coach`/`recruiter`/`legacy_coach`, each `{display_name, related_user_id, name, tiktok_handle: None}` — `tiktok_handle` is left for the caller to enrich via SQLAlchemy, not filled here); `available: bool` (`False` only if the memory DB file itself doesn't exist).
**Permissions**: None directly; consumed by whatever surface renders the Intelligence Record, itself behind Pathway's normal auth.
**Failure behavior**: Wrapped end-to-end in try/except — any failure (missing DB, missing entity, relationship-query error) degrades to a documented empty/no-entity shape rather than raising. A missing Building for the `user_id` returns `available: True` with all lists empty (distinct from `available: False`, which means the DB itself was unreachable).
**Cost**: Up to three filtered `hearth_episodes` queries (each capped, each excluding Experience-Evaluator-promoted episode types via `_exclude_promoted_evaluator_sql()`) plus one `JOIN`-based relationship query restricted to four specific relationship types — five queries total, all bounded.
**When it should be used**: The natural "give me everything relevant about this one creator" tool when the caller already has a Pathway `user_id` rather than a `hearth_entities.id` — a genuine alternative to `get_connected_context()` for creator-scoped (not general-Building) investigation, with its own independent category/cap conventions rather than reusing Traversal's.

---

### Full Awareness Context Assembly — has write side effects
**Actual function / location**: `build_context(data, open_episodes, memory_conn=None, ...)` — `hearth/hearth_context.py:456`
**Purpose**: The single translation boundary between raw Pathway/memory data and the text Gemini actually sees — assembles per-person contexts, unattached concerns, worldview summary, and relevant principles into one `HearthAwarenessContext`.
**Inputs**: `data: dict` (Pathway-side pre-fetched data, e.g. from `morning_briefing.py`); `open_episodes: list` (typically from `get_open_episodes()`); `memory_conn` (optional — see note below).
**Outputs**: `HearthAwarenessContext` — `person_contexts` (list of `PersonContext`, each with `display_name`, `open_concerns`, etc.), `unattached_concerns`, worldview summary, relevant principles, plus several fields computed but deliberately never rendered (new-user joins, today's battles, recent training-comment counts).
**Permissions**: None.
**Failure behavior**: Never lets a worldview read failure break briefing — `collect_worldview_summary()` internally falls back to empty on any exception.
**Note — not a pure read**: When `memory_conn` is a real connection, this function *writes* `hearth_episodes.last_briefed_at` (for `pattern`-category episodes just included) and calls `mark_principle_used()` (via `collect_relevant_principles()`, itself a write — see above). Passing `memory_conn=None` (as `hearth_ask._answer_needs_attention_today()` does) skips both writes but, per `HEARTH_MIND_99` finding #17, *also* silently skips three unrelated reads (coach-name lookups, `get_recent_resolutions()`, `collect_relevant_principles()`) — a documented, fragile coupling a future caller should be aware of before assuming "no writes" and "no reads lost" are the same guarantee.
**Cost**: Composes most of the reads cataloged above (`get_entity_context()` per person, `get_related_entities()`, `collect_relevant_principles()`, `collect_worldview_summary()`) — the most expensive single call in this catalog, roughly linear in the number of distinct Buildings with open episodes.
**When it should be used**: The one function Daily Brief and Ask Hearth's needs-attention route both call, specifically so the two can never silently diverge on what counts as brief-worthy. Any future "what needs attention" tool should call this rather than re-deriving `_should_brief()`'s filtering rules independently — but callers must pass a real `memory_conn` if they want the coach-name/resolutions/principles enrichment, understanding that doing so also re-enables the two writes.

---

## Gaps Noticed

The following are **not** documented tools — they are capabilities a future reasoning layer would plausibly want that do not exist today, called out separately per the phase brief so they are not mistaken for something already built.

- **No standalone "reflection refs for this Building" read.** `hearth_entity_reflection_refs` (the provenance breadcrumb table linking a Building to the Worldview rows genuinely created for it) has exactly one read path today: a bulk `COUNT(*)` inside `get_context_pointers()`, folded into `reflection_count`. There is no function that returns *which* belief/uncertainty/change rows a given Building's refs actually point to — a "why does Hearth believe this about this Building" tool would need one, and would have to be built new, not just exposed from existing code.
- **No single "everything about this Building" tool that includes Worldview content.** `get_building_summary()`/`get_connected_context()` intentionally surface Worldview only as a count (`reflection_count`); `get_creator_hearth_context()` surfaces Episodes and Roads but not Worldview at all; `collect_worldview_summary()` covers Worldview but not Episodes/Furniture/State. A future "full Building dossier" tool would need to compose at least three of the functions cataloged above — none of them does this today.
- **No text/semantic search across Furniture, Episodes, or Worldview content.** Every lookup in this catalog is either an exact/ambiguous-aware name match (`resolve_entity()`), a substring match on `display_name` only (`find_entities_by_display_name()`), or a whole-word literal scan (`find_entity_mentions()`). Nothing searches the *content* of a Furniture fact, an episode description, or a belief/lesson's text — a reasoning layer wanting "find every Building where Hearth has noted something about X" has no existing tool to call.
- **No cross-Building aggregate query surface for a reasoning layer.** The bulk/aggregate read functions that do exist (`list_all_furniture`, `get_furniture_coverage_by_entity`, `get_road_counts_by_entity`, etc., in `hearth_reader.py`) are scoped to `hearth_worldview_audit.py`'s self-diagnostic use case specifically (data-quality findings, not investigative answers) and were excluded from this catalog on that basis — see the introduction. A future phase should decide whether a reasoning-layer-facing version of "how many Buildings have no Furniture at all" is worth building as a distinct, general-purpose tool rather than reusing the audit-specific one as-is.

---

*End of catalog. No code was written or modified to produce this document.*
