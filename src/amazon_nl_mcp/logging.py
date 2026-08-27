"""Structured logging with redaction.

The service handles a signed-in Amazon account. Names, delivery addresses, order
history and the bearer token must never reach journald, so every event passes
through :func:`_redact` before it is rendered.
"""

from __future__ import annotations

import logging
import re
import sys
from collections.abc import MutableMapping
from typing import Any

import structlog

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_BEARER = re.compile(r"(?i)(bearer\s+)[\w\-._~+/]+=*")
_GREETING = re.compile(r"(?i)\b(hallo|hello|hi)[, ]+([^\s,][^,]{0,40})")
_SENSITIVE_KEYS = frozenset(
    {"auth_token", "token", "authorization", "password", "cookie", "cookies", "account_name", "email"}
)

_REDACTED = "[redacted]"


def _scrub_text(value: str) -> str:
    value = _EMAIL.sub(_REDACTED, value)
    value = _BEARER.sub(rf"\1{_REDACTED}", value)
    return _GREETING.sub(rf"\1 {_REDACTED}", value)


def _redact(_logger: Any, _name: str, event: MutableMapping[str, Any]) -> MutableMapping[str, Any]:
    for key, value in list(event.items()):
        if key.lower() in _SENSITIVE_KEYS:
            event[key] = _REDACTED
        elif isinstance(value, str):
            event[key] = _scrub_text(value)
    return event


def configure_logging(level: str = "INFO", fmt: str = "console") -> None:
    """Install the structlog pipeline over stdlib logging (journald reads stderr)."""
    logging.basicConfig(format="%(message)s", stream=sys.stderr, level=level.upper(), force=True)
    # Playwright is chatty at DEBUG and its records can carry page text.
    logging.getLogger("playwright").setLevel(max(logging.INFO, logging.getLevelName(level.upper())))

    renderer: structlog.types.Processor = (
        structlog.processors.JSONRenderer() if fmt == "json" else structlog.dev.ConsoleRenderer()
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _redact,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(level.upper())),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)  # type: ignore[no-any-return]
