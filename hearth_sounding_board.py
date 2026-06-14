#!/usr/bin/env python3
"""
Hearth Sounding Board — terminal interface for reviewing open questions and distilling principles.

Usage:
    python hearth_sounding_board.py
    python hearth_sounding_board.py --limit 5
    python hearth_sounding_board.py --dry-run
"""

import argparse

import hearth_memory
import hearth_principles
import hearth_questions


HEADER = (
    "HEARTH SOUNDING BOARD\n"
    "Constitutional rule: Hearth may suggest lessons. Humans approve lessons."
)


# ---------------------------------------------------------------------------
# Input helpers
# ---------------------------------------------------------------------------

def _collect_multiline(prompt_text):
    """Collect multi-line input until two consecutive empty lines or a lone dot."""
    print(prompt_text)
    lines = []
    consecutive_empty = 0
    while True:
        try:
            line = input()
        except EOFError:
            break
        if line.strip() == ".":
            break
        if line == "":
            consecutive_empty += 1
            if consecutive_empty >= 2:
                break
            lines.append(line)
        else:
            consecutive_empty = 0
            lines.append(line)
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines)


def _prompt_choice(label, valid):
    """Prompt until the user enters one of the valid choices (case-insensitive)."""
    valid_upper = [v.upper() for v in valid]
    while True:
        raw = input(f"{label}: ").strip().upper()
        if raw in valid_upper:
            return raw
        print(f"  Enter one of: {', '.join(valid_upper)}")


# ---------------------------------------------------------------------------
# Principle proposal (local template — no external API)
# ---------------------------------------------------------------------------

def _propose_principle(question, answer_text):
    """
    Build a proposed principle from question context and answer.
    Uses the first 120 characters of the answer combined with the question's
    topic tags. Intentionally simple — Brian will edit if needed.
    """
    tags = question["topic_tags"] or ""
    excerpt = answer_text.strip()[:120].strip()
    if excerpt and excerpt[-1] not in (".", "!", "?"):
        excerpt += "."
    if tags:
        return f"{excerpt} This applies to: {tags}."
    return excerpt


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def _count_open_questions(conn):
    row = conn.execute(
        "SELECT COUNT(*) FROM hearth_questions WHERE status = 'open';"
    ).fetchone()
    return row[0]


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------

def _display_question(q, index, total, prefix=""):
    print(f"\n{prefix}--- Question {index} of {total} ---")
    print(f"{prefix}Question:     {q['question_text']}")
    if q["topic_tags"]:
        print(f"{prefix}Tags:         {q['topic_tags']}")
    if q["triggered_by"]:
        print(f"{prefix}Triggered by: {q['triggered_by']}")
    print(f"{prefix}Created:      {q['created_at'][:10]}")


# ---------------------------------------------------------------------------
# Answer flow
# ---------------------------------------------------------------------------

def _handle_answer(conn, question):
    """Run the answer → propose → approve/edit/reject flow for one question."""
    answer_text = _collect_multiline("\nYour answer (press Enter twice when done):")
    if not answer_text.strip():
        print("  No answer entered.")
        return

    proposed = _propose_principle(question, answer_text)
    print(f"\nProposed principle:\n{proposed}\n")

    while True:
        decision = _prompt_choice("[Y] Approve  [E] Edit  [R] Reject — choose", ["Y", "E", "R"])

        if decision == "Y":
            hearth_principles.create_principle(
                conn,
                content=proposed,
                topic_tags=question["topic_tags"],
                source="sounding_board",
                confidence=0.7,
            )
            hearth_questions.mark_question_answered(conn, question["question_id"])
            print("Principle saved. Question marked answered.")
            return

        if decision == "E":
            print(f"\nCurrent principle:\n{proposed}\n")
            edited = _collect_multiline(
                "Enter your edited principle (press Enter twice when done):"
            )
            if not edited.strip():
                print("  No text entered. Returning to approval prompt.")
                print(f"\nProposed principle:\n{proposed}\n")
                continue
            print(f"\nEdited principle:\n{edited}\n")
            save = _prompt_choice("Save this principle? [Y/N]", ["Y", "N"])
            if save == "Y":
                hearth_principles.create_principle(
                    conn,
                    content=edited,
                    topic_tags=question["topic_tags"],
                    source="sounding_board",
                    confidence=0.7,
                )
                hearth_questions.mark_question_answered(conn, question["question_id"])
                print(f"[SOUNDING BOARD] Original proposal: {proposed[:80]}")
                print(f"[SOUNDING BOARD] Saved as edited:   {edited[:80]}")
                print("Edited principle saved. Question marked answered.")
                return
            print(f"\nProposed principle:\n{proposed}\n")
            continue

        if decision == "R":
            keep_or_dismiss = _prompt_choice(
                "Keep question open or dismiss it? [K/D]", ["K", "D"]
            )
            if keep_or_dismiss == "D":
                hearth_questions.dismiss_question(conn, question["question_id"])
                print("Question dismissed.")
            else:
                print("Question remains open.")
            return


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run(limit, dry_run):
    conn = hearth_memory.get_memory_connection()
    try:
        print(HEADER)
        print()

        questions = hearth_questions.list_open_questions(conn, limit=limit)

        if not questions:
            print("No open questions right now. All caught up.")
            return

        total = len(questions)
        print(f"Found {total} open question(s).")

        if dry_run:
            print("\n[DRY RUN] No database writes will occur.\n")
            for i, q in enumerate(questions, 1):
                _display_question(q, i, total, prefix="[DRY RUN] ")
                print("[DRY RUN] Would prompt: [A] Answer  [D] Dismiss  [S] Skip  [Q] Quit")
            print(f"\n[DRY RUN] End of dry run. {total} question(s) shown.")
            return

        for i, question in enumerate(questions, 1):
            _display_question(question, i, total)
            print("\n[A] Answer  [D] Dismiss  [S] Skip  [Q] Quit")
            action = _prompt_choice("Your choice", ["A", "D", "S", "Q"])

            if action == "Q":
                open_count = _count_open_questions(conn)
                print(f"\nExiting. {open_count} question(s) remain open.")
                return

            if action == "D":
                hearth_questions.dismiss_question(conn, question["question_id"])
                print("Question dismissed.")

            elif action == "S":
                print("Skipping — question remains open.")

            elif action == "A":
                _handle_answer(conn, question)

        print(f"\nAll {total} questions reviewed.")

    except KeyboardInterrupt:
        print("\nExiting sounding board.")
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(
        description="Hearth Sounding Board — review open questions and distill principles."
    )
    parser.add_argument(
        "--limit", type=int, default=20,
        help="Max questions to review per session (default: 20)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Display questions without writing to the database",
    )
    args = parser.parse_args()
    run(limit=args.limit, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
