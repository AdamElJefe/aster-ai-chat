"""The single server-side age-verification gate."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Annotated, Literal

import asyncpg
from fastapi import Depends, Request

from aster_api.age.region import region_allowed
from aster_api.auth.deps import Ctx, User
from aster_api.auth.sessions import CurrentUser
from aster_api.core.config import Settings
from aster_api.core.errors import ApiError

AccessState = Literal["verified", "unverified", "expired", "underage", "region_blocked", "policy_changed"]


@dataclass(frozen=True)
class AdultUser:
    user: CurrentUser
    region: str

    @property
    def id(self) -> uuid.UUID:
        return self.user.id


async def access_state(conn: asyncpg.Connection, settings: Settings, user_id: uuid.UUID, region: str) -> AccessState:
    if not region_allowed(settings, region):
        return "region_blocked"
    row = await conn.fetchrow(
        """
        SELECT age_verified, policy_version, region, expires_at > now() AS live
          FROM age_attestations
         WHERE user_id = $1 AND revoked_at IS NULL
         ORDER BY verified_at DESC
         LIMIT 1
        """,
        user_id,
    )
    if row is None:
        return "unverified"
    if not row["age_verified"]:
        return "underage"
    if not row["live"]:
        return "expired"
    if row["policy_version"] != settings.age_policy_version:
        return "policy_changed"
    if not region_allowed(settings, row["region"]):
        return "region_blocked"
    return "verified"


async def require_adult(request: Request, user: User, ctx: Ctx) -> AdultUser:
    region = ctx.region.resolve(request)
    async with ctx.db.tenant(user.id) as conn:
        state = await access_state(conn, ctx.settings, user.id, region)
    if state != "verified":
        # Deliberately generic: the reason is visible to the user via GET /me, not leaked per route.
        raise ApiError(403, "ADULT_ACCESS_REQUIRED", "adult verification required for this feature")
    return AdultUser(user=user, region=region)


Adult = Annotated[AdultUser, Depends(require_adult)]
