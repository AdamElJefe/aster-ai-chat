"""Usage ledger. M1 meters chat messages against a daily plan allowance.

Flow per generation job: reserve(-1) at send → settle(0, token counts) on success or
release(+1) on failure. The unique (causation_id, event_type) index makes each step idempotent.
"""

from __future__ import annotations

import uuid
from typing import Any

import asyncpg

from aster_api.core.crypto import subject_hash
from aster_api.core.errors import ApiError

UNIT = "chat_message"


async def usage_today(conn: asyncpg.Connection, user_id: uuid.UUID) -> int:
    used = await conn.fetchval(
        """
        SELECT coalesce(-sum(amount) FILTER (WHERE event_type IN ('reserve', 'release')), 0)
          FROM credit_ledger
         WHERE user_id = $1 AND unit = $2
           AND created_at >= date_trunc('day', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'
        """,
        user_id,
        UNIT,
    )
    return int(used)


async def bonus_today(conn: asyncpg.Connection, user_id: uuid.UUID) -> int:
    return int(
        await conn.fetchval(
            """
            SELECT coalesce(sum(amount), 0) FROM credit_ledger
             WHERE user_id = $1 AND unit = $2 AND event_type IN ('grant', 'adjustment')
               AND created_at >= date_trunc('day', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'
            """,
            user_id,
            UNIT,
        )
    )


async def reserve(
    conn: asyncpg.Connection, user_id: uuid.UUID, plan_id: str, causation_id: uuid.UUID, pepper: str
) -> None:
    # Serialize concurrent reservations for one user so two requests can't both take the last slot.
    await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", f"ledger:{user_id}")
    limit = await conn.fetchval("SELECT daily_message_limit FROM plan_definitions WHERE id = $1", plan_id)
    if await usage_today(conn, user_id) >= int(limit) + await bonus_today(conn, user_id):
        raise ApiError(402, "USAGE_LIMIT_REACHED", "daily message allowance used up")
    await _append(conn, user_id, "reserve", -1, causation_id, pepper)


async def settle(
    conn: asyncpg.Connection, user_id: uuid.UUID, causation_id: uuid.UUID, pepper: str, metadata: dict[str, Any]
) -> None:
    await _append(conn, user_id, "settle", 0, causation_id, pepper, metadata)


async def release(conn: asyncpg.Connection, user_id: uuid.UUID, causation_id: uuid.UUID, pepper: str) -> None:
    # Only release something that was reserved and not settled.
    states = {
        r["event_type"]
        for r in await conn.fetch("SELECT event_type FROM credit_ledger WHERE causation_id = $1", causation_id)
    }
    if "reserve" in states and not states & {"settle", "release"}:
        await _append(conn, user_id, "release", 1, causation_id, pepper)


async def grant(conn: asyncpg.Connection, user_id: uuid.UUID, amount: int, pepper: str, reason: str) -> None:
    await _append(conn, user_id, "adjustment", amount, None, pepper, {"reason": reason})


async def _append(
    conn: asyncpg.Connection,
    user_id: uuid.UUID,
    event_type: str,
    amount: int,
    causation_id: uuid.UUID | None,
    pepper: str,
    metadata: dict[str, Any] | None = None,
) -> None:
    await conn.execute(
        """
        INSERT INTO credit_ledger (user_id, subject_hash, unit, event_type, amount, causation_id, metadata)
        VALUES ($1, $2, $3, $4, $5, $6, $7)
        ON CONFLICT (causation_id, event_type) WHERE causation_id IS NOT NULL DO NOTHING
        """,
        user_id,
        subject_hash(user_id, pepper),
        UNIT,
        event_type,
        amount,
        causation_id,
        metadata or {},
    )
