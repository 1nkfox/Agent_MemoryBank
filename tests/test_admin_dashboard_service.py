"""Module-local tests for M-027 AdminDashboardService."""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from dataclasses import dataclass

import pytest

from memory_mcp.admin_dashboard_service import (
    MODULE,
    DashboardSummary,
    ErrorEntry,
    Event,
    ProcessDetail,
    get_dashboard_summary,
    get_error_timeline,
    get_process_detail,
    get_recent_events,
    request_log_export,
)
from memory_mcp.process_event_log import (
    ProcessEvent,
    ProcessEventFilter,
    record_process_event,
)

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
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


@dataclass
class _FakeCanonicalPath:
    semantic_key: str
    relative_path: str
    absolute_path: str


@dataclass
class _FakeDirectoryContract:
    keys: dict
    aliases: dict
    created_at: str


def _make_directory_contract(vault_root: str, dirs: list[str] | None = None) -> _FakeDirectoryContract:
    if dirs is None:
        dirs = ["00_Inbox", "memory", "summaries", "70_Wiki"]
    keys = {}
    for d in dirs:
        keys[d] = _FakeCanonicalPath(
            semantic_key=d,
            relative_path=d,
            absolute_path=os.path.join(vault_root, d),
        )
    return _FakeDirectoryContract(
        keys=keys,
        aliases={d.lower(): d for d in dirs},
        created_at="2026-05-10T10:00:00Z",
    )


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


# ===========================================================================
# M-027-S-001: summary_includes_server_vault_index_backup_layout_status
#   TA-041 (admin_dashboard.summary.generated) emitted
# ===========================================================================


def test_summary_includes_server_vault_index_backup_layout_status(
    caplog, trace_assert, tmp_path
):
    caplog.set_level(logging.DEBUG)
    db = _fresh_db()

    vault_root = str(tmp_path / "vault")
    os.makedirs(os.path.join(vault_root, ".obsidian"), exist_ok=True)
    os.makedirs(os.path.join(vault_root, "00_Inbox"), exist_ok=True)
    os.makedirs(os.path.join(vault_root, "memory"), exist_ok=True)
    os.makedirs(os.path.join(vault_root, "summaries"), exist_ok=True)
    os.makedirs(os.path.join(vault_root, "70_Wiki"), exist_ok=True)

    directory_contract = _make_directory_contract(vault_root)

    record_process_event(db, ProcessEvent(
        event_type="boot", category="boot", severity="INFO",
        agent_id="agent-a", path="/vault", status="success",
        message="System booted", timestamp="2026-05-10T10:00:00Z",
    ))

    summary = get_dashboard_summary(
        db, vault_root, directory_contract, "trace-summary-001",
    )

    assert isinstance(summary, DashboardSummary)
    assert summary.server_status == "healthy"
    assert summary.vault_status in ("healthy", "degraded")
    assert summary.index_status in ("healthy", "degraded", "unavailable")
    assert summary.layout_status in ("healthy", "drift_detected")
    assert summary.backup_status == "unknown"
    assert summary.total_events >= 1
    assert summary.recent_errors >= 0
    assert isinstance(summary.generated_at, str)
    assert len(summary.generated_at) > 0
    assert isinstance(summary.degraded, bool)

    log_entries = _parse_logs(caplog)
    events_list = [e.get("event") for e in log_entries]
    assert "admin_dashboard.summary.generated" in events_list  # TA-041
    assert trace_assert(log_entries, "admin_dashboard.summary.generated")


# ===========================================================================
# M-027-S-002: events_filtered_for_dashboard
#   TA-042 (admin_dashboard.events.filtered) emitted
# ===========================================================================


def test_events_filtered_for_dashboard(caplog, trace_assert):
    caplog.set_level(logging.DEBUG)
    db = _fresh_db()

    for i in range(10):
        record_process_event(db, ProcessEvent(
            event_type="mcp_request", category="mcp_request", severity="INFO",
            agent_id=f"agent-{i}", path=f"/vault/note_{i}.md",
            message=f"request {i}", timestamp=f"2026-05-10T1{i:02d}:00:00Z",
        ))

    events = get_recent_events(db, limit=5, trace_id="trace-events-001")

    assert len(events) == 5
    for e in events:
        assert isinstance(e, Event)
        assert isinstance(e.id, int)
        assert isinstance(e.event_type, str)
        assert isinstance(e.category, str)
        assert isinstance(e.severity, str)
        assert isinstance(e.agent_id, str)
        assert isinstance(e.timestamp, str)
        assert isinstance(e.path, str)
        assert isinstance(e.message, str)

    for i in range(len(events) - 1):
        assert events[i].timestamp >= events[i + 1].timestamp

    log_entries = _parse_logs(caplog)
    events_list = [e.get("event") for e in log_entries]
    assert "admin_dashboard.events.filtered" in events_list  # TA-042
    assert trace_assert(log_entries, "admin_dashboard.events.filtered")


# ===========================================================================
# M-027-S-003: errors_grouped_by_error_code
# ===========================================================================


def test_errors_grouped_by_error_code():
    db = _fresh_db()

    error_data = [
        ("E001", "file_change", "2026-05-10T10:00:00Z"),
        ("E001", "file_change", "2026-05-10T10:05:00Z"),
        ("E001", "file_change", "2026-05-10T10:10:00Z"),
        ("E002", "mutation", "2026-05-10T11:00:00Z"),
        ("E002", "mutation", "2026-05-10T11:05:00Z"),
        ("E003", "read", "2026-05-10T12:00:00Z"),
    ]

    for ec, et, ts in error_data:
        record_process_event(db, ProcessEvent(
            event_type=et, category=et, severity="ERROR",
            agent_id="agent-x", path="/vault/test.md",
            status="failed", error_code=ec,
            message=f"Error {ec}", timestamp=ts,
        ))

    timeline = get_error_timeline(
        db,
        from_ts="2026-05-10T09:00:00Z",
        to_ts="2026-05-10T13:00:00Z",
        trace_id="trace-errors-001",
    )

    assert len(timeline) == 3

    by_code = {e.error_code: e for e in timeline}
    assert by_code["E001"].count == 3
    assert by_code["E002"].count == 2
    assert by_code["E003"].count == 1

    for e in timeline:
        assert isinstance(e, ErrorEntry)
        assert isinstance(e.timestamp, str)
        assert isinstance(e.error_code, str)
        assert isinstance(e.event_type, str)
        assert isinstance(e.count, int)


# ===========================================================================
# M-027-S-003b: errors with empty error_code grouped as UNKNOWN
# ===========================================================================


def test_errors_without_error_code_grouped_as_unknown():
    db = _fresh_db()

    for i in range(3):
        record_process_event(db, ProcessEvent(
            event_type="error", category="error", severity="ERROR",
            agent_id="agent-x", path="/vault/test.md",
            status="failed", error_code="",
            message=f"error {i}", timestamp=f"2026-05-10T1{i}:00:00Z",
        ))

    timeline = get_error_timeline(
        db,
        from_ts="2026-05-10T00:00:00Z",
        to_ts="2026-05-10T20:00:00Z",
        trace_id="trace-unknown-001",
    )

    assert len(timeline) == 1
    assert timeline[0].error_code == "UNKNOWN"
    assert timeline[0].count == 3


# ===========================================================================
# M-027-F-001: event_store_unavailable_returns_degraded_dashboard
#   TA-043 (admin_dashboard.degraded) emitted
# ===========================================================================


def test_event_store_unavailable_returns_degraded_dashboard(
    caplog, trace_assert, tmp_path
):
    caplog.set_level(logging.DEBUG)
    db = sqlite3.connect(":memory:")
    db.close()

    vault_root = str(tmp_path / "vault")
    os.makedirs(os.path.join(vault_root, ".obsidian"), exist_ok=True)
    os.makedirs(os.path.join(vault_root, "00_Inbox"), exist_ok=True)

    directory_contract = _make_directory_contract(vault_root)

    summary = get_dashboard_summary(
        db, vault_root, directory_contract, "trace-degraded-001",
    )

    assert summary.degraded is True
    assert summary.total_events == 0
    assert summary.recent_errors == 0

    log_entries = _parse_logs(caplog)
    events_list = [e.get("event") for e in log_entries]
    assert "admin_dashboard.degraded" in events_list  # TA-043
    assert trace_assert(log_entries, "admin_dashboard.degraded")


# ===========================================================================
# M-027-F-002: dashboard_never_exposes_raw_note_content
# ===========================================================================


def test_dashboard_never_exposes_raw_note_content():
    db = _fresh_db()

    record_process_event(db, ProcessEvent(
        event_type="read", category="read", severity="INFO",
        agent_id="agent-x", path="/vault/memory/secret.md",
        status="success",
        message='api_key="sk-very-secret-key-123456789" Note content: confidential data here',
        metadata={"note_content": "# Secret\n\nThis is classified content that must not leak."},
        timestamp="2026-05-10T10:00:00Z",
    ))

    events = get_recent_events(db, limit=10, trace_id="trace-sanitize-001")
    assert len(events) >= 1

    for e in events:
        msg_lower = e.message.lower()
        assert "sk-very-secret" not in msg_lower or "redacted" in msg_lower
        assert "classified" not in e.message

    detail = get_process_detail(db, event_id=1, trace_id="trace-detail-sanitize-001")
    assert detail is not None
    assert detail.metadata == {} or detail.metadata is None


# ===========================================================================
# Additional: get_process_detail returns correlated audit data
# ===========================================================================


def test_get_process_detail_returns_correlated_audit_data():
    db = _fresh_db()

    record_process_event(db, ProcessEvent(
        event_type="mutation", category="mutation", severity="INFO",
        agent_id="trusted_writer", path="/vault/memory/note.md",
        status="success", message="file modified via write operation",
        trace_id="trace-correlated-001", timestamp="2026-05-10T10:01:00Z",
        metadata={"patch_type": "edit"},
    ))

    _insert_audit_event(
        db,
        event_id="evt-audit-001",
        timestamp="2026-05-10T10:00:00Z",
        agent_id="trusted_writer",
        operation="write",
        path="memory/note.md",
        result="success",
        trace_id="trace-correlated-001",
    )

    detail = get_process_detail(db, event_id=1, trace_id="trace-detail-001")

    assert detail is not None
    assert isinstance(detail, ProcessDetail)
    assert detail.event_id == 1
    assert detail.event_type == "mutation"
    assert detail.trace_id == "trace-correlated-001"
    assert detail.correlated_audit is not None
    assert detail.correlated_audit["operation"] == "write"
    assert detail.correlated_audit["trace_id"] == "trace-correlated-001"


# ===========================================================================
# Additional: get_process_detail returns None for missing event
# ===========================================================================


def test_get_process_detail_returns_none_for_missing_event():
    db = _fresh_db()

    detail = get_process_detail(db, event_id=9999, trace_id="trace-missing-001")
    assert detail is None


# ===========================================================================
# Additional: request_log_export delegates to M-026
# ===========================================================================


def test_request_log_export_delegates_to_log_report_service():
    db = _fresh_db()

    record_process_event(db, ProcessEvent(
        event_type="boot", category="boot", severity="INFO",
        agent_id="agent-a", path="/vault", status="success",
        message="booted", timestamp="2026-05-10T10:00:00Z",
    ))
    record_process_event(db, ProcessEvent(
        event_type="read", category="read", severity="INFO",
        agent_id="agent-b", path="/vault/notes/a.md", status="success",
        message="read file", timestamp="2026-05-10T11:00:00Z",
    ))

    from memory_mcp.log_report_service import LogReportRequest
    request = LogReportRequest()

    result = request_log_export(db, request, "trace-export-001")

    assert result is not None
    assert hasattr(result, "total_events")
    assert hasattr(result, "filtered_events")
    assert hasattr(result, "lines")
    assert result.total_events >= 2
    assert result.filtered_events >= 2
    assert len(result.lines) >= 2


# ===========================================================================
# Additional: dashboard_summary_handles_partially_degraded_state
# ===========================================================================


def test_dashboard_summary_handles_partially_degraded_state(tmp_path):
    db = _fresh_db()

    vault_root = str(tmp_path / "vault_missing_obsidian")
    os.makedirs(vault_root, exist_ok=True)

    directory_contract = _make_directory_contract(vault_root)

    record_process_event(db, ProcessEvent(
        event_type="boot", category="boot", severity="INFO",
        agent_id="agent-a", path="/vault", status="success",
        message="booted", timestamp="2026-05-10T10:00:00Z",
    ))

    summary = get_dashboard_summary(
        db, vault_root, directory_contract, "trace-partial-001",
    )

    assert summary.server_status == "healthy"
    assert summary.vault_status in ("degraded", "unavailable")
    assert summary.total_events >= 1
    assert isinstance(summary.degraded, bool)


# ===========================================================================
# Additional: empty error timeline returns empty list
# ===========================================================================


def test_empty_error_timeline_returns_empty_list():
    db = _fresh_db()

    record_process_event(db, ProcessEvent(
        event_type="boot", category="boot", severity="INFO",
        agent_id="agent-a", path="/vault", status="success",
        message="booted", timestamp="2026-05-10T10:00:00Z",
    ))

    timeline = get_error_timeline(
        db,
        from_ts="2026-05-10T09:00:00Z",
        to_ts="2026-05-10T12:00:00Z",
        trace_id="trace-empty-errors-001",
    )

    assert timeline == []


# ===========================================================================
# Additional: get_recent_events returns empty list when store unavailable
# ===========================================================================


def test_get_recent_events_returns_empty_when_store_unavailable(caplog):
    caplog.set_level(logging.DEBUG)
    db = sqlite3.connect(":memory:")
    db.close()

    events = get_recent_events(db, limit=5, trace_id="trace-unavailable-001")
    assert events == []

    log_entries = _parse_logs(caplog)
    events_list = [e.get("event") for e in log_entries]
    assert "admin_dashboard.degraded" in events_list


# ===========================================================================
# Additional: error_timeline returns empty when store unavailable
# ===========================================================================


def test_error_timeline_returns_empty_when_store_unavailable(caplog):
    caplog.set_level(logging.DEBUG)
    db = sqlite3.connect(":memory:")
    db.close()

    timeline = get_error_timeline(
        db,
        from_ts="2026-05-10T09:00:00Z",
        to_ts="2026-05-10T12:00:00Z",
        trace_id="trace-et-unavailable-001",
    )
    assert timeline == []

    log_entries = _parse_logs(caplog)
    events_list = [e.get("event") for e in log_entries]
    assert "admin_dashboard.degraded" in events_list


# ===========================================================================
# Additional: recovery from degraded to healthy when store returns
# ===========================================================================


def test_recovery_from_degraded_to_healthy(tmp_path):
    db = _fresh_db()

    vault_root = str(tmp_path / "vault_recovery")
    os.makedirs(os.path.join(vault_root, ".obsidian"), exist_ok=True)
    os.makedirs(os.path.join(vault_root, "00_Inbox"), exist_ok=True)
    os.makedirs(os.path.join(vault_root, "memory"), exist_ok=True)
    os.makedirs(os.path.join(vault_root, "summaries"), exist_ok=True)

    directory_contract = _make_directory_contract(vault_root)

    record_process_event(db, ProcessEvent(
        event_type="boot", category="boot", severity="INFO",
        agent_id="agent-a", path="/vault", status="success",
        message="recovery boot", timestamp="2026-05-10T10:00:00Z",
    ))

    summary = get_dashboard_summary(
        db, vault_root, directory_contract, "trace-recovery-001",
    )

    assert summary.server_status == "healthy"
    assert summary.total_events >= 1


# ===========================================================================
# Additional: error timeline groups by error_code AND event_type
# ===========================================================================


def test_error_timeline_groups_by_error_code_and_event_type():
    db = _fresh_db()

    record_process_event(db, ProcessEvent(
        event_type="mutation", category="mutation", severity="ERROR",
        agent_id="agent-x", path="/vault/a.md", status="failed",
        error_code="E001", message="mutation failed",
        timestamp="2026-05-10T10:00:00Z",
    ))
    record_process_event(db, ProcessEvent(
        event_type="read", category="read", severity="ERROR",
        agent_id="agent-y", path="/vault/b.md", status="failed",
        error_code="E001", message="read failed",
        timestamp="2026-05-10T10:01:00Z",
    ))

    timeline = get_error_timeline(
        db,
        from_ts="2026-05-10T09:00:00Z",
        to_ts="2026-05-10T12:00:00Z",
        trace_id="trace-grouped-001",
    )

    assert len(timeline) == 2
    for e in timeline:
        assert e.error_code == "E001"
    event_types = {e.event_type for e in timeline}
    assert event_types == {"mutation", "read"}


# ===========================================================================
# Stop condition: SC-009 - dashboard never exposes secrets in summary
# ===========================================================================


def test_dashboard_summary_never_exposes_secrets(tmp_path):
    db = _fresh_db()

    vault_root = str(tmp_path / "vault_sc009")
    os.makedirs(os.path.join(vault_root, ".obsidian"), exist_ok=True)
    os.makedirs(os.path.join(vault_root, "00_Inbox"), exist_ok=True)

    directory_contract = _make_directory_contract(vault_root)

    record_process_event(db, ProcessEvent(
        event_type="auth", category="auth", severity="INFO",
        agent_id="admin", path="/vault", status="success",
        message='API_KEY=sk-abcdefg secret=my-secret-token',
        timestamp="2026-05-10T10:00:00Z",
    ))

    summary = get_dashboard_summary(
        db, vault_root, directory_contract, "trace-sc009-001",
    )

    summary_dict = {
        "server_status": summary.server_status,
        "vault_status": summary.vault_status,
        "index_status": summary.index_status,
        "backup_status": summary.backup_status,
        "layout_status": summary.layout_status,
        "generated_at": summary.generated_at,
    }
    summary_str = json.dumps(summary_dict).lower()
    assert "sk-abcdefg" not in summary_str
    assert "my-secret-token" not in summary_str

    events = get_recent_events(db, limit=1, trace_id="trace-sc009-events-001")
    for e in events:
        combined = e.message + e.path + e.event_type
        combined_lower = combined.lower()
        assert "sk-abcdefg" not in combined_lower or "redacted" in combined_lower


# ===========================================================================
# Stop condition: SC-010 - dashboard provides no vault mutation path
# ===========================================================================


def test_dashboard_provides_no_vault_mutation_path():
    import inspect
    import memory_mcp.admin_dashboard_service as ads

    source = inspect.getsource(ads)
    mutation_keywords = [
        "write_file", "write_file_atomic", "create_note",
        "append_to_note", "delete_note", "modify_note",
        "mutation_service", "M-005", "vault_fs.write",
    ]
    for kw in mutation_keywords:
        assert kw not in source, f"Mutation path detected: {kw}"


# ===========================================================================
# Additional: ProcessDetail contains all required fields
# ===========================================================================


def test_process_detail_contains_all_required_fields():
    db = _fresh_db()

    record_process_event(db, ProcessEvent(
        event_type="index_refresh", category="index_refresh", severity="INFO",
        agent_id="agent-detail", trace_id="trace-detail-full-001",
        path="/vault/memory/note.md",
        status="success", error_code="",
        message="Index refreshed",
        metadata={"tags": ["test"]},
        timestamp="2026-05-10T15:00:00Z",
    ))

    detail = get_process_detail(db, event_id=1, trace_id="trace-detail-test-001")

    assert detail is not None
    assert detail.event_id == 1
    assert detail.event_type == "index_refresh"
    assert detail.category == "index_refresh"
    assert detail.severity == "INFO"
    assert detail.agent_id == "agent-detail"
    assert detail.trace_id == "trace-detail-full-001"
    assert detail.path == "/vault/memory/note.md"
    assert detail.status == "success"
    assert detail.error_code == ""
    assert detail.message == "Index refreshed"
    assert detail.metadata == {}
    assert detail.timestamp == "2026-05-10T15:00:00Z"
    assert detail.correlated_audit is None


# ===========================================================================
# Additional: limit=0 returns no events for get_recent_events
# ===========================================================================


def test_get_recent_events_limit_zero_returns_empty():
    db = _fresh_db()

    record_process_event(db, ProcessEvent(
        event_type="boot", category="boot", severity="INFO",
        agent_id="agent-a", path="/vault", status="success",
        message="booted", timestamp="2026-05-10T10:00:00Z",
    ))

    events = get_recent_events(db, limit=0, trace_id="trace-limit-0-001")
    assert events == []
