# Hearth Mind Inventory — Identity & Constitution

Covers: `hearth_identity.py`, `hearth_principles.py`, `seed_hearth_identity.py`.

See also: the Worldview room's own `hearth_worldview_identity` table (documented in `HEARTH_MIND_04_WORLDVIEW.md`), which is a *different* "Identity" concept from the one in this document — this file covers Pathway-user identity resolution and Hearth's Constitution (principles); the Worldview room's Identity table holds free-text organizational facts.

---

## Pathway Identity Resolution (`hearth_identity.py`)

- **Purpose**: Translate a Pathway `users.id` into human-presentable identity fields, and provide one centralized definition of "who counts as staff" vs. "who counts as a creator" so every watcher/module in Hearth agrees.
- **What it knows**: An explicit allowlist of public-safe user columns (`_IDENTITY_COLUMNS`, :24-27) — `password_hash` is never returned; the canonical `STAFF_ROLES` set (`{"ceo","manager","coach","navigator","it","admin"}`, :115-122); the rule that creator identity (`is_pathway_creator`/`is_shop_creator`) and staff identity (`role`) are independent and can overlap (:103-110, 150-179); name-fallback order for display (`name` → `tiktok_handle` → `email` → `"User {id}"`, :87-99).
- **What it can read**: Pathway's `users` table only, via `DATABASE_URL`, restricted to the allowlisted columns. Never touches `hearth_memory.db`.
- **What it can write**: Nothing. `get_pathway_connection()` opens the connection with `mode=ro` explicitly (:45). No INSERT/UPDATE/DELETE anywhere in the file.
- **What it can propose**: Nothing.
- **What it can never do**: Open a writable connection to Pathway; return `password_hash`. Every public function is documented to never raise — it returns `None`/falls back instead of propagating exceptions.
- **Rooms touched**: None directly. Feeds identity resolution into Worldview-room rendering (via `hearth_context.py`) and into watcher logic that populates Episodes.
- **Dependencies**: None — stdlib only (`sqlite3`, `os`, `dotenv`).
- **Consumers**:
  - `hearth_experience_evaluator.py:38` — imports `get_user_identity, is_staff_user`, but **neither name is referenced anywhere else in that file** (dead import — see conflicts doc).
  - `morning_briefing.py:26,303,361` — `hearth_identity.STAFF_ROLES` used in `query_training_comment_waiting` and `query_support_request_waiting`.
  - `hearth_context.py:224,281` — local imports inside `_resolve_worldview_subject_name()` and `collect_worldview_summary()`.

**Known drift**: `STAFF_ROLES` is documented as the single centralized source of truth ("every watcher across Hearth... agrees on what counts as staff," :112-114), but two other modules define their own, different staff-role sets that do not import from `hearth_identity`: `hearth_pulse.py:33` (`_STAFF_ROLES = ("manager","coach","ceo","it","navigator")`, missing `"admin"`) and `morning_briefing.py:915` (`_STAFF_ROLES = frozenset({"admin","manager","coach"})`, missing `"navigator"` and `"it"`) — the latter inside the *same file* that elsewhere correctly imports and uses `hearth_identity.STAFF_ROLES`. Full detail in the conflicts document.

---

## Constitution / Principles Storage (`hearth_principles.py`)

- **Purpose**: Durable, human-curated "wisdom layer" — beliefs about *how* Hearth should interpret creator behavior and make judgments — kept distinct from episodic/observational memory. This module is the storage layer for what the project calls the Constitution room.
- **What it knows**: Valid status lifecycle `{"active", "superseded", "under_review"}` (:15); confidence-adjustment amounts (+0.02 on use/confirm, -0.15 with a floor of 0.1 on contradiction, :102,165); dedupe-by-exact-content on create (:64-69).
- **What it can read**: Its own table, `hearth_principles`, in `hearth_memory.db` — by id, by tag (`topic_tags LIKE`), by status, ordered by confidence/created_at.
- **What it can write**: Full CRUD on `hearth_principles`: `create_principle()`, `increase_principle_confidence()`, `mark_principle_used()`, `update_principle_status()`, `flag_principle_for_review()`, `supersede_principle()`. No DELETE anywhere — rows are only status-transitioned, never physically removed.
- **What it can propose**: Not a proposal system itself — `create_principle()` inserts directly at `status='active'`. The human-approval gate for *new* principles lives one layer up, in `hearth_sounding_board.py`'s approve/edit/reject flow, which then calls `create_principle()` directly.
- **What it can never do**: `get_principles_connection()` is read-write, unlike `hearth_identity.py`'s Pathway connection — there is no read-only guard here (there doesn't need to be; this table is Hearth's own). Principles are never merged with Pathway tables (verified — no Pathway table reference anywhere in the file), though they *are* stored in the same physical SQLite file as episodes/entities, just a different table — the separation is logical (different table, different lifecycle, human-gated writes only), not physical.
- **Rooms touched**: Constitution.
- **Dependencies**: `hearth_memory.MEMORY_DB_PATH` only.
- **Consumers**:
  - `hearth_context.py:166,181,188` — `collect_relevant_principles()` fetches by tag per episode-type and calls `mark_principle_used()` on every selected principle.
  - `hearth_soul.py:21,268` — reads principles inside `_confidence_delta()` to decide whether a worldview confidence change should use the "grounded" or "ungrounded" delta. **Never calls `create_principle` or any write function** — its own docstring states "Soul may suggest lessons; only a human promotes a lesson into `hearth_principles`," and it is architecturally locked to read-only use of this module.
  - `hearth_sounding_board.py:14,125,148` — the **only** production call site of `create_principle()`, inside the human approve/edit/reject terminal flow.

**Operational notes**:
- `increase_principle_confidence()` filters to `status='active'` rows (no-op otherwise), but `flag_principle_for_review()` and `supersede_principle()` have **no status filter at all** — either can be called on an already-`superseded` principle, silently resurrecting or re-superseding it.
- `mark_principle_used()` performs two separate UPDATE statements (two commits) for one logical action, redundant but harmless.
- The module's own smoke test (`if __name__ == "__main__":`, :219-289) writes permanent rows into the **production** `hearth_memory.db` (there is no test-DB isolation) — running `python hearth_principles.py` directly leaves `[SMOKE TEST ...]` rows with `source='smoke_test'` permanently in the table (status ends up `superseded`, so they don't surface in `list_active_principles()`, but they persist forever).

---

## Identity Seeding Script (`seed_hearth_identity.py`)

- **Purpose**: One-time bootstrap of baseline organizational context (who key people are, their roles, what Pathway Agency is, the multi-identity model) into `hearth_worldview_identity`, so this context exists even before Soul has learned anything from behavior.
- **What it knows**: A hardcoded roster of 8 named people plus 4 org-level facts and one purpose statement, all as literal prose strings in `IDENTITY_ENTRIES` (:29-97).
- **What it can read**: Only checks for the existence of the `hearth_worldview_identity` table and counts active rows before/after, for reporting.
- **What it can write**: `hearth_worldview_identity` rows only, exclusively through `hearth_worldview.upsert_identity()` — one call per `IDENTITY_ENTRIES` key. Its docstring's claim — "does not touch beliefs, relationships, uncertainties, changes, recent lessons, or any non-worldview Hearth table" — is verified true.
- **What it can propose**: Nothing — writes go straight to `active` status, no draft/proposal intermediate.
- **What it can never do**: Create duplicate active rows for the same `identity_key` (`upsert_identity()` updates the existing active row in place). Exits with `sys.exit(1)` if `hearth_worldview_identity` doesn't exist yet.
- **Rooms touched**: Worldview (Identity's storage layer physically lives inside `hearth_worldview.py`, not a dedicated identity module).
- **Dependencies**: `hearth_worldview.get_worldview_connection`, `hearth_worldview.upsert_identity`.
- **Consumers**: **None found.** No other file imports this script. It is run manually (`python seed_hearth_identity.py`), and nothing re-runs it automatically when the roster changes.

**Gaps**: The hardcoded roster is never cross-checked against the live Pathway `users` table that `hearth_identity.py` reads from — if a real person's role changes, or a new staff member is hired, this file silently goes stale with no validation or alert. Combined with having zero consumers, there is no automated mechanism ensuring this baseline identity data is present in a fresh DB or kept in sync with reality.
