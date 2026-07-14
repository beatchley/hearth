"""
Hearth Attention Frame — Phase 7a.

Introduces Hearth's first concept of conversational continuity: a
session-scoped Attention Frame that lets a follow-up question ("What
training has he watched?") be understood without the manager re-typing
the Building's name every turn.

FOUNDATIONAL PRINCIPLE (see docs/HEARTH_MIND_05... and the Phase 7a build
brief): Working Memory is attention. It is not organizational memory, not
evidence, not permanent knowledge. Everything stored here exists solely to
help Hearth maintain continuity during one active conversation. When the
conversation ends (explicit end action, idle timeout, or a fresh page
load issuing a new session id), the frame disappears. Nothing in this
module writes to hearth_memory.db or any other durable store. Hearth must
never reason "I know this because I said it earlier" from frame contents
— only "I previously retrieved evidence showing..." (the frame stores a
pointer to retrieved evidence, not a substitute for re-validating it).

Naming note: this is deliberately NOT called "Working Memory" in code —
that term is reserved for the broader architectural concept a later phase
may formalize. AttentionFrame names exactly what Phase 7a builds: the
single Building (and recent evidence/plan) Hearth is currently attending
to in one conversation.

Storage: an in-memory dict keyed by an opaque session id, guarded by a
lock (Flask/gunicorn workers may serve requests on multiple threads).
Deliberately not the Flask session cookie itself — retrieved evidence and
a retrieval plan across a few turns can comfortably exceed a cookie's
~4KB budget. Deliberately not a new DB table either: the content is
transient by design (idle-timeout-bounded, cleared on conversation end),
so a table would need its own cleanup job / TTL machinery to avoid
becoming a second, accidental permanent-memory system — exactly what this
phase is not supposed to build. A process-local dict is the simplest
mechanism that fits; see the completion report for the operational
tradeoff this implies (a frame does not survive a process restart or
follow a request to a different worker process) and why that is
acceptable for this phase's scope.

Phase 7b note: if a future phase needs short-lived staging for Fact
Extractor processing, it can key its own entries into the same
_FRAMES-style store (or reuse AttentionFrame.open_threads /a new field)
without redesign — the get/touch/clear/idle-expiry mechanics below are
generic to "small, session-scoped, TTL'd state," not specific to
conversation continuity. No extraction behavior is implemented here.
"""

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

# 30 minutes: no existing session-timeout convention exists elsewhere in
# Pathway Portal to match (confirmed during investigation — no
# PERMANENT_SESSION_LIFETIME, no Flask-Session extension, nothing), so this
# uses the phase brief's own suggested default.
IDLE_TIMEOUT_SECONDS = 30 * 60


@dataclass
class AttentionFrame:
    """Transient, session-scoped conversational context. Not evidence, not
    organizational truth — see module docstring.

    focused_entity_id / focused_entity_name: the Building the conversation
    currently centers on, if any. Updated after any turn that resolves a
    Building (a fixed-pattern entity route or a successful manager-advice
    turn) — untouched by turns that don't resolve one (needs_attention_today,
    not_found, ambiguous, unsupported), so a mid-conversation resolution
    failure never clobbers a still-relevant earlier focus.

    goal: the free-text question that most recently drove the conversation
    — a human-readable breadcrumb, not something re-parsed as fact.

    last_evidence: the manager-advice cognitive path's evidence dict
    (tool_name -> retrieved data) from the most recent turn that ran it,
    keyed by focused_entity_id's value at the time it was captured. Reused
    only to *reduce retrieval* on a later turn about the same Building
    (hearth_manager_advice.run_manager_advice_path's prior_evidence
    parameter) — reuse never substitutes for re-running deterministic
    validation or the authorization check, both of which run unconditionally
    every turn regardless of what this dict holds.

    last_plan: the most recent manager-advice retrieval plan
    (goal/known/to_verify), inspectable the same way hearth_ask.AskHearthResult
    already exposes it per-turn.

    open_threads: short human-readable notes about unresolved threads in
    the conversation (e.g. an ambiguous name the manager hasn't
    disambiguated yet). Advisory only — nothing reads these back into a
    prompt today; present so a future turn/phase has somewhere natural to
    look, without over-building unused plumbing now.
    """

    session_id: str
    focused_entity_id: Optional[int] = None
    focused_entity_name: Optional[str] = None
    goal: Optional[str] = None
    last_evidence: Optional[dict] = None
    last_plan: Optional[dict] = None
    open_threads: list = field(default_factory=list)
    turn_count: int = 0
    created_at: float = field(default_factory=time.monotonic)
    last_active_at: float = field(default_factory=time.monotonic)

    def touch(self) -> None:
        self.last_active_at = time.monotonic()


_FRAMES: dict = {}
_LOCK = threading.Lock()


def _is_expired(frame: AttentionFrame, now: float) -> bool:
    return (now - frame.last_active_at) > IDLE_TIMEOUT_SECONDS


def _purge_expired_locked(now: float) -> None:
    expired = [sid for sid, frame in _FRAMES.items() if _is_expired(frame, now)]
    for sid in expired:
        del _FRAMES[sid]
    if expired:
        logger.info("[attention_frame] purged %d idle-expired frame(s)", len(expired))


def get_frame(session_id: Optional[str]) -> Optional[AttentionFrame]:
    """Return the live frame for session_id, or None if it doesn't exist or
    has idle-timed-out. Does not create one — see get_or_create_frame().
    Never raises on a missing/None session_id; simply returns None.
    """
    if not session_id:
        return None
    now = time.monotonic()
    with _LOCK:
        _purge_expired_locked(now)
        return _FRAMES.get(session_id)


def get_or_create_frame(session_id: str) -> AttentionFrame:
    """Return the live frame for session_id, creating a fresh one if it
    doesn't exist or has idle-timed-out. Every call counts as activity —
    touches the frame's last_active_at so an actively-used conversation
    never idle-expires mid-use.
    """
    now = time.monotonic()
    with _LOCK:
        _purge_expired_locked(now)
        frame = _FRAMES.get(session_id)
        if frame is None:
            frame = AttentionFrame(session_id=session_id)
            _FRAMES[session_id] = frame
        else:
            frame.touch()
        return frame


def clear_frame(session_id: Optional[str]) -> None:
    """Explicit "end conversation" — drops the frame immediately, the
    fastest and most direct of the three lifecycle-end conditions (the
    other two, page refresh and idle timeout, are handled by issuing a new
    session id and by _purge_expired_locked respectively). Safe to call
    with a missing/None session_id (no-op).
    """
    if not session_id:
        return
    with _LOCK:
        _FRAMES.pop(session_id, None)


def frame_count() -> int:
    """Number of live (not-yet-purged) frames — exposed for tests only."""
    with _LOCK:
        _purge_expired_locked(time.monotonic())
        return len(_FRAMES)


if __name__ == "__main__":
    print("Step 1: get_or_create_frame() creates, get_frame() finds it")
    f = get_or_create_frame("smoke-session-1")
    assert f.session_id == "smoke-session-1"
    assert f.focused_entity_id is None
    assert get_frame("smoke-session-1") is f
    assert get_frame("no-such-session") is None

    print("Step 2: state round-trips through the same session id")
    f.focused_entity_id = 42
    f.focused_entity_name = "Ethan"
    again = get_frame("smoke-session-1")
    assert again.focused_entity_id == 42
    assert again.focused_entity_name == "Ethan"

    print("Step 3: clear_frame() removes it")
    clear_frame("smoke-session-1")
    assert get_frame("smoke-session-1") is None
    clear_frame("already-gone")  # no-op, must not raise

    print("Step 4: idle timeout expires a frame")
    f2 = get_or_create_frame("smoke-session-2")
    f2.last_active_at -= (IDLE_TIMEOUT_SECONDS + 1)  # simulate 30+ min idle
    assert get_frame("smoke-session-2") is None

    print("Step 5: get_or_create_frame() on an idle-expired id starts fresh")
    f3 = get_or_create_frame("smoke-session-3")
    f3.focused_entity_id = 7
    f3.last_active_at -= (IDLE_TIMEOUT_SECONDS + 1)
    f3_new = get_or_create_frame("smoke-session-3")
    assert f3_new.focused_entity_id is None  # fresh frame, old one purged

    clear_frame("smoke-session-3")
    print("\nAll hearth_attention_frame smoke test assertions passed.")
