"""
Hearth Furniture Taxonomy — the single source of truth for controlled
Furniture categories.

Furniture holds durable facts about a Building that stand on their own
("the because test": if a statement only makes sense because of something
else, it belongs to Worldview, not Furniture). fact_type on
hearth_entity_furniture is free text at the schema level, but every writer
of new Furniture (the Fact Extractor, and the manual Furniture UI) should
draw from this fixed list rather than inventing categories ad hoc.

"relationship" is deliberately excluded — Roads own relationships, not
Furniture. Any writer that decides something is fundamentally a
relationship must not use "other" as a fallback; it should produce no
Furniture candidate at all.

Existing hearth_entity_furniture rows written before this taxonomy existed
(e.g. fact_type="description") are untouched — this module governs new
writes only, not a migration of historical data.
"""

FURNITURE_CATEGORIES = (
    "skill",
    "interest",
    "content",
    "preference",
    "role",
    "trait",
    "other",
)
