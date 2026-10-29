"""Opaque server-side sessions. The identity source (dev login now, OIDC later) only
decides *which user*; everything after that is provider-agnostic."""

from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import asyncpg

from aster_api.core.crypto import hash_token

SESSION_COOKIE = "aster_session"


@dataclass(frozen=True)
class CurrentUser:
    id: uuid.UUID
    handle: str
    role: str
    plan_id: str
    session_id: uuid.UUID
    csrf_token: str


async def create_session(conn: asyncpg.Connection, user_id: uuid.UUID, pepper: str, ttl_hours: int) -> tuple[str, str]:
    token = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(24)
    await conn.execute(
        "INSERT INTO sessions (user_id, token_hash, csrf_token, expires_at) VALUES ($1, $2, $3, $4)",
        user_id,
        hash_token(token, pepper),
        csrf,
        datetime.now(UTC) + timedelta(hours=ttl_hours),
    )
    return token, csrf


async def resolve_session(conn: asyncpg.Connection, token: str, pepper: str) -> CurrentUser | None:
    row = await conn.fetchrow(
        """
        SELECT s.id AS session_id, s.csrf_token, u.id, u.handle, u.role, u.plan_id
          FROM sessions s JOIN users u ON u.id = s.user_id
         WHERE s.token_hash = $1 AND s.revoked_at IS NULL AND s.expires_at > now()
           AND u.status = 'active'
        """,
        hash_token(token, pepper),
    )
    if row is None:
        return None
    return CurrentUser(
        id=row["id"],
        handle=row["handle"],
        role=row["role"],
        plan_id=row["plan_id"],
        session_id=row["session_id"],
        csrf_token=row["csrf_token"],
    )


async def revoke_session(conn: asyncpg.Connection, session_id: uuid.UUID) -> None:
    await conn.execute("UPDATE sessions SET revoked_at = now() WHERE id = $1", session_id)
