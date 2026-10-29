"""Typed settings loaded from the environment.

Startup guards live here: staging/prod refuse mock adapters and unapproved ones.
"""

from __future__ import annotations

import base64
import os
from functools import lru_cache
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

AppEnv = Literal["dev", "test", "staging", "prod"]
MOCK_ADAPTERS = {"mock_age", "mock_text", "dev_auth", "header_region"}


class Settings(BaseModel):
    app_env: AppEnv = "dev"
    app_name: str = "Aster"
    database_url: str = "postgresql://aster_app:aster_app_dev@localhost:5432/aster"
    database_owner_url: str = "postgresql://aster_owner:aster_owner_dev@localhost:5432/aster"
    redis_url: str = "redis://localhost:6379/0"

    # Adapters
    auth_provider: str = "dev_auth"
    age_provider: str = "mock_age"
    region_resolver: str = "header_region"
    text_provider: str = "mock_text"
    approved_adapters: list[str] = Field(default_factory=list)

    # Secrets (never defaults outside dev/test; see validate_secrets)
    data_keys: str = ""  # "k1:<base64 32 bytes>,k0:<...>"
    active_data_key_id: str = "k1"
    session_pepper: str = ""
    mock_age_signing_secret: str = ""

    # Policy
    allowed_regions: list[str] = Field(default_factory=list)
    age_policy_version: str = "2026-10-dev"
    age_attestation_ttl_days: int = 365
    session_ttl_hours: int = 24 * 14
    cookie_secure: bool = True

    # Text model
    text_model_id: str = "mock-companion-v0"
    text_base_url: str = ""
    text_api_key: str = ""
    max_context_tokens: int = 8192
    max_output_tokens: int = 512
    mock_token_delay_ms: int = 25

    # Worker
    worker_lease_seconds: int = 120
    worker_max_attempts: int = 3
    worker_poll_interval_ms: int = 250
    stream_ttl_seconds: int = 3600

    # Rate limits
    send_rate_per_minute: int = 20

    @field_validator("approved_adapters", "allowed_regions", mode="before")
    @classmethod
    def _split_csv(cls, v: object) -> object:
        if isinstance(v, str):
            return [s.strip() for s in v.split(",") if s.strip()]
        return v

    @model_validator(mode="after")
    def _guard_environment(self) -> Settings:
        if self.app_env in ("staging", "prod"):
            selected = {self.auth_provider, self.age_provider, self.region_resolver, self.text_provider}
            mocks = selected & MOCK_ADAPTERS
            if mocks:
                raise ValueError(f"mock adapters are not allowed in {self.app_env}: {sorted(mocks)}")
            unapproved = selected - set(self.approved_adapters)
            if unapproved:
                raise ValueError(f"adapters not in APPROVED_ADAPTERS: {sorted(unapproved)}")
            if not self.cookie_secure:
                raise ValueError("cookie_secure must be true outside dev/test")
        for name in ("data_keys", "session_pepper"):
            if not getattr(self, name):
                raise ValueError(f"{name.upper()} must be set")
        if self.age_provider == "mock_age" and not self.mock_age_signing_secret:
            raise ValueError("MOCK_AGE_SIGNING_SECRET must be set when using mock_age")
        return self

    def data_key_ring(self) -> dict[str, bytes]:
        ring: dict[str, bytes] = {}
        for part in self.data_keys.split(","):
            kid, _, b64 = part.strip().partition(":")
            key = base64.b64decode(b64)
            if len(key) != 32:
                raise ValueError(f"data key {kid} must be 32 bytes")
            ring[kid] = key
        if self.active_data_key_id not in ring:
            raise ValueError("ACTIVE_DATA_KEY_ID not present in DATA_KEYS")
        return ring

    @property
    def is_dev_like(self) -> bool:
        return self.app_env in ("dev", "test")


def settings_from_env(env: dict[str, str] | None = None) -> Settings:
    source = os.environ if env is None else env
    fields = Settings.model_fields
    values = {name: source[name.upper()] for name in fields if name.upper() in source}
    return Settings(**values)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return settings_from_env()
