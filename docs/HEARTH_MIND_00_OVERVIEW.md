# Hearth Mind — Architectural Inventory (Phase 0)

**Version 1.0 — July 12, 2026**
**Status: Inventory of what exists today. Not a design document, not a roadmap.**

---

## Purpose

This is Phase 0 ("Inventory the Mind") of the Hearth Intelligence Layer initiative. Everything built so far has given Hearth a body: Identity, Constitution, Buildings, Furniture, Roads, State, Episodes, Worldview, Reflection, Traversal, Fact Extraction. Before building a general cognitive process that can use these systems together, this inventory documents what actually exists — as opposed to what any single docstring, comment, or memory claims exists. Several of the findings below are exactly that gap: a comment or module docstring asserting something the code no longer does (or never did).

This document set is organized by capability area, not by file. Each capability follows a fixed structure — Purpose, What it knows, What it can read, What it can write, What it can propose, What it can never do, Rooms touched, Dependencies, Consumers — so any two capabilities can be compared apples-to-apples.

Read [`HEARTH_MIND_99_CONFLICTS_AND_OPEN_QUESTIONS.md`](HEARTH_MIND_99_CONFLICTS_AND_OPEN_QUESTIONS.md) before making any Phase 1 architectural decision — it collects every documentation/reality mismatch, dead code path, and unresolved design tension found during this investigation, each with a recommendation but no decision made on Brian's behalf.

---

## Document map

| File | Covers |
|---|---|
| `HEARTH_MIND_00_OVERVIEW.md` | This file — architecture map, room taxonomy, system diagram |
| `HEARTH_MIND_01_IDENTITY_AND_CONSTITUTION.md` | `hearth_identity.py`, `hearth_principles.py`, `seed_hearth_identity.py` |
| `HEARTH_MIND_02_MEMORY_CORE_AND_SOUL.md` | `hearth_memory.py`, `hearth_soul.py`, `hearth_context.py`, `migrate_add_six_room_schema.py` |
| `HEARTH_MIND_03_FURNITURE.md` | `hearth_furniture.py`, `hearth_furniture_proposals.py`, `hearth_furniture_taxonomy.py`, furniture schema migration |
| `HEARTH_MIND_04_WORLDVIEW.md` | `hearth_worldview.py` and its six tables, worldview migrations |
| `HEARTH_MIND_05_TRAVERSAL_AND_ASK_HEARTH.md` | `hearth_traversal.py`, `hearth_ask.py` |
| `HEARTH_MIND_06_FACT_EXTRACTION.md` | `hearth_fact_extractor.py`, `hearth_comment_classifier.py`, backfill script |
| `HEARTH_MIND_07_PULSE_AND_EXPERIENCE_EVALUATOR.md` | `hearth_pulse.py`, `hearth_experience_evaluator.py` |
| `HEARTH_MIND_08_ENTITY_RESOLUTION_AND_RELATIONSHIPS.md` | `hearth_entity_resolution.py`, `hearth_relationships.py` |
| `HEARTH_MIND_09_QUESTIONS_AND_MAINTENANCE.md` | `hearth_questions.py`, `hearth_sounding_board.py`, the three dedup report scripts |
| `HEARTH_MIND_10_DAILY_BRIEF.md` | `morning_briefing.py` |
| `HEARTH_MIND_11_INFRASTRUCTURE.md` | `hearth_trace.py`, `hearth_gemini_config.py` |
| `HEARTH_MIND_12_PATHWAY_PORTAL_INTEGRATION.md` | The Watcher pattern, scheduler, admin routes, bridge, reader, seed/audit scripts (pathway-portal side) |
| `HEARTH_MIND_99_CONFLICTS_AND_OPEN_QUESTIONS.md` | Every documentation/reality mismatch and unresolved design tension found, with recommendations, decisions left to Brian |

---

## Two codebases, one mind

Hearth's "body" is split across two repositories that this inventory treats as one system:

- **`/Users/brianatchley/hearth/`** — Hearth's mind. Standalone Python modules (no Flask, no web framework) that read/write `hearth_memory.db` (a local SQLite file) and, in a few explicitly-documented cases, read Pathway Portal's own database directly.
- **`/Users/brianatchley/pathway-portal/backend/`** — the Flask web application. It hosts the admin UI, the APScheduler-based cron scheduler that drives Hearth's time-based cognition, and a bridge/reader layer (`app/hearth_reader.py`, `app/hearth_bridge.py`) that is the documented single gateway between Pathway code and `hearth_memory.db`.

A third location, **`/Users/brianatchley/pathway-portal/hearth/`**, is a deployed copy of the first — confirmed byte-identical on every file diffed during this investigation. The scheduler and CLI commands locate it via `HEARTH_PATH` (env var) or a relative-path fallback and `sys.path`-inject it at runtime. Nothing in the codebase automates keeping the two copies in sync; that is presently a manual/deploy-process step outside the scope of this inventory.

**The "Pathway never imports from hearth/" rule has more exceptions than any single docstring admits.** `hearth_reader.py`'s own docstring names three deliberate exceptions (`get_connected_context`, `ask_hearth`, the Furniture-proposal wrappers). In practice there are at least two more crossing points: `hearth_scheduler.py`'s pulse job imports `hearth_pulse`/`hearth_experience_evaluator` directly, and `commands.py`/`hearth_scheduler.py` import `hearth_fact_extractor`/`hearth_furniture_proposals` directly for the furniture-extractor job. Separately, `hearth_fact_extractor.py` (which lives in `hearth/`) opens a **direct, read-only SQLite connection to Pathway's own database** — a crossing in the opposite direction, not routed through `hearth_reader.py` at all. See the conflicts document for the full list.

---

## The room taxonomy — and its inconsistency

The project brief and most of the codebase refer to eight rooms: **Identity, Constitution, Furniture, State, Roads, Episodes, Worldview, Reflection.** This is the taxonomy used throughout this document set. But it is not the taxonomy used everywhere in the code itself:

- `migrate_add_six_room_schema.py`'s own docstring names six rooms — **Identity, Furniture, State, Relationships/Roads, Experience, Reflection** — using "Experience" where the rest of the codebase says "Episodes," and omitting Worldview and Constitution entirely, despite Worldview being one of the largest, most actively-written subsystems in the codebase (6 tables, 1003 lines in `hearth_worldview.py` alone) and despite `hearth_soul.py` treating Worldview as architecturally central to its own stated purpose.
- The word **"Constitution"** does not appear as a room, table, or module name anywhere in the code. The closest structural analog, `hearth_principles.py`, never calls itself "Constitution" — the only occurrence of the word in any `.py` file is a UI header string in `hearth_sounding_board.py` ("Constitutional rule: Hearth may suggest lessons. Humans approve lessons."), which reads as a policy statement, not a room label.
- **Reflection is two structurally unrelated tables**, both plausibly "the Reflection room": `hearth_reflections` (Soul's flat, non-entity-scoped, per-pipeline-run operational log — "a black-box log, not a journal") and `hearth_entity_reflection_refs` (an entity-scoped breadcrumb index into worldview/question rows, added by the six-room migration). Nothing in the code states which one *is* the Reflection room, or whether both are.

This is flagged, not resolved, per the instructions governing this investigation. See the conflicts document for the full detail and a recommendation.

---

## How to read the per-capability sections

Every capability in the following documents uses this exact structure:

- **Purpose** — what problem it solves
- **What it knows** — knowledge unique to this capability (thresholds, heuristics, taxonomies)
- **What it can read** — data sources, memory rooms, or systems it may inspect
- **What it can write** — memory or state it may directly create or modify; explicitly states "nothing" where true
- **What it can propose** — whether it creates proposals, and of what kind
- **What it can never do** — explicit architectural boundaries, enforced either by code (guards, read-only connections) or by convention (comments, docstrings) — the distinction between the two is called out wherever it matters, since a convention is not a guarantee
- **Rooms touched** — which of Identity / Constitution / Furniture / State / Roads / Episodes / Worldview / Reflection participate, or none
- **Dependencies** — what other Hearth modules it relies on
- **Consumers** — what currently calls it, with file:line citations where found. Where nothing calls it, that is stated explicitly — several fully-built capabilities in this codebase have zero production consumers today.

File:line citations throughout this document set refer to the state of the repository at commit `603a727` (the HEAD of `main` at the time this inventory was produced).
