from __future__ import annotations

import base64
import hashlib
import json
import uuid
from datetime import datetime
from typing import Any

import asyncpg
from fastapi import APIRouter, Query, Request, Response

from aster_api.age.gate import Adult, access_state
from aster_api.auth.deps import Ctx, MaybeUser
from aster_api.characters.policy import screen_card
from aster_api.characters.schemas import (
    CharacterCard,
    CharacterDetail,
    CharacterPage,
    CharacterPreview,
    CharacterView,
)
from aster_api.core.audit import audit
from aster_api.core.errors import ApiError, not_found

router = APIRouter(prefix="/api/v1/characters", tags=["characters"])

_SELECT = """
    SELECT d.id, d.creator_id, d.current_version, d.visibility, d.moderation_state, d.is_curated,
           d.tags, d.created_at, d.updated_at, v.card
      FROM character_definitions d
      JOIN character_versions v ON v.character_id = d.id AND v.version = d.current_version
"""


def content_hash(card: CharacterCard) -> str:
    return hashlib.sha256(json.dumps(card.model_dump(), sort_keys=True).encode()).hexdigest()


def _preview(row: asyncpg.Record) -> CharacterPreview:
    card = row["card"]
    return CharacterPreview(
        id=row["id"],
        display_name=card["display_name"],
        tagline=card.get("tagline", ""),
        tags=list(row["tags"]),
        is_curated=row["is_curated"],
    )


def _detail(row: asyncpg.Record, viewer: uuid.UUID) -> CharacterDetail:
    return CharacterDetail(
        id=row["id"],
        version=row["current_version"],
        is_owner=row["creator_id"] == viewer,
        is_curated=row["is_curated"],
        visibility=row["visibility"],
        moderation_state=row["moderation_state"],
        tags=list(row["tags"]),
        card=CharacterCard(**row["card"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _check_policy(card: CharacterCard) -> None:
    violations = screen_card(card)
    if violations:
        raise ApiError(422, "CONTENT_POLICY", "character violates content rules: " + ", ".join(violations))


def _encode_cursor(ts: datetime, id_: uuid.UUID) -> str:
    return base64.urlsafe_b64encode(f"{ts.isoformat()}|{id_}".encode()).decode()


def _decode_cursor(cursor: str | None) -> tuple[datetime, uuid.UUID] | None:
    if not cursor:
        return None
    try:
        ts, id_ = base64.urlsafe_b64decode(cursor.encode()).decode().split("|")
        return datetime.fromisoformat(ts), uuid.UUID(id_)
    except Exception as exc:
        raise ApiError(422, "VALIDATION_ERROR", "invalid cursor") from exc


async def create_character(
    conn: asyncpg.Connection, creator_id: uuid.UUID, card: CharacterCard, *, curated: bool = False
) -> uuid.UUID:
    char_id: uuid.UUID = await conn.fetchval(
        "INSERT INTO character_definitions (creator_id, tags, is_curated) VALUES ($1, $2, $3) RETURNING id",
        creator_id,
        card.content_tags,
        curated,
    )
    await conn.execute(
        "INSERT INTO character_versions (character_id, version, display_name, declared_age, card, content_hash)"
        " VALUES ($1, 1, $2, $3, $4, $5)",
        char_id,
        card.display_name,
        card.declared_age,
        card.model_dump(),
        content_hash(card),
    )
    return char_id


@router.get("", response_model=CharacterPage)
async def list_characters(
    ctx: Ctx,
    user: MaybeUser,
    scope: str = Query("catalog", pattern="^(catalog|mine)$"),
    tag: str | None = Query(None, max_length=32),
    q: str | None = Query(None, max_length=80),
    cursor: str | None = None,
    limit: int = Query(24, ge=1, le=100),
) -> CharacterPage:
    after = _decode_cursor(cursor)
    where: list[str] = []
    args: list[Any] = []
    if scope == "mine":
        if user is None:
            raise ApiError(401, "UNAUTHENTICATED", "sign in required")
        args.append(user.id)
        where.append(f"d.creator_id = ${len(args)}")
    else:
        where.append("d.visibility = 'public' AND d.moderation_state = 'approved'")
    if tag:
        args.append(tag.lower())
        where.append(f"${len(args)} = ANY(d.tags)")
    if q:
        args.append(f"%{q}%")
        where.append(f"v.display_name ILIKE ${len(args)}")
    if after:
        args.extend(after)
        where.append(f"(d.created_at, d.id) < (${len(args) - 1}, ${len(args)})")
    args.append(limit + 1)
    sql = f"{_SELECT} WHERE {' AND '.join(where)} ORDER BY d.created_at DESC, d.id DESC LIMIT ${len(args)}"

    if user is None:
        async with ctx.db.system() as conn:
            rows = await conn.fetch(sql, *args)
    else:
        async with ctx.db.tenant(user.id) as conn:
            rows = await conn.fetch(sql, *args)
    items = [_preview(r) for r in rows[:limit]]
    nxt = _encode_cursor(rows[limit - 1]["created_at"], rows[limit - 1]["id"]) if len(rows) > limit else None
    return CharacterPage(items=items, next_cursor=nxt)


@router.post("", response_model=CharacterDetail, status_code=201)
async def create(card: CharacterCard, adult: Adult, ctx: Ctx) -> CharacterDetail:
    _check_policy(card)
    async with ctx.db.tenant(adult.id) as conn:
        char_id = await create_character(conn, adult.id, card)
        row = await conn.fetchrow(f"{_SELECT} WHERE d.id = $1", char_id)
    return _detail(row, adult.id)


@router.get("/{character_id}", response_model=CharacterView)
async def get_character(character_id: uuid.UUID, request: Request, user: MaybeUser, ctx: Ctx) -> CharacterView:
    if user is None:
        async with ctx.db.system() as conn:
            row = await conn.fetchrow(f"{_SELECT} WHERE d.id = $1", character_id)
        if row is None:
            raise not_found("character")
        return CharacterView(preview=_preview(row))
    region = ctx.region.resolve(request)
    async with ctx.db.tenant(user.id, user.role) as conn:
        row = await conn.fetchrow(f"{_SELECT} WHERE d.id = $1", character_id)
        if row is None:
            raise not_found("character")
        state = await access_state(conn, ctx.settings, user.id, region)
    view = CharacterView(preview=_preview(row))
    if state == "verified":
        view.detail = _detail(row, user.id)
    return view


@router.patch("/{character_id}", response_model=CharacterDetail)
async def update_character(character_id: uuid.UUID, card: CharacterCard, adult: Adult, ctx: Ctx) -> CharacterDetail:
    """Full-card replacement producing an immutable new version. Old versions stay intact for
    conversations pinned to them. Non-admin edits to a shared character send it back to draft."""
    _check_policy(card)
    async with ctx.db.tenant(adult.id) as conn:
        cur = await conn.fetchrow(
            "SELECT current_version, visibility, is_curated FROM character_definitions"
            " WHERE id = $1 AND creator_id = $2 FOR UPDATE",
            character_id,
            adult.id,
        )
        if cur is None:
            raise not_found("character")
        new_version = cur["current_version"] + 1
        await conn.execute(
            "INSERT INTO character_versions (character_id, version, display_name, declared_age, card, content_hash)"
            " VALUES ($1, $2, $3, $4, $5, $6)",
            character_id,
            new_version,
            card.display_name,
            card.declared_age,
            card.model_dump(),
            content_hash(card),
        )
        keep_approval = cur["is_curated"] and adult.user.role == "admin"
        await conn.execute(
            """
            UPDATE character_definitions
               SET current_version = $2, tags = $3, updated_at = now(),
                   visibility = CASE WHEN $4 THEN visibility ELSE 'private' END,
                   moderation_state = CASE WHEN $4 THEN moderation_state ELSE 'draft' END
             WHERE id = $1
            """,
            character_id,
            new_version,
            card.content_tags,
            keep_approval,
        )
        row = await conn.fetchrow(f"{_SELECT} WHERE d.id = $1", character_id)
    return _detail(row, adult.id)


@router.post("/{character_id}/publish", response_model=CharacterDetail)
async def request_publication(character_id: uuid.UUID, adult: Adult, ctx: Ctx) -> CharacterDetail:
    """Queues for review only. Nothing is exposed until a moderator approves."""
    async with ctx.db.tenant(adult.id) as conn:
        updated = await conn.fetchval(
            "UPDATE character_definitions SET moderation_state = 'pending_review', updated_at = now()"
            " WHERE id = $1 AND creator_id = $2 AND moderation_state IN ('draft', 'rejected') RETURNING id",
            character_id,
            adult.id,
        )
        if updated is None:
            raise not_found("character")
        await audit(conn, adult.id, "character.publish_requested", f"character:{character_id}")
        row = await conn.fetchrow(f"{_SELECT} WHERE d.id = $1", character_id)
    return _detail(row, adult.id)


@router.delete("/{character_id}", status_code=204)
async def delete_character(character_id: uuid.UUID, adult: Adult, ctx: Ctx) -> Response:
    async with ctx.db.tenant(adult.id) as conn:
        row = await conn.fetchrow(
            "SELECT is_curated FROM character_definitions WHERE id = $1 AND creator_id = $2",
            character_id,
            adult.id,
        )
        if row is None:
            raise not_found("character")
        if row["is_curated"]:
            raise ApiError(409, "CURATED_CHARACTER", "curated characters are retired by admins, not deleted")
        await conn.execute("DELETE FROM character_definitions WHERE id = $1", character_id)
    return Response(status_code=204)
