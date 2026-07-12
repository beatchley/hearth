# Hearth Mind Inventory — Worldview

Covers: `hearth_worldview.py` (1003 lines, six tables) and its migrations (`migrate_add_hearth_worldview.py`, `migrate_add_worldview_source_run.py`, plus the uncertainty-related migrations `migrate_add_uncertainty_answer_fields.py` and `migrate_add_uncertainty_last_seen_at.py`).

Worldview is Hearth's room for *interpreted* understanding — as distinct from Episodes (raw observed events) and Furniture (durable, non-inferential facts). It has six sub-concepts, each treated by the code as a distinct capability with its own table, lifecycle, and (in most cases) sole writer: `hearth_soul.py`.

---

## Identity (`hearth_worldview_identity`)

- **Purpose**: Hold static/slow-changing organizational facts Hearth should simply *know* — who key people are, their roles — as opposed to something inferred from behavior.
- **What it knows**: `identity_key` → `identity_value` string pairs.
- **What it can write**: Directly, via `upsert_identity()` — inserts a new row or updates the existing active row for `identity_key` in place; never creates duplicate active rows.
- **What it can propose**: No proposal mechanism; writes are direct and immediate. In practice this table is populated almost entirely by the one-time manual script `seed_hearth_identity.py` (see `HEARTH_MIND_01`). Its docstring claims "Soul can refine nuance over time via `upsert_identity()`," but **no automated call site in `hearth_soul.py` actually calls `upsert_identity()`** — today Identity is entirely human-seeded, not learned.
- **Rooms touched**: Worldview.
- **Consumers**: `seed_hearth_identity.py` (writer, manual). `hearth_context.py` reads it via `get_worldview_snapshot()`, but the `WorldviewSummary` dataclass it renders into has no `identity` field — Identity rows are silently dropped before ever reaching `render_for_llm()` (see conflicts document).

---

## Beliefs (`hearth_worldview_beliefs`)

- **Purpose**: Hearth's settled(-ish), confidence-scored interpretations about an entity — e.g. "this creator is responsive" — distinct from a raw episode or a one-off observation.
- **What it knows**: `subject_type`/`subject_id`, `belief_type`, `belief_text`, `confidence` (0.0–1.0), `status` (active/archived), confirm/challenge timestamps, provenance (`source_episode_id` or `source_run`).
- **What it can write**: Directly, via `add_belief()` / `update_belief()`. Confidence is clamped to [0,1]; `_validate_source_episode_id()` guards against writing scan labels into `source_episode_id`.
- **What it can propose**: No proposal path — writes go straight to `active`. The only writer today is `hearth_soul.py`, implementing exactly two belief types: `responsiveness` (derived from episode-resolution counts within a run) and `engagement_momentum` (derived from activity-type diversity over a rolling 14-day window, with decay after 21 days of inactivity). Both are deliberately conservative — a single negative event never creates a belief.
- **What it can never do**: Form a belief from a single episode of any kind. Read raw private-DM events for momentum (permanently excluded per the sensory policy). Exceed confidence 0.85 for momentum beliefs.
- **Rooms touched**: Worldview (write); read by `hearth_experience_evaluator.py` and `hearth_pulse.py` (the latter explicitly "informational only" — see `HEARTH_MIND_07`).
- **Dependencies**: `hearth_principles.py` (for the grounded/ungrounded confidence delta); calls `hearth_worldview.create_entity_ref()` on genuine creation.
- **Consumers**: `hearth_soul.py` (sole writer), `hearth_context.py` (read), `hearth_pulse.py` (read, unused for classification), `hearth_experience_evaluator.py` (read, used to decide if a signal "changes understanding"). `hearth_belief_dedup_report.py` bypasses the module entirely with raw SQL to archive duplicate active rows — a maintenance script, not part of the write architecture.

---

## Relationships (`hearth_worldview_relationships`)

- **Purpose**: Hearth's *interpreted* understanding of a relationship dynamic between two entities — explicitly distinct from the raw assignment record in `hearth_relationships.py` ("This stores Hearth's interpretation of a relationship, not a raw assignment record," :376-378).
- **What it knows**: `entity_a_type/id`, `entity_b_type/id`, `relationship_type`, `relationship_summary`, `confidence`, `status`.
- **What it can write**: Directly, via `add_relationship_understanding()` / `update_relationship_understanding()` — but **nothing in the codebase ever calls either function outside the module's own smoke test.** `hearth_soul.py` — the stated "only writer to worldview" — never touches this table. The live DB confirms zero rows.
- **What it can never do**: Currently be created by any production code path. This is a fully-built, fully-wired-for-reading capability with no writer at all — a complete, dead capability, not broken, just never activated.
- **Rooms touched**: Worldview (schema + read plumbing only).
- **Consumers**: `hearth_context.py`'s `collect_worldview_summary()` reads it, but since it's always empty, this is currently a no-op read path.

---

## Uncertainties (`hearth_worldview_uncertainties`)

- **Purpose**: Track things Hearth is unsure about and wants to watch or eventually ask a human — the mechanism that turns Soul's caution into a surfaced question.
- **What it knows**: `subject_type/id`, `uncertainty_text`, `why_it_matters`, `possible_question`, `confidence`, `priority`, `status` (open / question_surfaced / resolved / archived / dismissed), `last_seen_at`, and five unused columns — `answer_text`, `answered_by`, `answered_at`, `acknowledged_at`, `acknowledged_by` (see below).
- **What it can write**: Directly, via `open_uncertainty()`, `upsert_uncertainty()` (find-or-refresh-or-create), `update_uncertainty()`, `resolve_uncertainty()`.
- **What it can propose**: No approval gate for *creation* — `open_uncertainty`/`upsert_uncertainty` write directly to `status='open'`. There is a genuine lifecycle state machine, unlike beliefs: `open` → `question_surfaced` (via `hearth_questions.surface_worldview_questions()`, once it becomes a `hearth_questions` row for human review) → `resolved` (via `resolve_uncertainty()`, when the linked question is answered/dismissed). This gates *surfacing*, not *creation* — Soul still writes the uncertainty unilaterally; a human only ever sees/acts on it after the fact. See conflicts document: `surface_worldview_questions()` is itself dead code in production.
- **What it can never do**: `upsert_uncertainty()` never reuses a terminal-status row — always opens a fresh one.
- **Rooms touched**: Worldview (write); surfaces into `hearth_questions` for human review (see `HEARTH_MIND_09`).
- **Consumers**: `hearth_soul.py` (writer), `hearth_questions.py` (reads, advances status, resolves), `hearth_context.py` (read, with suppression filtering), `hearth_experience_evaluator.py` / `hearth_pulse.py` (read-only, informational). `hearth_uncertainty_dedup_report.py` bypasses the module with raw SQL DELETE for duplicate cleanup.

**Orphaned schema**: `migrate_add_uncertainty_answer_fields.py` added five columns (`answer_text`, `answered_by`, `answered_at`, `acknowledged_at`, `acknowledged_by`) explicitly to "support the Hearth communication loop." A repo-wide grep found **zero references** to any of these five columns outside the migration file itself. The actual answer-tracking that is live uses `hearth_questions.answered_at` instead, a separate table. This is a partially-implemented feature: schema shipped, nothing uses it.

`migrate_add_uncertainty_last_seen_at.py`, by contrast, is genuinely wired in — `last_seen_at` is read/written throughout `upsert_uncertainty()`/`update_uncertainty()` and referenced in `hearth_soul.py`.

---

## Watched Changes (`hearth_worldview_changes`)

- **Purpose**: Track directional motion in a subject over time ("is this getting better or worse") — distinct from a belief (a settled state) or an uncertainty (an open question).
- **What it knows**: `subject_type/id`, `change_text`, `previous_state`, `current_state`, `direction`, `confidence`, `status` (default `watching`).
- **What it can write**: Directly, via `record_change()` / `update_change()`. Two production writers in `hearth_soul.py`: `_upsert_episode_type_change` (cross-entity volume spikes) and `_upsert_creator_quiet_watch` (per-entity quiet-duration escalation, only above medium/high severity). Both dedupe to at most one `watching` row per subject, refreshed via `last_seen_at` rather than re-inserted.
- **What it can never do**: Transition to a terminal/resolved status — no function anywhere sets `status` away from `'watching'`. Changes appear to accumulate indefinitely once opened.
- **Rooms touched**: Worldview (write); surfaced into Daily Brief context with an explicit suppression list so certain rows never leak into manager-facing briefings even though they're written and queryable.
- **Consumers**: `hearth_soul.py` (writer, also reinforces a recurrence lesson when a change recurs across runs), `hearth_context.py` (`_infer_worldview_episode_type` reverse-engineers episode_type from naming conventions to apply brief suppression), `hearth_experience_evaluator.py` / `hearth_pulse.py` (read-only).

---

## Recent Lessons (`hearth_worldview_recent_lessons`)

- **Purpose**: Provisional, cross-cutting patterns Soul has noticed that are candidates for eventual promotion into a settled organizational principle — explicitly *not* settled knowledge themselves.
- **What it knows**: `lesson_text`, `topic_tags`, `confidence`, `status` (default `provisional`), `times_confirmed`/`times_challenged`, `candidate_for_principle` flag.
- **What it can write**: Directly, via `add_recent_lesson()` / `update_recent_lesson()` / `confirm_recent_lesson()` / `challenge_recent_lesson()`. The only production writer is `_reinforce_recurrence_lesson`, which only fires on the *second* occurrence of an already-existing watched change — a single spike never becomes a lesson.
- **What it can propose**: This is the one capability with an explicit, named human-approval boundary stated in code comments: "Soul may suggest lessons; only a human promotes a lesson into `hearth_principles`." **But this rule is asserted, not enforced.** `candidate_for_principle` is never set to a non-default value by any code, and no code path anywhere reads `hearth_worldview_recent_lessons` and writes into `hearth_principles`. Unlike Furniture's formal `pending`/`approved` proposal table, there is no promotion workflow of any kind here — the boundary is a comment, not a gate.
- **What it can never do**: Self-promote into `hearth_principles` — no code path does this at all, human-triggered or automated.
- **Rooms touched**: Worldview (write).
- **Consumers**: `hearth_soul.py` (writer), `hearth_context.py` (read, passed through unmodified), `hearth_pulse.py` (read-only).

---

## Entity Reflection Refs (`hearth_entity_reflection_refs`) — cross-cutting, bridges Worldview to Identity

- **Purpose**: Leave a breadcrumb linking a Building to a worldview artifact genuinely created for it, so a future "why does Hearth believe this" query can follow provenance rather than re-derive it.
- **What it can write**: Via `create_entity_ref()` (in `hearth_worldview.py`, documented fully in `HEARTH_MIND_02`) — idempotent, `UNIQUE(entity_id, reflection_type, reflection_id)`.
- **What it can never do**: Accept a `reflection_type` outside `{worldview_belief, worldview_uncertainty, worldview_change}` — no `worldview_lesson` or `worldview_relationship` type exists yet, so lessons and relationships leave no breadcrumb even if/when relationships gain a writer.
- **Consumers**: `hearth_soul.py` (4 creation branches — see `HEARTH_MIND_02`). `hearth_traversal.py` reads only a bulk `COUNT(*)` from it, for the `reflection_count` context pointer.

---

## The direct-write pattern, in summary

Every one of Worldview's five populated tables (Identity, Beliefs, Uncertainties, Watched Changes, Recent Lessons) is written **directly** by `hearth_soul.py` to its live status, with no human-review gate before the write becomes visible in Daily Brief context and Ask Hearth-adjacent retrieval. This is architecturally different from Furniture, where the Fact Extractor is structurally barred from writing `hearth_entity_furniture` directly and must go through a `pending`/`approved`/`dismissed` proposal table. The only worldview-adjacent thing resembling a review gate is uncertainty *surfacing* (not creation) into `hearth_questions`, and the "human promotes a lesson into a principle" rule, which — as noted above — has no implementing code at all. See `HEARTH_MIND_99_CONFLICTS_AND_OPEN_QUESTIONS.md` for the full analysis and a recommendation.
