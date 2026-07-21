"""
Focused REAL-Ollama evaluation for the checkin_feedback_waiting interpreter
(Build 1). Not part of the automated regression suite (test_answer_interpreter.py
mocks every provider call) — this file makes real calls to the installed
llama3.1:8b model via Ollama's JSON-schema-constrained /api/chat endpoint and
is meant to be run manually, once, after the prompt/validator have stabilized.

Does not touch any database — pure prompt/validator evaluation.

Run: venv/bin/python3 test_answer_interpreter_local_model_eval.py
"""

import time

import hearth_answer_interpreter as hai

_Q = "Is the checkin feedback waiting episode for a creator part of a larger pattern?"

# (answer, expected_label_or_None). expected_label is None for bare/short
# answers where the "correct" raw model label is intentionally undefined —
# what matters for those is that deterministic validation lands on
# insufficient_information regardless of what the model said.
#
# These test answers to the ACTUAL generated question — "is this part of a
# LARGER PATTERN" — not the nearby "is this delay normal or concerning"
# question a prior version of this dataset conflated it with. See the
# semantic-contract review notes in hearth_answer_interpreter.py.
_EVAL_CASES = [
    ("No, this is an isolated delay.", "expected_pattern"),
    ("Managers just need a little more time to finish the feedback.", "expected_pattern"),
    ("This is normal; the check-in was only submitted yesterday.", "expected_pattern"),
    ("Nothing unusual here yet.", "expected_pattern"),
    ("Managers just need more time.", "expected_pattern"),
    ("Yes, this manager is behind on several check-ins.", "meaningful_signal"),
    ("The manager has repeatedly failed to complete these.", "meaningful_signal"),
    ("This keeps happening with this manager.", "meaningful_signal"),
    ("This has happened multiple times before.", "meaningful_signal"),
    ("It depends on how many other check-ins this manager currently has outstanding.", "context_dependent"),
    ("It depends on how long it has been waiting.", "context_dependent"),
    ("A few days is fine, but more than a week needs follow-up.", "context_dependent"),
    ("It may be; I need to check their other outstanding feedback.", "insufficient_information"),
    ("I cannot tell yet whether this is happening elsewhere.", "insufficient_information"),
    ("I need to check when it was submitted.", "insufficient_information"),
    ("I can't tell without knowing how old the request is.", "insufficient_information"),
    ("Let's wait another day before deciding.", "insufficient_information"),
    ("The creator missed yesterday's battle.", "unrelated_or_unclear"),
    ("Ask Sarah about the payroll report.", "unrelated_or_unclear"),
    ("The battle schedule needs updated.", "unrelated_or_unclear"),
    ("yes", None),
    ("no", None),
    ("maybe", None),
    ("I don't know", None),
    ("not sure", None),
    ("wait", None),
    ("they need more time", "expected_pattern"),
]

_SUBSTANTIVE = {"expected_pattern", "meaningful_signal"}


def main():
    print(f"Evaluating {len(_EVAL_CASES)} cases against Ollama model={hai.HEARTH_INTERPRETER_OLLAMA_MODEL}"
          f" host={hai.HEARTH_INTERPRETER_OLLAMA_HOST}\n")

    schema_valid = 0
    raw_agree = 0
    raw_scored = 0
    accepted_after_validation = 0
    ambiguous = 0
    rejected = 0
    false_substantive_acceptance = 0
    latencies = []
    boundary_failures = []

    for answer, expected in _EVAL_CASES:
        result, failure_category, failure_detail, latency = hai._call_ollama_structured(
            "checkin_feedback_waiting", 1, _Q, answer,
        )
        latencies.append(latency)

        if result is None:
            print(f"[SCHEMA-INVALID] answer={answer!r} category={failure_category} detail={failure_detail}")
            boundary_failures.append((answer, "schema_invalid", failure_category))
            continue

        schema_valid += 1
        raw_conclusion = result["conclusion"]

        if expected is not None:
            raw_scored += 1
            if raw_conclusion == expected:
                raw_agree += 1

        validated = hai.validate_interpretation(_Q, answer, raw_conclusion, result["reason"])

        if validated.status == "succeeded":
            accepted_after_validation += 1
            final = validated.conclusion
        elif validated.status == "ambiguous":
            ambiguous += 1
            final = None
        else:
            rejected += 1
            final = None

        # False substantive acceptance: the single most important safety
        # metric — an accepted expected_pattern/meaningful_signal that does
        # not match the predetermined label (or, for bare answers, any
        # accepted substantive label at all — those must always be
        # downgraded to insufficient_information).
        if final in _SUBSTANTIVE:
            if expected is None or final != expected:
                false_substantive_acceptance += 1
                boundary_failures.append((answer, "false_substantive_acceptance", f"got {final}, expected {expected}"))

        print(
            f"answer={answer!r:65.65} raw={raw_conclusion:25} conf={result['confidence']:.2f}"
            f" -> final={final!r:25} status={validated.status:10} latency={latency:.2f}s"
        )

    n = len(_EVAL_CASES)
    print("\n" + "=" * 78)
    print(f"cases:                          {n}")
    print(f"schema-valid rate:              {schema_valid}/{n} ({100*schema_valid/n:.0f}%)")
    print(f"raw model-label agreement:      {raw_agree}/{raw_scored} ({100*raw_agree/raw_scored:.0f}%)" if raw_scored else "raw model-label agreement: n/a")
    print(f"accepted-after-validation:      {accepted_after_validation}/{n}")
    print(f"ambiguous:                      {ambiguous}/{n}")
    print(f"rejected_by_validation:         {rejected}/{n}")
    print(f"false substantive acceptance:   {false_substantive_acceptance}/{n}  <-- most important safety metric")
    print(f"avg latency:                    {sum(latencies)/len(latencies):.2f}s")
    print(f"total wall time:                {sum(latencies):.2f}s")
    if boundary_failures:
        print("\nboundary failures:")
        for answer, kind, detail in boundary_failures:
            print(f"  [{kind}] {answer!r} — {detail}")
    else:
        print("\nno boundary failures observed in this run.")


if __name__ == "__main__":
    main()
