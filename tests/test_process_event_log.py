"""Module-local tests for M-025 ProcessEventLogService."""

from __future__ import annotations

import json
import logging
import sqlite3

import memory_mcp.process_event_log as pel
from memory_mcp.process_event_log import (
    ProcessEvent,
    ProcessEventFilter,
    correlate_by_trace_id,
    query_process_events,
    record_process_event,
)


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


# -- M-025-S-001: records_process_event_with_required_fields ----------------------------------


def test_records_process_event_with_required_fields(caplog, trace_assert):
    caplog.set_level(logging.DEBUG)
    db = _fresh_db()

    event = ProcessEvent(
        event_type="boot",
        category="boot",
        severity="INFO",
        agent_id="trusted_writer",
        trace_id="trace-001",
        path="/vault",
        status="success",
        message="System boot completed",
        timestamp="2026-05-10T10:00:00Z",
    )
    record_process_event(db, event)

    results = query_process_events(db, ProcessEventFilter())
    assert len(results) >= 1

    stored = results[0]
    assert stored.event_type == "boot"
    assert stored.category == "boot"
    assert stored.severity == "INFO"
    assert stored.agent_id == "trusted_writer"
    assert stored.trace_id == "trace-001"
    assert stored.path == "/vault"
    assert stored.status == "success"
    assert stored.message == "System boot completed"

    log_entries = _parse_logs(caplog)
    assert trace_assert(log_entries, "process_event.recorded")  # TA-035


# -- M-025-S-002: query_filters_by_time_severity_category_path_agent_trace --------------------


def test_query_filters_by_time_severity_category_path_agent_trace(caplog, trace_assert):
    caplog.set_level(logging.DEBUG)
    db = _fresh_db()

    record_process_event(db, ProcessEvent(
        event_type="boot", category="boot", severity="INFO",
        agent_id="agent-a", path="/vault", message="booted",
        timestamp="2026-05-10T10:00:00Z",
    ))
    record_process_event(db, ProcessEvent(
        event_type="error", category="error", severity="ERROR",
        agent_id="agent-b", path="/vault/notes", message="failed",
        timestamp="2026-05-10T11:00:00Z",
    ))
    record_process_event(db, ProcessEvent(
        event_type="mutation", category="mutation", severity="WARN",
        agent_id="agent-a", path="/vault/notes/file.md", message="changed",
        timestamp="2026-05-10T12:00:00Z",
    ))

    by_severity = query_process_events(db, ProcessEventFilter(severity="ERROR"))
    assert len(by_severity) == 1
    assert by_severity[0].severity == "ERROR"

    by_category = query_process_events(db, ProcessEventFilter(category="mutation"))
    assert len(by_category) == 1
    assert by_category[0].category == "mutation"

    by_agent = query_process_events(db, ProcessEventFilter(agent_id="agent-a"))
    assert len(by_agent) == 2

    by_path_prefix = query_process_events(db, ProcessEventFilter(path_prefix="/vault/notes"))
    assert len(by_path_prefix) == 2
    assert all(r.path.startswith("/vault/notes") for r in by_path_prefix)

    by_time = query_process_events(db, ProcessEventFilter(
        from_timestamp="2026-05-10T10:30:00Z",
        to_timestamp="2026-05-10T11:30:00Z",
    ))
    assert len(by_time) == 1
    assert by_time[0].severity == "ERROR"

    log_entries = _parse_logs(caplog)
    assert trace_assert(log_entries, "process_event.query.completed")  # TA-036


# -- M-025-S-003: records_file_change_and_sync_events -----------------------------------------


def test_records_file_change_and_sync_events():
    db = _fresh_db()

    record_process_event(db, ProcessEvent(
        event_type="file_change", category="file_change", severity="INFO",
        agent_id="agent-c", path="/vault/memory/note.md", status="success",
        message="file modified", timestamp="2026-05-10T13:00:00Z",
        metadata={"old_hash": "abc123", "new_hash": "def456"},
    ))
    record_process_event(db, ProcessEvent(
        event_type="sync_external_change", category="sync_external_change", severity="INFO",
        agent_id="agent-c", path="/vault/memory/note.md", status="success",
        message="sync completed", timestamp="2026-05-10T13:01:00Z",
        metadata={"source": "external"},
    ))

    all_events = query_process_events(db, ProcessEventFilter())
    assert len(all_events) == 2
    types = {e.event_type for e in all_events}
    assert "file_change" in types
    assert "sync_external_change" in types

    fc = [e for e in all_events if e.event_type == "file_change"][0]
    assert fc.metadata.get("old_hash") == "abc123"
    assert fc.metadata.get("new_hash") == "def456"


# -- M-025-F-001: secrets_raw_content_and_denylisted_details_redacted -------------------------


def test_secrets_raw_content_and_denylisted_details_redacted(caplog, trace_assert):
    caplog.set_level(logging.DEBUG)
    db = _fresh_db()

    event = ProcessEvent(
        event_type="auth", category="auth", severity="INFO",
        agent_id="agent-d", trace_id="trace-redact-001", path="/vault",
        status="success",
        message='API call with api_key=sk-very-secret-key-12345abc and authorization=Bearer token123456789',
        metadata={
            "api_key": "sk-should-not-appear-plain-text",
            "password": "admin1234",
            "Authorization": "Bearer sensitive-data-here",
            "note_content": "# Confidential\n\nThis is very secret content that should not appear in raw form.",
        },
        timestamp="2026-05-10T14:00:00Z",
    )
    record_process_event(db, event)

    results = query_process_events(db, ProcessEventFilter())
    assert len(results) == 1
    stored = results[0]

    stored_lower = stored.message.lower()
    assert "sk-very-secret" not in stored_lower or "redacted" in stored_lower
    assert "token123456789" not in stored_lower or "redacted" in stored_lower
    assert "sk-should-not-appear-plain-text" not in json.dumps(stored.metadata).lower()

    log_entries = _parse_logs(caplog)
    events_list = [e.get("event") for e in log_entries]
    assert "process_event.redacted" in events_list  # TA-037


# -- Additional: Empty filter returns all events ----------------------------------------------


def test_empty_query_returns_all_events():
    db = _fresh_db()

    for i in range(5):
        record_process_event(db, ProcessEvent(
            event_type="mcp_request", category="mcp_request", severity="INFO",
            agent_id=f"agent-{i}", path=f"/vault/note_{i}.md",
            message=f"request {i}", timestamp=f"2026-05-10T1{i}:00:00Z",
        ))

    results = query_process_events(db, ProcessEventFilter())
    assert len(results) == 5


# -- Additional: Multiple filter criteria combined (AND logic) --------------------------------


def test_multiple_filter_criteria_combined_and_logic():
    db = _fresh_db()

    record_process_event(db, ProcessEvent(
        event_type="mutation", category="mutation", severity="INFO",
        agent_id="agent-x", path="/vault/a.md", message="m1",
        timestamp="2026-05-10T10:00:00Z",
    ))
    record_process_event(db, ProcessEvent(
        event_type="mutation", category="mutation", severity="ERROR",
        agent_id="agent-x", path="/vault/b.md", message="m2",
        timestamp="2026-05-10T11:00:00Z",
    ))
    record_process_event(db, ProcessEvent(
        event_type="read", category="read", severity="ERROR",
        agent_id="agent-x", path="/vault/c.md", message="m3",
        timestamp="2026-05-10T12:00:00Z",
    ))

    results = query_process_events(db, ProcessEventFilter(
        severity="ERROR",
        agent_id="agent-x",
        category="mutation",
    ))
    assert len(results) == 1
    assert results[0].message == "m2"

    results2 = query_process_events(db, ProcessEventFilter(
        from_timestamp="2026-05-10T09:00:00Z",
        to_timestamp="2026-05-10T11:30:00Z",
        category="mutation",
    ))
    assert len(results2) == 2


# -- Additional: correlate_by_trace_id groups events correctly --------------------------------


def test_correlate_by_trace_id_groups_events_correctly():
    db = _fresh_db()

    trace_a = "corr-trace-aaa"
    trace_b = "corr-trace-bbb"

    for i in range(3):
        record_process_event(db, ProcessEvent(
            event_type="read", category="read", severity="INFO",
            agent_id="agent", trace_id=trace_a, path=f"/vault/a_{i}.md",
            message=f"read a{i}", timestamp=f"2026-05-10T1{i}:00:00Z",
        ))
    for i in range(2):
        record_process_event(db, ProcessEvent(
            event_type="mutation", category="mutation", severity="INFO",
            agent_id="agent", trace_id=trace_b, path=f"/vault/b_{i}.md",
            message=f"mutate b{i}", timestamp=f"2026-05-10T2{i}:00:00Z",
        ))

    correlated_a = correlate_by_trace_id(db, trace_a)
    assert len(correlated_a) == 3
    assert all(e.trace_id == trace_a for e in correlated_a)

    correlated_b = correlate_by_trace_id(db, trace_b)
    assert len(correlated_b) == 2
    assert all(e.trace_id == trace_b for e in correlated_b)

    correlated_none = correlate_by_trace_id(db, "nonexistent")
    assert len(correlated_none) == 0


# -- Additional: Large event payloads stored and retrieved correctly --------------------------


def test_large_event_payloads_stored_and_retrieved():
    db = _fresh_db()

    large_metadata = {
        "tags": ["tag"] * 50,
        "nested": {f"key_{i}": f"value_{i}" for i in range(100)},
    }
    large_message = "Record of operation " + "x" * 500

    event = ProcessEvent(
        event_type="index_refresh", category="index_refresh", severity="INFO",
        agent_id="agent-l", trace_id="large-trace-001", path="/vault/large.md",
        status="success", message=large_message, metadata=large_metadata,
        timestamp="2026-05-10T15:00:00Z",
    )
    record_process_event(db, event)

    results = query_process_events(db, ProcessEventFilter())
    assert len(results) == 1
    stored = results[0]

    assert stored.message == large_message
    assert len(stored.metadata["tags"]) == 50
    assert len(stored.metadata["nested"]) == 100
    assert stored.metadata["nested"]["key_50"] == "value_50"


# -- Additional: Various event categories ----------------------------------------------------


def test_all_event_categories_recorded():
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
            message=f"Event for {cat}", timestamp=f"2026-05-10T1{i:02d}:00:00Z",
        ))

    results = query_process_events(db, ProcessEventFilter())
    assert len(results) == len(categories)

    stored_cats = {e.category for e in results}
    assert stored_cats == set(categories)


# -- Additional: verify no audit_log import --------------------------------------------------


def test_no_audit_log_dependency():
    import inspect
    source = inspect.getsource(pel)
    assert "audit_log" not in source
    assert "M-009" not in source
