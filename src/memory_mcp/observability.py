"""Structured observability surface — M-012 ObservabilityService."""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

logger = logging.getLogger(__name__)

REDACTED_KEY_PATTERNS = {"api_key", "key", "secret", "token", "password", "credential"}


class ErrorCode(StrEnum):
    AUTH_INVALID_KEY = "AUTH_INVALID_KEY"
    AUTH_UNKNOWN_KEY = "AUTH_UNKNOWN_KEY"
    POLICY_DENIED = "POLICY_DENIED"
    FS_PATH_TRAVERSAL = "FS_PATH_TRAVERSAL"
    FS_SYMLINK_DENIED = "FS_SYMLINK_DENIED"
    PATCH_INVALID = "PATCH_INVALID"
    PATCH_UNSUPPORTED = "PATCH_UNSUPPORTED"
    REVISION_MISMATCH = "REVISION_MISMATCH"
    LOCK_TIMEOUT = "LOCK_TIMEOUT"
    AUDIT_WRITE_FAILED = "AUDIT_WRITE_FAILED"
    INDEX_CORRUPT = "INDEX_CORRUPT"
    CONFIG_INVALID = "CONFIG_INVALID"


def new_trace_id() -> str:
    return str(uuid.uuid4())


def _redact_data(data: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in data.items():
        if key.lower() in REDACTED_KEY_PATTERNS and isinstance(value, str):
            if len(value) > 4:
                result[key] = f"{value[:4]}...{value[-4:]}"
            else:
                result[key] = "<redacted>"
        else:
            result[key] = value
    return result


def log_trace_anchor(
    level: str,
    event: str,
    trace_id: str,
    module: str,
    function: str,
    block: str = "",
    data: dict[str, Any] | None = None,
    error_code: str | None = None,
) -> None:
    entry: dict[str, Any] = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "level": level,
        "trace_id": trace_id,
        "module": module,
        "function": function,
        "block": block,
        "event": event,
    }
    if error_code:
        entry["error_code"] = error_code
    if data is not None:
        entry["data"] = _redact_data(data)

    level_int = getattr(logging, level, logging.INFO)
    logger.log(level_int, json.dumps(entry))
