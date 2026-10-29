from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

Visibility = Literal["private", "unlisted", "public"]
ModerationState = Literal["draft", "pending_review", "approved", "rejected"]


class CharacterCard(BaseModel):
    """Persona semantics only. Visual identity is a separate versioned profile (planned)."""

    display_name: str = Field(min_length=1, max_length=80)
    declared_age: int = Field(ge=18, le=1000, description="Must be 18+; enforced again by a DB CHECK")
    tagline: str = Field(default="", max_length=280, description="Safe-for-general-audiences one-liner")
    description: str = Field(default="", max_length=2000)
    personality: str = Field(default="", max_length=4000)
    speech_style: str = Field(default="", max_length=1000)
    backstory: str = Field(default="", max_length=6000)
    scenario: str = Field(default="", max_length=4000)
    first_messages: list[str] = Field(min_length=1, max_length=5)
    example_dialogue: list[str] = Field(default_factory=list, max_length=10)
    content_tags: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("first_messages", "example_dialogue")
    @classmethod
    def _bounded_items(cls, v: list[str]) -> list[str]:
        for item in v:
            if not item.strip() or len(item) > 2000:
                raise ValueError("each entry must be 1-2000 characters")
        return v

    @field_validator("content_tags")
    @classmethod
    def _tags(cls, v: list[str]) -> list[str]:
        cleaned = sorted({t.strip().lower() for t in v if t.strip()})
        if any(len(t) > 32 for t in cleaned):
            raise ValueError("tags must be <= 32 characters")
        return cleaned


class CharacterPreview(BaseModel):
    """Shown before the adult gate: name, tagline, tags. No scenario, greeting or dialogue."""

    id: uuid.UUID
    display_name: str
    tagline: str
    tags: list[str]
    is_curated: bool
    ai_generated_fiction: bool = True


class CharacterDetail(BaseModel):
    id: uuid.UUID
    version: int
    is_owner: bool
    is_curated: bool
    visibility: Visibility
    moderation_state: ModerationState
    tags: list[str]
    card: CharacterCard
    created_at: datetime
    updated_at: datetime


class CharacterView(BaseModel):
    """Detail when the viewer has adult access; otherwise only the preview."""

    preview: CharacterPreview
    detail: CharacterDetail | None = None


class CharacterPage(BaseModel):
    items: list[CharacterPreview]
    next_cursor: str | None


class ModerateRequest(BaseModel):
    action: Literal["approve", "reject"]
    visibility: Visibility = "public"
    reason: str = Field(min_length=3, max_length=500)
