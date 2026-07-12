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

- **Purpose**: Decide whether a Pulse-classified event is *meaningful relative to what Hearth already believes* (worldview), and optionally promote that meaning into a durable Episode. "The rule is not 'training_viewed becomes an episode.' The rule is 'a signal becomes an episode when it changes understanding relative to worldview.'" Stated pipeline: `Pulse (hearth_events) → Experience Evaluator → episode candidate → [future] episode → Soul → worldview`.
- **What it knows**: Keyword taxonomies for matching worldview text to situational categories (`_QUIET_STUCK_KEYWORDS`, `_WAITING_KEYWORDS`) and a conservative per-event-type resolution mapping (currently only `checkin_submitted`). A fixed decision tree: waiting-hit + matching resolution keyword → `resolution`; quiet/stuck-hit + positive-activity event type → `momentum`; waiting-hit with no resolution match → `concern`; no matching worldview entry → `no_action`.
- **What it can read**: Pathway's `hearth_events` (read-only, only rows already `processed=1` with `experience_level IN ('signal','observation')` — trace rows are never read here). `hearth_memory.db` read-only: `hearth_entities`, and worldview via `get_active_beliefs`/`get_open_uncertainties`/`get_watched_changes` scoped by subject.
- **What it can write**: Nothing by default. When `HEARTH_EXPERIENCE_EVALUATOR_PROMOTE=1` (default `0`), it writes rows into `hearth_episodes` via `hearth_memory.create_episode()`, with `episode_type ∈ {resolution, momentum, concern}` and `reference_key = "pulse_event_{id}"` for idempotent dedup. It never writes to `hearth_events` and never writes to worldview tables directly.
- **What it can propose**: Not in the literal `hearth_furniture_proposals`-style sense. Its default (`PROMOTE=0`) mode is the closest thing to a dry-run: it returns a `candidates` list and only logs "would promote signal... as {candidate_type}" — no backing table, not queryable after the run ends.
- **What it can never do**: Write `hearth_worldview_*` tables. Update `hearth_events`. Read/evaluate `trace`-level events (excluded by the query itself, not just by rule). Treat `message_sent` differently than any other unlisted event type — it isn't in the positive-activity set, so it can only ever reach `concern`/`no_action` for it, never `momentum`/`resolution`, consistent with the private-message exclusion carried over from Pulse.
- **Rooms touched**: Reads Identity and Worldview, read-only. Writes Episodes only when `PROMOTE=1`.
- **Dependencies**: `hearth_memory`, `hearth_worldview`. Also imports `hearth_identity.get_user_identity, is_staff_user` — but **neither name is referenced anywhere else in the file** (dead import, likely a vestige of a planned staff-filtering feature that was never built — see conflicts document).
- **Consumers**: `hearth_scheduler.py:hearth_pulse_job()`, immediately after Pulse, gated on `HEARTH_EXPERIENCE_EVALUATOR_ENABLED` (default on). No other module in `hearth/` imports this. On the Pathway side, `hearth_reader.py` explicitly names and excludes this module's output — `concern`/`momentum`/`resolution` episode types are filtered from every manager-facing dashboard query via `_exclude_promoted_evaluator_sql()`, because their `description` field is "internal Pulse-signal debug text... not written for manager consumption." They remain visible to `hearth_memory` and Soul, just not to the admin UI.

**Currently a write-only feature with no display consumer**: even with promotion enabled, its episodes are excluded from every dashboard query, including the one that feeds Coach Hub routing. As of this reading, no manager-facing surface displays them. Documented as intentional V1 scope in `hearth_reader.py`, not a bug — but worth naming plainly: the promotion feature exists in code and is not yet "live" in any user-facing sense.

---

## A third, unrelated "momentum"

Both `hearth_experience_evaluator.py` (episode_type `"momentum"`) and `hearth_soul.py` (belief_type `"engagement_momentum"`) use the word "momentum" for genuinely different mechanisms with no cross-reference between them:

- **Experience Evaluator's `momentum`**: a single-event, worldview-*gated* signal — fires only when an entity already has an open quiet/stuck-related worldview entry and a positive-activity event then arrives. Reactive to a pre-existing concern.
- **Soul's `engagement_momentum` belief**: an aggregate, rolling-14-day-window pattern — counts distinct eligible activity types regardless of whether any concern was ever open, forming/reinforcing a belief once ≥4 distinct types are seen. Has no dependency on Experience Evaluator at all.

Same English word, disjoint mechanisms, disjoint rooms (Episode vs. Worldview belief), disjoint tables. This is a genuine naming collision worth flagging for intelligence-layer design, not a duplicate implementation to consolidate — see `HEARTH_MIND_99_CONFLICTS_AND_OPEN_QUESTIONS.md`.
