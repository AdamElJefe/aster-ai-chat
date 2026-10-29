"""Generation event transport over Redis Streams.

Event types: message.started, message.delta, message.completed, message.blocked,
message.failed, usage.finalized. message.started carries `attempt`; clients must clear any partial
text when they see it (a retried attempt restarts the reply).
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any

import redis.asyncio as aioredis

TERMINAL = frozenset({"message.completed", "message.failed", "message.blocked"})
WAKE_KEY = "gen:wake"
_STREAM_ID = re.compile(r"^\d+-\d+$")


def stream_key(request_id: uuid.UUID) -> str:
    return f"gen:{request_id}"


def valid_stream_id(value: str) -> bool:
    return bool(_STREAM_ID.match(value))


async def publish(
    redis: aioredis.Redis, request_id: uuid.UUID, conversation_id: uuid.UUID, event: str, data: dict[str, Any]
) -> str:
    payload = {"request_id": str(request_id), "conversation_id": str(conversation_id), **data}
    entry_id = await redis.xadd(
        stream_key(request_id), {"event": event, "data": json.dumps(payload, default=str)}, maxlen=20000
    )
    return str(entry_id)


async def finish(redis: aioredis.Redis, request_id: uuid.UUID, ttl_seconds: int) -> None:
    await redis.expire(stream_key(request_id), ttl_seconds)


async def read(
    redis: aioredis.Redis, request_id: uuid.UUID, after: str, block_ms: int
) -> list[tuple[str, str, dict[str, Any]]]:
    # decode_responses=True: keys, ids and fields are str at runtime; redis-py's stubs are looser.
    result: Any = await redis.xread({stream_key(request_id): after}, block=block_ms, count=200)
    out: list[tuple[str, str, dict[str, Any]]] = []
    for _key, entries in result or []:
        for entry_id, fields in entries:
            out.append((entry_id, fields["event"], json.loads(fields["data"])))
    return out


async def terminal_before(redis: aioredis.Redis, request_id: uuid.UUID, last_id: str) -> bool:
    """True if a terminal event exists at or before last_id (the client has already seen it)."""
    entries: Any = await redis.xrange(stream_key(request_id), min="-", max=last_id)
    return any(fields.get("event") in TERMINAL for _id, fields in entries)


def format_sse(event: str, data: dict[str, Any], event_id: str | None = None) -> str:
    head = f"id: {event_id}\n" if event_id else ""
    return f"{head}event: {event}\ndata: {json.dumps(data, default=str)}\n\n"
