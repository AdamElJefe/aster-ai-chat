# Runbook

## Processes

| Process | Command | Health |
|---|---|---|
| API | `uvicorn --factory aster_api.main:create_app` | `GET /health` (liveness), `GET /ready` (DB + Redis) |
| Chat worker | `python -m aster_api.generation.worker` | logs `worker started`; queue depth: `SELECT status, count(*) FROM generation_jobs GROUP BY 1` |
| Migrations | `python -m aster_api.migrate up|down N|status` (owner role, advisory-locked) | `status` |

## Common situations

**Replies stuck "pending".** Is the worker running? Jobs left by a crashed worker are re-claimed automatically when their lease expires (`WORKER_LEASE_SECONDS`, default 120). After `WORKER_MAX_ATTEMPTS` they fail and the usage reservation is released.

**User hit the daily limit in testing.** `SELECT ... FROM credit_ledger` shows reserve/settle/release rows. Grant via `aster_api.ledger.service.grant` (adds an `adjustment` row). Never edit ledger rows: a trigger blocks it.

**Rotate the content encryption key.** Add the new key to `DATA_KEYS` (`k2:...,k1:...`), set `ACTIVE_DATA_KEY_ID=k2`, restart API and worker. Old rows still decrypt with `k1`. A bulk re-encryption job is not built yet.

**Age provider down (real provider).** Fail closed: new verifications stop; existing attestations continue until expiry.

**Suspected cross-tenant access.** Run `pytest -m acceptance` against the build. Check `audit_events` (owner role; the app role can't read them).

## Redis contents

`gen:{request_id}` streams hold reply deltas (chat text) for `STREAM_TTL_SECONDS` after completion. Redis must stay on the private network and never be persisted to shared or unencrypted backups. `rl:send:*` are rate-limit counters.
