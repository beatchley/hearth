# Hearth Mind Inventory — Pulse & Experience Evaluator

Covers: `hearth_pulse.py`, `hearth_experience_evaluator.py`. These sit in a strict pipeline, not in parallel: Pulse runs first, Experience Evaluator runs second, both wired sequentially in `pathway-portal/backend/app/hearth_scheduler.py:hearth_pulse_job()` every 30 minutes.

**They are not duplicates.** Pulse measures whether one raw Pathway event, in isolation, is routine or noteworthy for that actor's own history of that event type — it never consults Worldview for its actual decision, and its output is written back onto the *same* event row it read, inside Pathway's own database. Experience Evaluator measures whether an event Pulse already flagged changes what Hearth currently believes (per Worldview) — it never re-derives importance, and its output (when enabled) is a *new* Episode row in Hearth's own memory database. Full detail below.

---

## Hearth Pulse (`hearth_pulse.py`)

- **Purpose**: Triage raw Pathway activity into an importance/urgency signal before anything else in Hearth looks at it. "Pathway only ever emits trace-level events into `hearth_events`; Hearth owns all interpretation of importance and concern level."
- **What it knows**: A hand-written classification rulebook, one function per `event_type`, dispatched from `classify_event()`. Classification is almost entirely recency/frequency-based ("how many days since this actor's last event of this type," or "is this the first one ever"), mapped to `(experience_level, importance_score, importance_reason)` where `experience_level ∈ {trace, signal, observation}`. `checkin_submitted` additionally does a cross-table lookup into `hearth_episodes` to enrich (not change) the reason text. `onboarding_step_completed` additionally reads Pathway's `onboarding_records` to detect full completion. `message_sent` (private DMs) is hard-coded to `trace`/0.0 with an explicit architectural note that private creator-to-creator conversation is permanently excluded.
- **What it can read**: Pathway's own `hearth_events` table and `onboarding_records` (both in Pathway's `app.db`, a **separate database from `hearth_memory.db`**); `hearth_memory.db` only conditionally (episode lookup for `checkin_submitted` events); since a recent session, a worldview snapshot via `hearth_worldview.get_worldview_snapshot()` — capped small, wrapped in a broad try/except so any worldview failure never blocks Pulse.
- **What it can write**: Only `hearth_events.processed`/`.experience_level`/`.importance_score`/`.importance_reason` — in Pathway's own `app.db`. It **never** writes to `hearth_memory.db` at all (its only call into that database is a read).
- **What it can never do**: Write to `hearth_memory.db`; write to Worldview; process private messages beyond a fixed `trace`/0.0 stamp (permanent policy, not a TODO).
- **Rooms touched**: None of the eight named rooms directly — `hearth_events` is a pre-room "sensory intake" layer in Pathway's own database, not part of the room schema at all. Its one read into `hearth_memory.db` (episode lookup) touches Episodes read-only; the worldview snapshot read touches Worldview read-only.
- **Dependencies**: `hearth_memory` (read-only), `hearth_worldview` (lazy import, read-only, exception-isolated).
- **Consumers**: `hearth_scheduler.py:hearth_pulse_job()`, `IntervalTrigger(minutes=30)`, which immediately after also calls `hearth_experience_evaluator.run_experience_evaluator()`. Nothing else in `hearth/` imports this module.

**Live-but-unused cost**: The production entry point (`process_unprocessed_hearth_events()`) calls the worldview-snapshot-fetching detailed function *without* `include_worldview_context=True`, so per its own branching logic the snapshot is computed and then thrown away every 30 minutes — never logged, never used. Only manual/local runs (the `__main__` block) actually surface it. A real but currently invisible cost, matching the module's own honest self-description ("purely informational for now").

---

## Hearth Experience Evaluator (`hearth_experience_evaluator.py`)

**Architecture decision: momentum-only, permanently.** The Experience
Evaluator detects momentum/trend patterns from general Pathway activity —
nothing else. Concern and resolution detection are permanently out of
scope for this module, by deliberate architecture decision, not because
the rules haven't been written yet or Pathway hasn't emitted the right
events yet. Concerns and resolutions belong exclusively to purpose-built
Watchers (`morning_briefing.py`'s `detect_*`/`resolve_*` functions), which
have real, specific knowledge of the condition they check — an unanswered
training comment, a missing Discord invite, a check-in still awaiting
feedback. This module only ever sees Pulse's general activity stream; it
has no comparable specific knowledge, and guessing at it from general
activity is exactly what caused a real production defect: duplicate and
contradictory episodes affecting active entities 11 and 39, plus invalid
episodes on the 15/16 duplicate-inactive-entity pair. The fix (a durable
per-event ledger, structural target discovery, deleting the false
`checkin_submitted → checkin_feedback_waiting` resolution mapping and the
generic "any event + any waiting item = concern" fallback) was originally
scoped as "no live resolution/concern rule exists *yet*"; a follow-up
decision made it permanent instead — see the module's own docstring
("Permanent scope: momentum only") for the full reasoning, and
`hearth_experience_evaluator_cleanup.py` for the one-time repair of the
affected entities.

- **Purpose**: Decide whether a Pulse-classified event is a genuine
  momentum/reactivation signal *relative to what Hearth already believes*
  (worldview), and optionally promote that into a durable Episode.
- **What it knows**: One allowlisted rule table (`_MOMENTUM_RULES`) matching
  positive-activity event types against structurally-identified "quiet/stuck"
  worldview targets — `hearth_soul`-authored `entity_episode` uncertainties
  (`new_creator_stuck`) and `creator_quiet_entity` watched changes. Target
  discovery is structural (subject_type/subject_id equality), not keyword
  text-matching. There is no resolution-rule table and no concern-rule
  table — not empty ones, no code path that could hold one.
- **What it can read**: Pathway's `hearth_events` (read-only, only rows
  already `processed=1` with `experience_level IN ('signal','observation')`
  — trace rows are never read here). `hearth_memory.db` read-only:
  `hearth_entities`, and worldview via `get_living_uncertainties`/
  `get_watched_changes` scoped by subject. Its own durable ledger,
  `hearth_experience_evaluations` — one row per source event, recording the
  terminal classification, exact matched target, and rule that fired.
- **What it can write**: Nothing by default. When
  `HEARTH_EXPERIENCE_EVALUATOR_PROMOTE=1` (default `0`), it writes rows
  into `hearth_episodes` via `hearth_memory.create_episode()`, with
  `episode_type = 'momentum'` (the only value it ever writes) and
  `reference_key = "pulse_event_{id}"` for idempotent dedup, hardened by a
  database-level partial unique index. It never writes to `hearth_events`
  and never writes to worldview tables directly. A source event is
  evaluated at most once, ever — a later `EVALUATOR_VERSION` bump does not
  cause it to be reconsidered; that would require a separate, deliberately
  human-invoked tool.
- **What it can never do**: Write `hearth_worldview_*` tables. Update
  `hearth_events`. Read/evaluate `trace`-level events. Produce a
  `resolution` or `concern` episode, under any circumstances — structurally,
  not just by policy.
- **Rooms touched**: Reads Identity and Worldview, read-only. Writes
  Episodes only when `PROMOTE=1`.
- **Dependencies**: `hearth_memory`, `hearth_worldview`.
- **Consumers**: `hearth_scheduler.py:hearth_pulse_job()`, immediately after
  Pulse, gated on `HEARTH_EXPERIENCE_EVALUATOR_ENABLED` (default on). No
  other module in `hearth/` imports this. On the Pathway side,
  `hearth_reader.py` and `hearth_memory.py` both exclude this module's
  promoted rows from every manager-facing/trusted-evidence consumer
  (Daily Brief, Soul, manager dashboards) until promotion has been
  independently verified — see `hearth_memory.EVALUATOR_PROMOTED_EPISODE_TYPES`.

**Currently a write-only feature with no display consumer**: even with
promotion enabled, its episodes are excluded from every dashboard query,
including the one that feeds Coach Hub routing. As of this reading, no
manager-facing surface displays them. Documented as intentional scope in
`hearth_reader.py`, not a bug.

---

## A third, unrelated "momentum"

Both `hearth_experience_evaluator.py` (episode_type `"momentum"`) and `hearth_soul.py` (belief_type `"engagement_momentum"`) use the word "momentum" for genuinely different mechanisms with no cross-reference between them:

- **Experience Evaluator's `momentum`**: a single-event, worldview-*gated* signal — fires only when an entity already has a living quiet/stuck-related worldview target and a positive-activity event then arrives. Reactive to a pre-existing quiet/stuck situation — not to be confused with the (removed, permanently out-of-scope) `concern` episode_type, a different and unrelated concept.
- **Soul's `engagement_momentum` belief**: an aggregate, rolling-14-day-window pattern — counts distinct eligible activity types regardless of whether any concern was ever open, forming/reinforcing a belief once ≥4 distinct types are seen. Has no dependency on Experience Evaluator at all.

Same English word, disjoint mechanisms, disjoint rooms (Episode vs. Worldview belief), disjoint tables. This is a genuine naming collision worth flagging for intelligence-layer design, not a duplicate implementation to consolidate — see `HEARTH_MIND_99_CONFLICTS_AND_OPEN_QUESTIONS.md`.
