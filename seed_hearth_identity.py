"""
Seed Hearth Identity — foundational organizational context for Hearth.

Seeds explicit identity/context entries into hearth_worldview_identity: who
key people are, what roles they hold, what Pathway Agency is, and how the
organization is structured. This is not learned behavior — it is the
baseline context Hearth should know from the beginning. Soul can refine
nuance over time via upsert_identity(), but this baseline should always be
present.

Safe to run more than once: upsert_identity() updates the existing active
row for a given identity_key in place rather than creating duplicates.

Writes only to hearth_worldview_identity, via upsert_identity(). Does not
touch beliefs, relationships, uncertainties, changes, recent lessons, or any
non-worldview Hearth table.
"""

import sys

try:
    from hearth_worldview import get_worldview_connection, upsert_identity
except ImportError as exc:
    print(f"ERROR: could not import hearth_worldview.py: {exc}")
    sys.exit(1)

SOURCE = "seed_hearth_identity"

IDENTITY_ENTRIES = {
    "person.stacy": (
        "Stacy, known in Pathway as ESCA3, is the owner and CEO of Pathway "
        "Agency. She is also a creator with both Pathway and Shop creator "
        "identities."
    ),
    "person.sarah_flynn": (
        "Sarah Flynn, known in Pathway as Scoutandblue, is a Manager "
        "responsible for creators. She is also a creator with both Pathway "
        "and Shop creator identities."
    ),
    "person.toxie": (
        "Toxie, known in Pathway as ToxieTurvy, is a Manager responsible "
        "for creators. She is also a creator with both Pathway and Shop "
        "creator identities."
    ),
    "person.rhonda_leslie": (
        "Rhonda Leslie, known in Pathway as Whippedbythebest, is a Coach "
        "who supports creators. She is also a creator with both Pathway "
        "and Shop creator identities."
    ),
    "person.brian_atchley": (
        "Brian Atchley, known in Pathway as beatchley, is IT — the "
        "technical builder of Pathway and Hearth. He is not an operational "
        "manager or coach; his activity on the hub is administrative and "
        "technical, not creator or coaching activity. He is also a creator "
        "with both Pathway and Shop creator identities."
    ),
    "person.brady_mynaugh": (
        "Brady Mynaugh, known in Pathway as magicalgetawaysbmore, is a "
        "Navigator. He is also a creator with both Pathway and Shop "
        "creator identities."
    ),
    "person.heather_velasco": (
        "Heather Velasco, known in Pathway as magicathomemama, is a "
        "Navigator. She is also a creator with both Pathway and Shop "
        "creator identities."
    ),
    "person.mrs_frankie": (
        "Mrs. Frankie, known in Pathway as frankie.wife5, is a Navigator. "
        "She is also a creator with both Pathway and Shop creator "
        "identities."
    ),
    "org.pathway_agency": (
        "Pathway Agency is a creator community and agency built around "
        "TikTok Live, TikTok Shop, creator development, coaching, and "
        "manager support."
    ),
    "org.multi_identity_model": (
        "Pathway users may hold multiple identities at once. A person can "
        "be both staff and creator — these identities overlap rather than "
        "exclude each other."
    ),
    "org.staff_roles": (
        "Staff roles (CEO, Manager, Coach, Navigator, IT) describe "
        "responsibilities within Pathway, not a person's total identity."
    ),
    "org.creator_identity": (
        "Creator identity is represented by Pathway creator and/or Shop "
        "creator flags, independent of any staff role a person may also "
        "hold."
    ),
    "hearth.purpose": (
        "Hearth exists to help managers notice concerns, remember context, "
        "understand patterns, and support creators — without replacing "
        "human judgment. Hearth should treat creator participation and "
        "staff responsibility as overlapping identities where appropriate."
    ),
}


def main():
    conn = get_worldview_connection()
    try:
        table_exists = conn.execute(
            "SELECT name FROM sqlite_master"
            " WHERE type = 'table' AND name = 'hearth_worldview_identity';"
        ).fetchone()
        if not table_exists:
            print(
                "ERROR: hearth_worldview_identity table does not exist. "
                "Run migrate_add_hearth_worldview.py first."
            )
            sys.exit(1)

        before_count = conn.execute(
            "SELECT COUNT(*) FROM hearth_worldview_identity WHERE status = 'active';"
        ).fetchone()[0]

        seeded_keys = []
        for identity_key, identity_value in IDENTITY_ENTRIES.items():
            upsert_identity(conn, identity_key, identity_value, source=SOURCE)
            seeded_keys.append(identity_key)

        after_count = conn.execute(
            "SELECT COUNT(*) FROM hearth_worldview_identity WHERE status = 'active';"
        ).fetchone()[0]

        print("Hearth identity seed complete.")
        print(f"  Active identity rows before: {before_count}")
        print(f"  Active identity rows after:  {after_count}")
        print(f"  Entries seeded: {len(seeded_keys)}")
        for key in seeded_keys:
            print(f"    - {key}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
