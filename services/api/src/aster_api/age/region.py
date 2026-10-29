"""Region resolution. Dev trusts an X-Dev-Region header; production must use a CDN header verified
by shared secret (adapter not built yet)."""

from __future__ import annotations

from typing import Protocol

from fastapi import Request

from aster_api.core.config import Settings

UNKNOWN_REGION = "UNKNOWN"


class RegionResolver(Protocol):
    def resolve(self, request: Request) -> str: ...


class HeaderRegionResolver:
    def __init__(self, default: str) -> None:
        self._default = default

    def resolve(self, request: Request) -> str:
        value = request.headers.get("x-dev-region", self._default).strip().upper()
        return value if 2 <= len(value) <= 16 else UNKNOWN_REGION


def build_region_resolver(settings: Settings) -> RegionResolver:
    if settings.region_resolver == "header_region":
        default = settings.allowed_regions[0] if settings.allowed_regions else UNKNOWN_REGION
        return HeaderRegionResolver(default)
    raise ValueError(f"no region resolver named {settings.region_resolver!r}")


def region_allowed(settings: Settings, region: str) -> bool:
    return region != UNKNOWN_REGION and region in settings.allowed_regions
