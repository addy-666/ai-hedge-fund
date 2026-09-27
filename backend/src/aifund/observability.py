"""Structured logging: JSON lines, UTC timestamps, correlation ids, secret redaction.

Redaction works two ways:
1. by key — any field whose name looks like a credential is replaced, at any nesting depth;
2. by value — the actual secret strings from Settings are registered at startup and scrubbed from every
   string in the event (messages, exception text, nested payloads), so a key that leaks into an error
   message from a third-party library is still removed.
"""

from __future__ import annotations

import logging
import re
import sys
from collections.abc import Iterator, Mapping, MutableMapping
from contextlib import contextmanager
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import IO, Any

import structlog
from pydantic import SecretStr

from aifund.config.settings import Settings

REDACTED = "***REDACTED***"
_SENSITIVE_KEY = re.compile(
    r"(pass(word)?|secret|token|api[_-]?key|authorization|auth|cookie|credential)", re.I
)
_MIN_SECRET_LEN = 6
_secrets: set[str] = set()


def register_secret(value: str | SecretStr | None) -> None:
    """Scrub this exact string from all future log output."""
    if value is None:
        return
    raw = value.get_secret_value() if isinstance(value, SecretStr) else value
    if len(raw) >= _MIN_SECRET_LEN:
        _secrets.add(raw)


def register_settings_secrets(settings: Settings) -> None:
    for name, field in type(settings).model_fields.items():
        if field.annotation is not None and "SecretStr" in str(field.annotation):
            register_secret(getattr(settings, name))


def clear_registered_secrets() -> None:
    _secrets.clear()


def _scrub_str(text: str) -> str:
    for secret in _secrets:
        if secret in text:
            text = text.replace(secret, REDACTED)
    return text


def _redact(value: Any, key: str | None = None) -> Any:
    if key is not None and _SENSITIVE_KEY.search(key):
        return REDACTED
    if isinstance(value, SecretStr):
        return REDACTED
    if isinstance(value, str):
        return _scrub_str(value)
    if isinstance(value, Mapping):
        return {k: _redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, list | tuple | set):
        return [_redact(v) for v in value]
    return value


def redact_processor(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    for k in list(event_dict):
        event_dict[k] = _redact(event_dict[k], None if k == "event" else k)
    return event_dict


def _shared_processors() -> list[structlog.typing.Processor]:
    return [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        redact_processor,  # last before rendering: also scrubs rendered exception text
    ]


def configure_logging(
    component: str,
    *,
    log_dir: Path | None = None,
    level: str = "INFO",
    stream: IO[str] | None = None,
) -> None:
    """Configure stdlib logging + structlog to emit redacted JSON lines to ``stream`` (default stderr)
    and, if ``log_dir`` is given, to a daily-rotated ``<component>.jsonl`` kept for 30 days."""
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=_shared_processors(),
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.JSONRenderer(sort_keys=False),
        ],
    )
    handlers: list[logging.Handler] = [logging.StreamHandler(stream or sys.stderr)]
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        handlers.append(
            TimedRotatingFileHandler(
                log_dir / f"{component}.jsonl", when="midnight", utc=True, backupCount=30, encoding="utf-8"
            )
        )
    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    for handler in handlers:
        handler.setFormatter(formatter)
        root.addHandler(handler)
    root.setLevel(level)

    structlog.configure(
        processors=[*_shared_processors(), structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=False,
    )
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(component=component)


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    return logger


@contextmanager
def correlation(**ids: str | int | None) -> Iterator[None]:
    """Bind correlation ids (decision_id, intent_id, position_id, ...) for everything logged inside."""
    present = {k: v for k, v in ids.items() if v is not None}
    with structlog.contextvars.bound_contextvars(**present):
        yield
