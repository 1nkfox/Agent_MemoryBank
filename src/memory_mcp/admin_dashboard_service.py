"""Admin dashboard service — M-027 AdminDashboardService."""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from memory_mcp.observability import log_trace_anchor, new_trace_id
from memory_mcp.process_event_log import (
    BUSY_TIMEOUT_MS,
    ProcessEventFilter,
    _connect,
    _ensure_schema,
    query_process_events,
)

MODULE = "admin_dashboard_service"
MODULE_BLOCK = "M-027"


@dataclass
class DashboardSummary:
    server_status: str
    vault_status: str
    index_status: str
    backup_status: str
    layout_status: str
    total_events: int
    recent_errors: int
    generated_at: str
    degraded: bool


@dataclass
class Event:
    id: int
    event_type: str
    category: str
    severity: str
    agent_id: str
    timestamp: str
    path: str
    message: str


@dataclass
class ErrorEntry:
    timestamp: str
    error_code: str
    event_type: str
    count: int


@dataclass
class ProcessDetail:
    event_id: int
    event_type: str
    category: str
    severity: str
    agent_id: str
    trace_id: str
    path: str
    status: str
    error_code: str
    message: str
    metadata: dict[str, Any]
    timestamp: str
    correlated_audit: dict[str, Any] | None


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


_SENSITIVE_KEYWORDS = frozenset({
    "api_key", "apikey", "password", "passwd", "pwd",
    "authorization", "bearer", "token", "secret",
    "credential", "private_key",
})


def _contains_sensitive(value: str) -> bool:
    lowered = value.lower()
    for kw in _SENSITIVE_KEYWORDS:
        if kw in lowered:
            return True
    if lowered.startswith("sk-") or lowered.startswith("ghp_"):
        return True
    return False


def _sanitize_event_for_dashboard(raw_event: dict[str, Any]) -> dict[str, Any]:
    sanitized: dict[str, Any] = {}
    for key, value in raw_event.items():
        if key == "message" and isinstance(value, str):
            sanitized[key] = "[redacted]" if _contains_sensitive(value) else value
        elif key in ("metadata", "metadata_json"):
            sanitized[key] = {}
        elif key == "id":
            sanitized[key] = int(value) if value is not None else 0
        else:
            sanitized[key] = value
    return sanitized


def _check_server_status(vault_root: str) -> str:
    if os.path.isdir(vault_root):
        return "healthy"
    return "unavailable"


def _check_vault_status(vault_root: str, trace_id: str) -> str:
    try:
        from memory_mcp.vault_layout import detect_vault_layout

        report = detect_vault_layout(vault_root, require_obsidian_marker=False)
        if report.obsidian_marker_found:
            return "healthy"
        return "degraded"
    except Exception:
        log_trace_anchor(
            level="WARNING",
            event="admin_dashboard.degraded",
            trace_id=trace_id,
            module=MODULE,
            function="get_dashboard_summary",
            block=MODULE_BLOCK,
            data={"subsystem": "vault", "reason": "layout detection failed"},
        )
        return "unavailable"


def _check_index_status(vault_root: str, trace_id: str) -> str:
    index_db_path = os.path.join(vault_root, ".obsidian", "index.db")
    if os.path.isfile(index_db_path):
        try:
            conn = sqlite3.connect(index_db_path)
            conn.row_factory = sqlite3.Row
            conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
            conn.close()
            table_names = {row["name"] for row in rows}
            if "note_index" in table_names:
                return "healthy"
            return "degraded"
        except Exception:
            return "degraded"
    return "unavailable"


def _check_backup_status(config: Any, trace_id: str) -> str:
    try:
        from memory_mcp.backup_service import backup_health

        health = backup_health(config)
        if health.get("healthy"):
            return "healthy"
        return "degraded"
    except Exception:
        log_trace_anchor(
            level="WARNING",
            event="admin_dashboard.degraded",
            trace_id=trace_id,
            module=MODULE,
            function="get_dashboard_summary",
            block=MODULE_BLOCK,
            data={"subsystem": "backup", "reason": "backup health check failed"},
        )
        return "unavailable"


def _check_layout_status(
    directory_contract: Any, vault_root: str, trace_id: str
) -> str:
    try:
        from memory_mcp.vault_layout import detect_directory_drift

        actual_dirs: list[str] = []
        if os.path.isdir(vault_root):
            try:
                for entry in os.listdir(vault_root):
                    full = os.path.join(vault_root, entry)
                    if os.path.isdir(full):
                        actual_dirs.append(entry)
            except OSError:
                pass

        drift = detect_directory_drift(directory_contract, vault_root, actual_dirs)
        if drift.ok:
            return "healthy"
        return "drift_detected"
    except Exception:
        log_trace_anchor(
            level="WARNING",
            event="admin_dashboard.degraded",
            trace_id=trace_id,
            module=MODULE,
            function="get_dashboard_summary",
            block=MODULE_BLOCK,
            data={"subsystem": "layout", "reason": "layout drift check failed"},
        )
        return "unavailable"


def _query_audit_for_trace(
    conn: sqlite3.Connection, trace_id: str
) -> dict[str, Any] | None:
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
        "CREATE INDEX IF NOT EXISTS idx_dashboard_audit_trace ON audit_log(trace_id)"
    )
    rows = conn.execute(
        "SELECT * FROM audit_log WHERE trace_id = ? ORDER BY timestamp ASC LIMIT 1",
        (trace_id,),
    ).fetchall()
    if not rows:
        return None
    row = rows[0]
    return {
        "event_id": row["event_id"],
        "timestamp": row["timestamp"],
        "agent_id": row["agent_id"] or "",
        "operation": row["operation"] or "",
        "path": row["path"] or "",
        "old_revision": row["old_revision"] or "",
        "new_revision": row["new_revision"] or "",
        "dry_run": bool(row["dry_run"]),
        "policy_profile": row["policy_profile"] or "",
        "result": row["result"] or "",
        "affected_paths": row["affected_paths"] or "",
        "trace_id": row["trace_id"] or "",
    }


def _query_events_direct(
    conn: sqlite3.Connection,
    where_clause: str = "1=1",
    params: tuple = (),
    order: str = "ORDER BY timestamp DESC",
    limit: int = 0,
) -> list[dict[str, Any]]:
    query = f"SELECT * FROM process_events WHERE {where_clause} {order}"
    query += f" LIMIT {limit}"
    rows = conn.execute(query, params).fetchall()
    results: list[dict[str, Any]] = []
    for row in rows:
        results.append(dict(row))
    return results


def get_dashboard_summary(
    db: sqlite3.Connection | str,
    vault_root: str,
    directory_contract: Any,
    trace_id: str,
    config: Any = None,
) -> DashboardSummary:
    degraded = False
    summary_trace = trace_id or new_trace_id()

    server_status = _check_server_status(vault_root)

    vault_status = _check_vault_status(vault_root, summary_trace)
    if vault_status == "unavailable":
        degraded = True

    layout_status = _check_layout_status(directory_contract, vault_root, summary_trace)
    if layout_status == "unavailable":
        degraded = True

    index_status = _check_index_status(vault_root, summary_trace)
    if index_status == "unavailable":
        degraded = True

    backup_status = "unknown"
    if config is not None:
        backup_status = _check_backup_status(config, summary_trace)
        if backup_status == "unavailable":
            degraded = True

    total_events = 0
    recent_errors = 0
    try:
        conn = _connect(db)
        conn.row_factory = sqlite3.Row
        _ensure_schema(conn)
        total_row = conn.execute(
            "SELECT COUNT(*) as cnt FROM process_events"
        ).fetchone()
        if total_row:
            total_events = total_row["cnt"]
        error_row = conn.execute(
            "SELECT COUNT(*) as cnt FROM process_events WHERE severity = 'ERROR'"
        ).fetchone()
        if error_row:
            recent_errors = error_row["cnt"]
    except Exception:
        log_trace_anchor(
            level="ERROR",
            event="admin_dashboard.degraded",
            trace_id=summary_trace,
            module=MODULE,
            function="get_dashboard_summary",
            block=MODULE_BLOCK,
            data={"reason": "event store unavailable"},
        )
        degraded = True

    log_trace_anchor(
        level="INFO",
        event="admin_dashboard.summary.generated",
        trace_id=summary_trace,
        module=MODULE,
        function="get_dashboard_summary",
        block=MODULE_BLOCK,
        data={
            "server_status": server_status,
            "vault_status": vault_status,
            "index_status": index_status,
            "backup_status": backup_status,
            "layout_status": layout_status,
            "total_events": total_events,
            "recent_errors": recent_errors,
            "degraded": degraded,
        },
    )

    return DashboardSummary(
        server_status=server_status,
        vault_status=vault_status,
        index_status=index_status,
        backup_status=backup_status,
        layout_status=layout_status,
        total_events=total_events,
        recent_errors=recent_errors,
        generated_at=_utc_now(),
        degraded=degraded,
    )


def _query_audit_events(
    audit_db: str,
    limit: int,
) -> list[dict[str, Any]]:
    try:
        conn = sqlite3.connect(audit_db)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
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
        rows = conn.execute(
            "SELECT id, event_id, timestamp, agent_id, operation, path, result "
            "FROM audit_log ORDER BY timestamp DESC LIMIT ?",
            (limit,),
        ).fetchall()
        conn.close()
        return [
            {
                "id": row["id"],
                "event_type": row.get("operation", "audit"),
                "category": "audit",
                "severity": "INFO",
                "agent_id": row["agent_id"] or "",
                "timestamp": row["timestamp"],
                "path": row["path"] or "",
                "message": f"Operation: {row['operation']} — {row['result']}",
            }
            for row in rows
        ]
    except Exception:
        return []


def get_recent_events(
    db: sqlite3.Connection | str,
    limit: int,
    trace_id: str,
    audit_db: str = "",
) -> list[Event]:
    request_trace = trace_id or new_trace_id()

    try:
        conn = _connect(db)
        conn.row_factory = sqlite3.Row
        _ensure_schema(conn)

        raw_events = _query_events_direct(
            conn,
            where_clause="1=1",
            order="ORDER BY timestamp DESC",
            limit=limit,
        )
    except Exception:
        log_trace_anchor(
            level="ERROR",
            event="admin_dashboard.degraded",
            trace_id=request_trace,
            module=MODULE,
            function="get_recent_events",
            block=MODULE_BLOCK,
            data={"reason": "event store unavailable"},
        )
        return []

    audit_events: list[dict[str, Any]] = []
    if audit_db:
        audit_events = _query_audit_events(audit_db, limit)

    all_raw: list[dict[str, Any]] = raw_events + audit_events
    all_raw.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
    all_raw = all_raw[:limit]

    result: list[Event] = []
    for raw in all_raw:
        safe = _sanitize_event_for_dashboard({
            "id": raw.get("id", 0),
            "event_type": raw.get("event_type", ""),
            "category": raw.get("category", ""),
            "severity": raw.get("severity", ""),
            "agent_id": raw.get("agent_id", ""),
            "timestamp": raw.get("timestamp", ""),
            "path": raw.get("path", ""),
            "message": raw.get("message", ""),
        })
        result.append(Event(
            id=safe["id"],
            event_type=safe["event_type"],
            category=safe["category"],
            severity=safe["severity"],
            agent_id=safe["agent_id"],
            timestamp=safe["timestamp"],
            path=safe["path"],
            message=safe["message"],
        ))

    log_trace_anchor(
        level="INFO",
        event="admin_dashboard.events.filtered",
        trace_id=request_trace,
        module=MODULE,
        function="get_recent_events",
        block=MODULE_BLOCK,
        data={"limit": limit, "returned": len(result), "audit_events": len(audit_events)},
    )

    return result


def get_error_timeline(
    db: sqlite3.Connection | str,
    from_ts: str,
    to_ts: str,
    trace_id: str,
) -> list[ErrorEntry]:
    request_trace = trace_id or new_trace_id()

    try:
        filter_obj = ProcessEventFilter(
            from_timestamp=from_ts or None,
            to_timestamp=to_ts or None,
            severity="ERROR",
        )
        events = query_process_events(db, filter_obj)
    except Exception:
        log_trace_anchor(
            level="ERROR",
            event="admin_dashboard.degraded",
            trace_id=request_trace,
            module=MODULE,
            function="get_error_timeline",
            block=MODULE_BLOCK,
            data={"reason": "event store unavailable for error query"},
        )
        return []

    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for e in events:
        ec = e.error_code or "UNKNOWN"
        key = (ec, e.event_type)
        if key not in grouped:
            grouped[key] = {
                "error_code": ec,
                "event_type": e.event_type,
                "count": 0,
                "timestamp": e.timestamp,
            }
        grouped[key]["count"] += 1
        if e.timestamp < grouped[key]["timestamp"]:
            grouped[key]["timestamp"] = e.timestamp

    result: list[ErrorEntry] = [
        ErrorEntry(
            timestamp=entry["timestamp"],
            error_code=entry["error_code"],
            event_type=entry["event_type"],
            count=entry["count"],
        )
        for entry in grouped.values()
    ]

    result.sort(key=lambda x: x.timestamp)

    return result


def get_process_detail(
    db: sqlite3.Connection | str,
    event_id: int,
    trace_id: str,
) -> ProcessDetail | None:
    conn = _connect(db)
    conn.row_factory = sqlite3.Row
    _ensure_schema(conn)

    rows = conn.execute(
        "SELECT * FROM process_events WHERE id = ?", (event_id,)
    ).fetchall()

    if not rows:
        return None

    row = rows[0]
    raw: dict[str, Any] = {
        "id": row["id"],
        "event_type": row["event_type"] or "",
        "category": row["category"] or "",
        "severity": row["severity"] or "",
        "agent_id": row["agent_id"] or "",
        "trace_id": row["trace_id"] or "",
        "path": row["path"] or "",
        "status": row["status"] or "",
        "error_code": row["error_code"] or "",
        "message": row["message"] or "",
        "metadata": {},
        "timestamp": row["timestamp"] or "",
    }

    try:
        import json
        if row["metadata_json"]:
            raw["metadata"] = json.loads(row["metadata_json"])
    except Exception:
        raw["metadata"] = {}

    safe = _sanitize_event_for_dashboard(raw)

    correlated_audit: dict[str, Any] | None = None
    if raw["trace_id"]:
        correlated_audit = _query_audit_for_trace(conn, raw["trace_id"])

    return ProcessDetail(
        event_id=safe["id"],
        event_type=safe["event_type"],
        category=safe["category"],
        severity=safe["severity"],
        agent_id=safe["agent_id"],
        trace_id=safe["trace_id"],
        path=safe["path"],
        status=safe["status"],
        error_code=safe["error_code"],
        message=safe["message"],
        metadata=safe["metadata"],
        timestamp=safe["timestamp"],
        correlated_audit=correlated_audit,
    )


def request_log_export(
    db: sqlite3.Connection | str,
    request: Any,
    trace_id: str,
) -> Any:
    from memory_mcp.log_report_service import (
        LogReportRequest,
        build_log_report,
    )

    lr = LogReportRequest(
        from_timestamp=getattr(request, "from_timestamp", "") or "",
        to_timestamp=getattr(request, "to_timestamp", "") or "",
        severity=getattr(request, "severity", "") or "",
        category=getattr(request, "category", "") or "",
        event_type=getattr(request, "event_type", "") or "",
        agent_id=getattr(request, "agent_id", "") or "",
        admin_user=getattr(request, "admin_user", "") or "",
        path_prefix=getattr(request, "path_prefix", "") or "",
        trace_id=getattr(request, "trace_id", "") or "",
        status=getattr(request, "status", "") or "",
        error_code=getattr(request, "error_code", "") or "",
        max_lines=getattr(request, "max_lines", 10000),
    )

    return build_log_report(db, lr)
