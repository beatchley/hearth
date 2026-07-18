#!/usr/bin/env python3
"""
Furniture Fact Extractor test suite.

Covers the build spec's required checklist:
    1.  Because-test boundaries (GOOD vs NOT-Furniture examples)
    2.  One Building per proposal (entity-change rejection)
    3.  Multiple people in one message (candidate generation)
    4.  No Furniture case
    5.  Exact evidence validation
    6.  Duplicate suppression (fingerprint step + semantic step)
    7.  Ledger replay safety
    8.  Resolver regression (hearth_ask.py still works after extraction)
    9.  Approval transaction atomicity
    10. Privacy exclusions (approved sources only, no Hearth self-ingestion)

Deterministic checks (validation, fingerprinting, ledger, candidate
generation, atomicity, resolver) run against an isolated in-memory
database and never touch the real hearth_memory.db. Because-test-boundary
and semantic-duplicate checks make real Gemini calls (skipped, not failed,
if GEMINI_API_KEY is unavailable) since they test actual model behavior
against the extraction/dedup prompts, not just the deterministic scaffolding
around them.

Run directly:
    python3 test_fact_extractor.py
"""

import os
import sqlite3
import sys

from dotenv import load_dotenv

load_dotenv()

import hearth_entity_resolution
import hearth_fact_extractor as fx
import hearth_furniture
import hearth_furniture_proposals as fp
import hearth_memory
import hearth_relationships
import migrate_add_six_room_schema

_MARKER = "fact_extractor_test"

failures = []


def check(name, cond, msg=""):
    if not cond:
        failures.append(f"FAIL [{name}]" + (f": {msg}" if msg else ""))
        print(f"  FAIL: {name}" + (f" — {msg}" if msg else ""))
    else:
        print(f"  pass: {name}")


def _fresh_conn():
    """Isolated in-memory DB with hearth_entities, hearth_entity_furniture,
    hearth_furniture_proposals, and hearth_processed_sources — mirrors the
    codebase's established in-memory smoke-test pattern (see
    test_belief_differentiation.py)."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON;")
    hearth_memory.init_tables(conn)
    hearth_relationships.init_relationship_tables(conn)
    migrate_add_six_room_schema.migrate(conn)
    import migrate_add_furniture_proposals_schema as migrate_fp
    migrate_fp.create_tables(conn)
    return conn


def _add_entity(conn, display_name, user_id, entity_type="person", aliases=None):
    from datetime import datetime, timezone
    cur = conn.execute(
        "INSERT INTO hearth_entities (user_id, display_name, entity_type, aliases, created_at)"
        " VALUES (?, ?, ?, ?, ?);",
        (user_id, display_name, entity_type, aliases, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    return cur.lastrowid


def _gemini_client():
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return None
    try:
        from google import genai
    except ImportError:
        return None
    try:
        return genai.Client(api_key=api_key)
    except Exception:
        return None


def run():
    conn = _fresh_conn()

    hansolo = _add_entity(conn, "HanSolo", user_id=101)
    billybob = _add_entity(conn, "Billybob", user_id=102)
    frogman = _add_entity(conn, "Frogman", user_id=103)

    hansolo_row = conn.execute("SELECT * FROM hearth_entities WHERE id = ?;", (hansolo,)).fetchone()
    billybob_row = conn.execute("SELECT * FROM hearth_entities WHERE id = ?;", (billybob,)).fetchone()

    # -------------------------------------------------------------------
    # 3. Candidate generation — multiple people in one message, pronouns
    #    never create candidates
    # -------------------------------------------------------------------
    print("\n=== 3. Candidate generation: multiple people, no pronouns ===")
    text = "HanSolo and Billybob both streamed today. He said it went well."
    candidates = hearth_entity_resolution.find_entity_mentions(conn, text, entity_type="person")
    names = sorted(c["display_name"] for c in candidates)
    check("both named people found", names == ["Billybob", "HanSolo"], f"got {names}")
    check("pronoun 'He' created no extra candidate", len(candidates) == 2, f"got {len(candidates)}")

    subject_candidates = fx._generate_candidates(conn, "I love woodworking.", subject_user_id=101)
    check("subject_user_id resolves to author candidate",
          any(c["id"] == hansolo for c in subject_candidates))

    short_name_text = "Always he was quiet during the Announcement."
    al_row = _add_entity(conn, "Al", user_id=999)
    al_candidates = hearth_entity_resolution.find_entity_mentions(conn, short_name_text)
    check("short name 'Al' does not false-match inside 'Always'/'Announcement'",
          all(c["id"] != al_row for c in al_candidates))
    conn.execute("DELETE FROM hearth_entities WHERE id = ?;", (al_row,))
    conn.commit()

    # -------------------------------------------------------------------
    # 2. One Building per proposal — entity-change rejection
    # -------------------------------------------------------------------
    print("\n=== 2. Strict validation: entity changes -> rejected ===")
    wrong_subject = {
        "has_furniture_candidate": True,
        "subject_name": "Billybob",  # different from the candidate we asked about
        "fact": "Enjoys woodworking.",
        "category": "interest",
        "confidence": 0.9,
        "evidence_quote": "I love woodworking",
    }
    result = fx._validate_extraction(wrong_subject, hansolo_row, "I love woodworking on weekends.")
    check("entity-changed output rejected", result is None)

    # -------------------------------------------------------------------
    # 4. No Furniture case
    # -------------------------------------------------------------------
    print("\n=== 4. No Furniture case ===")
    no_candidate = {"has_furniture_candidate": False}
    check("has_furniture_candidate=false -> None",
          fx._validate_extraction(no_candidate, hansolo_row, "some text") is None)

    relationship_like = {
        "has_furniture_candidate": True,
        "subject_name": "HanSolo",
        "fact": "Is dating Billybob.",
        "category": "relationship",  # not in the taxonomy — must be rejected, not "other"
        "confidence": 0.95,
        "evidence_quote": "HanSolo is dating Billybob",
    }
    check("relationship category rejected (never falls back to other)",
          fx._validate_extraction(relationship_like, hansolo_row, "HanSolo is dating Billybob") is None)

    # -------------------------------------------------------------------
    # 5. Exact evidence validation
    # -------------------------------------------------------------------
    print("\n=== 5. Exact evidence validation ===")
    paraphrased = {
        "has_furniture_candidate": True,
        "subject_name": "HanSolo",
        "fact": "Enjoys woodworking.",
        "category": "interest",
        "confidence": 0.9,
        "evidence_quote": "he really likes making things from wood",  # not a substring
    }
    check("paraphrased evidence rejected",
          fx._validate_extraction(paraphrased, hansolo_row, "I love woodworking on weekends.") is None)

    exact = dict(paraphrased, evidence_quote="I love woodworking")
    good = fx._validate_extraction(exact, hansolo_row, "I love woodworking on weekends.")
    check("exact substring evidence accepted", good is not None)

    low_confidence = dict(exact, confidence=0.3)
    check("below conservative-bias floor rejected",
          fx._validate_extraction(low_confidence, hansolo_row, "I love woodworking on weekends.") is None)

    out_of_range = dict(exact, confidence=1.7)
    check("confidence outside [0,1] rejected",
          fx._validate_extraction(out_of_range, hansolo_row, "I love woodworking on weekends.") is None)

    empty_fact = dict(exact, fact="   ")
    check("empty fact rejected",
          fx._validate_extraction(empty_fact, hansolo_row, "I love woodworking on weekends.") is None)

    bad_category = dict(exact, category="mood")
    check("invalid category rejected",
          fx._validate_extraction(bad_category, hansolo_row, "I love woodworking on weekends.") is None)

    # -------------------------------------------------------------------
    # 6. Duplicate suppression — fingerprint step
    # -------------------------------------------------------------------
    print("\n=== 6. Duplicate suppression: fingerprint step ===")
    fp1 = fx.fingerprint_fact("Enjoys woodworking.")
    fp2 = fx.fingerprint_fact("enjoys woodworking!!")
    check("normalization makes near-identical text fingerprint identically", fp1 == fp2)

    pid = fp.create_furniture_proposal(
        conn, entity_id=hansolo, proposed_fact="Enjoys woodworking.", fact_type="interest",
        confidence=0.9, evidence_quote="I love woodworking", source_type="community_posts",
        source_record_id="1", source_author_user_id=101, semantic_fingerprint=fp1,
        extractor_version="v1",
    )
    check("exact duplicate detected via fingerprint (pending)",
          fx._exact_duplicate_exists(conn, hansolo, fp1))
    check("different entity, same fingerprint is NOT a duplicate",
          not fx._exact_duplicate_exists(conn, billybob, fp1))

    # -------------------------------------------------------------------
    # 7. Ledger replay safety
    # -------------------------------------------------------------------
    print("\n=== 7. Ledger replay safety ===")
    check("not processed initially",
          not fp.is_source_processed(conn, "community_posts", "42", "abchash", "v1"))
    fp.mark_source_processed(conn, "community_posts", "42", "abchash", "v1")
    check("processed after marking",
          fp.is_source_processed(conn, "community_posts", "42", "abchash", "v1"))
    fp.mark_source_processed(conn, "community_posts", "42", "abchash", "v1")  # replay
    count = conn.execute(
        "SELECT COUNT(*) FROM hearth_processed_sources WHERE source_record_id = '42';"
    ).fetchone()[0]
    check("replaying mark_source_processed does not duplicate the ledger row", count == 1)
    check("a different content_hash for the same record is a new, unprocessed identity",
          not fp.is_source_processed(conn, "community_posts", "42", "differenthash", "v1"))

    # -------------------------------------------------------------------
    # 9. Approval transaction atomicity
    # -------------------------------------------------------------------
    print("\n=== 9. Approval transaction atomicity ===")
    row, error = fp.approve_furniture_proposal(conn, pid, "test_reviewer")
    check("approve succeeds", error is None, str(error))
    check("proposal marked approved", row["status"] == "approved")
    check("applied_furniture_id populated", row["applied_furniture_id"] is not None)
    furniture_row = conn.execute(
        "SELECT * FROM hearth_entity_furniture WHERE id = ?;", (row["applied_furniture_id"],)
    ).fetchone()
    check("Furniture row exists with correct entity/text/source",
          furniture_row is not None
          and furniture_row["entity_id"] == hansolo
          and furniture_row["fact_text"] == "Enjoys woodworking."
          and furniture_row["source"] == "fact_extractor:community_posts")

    row2, error2 = fp.approve_furniture_proposal(conn, pid, "test_reviewer")
    check("double-approve fails cleanly (not pending)", row2 is None and error2 is not None)
    furniture_count = conn.execute(
        "SELECT COUNT(*) FROM hearth_entity_furniture WHERE fact_text = 'Enjoys woodworking.';"
    ).fetchone()[0]
    check("double-approve did not create a second Furniture row", furniture_count == 1)

    # dismiss path, separate proposal
    fp3 = fx.fingerprint_fact("Prefers evening livestreams.")
    pid2 = fp.create_furniture_proposal(
        conn, entity_id=billybob, proposed_fact="Prefers evening livestreams.", fact_type="preference",
        confidence=0.9, evidence_quote="I stream at night", source_type="community_posts",
        source_record_id="2", source_author_user_id=102, semantic_fingerprint=fp3,
        extractor_version="v1",
    )
    drow, derror = fp.dismiss_furniture_proposal(conn, pid2, "test_reviewer")
    check("dismiss succeeds", derror is None)
    check("dismiss writes no Furniture", conn.execute(
        "SELECT COUNT(*) FROM hearth_entity_furniture WHERE fact_text = 'Prefers evening livestreams.';"
    ).fetchone()[0] == 0)
    check("dismissed fingerprint now suppresses future identical proposals",
          fp.find_dismissed_by_fingerprint(conn, billybob, fp3) is not None)

    # -------------------------------------------------------------------
    # 10. Privacy exclusions
    # -------------------------------------------------------------------
    print("\n=== 10. Privacy exclusions ===")
    source_types = {c["source_type"] for c in fx.SOURCE_CONFIGS} | {fx.CHECKIN_ANSWERS_SOURCE_TYPE}
    expected = {
        "creator_notes", "shop_creator_notes", "community_posts", "community_comments",
        "training_comments", "support_messages", "announcements", "checkin_answers",
    }
    check("exactly the approved source types are configured (no DMs, no Live Discussions)",
          source_types == expected, f"got {source_types}")
    check("_is_hearth_authored is a real, present check (currently always False, documented why)",
          fx._is_hearth_authored("community_posts", {}) is False)

    # -------------------------------------------------------------------
    # 8. Resolver regression — extraction into hearth_entity_resolution.py
    #    did not change hearth_ask.py's behavior. Run its own smoke test in
    #    a subprocess so it exercises the real module end-to-end.
    # -------------------------------------------------------------------
    print("\n=== 8. Resolver regression (hearth_ask.py) ===")
    import subprocess
    result = subprocess.run(
        [sys.executable, "hearth_ask.py"], cwd=os.path.dirname(os.path.abspath(__file__)),
        capture_output=True, text=True, timeout=60,
    )
    check("hearth_ask.py smoke test still passes after resolver extraction",
          "All hearth_ask smoke test assertions passed." in result.stdout,
          result.stdout[-500:] + result.stderr[-500:])

    # -------------------------------------------------------------------
    # 1 & 6b. Because-test boundaries + semantic duplicate suppression —
    # real Gemini calls. Skipped (not failed) if no usable client.
    # -------------------------------------------------------------------
    print("\n=== 1. Because-test boundaries (live Gemini) ===")
    client = _gemini_client()
    if client is None:
        print("  SKIPPED: no GEMINI_API_KEY / google-genai available in this environment.")
    else:
        good_text = "HanSolo mentioned he really enjoys woodworking on his days off."
        good_result = fx._evaluate_candidate(client, hansolo_row, good_text)
        check("GOOD Furniture example ('enjoys woodworking') produces a candidate",
              good_result is not None, f"got {good_result}")
        if good_result:
            check("category is a valid taxonomy value", good_result["category"] in fx.FURNITURE_CATEGORIES)

        not_furniture_text = "HanSolo seems really discouraged lately and might be burning out."
        bad_result = fx._evaluate_candidate(client, hansolo_row, not_furniture_text)
        check("NOT-Furniture example ('seems discouraged'/'might be burning out') produces no candidate",
              bad_result is None, f"got {bad_result}")

        # Personal preferences unrelated to platform/creator activity are still
        # valid Furniture under "preference" — regression case for the real
        # community post that surfaced this gap ("has_furniture_candidate: false"
        # for a stated favorite food, before the prompt clarification below).
        personal_preference_text = (
            "If occurs to me that my favorite food is Cheese Burgers and French Fries."
        )
        preference_result = fx._evaluate_candidate(client, hansolo_row, personal_preference_text)
        check("personal preference unrelated to platform activity ('favorite food') produces a candidate",
              preference_result is not None, f"got {preference_result}")
        if preference_result:
            check("personal preference candidate is categorized as 'preference'",
                  preference_result["category"] == "preference", f"got {preference_result['category']}")

        relationship_text = "HanSolo has been dating Billybob for a few months now."
        rel_result = fx._evaluate_candidate(client, hansolo_row, relationship_text)
        check("relationship-shaped fact produces no candidate (never falls back to 'other')",
              rel_result is None, f"got {rel_result}")

        print("\n=== 6b. Semantic duplicate suppression (live Gemini) ===")
        is_dup = fx._is_semantic_duplicate(
            client, "Likes woodworking as a hobby.", ["Enjoys woodworking."],
        )
        check("paraphrased-equivalent fact recognized as a semantic duplicate", is_dup is True)

        not_dup = fx._is_semantic_duplicate(
            client, "Enjoys fishing.", ["Enjoys woodworking."],
        )
        check("unrelated fact is not flagged as a duplicate", not_dup is False)

    conn.close()

    print("\n" + ("=" * 60))
    if failures:
        print(f"{len(failures)} FAILURE(S):")
        for f in failures:
            print(f"  {f}")
        sys.exit(1)
    else:
        print("All Furniture Fact Extractor test assertions passed.")


if __name__ == "__main__":
    run()
