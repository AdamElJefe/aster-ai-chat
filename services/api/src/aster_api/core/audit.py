from __future__ import annotations

import uuid
from typing import Any

import asyncpg


async def audit(
    conn: asyncpg.Connection,
    actor: uuid.UUID | None,
    action: str,
    subject_ref: str,
    reason: str | None = None,
    attrs: dict[str, Any] | None = None,
) -> None:
    """Append an audit event. attrs must already be non-sensitive (ids, booleans, codes)."""
    await conn.execute(
        "INSERT INTO audit_events (actor_user_id, action, subject_ref, reason, attrs) VALUES ($1, $2, $3, $4, $5)",
        actor,
        action,
        subject_ref,
        reason,
        attrs or {},
    )
