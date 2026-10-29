"""Structured JSON logging with a redaction filter (no prompt or chat text in logs)."""

from __future__ import annotations

import json
import logging
import sys
from typing import Any

SENSITIVE_KEYS = frozenset(
    {
        "content",
        "prompt",
        "text",
        "message_text",
        "card",
        "scenario",
        "personality",
        "backstory",
        "first_messages",
        "example_dialogue",
        "token",
        "session",
        "cookie",
        "authorization",
        "csrf_token",
        "api_key",
        "password",
        "email",
        "attestation_token",
    }
)
_STD_ATTRS = set(vars(logging.makeLogRecord({})).keys()) | {"message", "asctime"}


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: ("[redacted]" if k.lower() in SENSITIVE_KEYS else redact(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        extra = {k: v for k, v in vars(record).items() if k not in _STD_ATTRS}
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            **redact(extra),
        }
        if record.exc_info:
            payload["exc_type"] = record.exc_info[0].__name__ if record.exc_info[0] else None
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    # Access logs include query strings; keep them off. Our middleware logs route + status instead.
    logging.getLogger("uvicorn.access").disabled = True
