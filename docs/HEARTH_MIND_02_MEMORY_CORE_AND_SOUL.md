# Hearth Mind Inventory — Memory Core & Soul

Covers: `hearth_memory.py`, `hearth_soul.py`, `hearth_context.py`, `migrate_add_six_room_schema.py`.

This is the central cluster of the codebase: the schema migration that defines the room structure, the low-level memory access layer, the interpretive engine (Soul) that turns raw episodes into Worldview understanding, and the context-assembly layer that turns all of Hearth's memory into the text Gemini actually sees.

---

## Six-Room Schema Migration (`migrate_add_six_room_schema.py`)

- **Purpose**: Additively extends `hearth_memory.db` with the columns/tables needed for a "six-room" entity model beyond the original entities/episodes schema.
- **What it knows**: The room→table mapping as of this migration — Identity → `hearth_entities` new columns (`entity_type`, `source`, `canonical_key`, `aliases`); Furniture → `hearth_entity_furniture`; State → `hearth_entity_state` + `hearth_entity_state_history`; Relationships/Roads → `hearth_relationships` new columns (`origin`, `status`, `activates_at`, `expires_at`, `transitioned_at`, `transition_reason`) + `hearth_relationship_events`; Reflection → `hearth_entity_reflection_refs`. Explicitly states "Experience" (`hearth_episodes`) is untouched by this migration.
- **What it can write**: The tables/columns above, via idempotent `ALTER TABLE`/`CREATE TABLE IF NOT EXISTS` statements.
- **What it can never do**: "does not add extraction, traversal, autonomous entity creation, or any write logic" (:11) — schema-only, no business logic.
- **Rooms touched**: Identity, Furniture, State, Roads, Reflection. Explicitly not Episodes. Does not create or reference Worldview or Constitution tables at all — see `HEARTH_MIND_00_OVERVIEW.md`'s room-taxonomy note.
- **Consumers**: Run standalone; `hearth_traversal.py`, `hearth_furniture.py`, `hearth_furniture_proposals.py`, `hearth_relationships.py` all depend on the tables it creates at runtime.

---

## Entity ("Building") Sync & Creation (`hearth_memory.py`)

- **Purpose**: Keep one `hearth_entities` row per Pathway user so all other Hearth subsystems have a stable local identity to attach memory to.
- **What it knows**: `hearth_entities` is Hearth's own learned-memory store; `display_name` is the only Pathway field stored here (:114-116); canonical key convention `'user:{id}'`.
- **What it can read**: Pathway's `users` table (`id`, `name`, `tiktok_handle`, `status='approved'`) via a passed-in connection.
- **What it can write**: `hearth_entities` — upsert `display_name` (`sync_users_to_entities`), or insert a bare new row on demand (`get_or_create_entity`).
- **What it can never do**: Write any Pathway table. Store anything beyond `display_name` from Pathway.
- **Rooms touched**: Identity.
- **Consumers**: `morning_briefing.py` (`get_or_create_entity` called ~9 times across issue-detection functions; `sync_users_to_entities` once per pipeline run); `hearth_ask.py` indirectly via entity resolution.

---

## Episode Tracking — the "Episodes" room (`hearth_memory.py`)

- **Purpose**: Records discrete, deduplicated observations ("episodes") about an entity — the raw evidence layer everything else (learned memory, Soul, context) is built from.
- **What it knows**: A two-mode dedup rule — match on `(episode_type, reference_key)` globally if a reference_key is given, else `(entity_id, episode_type)`; a fixed `_BRIEFING_CATEGORIES` map from episode_type → `awareness`/`pattern`/`action_needed`.
- **What it can write**: `hearth_episodes` — create (`create_episode`), refresh description/severity (`refresh_episode`), resolve (`resolve_episode`), stamp `last_briefed_at` (`update_last_briefed_at`). Episodes are never deleted, only marked resolved.
- **Rooms touched**: Episodes.
- **Consumers**: `morning_briefing.py` (heavy use throughout issue-detection); `hearth_ask.py`; `hearth_context.py`; `hearth_episode_dedup.py`; `hearth_experience_evaluator.py`.

---

## Learned-Memory Summarization (`hearth_memory.process_entity_observations` / `process_all_entities`)

- **Purpose**: Roll up an entity's full episode history into denormalized `hearth_entities` fields (`summary`, `patterns_noticed`, `concerns`) so downstream consumers don't have to re-derive patterns from raw episodes every time.
- **What it knows**: The "repeated evidence rule" — a pattern only counts if the same episode_type appears more than once; `awareness`-category episodes are excluded from patterns/concerns entirely; explicit instruction to stay factual — "Never asserts character, motivation, or anything not directly derivable from observed events" (:366-367).
- **What it can write**: `hearth_entities.summary`, `.patterns_noticed`, `.concerns`, `.first_observed_at`, `.last_observed_at`.
- **What it can never do**: Populate `strengths` — that field is explicitly deferred ("left for future phases when positive signal types exist," :407-408) and is schema-present but always-NULL today.
- **Rooms touched**: Identity (writes), reads Episodes.
- **Consumers**: `morning_briefing.py:1749`, once per pipeline run.

---

## Soul Reflection Log — the operational log, one of two "Reflection" tables (`hearth_soul.py`)

- **Purpose**: A per-run operational log of what Hearth's pipeline noticed — explicitly "a black-box log, not a journal" (:5).
- **What it knows**: Simple surprise-detection heuristics (≥2 new concerns for one entity, an episode_type appearing ≥3 times) and a threshold-based rule for generating a follow-up question.
- **What it can read**: In-memory lists/counts passed by the caller (`new_episodes`, `resolved_episodes`, `open_concerns`, `open_questions`) — does not query `hearth_episodes` itself.
- **What it can write**: `hearth_reflections` (`create_reflection`); optionally `hearth_questions` via `hearth_questions.create_question()` when `auto_question=True` and a question was derived.
- **What it can propose**: Auto-generated questions into `hearth_questions` — informational/review items.
- **Rooms touched**: Reflection (via `hearth_reflections` — see the Reflection-table ambiguity noted in the overview and conflicts docs).
- **Consumers**: `morning_briefing.py:1765`, once per pipeline run.

---

## Soul Worldview Reflection Engine (`hearth_soul.reflect_on_worldview` and helpers)

- **Purpose**: The single interpretive layer that turns raw episode signal into Hearth's durable "understanding" — beliefs, uncertainties, watched changes, provisional lessons. Stated architecture rule: "Pulse filters. Soul interprets. Worldview updates. Artifacts surface." (:11-14)
- **What it knows**: A large set of hand-tuned thresholds: `_REPEAT_CONCERN_THRESHOLD=2`, `_TYPE_SPIKE_THRESHOLD=3`, `_SINGLE_SIGNIFICANCE_TYPES`, `_CREATOR_QUIET_SIGNIFICANT_SEVERITIES`, confidence deltas (`_GROUNDED_DELTA=0.05` vs. `_UNGROUNDED_DELTA=0.03` depending on whether a supporting `hearth_principles` row exists), and engagement-momentum math (thresholds/decay, :216-679). Also encodes subject_type/subject_id provenance conventions (`"episode_type"`, `"entity_episode"`, `"creator_quiet_entity"`) that `hearth_context.py` separately, fragilely, re-decodes (see conflicts doc).
- **What it can read**: The worldview snapshot (`hearth_worldview.get_worldview_snapshot`); `hearth_principles.get_principles_by_tag` (read-only); Pathway's `hearth_events` table directly via its own read-only connection, for the engagement-momentum belief only (`_collect_momentum_activity`, :575-627).
- **What it can write**: `hearth_worldview_uncertainties`, `hearth_worldview_changes`, `hearth_worldview_recent_lessons`, `hearth_worldview_beliefs` (types `responsiveness` and `engagement_momentum`), and `hearth_entity_reflection_refs` via `create_entity_ref()` — all exclusively through `hearth_worldview.py`'s functions, never raw SQL.
- **What it can propose**: Provisional "recent lessons" (`status="provisional"`). Docstring: "Soul may suggest lessons; only a human promotes a lesson into `hearth_principles`" — but see the conflicts document: **no code path anywhere implements that promotion**, gated or otherwise. It is an asserted rule, not an enforced one.
- **What it can never do**: Write `hearth_principles` directly (verified — no such call anywhere in the file). Manufacture a belief from a single negative event ("don't manufacture a belief out of a single negative event," :510-511). Include private-DM event types in the momentum belief's input — permanently excluded per `HEARTH_SENSORY_POLICY.md` Category B.
- **Rooms touched**: Worldview (primary); Reflection (via `create_entity_ref`).
- **Dependencies**: `hearth_worldview`, `hearth_principles` (read-only), Pathway's `DATABASE_URL` (read-only, momentum only).
- **Consumers**: Called only from `generate_reflection()` in the same file, itself called from `morning_briefing.py:1765`. **Not the only writer to worldview** despite its own docstring's claim — see conflicts document: `hearth_questions.py` also writes `hearth_worldview_uncertainties` via a separate, human-triggered path.

### `create_entity_ref()` write path (defined in `hearth_worldview.py`, consumed here)

- **Purpose**: Leaves a breadcrumb linking a Building to a worldview artifact genuinely *created* for it, so a future provenance feature can follow evidence trails.
- **What it can write**: `hearth_entity_reflection_refs` only, via `INSERT ... ON CONFLICT DO NOTHING` keyed on `UNIQUE(entity_id, reflection_type, reflection_id)`. Valid `reflection_type` values in V1: `worldview_belief`, `worldview_uncertainty`, `worldview_change` — raises `ValueError` on anything else.
- **Consumers**: `hearth_soul.py` — exactly 4 call sites, matching the recent commit that added this path: `_upsert_entity_repeat_uncertainty` (:303), `_upsert_single_episode_uncertainty` (:427), `_upsert_creator_quiet_watch` (:474), `_upsert_responsiveness_belief` (:519). Notably, `_upsert_momentum_belief` (the other belief-creating function) does **not** call it — engagement-momentum beliefs leave no breadcrumb.
- **Note**: `hearth_traversal.py:84-87`'s comment that this ref table "has no write path yet" predates this commit and is now stale — see conflicts document.

---

## Context Assembly (`hearth_context.build_context`)

- **Purpose**: The single translation boundary between raw Pathway/memory data and Hearth's "own terms." Docstring: "The language model receives this context — not raw database rows, table names, column definitions" (:8-9).
- **What it knows**: Which episode categories are briefable (`_should_brief`, :392-426), a 3-day cooldown for `pattern`-category episodes, a hardcoded always/never-brief override list (`_ALWAYS_BRIEF_EPISODE_TYPES = {"missing_discord"}`, `_NEVER_BRIEF_EPISODE_TYPES = {"creator_quiet","new_creator_stuck","probation","training_comment_needs_response"}`), severity-based sort order.
- **What it can read**: `hearth_entities` (via `hearth_memory.get_entity_context`), `hearth_relationships` (via `hearth_relationships.get_related_entities`, for coach/recruiter roles), `hearth_principles` (via `collect_relevant_principles`), `hearth_worldview_*` (via `collect_worldview_summary`), plus the `data` dict passed in from `morning_briefing.py`'s own Pathway queries.
- **What it can write**: `hearth_episodes.last_briefed_at` only, and only for `pattern`-category episodes just included in a brief. Nothing else.
- **What it can never do**: Let a worldview read failure break briefing — `collect_worldview_summary` is designed to never raise, falling back to an empty summary on any exception (:254-257). Several computed values are deliberately never rendered: new-user joins, today's battles, recent training-comment counts (traced but never surfaced as observations), `recent_resolutions` (populated but excluded from brief output by design), shared-coach grouping (computed but never appended).
- **Rooms touched**: Reads Identity, Episodes, Roads, Worldview; writes one Episodes field.
- **Dependencies**: `hearth_memory`, `hearth_relationships`, `hearth_trace`, `hearth_principles` (local import), `hearth_worldview` + `hearth_identity` (local imports).
- **Consumers**: `morning_briefing.py:1753` (full context, with `memory_conn`); `hearth_ask.py:379` (calls with `memory_conn=None`, which silently drops worldview/principles/relationship enrichment — see conflicts document for the consequence).

## Context Rendering (`hearth_context.render_for_llm`)

- **Purpose**: Convert a `HearthAwarenessContext` into the exact text block sent to Gemini.
- **What it knows**: Section ordering (worldview renders first, "Session 4 priority order," :841); a specific bug-fix rule suppressing the "tracking age" suffix for `training_comment_waiting` concerns because the episode description already embeds the real-world source age — this fixed "the July 2026 training-comment brief bug" (:746-755, matches commit `603a727`); a worldview-row suppression rule (`_is_never_brief_worldview_row`) that back-infers an episode_type from a worldview row's `subject_type`/`subject_id` using three hardcoded string conventions, explicitly flagged in its own comment as fragile (:380-385).
- **What it can write**: Nothing — pure rendering.
- **What it can never do**: Emit table/column names or raw SQL structures. Render `recent_resolutions`, battles-today, or shared-coach-group data even though those are computed upstream.
- **Consumers**: `morning_briefing.py:1682`, inside `generate_hearth_message`.

## Principles Retrieval for Context (`hearth_context.collect_relevant_principles`)

- **Purpose**: Surface durable organizational beliefs (`hearth_principles`) relevant to today's active episode types.
- **What it knows**: A fixed `_EPISODE_TAG_MAP` from episode_type → principle domain tag (:146-156).
- **What it can write**: Marks principles as used (`mark_principle_used`) — metadata only.
- **Note**: No confidence filtering happens here — that gate is applied later in `render_for_llm` (only confidence ≥ 0.5 is actually rendered, :909-911), so `relevant_principles` on the context object can contain principles below the render threshold. Two-stage filter.

## Worldview Summary Retrieval for Context (`hearth_context.collect_worldview_summary`)

- **Purpose**: Read a capped, name-resolved snapshot of current worldview understanding for inclusion in a briefing.
- **What it knows**: A two-step identity-resolution path specific to worldview rows — `subject_id` for `subject_type="entity"` is `hearth_entities.id`, not a Pathway `users.id` directly, requiring a hop through `hearth_entities.user_id` before `hearth_identity.get_user_display_name` can resolve a real name (:206-214); a fixed per-category cap of 15 rows.
- **What it can read**: `hearth_worldview_*` via `get_worldview_snapshot`; Pathway's `users` table via `hearth_identity.get_pathway_connection()` for display-name resolution.
- **What it can never do**: Raise (must "never raise," :254-257); it is feature-flaggable off entirely via `HEARTH_WORLDVIEW_CONTEXT_ENABLED=0`.
- **Note**: The `WorldviewSummary` dataclass has no `identity` field — `hearth_worldview_identity` rows fetched by `get_worldview_snapshot()` are silently dropped before `render_for_llm()` ever sees them. See conflicts document.

---

For the full list of documentation/reality mismatches surfaced by this cluster (the "Soul is the only writer" claim, the stale `hearth_traversal.py` comment, the room-taxonomy inconsistency, the two Reflection tables, the always-NULL `strengths` field, the dead `entity_type` design, the fragile `_infer_worldview_episode_type` coupling, the misleading `new_episodes` parameter name), see `HEARTH_MIND_99_CONFLICTS_AND_OPEN_QUESTIONS.md`.
