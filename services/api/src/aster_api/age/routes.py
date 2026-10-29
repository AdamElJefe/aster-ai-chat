from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from aster_api.age.providers import AgeVerificationError, MockAgeProvider
from aster_api.age.region import region_allowed
from aster_api.auth.deps import Ctx, User
from aster_api.core.audit import audit
from aster_api.core.errors import ApiError

router = APIRouter(prefix="/api/v1", tags=["age"])

SESSION_TTL = timedelta(minutes=15)


class AgeStartResponse(BaseModel):
    session_ref: uuid.UUID
    provider: str
    redirect_url: str


class AgeCallbackRequest(BaseModel):
    token: str = Field(min_length=10, max_length=4096)


class AgeCallbackResponse(BaseModel):
    adult_access: bool


@router.post("/age/start", response_model=AgeStartResponse)
async def age_start(request: Request, user: User, ctx: Ctx) -> AgeStartResponse:
    region = ctx.region.resolve(request)
    if not region_allowed(ctx.settings, region):
        raise ApiError(403, "REGION_UNAVAILABLE", "the service is not available in your region")
    async with ctx.db.tenant(user.id) as conn:
        ref = await conn.fetchval(
            "INSERT INTO age_verification_sessions (user_id, provider, region, expires_at)"
            " VALUES ($1, $2, $3, $4) RETURNING id",
            user.id,
            ctx.age_provider.name,
            region,
            datetime.now(UTC) + SESSION_TTL,
        )
    return AgeStartResponse(
        session_ref=ref, provider=ctx.age_provider.name, redirect_url=ctx.age_provider.start_url(ref)
    )


@router.post("/age/callback", response_model=AgeCallbackResponse)
async def age_callback(body: AgeCallbackRequest, request: Request, user: User, ctx: Ctx) -> AgeCallbackResponse:
    denied = ApiError(400, "VERIFICATION_FAILED", "verification could not be completed")
    try:
        result = ctx.age_provider.verify(body.token)
    except AgeVerificationError:
        raise denied from None
    async with ctx.db.tenant(user.id) as conn:
        # RLS + user_id check: a token for someone else's session can't be redeemed here.
        sess = await conn.fetchrow(
            "UPDATE age_verification_sessions SET used_at = now()"
            " WHERE id = $1 AND user_id = $2 AND used_at IS NULL AND expires_at > now()"
            " RETURNING region",
            result.session_ref,
            user.id,
        )
        if sess is None:
            raise denied
        await conn.execute(
            "INSERT INTO age_attestations (user_id, provider, attestation_ref, age_verified, region,"
            " policy_version, expires_at) VALUES ($1, $2, $3, $4, $5, $6, $7)",
            user.id,
            ctx.age_provider.name,
            result.attestation_ref,
            result.is_adult,
            sess["region"],
            ctx.settings.age_policy_version,
            datetime.now(UTC) + timedelta(days=ctx.settings.age_attestation_ttl_days),
        )
        await audit(conn, user.id, "age.attestation_recorded", f"user:{user.id}", attrs={"adult": result.is_adult})
    return AgeCallbackResponse(adult_access=result.is_adult and region_allowed(ctx.settings, sess["region"]))


# --- Dev-only stand-in for the external provider's UI/backend --------------------------------


class MockDecideRequest(BaseModel):
    session_ref: uuid.UUID
    outcome: Literal["adult", "underage"]


class MockDecideResponse(BaseModel):
    token: str


@router.post("/dev/mock-age-provider/decide", response_model=MockDecideResponse, tags=["dev"])
async def mock_provider_decide(body: MockDecideRequest, user: User, ctx: Ctx) -> MockDecideResponse:
    if not ctx.settings.is_dev_like or not isinstance(ctx.age_provider, MockAgeProvider):
        raise ApiError(404, "NOT_FOUND", "not found")
    return MockDecideResponse(token=ctx.age_provider.issue(body.session_ref, body.outcome))
