# Hearth Mind Inventory — Daily Brief (`morning_briefing.py`)

1866 lines — the largest file in the codebase and Hearth's original proof of concept. It orchestrates most of the other capability areas documented elsewhere in this set; this document covers what's unique to it as an orchestrator, and defers to the other documents for the modules it calls into.

**Trigger/scheduling**: CLI entrypoint (`--scan`, `--force-brief`, `--send-brief`), but in production it's invoked via `run_pipeline(...)`, called directly from `pathway-portal/backend/app/commands.py`'s `_run_hearth_brief`/`_run_hearth_scan`, which are in turn called by `hearth_scheduler.py`'s APScheduler cron: `hearth_daily_job` (7:00 AM CT, brief+deliver), `hearth_midday_job` (12:00 PM CT, scan-only), `hearth_evening_job` (6:00 PM CT, scan-only).

**Delivery**: `morning_briefing.py` itself never delivers anywhere — `run_pipeline()` returns text (or `None`). Delivery is entirely external: `commands.py:_run_hearth_brief` calls `app.hearth_bridge.deliver_hearth_message(text)`, which inserts a row into Pathway's `admin_chat_messages` table (manager chat, not Slack). This means the module's own docstring claim of a pipeline `Pathway Data → Hearth Memory → Hearth Awareness Context → Gemini → Hearth Message` is one step short of the truth — `morning_briefing.py`'s actual boundary is "read + detect/write-memory + compose," not "read + compose + deliver."

**Imports**: `hearth_identity`, `hearth_memory`, `hearth_questions`, `hearth_relationships`, `hearth_context`, `hearth_soul`, `hearth_trace`, `hearth_gemini_config`. Does **not** import `hearth_worldview.py`, `hearth_principles.py`, `hearth_pulse.py`, or any Furniture-room module directly — those are reached only transitively through `hearth_context.py`/`hearth_soul.py`. **The Furniture room is entirely absent from this file's dependency graph** — no import or transitive call touches `hearth_entity_furniture`/`hearth_furniture_proposals` anywhere in this file or its direct dependencies. Furniture-room work is a fully separate pipeline, scheduled independently at 5:30 AM CT.

---

## Operational Query Layer

- **Purpose**: Pull today's raw Pathway signals (new users, battles, comments, check-ins, support threads, creator activity) into memory for downstream detection.
- **What it knows**: SQL knowledge of ~15 Pathway tables and the business thresholds for "stale" (`CHECKIN_FEEDBACK_WAITING_DAYS=3`, `TRAINING_COMMENT_WAITING_DAYS=3`, `SUPPORT_REQUEST_WAITING_DAYS=3`, `NEW_CREATOR_STUCK_DAYS=14`).
- **What it can read**: 13 named query functions, orchestrated by `collect_data()`. All connections opened `mode=ro`. Private creator-to-creator messages are explicitly, permanently excluded by design (:575-577).
- **What it can write/propose**: Nothing — pure read layer.
- **Consumers**: Called once per `run_pipeline()` invocation.

## Stale-Issue Auto-Resolution

- **Purpose**: Close episodes whose real-world condition has cleared.
- **What it knows**: Per-episode-type resolution conditions for 9 episode types.
- **What it can write**: `hearth_episodes.resolved`/`.resolved_at`, gated per-type so a failed query never causes a false resolution.
- **What it can never do**: Resolve `training_comment_needs_response` — there is no branch for it (see Surprises below); episodes of this type accumulate until manually resolved.

## Legacy Episode Migration

- **Purpose**: Permanently close out `unlinked_battle` episodes created under an obsolete assumption (that a NULL opponent_id is always a concern, when it's normal for external-agency opponents with no Pathway account).
- **Status**: Called unconditionally every run, labeled "idempotent — safe to run every startup," but **is not yet a no-op in production** — the live DB currently still has 2 open `unlinked_battle` episodes being actively resolved by this "migration" on every run.

## Core Issue/Episode Detection Engine

- **Purpose**: The primary "notice things" capability — 6 conditions become episodes: `probation`, `missing_discord`, `training_comment_needs_response`, `training_no_engagement`, `checkin_not_submitted`, `creator_quiet`.
- **What it knows**: A hand-built keyword heuristic (`_comment_needs_response()`) for whether a training comment needs a response, using flag-keywords and a positive-signal suppression list, plus its **own, separately-defined** `_STAFF_ROLES` set (see Surprises).
- **What it can never do**: Detect "battle concern" issues — explicitly, permanently deferred; the module lists 5 unbuilt signals with a comment stating detection is "intentionally absent" (:1259-1268), a documented blind spot, not an oversight in progress.

## Four Dedicated Watchers

Check-in Feedback Waiting, Training Comment Waiting, Support Request Waiting, and New Creator Stuck each follow the same shape: a query function that reads today's Pathway data, a detection function that creates/refreshes an episode. Training Comment Waiting depends on the externally-maintained `comment_type` classification (see `HEARTH_MIND_06_FACT_EXTRACTION.md`) and is the canonical, currently-maintained sibling of the legacy Core-Engine training-comment detector (see Surprises). New Creator Stuck deliberately excludes passive `page_visits` from its engagement-signal set.

## Hearth Voice / Message Generation

- **Purpose**: The only place an LLM is invoked in this file. Turns assembled context into natural-language morning-briefing prose.
- **What it knows**: `HEARTH_SYSTEM_PROMPT` — voice rules (warm, calm, specific; no headers/bullets/emojis; open with "Good morning team."), plus a feature-flagged worldview-guidance section teaching the model how to weigh beliefs/uncertainties/watched-changes/lessons against today's raw evidence.
- **What it can never do**: See raw database rows/table/column names (context is fully pre-translated by `hearth_context.py`); mention Gemini/AI/databases/queries/statistics.
- **Dependencies**: `hearth_context.render_for_llm`, `hearth_gemini_config.GEMINI_MODEL_NAME`.

## Pipeline Orchestration & Scan-Mode Control

- **Purpose**: The top-level conductor. `scan_mode` differentiates `"morning"` (send_brief defaults True) from `"midday"`/`"evening"`/`"manual"` (defaults False — watchers + reflection only, no Gemini call).
- **What it knows**: A daily duplicate-send guard that queries Pathway's `admin_chat_messages` table directly (`is_hearth=1 AND DATE(created_at)=today`) — this is the one place Hearth treats a Pathway-owned table as its own bookkeeping ledger rather than recording "brief sent" state in `hearth_memory.db`, a minor inconsistency against the otherwise strict "Pathway is truth, Hearth remembers" separation stated elsewhere.
- **What it writes** (by calling into other modules, spanning several rooms in one run): entity sync (Identity), relationship discovery (Roads), all episode writes from detection/watchers (Episodes), entity summary rollups, a reflection row plus possible worldview writes and an auto-question (via `hearth_soul.generate_reflection()`), and — via `hearth_context.build_context()` — marks `hearth_principles` rows as used (Constitution).
- **What it can never do**: Touch Furniture at all (see above). Send more than one daily brief per day unless `force_brief=True`.
- **Consumers**: `main()` (CLI), and `pathway-portal/backend/app/commands.py` (production path).

---

## Surprises specific to this file

**Duplicated, non-communicating "training comment needs a response" logic.** Two separate, parallel watchers exist for the same real-world event:
- **Legacy path** — the Core Detection Engine's `_comment_needs_response()` heuristic, scanning the last 48 hours by keyword, creating `training_comment_needs_response`. This type has **no resolution branch** in `resolve_stale_issues()` — the module admits it outright: "stays open until manually resolved; auto-resolution requires a Pathway query for subsequent manager responses on the same training (TODO for a future version)" (:675-677). These episodes accumulate forever once created.
- **Current path** — `query_training_comment_waiting()`/`detect_training_comment_waiting()`, using the DB-persisted `comment_type` classification and an explicit staff-response check, creating `training_comment_waiting`, which **is** auto-resolved.
- The file also defines its own, second `_STAFF_ROLES` set (`{"admin","manager","coach"}`, :915) for the legacy path — distinct from `hearth_identity.STAFF_ROLES`, which the current path correctly imports and uses elsewhere in the *same file*. See `HEARTH_MIND_99` for the full staff-roles drift picture.

Full detail on both of these, plus the daily-brief-state-lives-in-Pathway inconsistency and the legacy-migration-not-yet-a-no-op finding, is in `HEARTH_MIND_99_CONFLICTS_AND_OPEN_QUESTIONS.md`.
