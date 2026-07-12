# Hearth Mind Inventory — Questions & Maintenance Tooling

Covers: `hearth_questions.py`, `hearth_sounding_board.py`, `hearth_uncertainty_dedup_report.py`, `hearth_belief_dedup_report.py`, `hearth_episode_dedup.py`.

---

## Questions (`hearth_questions.py`)

- **Purpose**: Track open questions Hearth wants surfaced for human review — explicitly distinct from principles (settled beliefs) and episodes (observed events).
- **What it knows**: How to translate a `hearth_worldview_uncertainties` row into human-readable question text; how to avoid duplicating/re-surfacing questions already linked to the same uncertainty.
- **What it can read**: `hearth_questions` (own table); `hearth_worldview_uncertainties` via `hearth_worldview.get_open_uncertainties()`.
- **What it can write**: `hearth_questions` directly (create, mark answered, dismiss, update text), plus schema DDL that adds `source_type`/`worldview_uncertainty_id` columns. Indirectly writes `hearth_worldview_uncertainties.status` via `hearth_worldview.update_uncertainty(status="question_surfaced")` and, when a question is answered/dismissed, via `resolve_uncertainty`.
- **What it can never do**: Write `hearth_worldview_beliefs` or `hearth_principles`. Call an LLM ("No LLM involved by design," :206-209). Mutate episodes.
- **Rooms touched**: Worldview (advances uncertainty status), Reflection (consumed by/feeds `hearth_soul.generate_reflection`).
- **Consumers**: `hearth_sounding_board.py` (`list_open_questions`, `mark_question_answered`, `dismiss_question`). `morning_briefing.py` (`ensure_questions_table`, `list_open_questions`, passed into Soul's reflection). `hearth_soul.py` (`create_question` inside a legacy reflection path).

**Fully built, never called**: `surface_worldview_questions()` — the module's headline "worldview→question bridge," documented as one of the two question sources in the module's own docstring, with an extensive passing smoke test covering reuse, dedup, resolution-before-surfacing, and its feature flag — has **zero call sites in production**. `morning_briefing.py` calls `hearth_soul.generate_reflection()`, which triggers the legacy `create_question` path and separately `reflect_on_worldview()` (which writes uncertainties), but nothing in the pipeline ever calls `surface_worldview_questions()` to turn those new uncertainties into questions. Its guarding env flag (`HEARTH_WORLDVIEW_QUESTIONS_ENABLED`) is consequently inert — there's no live call for it to gate. Looks like a completed feature waiting on a one-line wiring change that never landed.

---

## Sounding Board (`hearth_sounding_board.py`)

- **Purpose**: Interactive terminal tool for a human to review open questions one at a time and distill approved answers into `hearth_principles` rows — the human-in-the-loop step that turns Hearth's uncertainty into settled, constitutional-grade guidance.
- **What it knows**: A template-based (no-LLM) principle-proposal heuristic — takes the first 120 chars of the human's typed answer plus the question's topic tags and stitches them into draft principle text.
- **What it can write**: `hearth_principles` (via `hearth_principles.create_principle()`, always `source="sounding_board"`, `confidence=0.7`); `hearth_questions.status` (which cascades into resolving the linked uncertainty).
- **What it can propose**: Yes — this is its core function. Proposes principle text for human approval: Approve as-is, Edit then approve, or Reject (with a sub-choice to dismiss or leave the source question open). Nothing reaches `hearth_principles` without explicit human approval — enforces the printed header rule "Hearth may suggest lessons. Humans approve lessons."
- **What it can never do**: Call an LLM/external API for the proposal (deliberate). Auto-approve a principle. Touch episodes or beliefs directly.
- **Rooms touched**: Constitution (writes), Worldview (indirectly resolves uncertainties), Reflection.
- **Consumers**: None — standalone, manually-run interactive CLI (`--limit`, `--dry-run`). No cron/deploy config references it.

---

## Three Dedup/Cleanup Scripts

All three are dry-run-by-default, standalone, manually-run maintenance tools with zero production consumers and zero cross-references between each other — despite conceptually doing the same job (remove a redundant row), they are **independently implemented with genuinely divergent approaches**, which matters directly for any future consolidation:

| | Uncertainty Dedup | Belief Dedup | Episode Dedup |
|---|---|---|---|
| **Target table** | `hearth_worldview_uncertainties` | `hearth_worldview_beliefs` | `hearth_episodes` (`checkin_not_submitted` only) |
| **Deletion semantics** | Hard `DELETE` | Soft archive (`status='archived'`, note appended) | `resolved=1` via `hearth_memory.resolve_episode()` |
| **Grouping key** | `(subject_type, subject_id)` | `(subject_type, subject_id, belief_type)` | Hand-parsed reference-key scheme + cross-DB lookup into Pathway's `checkin_submissions` to resolve submission→checkin id |
| **Keeper heuristic** | Prefer `question_surfaced` status, then most-recent `updated_at` | Highest `confidence`, then most-recent `updated_at`, then highest `id` (no lifecycle-stage preference) | Strictly oldest by `observed_at` (opposite direction from the other two) |
| **Connection helper** | Own private `get_connection()` | Own private `get_connection()` | Reuses `hearth_memory.get_memory_connection()` |
| **CLI flag** | `--apply` | `--apply` | `--execute` (plus a separate `--migrate-keys` mode) |

Episode Dedup exists because the originating watcher keyed episodes on a per-submission ID instead of a stable per-check-in-cycle key, so a resend created a spurious second "duplicate" episode for what is really one unresolved condition. It auto-cleans only `checkin_not_submitted`; every other episode type with detected duplicates is explicitly flagged "requires manual review" and left untouched — for those, it only ever proposes-via-print, never acts. It also has a separate optional step, `migrate_reference_keys`, to rewrite surviving episodes from the old unstable key format to the new stable one.

None of the three writes to any table outside its own target — no cross-contamination between Worldview, Episodes, and each other.
