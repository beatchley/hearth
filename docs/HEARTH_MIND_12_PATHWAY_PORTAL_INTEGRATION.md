# Hearth Mind Inventory — Pathway Portal Integration (Watchers, Scheduler, Admin Surfaces)

Covers `pathway-portal/backend/app/`: `hearth_reader.py`, `hearth_bridge.py`, `hearth_scheduler.py`, `hearth_calendar_watcher.py`, `hearth_worldview_audit.py`, `hearth_seed_buildings.py`, `commands.py`, plus Hearth-related routes in `routes/main.py`. This is where Hearth's mind meets the human-facing product.

---

## Community Calendar Watcher (`app/hearth_calendar_watcher.py`)

- **Purpose**: The first proof of the Watcher pattern — "observes structured source → creates proposal → human approves/dismisses → approved proposal writes to Hearth memory." Keeps two Building State keys (`next_session`, `event_status`) in sync with the Pathway Community Calendar.
- **What it knows**: How to select a single "authoritative" calendar event per Building when several match (recurring events always outrank one-time events; ties are treated as an ambiguous data condition and produce no proposal). Exact-string title/alias matching only — no fuzzy or AI matching. Derives `event_status` purely from three structured fields, no free-text parsing.
- **What it can read**: Pathway's `CommunityEvent` model (full app-DB access via SQLAlchemy), plus `hearth_entities`/state-proposal lookups via `hearth_reader.py` helpers.
- **What it can write**: Only `hearth_state_proposals` (via `create_state_proposal`) and `hearth_watcher_ambiguity_log` (once per run). Never touches the Pathway app DB directly, never touches `hearth_events` or chat tables.
- **What it can propose**: `next_session` and `event_status` proposals, at most one of each per Building per run, routed to `/admin/hearth/proposals`.
- **What it can never do**: Write State directly — per its module docstring and `commands.py`'s own comment ("Never writes State directly"), verified true in code: it never imports `add_state`/`update_state`, only `create_state_proposal`. The only path into `hearth_entity_state` is a human clicking Approve. Guess on an ambiguous calendar match.
- **Rooms touched**: State (via approval only); reads Identity.
- **Dependencies**: No LLM dependency at all — explicitly independent of `GEMINI_API_KEY` ("this Watcher never calls the LLM pipeline," `hearth_scheduler.py:315`).
- **Consumers**: CLI `flask run-community-calendar-watcher [--dry-run]`; APScheduler job daily at 6:00 AM CT; admin review UI at `/admin/hearth/proposals`.

---

## Hearth Reader (`app/hearth_reader.py`) — the documented single gateway

- **Purpose**: The single read/write gateway to `hearth_memory.db` for everything Pathway-side. Its own docstring states the rule explicitly: "Pathway code never imports from the `hearth/` directory — this module opens `hearth_memory.db` directly via sqlite3," with **three named exceptions**: `get_connected_context` (imports `hearth_traversal`), `ask_hearth` (imports `hearth_ask`), and the Furniture-proposal wrapper functions (import `hearth_furniture_proposals`/`hearth_furniture_taxonomy`) — all via `sys.path` injection. See the conflicts document for two additional, undocumented crossing points found elsewhere in the scheduler/commands layer.
- **What it knows**: Business rules for every room, encoded as Python functions rather than raw SQL scattered across routes: Furniture never hard-deletes (supersede/retract only); State keeps exactly one current row per (entity, key) plus an append-only history table; Roads reactivate rather than duplicate under a UNIQUE constraint; canonical-key slugging for manually-created Buildings; the exclusion filter for Experience-Evaluator-promoted episodes (see `HEARTH_MIND_07`).
- **What it can read/write**: All of `hearth_memory.db`'s room tables through well-scoped functions (`add_furniture`/`edit_furniture`/`retract_furniture`, `add_state`/`update_state`, `create_manual_building`, `add_manual_road`/`retire_manual_road`/`reactivate_manual_road`, `create_state_proposal`/`approve_state_proposal`/`dismiss_state_proposal`, `answer_question`/`acknowledge_question`/`resolve_question`). It does **not** read Pathway app tables itself — callers enrich separately.
- **What it can never do**: Write to the Pathway app DB — strictly `hearth_memory.db` via raw `sqlite3`.
- **Rooms touched**: All eight (Identity, Furniture, State + history, Roads, Episodes (read), Worldview (beliefs/uncertainties/changes/lessons), plus a Proposals sub-layer for State and Furniture). Constitution is not exposed through this reader — no `hearth_principles` functions were found in it.
- **Consumers**: Nearly every `/admin/hearth/*` route, `hearth_scheduler.py`, `hearth_calendar_watcher.py`, `hearth_worldview_audit.py`, `hearth_seed_buildings.py`.

---

## Hearth Bridge (`app/hearth_bridge.py`) — delivery, not a Watcher

- **Purpose**: One-way delivery of Hearth-generated text into two Pathway chat surfaces. "Hearth (or Pathway acting on Hearth's behalf) calls these `deliver_*` helpers; Pathway never imports from `hearth/`... can be swapped for an HTTP API call without changing anything else."
- **What it knows**: Same-day duplicate-suppression per channel (exact message text + `is_hearth=True` + today's date range).
- **What it can write**: `AdminChatMessage` rows and `RoleHubChatMessage` rows (Pathway app DB, `is_hearth=True`, no human author). Never touches `hearth_memory.db`.
- **What it can never do**: Crash the caller — every path wrapped in try/except with explicit rollback on failure.
- **Footgun**: `deliver_hearth_coach_hub_message` returns `False` for both "duplicate, nothing to do" and "real delivery error" (documented). Today's one caller handles this correctly, but any future caller that branches on `False` meaning specifically "error" would silently miscount duplicate-skips as failures.
- **Consumers**: `commands.py:_run_hearth_brief` (manager chat); `main.py:_notify_coach_hub_concern` (Coach Hub chat).

---

## Hearth Scheduler (`app/hearth_scheduler.py`)

- **Purpose**: In-process APScheduler background thread driving all of Hearth's time-based cognition, because Render's disk-per-service model means a separate Cron service can't share the SQLite disk.
- **What it knows**: The full daily cadence, and *why* jobs are ordered this way — Furniture Extractor (5:30 AM) and Calendar Watcher (6:00 AM) both explicitly scheduled ahead of the 7:00 AM Daily Brief, "so any new proposals are ready for a manager to review first thing."

| Job | Cadence (America/Chicago) | Requires Gemini |
|---|---|---|
| `hearth_furniture_extractor_job` | 5:30 AM daily | Yes |
| `hearth_daily_job` (brief) | 7:00 AM daily | Yes |
| `hearth_midday_job` (scan) | 12:00 PM daily | Yes |
| `hearth_evening_job` (scan) | 6:00 PM daily | Yes |
| `hearth_calendar_watcher_job` | 6:00 AM daily | No |
| `hearth_pulse_job` (Pulse + Experience Evaluator + Coach Hub routing) | every 30 min | No |

- **What it can never do**: Raise out to crash Pathway — every job body and the scheduler startup itself are wrapped in try/except; `start_hearth_scheduler` returns `None` on any startup failure rather than raising.
- **Dependencies**: `app.commands._run_hearth_brief`/`_run_hearth_scan`, and — via direct `sys.path` injection, not through `hearth_reader.py` — `hearth_pulse`, `hearth_experience_evaluator`, `hearth_fact_extractor` (see conflicts document for why this matters to the "Pathway never imports from hearth/" framing).

---

## Hearth Worldview Audit (`app/hearth_worldview_audit.py`)

- **Purpose**: A read-only self-diagnostic — "Can Hearth still trust its own memory enough to reason well?" Eight independent Auditors (Belief, Uncertainty, Road, Furniture, State, Building, Proposal, Provenance), each inspecting one domain, never writing or repairing anything.
- **What it knows**: Hardcoded heuristic thresholds (`STALE_BELIEF_DAYS=60`, `LONG_OPEN_UNCERTAINTY_DAYS=30`, `QUIET_BUILDING_DAYS=45`, `SILENT_WATCHER_DAYS=30`, etc.) and a hand-maintained registry (`EXPECTED_STATE_KEYS`, `KNOWN_WATCHERS = ["community_calendar"]`) explicitly noted as needing manual extension as new conventions/Watchers ship — no formal registry exists.
- **What it can read**: `hearth_memory.db` exclusively, via bulk `hearth_reader.py` helpers added specifically for this module.
- **What it can write**: Nothing — guaranteed by the module's own claim that every helper it calls only ever executes SELECT statements.
- **What it can propose**: Not proposals in the Watcher sense — a findings report (`critical`/`warning`/`observation`) for humans to act on manually.
- **What it can never do**: Repair, write, or let one Auditor's failure take down the whole report (each isolated via `_failure_finding`).
- **Consumers**: `/admin/hearth/worldview-audit` — manually triggered only, no scheduling, no caching, no stored history.

---

## Hearth Seed Buildings (`app/hearth_seed_buildings.py`)

- **Purpose**: One-time, idempotent seed of Hearth's first three non-person production Buildings (Pathway org, Pathway Unmuted recurring_event, Pathway Merch program) plus their Furniture/State and the Roads connecting them.
- **What it knows**: Per-room-type idempotency contracts (Building: reuse-by-canonical-key; Furniture: skip-if-exact-text-exists; State: leave-alone-if-set; Road: skip-if-active/reactivate-if-historical); a conservative, two-step lookup for "who is Stacy" (role='ceo' first, display-name-contains-'stacy' fallback second, refusing to guess on ambiguity).
- **What it can write**: `hearth_entities`, `hearth_entity_furniture`, `hearth_entity_state`, `hearth_relationships`, all via `hearth_reader.py`'s own functions (the same ones the manual admin UI uses).
- **Consumers**: CLI `flask seed-hearth-buildings [--dry-run]`.

---

## `record_hearth_event` (Pulse's event emitter, in `routes/main.py`)

- **Purpose**: Pathway-side trace-event logger feeding Pulse. Explicit boundary in its own docstring: "Pathway only emits events here — Hearth owns all interpretation of importance/concern level, never Pathway" (:75-79).
- **What it can write**: `hearth_events` in the **Pathway app DB** (not `hearth_memory.db`) — `experience_level='trace'` always at write time; Pulse does the interpreting later. Never raises; rolls back on failure.
- **Call sites**: 7 confirmed locations across `main.py` (training views, check-ins, etc.) — all Category A organizational-activity events, consistent with the sensory policy.

---

## `_build_manager_dashboard` / Coach Hub routing (`routes/main.py`)

- **Purpose**: Turns open concern episodes into (a) manager-facing dashboard boxes on `/admin/hearth-city` and (b) proactive Coach Hub chat notifications — the *same* function reused for both surfaces.
- **What it knows**: A three-tier concern-prioritization scheme (human-waiting-on-human is never suppressed; capped/cooldown-eligible action items; inactivity, summary-only) plus a 5-day coach-hub notification cooldown and a race-safe compare-and-swap claim pattern against a notification ledger to avoid duplicate sends — the code comments reference a real incident: one recipient got 6 identical messages in ~4 minutes on July 4.
- **Rooms touched**: None in `hearth_memory.db` (read-only consumer of already-written episodes); writes only to Pathway-app-DB notification/chat tables.

---

## Full surface inventory

**Admin routes** (`routes/main.py`):

| Route | Purpose |
|---|---|
| `/admin/hearth-city` | Main dashboard: entities, episodes, KPIs, manager boxes, worldview panel |
| `/admin/hearth/ask` (GET/POST) | Ask Hearth free-text Q&A box |
| `/admin/hearth/questions` (+ answer/acknowledge/resolve) | Surfaced-question review |
| `/admin/hearth/proposals` (+ approve/dismiss) | State-proposal review queue |
| `/admin/hearth/furniture-proposals` (+ approve/dismiss) | Furniture-proposal review queue |
| `/admin/hearth/buildings/new` | Create a manual (non-person) Building |
| `/admin/hearth/buildings/<id>/furniture` (+ add/edit/delete) | Building's Furniture |
| `/admin/hearth/buildings/<id>/state` (+ add/edit) | Building's State + history |
| `/admin/hearth/buildings/<id>/relationships` (+ add/retire/reactivate) | Building's Roads |
| `/admin/hearth/buildings/<id>/inspector` | Full per-Building rollup — the "Building Inspector" named in the project brief |
| `/admin/hearth/worldview-audit` | Run the 8 Worldview Auditors on demand |

**CLI commands** (`app/commands.py`): `hearth-brief`, `seed-hearth-buildings [--dry-run]`, `run-community-calendar-watcher [--dry-run]`, `run-furniture-fact-extractor [--dry-run] [--batch-limit N]`, `normalize-coach-hub-fingerprints [--apply]` (plus non-Hearth commands: `seed-categories`, `promote-ceo`, `seed-ceo`).

---

## Sensory-policy spot check

A grep of every Hearth-side integration file for Pathway's private-DM models (`PrivateMessage`, `Conversation`) found zero references outside `main.py`'s own unrelated DM-feature routes — consistent with `HEARTH_SENSORY_POLICY.md`'s Category B exclusion. No evidence of a violation was found in the files covered by this document. This should be spot-checked again against `hearth_pulse.py`/`hearth_context.py` specifically (covered in `HEARTH_MIND_07` and `HEARTH_MIND_02`), since those are the modules that actually decide what becomes a Pulse event or episode — no violation was found there either, but the check was performed independently by a different reviewer and is worth a second look in Phase 1 given how load-bearing this boundary is.

For the two undocumented `hearth/`↔`pathway-portal` boundary crossings found beyond `hearth_reader.py`'s three declared exceptions, and the "Never writes State/Furniture directly" docstring claims (verified true for State, only partially verifiable for Furniture within this document's scope), see `HEARTH_MIND_99_CONFLICTS_AND_OPEN_QUESTIONS.md`.
