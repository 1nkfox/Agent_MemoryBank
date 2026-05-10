"""Process event log service — M-025 ProcessEventLogService."""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from memory_mcp.observability import log_trace_anchor, new_trace_id

MODULE = "process_event_log"
MODULE_BLOCK = "M-025"
BUSY_TIMEOUT_MS = 5000


@dataclass
class ProcessEvent:
    event_type: str
    category: str
    severity: str = "INFO"
    agent_id: str = ""
    admin_user: str = ""
    trace_id: str = ""
    path: str = ""
    status: str = ""
    error_code: str = ""
    message: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    timestamp: str = ""


@dataclass
class ProcessEventFilter:
    from_timestamp: str | None = None
    to_timestamp: str | None = None
    severity: str | None = None
    category: str | None = None
    event_type: str | None = None
    agent_id: str | None = None
    admin_user: str | None = None
    path_prefix: str | None = None
    trace_id: str | None = None
    status: str | None = None
    error_code: str | None = None


_REDACT_PATTERNS: list[tuple[str, str]] = [
    (r'"(api[_-]?key|apikey)"\s*:\s*"[^"]{4,}"', r'"\1": "[redacted:api_key]"'),
    (r'"(password|passwd|pwd)"\s*:\s*"[^"]+"', r'"\1": "[redacted:password]"'),
    (r'"(authorization|auth)"\s*:\s*"[^"]+"', r'"\1": "[redacted:auth]"'),
    (r'"(note_content|patch_body|body)"\s*:\s*"[^"]{50,}"', r'"\1": "[redacted:content]"'),
    (r'"(secret|token|credential)"\s*:\s*"[^"]{4,}"', r'"\1": "[redacted:secret]"'),
    (r'"(content)"\s*:\s*"[^"]{200,}"', r'"\1": "[redacted:content]"'),
    (r'(?:api[_-]?key|apikey)\s*[:=]\s*["\']?[\w\-\.]{8,}["\']?', "[api_key redacted]"),
    (r'(?:authorization|auth)\s*[:=]\s*["\']?[\w\-\.]{8,}["\']?', "[auth_header redacted]"),
    (r'(?:password|passwd|pwd)\s*[:=]\s*["\']?[^\s"\'\]\}]{1,}["\']?', "[password redacted]"),
    (r'(?:bearer\s+)[\w\-\.]{20,}', "[bearer_token redacted]"),
]

_DENYLIST_PATTERNS: list[tuple[str, str]] = [
    (r'-----BEGIN\s+(?:RSA\s+)?PRIVATE\s+KEY-----[\s\S]*?-----END\s+(?:RSA\s+)?PRIVATE\s+KEY-----', "[private_key redacted]"),
    (r'ghp_[a-zA-Z0-9]{36}', "[github_token redacted]"),
    (r'sk-[a-zA-Z0-9]{32,}', "[openai_key redacted]"),
]


def _redact_string(value: str) -> str:
    for pattern, replacement in _REDACT_PATTERNS:
        value = re.sub(pattern, replacement, value, flags=re.IGNORECASE)
    for pattern, replacement in _DENYLIST_PATTERNS:
        value = re.sub(pattern, replacement, value, flags=re.IGNORECASE)
    return value


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect(db: sqlite3.Connection | str) -> sqlite3.Connection:
    if isinstance(db, sqlite3.Connection):
        return db
    if hasattr(db, "execute"):
        return db  # type: ignore[return-value]
    return sqlite3.connect(str(db))


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS process_events ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "event_type TEXT NOT NULL, "
        "category TEXT NOT NULL, "
        "severity TEXT NOT NULL DEFAULT 'INFO', "
        "agent_id TEXT, "
        "admin_user TEXT, "
        "trace_id TEXT, "
        "path TEXT, "
        "status TEXT, "
        "error_code TEXT, "
        "message TEXT, "
        "metadata_json TEXT, "
        "timestamp TEXT NOT NULL"
        ")"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_process_events_trace ON process_events(trace_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_process_events_timestamp ON process_events(timestamp)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_process_events_category ON process_events(category)"
    )
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")


def _row_to_event(row: sqlite3.Row) -> ProcessEvent:
    metadata = {}
    if row["metadata_json"]:
        try:
            metadata = json.loads(row["metadata_json"])
        except json.JSONDecodeError:
            metadata = {"_raw": row["metadata_json"]}
    return ProcessEvent(
        event_type=row["event_type"],
        category=row["category"],
        severity=row["severity"],
        agent_id=row["agent_id"] or "",
        admin_user=row["admin_user"] or "",
        trace_id=row["trace_id"] or "",
        path=row["path"] or "",
        status=row["status"] or "",
        error_code=row["error_code"] or "",
        message=row["message"] or "",
        metadata=metadata,
        timestamp=row["timestamp"],
    )


def record_process_event(db: sqlite3.Connection | str, event: ProcessEvent) -> None:
    trace_id = event.trace_id or new_trace_id()
    timestamp = event.timestamp or _utc_now()

    redacted_message = _redact_string(event.message)
    redacted_metadata_raw = _redact_string(json.dumps(event.metadata or {}, sort_keys=True))
    redacted_metadata = json.loads(redacted_metadata_raw)

    if redacted_message != event.message or redacted_metadata != event.metadata:
        log_trace_anchor(
            level="INFO",
            event="process_event.redacted",
            trace_id=trace_id,
            module=MODULE,
            function="record_process_event",
            block=MODULE_BLOCK,
            data={"event_type": event.event_type, "category": event.category},
        )

    metadata_json = json.dumps(redacted_metadata, sort_keys=True)

    conn = _connect(db)
    try:
        conn.row_factory = sqlite3.Row
        _ensure_schema(conn)
        conn.execute(
            "INSERT INTO process_events "
            "(event_type, category, severity, agent_id, admin_user, trace_id, "
            "path, status, error_code, message, metadata_json, timestamp) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event.event_type,
                event.category,
                event.severity or "INFO",
                event.agent_id or "",
                event.admin_user or "",
                trace_id,
                event.path or "",
                event.status or "",
                event.error_code or "",
                redacted_message,
                metadata_json,
                timestamp,
            ),
        )
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise

    log_trace_anchor(
        level="INFO",
        event="process_event.recorded",
        trace_id=trace_id,
        module=MODULE,
        function="record_process_event",
        block=MODULE_BLOCK,
        data={
            "event_type": event.event_type,
            "category": event.category,
            "severity": event.severity,
        },
    )


def query_process_events(
    db: sqlite3.Connection | str, filter: ProcessEventFilter
) -> list[ProcessEvent]:
    trace_id = new_trace_id()
    conn = _connect(db)
    conn.row_factory = sqlite3.Row
    _ensure_schema(conn)

    clauses: list[str] = []
    params: list[Any] = []

    if filter.from_timestamp is not None:
        clauses.append("timestamp >= ?")
        params.append(filter.from_timestamp)
    if filter.to_timestamp is not None:
        clauses.append("timestamp <= ?")
        params.append(filter.to_timestamp)
    if filter.severity is not None:
        clauses.append("severity = ?")
        params.append(filter.severity)
    if filter.category is not None:
        clauses.append("category = ?")
        params.append(filter.category)
    if filter.event_type is not None:
        clauses.append("event_type = ?")
        params.append(filter.event_type)
    if filter.agent_id is not None:
        clauses.append("agent_id = ?")
        params.append(filter.agent_id)
    if filter.admin_user is not None:
        clauses.append("admin_user = ?")
        params.append(filter.admin_user)
    if filter.path_prefix is not None:
        clauses.append("path LIKE ?")
        params.append(f"{filter.path_prefix}%")
    if filter.trace_id is not None:
        clauses.append("trace_id = ?")
        params.append(filter.trace_id)
    if filter.status is not None:
        clauses.append("status = ?")
        params.append(filter.status)
    if filter.error_code is not None:
        clauses.append("error_code = ?")
        params.append(filter.error_code)

    where = " AND ".join(clauses) if clauses else "1=1"
    query = f"SELECT * FROM process_events WHERE {where} ORDER BY timestamp ASC"
    rows = conn.execute(query, params).fetchall()

    results = [_row_to_event(row) for row in rows]

    log_trace_anchor(
        level="INFO",
        event="process_event.query.completed",
        trace_id=trace_id,
        module=MODULE,
        function="query_process_events",
        block=MODULE_BLOCK,
        data={"result_count": len(results)},
    )
    return results


def correlate_by_trace_id(db: sqlite3.Connection | str, trace_id: str) -> list[ProcessEvent]:
    return query_process_events(db, ProcessEventFilter(trace_id=trace_id))
