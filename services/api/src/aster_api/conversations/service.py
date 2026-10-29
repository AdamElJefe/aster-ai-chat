"""Conversation domain logic. All functions expect a tenant-scoped connection."""

from __future__ import annotations

import base64
import hashlib
import json
import uuid
from datetime import datetime
from typing import Any

import asyncpg

from aster_api.conversations.schemas import ConversationOut, MessageOut
from aster_api.core.crypto import ContentCipher
from aster_api.core.errors import ApiError, not_found
from aster_api.generation.prompt import TEMPLATE_VERSION
from aster_api.ledger import service as ledger

CONV_SELECT = """
    SELECT c.id, c.character_id, c.character_version, c.title, c.status, c.created_at, c.last_message_at,
           v.display_name AS character_name
      FROM conversations c
      JOIN character_versions v ON v.character_id = c.character_id AND v.version = c.character_version
"""


def conv_out(row: asyncpg.Record) -> ConversationOut:
    return ConversationOut(**{k: row[k] for k in ConversationOut.model_fields})


def request_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def encode_cursor(ts: datetime, id_: uuid.UUID) -> str:
    return base64.urlsafe_b64encode(f"{ts.isoformat()}|{id_}".encode()).decode()


def decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        ts, id_ = base64.urlsafe_b64decode(cursor.encode()).decode().split("|")
        return datetime.fromisoformat(ts), uuid.UUID(id_)
    except Exception as exc:
        raise ApiError(422, "VALIDATION_ERROR", "invalid cursor") from exc


async def check_idempotency(
    conn: asyncpg.Connection, user_id: uuid.UUID, purpose: str, key: str, req_hash: str
) -> dict[str, Any] | None:
    # Concurrent requests with the same key wait here, then replay the first one's response.
    await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", f"idem:{user_id}:{purpose}:{key}")
    row = await conn.fetchrow(
        "SELECT request_hash, response FROM idempotency_keys WHERE user_id = $1 AND purpose = $2 AND key = $3",
        user_id,
        purpose,
        key,
    )
    if row is None:
        return None
    if row["request_hash"] != req_hash:
        raise ApiError(409, "IDEMPOTENCY_KEY_REUSED", "this Idempotency-Key was used with a different request")
    response: dict[str, Any] = row["response"]
    return response


async def store_idempotency(
    conn: asyncpg.Connection, user_id: uuid.UUID, purpose: str, key: str, req_hash: str, response: dict[str, Any]
) -> None:
    await conn.execute(
        "INSERT INTO idempotency_keys (user_id, purpose, key, request_hash, response) VALUES ($1, $2, $3, $4, $5)",
        user_id,
        purpose,
        key,
        req_hash,
        json.loads(json.dumps(response, default=str)),
    )


async def insert_message(
    conn: asyncpg.Connection,
    cipher: ContentCipher,
    *,
    conversation_id: uuid.UUID,
    turn_id: uuid.UUID,
    user_id: uuid.UUID,
    role: str,
    variant: int,
    state: str,
    content: str | None,
    client_message_id: uuid.UUID | None = None,
) -> uuid.UUID:
    msg_id = uuid.uuid4()
    blob, key_id = cipher.encrypt(content, msg_id) if content is not None else (None, None)
    await conn.execute(
        """
        INSERT INTO messages (id, conversation_id, turn_id, user_id, role, variant, state, content_ciphertext,
                              key_id, client_message_id, completed_at)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, CASE WHEN $7 = 'completed' THEN now() END)
        """,
        msg_id,
        conversation_id,
        turn_id,
        user_id,
        role,
        variant,
        state,
        blob,
        key_id,
        client_message_id,
    )
    return msg_id


async def create_conversation(
    conn: asyncpg.Connection,
    cipher: ContentCipher,
    user_id: uuid.UUID,
    character_id: uuid.UUID,
    character_version: int | None,
    title: str,
    style_preset: str,
    model_id: str,
    max_output_tokens: int,
) -> uuid.UUID:
    # RLS limits this to characters the user may see (own, or approved public/unlisted).
    char = await conn.fetchrow("SELECT current_version FROM character_definitions WHERE id = $1", character_id)
    if char is None:
        raise not_found("character")
    version = character_version or char["current_version"]
    ver = await conn.fetchrow(
        "SELECT card, display_name FROM character_versions WHERE character_id = $1 AND version = $2",
        character_id,
        version,
    )
    if ver is None:
        raise not_found("character version")
    config = {
        "model_id": model_id,
        "template_version": TEMPLATE_VERSION,
        "max_output_tokens": max_output_tokens,
        "style_preset": style_preset,
    }
    conv_id: uuid.UUID = await conn.fetchval(
        "INSERT INTO conversations (user_id, character_id, character_version, title, config)"
        " VALUES ($1, $2, $3, $4, $5) RETURNING id",
        user_id,
        character_id,
        version,
        title or ver["display_name"],
        config,
    )
    turn_id = await conn.fetchval(
        "INSERT INTO message_turns (conversation_id, user_id, ordinal) VALUES ($1, $2, 0) RETURNING id",
        conv_id,
        user_id,
    )
    greeting = ver["card"]["first_messages"][0]
    msg_id = await insert_message(
        conn,
        cipher,
        conversation_id=conv_id,
        turn_id=turn_id,
        user_id=user_id,
        role="assistant",
        variant=0,
        state="completed",
        content=greeting,
    )
    await conn.execute("UPDATE message_turns SET active_variant_id = $2 WHERE id = $1", turn_id, msg_id)
    return conv_id


async def lock_active_conversation(conn: asyncpg.Connection, conversation_id: uuid.UUID) -> asyncpg.Record:
    conv = await conn.fetchrow("SELECT id, status FROM conversations WHERE id = $1 FOR UPDATE", conversation_id)
    if conv is None:
        raise not_found("conversation")
    if conv["status"] != "active":
        raise ApiError(409, "CONVERSATION_ARCHIVED", "unarchive the conversation to continue it")
    busy = await conn.fetchval(
        "SELECT 1 FROM generation_jobs WHERE conversation_id = $1 AND status IN ('queued', 'running')",
        conversation_id,
    )
    if busy:
        raise ApiError(409, "GENERATION_IN_PROGRESS", "wait for the current reply to finish")
    return conv


async def enqueue_reply(
    conn: asyncpg.Connection,
    cipher: ContentCipher,
    *,
    user_id: uuid.UUID,
    plan_id: str,
    pepper: str,
    conversation_id: uuid.UUID,
    content: str,
    client_message_id: uuid.UUID | None,
) -> dict[str, Any]:
    await lock_active_conversation(conn, conversation_id)
    max_chars = await conn.fetchval("SELECT max_message_chars FROM plan_definitions WHERE id = $1", plan_id)
    if len(content) > max_chars:
        raise ApiError(422, "MESSAGE_TOO_LONG", f"messages are limited to {max_chars} characters")
    job_id = uuid.uuid4()
    await ledger.reserve(conn, user_id, plan_id, job_id, pepper)
    ordinal = await conn.fetchval(
        "SELECT coalesce(max(ordinal), -1) + 1 FROM message_turns WHERE conversation_id = $1", conversation_id
    )
    turn_id = await conn.fetchval(
        "INSERT INTO message_turns (conversation_id, user_id, ordinal) VALUES ($1, $2, $3) RETURNING id",
        conversation_id,
        user_id,
        ordinal,
    )
    user_msg = await insert_message(
        conn,
        cipher,
        conversation_id=conversation_id,
        turn_id=turn_id,
        user_id=user_id,
        role="user",
        variant=0,
        state="completed",
        content=content,
        client_message_id=client_message_id,
    )
    asst_msg = await insert_message(
        conn,
        cipher,
        conversation_id=conversation_id,
        turn_id=turn_id,
        user_id=user_id,
        role="assistant",
        variant=0,
        state="pending",
        content=None,
    )
    await conn.execute("UPDATE message_turns SET active_variant_id = $2 WHERE id = $1", turn_id, asst_msg)
    await conn.execute(
        "INSERT INTO generation_jobs (id, user_id, conversation_id, assistant_message_id, kind)"
        " VALUES ($1, $2, $3, $4, 'reply')",
        job_id,
        user_id,
        conversation_id,
        asst_msg,
    )
    await conn.execute(
        "UPDATE conversations SET last_message_at = now(), updated_at = now() WHERE id = $1", conversation_id
    )
    return {
        "request_id": job_id,
        "conversation_id": conversation_id,
        "turn_id": turn_id,
        "user_message_id": user_msg,
        "assistant_message_id": asst_msg,
        "events_url": f"/api/v1/conversations/{conversation_id}/events/{job_id}",
    }


async def enqueue_regenerate(
    conn: asyncpg.Connection, *, user_id: uuid.UUID, plan_id: str, pepper: str, conversation_id: uuid.UUID
) -> dict[str, Any]:
    """New assistant variant on the latest turn. Earlier variants stay retrievable.
    Policy (disclosed in UI): a regeneration uses one message from the daily allowance."""
    await lock_active_conversation(conn, conversation_id)
    turn = await conn.fetchrow(
        "SELECT id, ordinal FROM message_turns WHERE conversation_id = $1 ORDER BY ordinal DESC LIMIT 1",
        conversation_id,
    )
    if turn is None or turn["ordinal"] == 0:
        raise ApiError(409, "NOTHING_TO_REGENERATE", "send a message first")
    job_id = uuid.uuid4()
    await ledger.reserve(conn, user_id, plan_id, job_id, pepper)
    variant = await conn.fetchval(
        "SELECT max(variant) + 1 FROM messages WHERE turn_id = $1 AND role = 'assistant'", turn["id"]
    )
    asst_msg = uuid.uuid4()
    await conn.execute(
        "INSERT INTO messages (id, conversation_id, turn_id, user_id, role, variant, state)"
        " VALUES ($1, $2, $3, $4, 'assistant', $5, 'pending')",
        asst_msg,
        conversation_id,
        turn["id"],
        user_id,
        variant,
    )
    await conn.execute("UPDATE message_turns SET active_variant_id = $2 WHERE id = $1", turn["id"], asst_msg)
    await conn.execute(
        "INSERT INTO generation_jobs (id, user_id, conversation_id, assistant_message_id, kind)"
        " VALUES ($1, $2, $3, $4, 'regenerate')",
        job_id,
        user_id,
        conversation_id,
        asst_msg,
    )
    return {
        "request_id": job_id,
        "conversation_id": conversation_id,
        "turn_id": turn["id"],
        "user_message_id": None,
        "assistant_message_id": asst_msg,
        "events_url": f"/api/v1/conversations/{conversation_id}/events/{job_id}",
    }


async def list_messages(
    conn: asyncpg.Connection, cipher: ContentCipher, conversation_id: uuid.UUID, before: int | None, limit: int
) -> tuple[list[MessageOut], int | None]:
    """Active branch only: each turn's user message plus its active assistant variant."""
    turns = await conn.fetch(
        """
        SELECT id, ordinal, active_variant_id FROM message_turns
         WHERE conversation_id = $1 AND ($2::int IS NULL OR ordinal < $2)
         ORDER BY ordinal DESC LIMIT $3
        """,
        conversation_id,
        before,
        limit + 1,
    )
    page = list(reversed(turns[:limit]))
    if not page:
        return [], None
    rows = await conn.fetch(
        """
        SELECT m.*, t.ordinal, g.id AS request_id,
               (SELECT count(*) FROM messages x WHERE x.turn_id = m.turn_id AND x.role = m.role) AS variant_count
          FROM messages m JOIN message_turns t ON t.id = m.turn_id
          LEFT JOIN generation_jobs g ON g.assistant_message_id = m.id
         WHERE m.turn_id = ANY($1::uuid[])
           AND (m.role = 'user' OR m.id = t.active_variant_id)
         ORDER BY t.ordinal, CASE m.role WHEN 'user' THEN 0 ELSE 1 END
        """,
        [t["id"] for t in page],
    )
    out = [message_out(r, cipher) for r in rows]
    next_before = page[0]["ordinal"] if len(turns) > limit else None
    return out, next_before


def message_out(r: asyncpg.Record, cipher: ContentCipher) -> MessageOut:
    content = None
    if r["state"] == "completed" and r["content_ciphertext"] is not None:
        content = cipher.decrypt(r["content_ciphertext"], r["key_id"], r["id"])
    return MessageOut(
        id=r["id"],
        turn_id=r["turn_id"],
        ordinal=r["ordinal"],
        role=r["role"],
        variant=r["variant"],
        variant_count=r["variant_count"],
        state=r["state"],
        content=content,
        created_at=r["created_at"],
        request_id=r["request_id"],
    )
