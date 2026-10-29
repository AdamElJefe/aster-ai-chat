"""Uniform error envelope: {"error": {"code", "message", "request_id"}}."""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

log = logging.getLogger("aster.errors")


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def not_found(what: str = "resource") -> ApiError:
    # Same response whether the row is missing or belongs to someone else: no existence oracle.
    return ApiError(404, "NOT_FOUND", f"{what} not found")


def _body(request: Request, code: str, message: str) -> dict[str, object]:
    return {"error": {"code": code, "message": message, "request_id": getattr(request.state, "request_id", None)}}


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(_body(request, exc.code, exc.message), status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        # Field locations only; never echo submitted values (could be sensitive text).
        fields = sorted({".".join(str(p) for p in e.get("loc", ())) for e in exc.errors()})
        return JSONResponse(_body(request, "VALIDATION_ERROR", f"invalid fields: {', '.join(fields)}"), status_code=422)

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = {404: "NOT_FOUND", 405: "METHOD_NOT_ALLOWED"}.get(exc.status_code, "HTTP_ERROR")
        return JSONResponse(_body(request, code, str(exc.detail)), status_code=exc.status_code)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled error", extra={"request_id": getattr(request.state, "request_id", None)})
        return JSONResponse(_body(request, "INTERNAL", "internal error"), status_code=500)
