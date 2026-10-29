"""Conversation lifecycle, generation worker, ledger correctness and streaming."""

from __future__ import annotations

import asyncio
import logging

import asyncpg
import pytest

from aster_api.generation import events
from tests.conftest import ClientFactory, adult_client, drain, new_conversation, parse_sse

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def _events(c, conv: str, rid: str, last: str | None = None) -> list[dict]:  # type: ignore[no-untyped-def]
    headers = {"Last-Event-ID": last} if last else {}
    r = await c.http.get(f"/api/v1/conversations/{conv}/events/{rid}", headers=headers)
    assert r.status_code == 200, r.text
    return parse_sse(r.text)


async def test_send_stream_persist(clients: ClientFactory, ctx) -> None:  # type: ignore[no-untyped-def]
    c = await adult_client(clients, "alice")
    conv = await new_conversation(c)
    greeting = (await c.get(f"/api/v1/conversations/{conv}/messages")).json()["items"]
    assert len(greeting) == 1 and greeting[0]["role"] == "assistant" and greeting[0]["content"]

    r = await c.send(conv, "How was your day?")
    assert r.status_code == 202
    acc = r.json()
    pending = (await c.get(f"/api/v1/conversations/{conv}/messages")).json()["items"]
    assert pending[-1]["state"] == "pending" and pending[-1]["content"] is None

    assert await drain(ctx) == 1
    evs = await _events(c, conv, acc["request_id"])
    kinds = [e["event"] for e in evs]
    assert kinds[0] == "message.started" and kinds[-2:] == ["message.completed", "usage.finalized"]
    assert all(e["data"]["request_id"] == acc["request_id"] and e["data"]["conversation_id"] == conv for e in evs)
    streamed = "".join(e["data"]["text"] for e in evs if e["event"] == "message.delta")
    final = next(e for e in evs if e["event"] == "message.completed")["data"]["content"]
    assert streamed == final and "Rina Kurosawa" in final

    msgs = (await c.get(f"/api/v1/conversations/{conv}/messages")).json()["items"]
    assert [(m["role"], m["state"]) for m in msgs] == [
        ("assistant", "completed"),
        ("user", "completed"),
        ("assistant", "completed"),
    ]
    assert msgs[-1]["content"] == final
    bal = (await c.get("/api/v1/credits/balance")).json()
    assert bal["used"] == 1 and bal["remaining"] == 49


async def test_content_encrypted_at_rest(clients: ClientFactory, ctx, owner_db: asyncpg.Connection) -> None:  # type: ignore[no-untyped-def]
    c = await adult_client(clients, "bob")
    conv = await new_conversation(c)
    await c.send(conv, "a very distinctive phrase zebra-umbrella")
    await drain(ctx)
    rows = await owner_db.fetch("SELECT content_ciphertext FROM messages WHERE conversation_id = $1", conv)
    assert rows and all(b"zebra-umbrella" not in bytes(r["content_ciphertext"]) for r in rows)


async def test_idempotent_send_three_times(clients: ClientFactory, ctx, owner_db: asyncpg.Connection) -> None:  # type: ignore[no-untyped-def]
    c = await adult_client(clients, "carol")
    conv = await new_conversation(c)
    results = await asyncio.gather(*[c.send(conv, "same", key="msg_fixed_key_1") for _ in range(3)])
    assert {r.status_code for r in results} == {202}
    assert len({r.json()["request_id"] for r in results}) == 1
    rid = results[0].json()["request_id"]
    assert await owner_db.fetchval("SELECT count(*) FROM generation_jobs WHERE conversation_id = $1", conv) == 1
    assert (
        await owner_db.fetchval(
            "SELECT count(*) FROM credit_ledger WHERE causation_id = $1 AND event_type='reserve'", rid
        )
        == 1
    )
    await drain(ctx)
    await drain(ctx)
    assert (
        await owner_db.fetchval(
            "SELECT count(*) FROM credit_ledger WHERE causation_id = $1 AND event_type='settle'", rid
        )
        == 1
    )
    r = await c.send(conv, "different body", key="msg_fixed_key_1")
    assert r.status_code == 409 and r.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"


async def test_idempotency_key_required(clients: ClientFactory) -> None:
    c = await adult_client(clients, "dan")
    conv = await new_conversation(c)
    r = await c.post(f"/api/v1/conversations/{conv}/messages", json={"content": "hi"})
    assert r.status_code == 422


async def test_one_generation_in_flight(clients: ClientFactory, ctx) -> None:  # type: ignore[no-untyped-def]
    c = await adult_client(clients, "eve")
    conv = await new_conversation(c)
    assert (await c.send(conv, "one")).status_code == 202
    r = await c.send(conv, "two")
    assert r.status_code == 409 and r.json()["error"]["code"] == "GENERATION_IN_PROGRESS"
    await drain(ctx)
    assert (await c.send(conv, "three")).status_code == 202


async def test_failure_releases_reservation(clients: ClientFactory, ctx, owner_db: asyncpg.Connection) -> None:  # type: ignore[no-untyped-def]
    c = await adult_client(clients, "fay")
    conv = await new_conversation(c)
    rid = (await c.send(conv, "please [[mock:fail]]")).json()["request_id"]
    await drain(ctx)
    evs = await _events(c, conv, rid)
    assert [e["event"] for e in evs][-2:] == ["message.failed", "usage.finalized"]
    assert evs[-1]["data"]["charged_messages"] == 0
    types = [
        r["event_type"]
        for r in await owner_db.fetch(
            "SELECT event_type FROM credit_ledger WHERE causation_id = $1 ORDER BY created_at", rid
        )
    ]
    assert types == ["reserve", "release"]
    assert (await c.get("/api/v1/credits/balance")).json()["used"] == 0
    msgs = (await c.get(f"/api/v1/conversations/{conv}/messages")).json()["items"]
    assert msgs[-1]["state"] == "failed"


async def test_retryable_failure_retries_then_succeeds(clients: ClientFactory, ctx) -> None:  # type: ignore[no-untyped-def]
    c = await adult_client(clients, "gus")
    conv = await new_conversation(c)
    rid = (await c.send(conv, "hello [[mock:flaky]]")).json()["request_id"]
    assert await drain(ctx) == 2
    evs = await _events(c, conv, rid)
    started = [e["data"]["attempt"] for e in evs if e["event"] == "message.started"]
    assert started == [1, 2]
    assert sum(e["event"] == "message.completed" for e in evs) == 1


async def test_daily_limit_enforced_server_side(clients: ClientFactory, ctx, owner_db: asyncpg.Connection) -> None:  # type: ignore[no-untyped-def]
    c = await adult_client(clients, "hal")
    await owner_db.execute("UPDATE plan_definitions SET daily_message_limit = 2 WHERE id = 'dev_free'")
    try:
        conv = await new_conversation(c)
        for i in range(2):
            assert (await c.send(conv, f"m{i}")).status_code == 202
            await drain(ctx)
        r = await c.send(conv, "over")
        assert r.status_code == 402 and r.json()["error"]["code"] == "USAGE_LIMIT_REACHED"
        assert await owner_db.fetchval("SELECT count(*) FROM generation_jobs WHERE conversation_id = $1", conv) == 2
    finally:
        await owner_db.execute("UPDATE plan_definitions SET daily_message_limit = 50 WHERE id = 'dev_free'")


async def test_regenerate_keeps_previous_variant(clients: ClientFactory, ctx) -> None:  # type: ignore[no-untyped-def]
    c = await adult_client(clients, "ida")
    conv = await new_conversation(c)
    await c.send(conv, "tell me a story")
    await drain(ctx)
    first = (await c.get(f"/api/v1/conversations/{conv}/messages")).json()["items"][-1]
    r = await c.post(f"/api/v1/conversations/{conv}/regenerate", headers={"Idempotency-Key": "regen_key_0001"})
    assert r.status_code == 202
    await drain(ctx)
    latest = (await c.get(f"/api/v1/conversations/{conv}/messages")).json()["items"][-1]
    assert latest["variant"] == 1 and latest["variant_count"] == 2 and latest["id"] != first["id"]
    export = (await c.get(f"/api/v1/conversations/{conv}/export")).json()
    variants = [m for m in export["messages"] if m["role"] == "assistant" and m["ordinal"] == 1]
    assert [(v["variant"], v["active"]) for v in variants] == [(0, False), (1, True)]
    assert variants[0]["content"] == first["content"]


async def test_export_and_delete_cascade(clients: ClientFactory, ctx, owner_db: asyncpg.Connection) -> None:  # type: ignore[no-untyped-def]
    c = await adult_client(clients, "jay")
    conv = await new_conversation(c)
    rid = (await c.send(conv, "export me")).json()["request_id"]
    await drain(ctx)
    export = (await c.get(f"/api/v1/conversations/{conv}/export")).json()
    assert export["format"] == "aster.conversation.v1"
    assert [m["role"] for m in export["messages"]] == ["assistant", "user", "assistant"]
    assert export["character_card"]["display_name"] == "Rina Kurosawa"
    assert await ctx.redis.exists(events.stream_key(rid))
    assert (await c.delete(f"/api/v1/conversations/{conv}")).status_code == 204
    for table in ("messages", "message_turns", "generation_jobs"):
        assert await owner_db.fetchval(f"SELECT count(*) FROM {table} WHERE conversation_id = $1", conv) == 0
    assert not await ctx.redis.exists(events.stream_key(rid))
    assert (await c.get(f"/api/v1/conversations/{conv}")).status_code == 404


async def test_archive_blocks_new_messages(clients: ClientFactory) -> None:
    c = await adult_client(clients, "kim")
    conv = await new_conversation(c)
    r = await c.patch(f"/api/v1/conversations/{conv}", json={"status": "archived", "title": "Old"})
    assert r.json()["status"] == "archived" and r.json()["title"] == "Old"
    assert (await c.send(conv, "hi")).status_code == 409
    archived = (await c.get("/api/v1/conversations", params={"status": "archived"})).json()["items"]
    assert [a["id"] for a in archived] == [conv]


async def test_conversation_list_pagination(clients: ClientFactory) -> None:
    c = await adult_client(clients, "lou")
    ids = [await new_conversation(c) for _ in range(5)]
    seen: list[str] = []
    cursor = None
    while True:
        params = {"limit": 2, **({"cursor": cursor} if cursor else {})}
        page = (await c.get("/api/v1/conversations", params=params)).json()
        seen += [i["id"] for i in page["items"]]
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert sorted(seen) == sorted(ids) and len(seen) == 5


async def test_sse_reconnect_never_duplicates_final(clients: ClientFactory, ctx) -> None:  # type: ignore[no-untyped-def]
    c = await adult_client(clients, "mia")
    conv = await new_conversation(c)
    rid = (await c.send(conv, "stream test with several words in it")).json()["request_id"]
    await drain(ctx)
    full = await _events(c, conv, rid)
    mid = full[len(full) // 2]
    resumed = await _events(c, conv, rid, last=mid["id"])
    assert [e["id"] for e in resumed] == [e["id"] for e in full[len(full) // 2 + 1 :]]
    assert sum(e["event"] == "message.completed" for e in resumed) == 1
    # Reconnect after the terminal event (EventSource auto-reconnect) yields nothing new.
    terminal = next(e for e in full if e["event"] == "message.completed")
    assert await _events(c, conv, rid, last=terminal["id"]) == []
    assert await _events(c, conv, rid, last=full[-1]["id"]) == []


async def test_sse_falls_back_to_db_when_stream_expired(clients: ClientFactory, ctx) -> None:  # type: ignore[no-untyped-def]
    c = await adult_client(clients, "ned")
    conv = await new_conversation(c)
    rid = (await c.send(conv, "expire")).json()["request_id"]
    await drain(ctx)
    final = (await c.get(f"/api/v1/conversations/{conv}/messages")).json()["items"][-1]["content"]
    await ctx.redis.delete(events.stream_key(rid))
    evs = await _events(c, conv, rid)
    assert [e["event"] for e in evs] == ["message.completed"] and evs[0]["data"]["content"] == final


async def test_lease_expiry_recovers_crashed_worker(clients: ClientFactory, ctx, owner_db: asyncpg.Connection) -> None:  # type: ignore[no-untyped-def]
    """A worker that claimed a job and died: the lease expires, another worker finishes it once."""
    c = await adult_client(clients, "ozzy")
    conv = await new_conversation(c)
    rid = (await c.send(conv, "crash me")).json()["request_id"]
    async with ctx.db.system() as conn:
        claim = await conn.fetchrow("SELECT * FROM claim_generation_job(60)")
    assert str(claim["job_id"]) == rid
    assert await drain(ctx) == 0  # leased, nothing else to claim
    await owner_db.execute("UPDATE generation_jobs SET lease_expires_at = now() - interval '1s' WHERE id = $1", rid)
    assert await drain(ctx) == 1
    msgs = (await c.get(f"/api/v1/conversations/{conv}/messages")).json()["items"]
    assert msgs[-1]["state"] == "completed"
    assert await owner_db.fetchval("SELECT attempt FROM generation_jobs WHERE id = $1", rid) == 2
    assert (
        await owner_db.fetchval(
            "SELECT count(*) FROM credit_ledger WHERE causation_id = $1 AND event_type = 'settle'", rid
        )
        == 1
    )


async def test_no_chat_text_in_logs(clients: ClientFactory, ctx, caplog: pytest.LogCaptureFixture) -> None:  # type: ignore[no-untyped-def]
    caplog.set_level(logging.DEBUG)
    c = await adult_client(clients, "pip")
    conv = await new_conversation(c)
    await c.send(conv, "secret-canary-7731 should never be logged")
    await drain(ctx)
    from aster_api.core.logging import JsonFormatter

    fmt = JsonFormatter()
    rendered = "\n".join(fmt.format(r) for r in caplog.records)
    assert "secret-canary-7731" not in rendered


async def test_sse_not_compressible_by_proxies(clients: ClientFactory, ctx) -> None:  # type: ignore[no-untyped-def]
    """Proxies that gzip responses (e.g. the Next.js dev/prod server) buffer the whole stream unless
    told not to transform it; that made replies appear all at once instead of streaming."""
    c = await adult_client(clients, "proxy_check")
    conv = await new_conversation(c)
    rid = (await c.send(conv, "hello")).json()["request_id"]
    await drain(ctx)
    r = await c.http.get(f"/api/v1/conversations/{conv}/events/{rid}")
    assert "no-transform" in r.headers["cache-control"]
    assert r.headers["content-type"].startswith("text/event-stream")
