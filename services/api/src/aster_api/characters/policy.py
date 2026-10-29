"""Deterministic engineering minimum for character cards.

This is NOT the moderation system and not a full content policy. It blocks
obvious minor/school-age framing so nothing in M1 can store such a card, and it errs toward blocking.
"""

from __future__ import annotations

import re

from aster_api.characters.schemas import CharacterCard

_MINOR_TERMS = re.compile(
    r"\b(underage|under-age|child|children|kid|kids|preteen|pre-teen|teen|teenager|teenaged|"
    r"loli|lolicon|shota|shotacon|schoolgirl|schoolboy|kogyaru|joshi ?kou?sei|"
    r"(elementary|middle|junior high|high)[ -]school(er)?|grade[ -]school(er)?)\b",
    re.IGNORECASE,
)
# "16 years old", "16-year-old", "age 16", "aged 16"
_AGE_PHRASES = re.compile(
    r"\b(?:(\d{1,2})\s*(?:-|\s)?\s*(?:years?|yrs?|y/?o)(?:\s*-?\s*old)?|(?:age|aged)\s*(\d{1,2}))\b",
    re.IGNORECASE,
)


def screen_card(card: CharacterCard) -> list[str]:
    """Return a list of violation codes; empty means allowed."""
    text = "\n".join(
        [
            card.display_name,
            card.tagline,
            card.description,
            card.personality,
            card.speech_style,
            card.backstory,
            card.scenario,
            *card.first_messages,
            *card.example_dialogue,
            *card.content_tags,
        ]
    )
    violations: list[str] = []
    if _MINOR_TERMS.search(text):
        violations.append("MINOR_OR_SCHOOL_AGE_FRAMING")
    for m in _AGE_PHRASES.finditer(text):
        age = int(m.group(1) or m.group(2))
        if age < 18:
            violations.append("STATED_AGE_UNDER_18")
            break
    return violations
