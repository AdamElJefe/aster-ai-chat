# aster-api

FastAPI application (`aster_api.main:create_app`) and chat worker (`python -m aster_api.generation.worker`).
See the repository README for setup; docs/ARCHITECTURE.md for the architecture.

| Module | Responsibility |
|---|---|
| `core/` | config + startup guards, DB (tenant transactions), crypto, errors, logging/redaction, rate limits |
| `auth/` | sessions, CSRF, dev login, `/me`, account deletion |
| `age/` | provider interface + mock, region resolver, the single adult gate |
| `characters/` | cards, immutable versions, catalog, publication requests, card policy minimum |
| `conversations/` | create/list/send/regenerate/export/delete, SSE events |
| `generation/` | provider adapters, prompt assembly, Redis event stream, worker |
| `ledger/` | append-only usage ledger, daily allowance |
| `admin/` | moderation queue (audited) |
