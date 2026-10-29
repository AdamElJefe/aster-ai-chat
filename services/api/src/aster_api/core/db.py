"""Postgres access. Every tenant query runs inside `Database.tenant(user_id)`, which opens a
transaction and sets app.user_id with SET LOCAL so RLS policies apply."""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import asyncpg


async def _init_connection(conn: asyncpg.Connection) -> None:
    for typ in ("json", "jsonb"):
        await conn.set_type_codec(typ, encoder=json.dumps, decoder=json.loads, schema="pg_catalog")


class Database:
    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._pool: asyncpg.Pool | None = None

    async def connect(self, min_size: int = 1, max_size: int = 10) -> None:
        self._pool = await asyncpg.create_pool(self._dsn, min_size=min_size, max_size=max_size, init=_init_connection)

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    @property
    def pool(self) -> asyncpg.Pool:
        if self._pool is None:
            raise RuntimeError("database not connected")
        return self._pool

    @asynccontextmanager
    async def tenant(self, user_id: uuid.UUID, role: str = "user") -> AsyncIterator[asyncpg.Connection]:
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "SELECT set_config('app.user_id', $1, true), set_config('app.role', $2, true)",
                str(user_id),
                role,
            )
            yield conn

    @asynccontextmanager
    async def system(self) -> AsyncIterator[asyncpg.Connection]:
        """Transaction with no tenant context: RLS tables return nothing. Used for auth tables."""
        async with self.pool.acquire() as conn, conn.transaction():
            yield conn

    async def ping(self) -> bool:
        async with self.pool.acquire() as conn:
            return bool(await conn.fetchval("SELECT 1"))
