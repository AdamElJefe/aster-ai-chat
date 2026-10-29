"""Age-verification provider interface and the dev/test mock.

The mock imitates the common "signed result" pattern: the provider signs an outcome bound to our
verification session id; our server verifies signature, expiry, binding and single use.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import uuid
from dataclasses import dataclass
from typing import Literal, Protocol

from aster_api.core.config import Settings


class AgeVerificationError(Exception):
    """Raised for any invalid provider result. Callers map it to a generic denial."""


@dataclass(frozen=True)
class VerifiedResult:
    session_ref: uuid.UUID
    is_adult: bool
    attestation_ref: str


class AgeVerificationProvider(Protocol):
    name: str

    def start_url(self, session_ref: uuid.UUID) -> str: ...

    def verify(self, token: str) -> VerifiedResult: ...


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


class MockAgeProvider:
    """Dev/test only. Never constructed in staging/prod (Settings guard)."""

    name = "mock_age"
    token_ttl_seconds = 600

    def __init__(self, secret: str) -> None:
        self._key = secret.encode()

    def start_url(self, session_ref: uuid.UUID) -> str:
        return f"/verify/mock-provider?session_ref={session_ref}"

    # Called only by the dev-only "provider" endpoint to simulate the vendor's signed result.
    def issue(self, session_ref: uuid.UUID, outcome: Literal["adult", "underage"], now: float | None = None) -> str:
        now = time.time() if now is None else now
        payload = {
            "sid": str(session_ref),
            "adult": outcome == "adult",
            "ref": f"mock-{uuid.uuid4()}",
            "iat": int(now),
            "exp": int(now) + self.token_ttl_seconds,
        }
        body = _b64(json.dumps(payload, separators=(",", ":")).encode())
        sig = _b64(hmac.new(self._key, body.encode(), hashlib.sha256).digest())
        return f"{body}.{sig}"

    def verify(self, token: str) -> VerifiedResult:
        try:
            body, sig = token.split(".", 1)
            expected = _b64(hmac.new(self._key, body.encode(), hashlib.sha256).digest())
            if not hmac.compare_digest(sig, expected):
                raise AgeVerificationError("bad signature")
            payload = json.loads(_unb64(body))
            if int(payload["exp"]) < time.time():
                raise AgeVerificationError("expired")
            return VerifiedResult(
                session_ref=uuid.UUID(payload["sid"]),
                is_adult=bool(payload["adult"]),
                attestation_ref=str(payload["ref"]),
            )
        except AgeVerificationError:
            raise
        except Exception as exc:  # malformed token of any kind
            raise AgeVerificationError("malformed") from exc


def build_age_provider(settings: Settings) -> AgeVerificationProvider:
    if settings.age_provider == "mock_age":
        return MockAgeProvider(settings.mock_age_signing_secret)
    raise ValueError(f"no age provider adapter named {settings.age_provider!r}")
