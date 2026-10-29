"""Chat generation worker. Run with: python -m aster_api.generation.worker

Claims jobs via claim_generation_job() (lease-based, SKIP LOCKED), streams deltas to Redis, persists
the final reply exactly once, and settles or releases usage. Safe to run several replicas.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import time
import uuid

from aster_api.core.config import get_settings
from aster_api.core.context import AppContext
from aster_api.core.logging import configure_logging
from aster_api.generation import events
from aster_api.generation.prompt import assemble
from aster_api.generation.providers import GenerationRequest, ProviderError
from aster_api.ledger import service as ledger

log = logging.getLogger("aster.worker")
HEARTBEAT_SECONDS = 5.0


class LeaseLost(Exception):
    pass


async def run_once(ctx: AppContext) -> bool:
    """Process at most one job. Returns False when the queue was empty."""
    async with ctx.db.system() as conn:
        claim = await conn.fetchrow("SELECT * FROM claim_generation_job($1)", ctx.settings.worker_lease_seconds)
    if claim is None or claim["job_id"] is None:
        return False
    await process_job(ctx, claim["job_id"], claim["owner_id"], claim["attempt"])
    return True


async def process_job(ctx: AppContext, job_id: uuid.UUID, owner: uuid.UUID, attempt: int) -> None:
    s = ctx.settings
    async with ctx.db.tenant(owner) as conn:
        job = await conn.fetchrow(
            """
            SELECT j.conversation_id, j.assistant_message_id, m.turn_id, m.variant, c.character_id, c.character_version,
                   c.config, v.card, v.declared_age, t.ordinal
              FROM generation_jobs j
              JOIN messages m ON m.id = j.assistant_message_id
              JOIN message_turns t ON t.id = m.turn_id
              JOIN conversations c ON c.id = j.conversation_id
              JOIN character_versions v ON v.character_id = c.character_id AND v.version = c.character_version
             WHERE j.id = $1
            """,
            job_id,
        )
        if job is None:  # conversation deleted after claim
            return
        conv_id = job["conversation_id"]
        if attempt > s.worker_max_attempts:
            await _fail(ctx, conn, job_id, owner, conv_id, job["assistant_message_id"], "MAX_ATTEMPTS", attempt)
            return
        history_rows = await conn.fetch(
            """
            SELECT m.id, m.role, m.content_ciphertext, m.key_id
              FROM message_turns t
              JOIN messages m ON m.turn_id = t.id AND (m.role = 'user' OR m.id = t.active_variant_id)
             WHERE t.conversation_id = $1 AND t.ordinal <= $2 AND m.state = 'completed'
             ORDER BY t.ordinal, CASE m.role WHEN 'user' THEN 0 ELSE 1 END
            """,
            conv_id,
            job["ordinal"],
        )
        await conn.execute(
            "UPDATE messages SET state = 'streaming' WHERE id = $1 AND state IN ('pending', 'streaming')",
            job["assistant_message_id"],
        )
    history = [(r["role"], ctx.cipher.decrypt(r["content_ciphertext"], r["key_id"], r["id"])) for r in history_rows]
    prompt = assemble(job["card"], job["declared_age"], history, s.max_context_tokens, s.max_output_tokens)

    await events.publish(
        ctx.redis,
        job_id,
        conv_id,
        "message.started",
        {
            "message_id": str(job["assistant_message_id"]),
            "attempt": attempt,
        },
    )
    last_beat = time.monotonic()

    async def on_delta(text: str) -> None:
        nonlocal last_beat
        await events.publish(ctx.redis, job_id, conv_id, "message.delta", {"text": text})
        if time.monotonic() - last_beat > HEARTBEAT_SECONDS:
            last_beat = time.monotonic()
            async with ctx.db.tenant(owner) as hb:
                ok = await hb.fetchval(
                    "UPDATE generation_jobs SET lease_expires_at = now() + make_interval(secs => $3)"
                    " WHERE id = $1 AND attempt = $2 AND status = 'running' RETURNING 1",
                    job_id,
                    attempt,
                    s.worker_lease_seconds,
                )
            if not ok:
                raise LeaseLost()

    req = GenerationRequest(
        model_id=job["config"]["model_id"],
        messages=prompt.messages,
        max_output_tokens=int(job["config"]["max_output_tokens"]),
        correlation_id=str(job_id),
        character_name=job["card"]["display_name"],
    )
    started = time.monotonic()
    try:
        result = await ctx.text.stream(req, on_delta)
    except LeaseLost:
        log.warning("lease lost; abandoning attempt", extra={"job_id": str(job_id), "attempt": attempt})
        return
    except ProviderError as exc:
        async with ctx.db.tenant(owner) as conn:
            if exc.retryable and attempt < s.worker_max_attempts:
                await conn.execute(
                    "UPDATE generation_jobs SET status = 'queued', error_code = $3, updated_at = now()"
                    " WHERE id = $1 AND attempt = $2 AND status = 'running'",
                    job_id,
                    attempt,
                    exc.code,
                )
                log.info("retrying generation", extra={"job_id": str(job_id), "code": exc.code, "attempt": attempt})
                return
            await _fail(ctx, conn, job_id, owner, conv_id, job["assistant_message_id"], exc.code, attempt)
        return

    msg_id = job["assistant_message_id"]
    blob, key_id = ctx.cipher.encrypt(result.text, msg_id)
    async with ctx.db.tenant(owner) as conn:
        # Guard on attempt: if our lease expired and another worker took over, we persist nothing.
        owned = await conn.fetchval(
            "UPDATE generation_jobs SET status = 'completed', lease_expires_at = NULL, updated_at = now()"
            " WHERE id = $1 AND attempt = $2 AND status = 'running' RETURNING 1",
            job_id,
            attempt,
        )
        if not owned:
            log.warning("job no longer owned at finalize", extra={"job_id": str(job_id)})
            return
        await conn.execute(
            """
            UPDATE messages SET state = 'completed', content_ciphertext = $2, key_id = $3, prompt_tokens = $4,
                   completion_tokens = $5, model_id = $6, template_version = $7, finish_reason = $8,
                   completed_at = now()
             WHERE id = $1 AND state IN ('pending', 'streaming')
            """,
            msg_id,
            blob,
            key_id,
            result.prompt_tokens,
            result.completion_tokens,
            result.model_id,
            prompt.template_version,
            result.finish_reason,
        )
        await ledger.settle(
            conn,
            owner,
            job_id,
            s.session_pepper,
            {
                "prompt_tokens": result.prompt_tokens,
                "completion_tokens": result.completion_tokens,
                "estimated": result.usage_estimated,
                "model_id": result.model_id,
            },
        )
        await conn.execute("UPDATE conversations SET last_message_at = now() WHERE id = $1", conv_id)

    await events.publish(
        ctx.redis,
        job_id,
        conv_id,
        "message.completed",
        {
            "message_id": str(msg_id),
            "variant": job["variant"],
            "content": result.text,
            "finish_reason": result.finish_reason,
        },
    )
    await events.publish(
        ctx.redis,
        job_id,
        conv_id,
        "usage.finalized",
        {
            "charged_messages": 1,
            "prompt_tokens": result.prompt_tokens,
            "completion_tokens": result.completion_tokens,
            "estimated": result.usage_estimated,
        },
    )
    await events.finish(ctx.redis, job_id, s.stream_ttl_seconds)
    log.info(
        "generation completed",
        extra={
            "job_id": str(job_id),
            "attempt": attempt,
            "ms": int((time.monotonic() - started) * 1000),
            "completion_tokens": result.completion_tokens,
            "history_truncated": prompt.history_turns_truncated,
        },
    )


async def _fail(ctx, conn, job_id, owner, conv_id, msg_id, code: str, attempt: int) -> None:  # type: ignore[no-untyped-def]
    await conn.execute(
        "UPDATE generation_jobs SET status = 'failed', error_code = $2, lease_expires_at = NULL, updated_at = now()"
        " WHERE id = $1",
        job_id,
        code,
    )
    await conn.execute("UPDATE messages SET state = 'failed' WHERE id = $1 AND state <> 'completed'", msg_id)
    await ledger.release(conn, owner, job_id, ctx.settings.session_pepper)
    await events.publish(
        ctx.redis,
        job_id,
        conv_id,
        "message.failed",
        {
            "message_id": str(msg_id),
            "code": code,
            "retryable": False,
        },
    )
    await events.publish(ctx.redis, job_id, conv_id, "usage.finalized", {"charged_messages": 0})
    await events.finish(ctx.redis, job_id, ctx.settings.stream_ttl_seconds)
    log.warning("generation failed", extra={"job_id": str(job_id), "code": code, "attempt": attempt})


async def run_forever(ctx: AppContext, stop: asyncio.Event) -> None:
    poll = ctx.settings.worker_poll_interval_ms / 1000
    while not stop.is_set():
        try:
            if await run_once(ctx):
                continue
            await ctx.redis.blpop([events.WAKE_KEY], timeout=max(poll, 0.1))
        except Exception:
            log.exception("worker loop error")
            await asyncio.sleep(1)


async def _main() -> None:
    configure_logging()
    ctx = await AppContext.create(get_settings())
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    log.info("worker started", extra={"provider": ctx.text.name})
    try:
        await run_forever(ctx, stop)
    finally:
        await ctx.close()


if __name__ == "__main__":
    asyncio.run(_main())
