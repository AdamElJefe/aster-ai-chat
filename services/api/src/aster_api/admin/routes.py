"""Minimal moderation console API. Every action is audited with actor + reason."""

from __future__ import annotations

import uuid

from fastapi import APIRouter

from aster_api.auth.deps import Admin, Ctx
from aster_api.characters.routes import _SELECT, _detail
from aster_api.characters.schemas import CharacterDetail, ModerateRequest
from aster_api.core.audit import audit
from aster_api.core.errors import not_found

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])


@router.get("/moderation/characters", response_model=list[CharacterDetail])
async def review_queue(admin: Admin, ctx: Ctx) -> list[CharacterDetail]:
    async with ctx.db.tenant(admin.id, "admin") as conn:
        rows = await conn.fetch(
            f"{_SELECT} WHERE d.moderation_state = 'pending_review' ORDER BY d.updated_at LIMIT 100"
        )
        await audit(conn, admin.id, "admin.review_queue_viewed", "characters", attrs={"count": len(rows)})
    return [_detail(r, admin.id) for r in rows]


@router.post("/moderation/characters/{character_id}", response_model=CharacterDetail)
async def moderate(character_id: uuid.UUID, body: ModerateRequest, admin: Admin, ctx: Ctx) -> CharacterDetail:
    async with ctx.db.tenant(admin.id, "admin") as conn:
        before = await conn.fetchrow(
            "SELECT visibility, moderation_state FROM character_definitions WHERE id = $1 FOR UPDATE", character_id
        )
        if before is None:
            raise not_found("character")
        if body.action == "approve":
            await conn.execute(
                "UPDATE character_definitions SET moderation_state = 'approved', visibility = $2, updated_at = now()"
                " WHERE id = $1",
                character_id,
                body.visibility,
            )
        else:
            await conn.execute(
                "UPDATE character_definitions SET moderation_state = 'rejected', visibility = 'private',"
                " updated_at = now() WHERE id = $1",
                character_id,
            )
        await audit(
            conn,
            admin.id,
            f"character.{body.action}",
            f"character:{character_id}",
            reason=body.reason,
            attrs={"before": dict(before), "visibility": body.visibility},
        )
        row = await conn.fetchrow(f"{_SELECT} WHERE d.id = $1", character_id)
    return _detail(row, admin.id)
