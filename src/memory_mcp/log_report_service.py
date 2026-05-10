"""Log report service — M-026 LogReportService."""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from memory_mcp.observability import log_trace_anchor, new_trace_id
from memory_mcp.process_event_log import (
    ProcessEventFilter,
    _connect,
    _ensure_schema,
    query_process_events,
)

MODULE = "log_report_service"
MODULE_BLOCK = "M-026"
DEFAULT_MAX_LINES = 10000
BUSY_TIMEOUT_MS = 5000


@dataclass
class LogReportRequest:
    from_timestamp: str = ""
    to_timestamp: str = ""
    severity: str = ""
    category: str = ""
    event_type: str = ""
    agent_id: str = ""
    admin_user: str = ""
    path_prefix: str = ""
    trace_id: str = ""
    status: str = ""
    error_code: str = ""
    max_lines: int = DEFAULT_MAX_LINES


@dataclass
class LogReportResult:
    lines: list[str] = field(default_factory=list)
    total_events: int = 0
    filtered_events: int = 0
    redacted_count: int = 0
    truncated: bool = False
    generated_at: str = ""
    trace_id: str = ""


_REDACT_RULES: list[tuple[str, str]] = [
    (r'(?:api[_-]?key|apikey)\s*[:=]\s*["\']?[\w\-\.]{8,}["\']?', "[REDACTED:API_KEY]"),
    (r'sk-[a-zA-Z0-9]{32,}', "[REDACTED:API_KEY]"),
    (r'ghp_[a-zA-Z0-9]{36}', "[REDACTED:API_KEY]"),
    (r'(?:password|passwd|pwd)\s*[:=]\s*["\']?[^\s"\'\]\}]{1,}["\']?', "[REDACTED:PASSWORD]"),
    (r'(?:authorization|auth)\s*[:=]\s*["\']?[\w\-\.]{8,}["\']?', "[REDACTED:AUTH_HEADER]"),
    (r'(?:bearer\s+)[\w\-\.]{20,}', "[REDACTED:AUTH_HEADER]"),
    (r'(?:note_content)\s*(?::|=)\s*["\']?[^"\'\]\}]+["\']?', "[REDACTED:NOTE_CONTENT]"),
    (r'(?:patch_body)\s*(?::|=)\s*["\']?[^"\'\]\}]+["\']?', "[REDACTED:PATCH_BODY]"),
]

_DENYLIST_PATH_PATTERNS: list[tuple[str, str]] = [
    (r'(?:^|\s|:)private/\S+', "[REDACTED:PATH]"),
    (r'(?:^|\s|:)secrets/\S+', "[REDACTED:PATH]"),
]


def _redact_line(line: str) -> tuple[str, bool]:
    original = line
    for pattern, replacement in _REDACT_RULES:
        line = re.sub(pattern, replacement, line, flags=re.IGNORECASE)
    for pattern, replacement in _DENYLIST_PATH_PATTERNS:
        line = re.sub(pattern, replacement, line, flags=re.IGNORECASE)
    return line, line != original


def _format_log_line(event: dict[str, Any]) -> str:
    return (
        f"[{event.get('timestamp', '')}] "
        f"[{event.get('severity', '')}] "
        f"[{event.get('category', '')}] "
        f"[{event.get('event_type', '')}] "
        f"agent={event.get('agent_id', '')} "
        f"trace={event.get('trace_id', '')} "
        f"path={event.get('path', '')} "
        f"status={event.get('status', '')} "
        f"error={event.get('error_code', '')} "
        f"msg={event.get('message', '')}"
    )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ensure_audit_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS audit_log ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "event_id TEXT NOT NULL, "
        "timestamp TEXT NOT NULL, "
        "agent_id TEXT, "
        "operation TEXT, "
        "path TEXT, "
        "old_revision TEXT, "
        "new_revision TEXT, "
        "dry_run INTEGER DEFAULT 0, "
        "policy_profile TEXT, "
        "result TEXT, "
        "affected_paths TEXT, "
        "trace_id TEXT"
        ")"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_audit_log_trace ON audit_log(trace_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_audit_log_timestamp ON audit_log(timestamp)"
    )
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")


def _request_to_filter(request: LogReportRequest) -> ProcessEventFilter:
    return ProcessEventFilter(
        from_timestamp=request.from_timestamp or None,
        to_timestamp=request.to_timestamp or None,
        severity=request.severity or None,
        category=request.category or None,
        event_type=request.event_type or None,
        agent_id=request.agent_id or None,
        admin_user=request.admin_user or None,
        path_prefix=request.path_prefix or None,
        trace_id=request.trace_id or None,
        status=request.status or None,
        error_code=request.error_code or None,
    )


def _query_audit_events(
    conn: sqlite3.Connection, request: LogReportRequest
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []

    if request.from_timestamp:
        clauses.append("timestamp >= ?")
        params.append(request.from_timestamp)
    if request.to_timestamp:
        clauses.append("timestamp <= ?")
        params.append(request.to_timestamp)
    if request.agent_id:
        clauses.append("agent_id = ?")
        params.append(request.agent_id)
    if request.path_prefix:
        clauses.append("path LIKE ?")
        params.append(f"{request.path_prefix}%")
    if request.trace_id:
        clauses.append("trace_id = ?")
        params.append(request.trace_id)

    where = " AND ".join(clauses) if clauses else "1=1"
    query = f"SELECT * FROM audit_log WHERE {where} ORDER BY timestamp ASC"
    rows = conn.execute(query, params).fetchall()

    events: list[dict[str, Any]] = []
    for row in rows:
        events.append({
            "timestamp": row["timestamp"],
            "severity": "INFO",
            "category": "audit",
            "event_type": row["operation"],
            "agent_id": row["agent_id"] or "",
            "trace_id": row["trace_id"] or "",
            "path": row["path"] or "",
            "status": row["result"] or "",
            "error_code": "",
            "message": (
                f"audit: {row['operation']} {row['path']} -> {row['result']}"
            ),
        })
    return events


def _process_event_to_dict(pe: Any) -> dict[str, Any]:
    return {
        "timestamp": pe.timestamp,
        "severity": pe.severity,
        "category": pe.category,
        "event_type": pe.event_type,
        "agent_id": pe.agent_id,
        "trace_id": pe.trace_id,
        "path": pe.path,
        "status": pe.status,
        "error_code": pe.error_code,
        "message": pe.message,
    }


def build_log_report(
    db: sqlite3.Connection | str, request: LogReportRequest
) -> LogReportResult:
    trace_id = new_trace_id()

    if (
        request.from_timestamp
        and request.to_timestamp
        and request.from_timestamp > request.to_timestamp
    ):
        raise ValueError(
            f"Invalid time range: from_timestamp ({request.from_timestamp}) "
            f"> to_timestamp ({request.to_timestamp})"
        )

    conn = _connect(db)
    conn.row_factory = sqlite3.Row
    _ensure_schema(conn)
    _ensure_audit_schema(conn)

    max_lines = request.max_lines if request.max_lines > 0 else DEFAULT_MAX_LINES
    if max_lines > DEFAULT_MAX_LINES:
        max_lines = DEFAULT_MAX_LINES

    pf = _request_to_filter(request)
    process_events = query_process_events(conn, pf)
    audit_events = _query_audit_events(conn, request)

    combined = [
        _process_event_to_dict(pe) for pe in process_events
    ] + audit_events

    combined.sort(key=lambda e: e.get("timestamp", ""))

    total_events = len(combined)
    redacted_count = 0
    formatted_lines: list[str] = []

    for event in combined:
        raw_line = _format_log_line(event)
        redacted_line, was_redacted = _redact_line(raw_line)
        if was_redacted:
            redacted_count += 1
        formatted_lines.append(redacted_line)

    truncated = len(formatted_lines) > max_lines
    if truncated:
        formatted_lines = formatted_lines[:max_lines]

    filtered_events = len(formatted_lines)

    if redacted_count > 0:
        log_trace_anchor(
            level="INFO",
            event="log_report.redacted",
            trace_id=trace_id,
            module=MODULE,
            function="build_log_report",
            block=MODULE_BLOCK,
            data={"redacted_count": redacted_count},
        )

    log_trace_anchor(
        level="INFO",
        event="log_report.created",
        trace_id=trace_id,
        module=MODULE,
        function="build_log_report",
        block=MODULE_BLOCK,
        data={
            "total_events": total_events,
            "filtered_events": filtered_events,
            "redacted_count": redacted_count,
            "truncated": truncated,
        },
    )

    return LogReportResult(
        lines=formatted_lines,
        total_events=total_events,
        filtered_events=filtered_events,
        redacted_count=redacted_count,
        truncated=truncated,
        generated_at=_utc_now(),
        trace_id=trace_id,
    )


def export_log_report(
    db: sqlite3.Connection | str, request: LogReportRequest
) -> str:
    result = build_log_report(db, request)

    log_trace_anchor(
        level="INFO",
        event="log_report.exported",
        trace_id=result.trace_id,
        module=MODULE,
        function="export_log_report",
        block=MODULE_BLOCK,
        data={
            "total_events": result.total_events,
            "filtered_events": result.filtered_events,
        },
    )

    return "\n".join(result.lines)
