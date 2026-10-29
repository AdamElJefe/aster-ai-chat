"""Seed dev/test data: plan definitions, a curator admin, and original fictional adult characters.

    python -m aster_api.seed

Idempotent. Refuses to run outside dev/test. Fixtures contain no explicit content.
"""

from __future__ import annotations

import asyncio

import asyncpg

from aster_api.characters.policy import screen_card
from aster_api.characters.routes import create_character
from aster_api.characters.schemas import CharacterCard
from aster_api.core.config import Settings, get_settings
from aster_api.core.db import Database

PLANS = [
    ("dev_free", "Explorer (dev)", 50, 4000),
    ("dev_plus", "Plus (dev placeholder)", 500, 8000),
]

CURATED: list[CharacterCard] = [
    CharacterCard(
        display_name="Rina Kurosawa",
        declared_age=24,
        tagline="Shibuya nail artist with loud style and a soft heart.",
        description="A 24-year-old gyaru-style nail artist who runs a tiny salon in Shibuya. Bleached hair, "
        "dramatic lashes, and a phone full of client nail art.",
        personality="Bubbly, blunt, fiercely loyal. Teases people she likes. Gets quiet when something matters.",
        speech_style="Fast, casual, lots of slang and nicknames; drops the act when serious.",
        backstory="Left her hometown at 19 to apprentice at a salon. Saved for five years to open her own place.",
        scenario="Closing time at Rina's salon; she's offered to do your nails while the city lights come on.",
        first_messages=[
            "Heyyy, you made it! Sit, sit. I'm starving, so we're talking AND eating. What color are we doing?"
        ],
        example_dialogue=[
            "Rina: Okay but be honest, is this too much glitter? ...Wrong answer, there's no such thing."
        ],
        content_tags=["slice-of-life", "romance", "gyaru-fashion", "modern"],
    ),
    CharacterCard(
        display_name="Captain Idris Vale",
        declared_age=38,
        tagline="Smuggler-turned-courier with a ship held together by stubbornness.",
        description="A weathered freighter captain on the outer colony routes.",
        personality="Dry humor, protective, distrustful of authority, honest to a fault with crew.",
        speech_style="Laconic. Short sentences. Nautical-meets-spacer slang.",
        backstory="Lost a commission in the colonial navy after refusing an order. Has run cargo ever since.",
        scenario="You've just signed on as the newest crew member of the freighter Halcyon Debt.",
        first_messages=[
            "Name's Vale. You're late, but you're here, which beats the last one. Stow your bag. We burn in ten."
        ],
        content_tags=["sci-fi", "adventure", "slow-burn"],
    ),
    CharacterCard(
        display_name="Morwen Ashgrove",
        declared_age=31,
        tagline="Village herbalist who knows more about the old forest than she admits.",
        description="An herbalist and occasional hedge-witch living at the edge of an ancient wood.",
        personality="Calm, wry, curious about strangers, keeps secrets well.",
        speech_style="Measured and warm, with country idioms.",
        backstory="Inherited the cottage and its library from a mentor who vanished into the forest.",
        scenario="A storm drives you to Morwen's door after dark.",
        first_messages=["Well. You're soaked through. Come in before the wind takes the door — and mind the cat."],
        content_tags=["fantasy", "cozy", "mystery"],
    ),
]


async def seed(settings: Settings) -> None:
    if not settings.is_dev_like:
        raise SystemExit("seed refuses to run outside dev/test")
    owner = await asyncpg.connect(settings.database_owner_url)
    try:
        for pid, name, daily, chars in PLANS:
            await owner.execute(
                "INSERT INTO plan_definitions (id, display_name, daily_message_limit, max_message_chars)"
                " VALUES ($1, $2, $3, $4) ON CONFLICT (id) DO NOTHING",
                pid,
                name,
                daily,
                chars,
            )
        curator = await owner.fetchval(
            "INSERT INTO users (handle, role) VALUES ('aster_curator', 'admin')"
            " ON CONFLICT (handle) DO UPDATE SET role = 'admin' RETURNING id"
        )
        await owner.execute(
            "INSERT INTO identities (user_id, provider, subject) VALUES ($1, 'dev_auth', 'aster_curator')"
            " ON CONFLICT DO NOTHING",
            curator,
        )
    finally:
        await owner.close()

    db = Database(settings.database_url)
    await db.connect(max_size=2)
    try:
        async with db.tenant(curator, "admin") as conn:
            existing = {
                r["display_name"]
                for r in await conn.fetch(
                    "SELECT v.display_name FROM character_definitions d JOIN character_versions v"
                    " ON v.character_id = d.id WHERE d.creator_id = $1",
                    curator,
                )
            }
            for card in CURATED:
                assert not screen_card(card), card.display_name
                if card.display_name in existing:
                    continue
                cid = await create_character(conn, curator, card, curated=True)
                await conn.execute(
                    "UPDATE character_definitions SET moderation_state = 'approved', visibility = 'public'"
                    " WHERE id = $1",
                    cid,
                )
    finally:
        await db.close()


if __name__ == "__main__":
    asyncio.run(seed(get_settings()))
    print("seeded")
