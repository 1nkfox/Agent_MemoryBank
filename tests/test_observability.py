"""Module-local tests for M-012 ObservabilityService."""

import json
import logging
import uuid

from memory_mcp.observability import (
    ErrorCode,
    log_trace_anchor,
    new_trace_id,
)


def _parse_logs(caplog) -> list[dict]:
    entries = []
    for record in caplog.records:
        try:
            entries.append(json.loads(record.message))
        except json.JSONDecodeError:
            pass
    return entries


def _by_event(entries: list[dict], event: str) -> dict:
    for entry in entries:
        if entry.get("event") == event:
            return entry
    raise AssertionError(f"Log entry with event '{event}' not found in {entries}")


def test_new_trace_id_is_unique_and_uuid_format():
    tid1 = new_trace_id()
    tid2 = new_trace_id()

    assert tid1 != tid2

    uuid.UUID(tid1)  # raises ValueError if not valid UUID
    uuid.UUID(tid2)


def test_log_trace_anchor_emits_structured_json(caplog):
    caplog.set_level(logging.DEBUG)

    trace_id = new_trace_id()
    log_trace_anchor(
        level="INFO",
        event="test.event",
        trace_id=trace_id,
        module="TestModule",
        function="testFn",
        block="TEST_BLOCK",
    )

    entries = _parse_logs(caplog)
    assert len(entries) == 1
    entry = entries[0]

    assert "timestamp" in entry
    assert entry["level"] == "INFO"
    assert entry["trace_id"] == trace_id
    assert entry["module"] == "TestModule"
    assert entry["function"] == "testFn"
    assert entry["block"] == "TEST_BLOCK"
    assert entry["event"] == "test.event"


def test_log_trace_anchor_includes_data_and_error_code(caplog):
    caplog.set_level(logging.DEBUG)

    trace_id = new_trace_id()
    log_trace_anchor(
        level="ERROR",
        event="auth.failure",
        trace_id=trace_id,
        module="AuthService",
        function="verifyKey",
        block="AUTH_CHECK",
        data={"attempt": 3, "key_id": "k-abc"},
        error_code=ErrorCode.AUTH_INVALID_KEY,
    )

    entries = _parse_logs(caplog)
    entry = entries[0]

    assert entry["level"] == "ERROR"
    assert entry["error_code"] == "AUTH_INVALID_KEY"
    assert entry["data"] == {"attempt": 3, "key_id": "k-abc"}


def test_error_codes_cover_auth_policy_fs_patch_revision_audit_failures():
    expected = {
        "AUTH_INVALID_KEY",
        "AUTH_UNKNOWN_KEY",
        "POLICY_DENIED",
        "FS_PATH_TRAVERSAL",
        "FS_SYMLINK_DENIED",
        "PATCH_INVALID",
        "PATCH_UNSUPPORTED",
        "REVISION_MISMATCH",
        "LOCK_TIMEOUT",
        "AUDIT_WRITE_FAILED",
        "INDEX_CORRUPT",
        "CONFIG_INVALID",
    }
    actual = {e.name for e in ErrorCode}
    missing = expected - actual
    extra = actual - expected
    assert not missing, f"Missing error codes: {missing}"
    assert not extra, f"Extra error codes: {extra}"
    assert len(ErrorCode) == 12


def test_redaction_removes_api_key_from_log(caplog):
    caplog.set_level(logging.DEBUG)

    trace_id = new_trace_id()
    raw_key = "sk-secret-value-1234"
    log_trace_anchor(
        level="INFO",
        event="api.call",
        trace_id=trace_id,
        module="TestModule",
        function="testFn",
        block="API_CALL",
        data={"api_key": raw_key},
    )

    entries = _parse_logs(caplog)
    entry = entries[0]

    raw_log_text = json.dumps(entry)
    assert raw_key not in raw_log_text
    assert "sk-s...1234" in raw_log_text
    assert entry["data"]["api_key"] == "sk-s...1234"


def test_redaction_handles_short_values(caplog):
    caplog.set_level(logging.DEBUG)

    trace_id = new_trace_id()
    log_trace_anchor(
        level="INFO",
        event="test.event",
        trace_id=trace_id,
        module="TestModule",
        function="testFn",
        block="TEST",
        data={"key": "xy"},
    )

    entries = _parse_logs(caplog)
    entry = entries[0]

    assert entry["data"]["key"] == "<redacted>"


def test_redaction_handles_multiple_sensitive_keys(caplog):
    caplog.set_level(logging.DEBUG)

    trace_id = new_trace_id()
    log_trace_anchor(
        level="INFO",
        event="test.event",
        trace_id=trace_id,
        module="TestModule",
        function="testFn",
        block="TEST",
        data={
            "api_key": "sk-abcdef1234567890",
            "secret": "my-secret-value-here",
            "token": "bearer-token-data",
            "password": "super-secret-pw",
            "credential": "cred-value-1234",
            "safe_field": "visible-value",
            "nested": {"inner": "data"},
        },
    )

    entries = _parse_logs(caplog)
    entry = entries[0]
    data = entry["data"]

    assert data["api_key"] == "sk-a...7890"
    assert data["secret"] == "my-s...here"
    assert data["token"] == "bear...data"
    assert data["password"] == "supe...t-pw"
    assert data["credential"] == "cred...1234"
    assert data["safe_field"] == "visible-value"
    assert data["nested"] == {"inner": "data"}
