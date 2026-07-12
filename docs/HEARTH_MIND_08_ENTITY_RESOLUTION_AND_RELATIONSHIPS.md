# Hearth Mind Inventory — Entity Resolution & Relationships (Roads)

Covers: `hearth_entity_resolution.py`, `hearth_relationships.py`.

---

## Entity Resolution (`hearth_entity_resolution.py`)

- **Purpose**: Deterministically map free text (a name typed by a manager, or a name mentioned inside a Pathway record) to a specific `hearth_entities` row, without ever guessing between plausible candidates.
- **What it knows**: A layered matching order — exact `display_name` → case-insensitive `display_name` → comma-separated `aliases` — and the rule that 2+ matches at any layer is `ambiguous`, never a guess. A whole-word regex convention (`\b{name}\b`) to avoid short-name false positives (e.g. "Al" inside "Always").
- **What it can read**: `hearth_entities` (`id`, `display_name`, `aliases`, `user_id`, `entity_type`).
- **What it can write**: Nothing — every function is read-only.
- **What it can never do**: Fuzzy-match — "V1 does not guess" (:17-18, 54-56). Write to the DB. Resolve pronouns or perform coreference. Pick a winner among same-name collisions.
- **Rooms touched**: Identity (reads `hearth_entities`).
- **Dependencies**: None — pure stdlib.
- **Consumers**: `hearth_ask.py` (sole entity-resolution step in Ask Hearth). `hearth_fact_extractor.py` (`resolve_entity_by_user_id` for self-subject, `find_entity_mentions` for text-scan candidates).

**Dead column**: `hearth_entities.aliases` has no production write path anywhere today (confirmed — only test helpers write it), so the alias-match layer in both `resolve_entity()` and `find_entity_mentions()` is currently unreachable in production. Self-documented in the module's own docstring as intentional forward-compatibility scaffolding, not a bug.

**Documented, deliberate asymmetry in how same-name collisions are handled** — this is the answer to "what happens with two people who share a first name":
- **Ask Hearth's explicit-query path** (`resolve_entity()`): 2+ matches → `status="ambiguous"`, surfaced to the manager as a clarifying question ("I found more than one Building matching 'X': A, B. Which one did you mean?"). Defers to a human rather than guessing.
- **Fact Extractor's passive text-scan path** (`find_entity_mentions()`): the opposite, by explicit design — "Unlike `resolve_entity()`, this never treats a same-name collision as 'ambiguous' — both are returned as independent candidates; each one gets its own downstream evaluation" (:138-143). Each candidate is independently sent through the Gemini extraction prompt as if it were the sole referent, relying entirely on the extractor's own conservative confidence floor to reject facts that don't clearly single out that specific candidate — no cross-checking against context (which coach, which program) to break the tie. This is a real, if consciously chosen, architectural gap for the mention-scan path.

---

## Relationship Discovery / Roads (`hearth_relationships.py`)

- **Purpose**: Derive "roads" (edges) between Buildings purely from concrete, structural Pathway data — coach assignments, shared-coach peer groups, program-specific coaching, recruiter chains — and keep them in sync as Pathway's underlying data changes.
- **What it knows**: Idempotent upsert semantics; the rule that structural roads transition to `historical` (never deleted) when their source data disappears; the relationship-type vocabulary (`coach_of`/`coached_by`, `creator_role_peer`, `cn_coach_of`/`coached_by_cn`, `shop_coach_of`/`coached_by_shop`, `recruited_by`/`recruiter_of`); confidence conventions (1.0 for explicit FK-derived roads, 0.7 for structural/grouping inference); a peer-group size bound (2–10 members) to keep `creator_role_peer` "bounded and meaningful."
- **What it can read**: Pathway (`users`, `creator_coach_assignments`); `hearth_memory.db` (`hearth_entities` for the user_id→entity_id map, `hearth_relationships` for stale-road detection).
- **What it can write**: `hearth_relationships` only, via `upsert_relationship()` (INSERT ... ON CONFLICT DO UPDATE) and `_transition_stale_roads()` (UPDATE only — sets `status='historical'`, never deletes). **Never writes `hearth_relationship_events`**, despite that table existing specifically for this purpose (see below). Never writes Pathway, `hearth_entities`, or any other table — "Pathway is never modified" (module docstring, :8).
- **What it can propose**: Nothing — no staging/proposal table. All discovered roads are written directly as `active` structural facts.
- **What it can never do**: Modify Pathway. Invent relationships not backed by concrete Pathway columns ("Hearth relationship roads are derived, not invented," :9; "No emotional, personal, or subjective relationships are ever created," :11). Delete a road row — only ever deactivated/transitioned ("Roads are never deleted," :109).
- **Rooms touched**: Roads (write); reads Identity.
- **Consumers**: `morning_briefing.py` — the only place discovery is actually run (`init_relationship_tables()` → `discover_relationships()` → `discover_recruiter_relationships()` → `discover_program_coach_relationships()`, once per pipeline run). `hearth_context.py` — read-only, via `get_related_entities()`, to pull coach/cn_coach/shop_coach/recruiter names into briefing context.

**Two schema-shipped, code-unwired features in this table**:
- `hearth_relationship_events` — created and indexed by `migrate_add_six_room_schema.py` (columns for `relationship_id`, `event_type`, `source_table`, `source_record_id`, `observed_at`, `notes`), clearly designed as an append-only evidence/history log for Roads. **Nothing ever inserts into it.** The module that owns all relationship writes never touches it. A planned-but-unbuilt half of the Roads room: schema shipped, write logic never followed up.
- `hearth_relationships.activates_at` / `.expires_at` — added by the same migration, never read or written by any SELECT/INSERT/UPDATE outside the `ALTER TABLE` statement itself. Provisioned for time-boxed relationships, currently inert.

For the connection point some engineers may expect here (`create_entity_ref()`, added in the same commit series that touched Soul's entity-creation branches) — that function lives in `hearth_worldview.py`, not either of these files, and has no dependency on `hearth_entity_resolution.py`: the `entity_id` values it's called with come from Soul's own episode processing (already-known entity IDs), not from any free-text resolution step. See `HEARTH_MIND_02_MEMORY_CORE_AND_SOUL.md`.
