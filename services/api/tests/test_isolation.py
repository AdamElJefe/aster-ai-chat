"""Acceptance: three independent accounts cannot reach each other's data, at the API *and* at the
database (RLS) layer."""

from __future__ import annotations

import itertools
import uuid

import asyncpg
import pytest

from tests.conftest import APP_URL, Client, ClientFactory, adult_client, drain, new_conversation

pytestmark = [pytest.mark.asyncio(loop_scope="session"), pytest.mark.acceptance]

CARD = {"display_name": "Private Pal", "declared_age": 33, "first_messages": ["Hey."]}


async def _setup(clients: ClientFactory, ctx) -> dict[str, dict]:  # type: ignore[no-untyped-def]
    data: dict[str, dict] = {}
    for handle in ("alice", "bob", "carol"):
        c = await adult_client(clients, handle)
        char = (await c.post("/api/v1/characters", json={**CARD, "display_name": f"{handle} pal"})).json()["id"]
        conv = await new_conversation(c)
        rid = (await c.send(conv, f"{handle} private note")).json()["request_id"]
        await drain(ctx)
        data[handle] = {"client": c, "char": char, "conv": conv, "rid": rid}
    return data


async def test_three_accounts_api_isolation(clients: ClientFactory, ctx, owner_db: asyncpg.Connection) -> None:  # type: ignore[no-untyped-def]
    data = await _setup(clients, ctx)
    for attacker, victim in itertools.permutations(data, 2):
        a: Client = data[attacker]["client"]
        v = data[victim]
        conv, rid, char = v["conv"], v["rid"], v["char"]
        probes = [
            await a.get(f"/api/v1/conversations/{conv}"),
            await a.get(f"/api/v1/conversations/{conv}/messages"),
            await a.get(f"/api/v1/conversations/{conv}/events/{rid}"),
            await a.get(f"/api/v1/conversations/{conv}/export"),
            await a.get(f"/api/v1/conversations/{data[attacker]['conv']}/events/{rid}"),  # own conv, foreign job
            await a.post(
                f"/api/v1/conversations/{conv}/regenerate", headers={"Idempotency-Key": f"x_{uuid.uuid4().hex}"}
            ),
            await a.send(conv, "intrusion"),
            await a.patch(f"/api/v1/conversations/{conv}", json={"title": "pwned"}),
            await a.delete(f"/api/v1/conversations/{conv}"),
            await a.get(f"/api/v1/characters/{char}"),
            await a.patch(f"/api/v1/characters/{char}", json=CARD),
            await a.delete(f"/api/v1/characters/{char}"),
            await a.post("/api/v1/conversations", json={"character_id": char}),
        ]
        for r in probes:
            assert r.status_code == 404, (attacker, victim, r.request.url, r.status_code, r.text)
        listed = (await a.get("/api/v1/conversations")).json()["items"]
        assert conv not in {c["id"] for c in listed}
        mine = (await a.get("/api/v1/characters", params={"scope": "mine"})).json()["items"]
        assert char not in {c["id"] for c in mine}

    # Victims' data is intact after all probes.
    for handle, v in data.items():
        msgs = (await v["client"].get(f"/api/v1/conversations/{v['conv']}/messages")).json()["items"]
        assert len(msgs) == 3 and msgs[1]["content"] == f"{handle} private note"
        assert (await v["client"].get(f"/api/v1/conversations/{v['conv']}")).json()["title"] != "pwned"
    # No ledger entries were created by rejected intrusions.
    assert await owner_db.fetchval("SELECT count(*) FROM credit_ledger WHERE event_type = 'reserve'") == 3


async def test_rls_blocks_cross_tenant_sql(clients: ClientFactory, ctx) -> None:  # type: ignore[no-untyped-def]
    """Even a buggy query without an owner filter returns nothing for another tenant's rows."""
    data = await _setup(clients, ctx)
    alice_id = data["alice"]["client"].user_id
    conn = await asyncpg.connect(APP_URL)
    try:
        assert await conn.fetchval("SELECT rolbypassrls FROM pg_roles WHERE rolname = current_user") is False
        tables = [
            "conversations",
            "messages",
            "message_turns",
            "generation_jobs",
            "credit_ledger",
            "age_attestations",
            "idempotency_keys",
        ]
        async with conn.transaction():
            await conn.execute("SELECT set_config('app.user_id', $1, true)", str(alice_id))
            for t in tables:
                foreign = await conn.fetchval(f"SELECT count(*) FROM {t} WHERE user_id <> $1", alice_id)
                assert foreign == 0, t
                assert await conn.fetchval(f"SELECT count(*) FROM {t}") > 0, t
            # Private characters of others invisible; curated public ones visible.
            names = {r["display_name"] for r in await conn.fetch("SELECT v.display_name FROM character_versions v")}
            assert "alice pal" in names and "bob pal" not in names and "carol pal" not in names
            # Writes into another tenant are rejected.
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await conn.execute(
                    "INSERT INTO conversations (user_id, character_id, character_version, config)"
                    " VALUES ($1, $2, 1, '{}')",
                    data["bob"]["client"].user_id,
                    data["alice"]["char"],
                )
        async with conn.transaction():  # no tenant context at all → nothing
            for t in tables:
                assert await conn.fetchval(f"SELECT count(*) FROM {t}") == 0, t
            assert await conn.fetchval("SELECT count(*) FROM audit_events") == 0  # write-only for the app
    finally:
        await conn.close()


async def test_account_deletion_removes_data_and_pseudonymizes_ledger(  # type: ignore[no-untyped-def]
    clients: ClientFactory, ctx, owner_db: asyncpg.Connection
) -> None:
    data = await _setup(clients, ctx)
    alice: Client = data["alice"]["client"]
    r = await alice.delete("/api/v1/me", json={"confirm_handle": "wrong"})
    assert r.status_code == 422
    assert (await alice.delete("/api/v1/me", json={"confirm_handle": "alice"})).status_code == 204
    for table in ("conversations", "messages", "age_attestations", "sessions", "character_definitions"):
        col = "creator_id" if table == "character_definitions" else "user_id"
        assert await owner_db.fetchval(f"SELECT count(*) FROM {table} WHERE {col} = $1", alice.user_id) == 0, table
    orphaned = await owner_db.fetchval("SELECT count(*) FROM credit_ledger WHERE user_id IS NULL")
    assert orphaned == 2  # reserve + settle, kept for reconciliation without the user id
    assert (await alice.get("/api/v1/me")).status_code == 401
    # Other users unaffected.
    bob = data["bob"]
    assert len((await bob["client"].get(f"/api/v1/conversations/{bob['conv']}/messages")).json()["items"]) == 3
