"""
Shared safe-event-type allowlist — Hearth Sensory Policy enforcement point.

hearth_events (Pathway's event log — see backend/app/__init__.py's
_safe_create_hearth_events_table) is read by three independent consumers in
this package: Pulse (hearth_pulse.py), the Experience Evaluator
(hearth_experience_evaluator.py), and Soul's momentum belief
(hearth_soul.py). Per docs/HEARTH_SENSORY_POLICY.md Category B, private
conversation activity (message_sent) must never be visible to any of
them — not even at the metadata level (who/whom/when).

SAFE_HEARTH_EVENT_TYPES is an explicit allowlist, not a denylist
(e.g. NOT IN ('message_sent')), by deliberate choice: a denylist leaves
every future private event type visible by default until someone
remembers to add it to the exclusion list. An allowlist requires a
positive, reviewed decision before any new event type becomes visible to
Hearth at all — the same posture hearth_soul.py's
_MOMENTUM_ELIGIBLE_EVENT_TYPES has always taken.

Architectural note on why this lives here and not in
backend/app/investigation_catalog.py (which defines its own, identical
SAFE_HEARTH_EVENT_TYPES): this hearth/ package is deployed and run
independently of the Flask app — it connects to the Pathway DB directly
over sqlite3 via DATABASE_URL and must keep working standalone (as it
does in the ~/hearth repo, which has no backend/app at all). Some
backend/app modules (hearth_reader.py, hearth_scheduler.py, commands.py)
already reach the other direction — sys.path-inserting this hearth/
directory to import ITS modules — but nothing in hearth/ imports back
into backend/app, and adding that dependency here would break
standalone use. The two constants are therefore kept in sync by hand,
not by import. If you change one, change the other.
"""

SAFE_HEARTH_EVENT_TYPES = frozenset({
    "training_viewed",
    "user_signed_in",
    "community_message_created",
    "battle_requested",
    "event_signup_created",
    "checkin_submitted",
    "onboarding_step_completed",
})
