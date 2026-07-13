# Hearth V1 Tool Subset — Toxie/Ethan Scenario

**Scope note**: This document selects the specific tools needed for one scenario — Toxie asking about Ethan — and is a scenario-scoped tool profile, not a general-purpose tool registry. A future, broader "registry" concept may eventually catalog all safe tools independent of any specific scenario, with individual scenarios each selecting a subset from it (similar in spirit to how this document already sorts tools into groups, but generalized beyond just this one scenario). That broader registry does not exist yet and is not being built now — this note exists only to prevent this document's filename and scope from being mistaken for that larger concept later.

**Status: documentation only.** No code was written or modified to produce this document, no tool registry or calling mechanism exists yet, and no existing function's behavior changed. This is a follow-up to `HEARTH_COGNITIVE_TOOLS.md` (the Phase 2 catalog) that narrows that catalog down to the minimal, safe subset the roadmap's first bounded reasoning scenario (Phase 4) actually needs, and resolves two specific problems Phase 2 already flagged so Phase 3/4 can build on a settled list instead of the raw catalog.

**The scenario**: Toxie asks Hearth — *"I noticed Ethan has not been going live much lately and I was thinking about reaching out to him. What do you think?"*

**Note on catalog size**: The Phase 2 catalog contains **52 distinct `###`-level entries**, not 48 — some entries document more than one closely-related function under one heading (e.g. "Seeded Organizational Identity Facts" covers both `get_identity()` and `get_identity_value()`). This document sorts all 52 entries. See the completion report for this discrepancy.

---

## Task 1 — Four-Group Sort

**How the groups were applied**: Group 3 (unsafe) is evaluated first and is scenario-independent — a tool with a write side effect or hidden behavior is excluded from any future registry regardless of whether this scenario needs it, because the point is that a reasoning layer must never trigger one unknowingly. Everything else is then judged strictly against whether the Toxie/Ethan scenario has a concrete use for it: tools actually needed land in Group 1 (usable as-is) or Group 2 (needs a wrapper first); everything not needed for this specific scenario — including tools that are perfectly safe and well-built — lands in Group 4. Group 4 is the largest group by design; this scenario is deliberately narrow.

| # | Tool (catalog heading) | Group | Reason |
|---|---|---|---|
| 1 | Resolve Building by Name | 2 — needs wrapper | **Selected.** Requires an externally-managed `memory_conn` and returns an `EntityResolution` dataclass wrapping a raw `sqlite3.Row` — needs a thin adapter to be callable with primitive args and return JSON-serializable output. |
| 2 | Resolve Building by Pathway User ID | 4 — not needed | Ethan is identified by name in the question, not by a Pathway `user_id` already in hand. |
| 3 | Scan Text for Known Buildings | 4 — not needed | The scenario names one Building directly; there's no block of free text to scan for mentions. |
| 4 | Search Buildings by Name Fragment | 4 — not needed | `resolve_entity()`'s exact/ambiguous-aware match already suffices for a specific name like "Ethan." |
| 5 | Look Up Building by ID | 4 — not needed | Once resolved, Ethan's Identity row is already returned inside `get_connected_context()`'s output. |
| 6 | Look Up Building by Canonical Key | 4 — not needed | Ethan is a person Building, not a manually-created org/event/program. |
| 7 | Look Up Building by Pathway User ID (Pathway-side) | 4 — not needed | Same reason as #2. |
| 8 | List All Other Buildings | 4 — not needed | The scenario is single-Building-scoped, not a global listing. |
| 9 | Building Compact Snapshot | 4 — not needed | Its content is already returned inside `get_connected_context()`'s `source.summary` — calling it separately would be redundant. |
| 10 | Building Context-Volume Pointers | 4 — not needed | Already invoked internally by, and returned inside, `get_connected_context()`. |
| 11 | Full-Fidelity Road Reader (`_get_active_roads`) | 4 — not needed | Not callable in isolation anyway, and `get_connected_context()` already surfaces Roads. |
| 12 | **One-Hop Connected Context** | **1 — usable now** | **Selected.** Self-manages its own connection (via the Pathway-side wrapper) and returns plain dicts — no adapter needed. |
| 13 | Ask Hearth (Full Question Answering) | 4 — not needed | Its deterministic 3-pattern router cannot classify this open-ended, opinion-seeking question — it would return `status="unsupported"`. See Task 3 note. |
| 14 | Tell-Me-About / Connected-To Retrieval (`_answer_entity_question`) | 4 — not needed | Private, and its retrieval is identical to calling `get_connected_context()` directly. |
| 15 | Who-Needs-Attention-Today Retrieval (`_answer_needs_attention_today`) | 4 — not needed | This scenario is about one named Building, not a "who needs attention" sweep. |
| 16 | Active Furniture Facts for a Building | 4 — not needed | `get_connected_context()` already includes Ethan's most recent Furniture facts. |
| 17 | Furniture Facts by Status (Pathway-side) | 4 — not needed | Same reason as #16; full/non-active history isn't relevant here. |
| 18 | Furniture Category Vocabulary | 4 — not needed | Nothing in this scenario proposes or validates a new Furniture fact. |
| 19 | Pending Furniture Proposals Queue | 4 — not needed | Not about unreviewed Fact-Extractor proposals. |
| 20 | Current State Values for a Building | 4 — not needed | `get_connected_context()` already includes Ethan's current State. |
| 21 | Single Current State Value | 4 — not needed | No single known State key is central to this question. |
| 22 | State Change History | 4 — not needed | The trend question is better answered by Episodes/Watched Changes than by State history. |
| 23 | Open Episodes | 4 — not needed | Superseded for this scenario by `get_episodes_for_entity()`, which gives a fuller any-status recent timeline for one Building in a single call. |
| 24 | Recent Episodes (Any Status) | 4 — not needed | Global/unscoped feed; the scenario needs episodes for Ethan specifically. |
| 25 | Recently Resolved Episodes | 4 — not needed | Not essential to establishing whether Ethan has gone quiet. |
| 26 | Building + Open Episodes + Total Count | 4 — not needed | `get_episodes_for_entity()` gives a more directly useful any-status recent timeline for this specific claim. |
| 27 | **Recent Episodes for a Building (Pathway-side)** | **1 — usable now** | **Selected.** Self-manages its own connection and returns plain dicts — no adapter needed. |
| 28 | Seeded Organizational Identity Facts | 4 — not needed | Not about static seeded org facts (e.g. "who is the CEO"). |
| 29 | **Active Beliefs About a Subject** | **2 — needs wrapper** | **Selected.** Requires an externally-managed `conn` and returns `list[sqlite3.Row]` — needs a thin adapter. |
| 30 | Interpreted Relationship Dynamics | 4 — not needed | A dead capability today (0 rows in production) — would return nothing. |
| 31 | **Open / Living Uncertainties** | **2 — needs wrapper** | **Selected** (the `get_living_uncertainties()` variant). Requires an externally-managed `conn` and returns `list[sqlite3.Row]` — needs a thin adapter. |
| 32 | **Watched Changes** | **2 — needs wrapper** | **Selected.** Requires an externally-managed `conn` and returns `list[sqlite3.Row]` — needs a thin adapter. |
| 33 | Recent Provisional Lessons | 4 — not needed | Not scoped to a single Building's subject in a way this question calls for. |
| 34 | Full Worldview Snapshot | 4 — not needed | Unscoped to one Building (returns everything, capped, not filtered by subject) — the scoped per-subject Worldview reads are the correct choice instead. |
| 35 | Name-Resolved Worldview Summary for Briefing | 4 — not needed | Also unscoped to one Building; same reason as #34. |
| 36 | Active Principles List | 4 — not needed | Doesn't require enumerating all organizational principles. |
| 37 | Principles by Topic Tag | 4 — not needed | No specific principle-tag lookup is required here. |
| 38 | Principles Under Review | 4 — not needed | Not relevant to this scenario. |
| 39 | Principles Relevant to Active Episode Types | **3 — unsafe** | Documented write side effect: every call runs `mark_principle_used()`, incrementing `times_used` and bumping confidence. Excluded outright, per the task brief's explicit example. |
| 40 | Open Surfaced Questions | 4 — not needed | Global, not scoped to Ethan. |
| 41 | One Question by ID | 4 — not needed | No known `question_id` is in hand for this scenario. |
| 42 | Questions by Topic Tag | 4 — not needed | Not relevant to this scenario. |
| 43 | Raw Roads for a Building (`hearth/`-side) | 4 — not needed | `get_connected_context()` already surfaces Ethan's Roads; this one-directional reader is superseded there. |
| 44 | Related Buildings with Relationship Metadata | 4 — not needed | Same reason as #43. |
| 45 | Roads Connected to a Building, Either Side (Pathway-side) | 4 — not needed | `get_connected_context()` already covers this for the scenario's purposes. |
| 46 | Historical Road Count | 4 — not needed | Historical (inactive) Roads aren't relevant to this question. |
| 47 | Specific Road Lookup | 4 — not needed | No specific (from, to, type) triple is in question. |
| 48 | Pathway User Identity Fields | 4 — not needed | Toxie's identity is already known from her authenticated session; Ethan's display name is already in `get_connected_context()`'s output. |
| 49 | Human-Readable Display Name for a User | 4 — not needed | Same reason as #48. |
| 50 | Creator/Staff Identity Flags | 4 — not needed | Staff/creator filtering isn't part of this single-Building question. |
| 51 | Per-Creator Hearth Context (Intelligence Record) | 4 — not needed | A real, well-built alternative to `get_connected_context()` (pre-categorized concerns/patterns/coach info) — considered and not selected; see judgment calls in the completion report. |
| 52 | Full Awareness Context Assembly | **3 — unsafe** | Documented write side effect: with a real connection, writes `hearth_episodes.last_briefed_at` and (via `collect_relevant_principles()`) `hearth_principles`. Excluded outright, per the task brief's explicit example. |

**Group totals**: Group 1 (usable now) = 2. Group 2 (needs wrapper) = 4. Group 3 (unsafe) = 2. Group 4 (not needed for this scenario) = 44.

---

## Task 2 — Resolving the `get_relationships_for_entity()` Naming Collision

Phase 2 found two different, non-interchangeable functions sharing one name. No code changes are made here — this section only proposes the distinct names a future tool registry should use to refer to each one unambiguously.

| Proposed name | Maps to | Why this name |
|---|---|---|
| **`get_roads_from_entity()`** | `hearth/hearth_relationships.py:336`'s `get_relationships_for_entity(memory_conn, entity_id, active_only=True)` | One-directional — only reads Roads where the given Building is `entity_id_1` ("from" this Building). Returns `list[sqlite3.Row]`. Can silently miss the reverse direction of an asymmetric-looking pair (e.g. `coached_by`), which is exactly the behavior "from" signals to a reader. |
| **`get_roads_touching_entity()`** | `pathway-portal/backend/app/hearth_reader.py:1911`'s `get_relationships_for_entity(entity_id, active_only=True)` | Both-directional — checks the Building on *either* side (`entity_id_1` or `entity_id_2`), so it never misses a direction. "Touching" signals inclusive, either-side matching. Returns `list[dict]`, already enriched with `direction` (`incoming`/`outgoing`). |

Neither of these two functions was selected for the V1 scenario tool set (Task 3) — `get_connected_context()` already surfaces Roads for the one Building this scenario cares about — but the naming fix is recorded here now so it's settled before either function is ever exposed through a registry.

---

## Task 3 — V1 Scenario Tool Set

Six tools, selected because each is justified by a concrete step in actually answering the Toxie/Ethan question — not because they seemed generally useful.

**Answering flow this set supports**: resolve "Ethan" → get his general Building context (identity, current state, recent furniture, one-hop relationships including any coach) → get his recent episode history (does the record support "not going live much lately"?) → check what Hearth has already interpreted about him (a belief about his engagement, a tracked worsening/quiet trend, an open uncertainty) → synthesize an answer grounded in all of the above.

---

### 1. Resolve Building by Name
- **Catalog entry**: "Resolve Building by Name" (`resolve_entity(memory_conn, query)`, `hearth/hearth_entity_resolution.py`)
- **Usable as-is / needs wrapper**: Needs a wrapper. It requires the caller to open, pass in, and manage a raw `memory_conn` (a live `sqlite3.Connection` object — not something a tool-calling interface working in primitive/JSON arguments can pass), and it returns an `EntityResolution` dataclass whose `entity_row` field is a raw `sqlite3.Row`, not JSON-serializable. A wrapper would need to open its own `hearth_memory.db` connection, call the function, convert the dataclass (and its embedded row) to a plain dict, close the connection, and return that — the same self-contained pattern `get_connected_context()`'s Pathway wrapper already uses.
- **Role in the scenario**: The mandatory first step — turns the free-text name "Ethan" into a specific `entity_id`, which every other selected tool below needs as an input. Its `ambiguous` status must also be handled (there could be more than one Ethan) before proceeding.

### 2. One-Hop Connected Context
- **Catalog entry**: "One-Hop Connected Context (Traversal V1)" (`get_connected_context(entity_id, max_neighbors=10)`, `hearth/hearth_traversal.py` + Pathway-side wrapper in `hearth_reader.py`)
- **Usable as-is / needs wrapper**: Usable as-is via the existing Pathway-side wrapper — it already self-manages its own connection and returns plain dicts with no adapter required.
- **Role in the scenario**: Gives Ethan's general biographical grounding in one bounded call — his Identity summary/patterns/concerns, current State, most-recent Furniture facts, and his one-hop Roads (which would surface his coach, if any — directly relevant to "should I reach out," or whether someone closer to him already would). Also returns content-volume pointers (`episode_count`, etc.), which signal whether it's worth calling tool #3 below at all.

### 3. Recent Episodes for a Building (Pathway-side)
- **Catalog entry**: "Recent Episodes for a Building (Pathway-side)" (`get_episodes_for_entity(entity_id, limit=20)`, `pathway-portal/backend/app/hearth_reader.py`)
- **Usable as-is / needs wrapper**: Usable as-is — self-manages its own connection, returns `list[dict]`, and already takes the same `entity_id` primitive that tool #1 produces and tool #2 consumes.
- **Role in the scenario**: Retrieves Ethan's recent activity/episode timeline (any status, not just open) to directly support or complicate Toxie's claim — e.g. confirming an existing `creator_quiet` episode, or showing this has happened and resolved before. This is the concrete evidence-gathering step behind "has not been going live much lately."

### 4. Active Beliefs About a Subject
- **Catalog entry**: "Active Beliefs About a Subject" (`get_active_beliefs(conn, subject_type="entity", subject_id=<id>)`, `hearth/hearth_worldview.py`)
- **Usable as-is / needs wrapper**: Needs a wrapper — same connection-management issue as tool #1, plus it returns `list[sqlite3.Row]`, not JSON-serializable dicts. A wrapper would open/close its own connection and convert each row to a dict.
- **Role in the scenario**: Checks whether Hearth already holds an `engagement_momentum` or `responsiveness` belief about Ethan — Hearth's own settled, confidence-scored interpretation of his recent activity level and how he tends to respond when reached out to. This is what lets Hearth answer "what do you think" with an actual opinion grounded in interpreted understanding, not just raw event counts.

### 5. Watched Changes
- **Catalog entry**: "Watched Changes" (`get_watched_changes(conn, subject_type="entity", subject_id=<id>)`, `hearth/hearth_worldview.py`)
- **Usable as-is / needs wrapper**: Needs a wrapper — same two issues as tool #4 (external connection, `sqlite3.Row` output).
- **Role in the scenario**: This is the single most directly relevant piece of evidence available: `hearth_soul.py`'s `_upsert_creator_quiet_watch()` is a live production path that watches exactly this pattern — per-entity quiet-duration escalation. If Hearth is already tracking a `watching` change on Ethan with a `worsening` direction, that independently corroborates Toxie's observation (or, if nothing is being tracked, that's useful too — it tells Hearth its own detection hasn't caught this yet).

### 6. Open / Living Uncertainties
- **Catalog entry**: "Open / Living Uncertainties" (`get_living_uncertainties(conn, subject_type="entity", subject_id=<id>)`, `hearth/hearth_worldview.py`)
- **Usable as-is / needs wrapper**: Needs a wrapper — same two issues as tools #4 and #5.
- **Role in the scenario**: Checks whether Hearth is already privately uncertain about something regarding Ethan (e.g. an unresolved "is Ethan disengaging?" question). If one exists, Toxie's message is directly relevant new evidence Hearth could use to answer or escalate it; if none exists, that's a useful negative signal too. This is the weakest-justified of the six — see the completion report's judgment calls — but it's kept because it's the only tool in the catalog that would tell Hearth "I've already been privately wondering about this," which materially changes how confidently Hearth can answer "what do you think."

---

## Task 4 — Scenario-Scoped Missing Tools

The Toxie/Ethan scenario **can be fully answered** with the six tools above (each wrapped as described), composed by whatever orchestrates Phase 3/4's reasoning loop. No missing tool blocks answering this question. One real, scenario-specific limitation is worth naming precisely rather than glossing over:

- **No livestream-specific activity signal.** Toxie's claim is specifically "Ethan has not been going live much" — but nothing in the tool set (or the underlying schema) tracks livestream frequency as its own signal. The closest existing proxies are all general-purpose quietness detectors: `creator_quiet` episodes, the `engagement_momentum` belief (derived from "activity-type diversity over a rolling 14-day window"), and the quiet-duration `watched_change`. All three respond to *any* drop in tracked activity, not specifically to a drop in livestreaming. This means Hearth's answer can honestly say "my own signals do/don't show a general quietness pattern for Ethan," but cannot independently confirm or refute the specific "going live" claim Toxie made — it would have to take that part of her observation at face value rather than verify it. Building a livestream-specific signal is not something to design here; it's named so Phase 3/4 doesn't accidentally assume Hearth can verify a claim it actually can't.

No other gap from Phase 2's "Gaps Noticed" section (no reflection-refs-by-Building read, no single all-rooms-plus-Worldview dossier tool, no text/semantic search, no cross-Building aggregate surface) is required for this scenario — each would only matter for a broader or differently-shaped question than the one Toxie actually asked.

---

*End of document. No code was written or modified to produce this document.*
