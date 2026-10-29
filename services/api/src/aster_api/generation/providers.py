"""Text inference adapters. Domain code sees only these types, never vendor objects."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Protocol

import httpx

from aster_api.core.config import Settings

OnDelta = Callable[[str], Awaitable[None]]


@dataclass(frozen=True)
class ChatMessage:
    role: str  # "system" | "user" | "assistant"
    content: str


@dataclass(frozen=True)
class GenerationRequest:
    model_id: str
    messages: list[ChatMessage]
    max_output_tokens: int
    correlation_id: str
    temperature: float = 0.8
    deadline_seconds: float = 120.0
    character_name: str = ""  # used by the mock only


@dataclass
class GenerationResult:
    text: str
    prompt_tokens: int
    completion_tokens: int
    finish_reason: str
    model_id: str
    backend: str
    usage_estimated: bool = False
    phases_ms: dict[str, int] = field(default_factory=dict)


class ProviderError(Exception):
    def __init__(self, code: str, retryable: bool) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable


class TextInferenceProvider(Protocol):
    name: str

    async def stream(self, req: GenerationRequest, on_delta: OnDelta) -> GenerationResult: ...

    async def aclose(self) -> None: ...


def estimate_tokens(text: str) -> int:
    """Rough budget estimate (~4 chars/token). Real counts come from the backend when available."""
    return max(1, len(text) // 4)


class MockTextProvider:
    """Deterministic, content-neutral replies for dev/CI. Never used outside dev/test.

    Failure injection (dev/test only): a user message containing [[mock:fail]] fails permanently;
    [[mock:flaky]] fails with a retryable error on the first attempt of each request.
    """

    name = "mock_text"
    _OPENERS = (
        "{name} tilts their head, thinking about that.",
        "{name} laughs softly.",
        "{name} leans back with a thoughtful look.",
        "{name} smiles and takes a moment before answering.",
    )
    _FOLLOW_UPS = (
        "Tell me more about that.",
        "What made you think of it?",
        "I want the whole story.",
        "And how did that go?",
    )

    def __init__(self, token_delay_ms: int) -> None:
        self._delay = token_delay_ms / 1000
        self._flaky_seen: set[str] = set()

    async def stream(self, req: GenerationRequest, on_delta: OnDelta) -> GenerationResult:
        last_user = next((m.content for m in reversed(req.messages) if m.role == "user"), "")
        if "[[mock:fail]]" in last_user:
            raise ProviderError("MOCK_FAILURE", retryable=False)
        if "[[mock:flaky]]" in last_user and req.correlation_id not in self._flaky_seen:
            self._flaky_seen.add(req.correlation_id)
            raise ProviderError("MOCK_UNAVAILABLE", retryable=True)

        digest = int(hashlib.sha256((req.correlation_id + last_user).encode()).hexdigest(), 16)
        opener = self._OPENERS[digest % len(self._OPENERS)].format(name=req.character_name or "They")
        topic = " ".join(last_user.split()[:8])
        follow_up = self._FOLLOW_UPS[(digest // 7) % len(self._FOLLOW_UPS)]
        text = f"*{opener}* “{topic}”, huh? {follow_up}"
        words = text.split(" ")
        for i, word in enumerate(words):
            await on_delta(word if i == 0 else " " + word)
            if self._delay:
                await asyncio.sleep(self._delay)
        prompt_tokens = sum(estimate_tokens(m.content) for m in req.messages)
        return GenerationResult(
            text=text,
            prompt_tokens=prompt_tokens,
            completion_tokens=estimate_tokens(text),
            finish_reason="stop",
            model_id=req.model_id,
            backend="mock",
            usage_estimated=True,
        )

    async def aclose(self) -> None:
        return None


class OpenAICompatibleProvider:
    """Streams from an OpenAI-compatible endpoint (e.g. vLLM) on a private network.
    Not exercised in CI (no model). Point TEXT_BASE_URL at vLLM, Ollama or a hosted API."""

    name = "openai_compat"

    def __init__(self, base_url: str, api_key: str) -> None:
        self._client = httpx.AsyncClient(base_url=base_url, headers={"Authorization": f"Bearer {api_key}"})

    async def stream(self, req: GenerationRequest, on_delta: OnDelta) -> GenerationResult:
        body = {
            "model": req.model_id,
            "messages": [{"role": m.role, "content": m.content} for m in req.messages],
            "max_tokens": req.max_output_tokens,
            "temperature": req.temperature,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        parts: list[str] = []
        finish = "unknown"
        usage: dict[str, int] = {}
        try:
            async with asyncio.timeout(req.deadline_seconds):
                async with self._client.stream("POST", "/v1/chat/completions", json=body) as resp:
                    if resp.status_code >= 500 or resp.status_code == 429:
                        raise ProviderError(f"UPSTREAM_{resp.status_code}", retryable=True)
                    if resp.status_code >= 400:
                        raise ProviderError(f"UPSTREAM_{resp.status_code}", retryable=False)
                    async for line in resp.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            break
                        chunk = json.loads(data)
                        if chunk.get("usage"):
                            usage = chunk["usage"]
                        for choice in chunk.get("choices", []):
                            delta = choice.get("delta", {}).get("content")
                            if delta:
                                parts.append(delta)
                                await on_delta(delta)
                            if choice.get("finish_reason"):
                                finish = choice["finish_reason"]
        except TimeoutError as exc:
            raise ProviderError("DEADLINE_EXCEEDED", retryable=True) from exc
        except httpx.TransportError as exc:
            raise ProviderError("UPSTREAM_UNREACHABLE", retryable=True) from exc
        text = "".join(parts)
        return GenerationResult(
            text=text,
            prompt_tokens=int(usage.get("prompt_tokens", 0)) or sum(estimate_tokens(m.content) for m in req.messages),
            completion_tokens=int(usage.get("completion_tokens", 0)) or estimate_tokens(text),
            finish_reason=finish,
            model_id=req.model_id,
            backend="openai_compat",
            usage_estimated=not usage,
        )

    async def aclose(self) -> None:
        await self._client.aclose()


def build_text_provider(settings: Settings) -> TextInferenceProvider:
    if settings.text_provider == "mock_text":
        return MockTextProvider(settings.mock_token_delay_ms)
    if settings.text_provider == "openai_compat":
        if not settings.text_base_url:
            raise ValueError("TEXT_BASE_URL required for openai_compat")
        return OpenAICompatibleProvider(settings.text_base_url, settings.text_api_key)
    raise ValueError(f"unknown text provider {settings.text_provider!r}")
