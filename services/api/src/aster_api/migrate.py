"""Minimal SQL migration runner (owner role). Usage:

python -m aster_api.migrate up        # apply pending
python -m aster_api.migrate down 1    # roll back N
python -m aster_api.migrate status
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import asyncpg

from aster_api.core.config import get_settings

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"
LOCK_ID = 7_142_026  # advisory lock so concurrent deploys can't race


def _discover() -> list[str]:
    return sorted(p.name.removesuffix(".up.sql") for p in MIGRATIONS_DIR.glob("*.up.sql"))


async def _applied(conn: asyncpg.Connection) -> list[str]:
    await conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations"
        " (name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
    )
    return [r["name"] for r in await conn.fetch("SELECT name FROM schema_migrations ORDER BY name")]


async def up(dsn: str) -> list[str]:
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute("SELECT pg_advisory_lock($1)", LOCK_ID)
        done = set(await _applied(conn))
        ran = []
        for name in _discover():
            if name in done:
                continue
            sql = (MIGRATIONS_DIR / f"{name}.up.sql").read_text()
            async with conn.transaction():
                await conn.execute(sql)
                await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1)", name)
            ran.append(name)
        return ran
    finally:
        await conn.close()


async def down(dsn: str, steps: int) -> list[str]:
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute("SELECT pg_advisory_lock($1)", LOCK_ID)
        applied = await _applied(conn)
        reverted = []
        for name in reversed(applied[-steps:]):
            sql = (MIGRATIONS_DIR / f"{name}.down.sql").read_text()
            async with conn.transaction():
                await conn.execute(sql)
                await conn.execute("DELETE FROM schema_migrations WHERE name = $1", name)
            reverted.append(name)
        return reverted
    finally:
        await conn.close()


async def status(dsn: str) -> dict[str, bool]:
    conn = await asyncpg.connect(dsn)
    try:
        done = set(await _applied(conn))
        return {name: name in done for name in _discover()}
    finally:
        await conn.close()


def main(argv: list[str]) -> None:
    dsn = get_settings().database_owner_url
    cmd = argv[1] if len(argv) > 1 else "up"
    if cmd == "up":
        print("applied:", asyncio.run(up(dsn)) or "nothing")
    elif cmd == "down":
        print("reverted:", asyncio.run(down(dsn, int(argv[2]) if len(argv) > 2 else 1)))
    elif cmd == "status":
        for name, ok in asyncio.run(status(dsn)).items():
            print(("[x] " if ok else "[ ] ") + name)
    else:
        raise SystemExit(f"unknown command {cmd}")


if __name__ == "__main__":
    main(sys.argv)
