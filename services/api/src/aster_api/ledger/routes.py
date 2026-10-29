from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Query
from pydantic import BaseModel

from aster_api.auth.deps import Ctx, User
from aster_api.ledger.service import bonus_today, usage_today

router = APIRouter(prefix="/api/v1/credits", tags=["credits"])


class BalanceResponse(BaseModel):
    unit: str
    period: str
    limit: int
    used: int
    remaining: int


class LedgerEntry(BaseModel):
    id: uuid.UUID
    unit: str
    event_type: str
    amount: int
    causation_id: uuid.UUID | None
    created_at: datetime


class LedgerPage(BaseModel):
    items: list[LedgerEntry]
    next_cursor: datetime | None


@router.get("/balance", response_model=BalanceResponse)
async def balance(user: User, ctx: Ctx) -> BalanceResponse:
    async with ctx.db.tenant(user.id) as conn:
        base = await conn.fetchval("SELECT daily_message_limit FROM plan_definitions WHERE id = $1", user.plan_id)
        limit = int(base) + await bonus_today(conn, user.id)
        used = await usage_today(conn, user.id)
    return BalanceResponse(
        unit="chat_message", period="day_utc", limit=limit, used=used, remaining=max(limit - used, 0)
    )


@router.get("/transactions", response_model=LedgerPage)
async def transactions(
    user: User, ctx: Ctx, cursor: datetime | None = None, limit: int = Query(50, ge=1, le=200)
) -> LedgerPage:
    async with ctx.db.tenant(user.id) as conn:
        rows = await conn.fetch(
            """
            SELECT id, unit, event_type, amount, causation_id, created_at FROM credit_ledger
             WHERE user_id = $1 AND ($2::timestamptz IS NULL OR created_at < $2)
             ORDER BY created_at DESC LIMIT $3
            """,
            user.id,
            cursor,
            limit + 1,
        )
    items = [LedgerEntry(**dict(r)) for r in rows[:limit]]
    return LedgerPage(items=items, next_cursor=items[-1].created_at if len(rows) > limit else None)
