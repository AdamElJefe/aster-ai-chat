"""Character catalog, versioning, visibility and publication gates."""

from __future__ import annotations

import asyncpg
import pytest

from tests.conftest import ClientFactory, adult_client

pytestmark = pytest.mark.asyncio(loop_scope="session")

CARD = {
    "display_name": "Juniper Hale",
    "declared_age": 29,
    "tagline": "Lighthouse keeper with a telescope habit.",
    "scenario": "A foggy night on the island.",
    "first_messages": ["You found the lighthouse. Brave or lost?"],
    "content_tags": ["Mystery", "cozy"],
}


async def test_anonymous_catalog_is_preview_only(clients: ClientFactory) -> None:
    anon = await clients.anonymous()
    page = (await anon.get("/api/v1/characters")).json()
    assert {i["display_name"] for i in page["items"]} == {"Rina Kurosawa", "Captain Idris Vale", "Morwen Ashgrove"}
    assert all(i["ai_generated_fiction"] for i in page["items"])
    view = (await anon.get(f"/api/v1/characters/{page['items'][0]['id']}")).json()
    assert view["detail"] is None and "scenario" not in view["preview"]


async def test_unverified_user_gets_preview_verified_gets_detail(clients: ClientFactory) -> None:
    c = await clients.login("viewer")
    cid = (await c.get("/api/v1/characters")).json()["items"][0]["id"]
    assert (await c.get(f"/api/v1/characters/{cid}")).json()["detail"] is None
    a = await adult_client(clients, "viewer2")
    detail = (await a.get(f"/api/v1/characters/{cid}")).json()["detail"]
    assert detail["card"]["first_messages"] and detail["is_owner"] is False


async def test_create_private_by_default_and_hidden_from_others(clients: ClientFactory) -> None:
    owner = await adult_client(clients, "creator")
    other = await adult_client(clients, "stranger")
    r = await owner.post("/api/v1/characters", json=CARD)
    assert r.status_code == 201
    char = r.json()
    assert char["visibility"] == "private" and char["moderation_state"] == "draft" and char["version"] == 1
    assert char["tags"] == ["cozy", "mystery"]
    mine = (await owner.get("/api/v1/characters", params={"scope": "mine"})).json()["items"]
    assert [m["id"] for m in mine] == [char["id"]]
    catalog = (await other.get("/api/v1/characters")).json()["items"]
    assert char["id"] not in {i["id"] for i in catalog}
    assert (await other.get(f"/api/v1/characters/{char['id']}")).status_code == 404
    r = await other.post("/api/v1/conversations", json={"character_id": char["id"]})
    assert r.status_code == 404


async def test_under_18_rejected_at_api_and_db(clients: ClientFactory, owner_db: asyncpg.Connection) -> None:
    c = await adult_client(clients, "ager")
    r = await c.post("/api/v1/characters", json={**CARD, "declared_age": 17})
    assert r.status_code == 422
    char_id = (await c.post("/api/v1/characters", json=CARD)).json()["id"]
    with pytest.raises(asyncpg.CheckViolationError):
        await owner_db.execute(
            "INSERT INTO character_versions (character_id, version, display_name, declared_age, card, content_hash)"
            " VALUES ($1, 99, 'x', 17, '{}', 'h')",
            char_id,
        )


async def test_minor_framing_rejected(clients: ClientFactory) -> None:
    c = await adult_client(clients, "framer")
    r = await c.post("/api/v1/characters", json={**CARD, "scenario": "after class at her high school"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "CONTENT_POLICY"


async def test_edit_creates_immutable_version(clients: ClientFactory, owner_db: asyncpg.Connection) -> None:
    c = await adult_client(clients, "editor")
    cid = (await c.post("/api/v1/characters", json=CARD)).json()["id"]
    r = await c.patch(f"/api/v1/characters/{cid}", json={**CARD, "tagline": "Updated."})
    assert r.status_code == 200 and r.json()["version"] == 2
    versions = await owner_db.fetch(
        "SELECT version, card->>'tagline' AS t FROM character_versions WHERE character_id = $1 ORDER BY 1", cid
    )
    assert [(v["version"], v["t"]) for v in versions] == [(1, CARD["tagline"]), (2, "Updated.")]
    with pytest.raises(asyncpg.RaiseError, match="immutable"):
        await owner_db.execute("UPDATE character_versions SET display_name = 'hacked' WHERE character_id = $1", cid)


async def test_conversation_pins_version(clients: ClientFactory) -> None:
    c = await adult_client(clients, "pinner")
    cid = (await c.post("/api/v1/characters", json=CARD)).json()["id"]
    conv = (await c.post("/api/v1/conversations", json={"character_id": cid})).json()
    await c.patch(f"/api/v1/characters/{cid}", json={**CARD, "first_messages": ["New greeting"]})
    assert (await c.get(f"/api/v1/conversations/{conv['id']}")).json()["character_version"] == 1
    msgs = (await c.get(f"/api/v1/conversations/{conv['id']}/messages")).json()["items"]
    assert msgs[0]["content"] == CARD["first_messages"][0]


async def test_publish_requires_review_then_admin_approval(
    clients: ClientFactory, owner_db: asyncpg.Connection
) -> None:
    creator = await adult_client(clients, "publisher")
    other = await adult_client(clients, "browser")
    cid = (await creator.post("/api/v1/characters", json=CARD)).json()["id"]
    r = await creator.post(f"/api/v1/characters/{cid}/publish")
    assert r.json()["moderation_state"] == "pending_review" and r.json()["visibility"] == "private"
    # Pending: invisible in public catalog and by direct id.
    assert cid not in {i["id"] for i in (await other.get("/api/v1/characters")).json()["items"]}
    assert (await other.get(f"/api/v1/characters/{cid}")).status_code == 404
    # Non-admin can't moderate.
    r = await other.post(f"/api/v1/admin/moderation/characters/{cid}", json={"action": "approve", "reason": "lgtm"})
    assert r.status_code == 403
    admin = await adult_client(clients, "aster_curator")
    queue = (await admin.get("/api/v1/admin/moderation/characters")).json()
    assert [q["id"] for q in queue] == [cid]
    r = await admin.post(
        f"/api/v1/admin/moderation/characters/{cid}", json={"action": "approve", "reason": "original, adult"}
    )
    assert r.status_code == 200 and r.json()["visibility"] == "public"
    assert cid in {i["id"] for i in (await other.get("/api/v1/characters")).json()["items"]}
    audits = await owner_db.fetch(
        "SELECT action, reason FROM audit_events WHERE subject_ref = $1 ORDER BY created_at", f"character:{cid}"
    )
    assert [a["action"] for a in audits] == ["character.publish_requested", "character.approve"]
    # Creator edit after approval sends it back to private draft.
    await creator.patch(f"/api/v1/characters/{cid}", json={**CARD, "tagline": "changed"})
    assert (await other.get(f"/api/v1/characters/{cid}")).status_code == 404


async def test_db_constraint_blocks_public_without_approval(
    clients: ClientFactory, owner_db: asyncpg.Connection
) -> None:
    c = await adult_client(clients, "sneaky")
    cid = (await c.post("/api/v1/characters", json=CARD)).json()["id"]
    with pytest.raises(asyncpg.CheckViolationError):
        await owner_db.execute("UPDATE character_definitions SET visibility = 'public' WHERE id = $1", cid)


async def test_delete_own_character_but_not_curated(clients: ClientFactory) -> None:
    c = await adult_client(clients, "deleter")
    cid = (await c.post("/api/v1/characters", json=CARD)).json()["id"]
    assert (await c.delete(f"/api/v1/characters/{cid}")).status_code == 204
    assert (await c.get(f"/api/v1/characters/{cid}")).status_code == 404
    curated = (await c.get("/api/v1/characters")).json()["items"][0]["id"]
    assert (await c.delete(f"/api/v1/characters/{curated}")).status_code == 404  # not the owner
