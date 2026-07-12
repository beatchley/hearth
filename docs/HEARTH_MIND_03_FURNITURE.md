# Hearth Mind Inventory — Furniture

Covers: `hearth_furniture.py`, `hearth_furniture_proposals.py`, `hearth_furniture_taxonomy.py`, `migrate_add_furniture_proposals_schema.py`. The producer that feeds this system (`hearth_fact_extractor.py`) is documented in `HEARTH_MIND_06_FACT_EXTRACTION.md`; this file documents the storage/taxonomy/proposal layer it writes into.

---

## Furniture Access Layer (`hearth_furniture.py`)

- **Purpose**: Single, shared write path onto `hearth_entity_furniture` so every Furniture row's provenance is traceable regardless of which caller wrote it.
- **What it knows**: The required-field contract for a Furniture row (`fact_text`, `fact_type`, `source`, `confidence` all mandatory, no silent defaults); timestamps default to `now()` if not supplied.
- **What it can read**: `hearth_entity_furniture`, via `get_active_furniture(conn, entity_id)` — rows where `status='active'`, newest first.
- **What it can write**: `hearth_entity_furniture` rows directly, via `create_furniture(...)`. Takes an existing connection and does **not** commit — the caller owns the transaction boundary (:12-15).
- **What it can never do**: Enforce `fact_type` against the controlled taxonomy — explicitly documented as the caller's job ("no layer performs another layer's job," :30-33). Commit its own transaction.
- **Rooms touched**: Furniture only.
- **Consumers**: `hearth_furniture_proposals.approve_furniture_proposal()` is the only confirmed caller of `create_furniture()`. `hearth_fact_extractor.py` only reads via `get_active_furniture()` (for duplicate suppression) — it never calls `create_furniture()` itself.
- **Documentation note**: This module's own docstring claims the manual Furniture admin UI (`hearth_reader.py`'s `add_furniture()`) also routes through this shared path — that is false. See conflicts document.

---

## Furniture Proposal Review Queue (`hearth_furniture_proposals.py`)

- **Purpose**: The human-in-the-loop gate between the Fact Extractor's inferences and actual Furniture writes — "the extractor never writes Furniture directly."
- **What it knows**: The full proposal lifecycle — required fields (`proposed_fact`, `fact_type`, `confidence`, `evidence_quote`, `source_type`, `source_record_id`, `semantic_fingerprint`, `extractor_version`); valid status transitions (`pending` → `approved`/`dismissed`, one-way, no "superseded" state for *proposals* — see conflicts document for how this differs from Furniture rows themselves); a functions-raise-vs-UI-returns-error-tuples convention matching `hearth_reader.py`'s Flask needs.
- **What it can read**: `hearth_furniture_proposals` (all columns), plus a JOIN to `hearth_entities` in `get_furniture_proposals()` for display enrichment.
- **What it can write**: `hearth_furniture_proposals` rows directly (insert, status updates). Via `approve_furniture_proposal()` it also indirectly writes `hearth_entity_furniture` — atomically, in the same transaction, closing a race-window risk that the analogous State-proposal approve path (pathway-portal side) does not close (comment at :168-172).
- **What it can propose**: This *is* the proposal mechanism. `create_furniture_proposal(...)` inserts a `pending` row and commits immediately (not part of a larger transaction, :51). `approve_furniture_proposal(conn, proposal_id, reviewed_by)` atomically writes the Furniture row and marks the proposal `approved` with `reviewed_by`/`reviewed_at`/`applied_furniture_id` in one commit. `dismiss_furniture_proposal()` marks `dismissed` and writes nothing to Furniture.
- **What it can never do**: Merge, update, or strengthen an existing proposal — no "superseded" status for proposals; a fingerprint-duplicate candidate is suppressed *before* proposal creation, never resolved after. Approve/dismiss a non-pending proposal (guarded, returns an error tuple rather than mutating).
- **Rooms touched**: Furniture (read/write); reads Identity (`hearth_entities`) for display enrichment.
- **Consumers**: `hearth_fact_extractor.py` (fingerprint lookups, `get_pending_proposals_for_entity`, `create_furniture_proposal`). `pathway-portal/backend/app/hearth_reader.py:2592-2673` wraps `get_furniture_proposals`, `approve_furniture_proposal`, `dismiss_furniture_proposal` via a documented `sys.path` exception. `pathway-portal/backend/app/routes/main.py` exposes these as the `/admin/hearth/furniture-proposals` review page + approve/dismiss POST routes. `hearth_scheduler.py` runs the producer (`hearth_fact_extractor.run_batch`) daily at 5:30 AM CT, gated on Gemini being enabled.

---

## Processed-Sources Replay-Safety Ledger (`hearth_furniture_proposals.py`)

- **Purpose**: Make daily extraction re-runs idempotent — a given source record's exact content is only ever evaluated once per extractor version, regardless of how many times the batch job runs.
- **What it knows**: Identity is the 4-tuple `(source_type, source_record_id, content_hash, extractor_version)`. A changed `content_hash` (source record edited) is a *new* identity and will be re-evaluated — "deliberate, not a bug" (:14).
- **What it can read/write**: `hearth_processed_sources`, via `is_source_processed(...)` and `mark_source_processed(...)` — the latter an `INSERT ... ON CONFLICT DO NOTHING`, commits immediately.
- **What it can never do**: Mark a record processed until all candidate Buildings for that record have been evaluated (enforced by call-order in `hearth_fact_extractor.py`, marked only after the full candidate loop completes, and only outside dry-run).
- **Rooms touched**: None of the eight named rooms — bookkeeping infrastructure.
- **Consumers**: `hearth_fact_extractor.py`.

---

## Furniture Taxonomy (`hearth_furniture_taxonomy.py`)

- **Purpose**: Single source of truth for the controlled vocabulary of Furniture `fact_type` values.
- **What it knows**: Seven allowed categories — `skill`, `interest`, `content`, `preference`, `role`, `trait`, `other` (:22-30). `"relationship"` is deliberately excluded — "Roads own relationships, not Furniture" — and a writer detecting a relationship-fact should produce *no* candidate rather than fall back to `"other"`.
- **What it can never do**: Enforce itself — `fact_type` is free text at the schema level (no CHECK constraint on `hearth_entity_furniture`), so this module only governs *new* writes by convention. It does not touch/migrate historical rows written before it existed (e.g., legacy `fact_type="description"` rows).
- **Consumers**: `hearth_fact_extractor.py` validates extracted categories against it. `hearth_reader.py:get_furniture_categories()` wraps it for the admin UI. **Not consumed** by `hearth_furniture.create_furniture()` itself or by the manual-entry admin routes — see conflicts document; this means taxonomy enforcement is currently one-sided (Fact Extractor writes only).

---

## Furniture Proposals Schema Migration (`migrate_add_furniture_proposals_schema.py`)

- **Purpose**: One-time, purely additive schema setup for `hearth_furniture_proposals` and `hearth_processed_sources` plus indexes.
- **What it can never do**: Touch `hearth_entity_furniture` or any pre-existing table.
- **Consumers**: Run standalone; not imported elsewhere.

---

## Summary of the V1 gap

The most consequential finding in this cluster: **the manual Furniture admin UI bypasses the shared write path entirely.** `hearth_reader.py`'s `add_furniture()`, `edit_furniture()`, and `retract_furniture()` each perform their own raw SQL directly against `hearth_entity_furniture` and never import `hearth_furniture.py` at all — despite that module's docstring explicitly naming them as consumers "via the sys.path exception documented there." The sys.path exception does exist in `hearth_reader.py`, but only for the newer proposal-wrapper block, never wired to the manual-entry functions. Practical consequence: manually-entered Furniture facts can carry any `fact_type` string, with no taxonomy validation — only Fact-Extractor-sourced proposals are taxonomy-checked. Both files were introduced in the same commit, so this is not drift between two separate changes — it is a documented architecture that was never actually implemented for the manual path in this V1. Full detail and recommendation in `HEARTH_MIND_99_CONFLICTS_AND_OPEN_QUESTIONS.md`.
