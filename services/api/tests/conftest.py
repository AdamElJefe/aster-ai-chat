"""Test harness: real Postgres (RLS needs it) and real Redis. No mocks of the data layer.

Env:
  TEST_PG_SUPERUSER_URL  superuser DSN used to (re)create the test DB (default postgres@localhost)
  TEST_PG_HOST           host for role DSNs (default localhost)
  TEST_REDIS_URL         default redis://localhost:6379/15
"""

from __future__ import annotations

import base64
import os
import re
import secrets
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import asyncpg
import httpx
import pytest
import pytest_asyncio
import redis.asyncio as aioredis
from asgi_lifespan import LifespanManager

from aster_api import migrate
from aster_api.core.config import Settings
from aster_api.core.context import AppContext
from aster_api.generation import worker
from aster_api.main import create_app
from aster_api.seed import seed

ROOT = Path(__file__).resolve().parents[3]
TEST_DB = "aster_test"
PG_HOST = os.environ.get("TEST_PG_HOST", "localhost")
SUPER_URL = os.environ.get("TEST_PG_SUPERUSER_URL", f"postgresql://postgres:postgres@{PG_HOST}:5432/postgres")
REDIS_URL = os.environ.get("TEST_REDIS_URL", "redis://localhost:6379/15")
OWNER_URL = f"postgresql://aster_owner:aster_owner_dev@{PG_HOST}:5432/{TEST_DB}"
APP_URL = f"postgresql://aster_app:aster_app_dev@{PG_HOST}:5432/{TEST_DB}"


def make_settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = dict(
        app_env="test",
        database_url=APP_URL,
        database_owner_url=OWNER_URL,
        redis_url=REDIS_URL,
        data_keys="k1:" + base64.b64encode(b"\x01" * 32).decode(),
        active_data_key_id="k1",
        session_pepper="test-pepper",
        mock_age_signing_secret="test-age-secret",
        allowed_regions=["US-TEST", "CA-TEST"],
        cookie_secure=False,
        mock_token_delay_ms=0,
        send_rate_per_minute=1000,
    )
    base.update(overrides)
    return Settings(**base)


async def _superuser_exec_sql_file(conn: asyncpg.Connection, path: Path) -> None:
    await conn.execute(path.read_text())


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    return "asyncio"


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def database() -> AsyncIterator[None]:
    su = await asyncpg.connect(SUPER_URL)
    try:
        await _superuser_exec_sql_file(su, ROOT / "infra/postgres/init.sql")
        await su.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = $1 AND pid <> pg_backend_pid()",
            TEST_DB,
        )
        await su.execute(f"DROP DATABASE IF EXISTS {TEST_DB}")
        await su.execute(f"CREATE DATABASE {TEST_DB} OWNER aster_owner")
    finally:
        await su.close()
    su_db = await asyncpg.connect(re.sub(r"/[^/]+$", f"/{TEST_DB}", SUPER_URL))
    try:
        # create_db.sql minus the psql meta-commands
        await su_db.execute(
            """
            ALTER SCHEMA public OWNER TO aster_owner;
            REVOKE CREATE ON SCHEMA public FROM PUBLIC;
            GRANT USAGE ON SCHEMA public TO aster_app;
            ALTER DEFAULT PRIVILEGES FOR ROLE aster_owner IN SCHEMA public
                GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO aster_app;
            """
        )
    finally:
        await su_db.close()
    await migrate.up(OWNER_URL)
    yield


TABLES = (
    "audit_events, credit_ledger, idempotency_keys, generation_jobs, messages, message_turns, conversations, "
    "character_versions, character_definitions, age_verification_sessions, age_attestations, sessions, "
    "identities, users"
)


@pytest_asyncio.fixture(loop_scope="session")
async def clean(database: None) -> AsyncIterator[None]:
    conn = await asyncpg.connect(OWNER_URL)
    try:
        # Ledger is append-only by trigger; truncate bypasses row triggers, which is what we want in tests.
        await conn.execute(f"TRUNCATE {TABLES} CASCADE")
    finally:
        await conn.close()
    r = aioredis.from_url(REDIS_URL)
    await r.flushdb()
    await r.aclose()
    yield


@pytest_asyncio.fixture(loop_scope="session")
async def settings(clean: None) -> Settings:
    s = make_settings()
    await seed(s)
    return s


@pytest_asyncio.fixture(loop_scope="session")
async def app_ctx(settings: Settings) -> AsyncIterator[tuple[Any, AppContext]]:
    app = create_app(settings, manage_logging=False)
    async with LifespanManager(app):
        yield app, app.state.ctx


@pytest_asyncio.fixture(loop_scope="session")
async def ctx(app_ctx: tuple[Any, AppContext]) -> AppContext:
    return app_ctx[1]


@pytest_asyncio.fixture(loop_scope="session")
async def owner_db(database: None) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(OWNER_URL)
    yield conn
    await conn.close()


@dataclass
class Client:
    http: httpx.AsyncClient
    handle: str
    user_id: uuid.UUID
    csrf: str

    def _h(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        return {"X-CSRF-Token": self.csrf, **(extra or {})}

    async def get(self, url: str, **kw: Any) -> httpx.Response:
        return await self.http.get(url, **kw)

    async def post(self, url: str, json: Any = None, headers: dict[str, str] | None = None) -> httpx.Response:
        return await self.http.post(url, json=json, headers=self._h(headers))

    async def patch(self, url: str, json: Any = None) -> httpx.Response:
        return await self.http.patch(url, json=json, headers=self._h())

    async def delete(self, url: str, json: Any = None) -> httpx.Response:
        return await self.http.request("DELETE", url, json=json, headers=self._h())

    async def send(self, conv_id: Any, content: str, key: str | None = None) -> httpx.Response:
        return await self.post(
            f"/api/v1/conversations/{conv_id}/messages",
            json={"content": content},
            headers={"Idempotency-Key": key or f"msg_{secrets.token_hex(8)}"},
        )


class ClientFactory:
    def __init__(self, app: Any) -> None:
        self.app = app
        self._open: list[httpx.AsyncClient] = []

    async def login(self, handle: str, region: str = "US-TEST") -> Client:
        http = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://test", headers={"X-Dev-Region": region}
        )
        self._open.append(http)
        r = await http.post("/api/v1/auth/dev/login", json={"handle": handle})
        assert r.status_code == 200, r.text
        body = r.json()
        return Client(http=http, handle=handle, user_id=uuid.UUID(body["user_id"]), csrf=body["csrf_token"])

    async def anonymous(self) -> httpx.AsyncClient:
        http = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://test")
        self._open.append(http)
        return http

    async def close(self) -> None:
        for c in self._open:
            await c.aclose()


@pytest_asyncio.fixture(loop_scope="session")
async def clients(app_ctx: tuple[Any, AppContext]) -> AsyncIterator[ClientFactory]:
    f = ClientFactory(app_ctx[0])
    yield f
    await f.close()


async def verify_adult(c: Client, outcome: str = "adult") -> httpx.Response:
    start = await c.post("/api/v1/age/start")
    assert start.status_code == 200, start.text
    ref = start.json()["session_ref"]
    tok = await c.post("/api/v1/dev/mock-age-provider/decide", json={"session_ref": ref, "outcome": outcome})
    assert tok.status_code == 200, tok.text
    return await c.post("/api/v1/age/callback", json={"token": tok.json()["token"]})


async def adult_client(clients: ClientFactory, handle: str) -> Client:
    c = await clients.login(handle)
    r = await verify_adult(c)
    assert r.status_code == 200 and r.json()["adult_access"] is True, r.text
    return c


async def curated_id(c: Client, name: str = "Rina Kurosawa") -> str:
    r = await c.get("/api/v1/characters", params={"q": name})
    return str(r.json()["items"][0]["id"])


async def new_conversation(c: Client, character_id: str | None = None) -> str:
    cid = character_id or await curated_id(c)
    r = await c.post("/api/v1/conversations", json={"character_id": cid})
    assert r.status_code == 201, r.text
    return str(r.json()["id"])


async def drain(ctx: AppContext, limit: int = 100) -> int:
    n = 0
    while n < limit and await worker.run_once(ctx):
        n += 1
    return n


def parse_sse(text: str) -> list[dict[str, Any]]:
    import json

    out = []
    for block in text.split("\n\n"):
        ev: dict[str, Any] = {}
        for line in block.splitlines():
            if line.startswith("id: "):
                ev["id"] = line[4:]
            elif line.startswith("event: "):
                ev["event"] = line[7:]
            elif line.startswith("data: "):
                ev["data"] = json.loads(line[6:])
        if "event" in ev:
            out.append(ev)
    return out
