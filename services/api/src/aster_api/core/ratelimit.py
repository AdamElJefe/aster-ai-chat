from __future__ import annotations

import redis.asyncio as aioredis

from aster_api.core.errors import ApiError


async def check_rate(redis: aioredis.Redis, key: str, limit: int, window_seconds: int) -> None:
    """Fixed-window counter. Coarse but cheap; edge/WAF limits sit in front in production."""
    pipe = redis.pipeline()
    pipe.incr(key)
    pipe.expire(key, window_seconds, nx=True)
    count, _ = await pipe.execute()
    if int(count) > limit:
        raise ApiError(429, "RATE_LIMITED", "too many requests, slow down")
