from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

MessageState = Literal["pending", "streaming", "completed", "failed", "blocked"]


class CreateConversationRequest(BaseModel):
    character_id: uuid.UUID
    character_version: int | None = Field(None, ge=1, description="Defaults to the character's current version")
    title: str = Field("", max_length=120)
    style_preset: Literal["balanced", "playful_short", "descriptive"] = "balanced"


class UpdateConversationRequest(BaseModel):
    title: str | None = Field(None, max_length=120)
    status: Literal["active", "archived"] | None = None


class ConversationOut(BaseModel):
    id: uuid.UUID
    character_id: uuid.UUID
    character_version: int
    character_name: str
    title: str
    status: Literal["active", "archived"]
    created_at: datetime
    last_message_at: datetime


class ConversationPage(BaseModel):
    items: list[ConversationOut]
    next_cursor: str | None


class MessageOut(BaseModel):
    id: uuid.UUID
    turn_id: uuid.UUID
    ordinal: int
    role: Literal["user", "assistant"]
    variant: int
    variant_count: int
    state: MessageState
    content: str | None = Field(None, description="Present only when state is completed")
    created_at: datetime
    request_id: uuid.UUID | None = Field(None, description="Generation that produced this assistant message")


class MessagePage(BaseModel):
    items: list[MessageOut]
    next_before: int | None = Field(None, description="Pass as ?before= to load older turns")


class SendMessageRequest(BaseModel):
    content: str = Field(min_length=1, max_length=20000)
    client_message_id: uuid.UUID | None = None


class GenerationAccepted(BaseModel):
    request_id: uuid.UUID
    conversation_id: uuid.UUID
    turn_id: uuid.UUID
    user_message_id: uuid.UUID | None
    assistant_message_id: uuid.UUID
    events_url: str


class ExportMessage(BaseModel):
    ordinal: int
    role: str
    variant: int
    active: bool
    state: str
    content: str | None
    created_at: datetime


class ConversationExport(BaseModel):
    format: Literal["aster.conversation.v1"] = "aster.conversation.v1"
    exported_at: datetime
    conversation: ConversationOut
    character_card: dict[str, Any]
    messages: list[ExportMessage]
