# Hearth Mind Inventory — Conflicts, Surprises, and Open Questions

Per the ground rules of this investigation: where sources disagree, or where documentation and code diverge, this document records **Source A, Source B, the nature of the disagreement, and a recommendation** — and stops there. No conflict below has been resolved. All decisions are Brian's.

Findings are grouped by theme, most architecturally significant first.

---

## 1. "Soul is the only writer to worldview" is asserted twice and is false

- **Source A**: `hearth_soul.py:6-9` and `:206-212` — "since Session 3, [Soul] is the only writer to Hearth's worldview... the only code path that writes to `hearth_worldview_*` tables, and only ever through `hearth_worldview.py`."
- **Source B**: `hearth_questions.py:130-157` — `mark_question_answered()` → `_resolve_linked_uncertainty()` → `hearth_worldview.resolve_uncertainty()`, which writes `hearth_worldview_uncertainties`. This is live in production: `hearth_sounding_board.py:132,155` calls `mark_question_answered()` from the human-review terminal tool, so a human answering a question can resolve a worldview uncertainty entirely outside Soul's reflection pass.
- **Nature of disagreement**: Soul's own module docstring makes an absolute claim about being the sole worldview writer that a second, human-triggered code path directly contradicts. (A third potential writer, `hearth_questions.surface_worldview_questions()`, also touches worldview but appears to be dead code in production — see #6 below, so it doesn't currently compound this.)
- **Recommendation**: Either update Soul's docstring to say "the only *autonomous* writer" (and name Sounding Board's resolve-on-answer as the one deliberate human-triggered exception), or decide that worldview writes should in fact be Soul-exclusive and route Sounding Board's resolution through a different, more explicit path. This matters for Phase 1 because any general cognitive process reasoning about "what changed my worldview and why" needs to know both writers exist.

---

## 2. Two parallel, non-communicating "training comment needs a response" detectors

- **Source A**: `morning_briefing.py`'s legacy Core Detection Engine — `_comment_needs_response()` (a hand-built keyword heuristic over the last 48 hours), creating episode_type `training_comment_needs_response`. This type has **no resolution branch** in `resolve_stale_issues()`; the code says outright: "stays open until manually resolved; auto-resolution requires a Pathway query for subsequent manager responses on the same training (TODO for a future version)" (:675-677). These episodes accumulate forever once created.
- **Source B**: `morning_briefing.py`'s current watcher — `query_training_comment_waiting()`/`detect_training_comment_waiting()`, using the DB-persisted `comment_type` classification (from `hearth_comment_classifier.py`) and an explicit staff-response check, creating episode_type `training_comment_waiting`, which **is** auto-resolved.
- **Nature of disagreement**: Not a documentation/code mismatch — both code paths are live and running every scan, in the same file, for the same real-world event, using different signals and never talking to each other. The file also defines two different `_STAFF_ROLES` sets to go with them (see #3).
- **Recommendation**: This looks like an in-progress migration (from keyword-heuristic to classifier-backed detection) where the old path was never retired. If `training_comment_waiting` is now trusted, the legacy `training_comment_needs_response` path and its accumulating unresolvable episodes should probably be removed. Worth checking the live episode count for this type before deciding — if it's already large, a cleanup script (in the spirit of the three dedup scripts) may be needed as part of retiring it, not just a code deletion.

---

## 3. Three different "who counts as staff" definitions, despite one being explicitly documented as centralized

- **Source A**: `hearth_identity.py:112-114` — `STAFF_ROLES = {"ceo","manager","coach","navigator","it","admin"}`, with a comment claiming it is "centralized here so every watcher across Hearth... agrees on what counts as staff."
- **Source B**: `hearth_pulse.py:33` — `_STAFF_ROLES = ("manager","coach","ceo","it","navigator")` (missing `"admin"`), defined locally, never imported from `hearth_identity`.
- **Source C**: `morning_briefing.py:915` — `_STAFF_ROLES = frozenset({"admin","manager","coach"})` (missing `"navigator"`, `"it"`), used by the legacy `_comment_needs_response()` path — defined in the **same file** that elsewhere (lines 303, 361) correctly imports and uses `hearth_identity.STAFF_ROLES` for the current watcher path.
- **Nature of disagreement**: A documented "single source of truth" has two silent, drifted local copies, one of which coexists with the correct centralized usage inside the same file.
- **Recommendation**: Replace both local `_STAFF_ROLES` definitions with imports from `hearth_identity.STAFF_ROLES`, or confirm (and document, if so) that `hearth_pulse.py`'s narrower set is intentional for its own purpose. Also relevant: `hearth_pulse.py`'s `_STAFF_ROLES` and the `get_user_identity, is_staff_user` import in `hearth_experience_evaluator.py:38` both appear to be **unused** (see #11) — this looks like a staff-filtering feature that was planned across three files and only partially wired.

---

## 4. Room taxonomy is inconsistent between the schema migration and the rest of the codebase

- **Source A**: The project brief and nearly all other code refer to eight rooms — Identity, Constitution, Furniture, State, Roads, Episodes, Worldview, Reflection.
- **Source B**: `migrate_add_six_room_schema.py`'s own docstring names six rooms — **Identity, Furniture, State, Relationships/Roads, Experience, Reflection** — using "Experience" where the rest of the codebase says "Episodes" (`hearth_episodes`), and omitting Worldview and Constitution entirely, despite Worldview being one of the largest, most actively-written subsystems in the codebase (6 tables, 1003 lines in `hearth_worldview.py`) and despite `hearth_soul.py` treating Worldview as architecturally central to its own stated purpose.
- **Nature of disagreement**: The word "Constitution" does not appear as a room, table, or module name anywhere in the code — the only occurrence of the word in any `.py` file is a UI header string in `hearth_sounding_board.py` ("Constitutional rule: Hearth may suggest lessons. Humans approve lessons."), which reads as a policy statement, not a room label. The closest structural analog, `hearth_principles.py`, never calls itself "Constitution" in its own code.
- **Recommendation**: Decide on one canonical room taxonomy and terminology before Phase 1 (Canonical Identity) begins, since that phase will presumably need a stable name for each room to build on. If Worldview and Constitution are meant to be first-class rooms (as the project brief implies), the six-room migration's docstring is simply out of date and should be corrected or superseded by a documented eight-room model.

---

## 5. Reflection is two structurally unrelated tables

- **Source A**: `hearth_soul.py`'s `hearth_reflections` table — a flat, non-entity-scoped, per-pipeline-run operational log. Module docstring: "a black-box log, not a journal" (:5).
- **Source B**: `hearth_entity_reflection_refs` — an entity-scoped breadcrumb index into worldview/question rows, added by the six-room migration, described there as "a lightweight lookup/index into existing worldview and question records. Does not duplicate their content."
- **Nature of disagreement**: These are structurally unrelated tables (one has no `entity_id` at all; the other is defined entirely by `entity_id`) that both plausibly answer to "the Reflection room." Nothing in the code states which one *is* the Reflection room, or whether both are, or whether they should eventually be merged/renamed to disambiguate.
- **Recommendation**: Name them distinctly in documentation going forward — e.g. "Reflection Log" for `hearth_reflections` and "Reflection Index" or "Provenance" for `hearth_entity_reflection_refs" — until a deliberate decision is made about whether they're one room or two.

---

## 6. A fully-built, tested feature with zero production callers: `surface_worldview_questions()`

- **Source A**: `hearth_questions.py:259-300`, the "worldview→question bridge," described in the module's own docstring as one of two question sources, with an extensive passing smoke test (lines 385-483) covering possible-question reuse, dedup-on-reopen, resolution-before-surfacing, and its feature flag.
- **Source B**: A repo-wide grep shows **zero call sites** outside the file's own `__main__` smoke test. `morning_briefing.py` calls `hearth_soul.generate_reflection()`, which triggers a legacy question path and separately `reflect_on_worldview()` (which writes uncertainties) — but nothing in the pipeline ever calls `surface_worldview_questions()` to turn those uncertainties into questions.
- **Nature of disagreement**: Not a contradiction so much as an apparently-complete feature that was never wired in. Its guarding env flag, `HEARTH_WORLDVIEW_QUESTIONS_ENABLED`, is consequently inert — there's no live call for it to gate.
- **Recommendation**: This looks like a one-line wiring change away from being live (call it from `morning_briefing.py`'s pipeline, likely near where `reflect_on_worldview()` runs). Worth confirming whether it was intentionally held back for a reason not visible in the code, or simply dropped.

---

## 7. Worldview's Relationships table: fully built, fully readable, zero writers

- **Source A**: `hearth_worldview.py`'s `add_relationship_understanding()` / `update_relationship_understanding()` — implemented, tested in the module's own smoke test, and actively read by `hearth_context.py`'s `collect_worldview_summary()`.
- **Source B**: A repo-wide grep found no production call site for either write function. The live `hearth_worldview_relationships` table has 0 rows.
- **Nature of disagreement**: Not a contradiction, but worth flagging plainly: this is a complete, dead capability, not a broken one. `hearth_soul.py` — the stated writer for everything else in Worldview — never touches it.
- **Recommendation**: Given the codebase's "Roads" naming elsewhere, this may have been intended to eventually connect to `hearth_relationships.py`'s raw assignment data (interpreting *why* a road exists, not just that it does), but no such connection exists today. Decide whether to build a writer, or remove the unused capability, before Phase 1 treats it as available.

---

## 8. "Only a human promotes a lesson into a principle" is a comment, not a gate

- **Source A**: `hearth_soul.py:12-13, 210-211` — "Soul may suggest lessons; only a human promotes a lesson into `hearth_principles`," presented as an architectural/constitutional rule.
- **Source B**: `hearth_worldview_recent_lessons.candidate_for_principle` is never set to a non-default value by any code, and no code path anywhere — human-triggered or automated — reads `hearth_worldview_recent_lessons` and writes into `hearth_principles`.
- **Nature of disagreement**: Unlike Furniture, which has a formal `pending`/`approved`/`dismissed` proposal table enforcing its human-approval boundary structurally, Worldview's "human promotes lessons" rule is asserted in comments only. There is no promotion workflow of any kind — not even a manual one a human could exercise if they wanted to. It is not disabled; it does not exist.
- **Recommendation**: If lesson→principle promotion is a real intended capability, it needs a workflow (likely modeled on `hearth_sounding_board.py`'s question→principle flow, or Furniture's proposal table). If it's not currently a priority, the docstring language should be softened so a future reader doesn't assume a safeguard exists that doesn't.

---

## 9. Worldview writes are direct everywhere; Furniture writes are proposal-gated — inconsistent by design, not by neglect

- **Source A**: `hearth_furniture_proposals.py` — "The Furniture Fact Extractor never writes `hearth_entity_furniture` directly. It writes proposals here; a human approves or dismisses each one."
- **Source B**: All five of Worldview's populated tables (Identity, Beliefs, Uncertainties, Watched Changes, Recent Lessons) are written **directly** by `hearth_soul.py` to their live status, immediately visible in Daily Brief context and Ask Hearth retrieval, with no human-review gate before that happens.
- **Nature of disagreement**: Not a bug in either system individually — both are internally consistent — but the two rooms follow genuinely different governance models for what is, in both cases, an inference from behavior rather than an observed fact. Uncertainty *surfacing* into `hearth_questions` is the closest thing Worldview has to a review gate, and even that reviews whether to *ask about* something Hearth already privately believes, not whether Hearth gets to hold that belief at all.
- **Recommendation**: Worth an explicit decision (not just an inherited accident) about whether Worldview should eventually gain a proposal layer like Furniture's, especially since Worldview content — unlike Furniture facts — actively shapes what gets said in the Daily Brief. This is squarely a Phase 1/Intelligence Layer question, not something to retrofit casually.

---

## 10. The manual Furniture admin UI bypasses the shared write path its own module docstring claims it uses

- **Source A**: `hearth_furniture.py:4-10` — states this module "owns the one shared INSERT path onto that table, used by: the manual Furniture UI (`hearth_reader.py`'s `add_furniture()`, via the sys.path exception documented there)."
- **Source B**: `hearth_reader.py`'s `add_furniture()`, `edit_furniture()`, and `retract_furniture()` each perform their own raw SQL directly against `hearth_entity_furniture` and never import `hearth_furniture.py` at all. The `sys.path` exception described does exist in `hearth_reader.py`, but only for a separate, newer wrapper block (the Furniture-proposal functions) — never wired to the manual-entry functions.
- **Nature of disagreement**: Both files were introduced in the same commit (confirmed on both the `hearth/` and `pathway-portal` sides), so this is not drift between two separate changes — it's a documented architecture that was never actually implemented for the manual-entry path in this V1. A direct, real consequence: `hearth_furniture_taxonomy.FURNITURE_CATEGORIES` is enforced for Fact-Extractor-sourced proposals but **not** for manually-entered Furniture, which can carry any `fact_type` string.
- **Recommendation**: Route `add_furniture()`/`edit_furniture()` through `hearth_furniture.create_furniture()` (or otherwise enforce the taxonomy at the manual-entry layer), or update the docstring to stop claiming this already happens.

---

## 11. Dead imports suggesting an unfinished staff-filtering feature in Pulse/Experience Evaluator

- **Source A**: `hearth_experience_evaluator.py:38` — `from hearth_identity import get_user_identity, is_staff_user` — neither name is referenced anywhere else in the file.
- **Source B**: `hearth_pulse.py:33` — `_STAFF_ROLES` is defined but never referenced in any classification logic in that file either.
- **Nature of disagreement**: Not a contradiction between two sources, but a consistent pattern across two files suggesting an abandoned plan to filter staff-authored events out of Pulse classification/Experience Evaluator promotion (e.g., so a staff member's own sign-in doesn't get promoted as creator "momentum"). No code path in either file currently does this filtering — a staff member's events flow through identical rules to a creator's.
- **Recommendation**: Either implement the staff-exclusion filtering these imports/constants suggest was planned, or remove the dead code. Worth checking with whoever wrote this whether staff events currently polluting Pulse/Episode data is an actual observed problem.

---

## 12. `hearth_traversal.py`'s reflection-count comment is stale relative to a commit that shipped after it

- **Source A**: `hearth_traversal.py:84-88` — "reflection_count uses Option A: only `hearth_entity_reflection_refs`... That ref table has no write path yet, so this will read 0 for nearly every Building today — expected, not a bug."
- **Source B**: `hearth_worldview.create_entity_ref()` was wired into 4 Soul creation branches in a later commit (`c17f5ad`, "Add create_entity_ref() write path, wire into 4 Soul creation branches") — confirmed live: the current `hearth_memory.db` has non-zero rows in `hearth_entity_reflection_refs`.
- **Nature of disagreement**: Straightforward documentation drift — the comment was accurate when written and is not anymore.
- **Recommendation**: Update the comment. Low priority, but worth catching now since Phase 1 work may reasonably read this comment and conclude the pointer is still always zero.

---

## 13. `hearth_ask.py`'s module docstring claims its own Flask UI "is not built here" — it is, and is live

- **Source A**: `hearth_ask.py`'s module docstring — "A Flask admin page in pathway-portal calls into this module; that page is a separate, later piece of work and is not built here" (present tense).
- **Source B**: `pathway-portal/backend/app/routes/main.py:2589-2608` — the `/admin/hearth/ask` route (`admin_hearth_ask`, GET/POST, role-gated to `ceo`/`manager`/`it`) is live, renders a real template, and the template's use of `result.entity_id` (linking to the Building Inspector) confirms that field's stated purpose is already exercised, not merely reserved.
- **Nature of disagreement**: Documentation/reality drift, the same shape as #12 — accurate when written, not updated after the dependent work landed.
- **Recommendation**: Update the docstring. Worth a broader pass, given how many of these findings are "this docstring was true as of an earlier commit" — see the closing note at the end of this document.

---

## 14. `hearth_relationship_events` — schema exists, nothing writes to it

- **Source A**: `migrate_add_six_room_schema.py` creates and indexes `hearth_relationship_events` (columns for `relationship_id`, `event_type`, `source_table`, `source_record_id`, `observed_at`, `notes`) — clearly designed as an append-only evidence/history log for the Roads room.
- **Source B**: `hearth_relationships.py` — the module that owns all relationship writes — never inserts into it. A repo-wide grep confirms zero `INSERT INTO hearth_relationship_events` anywhere in the codebase.
- **Nature of disagreement**: A planned-but-unbuilt half of the Roads room: schema shipped, write logic never followed up. Similarly, `hearth_relationships.activates_at`/`.expires_at` (added in the same migration) are never read or written outside the `ALTER TABLE` statement itself — provisioned for time-boxed relationships, currently inert.
- **Recommendation**: Decide whether Roads needs an event/evidence log (useful for the same "why does Hearth believe this" provenance goal that `hearth_entity_reflection_refs` serves for Worldview), or drop the unused schema.

---

## 15. Undocumented crossing points beyond `hearth_reader.py`'s three declared exceptions

- **Source A**: `hearth_reader.py`'s docstring names exactly three deliberate exceptions to "Pathway never imports from `hearth/`": `get_connected_context`, `ask_hearth`, and the Furniture-proposal wrappers.
- **Source B**: Two more crossing points exist elsewhere: (1) `hearth_scheduler.py:143,160` directly imports `hearth_pulse.process_unprocessed_hearth_events` and `hearth_experience_evaluator.run_experience_evaluator` after manually inserting `hearth/` into `sys.path` — bypassing `hearth_reader.py` entirely. (2) `hearth_fact_extractor.py` (which lives in `hearth/`) opens a **direct, read-only SQLite connection to Pathway's own database** via `DATABASE_URL` — a crossing in the *opposite* direction, from `hearth/` into Pathway's DB, not routed through `hearth_reader.py` or documented as an exception anywhere.
- **Nature of disagreement**: The "single documented gateway" framing undersells how many places actually cross the boundary. This isn't necessarily wrong architecture — the scheduler needs low-latency access to run jobs, and the Fact Extractor's whole job requires reading Pathway's content tables — but an engineer auditing "does Pathway import from `hearth/`?" by reading only `hearth_reader.py`'s docstring would miss both of these.
- **Recommendation**: Either fold these two into `hearth_reader.py`'s documented exception list (even though they don't physically go through that file, they're the same category of exception) or maintain a separate, explicit "boundary crossings" list somewhere both codebases can point to. This is exactly the kind of map Phase 1 will need.

---

## 16. `GO_LIVE_AT` in the Fact Extractor is a manually-set constant sitting right at "today"

- **Source A**: `hearth_fact_extractor.py:79-83` — `GO_LIVE_AT = "2026-07-11T00:00:00+00:00"`, with an inline comment: "Adjust before first production run if the actual introduction date differs from when this constant was written."
- **Source B**: This inventory was produced on 2026-07-12 — one day after the constant's value.
- **Nature of disagreement**: Not a contradiction, just a fragile hand-set value with no automated tie to the actual deploy date and no code that warns if it's stale.
- **Recommendation**: Worth a one-time confirmation that this date matches the real production introduction date before relying on it further, and consider whether a future version should derive this from a deploy marker rather than a hardcoded literal.

---

## 17. Ask Hearth's `memory_conn=None` call to `build_context()` silently drops more than its own comment says

- **Source A**: `hearth_ask.py:373-378` — justifies passing `memory_conn=None` to `hearth_context.build_context()` by noting build_context's two *writes* are both gated behind `if memory_conn:`, so there's "zero risk of Ask Hearth mutating Daily Brief's own state."
- **Source B**: The same `if memory_conn:` guard in `hearth_context.py` (lines 556, 664, 703) also skips three *reads* — coach-name lookups via `get_entity_context`, `get_recent_resolutions`, and `collect_relevant_principles` — none of which the comment mentions.
- **Nature of disagreement**: The comment's safety claim is correct but incomplete — it explains why this is safe for writes without acknowledging that it also degrades reads. Currently harmless, because `_build_needs_attention_raw_text()` never reads the fields that would be affected — but this is a fragile coupling between two files: if anyone extends the needs-attention prose to mention a coach name, it will silently render as missing, with no error anywhere pointing at the cause.
- **Recommendation**: Add a comment at the `hearth_context.py` gate itself (not just the caller) noting that `memory_conn=None` is a supported "shallow mode," or make the read/write split more explicit with two separate parameters.

---

## 18. `WorldviewSummary` silently drops Identity rows before they reach the LLM

- **Source A**: `hearth_context.collect_worldview_summary()` fetches a full six-key snapshot via `hearth_worldview.get_worldview_snapshot()`, which includes Identity rows.
- **Source B**: The `WorldviewSummary` dataclass (`hearth_context.py:98-118`) only carries `active_beliefs`, `active_relationships`, `open_uncertainties`, `watched_changes`, `recent_lessons` — no `identity` field. Identity rows are silently dropped before `render_for_llm()` ever sees them.
- **Nature of disagreement**: Since Identity is Hearth's one source of stable, human-seeded ground truth (who Stacy/Sarah/Toxie/etc. are — see `HEARTH_MIND_01`), this means none of that seeded context currently reaches Daily-Brief-bound output through the worldview path, despite the schema and data both existing.
- **Recommendation**: Add an `identity` field to `WorldviewSummary` and decide how (or whether) it should be rendered, given that Identity entries are prose-heavy human context rather than derived signal.

---

## 19. Orphaned schema: uncertainty "answer" fields exist but nothing uses them

- **Source A**: `migrate_add_uncertainty_answer_fields.py`'s docstring — these five columns (`answer_text`, `answered_by`, `answered_at`, `acknowledged_at`, `acknowledged_by`) "support the Hearth communication loop — managers can answer surfaced questions and Hearth can reference those answers later."
- **Source B**: A repo-wide grep found zero references to any of these five column names outside the migration file. The live answer-tracking loop uses `hearth_questions.answered_at` instead, a separate table.
- **Nature of disagreement**: Partial implementation — schema shipped for a feature, the feature itself never built on top of it.
- **Recommendation**: Either build the intended loop on `hearth_worldview_uncertainties` directly, or remove the dead columns and rely solely on `hearth_questions`.

---

## 20. Legacy `unlinked_battle` "migration" is still doing real work every run

- **Source A**: `morning_briefing.py:838-840` — `resolve_legacy_unlinked_battles()` is labeled a one-time migration cleanup, "idempotent — safe to run every startup," called unconditionally on every pipeline run, every scan mode, indefinitely.
- **Source B**: A live check of `hearth_memory.db` at the time of this inventory shows 2 still-open `unlinked_battle` episodes — this "migration" is not yet a no-op in production.
- **Nature of disagreement**: Not a contradiction, but the framing ("migration," implying a one-time transitional cost) doesn't match the reality of a function still doing meaningful cleanup work on every run, and using raw inline SQL against `hearth_episodes` rather than the `hearth_memory` accessor pattern used everywhere else in the file.
- **Recommendation**: No urgency, but once this genuinely reaches zero open rows for a sustained period, it's a safe candidate for removal — worth a note to revisit rather than leaving it as permanent pipeline overhead.

---

## 21. Daily Brief's duplicate-send guard reads Pathway's own table as Hearth's bookkeeping ledger

- **Source A**: `hearth_relationships.py`'s docstring and others state a general architecture rule: "Pathway is never modified... Pathway is truth, Hearth remembers."
- **Source B**: `morning_briefing.py:1782-1802` — the daily duplicate-send guard queries Pathway's `admin_chat_messages` table directly (`is_hearth=1 AND DATE(created_at)=today`) to decide whether a brief was already sent today, rather than recording "brief sent" state in `hearth_memory.db`.
- **Nature of disagreement**: A minor but real inconsistency — this is the one place Hearth treats a Pathway-owned table as its own state rather than keeping that bookkeeping in its own memory.
- **Recommendation**: Low priority; functionally fine today, but if Hearth's memory DB is ever meant to be a complete, self-contained record of Hearth's own actions, this is a gap worth closing.

---

## Smaller findings (self-documented as intentional, included for completeness)

These were flagged by the investigation but are lower-stakes — mostly self-aware technical debt already called out in code comments, included here so the full list lives in one place:

- **`hearth_memory.entities.strengths`** is schema-present, read by `hearth_traversal.get_building_summary()`, but never written by any code — explicitly deferred in `hearth_memory.py:407-408` ("left for future phases when positive signal types exist").
- **`hearth_entities.entity_type`** supports non-person Buildings (`project:*`, `event:*`) in schema and is gated on correctly by the Fact Extractor, but the live DB has zero non-person entities and no code path creates one — schema-ready, code-inert.
- **`hearth_entities.aliases`** has no production write path anywhere — the alias-match layer in both `hearth_entity_resolution.resolve_entity()` and `find_entity_mentions()` is currently unreachable in practice. Self-documented as intentional forward-compatibility scaffolding.
- **`hearth_fact_extractor._is_hearth_authored()`** is a permanent stub always returning `False`, self-documented as a real self-ingestion risk if the schema ever adds a Hearth-authorship marker.
- **`hearth_context._infer_worldview_episode_type`** reverse-engineers an episode_type from a worldview row's `subject_type`/`subject_id` using three hardcoded string conventions with no shared constant — the code's own comment warns this is a known leak vector for any future Soul-side convention it doesn't recognize.
- **`hearth_soul.py`'s `new_episodes` parameter** is misleadingly named — callers actually pass all currently *open* episodes, not just newly-created ones, a self-documented "historical artifact" (:729-731).
- **`hearth_traversal.py`'s "read-only" claim** (and, by extension, `hearth_ask.py`'s) is a code-discipline convention, not a database-enforced boundary — the underlying connection has no `PRAGMA query_only` or read-only mode guard.
- **The three dedup/maintenance scripts** (`hearth_uncertainty_dedup_report.py`, `hearth_belief_dedup_report.py`, `hearth_episode_dedup.py`) independently implement genuinely divergent deletion semantics, grouping keys, and keeper heuristics for conceptually the same operation — see `HEARTH_MIND_09` for the full comparison table. Not a bug, but a real question for anyone building a general "clean up duplicate memory" capability in Phase 1+: there is no existing shared base to extract from.
- **The deployment duplicate tree** — `/Users/brianatchley/pathway-portal/hearth/` is a manually-synced, byte-identical copy of `/Users/brianatchley/hearth/`. Nothing in either codebase automates keeping them in sync; that is presently a manual/deploy-process step.

---

## A pattern worth naming on its own

At least five separate findings above (#6, #12, #13, plus the `hearth_worldview.py` module docstring calling out its own staleness re: which modules it's "not wired into yet," plus `hearth_context.py`'s worldview-audit comment) are the same shape: **a comment or docstring that was accurate at the commit it was written, and was never revisited when a later commit made it false.** None of these are individually urgent, but collectively they suggest that as Phase 1 and beyond touch these files, a lightweight habit of updating the *adjacent* stale claim (not just the code) when editing nearby would prevent this list from growing. Worth deciding whether that's a norm worth stating explicitly for this codebase.
