"""Module-local tests for M-026 LogReportService."""

from __future__ import annotations

import json
import logging
import sqlite3

import pytest

from memory_mcp.log_report_service import (
    LogReportRequest,
    LogReportResult,
    build_log_report,
    export_log_report,
)
from memory_mcp.process_event_log import (
    ProcessEvent,
    ProcessEventFilter,
    record_process_event,
    query_process_events,
)

MODULE = "log_report_service"

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _parse_logs(caplog) -> list[dict]:
    entries: list[dict] = []
    for record in caplog.records:
        try:
            entries.append(json.loads(record.message))
        except json.JSONDecodeError:
            pass
    return entries


def _fresh_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _insert_audit_event(conn: sqlite3.Connection, **kwargs) -> None:
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
        "INSERT INTO audit_log "
        "(event_id, timestamp, agent_id, operation, path, "
        "old_revision, new_revision, dry_run, policy_profile, "
        "result, affected_paths, trace_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            kwargs.get("event_id", "evt-001"),
            kwargs.get("timestamp", "2026-05-10T10:00:00Z"),
            kwargs.get("agent_id", "trusted_writer"),
            kwargs.get("operation", "write"),
            kwargs.get("path", "memory/note.md"),
            kwargs.get("old_revision", "abc123"),
            kwargs.get("new_revision", "def456"),
            int(kwargs.get("dry_run", False)),
            kwargs.get("policy_profile", "trusted_writer"),
            kwargs.get("result", "success"),
            kwargs.get("affected_paths", "memory/note.md"),
            kwargs.get("trace_id", "trace-audit-001"),
        ),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# M-026-S-001: exports_sanitized_log_for_datetime_range (DET + TRACE)
#   TA-038 (log_report.created) and TA-039 (log_report.exported) emitted
# ---------------------------------------------------------------------------


def test_exports_sanitized_log_for_datetime_range(caplog, trace_assert):
    caplog.set_level(logging.DEBUG)
    db = _fresh_db()

    record_process_event(db, ProcessEvent(
        event_type="boot", category="boot", severity="INFO",
        agent_id="agent-a", path="/vault", status="success",
        message="System boot completed",
        timestamp="2026-05-10T10:00:00Z",
    ))
    record_process_event(db, ProcessEvent(
        event_type="read", category="read", severity="INFO",
        agent_id="agent-b", path="/vault/notes/a.md", status="success",
        message="Read file a.md",
        timestamp="2026-05-10T11:00:00Z",
    ))
    record_process_event(db, ProcessEvent(
        event_type="error", category="error", severity="ERROR",
        agent_id="agent-c", path="/vault/notes/b.md", status="failed",
        message="File not found",
        timestamp="2026-05-10T12:00:00Z",
    ))

    request = LogReportRequest(
        from_timestamp="2026-05-10T10:30:00Z",
        to_timestamp="2026-05-10T11:30:00Z",
    )
    output = export_log_report(db, request)

    lines = output.split("\n")
    assert len(lines) == 1
    assert "read" in lines[0]
    assert "agent-b" in lines[0]
    assert "[boot]" not in lines[0]
    assert "[ERROR]" not in output

    log_entries = _parse_logs(caplog)
    events = [e.get("event") for e in log_entries]
    assert "log_report.created" in events
    assert "log_report.exported" in events
    assert trace_assert(log_entries, "log_report.created", "log_report.exported")


# ---------------------------------------------------------------------------
# M-026-S-002: filters_report_by_all_supported_dimensions (DET)
# ---------------------------------------------------------------------------


def test_filters_report_by_all_supported_dimensions(caplog):
    caplog.set_level(logging.DEBUG)
    db = _fresh_db()

    events_data = [
        ("boot", "boot", "INFO", "agent-a", "", "/vault", "success", "", "boot msg"),
        ("mutation", "mutation", "INFO", "agent-b", "admin1", "/vault/notes/x.md", "success", "", "mutated"),
        ("error", "error", "ERROR", "agent-c", "admin2", "/vault/notes/y.md", "failed", "E001", "failed msg"),
        ("read", "read", "WARN", "agent-d", "admin1", "/vault/summaries/z.md", "denied", "E002", "denied msg"),
        ("file_change", "file_change", "INFO", "agent-e", "admin3", "/vault/private/secret.md", "conflict", "E003", "conflict msg"),
    ]

    for et, cat, sev, ag, au, pth, st, ec, msg in events_data:
        record_process_event(db, ProcessEvent(
            event_type=et, category=cat, severity=sev,
            agent_id=ag, admin_user=au, path=pth,
            status=st, error_code=ec, message=msg,
            timestamp=f"2026-05-10T1{len([e for e in events_data if e[3]==ag]):02d}:00:00Z",
        ))

    by_severity = build_log_report(db, LogReportRequest(severity="ERROR"))
    assert by_severity.filtered_events == 1
    assert "ERROR" in by_severity.lines[0]

    by_category = build_log_report(db, LogReportRequest(category="mutation"))
    assert by_category.filtered_events == 1
    assert "mutation" in by_category.lines[0]

    by_agent = build_log_report(db, LogReportRequest(agent_id="agent-b"))
    assert by_agent.filtered_events == 1
    assert "agent-b" in by_agent.lines[0]

    by_admin = build_log_report(db, LogReportRequest(admin_user="admin1"))
    assert by_admin.filtered_events == 2

    by_path = build_log_report(db, LogReportRequest(path_prefix="/vault/notes"))
    assert by_path.filtered_events == 2
    assert all("/vault/notes" in ln for ln in by_path.lines)

    by_status = build_log_report(db, LogReportRequest(status="denied"))
    assert by_status.filtered_events == 1
    assert "denied" in by_status.lines[0]

    by_error_code = build_log_report(db, LogReportRequest(error_code="E002"))
    assert by_error_code.filtered_events == 1
    assert "E002" in by_error_code.lines[0]

    by_event_type = build_log_report(db, LogReportRequest(event_type="file_change"))
    assert by_event_type.filtered_events == 1
    assert "file_change" in by_event_type.lines[0]


# ---------------------------------------------------------------------------
# M-026-S-003: includes_audit_correlated_file_changes (DET)
#   cross-correlates with audit events
# ---------------------------------------------------------------------------


def test_includes_audit_correlated_file_changes(caplog):
    caplog.set_level(logging.DEBUG)
    db = _fresh_db()

    record_process_event(db, ProcessEvent(
        event_type="file_change", category="file_change", severity="INFO",
        agent_id="trusted_writer", path="/vault/memory/note.md",
        status="success", message="file modified via write operation",
        trace_id="trace-audit-001", timestamp="2026-05-10T10:01:00Z",
    ))
    record_process_event(db, ProcessEvent(
        event_type="read", category="read", severity="INFO",
        agent_id="readonly_agent", path="/vault/inbox/draft.md",
        status="success", message="Read draft",
        timestamp="2026-05-10T10:02:00Z",
    ))

    _insert_audit_event(
        db,
        event_id="evt-audit-001",
        timestamp="2026-05-10T10:00:00Z",
        agent_id="trusted_writer",
        operation="write",
        path="memory/note.md",
        result="success",
        trace_id="trace-audit-001",
    )

    request = LogReportRequest(trace_id="trace-audit-001")
    result = build_log_report(db, request)

    assert result.total_events >= 2
    categories = set()
    for line in result.lines:
        if "file_change" in line:
            categories.add("process")
        if "audit: write" in line:
            categories.add("audit")
    assert "process" in categories
    assert "audit" in categories


# ---------------------------------------------------------------------------
# M-026-F-001: invalid_time_range_rejected (DET)
# ---------------------------------------------------------------------------


def test_invalid_time_range_rejected():
    db = _fresh_db()

    request = LogReportRequest(
        from_timestamp="2026-05-10T12:00:00Z",
        to_timestamp="2026-05-10T10:00:00Z",
    )
    with pytest.raises(ValueError, match="Invalid time range"):
        build_log_report(db, request)


# ---------------------------------------------------------------------------
# M-026-F-002: report_export_redacts_secrets_and_raw_content (DET + TRACE)
#   TA-040 (log_report.redacted) emitted
# ---------------------------------------------------------------------------


def test_report_export_redacts_secrets_and_raw_content(caplog, trace_assert):
    caplog.set_level(logging.DEBUG)
    db = _fresh_db()

    record_process_event(db, ProcessEvent(
        event_type="auth", category="auth", severity="INFO",
        agent_id="agent-secret", path="/vault",
        status="success",
        message='Auth with api_key=sk-abcdefghijklmnop123456789 and password=supersecret123',
        timestamp="2026-05-10T14:00:00Z",
    ))
    record_process_event(db, ProcessEvent(
        event_type="mutation", category="mutation", severity="INFO",
        agent_id="agent-secret", path="private/config.yaml",
        status="success",
        message='Patch with patch_body=---\n+new_secret: value\n secret content',
        timestamp="2026-05-10T14:01:00Z",
    ))

    request = LogReportRequest()
    result = build_log_report(db, request)

    output = "\n".join(result.lines)
    lower_output = output.lower()

    assert "sk-abcdefghijklmnop123456789" not in lower_output
    assert "supersecret123" not in lower_output

    assert (
        "[api_key redacted]" in lower_output
        or "[redacted:api_key]" in lower_output
        or "[REDACTED:API_KEY]" in lower_output
    )
    assert (
        "[password redacted]" in lower_output
        or "[redacted:password]" in lower_output
        or "[REDACTED:PASSWORD]" in lower_output
    )

    assert result.redacted_count >= 1
    assert result.redacted_count <= result.total_events

    log_entries = _parse_logs(caplog)
    events_list = [e.get("event") for e in log_entries]
    assert "log_report.redacted" in events_list
    assert trace_assert(log_entries, "log_report.redacted")


# ---------------------------------------------------------------------------
# Additional: max_lines truncation with truncated flag
# ---------------------------------------------------------------------------


def test_max_lines_truncation_with_truncated_flag():
    db = _fresh_db()

    for i in range(10):
        record_process_event(db, ProcessEvent(
            event_type="mcp_request", category="mcp_request", severity="INFO",
            agent_id="agent", path=f"/vault/note_{i}.md",
            message=f"request {i}",
            timestamp=f"2026-05-10T1{i:02d}:00:00Z",
        ))

    request = LogReportRequest(max_lines=5)
    result = build_log_report(db, request)

    assert result.truncated is True
    assert result.filtered_events == 5
    assert result.total_events == 10
    assert len(result.lines) == 5

    request_unlimited = LogReportRequest(max_lines=10000)
    result_unlimited = build_log_report(db, request_unlimited)
    assert result_unlimited.truncated is False
    assert result_unlimited.filtered_events == 10


# ---------------------------------------------------------------------------
# Additional: empty results for no matching events
# ---------------------------------------------------------------------------


def test_empty_results_for_no_matching_events():
    db = _fresh_db()

    record_process_event(db, ProcessEvent(
        event_type="boot", category="boot", severity="INFO",
        agent_id="agent", path="/vault",
        message="booted", timestamp="2026-05-10T10:00:00Z",
    ))

    request = LogReportRequest(category="nonexistent_category")
    result = build_log_report(db, request)

    assert result.total_events == 0
    assert result.filtered_events == 0
    assert result.lines == []
    assert result.truncated is False


# ---------------------------------------------------------------------------
# Additional: multiple filter dimensions combined (AND logic)
# ---------------------------------------------------------------------------


def test_multiple_filter_dimensions_combined_and_logic():
    db = _fresh_db()

    record_process_event(db, ProcessEvent(
        event_type="mutation", category="mutation", severity="INFO",
        agent_id="agent-x", path="/vault/a.md",
        status="success", message="m1",
        timestamp="2026-05-10T10:00:00Z",
    ))
    record_process_event(db, ProcessEvent(
        event_type="mutation", category="mutation", severity="ERROR",
        agent_id="agent-x", path="/vault/b.md",
        status="failed", message="m2",
        timestamp="2026-05-10T11:00:00Z",
    ))
    record_process_event(db, ProcessEvent(
        event_type="read", category="read", severity="ERROR",
        agent_id="agent-x", path="/vault/c.md",
        status="success", message="m3",
        timestamp="2026-05-10T12:00:00Z",
    ))

    request = LogReportRequest(
        severity="ERROR",
        agent_id="agent-x",
        category="mutation",
    )
    result = build_log_report(db, request)
    assert result.filtered_events == 1
    assert "m2" in result.lines[0]

    request2 = LogReportRequest(
        from_timestamp="2026-05-10T09:00:00Z",
        to_timestamp="2026-05-10T11:30:00Z",
        category="mutation",
    )
    result2 = build_log_report(db, request2)
    assert result2.filtered_events == 2


# ---------------------------------------------------------------------------
# Additional: sanitized .log format matches expected line structure
# ---------------------------------------------------------------------------


def test_sanitized_log_format_matches_expected_line_structure():
    db = _fresh_db()

    record_process_event(db, ProcessEvent(
        event_type="index_refresh", category="index_refresh", severity="INFO",
        agent_id="agent-fmt", trace_id="trace-fmt-001",
        path="/vault/memory/note.md",
        status="success", error_code="",
        message="Index refreshed successfully",
        timestamp="2026-05-10T15:00:00Z",
    ))

    request = LogReportRequest()
    result = build_log_report(db, request)

    assert len(result.lines) == 1
    line = result.lines[0]

    assert "[2026-05-10T15:00:00Z]" in line
    assert "[INFO]" in line
    assert "[index_refresh]" in line
    assert "[index_refresh]" in line
    assert "agent=agent-fmt" in line
    assert "trace=trace-fmt-001" in line
    assert "path=/vault/memory/note.md" in line
    assert "status=success" in line
    assert "error=" in line
    assert "msg=Index refreshed successfully" in line

    expected_pattern = (
        r"\[[^\]]+\] \[[^\]]+\] \[[^\]]+\] \[[^\]]+\] "
        r"agent=\S+ trace=\S+ path=\S+ status=\S+ error=\S* msg=.+"
    )
    import re
    assert re.match(expected_pattern, line), f"Line format mismatch: {line}"


# ---------------------------------------------------------------------------
# Additional: LogReportResult has correct structure
# ---------------------------------------------------------------------------


def test_log_report_result_has_correct_structure():
    db = _fresh_db()

    record_process_event(db, ProcessEvent(
        event_type="boot", category="boot", severity="INFO",
        agent_id="agent", path="/vault", status="success",
        message="booted", timestamp="2026-05-10T10:00:00Z",
    ))

    result = build_log_report(db, LogReportRequest())

    assert isinstance(result, LogReportResult)
    assert isinstance(result.lines, list)
    assert isinstance(result.total_events, int)
    assert isinstance(result.filtered_events, int)
    assert isinstance(result.redacted_count, int)
    assert isinstance(result.truncated, bool)
    assert isinstance(result.generated_at, str)
    assert len(result.generated_at) > 0
    assert isinstance(result.trace_id, str)
    assert len(result.trace_id) > 0


# ---------------------------------------------------------------------------
# Additional: all event categories appear in report
# ---------------------------------------------------------------------------


def test_all_event_categories_in_report():
    db = _fresh_db()

    categories = [
        "boot", "config", "auth", "mcp_request", "policy", "read",
        "mutation", "file_change", "promotion", "index_refresh",
        "sync_external_change", "backup", "membank_init", "vault_layout",
        "admin_dashboard", "error",
    ]

    for i, cat in enumerate(categories):
        record_process_event(db, ProcessEvent(
            event_type=cat, category=cat, severity="INFO",
            agent_id="agent", path=f"/vault/{cat}.md",
            message=f"Event for {cat}",
            timestamp=f"2026-05-10T1{i:02d}:00:00Z",
        ))

    result = build_log_report(db, LogReportRequest())
    assert result.total_events == len(categories)

    for cat in categories:
        assert any(cat in line for line in result.lines), f"Category {cat} not found in report"


# ---------------------------------------------------------------------------
# Additional: export returns formatted string with newlines
# ---------------------------------------------------------------------------


def test_export_returns_formatted_string_with_newlines():
    db = _fresh_db()

    record_process_event(db, ProcessEvent(
        event_type="boot", category="boot", severity="INFO",
        agent_id="agent", path="/vault", status="success",
        message="first", timestamp="2026-05-10T10:00:00Z",
    ))
    record_process_event(db, ProcessEvent(
        event_type="read", category="read", severity="INFO",
        agent_id="agent", path="/vault/notes/a.md", status="success",
        message="second", timestamp="2026-05-10T11:00:00Z",
    ))

    output = export_log_report(db, LogReportRequest())
    assert isinstance(output, str)
    lines = output.split("\n")
    assert len(lines) == 2
    assert "first" in lines[0]
    assert "second" in lines[1]
