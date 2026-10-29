"""Acceptance with real processes: a 20-turn conversation persists across an API restart, the
worker runs as a separate process, and SSE is consumed over real HTTP.

Run: pytest -m e2e   (spawns uvicorn + worker subprocesses on a free port)
"""

from __future__ import annotations

import base64
import json
import os
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from tests.conftest import APP_URL, OWNER_URL, REDIS_URL

pytestmark = [pytest.mark.e2e, pytest.mark.acceptance]

API_DIR = Path(__file__).resolve().parents[1]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _env() -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        {
            "APP_ENV": "test",
            "DATABASE_URL": APP_URL,
            "DATABASE_OWNER_URL": OWNER_URL,
            "REDIS_URL": REDIS_URL,
            "DATA_KEYS": "k1:" + base64.b64encode(b"\x01" * 32).decode(),
            "ACTIVE_DATA_KEY_ID": "k1",
            "SESSION_PEPPER": "test-pepper",
            "MOCK_AGE_SIGNING_SECRET": "test-age-secret",
            "ALLOWED_REGIONS": "US-TEST",
            "COOKIE_SECURE": "false",
            "MOCK_TOKEN_DELAY_MS": "2",
            "SEND_RATE_PER_MINUTE": "1000",
        }
    )
    return env


class Proc:
    def __init__(self, args: list[str]) -> None:
        self.args = args
        self.p: subprocess.Popen[bytes] | None = None

    def start(self) -> None:
        self.p = subprocess.Popen(self.args, cwd=API_DIR, env=_env(), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

    def stop(self) -> None:
        if self.p and self.p.poll() is None:
            self.p.send_signal(signal.SIGTERM)
            try:
                self.p.wait(10)
            except subprocess.TimeoutExpired:
                self.p.kill()


@pytest.fixture
def stack(settings) -> Iterator[tuple[Proc, Proc, str]]:  # type: ignore[no-untyped-def]
    port = _free_port()
    api = Proc([sys.executable, "-m", "uvicorn", "--factory", "aster_api.main:create_app", "--port", str(port)])
    wrk = Proc([sys.executable, "-m", "aster_api.generation.worker"])
    api.start()
    wrk.start()
    base = f"http://127.0.0.1:{port}"
    _wait_ready(base)
    yield api, wrk, base
    api.stop()
    wrk.stop()


def _wait_ready(base: str, timeout: float = 20) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if httpx.get(f"{base}/ready", timeout=1).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.2)
    raise RuntimeError("API did not become ready")


def _stream(client: httpx.Client, url: str) -> list[tuple[str, dict]]:
    out: list[tuple[str, dict]] = []
    event = None
    with client.stream("GET", url, timeout=30) as r:
        assert r.status_code == 200
        for line in r.iter_lines():
            if line.startswith("event: "):
                event = line[7:]
            elif line.startswith("data: ") and event:
                out.append((event, json.loads(line[6:])))
    return out


def test_twenty_turns_survive_api_restart(stack) -> None:  # type: ignore[no-untyped-def]
    api, _wrk, base = stack
    c = httpx.Client(base_url=base, headers={"X-Dev-Region": "US-TEST"})
    login = c.post("/api/v1/auth/dev/login", json={"handle": "restart_user"}).json()
    c.headers["X-CSRF-Token"] = login["csrf_token"]
    ref = c.post("/api/v1/age/start").json()["session_ref"]
    tok = c.post("/api/v1/dev/mock-age-provider/decide", json={"session_ref": ref, "outcome": "adult"}).json()["token"]
    assert c.post("/api/v1/age/callback", json={"token": tok}).json()["adult_access"] is True
    char = c.get("/api/v1/characters").json()["items"][0]["id"]
    conv = c.post("/api/v1/conversations", json={"character_id": char}).json()["id"]

    def turn(i: int) -> None:
        r = c.post(
            f"/api/v1/conversations/{conv}/messages",
            json={"content": f"turn {i}"},
            headers={"Idempotency-Key": f"msg_restart_{i:04d}"},
        )
        assert r.status_code == 202, r.text
        evs = _stream(c, r.json()["events_url"])
        assert sum(1 for e, _ in evs if e == "message.completed") == 1, evs

    for i in range(10):
        turn(i)

    # Restart the API mid-conversation (cookie session survives: it's server-side in Postgres).
    api.stop()
    api.start()
    _wait_ready(base)

    for i in range(10, 20):
        turn(i)

    msgs = c.get(f"/api/v1/conversations/{conv}/messages", params={"limit": 200}).json()["items"]
    assert len(msgs) == 41  # greeting + 20 × (user + assistant)
    assert [m["content"] for m in msgs if m["role"] == "user"] == [f"turn {i}" for i in range(20)]
    assert all(m["state"] == "completed" and m["content"] for m in msgs)
    assert c.get("/api/v1/credits/balance").json()["used"] == 20
    c.close()


def test_in_flight_reply_survives_api_restart(stack) -> None:  # type: ignore[no-untyped-def]
    """The reply is produced by the worker, so killing the API mid-stream loses nothing."""
    api, _wrk, base = stack
    c = httpx.Client(base_url=base, headers={"X-Dev-Region": "US-TEST"})
    c.headers["X-CSRF-Token"] = c.post("/api/v1/auth/dev/login", json={"handle": "inflight_user"}).json()["csrf_token"]
    ref = c.post("/api/v1/age/start").json()["session_ref"]
    tok = c.post("/api/v1/dev/mock-age-provider/decide", json={"session_ref": ref, "outcome": "adult"}).json()["token"]
    c.post("/api/v1/age/callback", json={"token": tok})
    conv = c.post(
        "/api/v1/conversations", json={"character_id": c.get("/api/v1/characters").json()["items"][0]["id"]}
    ).json()["id"]
    acc = c.post(
        f"/api/v1/conversations/{conv}/messages",
        json={"content": "long " * 40},
        headers={"Idempotency-Key": "msg_inflight_0001"},
    ).json()
    api.stop()
    time.sleep(0.5)
    api.start()
    _wait_ready(base)
    evs = _stream(c, acc["events_url"])  # full replay from the start
    assert sum(1 for e, _ in evs if e == "message.completed") == 1
    msgs = c.get(f"/api/v1/conversations/{conv}/messages").json()["items"]
    assert msgs[-1]["state"] == "completed"
    c.close()
