"""Identity, sessions, CSRF and the age-verification gate."""

from __future__ import annotations

import pytest

from tests.conftest import ClientFactory, adult_client, curated_id, verify_adult

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_health_and_ready(clients: ClientFactory) -> None:
    anon = await clients.anonymous()
    assert (await anon.get("/health")).json()["status"] == "ok"
    ready = await anon.get("/ready")
    assert ready.status_code == 200 and ready.json() == {"status": "ok", "database": True, "redis": True}


async def test_login_me_logout(clients: ClientFactory) -> None:
    c = await clients.login("alice")
    me = (await c.get("/api/v1/me")).json()
    assert me["handle"] == "alice" and me["adult_access"] == "unverified"
    assert me["entitlements"]["daily_message_limit"] == 50
    assert "AI" in me["ai_disclosure"]
    assert (await c.post("/api/v1/auth/logout")).status_code == 204
    assert (await c.get("/api/v1/me")).status_code == 401


async def test_session_cookie_flags(clients: ClientFactory) -> None:
    anon = await clients.anonymous()
    r = await anon.post("/api/v1/auth/dev/login", json={"handle": "flagcheck"})
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie


async def test_csrf_required_on_mutations(clients: ClientFactory) -> None:
    c = await clients.login("bob")
    r = await c.http.post("/api/v1/age/start")  # no X-CSRF-Token
    assert r.status_code == 403 and r.json()["error"]["code"] == "CSRF_FAILED"
    r = await c.http.post("/api/v1/age/start", headers={"X-CSRF-Token": "wrong"})
    assert r.status_code == 403


async def test_error_envelope_has_request_id(clients: ClientFactory) -> None:
    anon = await clients.anonymous()
    r = await anon.get("/api/v1/me")
    body = r.json()["error"]
    assert body["code"] == "UNAUTHENTICATED" and body["request_id"] == r.headers["x-request-id"]


async def test_unverified_user_blocked_without_charge(clients: ClientFactory, owner_db) -> None:  # type: ignore[no-untyped-def]
    c = await clients.login("carol")
    char_id = await curated_id(c)
    for method, url, body in [
        ("post", "/api/v1/conversations", {"character_id": char_id}),
        ("get", "/api/v1/conversations", None),
        ("post", "/api/v1/characters", {"display_name": "X", "declared_age": 30, "first_messages": ["hi"]}),
    ]:
        r = await (c.post(url, json=body) if method == "post" else c.get(url))
        assert r.status_code == 403 and r.json()["error"]["code"] == "ADULT_ACCESS_REQUIRED", url
    assert await owner_db.fetchval("SELECT count(*) FROM credit_ledger") == 0


async def test_full_mock_verification_unlocks(clients: ClientFactory) -> None:
    c = await adult_client(clients, "dave")
    assert (await c.get("/api/v1/me")).json()["adult_access"] == "verified"
    assert (await c.get("/api/v1/conversations")).status_code == 200


async def test_underage_result_denies(clients: ClientFactory) -> None:
    c = await clients.login("erin")
    r = await verify_adult(c, outcome="underage")
    assert r.status_code == 200 and r.json()["adult_access"] is False
    assert (await c.get("/api/v1/me")).json()["adult_access"] == "underage"
    assert (await c.get("/api/v1/conversations")).status_code == 403


async def test_token_replay_and_cross_user_redemption_fail(clients: ClientFactory) -> None:
    a = await clients.login("frank")
    b = await clients.login("grace")
    ref = (await a.post("/api/v1/age/start")).json()["session_ref"]
    tok = (await a.post("/api/v1/dev/mock-age-provider/decide", json={"session_ref": ref, "outcome": "adult"})).json()[
        "token"
    ]
    # Bob can't redeem Frank's session token.
    r = await b.post("/api/v1/age/callback", json={"token": tok})
    assert r.status_code == 400 and r.json()["error"]["code"] == "VERIFICATION_FAILED"
    assert (await a.post("/api/v1/age/callback", json={"token": tok})).status_code == 200
    # Replay of the same token fails (single-use session).
    assert (await a.post("/api/v1/age/callback", json={"token": tok})).status_code == 400


async def test_forged_token_rejected(clients: ClientFactory) -> None:
    c = await clients.login("heidi")
    r = await c.post("/api/v1/age/callback", json={"token": "eyJzaWQiOiJ4In0.forgedsignature"})
    assert r.status_code == 400


async def test_blocked_region_cannot_start_or_use(clients: ClientFactory) -> None:
    blocked = await clients.login("ivan", region="XX-BLOCKED")
    r = await blocked.post("/api/v1/age/start")
    assert r.status_code == 403 and r.json()["error"]["code"] == "REGION_UNAVAILABLE"
    # Verified in an allowed region, then requests arrive from a blocked one.
    c = await adult_client(clients, "judy")
    r = await c.http.get("/api/v1/conversations", headers={"X-Dev-Region": "XX-BLOCKED"})
    assert r.status_code == 403
    me = await c.http.get("/api/v1/me", headers={"X-Dev-Region": "XX-BLOCKED"})
    assert me.json()["adult_access"] == "region_blocked"


async def test_expired_revoked_and_policy_change(clients: ClientFactory, owner_db, ctx) -> None:  # type: ignore[no-untyped-def]
    c = await adult_client(clients, "ken")
    await owner_db.execute(
        "UPDATE age_attestations SET expires_at = now() - interval '1 second' WHERE user_id = $1", c.user_id
    )
    assert (await c.get("/api/v1/me")).json()["adult_access"] == "expired"
    assert (await c.get("/api/v1/conversations")).status_code == 403

    c2 = await adult_client(clients, "lena")
    await owner_db.execute("UPDATE age_attestations SET revoked_at = now() WHERE user_id = $1", c2.user_id)
    assert (await c2.get("/api/v1/conversations")).status_code == 403

    c3 = await adult_client(clients, "mallory")
    await owner_db.execute("UPDATE age_attestations SET policy_version = 'old' WHERE user_id = $1", c3.user_id)
    assert (await c3.get("/api/v1/me")).json()["adult_access"] == "policy_changed"


async def test_client_cannot_assert_age(clients: ClientFactory) -> None:
    c = await clients.login("niaj")
    # Extra fields / headers claiming adulthood change nothing.
    r = await c.http.get("/api/v1/conversations", headers={"X-Adult-Verified": "true"}, cookies={"adult": "1"})
    assert r.status_code == 403


async def test_attestation_stores_no_identity_documents(owner_db) -> None:  # type: ignore[no-untyped-def]
    cols = {
        r["column_name"]
        for r in await owner_db.fetch(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'age_attestations'"
        )
    }
    assert not cols & {"dob", "date_of_birth", "document", "id_image", "selfie", "birthdate"}
