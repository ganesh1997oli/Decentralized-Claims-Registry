"""Small, dependency-free structured logging boundary for runtime services.

The JSON schema stays stable so operators, log sinks, and
alerts should search fields such as ``event_id`` and ``claim_id`` without
parsing human prose. Callers provide an event name and structured values; this
module owns serialization and defensive redaction.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, TextIO

_SENSITIVE_KEY_PARTS = (
    "api_key",
    "authorization",
    "credential",
    "password",
    "private_key",
    "secret",
    "token",
)
_URL_CREDENTIALS = re.compile(r"(://[^:/\s]+:)[^@/\s]+(@)")
_BEARER_TOKEN = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+")
_SECRET_QUERY_VALUE = re.compile(
    r"(?i)([?&][^=&]*(?:api[_-]?key|password|secret|signature|token)[^=&]*=)[^&#\s]+"
)
_REDACTED = "[REDACTED]"


def _is_sensitive_key(key: str) -> bool:
    normalized = key.lower().replace("-", "_")
    return any(part in normalized for part in _SENSITIVE_KEY_PARTS)


def _sanitize(value: Any, *, key: str = "") -> Any:
    """Return a JSON-safe value while removing common credential shapes."""

    if _is_sensitive_key(key):
        return _REDACTED
    if isinstance(value, Mapping):
        return {
            str(item_key): _sanitize(item, key=str(item_key))
            for item_key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_sanitize(item) for item in value]
    if isinstance(value, str):
        without_url_password = _URL_CREDENTIALS.sub(r"\1[REDACTED]\2", value)
        without_query_secrets = _SECRET_QUERY_VALUE.sub(
            r"\1[REDACTED]", without_url_password
        )
        return _BEARER_TOKEN.sub(r"\1[REDACTED]", without_query_secrets)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)


class JsonLogFormatter(logging.Formatter):
    """Serialize one log record into one ingestion-friendly JSON object."""

    def __init__(self, service: str) -> None:
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        event = getattr(record, "event_name", "application.log")
        fields = getattr(record, "event_fields", {})
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "severity": record.levelname,
            "service": self.service,
            "logger": record.name,
            "event": event,
            "message": _sanitize(record.getMessage()),
        }
        if isinstance(fields, Mapping):
            payload.update(_sanitize(fields))
        if record.exc_info:
            # Exception text can contain URLs and adapter configuration. Record
            # the class for grouping while keeping details out of the log sink.
            payload["exception_type"] = record.exc_info[0].__name__
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def configure_logging(
    service: str,
    *,
    level: str | int | None = None,
    stream: TextIO | None = None,
) -> None:
    """Configure process-wide JSON logs once at a service entry point."""

    configured_level = level or os.environ.get("LOG_LEVEL", "INFO").upper()
    handler = logging.StreamHandler(stream or sys.stdout)
    handler.setFormatter(JsonLogFormatter(service))
    logging.basicConfig(
        level=configured_level,
        handlers=[handler],
        force=True,
    )


class EventLogger:
    """Structured logger that requires stable event names."""

    def __init__(self, name: str) -> None:
        self._logger = logging.getLogger(name)

    def _log(self, level: int, event: str, fields: Mapping[str, Any]) -> None:
        self._logger.log(
            level,
            event,
            extra={"event_name": event, "event_fields": dict(fields)},
        )

    def debug(self, event: str, **fields: Any) -> None:
        self._log(logging.DEBUG, event, fields)

    def info(self, event: str, **fields: Any) -> None:
        self._log(logging.INFO, event, fields)

    def warning(self, event: str, **fields: Any) -> None:
        self._log(logging.WARNING, event, fields)

    def error(self, event: str, **fields: Any) -> None:
        self._log(logging.ERROR, event, fields)


def get_event_logger(name: str) -> EventLogger:
    """Create a structured event logger for a module."""

    return EventLogger(name)
