"""
Focused REAL-Ollama evaluation for the general-purpose manager-answer
interpreter (Build 2). Not part of the automated regression suite
(test_answer_interpreter.py mocks every provider call) — this file makes
real calls to the installed llama3.1:8b model via Ollama's JSON-schema-
constrained /api/chat endpoint and is meant to be run manually, once, after
the generic prompt/validator have changed.

Does not touch any database — pure prompt/validator evaluation. Deliberately
spans several DIFFERENT question shapes/families (including a synthetic one)
rather than one fixed question, since Build 2's prompt is family-agnostic —
there is no longer a single "the" question to evaluate against.

Run: venv/bin/python3 test_answer_interpreter_local_model_eval.py
"""

import sqlite3
import tempfile
import time

import hearth_answer_interpreter as hai
import hearth_worldview
import migrate_add_answer_interpretations as ledger_migration
import migrate_add_question_family_fields as family_migration
import migrate_add_uncertainty_answer_fields as answer_fields_migration
import migrate_add_universal_claims_schema as claims_migration

# (question_text, why_it_matters, context_text, answer_text, expected_status_or_None).
# expected_status is None where the "correct" call is a judgment call this
# eval is meant to surface, not assert against (e.g. some tentative answers).
_EVAL_CASES = [
    # checkin_feedback_waiting-shaped — substantive pattern answer
    (
        "Is the checkin feedback waiting episode for a creator part of a larger pattern?",
        None, "A creator submitted check-in responses but has not yet received feedback.",
        "Yes, this manager is behind on several check-ins.", "supported",
    ),
    # same family — isolated, non-pattern answer (still "supported": it
    # establishes the proposition asked, just in the negative)
    (
        "Is the checkin feedback waiting episode for a creator part of a larger pattern?",
        None, "A creator submitted check-in responses but has not yet received feedback.",
        "No, this is an isolated delay.", "supported",
    ),
    # missing_discord-shaped — binary question, bare "yes"
    (
        "Is Discord required for this creator's program track?",
        "Determines whether the missing-Discord episode needs follow-up.",
        "A creator has not linked a Discord account.",
        "Yes.", "supported",
    ),
    # explanatory question, bare "yes" — should NOT be treated as sufficient
    (
        "Why has this creator been stuck in onboarding for two weeks?",
        "Prolonged onboarding stalls may indicate a process gap.",
        "A new creator has made no onboarding progress in two weeks.",
        "Yes.", "insufficient_information",
    ),
    # tentative/hedged answer
    (
        "Is this manager's recent concern volume expected or unusual?",
        None, "This manager had 5 new concern episodes in one run.",
        "It might be unusual, I'd need to check their history.", "insufficient_information",
    ),
    # unrelated answer
    (
        "Is the checkin feedback waiting episode for a creator part of a larger pattern?",
        None, None,
        "The battle schedule needs updated.", "unrelated_or_unclear",
    ),
    # synthetic, never-before-seen family — proves the prompt is generic
    (
        "Is Widget Project X's rollout currently blocked?",
        "Blocked rollouts need executive attention.",
        "Widget Project X is a new initiative Hearth has never modeled before.",
        "Yes, it's blocked because the vendor hasn't delivered the API keys yet.", "supported",
    ),
]


def main():
    print(f"Evaluating {len(_EVAL_CASES)} cases against Ollama"
          f" model={hai.HEARTH_INTERPRETER_OLLAMA_MODEL} host={hai.HEARTH_INTERPRETER_OLLAMA_HOST}\n")

    schema_valid = 0
    status_agree = 0
    status_scored = 0
    contract_violations = 0
    claims_total = 0
    claims_structurally_valid = 0
    latencies = []
    boundary_failures = []

    for question_text, why_it_matters, context_text, answer_text, expected in _EVAL_CASES:
        result, failure_category, failure_detail, latency = hai._call_ollama_structured(
            question_text, why_it_matters, context_text, answer_text,
        )
        latencies.append(latency)

        if result is None:
            print(f"[SCHEMA-INVALID] answer={answer_text!r} category={failure_category} detail={failure_detail}")
            boundary_failures.append((answer_text, "schema_invalid", failure_category))
            continue

        schema_valid += 1
        raw_status = result["status"]
        if expected is not None:
            status_scored += 1
            if raw_status == expected:
                status_agree += 1
            else:
                boundary_failures.append((answer_text, "status_mismatch", f"got {raw_status}, expected {expected}"))

        det = hai.deterministic_validate(question_text, why_it_matters, context_text, answer_text,
                                          raw_status, result["claims"])
        if det["contract_violation"]:
            contract_violations += 1
            print(f"[CONTRACT VIOLATION] answer={answer_text!r} -> {det['contract_violation']}")
        for cc in det["claim_checks"]:
            claims_total += 1
            if cc["structurally_valid"]:
                claims_structurally_valid += 1
            else:
                print(f"    claim rejected: {cc['reason']}")

        print(
            f"question={question_text!r:55.55} answer={answer_text!r:45.45}"
            f" -> status={raw_status:25} claims={len(result['claims'])} latency={latency:.2f}s"
        )

    n = len(_EVAL_CASES)
    print("\n" + "=" * 78)
    print(f"cases:                          {n}")
    print(f"schema-valid rate:              {schema_valid}/{n} ({100*schema_valid/n:.0f}%)")
    if status_scored:
        print(f"status agreement (scored only): {status_agree}/{status_scored} ({100*status_agree/status_scored:.0f}%)")
    print(f"contract violations:            {contract_violations}/{n}")
    print(f"claims structurally valid:      {claims_structurally_valid}/{claims_total}" if claims_total else "claims proposed: 0")
    print(f"avg latency:                    {sum(latencies)/len(latencies):.2f}s")
    print(f"total wall time:                {sum(latencies):.2f}s")
    if boundary_failures:
        print("\nboundary failures:")
        for answer, kind, detail in boundary_failures:
            print(f"  [{kind}] {answer!r} — {detail}")
    else:
        print("\nno boundary failures observed in this run.")


# ---------------------------------------------------------------------------
# Phase 2 — full-pipeline batch evaluation against a live Ollama instance.
#
# Runs the ACTUAL process_one_uncertainty() code path (durable ledger writes,
# deterministic validation, real constrained semantic-check calls) end to
# end against a throwaway temp sqlite db — no mocking anywhere. Gemini
# fallback stays at its default (disabled) — this eval never enables a paid
# path. Reports the metrics needed to judge whether Reflection-time latency
# is acceptable for a realistic mixed batch.
# ---------------------------------------------------------------------------

# (question_family, question_version, question_text, why_it_matters, context_text, answer_text)
_PIPELINE_BATCH_CASES = [
    # checkin_feedback_waiting — supported, 1 claim (pattern confirmed)
    ("checkin_feedback_waiting", 1,
     "Is the checkin feedback waiting episode for this creator part of a larger pattern?",
     "A single delay may or may not indicate something worth acting on.",
     "A creator submitted check-in responses but has not yet received feedback from their coach.",
     "Yes, this manager is behind on several check-ins this week."),

    # checkin_feedback_waiting — supported, 1 claim (isolated, negative claim)
    ("checkin_feedback_waiting", 1,
     "Is the checkin feedback waiting episode for this creator part of a larger pattern?",
     "A single delay may or may not indicate something worth acting on.",
     "A creator submitted check-in responses but has not yet received feedback from their coach.",
     "No, this is an isolated delay — first time this has happened with this manager."),

    # missing_discord — supported, likely 2 claims (requirement + follow-up owner)
    ("missing_discord", 1,
     "Is the missing discord episode for this creator part of a larger pattern?",
     "Determines whether the missing-Discord episode needs follow-up.",
     "A creator has not linked a Discord account for their program track.",
     "Yes, Discord is required for this track, and I've asked IT to follow up with the creator directly."),

    # new_creator_stuck — supported, likely 2-3 claims (blocker + pattern + systemic status)
    ("new_creator_stuck", 1,
     "Is the new creator stuck episode for this creator part of a larger pattern?",
     "A stuck new creator may indicate a process gap worth fixing.",
     "A new creator has made no onboarding progress in two weeks.",
     "Yes, they're stuck waiting on their welcome kit, and this is the third time this month a new"
     " creator has gotten stuck at this exact step, so I think it's becoming a systemic issue."),

    # recent_concern_volume — supported, 1 claim
    ("recent_concern_volume", 1,
     "Is this creator's recent concern volume expected or unusual?",
     "Repeated same-run concerns may indicate an emerging issue.",
     "This creator had 5 new concern episodes in a single run.",
     "This is unusual — this creator doesn't typically generate this many concerns at once."),

    # recent_concern_volume — insufficient_information (tentative)
    ("recent_concern_volume", 1,
     "Is this creator's recent concern volume expected or unusual?",
     "Repeated same-run concerns may indicate an emerging issue.",
     "This creator had 4 new concern episodes in a single run.",
     "It might be a temporary spike, I'm honestly not sure yet."),

    # checkin_feedback_waiting — bare "yes" to a binary-shaped question
    ("checkin_feedback_waiting", 1,
     "Is the checkin feedback waiting episode for this creator part of a larger pattern?",
     "A single delay may or may not indicate something worth acting on.",
     "A creator submitted check-in responses but has not yet received feedback from their coach.",
     "Yes."),

    # checkin_feedback_waiting — unrelated answer
    ("checkin_feedback_waiting", 1,
     "Is the checkin feedback waiting episode for this creator part of a larger pattern?",
     "A single delay may or may not indicate something worth acting on.",
     "A creator submitted check-in responses but has not yet received feedback from their coach.",
     "The battle schedule needs updating for next week."),

    # missing_discord — bare "no"
    ("missing_discord", 1,
     "Is the missing discord episode for this creator part of a larger pattern?",
     "Determines whether the missing-Discord episode needs follow-up.",
     "A creator has not linked a Discord account for their program track.",
     "No."),

    # missing_discord — insufficient_information (needs to check)
    ("missing_discord", 1,
     "Is the missing discord episode for this creator part of a larger pattern?",
     "Determines whether the missing-Discord episode needs follow-up.",
     "A creator has not linked a Discord account for their program track.",
     "I'd have to check with the recruiter to know if Discord is actually required for this track."),

    # new_creator_stuck — supported, 1 claim (simple blocker, no pattern claim)
    ("new_creator_stuck", 1,
     "Is the new creator stuck episode for this creator part of a larger pattern?",
     "A stuck new creator may indicate a process gap worth fixing.",
     "A new creator has made no onboarding progress in ten days.",
     "No pattern here — they're just waiting on their contract to be countersigned."),

    # synthetic, never-before-seen family — proves the live pipeline is generic
    ("future_family_never_seen_before", 1,
     "Is Widget Project X's rollout currently blocked?",
     "Blocked rollouts need executive attention.",
     "Widget Project X is a new initiative Hearth has never modeled before.",
     "Yes, it's blocked because the vendor hasn't delivered the API keys yet."),
]


def _make_live_eval_db():
    conn = sqlite3.connect(tempfile.mktemp(suffix="_live_eval.db"))
    conn.row_factory = sqlite3.Row
    hearth_worldview.ensure_worldview_tables(conn)
    answer_fields_migration.migrate(conn)
    family_migration.migrate(conn)
    ledger_migration.migrate(conn)
    claims_migration.migrate(conn)
    return conn


def run_pipeline_batch_eval():
    print(f"\n\n{'#' * 78}\n# PHASE 2 — full-pipeline live batch eval ({len(_PIPELINE_BATCH_CASES)} cases)\n{'#' * 78}\n")
    print(f"Ollama model={hai.HEARTH_INTERPRETER_OLLAMA_MODEL} host={hai.HEARTH_INTERPRETER_OLLAMA_HOST}")
    print(f"Gemini fallback enabled: {hai.HEARTH_INTERPRETER_GEMINI_FALLBACK_ENABLED} (must be False — no paid path)")

    conn = _make_live_eval_db()
    for i, (family, version, q, why, ctx, ans) in enumerate(_PIPELINE_BATCH_CASES):
        uid, _ = hearth_worldview.upsert_uncertainty(
            conn, subject_type="entity_episode", subject_id=f"{family}:{900 + i}",
            uncertainty_text=ctx, why_it_matters=why, possible_question=q,
            question_family=family, question_version=version,
        )
        conn.execute(
            "UPDATE hearth_worldview_uncertainties SET status='answered', answer_text=?,"
            " answered_by='live_eval', answered_at=? WHERE id=?;",
            (ans, hai._now(), uid),
        )
    conn.commit()

    orig_semantic = hai._call_ollama_semantic_check
    semantic_stats = {"n": 0, "latencies": []}

    def _counting_semantic(*a, **k):
        start = time.monotonic()
        result = orig_semantic(*a, **k)
        semantic_stats["n"] += 1
        semantic_stats["latencies"].append(time.monotonic() - start)
        return result

    hai._call_ollama_semantic_check = _counting_semantic

    eligible = hai.get_eligible_answered_uncertainties(conn)
    print(f"eligible rows: {len(eligible)} (expected {len(_PIPELINE_BATCH_CASES)})\n")

    row_records = []
    batch_start = time.monotonic()
    try:
        for row in eligible:
            row_start = time.monotonic()
            outcome = hai.process_one_uncertainty(conn, row)
            row_elapsed = time.monotonic() - row_start
            row_records.append((row, outcome, row_elapsed))
            print(
                f"  [{row['question_family']:32.32}] answer={row['answer_text']!r:55.55}"
                f" -> {outcome.get('status'):22} universal={outcome.get('universal_status', '-'):22}"
                f" {row_elapsed:.2f}s"
            )
    finally:
        hai._call_ollama_semantic_check = orig_semantic
    batch_elapsed = time.monotonic() - batch_start

    total_primary_calls = len(row_records)
    total_semantic_calls = semantic_stats["n"]
    failures = [r for r, o, _t in row_records if o.get("status") == "failed"]
    gemini_used = [r for r, o, _t in row_records if o.get("gemini_used")]
    row_times = [t for _r, _o, t in row_records]

    accepted_claims_total = 0
    rejected_claims_total = 0
    for row, outcome, _t in row_records:
        accepted = hai.get_current_accepted(conn, row["id"])
        if accepted is None:
            continue
        for c in hai.get_claims_for_interpretation(conn, accepted["id"]):
            if c["accepted"]:
                accepted_claims_total += 1
            else:
                rejected_claims_total += 1

    print("\n" + "=" * 78)
    print("LIVE PIPELINE BATCH EVAL — RESULTS")
    print("=" * 78)
    print(f"total questions:                 {total_primary_calls}")
    print(f"total primary interpretation calls: {total_primary_calls}  (1 per question — Gemini fallback disabled)")
    print(f"total semantic-check calls:       {total_semantic_calls}")
    print(f"total elapsed (this batch):       {batch_elapsed:.2f}s")
    print(f"avg time per question:           {sum(row_times)/len(row_times):.2f}s")
    print(f"max time per question:            {max(row_times):.2f}s")
    print(f"failures/timeouts:                {len(failures)}")
    for r, o, _t in row_records:
        if o.get("status") == "failed":
            print(f"    FAILED: family={r['question_family']} category={o.get('failure_category')}")
    print(f"Gemini fallback invoked:          {len(gemini_used)} (expected 0 — disabled by default)")
    print(f"accepted claims (stored):         {accepted_claims_total}")
    print(f"rejected claims (stored):         {rejected_claims_total}")

    outcomes_by_status = {}
    for _r, o, _t in row_records:
        key = (o.get("status"), o.get("universal_status"))
        outcomes_by_status[key] = outcomes_by_status.get(key, 0) + 1
    print("\noutcome breakdown (attempt status, universal_status):")
    for key, count in sorted(outcomes_by_status.items(), key=lambda kv: -kv[1]):
        print(f"    {key}: {count}")

    projected_10q_batch = (sum(row_times) / len(row_times)) * hai.HEARTH_INTERPRETER_BATCH_SIZE
    print(f"\nprojected time for a full HEARTH_INTERPRETER_BATCH_SIZE={hai.HEARTH_INTERPRETER_BATCH_SIZE}"
          f" batch at this avg/question: ~{projected_10q_batch:.0f}s")
    print("Reflection/Morning Briefing runs as a background scan (not in the user-facing request"
          " path), so this is judged against 'does not meaningfully delay the next scheduled scan',"
          " not against a live HTTP timeout.")

    conn.close()
    return {
        "total_primary_calls": total_primary_calls,
        "total_semantic_calls": total_semantic_calls,
        "batch_elapsed": batch_elapsed,
        "avg_per_question": sum(row_times) / len(row_times),
        "max_per_question": max(row_times),
        "failures": len(failures),
        "gemini_used": len(gemini_used),
        "accepted_claims_total": accepted_claims_total,
        "rejected_claims_total": rejected_claims_total,
    }


if __name__ == "__main__":
    main()
    run_pipeline_batch_eval()
