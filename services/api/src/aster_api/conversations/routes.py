from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Header, Query, Request, Response
from fastapi.responses import StreamingResponse

from aster_api.age.gate import Adult
from aster_api.auth.deps import Ctx
from aster_api.conversations import service as svc
from aster_api.conversations.schemas import (
    ConversationExport,
    ConversationOut,
    ConversationPage,
    CreateConversationRequest,
    ExportMessage,
    GenerationAccepted,
    MessagePage,
    SendMessageRequest,
    UpdateConversationRequest,
)
from aster_api.core.context import AppContext
from aster_api.core.errors import not_found
from aster_api.core.ratelimit import check_rate
from aster_api.generation import events

router = APIRouter(prefix="/api/v1/conversations", tags=["conversations"])

IdempotencyKey = Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=128)]


@router.post("", response_model=ConversationOut, status_code=201)
async def create_conversation(body: CreateConversationRequest, adult: Adult, ctx: Ctx) -> ConversationOut:
    async with ctx.db.tenant(adult.id) as conn:
        conv_id = await svc.create_conversation(
            conn,
            ctx.cipher,
            adult.id,
            body.character_id,
            body.character_version,
            body.title,
            body.style_preset,
            ctx.settings.text_model_id,
            ctx.settings.max_output_tokens,
        )
        row = await conn.fetchrow(f"{svc.CONV_SELECT} WHERE c.id = $1", conv_id)
    return svc.conv_out(row)


@router.get("", response_model=ConversationPage)
async def list_conversations(
    adult: Adult,
    ctx: Ctx,
    cursor: str | None = None,
    limit: int = Query(20, ge=1, le=100),
    status: str = Query("active", pattern="^(active|archived)$"),
) -> ConversationPage:
    after = svc.decode_cursor(cursor) if cursor else None
    async with ctx.db.tenant(adult.id) as conn:
        rows = await conn.fetch(
            f"""{svc.CONV_SELECT}
             WHERE c.user_id = $1 AND c.status = $2
               AND ($3::timestamptz IS NULL OR (c.last_message_at, c.id) < ($3, $4::uuid))
             ORDER BY c.last_message_at DESC, c.id DESC LIMIT $5""",
            adult.id,
            status,
            after[0] if after else None,
            after[1] if after else None,
            limit + 1,
        )
    items = [svc.conv_out(r) for r in rows[:limit]]
    nxt = svc.encode_cursor(items[-1].last_message_at, items[-1].id) if len(rows) > limit else None
    return ConversationPage(items=items, next_cursor=nxt)


@router.get("/{conversation_id}", response_model=ConversationOut)
async def get_conversation(conversation_id: uuid.UUID, adult: Adult, ctx: Ctx) -> ConversationOut:
    async with ctx.db.tenant(adult.id) as conn:
        row = await conn.fetchrow(f"{svc.CONV_SELECT} WHERE c.id = $1", conversation_id)
    if row is None:
        raise not_found("conversation")
    return svc.conv_out(row)


@router.patch("/{conversation_id}", response_model=ConversationOut)
async def update_conversation(
    conversation_id: uuid.UUID, body: UpdateConversationRequest, adult: Adult, ctx: Ctx
) -> ConversationOut:
    async with ctx.db.tenant(adult.id) as conn:
        updated = await conn.fetchval(
            "UPDATE conversations SET title = coalesce($2, title), status = coalesce($3, status), updated_at = now()"
            " WHERE id = $1 RETURNING id",
            conversation_id,
            body.title,
            body.status,
        )
        if updated is None:
            raise not_found("conversation")
        row = await conn.fetchrow(f"{svc.CONV_SELECT} WHERE c.id = $1", conversation_id)
    return svc.conv_out(row)


@router.delete("/{conversation_id}", status_code=204)
async def delete_conversation(conversation_id: uuid.UUID, adult: Adult, ctx: Ctx) -> Response:
    """Hard delete: messages, turns, jobs cascade. Transient event streams are removed too."""
    async with ctx.db.tenant(adult.id) as conn:
        job_ids = [
            r["id"]
            for r in await conn.fetch("SELECT id FROM generation_jobs WHERE conversation_id = $1", conversation_id)
        ]
        deleted = await conn.fetchval("DELETE FROM conversations WHERE id = $1 RETURNING id", conversation_id)
    if deleted is None:
        raise not_found("conversation")
    if job_ids:
        await ctx.redis.delete(*[events.stream_key(j) for j in job_ids])
    return Response(status_code=204)


@router.get("/{conversation_id}/messages", response_model=MessagePage)
async def list_messages(
    conversation_id: uuid.UUID,
    adult: Adult,
    ctx: Ctx,
    before: int | None = Query(None, ge=0),
    limit: int = Query(50, ge=1, le=200),
) -> MessagePage:
    async with ctx.db.tenant(adult.id) as conn:
        if not await conn.fetchval("SELECT 1 FROM conversations WHERE id = $1", conversation_id):
            raise not_found("conversation")
        items, next_before = await svc.list_messages(conn, ctx.cipher, conversation_id, before, limit)
    return MessagePage(items=items, next_before=next_before)


async def _accept(ctx: AppContext, payload: dict[str, Any]) -> GenerationAccepted:
    await ctx.redis.lpush(events.WAKE_KEY, "1")
    await ctx.redis.ltrim(events.WAKE_KEY, 0, 99)
    return GenerationAccepted(**payload)


@router.post("/{conversation_id}/messages", response_model=GenerationAccepted, status_code=202)
async def send_message(
    conversation_id: uuid.UUID, body: SendMessageRequest, adult: Adult, ctx: Ctx, idempotency_key: IdempotencyKey
) -> GenerationAccepted:
    req_hash = svc.request_hash({"c": str(conversation_id), "content": body.content})
    async with ctx.db.tenant(adult.id) as conn:
        replay = await svc.check_idempotency(conn, adult.id, "message.send", idempotency_key, req_hash)
        if replay is not None:
            return GenerationAccepted(**replay)
    await check_rate(ctx.redis, f"rl:send:{adult.id}", ctx.settings.send_rate_per_minute, 60)
    async with ctx.db.tenant(adult.id) as conn:
        replay = await svc.check_idempotency(conn, adult.id, "message.send", idempotency_key, req_hash)
        if replay is not None:
            return GenerationAccepted(**replay)
        payload = await svc.enqueue_reply(
            conn,
            ctx.cipher,
            user_id=adult.id,
            plan_id=adult.user.plan_id,
            pepper=ctx.settings.session_pepper,
            conversation_id=conversation_id,
            content=body.content,
            client_message_id=body.client_message_id,
        )
        await svc.store_idempotency(conn, adult.id, "message.send", idempotency_key, req_hash, payload)
    return await _accept(ctx, payload)


@router.post("/{conversation_id}/regenerate", response_model=GenerationAccepted, status_code=202)
async def regenerate(
    conversation_id: uuid.UUID, adult: Adult, ctx: Ctx, idempotency_key: IdempotencyKey
) -> GenerationAccepted:
    req_hash = svc.request_hash({"c": str(conversation_id), "op": "regenerate"})
    await check_rate(ctx.redis, f"rl:send:{adult.id}", ctx.settings.send_rate_per_minute, 60)
    async with ctx.db.tenant(adult.id) as conn:
        replay = await svc.check_idempotency(conn, adult.id, "message.regenerate", idempotency_key, req_hash)
        if replay is not None:
            return GenerationAccepted(**replay)
        payload = await svc.enqueue_regenerate(
            conn,
            user_id=adult.id,
            plan_id=adult.user.plan_id,
            pepper=ctx.settings.session_pepper,
            conversation_id=conversation_id,
        )
        await svc.store_idempotency(conn, adult.id, "message.regenerate", idempotency_key, req_hash, payload)
    return await _accept(ctx, payload)


@router.get(
    "/{conversation_id}/events/{request_id}",
    response_class=StreamingResponse,
    responses={200: {"content": {"text/event-stream": {}}, "description": "SSE stream of generation events"}},
)
async def generation_events(
    conversation_id: uuid.UUID,
    request_id: uuid.UUID,
    request: Request,
    adult: Adult,
    ctx: Ctx,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> StreamingResponse:
    """Replayable SSE. Reconnect with Last-Event-ID to resume; terminal events are delivered once
    per stream position, so a reconnect never yields a second final message."""
    async with ctx.db.tenant(adult.id) as conn:
        job = await conn.fetchval(
            "SELECT 1 FROM generation_jobs WHERE id = $1 AND conversation_id = $2", request_id, conversation_id
        )
    if job is None:
        raise not_found("generation")
    start = last_event_id if last_event_id and events.valid_stream_id(last_event_id) else "0-0"
    return StreamingResponse(
        _event_stream(ctx, adult.id, conversation_id, request_id, start, request),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
    )


async def _terminal_from_db(
    ctx: AppContext, user_id: uuid.UUID, conversation_id: uuid.UUID, request_id: uuid.UUID
) -> str | None:
    """If the job finished but its stream is gone/incomplete, rebuild the terminal event from Postgres."""
    async with ctx.db.tenant(user_id) as conn:
        row = await conn.fetchrow(
            """
            SELECT j.status, j.error_code, m.id AS message_id, m.variant, m.state, m.content_ciphertext, m.key_id
              FROM generation_jobs j JOIN messages m ON m.id = j.assistant_message_id
             WHERE j.id = $1
            """,
            request_id,
        )
    if row is None or row["status"] in ("queued", "running"):
        return None
    base = {
        "request_id": str(request_id),
        "conversation_id": str(conversation_id),
        "message_id": str(row["message_id"]),
    }
    if row["status"] == "completed" and row["state"] == "completed":
        content = ctx.cipher.decrypt(row["content_ciphertext"], row["key_id"], row["message_id"])
        return events.format_sse("message.completed", {**base, "variant": row["variant"], "content": content})
    return events.format_sse("message.failed", {**base, "code": row["error_code"] or "FAILED", "retryable": False})


async def _event_stream(
    ctx: AppContext,
    user_id: uuid.UUID,
    conversation_id: uuid.UUID,
    request_id: uuid.UUID,
    start: str,
    request: Request,
) -> AsyncIterator[str]:
    last = start
    yield "retry: 2000\n\n"
    if start != "0-0" and await events.terminal_before(ctx.redis, request_id, start):
        # Client already received the terminal event (EventSource auto-reconnect): send nothing more.
        return
    while True:
        if await request.is_disconnected():
            return
        if not await ctx.redis.exists(events.stream_key(request_id)):
            terminal = await _terminal_from_db(ctx, user_id, conversation_id, request_id)
            if terminal is not None:
                yield terminal
                return
        entries = await events.read(ctx.redis, request_id, last, block_ms=2000)
        for entry_id, event, data in entries:
            last = entry_id
            yield events.format_sse(event, data, entry_id)
            if event in events.TERMINAL:
                # Trailing usage.finalized is emitted right after the terminal event; include it if present.
                tail = await events.read(ctx.redis, request_id, last, block_ms=1)
                for tid, tev, tdata in tail:
                    yield events.format_sse(tev, tdata, tid)
                return
        if not entries:
            terminal = await _terminal_from_db(ctx, user_id, conversation_id, request_id)
            if terminal is not None:
                # Completed while we were waiting and the stream lacked a terminal event (or expired).
                yield terminal
                return
            yield ": keepalive\n\n"
            await asyncio.sleep(0)


@router.get("/{conversation_id}/export", response_model=ConversationExport)
async def export_conversation(conversation_id: uuid.UUID, adult: Adult, ctx: Ctx) -> ConversationExport:
    async with ctx.db.tenant(adult.id) as conn:
        conv = await conn.fetchrow(f"{svc.CONV_SELECT} WHERE c.id = $1", conversation_id)
        if conv is None:
            raise not_found("conversation")
        card = await conn.fetchval(
            "SELECT card FROM character_versions WHERE character_id = $1 AND version = $2",
            conv["character_id"],
            conv["character_version"],
        )
        rows = await conn.fetch(
            """
            SELECT m.*, t.ordinal, (m.id = t.active_variant_id OR m.role = 'user') AS active
              FROM messages m JOIN message_turns t ON t.id = m.turn_id
             WHERE m.conversation_id = $1 ORDER BY t.ordinal, m.role DESC, m.variant
            """,
            conversation_id,
        )
    messages = [
        ExportMessage(
            ordinal=r["ordinal"],
            role=r["role"],
            variant=r["variant"],
            active=r["active"],
            state=r["state"],
            content=ctx.cipher.decrypt(r["content_ciphertext"], r["key_id"], r["id"])
            if r["content_ciphertext"]
            else None,
            created_at=r["created_at"],
        )
        for r in rows
    ]
    return ConversationExport(
        exported_at=datetime.now(UTC), conversation=svc.conv_out(conv), character_card=card, messages=messages
    )
