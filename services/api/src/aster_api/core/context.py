"""Process-wide dependencies, built once at startup and stored on app.state.ctx."""

from __future__ import annotations

from dataclasses import dataclass

import redis.asyncio as aioredis

from aster_api.age.providers import AgeVerificationProvider, build_age_provider
from aster_api.age.region import RegionResolver, build_region_resolver
from aster_api.core.config import Settings
from aster_api.core.crypto import ContentCipher
from aster_api.core.db import Database
from aster_api.generation.providers import TextInferenceProvider, build_text_provider


@dataclass
class AppContext:
    settings: Settings
    db: Database
    redis: aioredis.Redis
    cipher: ContentCipher
    age_provider: AgeVerificationProvider
    region: RegionResolver
    text: TextInferenceProvider

    @classmethod
    async def create(cls, settings: Settings) -> AppContext:
        db = Database(settings.database_url)
        await db.connect()
        return cls(
            settings=settings,
            db=db,
            redis=aioredis.from_url(settings.redis_url, decode_responses=True, socket_timeout=30),
            cipher=ContentCipher(settings.data_key_ring(), settings.active_data_key_id),
            age_provider=build_age_provider(settings),
            region=build_region_resolver(settings),
            text=build_text_provider(settings),
        )

    async def close(self) -> None:
        await self.db.close()
        await self.redis.aclose()
        await self.text.aclose()
