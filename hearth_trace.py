"""
Hearth Observation Trace — developer-only audit trail for issue detection.

One-line summaries are always written to stdout (visible in Render logs).
Set HEARTH_TRACE=1 to also print the full structured report at the end of a run.

This module is never passed to the LLM and never surfaces to managers.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


@dataclass
class TraceEntry:
    """One auditable event in the Hearth detection pipeline."""
    rule_name: str
    episode_type: str
    # created_episode | reused_open_episode | resolved_episode | included_in_briefing
    action_taken: str
    reason: str
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    reference_key: Optional[str] = None
    source_table: Optional[str] = None
    source_record_id: Optional[int] = None
    source_fields: Optional[dict] = None
    entity_user_id: Optional[int] = None
    entity_display_name: Optional[str] = None
    confidence: str = "high"


def _one_line(e: TraceEntry) -> str:
    parts = ["[HEARTH TRACE]", f"rule={e.rule_name}", f"episode_type={e.episode_type}"]
    if e.reference_key:
        parts.append(f"reference_key={e.reference_key}")
    if e.source_table:
        parts.append(f"source_table={e.source_table}")
    if e.source_record_id is not None:
        parts.append(f"source_record_id={e.source_record_id}")
    if e.source_fields:
        kv = ", ".join(f"{k}: {v}" for k, v in e.source_fields.items())
        parts.append(f"source_fields={{{kv}}}")
    if e.entity_display_name:
        parts.append(f"entity={e.entity_display_name}")
    elif e.entity_user_id is not None:
        parts.append(f"entity_user_id={e.entity_user_id}")
    parts.append(f"action={e.action_taken}")
    parts.append(f"reason={e.reason}")
    parts.append(f"confidence={e.confidence}")
    return " ".join(parts)


class Tracer:
    """Collects and emits trace entries for one Hearth pipeline run."""

    def __init__(self):
        self._entries: list = []

    def record(self, entry: TraceEntry) -> None:
        """Record an entry and emit a one-line log immediately."""
        try:
            self._entries.append(entry)
            print(_one_line(entry))
        except Exception:
            pass

    def print_report(self) -> None:
        """Print the full structured report. Called only when HEARTH_TRACE=1."""
        try:
            sep = "=" * 60
            if not self._entries:
                print(f"\n{sep}\n[HEARTH TRACE REPORT] No entries recorded.\n{sep}\n")
                return
            print(f"\n{sep}")
            print(f"[HEARTH TRACE REPORT] {len(self._entries)} entries")
            print(sep)
            for i, e in enumerate(self._entries, 1):
                print(f"\n  [{i}] {e.timestamp}")
                print(f"      rule          : {e.rule_name}")
                print(f"      episode_type  : {e.episode_type}")
                print(f"      action        : {e.action_taken}")
                print(f"      reason        : {e.reason}")
                print(f"      confidence    : {e.confidence}")
                if e.reference_key:
                    print(f"      reference_key : {e.reference_key}")
                if e.source_table:
                    print(f"      source_table  : {e.source_table}")
                if e.source_record_id is not None:
                    print(f"      source_id     : {e.source_record_id}")
                if e.source_fields:
                    print(f"      source_fields :")
                    for k, v in e.source_fields.items():
                        print(f"        {k}: {v}")
                if e.entity_user_id is not None:
                    print(f"      entity_user_id: {e.entity_user_id}")
                if e.entity_display_name:
                    print(f"      entity        : {e.entity_display_name}")
            print(f"\n{sep}\n")
        except Exception:
            pass

    @property
    def entries(self):
        return list(self._entries)


class _NullTracer:
    """Drop-in replacement when no tracer is wired up. All calls are no-ops."""
    def record(self, _entry):
        pass
    def print_report(self):
        pass


NULL_TRACER = _NullTracer()
