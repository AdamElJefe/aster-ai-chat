"""Request dependencies: context, current user, CSRF, admin role."""

from __future__ import annotations

import secrets
from typing import Annotated

from fastapi import Depends, Request

from aster_api.auth.sessions import SESSION_COOKIE, CurrentUser, resolve_session
from aster_api.core.context import AppContext
from aster_api.core.errors import ApiError

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def get_ctx(request: Request) -> AppContext:
    ctx: AppContext = request.app.state.ctx
    return ctx


Ctx = Annotated[AppContext, Depends(get_ctx)]


async def current_user(request: Request, ctx: Ctx) -> CurrentUser:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        raise ApiError(401, "UNAUTHENTICATED", "sign in required")
    async with ctx.db.system() as conn:
        user = await resolve_session(conn, token, ctx.settings.session_pepper)
    if user is None:
        raise ApiError(401, "UNAUTHENTICATED", "session expired or invalid")
    # CSRF: double-submit token bound to the session, required on every mutating request.
    if request.method not in SAFE_METHODS:
        sent = request.headers.get("x-csrf-token", "")
        if not secrets.compare_digest(sent, user.csrf_token):
            raise ApiError(403, "CSRF_FAILED", "missing or invalid CSRF token")
    request.state.user_id = user.id
    return user


User = Annotated[CurrentUser, Depends(current_user)]


async def optional_user(request: Request, ctx: Ctx) -> CurrentUser | None:
    if not request.cookies.get(SESSION_COOKIE):
        return None
    try:
        return await current_user(request, ctx)
    except ApiError:
        return None


MaybeUser = Annotated[CurrentUser | None, Depends(optional_user)]


async def require_admin(user: User) -> CurrentUser:
    if user.role != "admin":
        raise ApiError(403, "FORBIDDEN", "admin role required")
    return user


Admin = Annotated[CurrentUser, Depends(require_admin)]
