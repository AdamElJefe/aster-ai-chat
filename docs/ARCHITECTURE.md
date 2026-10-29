# Architecture

## Processes

| Process | Role |
|---|---|
| `apps/web` (Next.js) | UI. Calls the API same-origin through a rewrite, so the session cookie stays first-party. |
| `services/api` (FastAPI) | Auth, characters, conversations, usage ledger, SSE event stream. |
| Chat worker (`python -m aster_api.generation.worker`) | Claims generation jobs, calls the model, streams tokens, saves replies. Scales horizontally. |
| PostgreSQL | Source of truth. Row-level security on every per-user table. |
| Redis | Short-lived token streams (`gen:{request_id}`) and rate-limit counters. |

## Sending a message

1. `POST /api/v1/conversations/{id}/messages` with an `Idempotency-Key` header.
2. In one transaction, the API checks access and the daily allowance, reserves one message, saves the user's message and an empty assistant message, and inserts a `generation_jobs` row. It returns `202` with a `request_id`.
3. A worker claims the job (`FOR UPDATE SKIP LOCKED` with a lease), assembles the prompt and streams tokens to a Redis stream.
4. The worker saves the final reply only if it still holds the lease, settles the usage reservation (or releases it on failure), and emits `message.completed`.
5. The browser reads `GET /conversations/{id}/events/{request_id}` (SSE). On reconnect it sends `Last-Event-ID` and the server replays from there. If the Redis stream has expired, the final message is rebuilt from Postgres.

## Data protection

- **Row-level security:** the app connects as a role without `BYPASSRLS` and sets `app.user_id` per transaction. Migrations run as a separate owner role.
- **Encryption:** message content is AES-256-GCM encrypted with the message ID as associated data, so ciphertext can't be moved between rows.
- **Logging:** structured JSON logs record route templates, status codes and timings, never message text.
- **Usage ledger:** append-only (enforced by a database trigger). Each job can be reserved, settled or released at most once (unique index).

## Prompt assembly

Fixed precedence: platform rules, then character card (fenced as data), then recent turns trimmed to a token budget, then the latest message. Character text and chat history are treated as untrusted input, so they can't override the platform rules.

## Configuration guards

Mock adapters (login, age check, model) are refused at startup outside `dev`/`test`, so a mock can't reach production by accident.
