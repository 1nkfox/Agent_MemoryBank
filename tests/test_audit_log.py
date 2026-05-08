"""Module-local tests for M-009 AuditLogService."""

import json
import logging
from pathlib import Path

import pytest

from memory_mcp.audit_log import AuditEvent, record_event, update_log_projection
from memory_mcp.config import load_config


def _parse_logs(caplog) -> list[dict]:
    entries: list[dict] = []
    for record in caplog.records:
        try:
            entries.append(json.loads(record.message))
        except json.JSONDecodeError:
            pass
    return entries


def _make_config(vault_root: Path, sample_config_dict: dict):
    cfg_dict = {
        **sample_config_dict,
        "vault": {**sample_config_dict["vault"], "root": str(vault_root)},
    }
    return load_config(cfg_dict)


def _full_event_data(**overrides):
    base = {
        "event_id": "evt-001",
        "timestamp": "2025-01-01T00:00:00Z",
        "agent_id": "test-agent",
        "operation": "write",
        "path": "memory/note.md",
        "old_revision": "abc123",
        "new_revision": "def456",
        "dry_run": False,
        "policy_profile": "trusted_writer",
        "result": "success",
        "affected_paths": ["memory/note.md"],
        "trace_id": "trace-001",
    }
    base.update(overrides)
    return base


class TestAuditLogService:

    @pytest.mark.asyncio
    async def test_record_event_writes_authoritative_audit(
        self, temp_vault_root, caplog, trace_assert, sample_config_dict
    ):
        """ET-DET ET-TRACE: TA-008 emitted; audit row has all 12 fields."""
        caplog.set_level(logging.DEBUG)
        config = _make_config(temp_vault_root, sample_config_dict)

        event_data = _full_event_data()
        event = await record_event(event_data, config)

        assert event.event_id == "evt-001"
        assert event.agent_id == "test-agent"
        assert event.operation == "write"
        assert event.path == "memory/note.md"

        audit_file = temp_vault_root / ".obsidian" / "audit.ndjson"
        assert audit_file.exists()
        raw = audit_file.read_text(encoding="utf-8")
        lines = [ln for ln in raw.strip().split("\n") if ln.strip()]
        assert len(lines) == 1

        parsed = json.loads(lines[0])
        assert parsed["event_id"] == "evt-001"
        assert parsed["timestamp"] == "2025-01-01T00:00:00Z"
        assert parsed["agent_id"] == "test-agent"
        assert parsed["operation"] == "write"
        assert parsed["path"] == "memory/note.md"
        assert parsed["old_revision"] == "abc123"
        assert parsed["new_revision"] == "def456"
        assert parsed["dry_run"] is False
        assert parsed["policy_profile"] == "trusted_writer"
        assert parsed["result"] == "success"
        assert parsed["affected_paths"] == ["memory/note.md"]
        assert parsed["trace_id"] == "trace-001"

        entries = _parse_logs(caplog)
        trace_assert(entries, "audit.authoritative_event.appended")

        ta_entry = next(e for e in entries if e.get("event") == "audit.authoritative_event.appended")
        assert ta_entry["level"] == "INFO"
        assert ta_entry["function"] == "record_event"
        assert ta_entry["block"] == "M-009"
        assert ta_entry["data"]["event_id"] == "evt-001"

    @pytest.mark.asyncio
    async def test_update_log_projection_appends_to_LOG_md(
        self, temp_vault_root, caplog, trace_assert, sample_config_dict
    ):
        """ET-DET ET-TRACE: TA-009 emitted; LOG.md contains new line."""
        caplog.set_level(logging.DEBUG)
        config = _make_config(temp_vault_root, sample_config_dict)

        event = AuditEvent(**_full_event_data())

        await update_log_projection(event, config)

        log_file = temp_vault_root / "LOG.md"
        assert log_file.exists()
        content = log_file.read_text(encoding="utf-8")
        assert "test-agent" in content
        assert "write" in content
        assert "memory/note.md" in content
        assert "→ success" in content

        entries = _parse_logs(caplog)
        trace_assert(entries, "audit.log_projection.updated")

        ta_entry = next(e for e in entries if e.get("event") == "audit.log_projection.updated")
        assert ta_entry["level"] == "INFO"
        assert ta_entry["function"] == "update_log_projection"
        assert ta_entry["block"] == "M-009"
        assert ta_entry["data"]["event_id"] == "evt-001"

    @pytest.mark.asyncio
    async def test_no_recursive_audit_loop(
        self, temp_vault_root, caplog, sample_config_dict
    ):
        """ET-DET: recording LOG.md update does not trigger another audit for LOG.md."""
        caplog.set_level(logging.DEBUG)
        config = _make_config(temp_vault_root, sample_config_dict)

        event = AuditEvent(**_full_event_data())
        await update_log_projection(event, config)

        log_file = temp_vault_root / "LOG.md"
        assert log_file.exists()
        assert "memory/note.md" in log_file.read_text(encoding="utf-8")

        audit_file = temp_vault_root / ".obsidian" / "audit.ndjson"
        if audit_file.exists():
            content = audit_file.read_text(encoding="utf-8")
            assert "LOG.md" not in content, (
                "Audit loop detected: LOG.md write was recorded as an audit event"
            )

        entries = _parse_logs(caplog)
        for entry in entries:
            if entry.get("event") == "audit.authoritative_event.appended":
                assert entry.get("data", {}).get("path") != "LOG.md"

    @pytest.mark.asyncio
    async def test_mutation_without_audit_event_is_impossible(
        self, temp_vault_root, sample_config_dict
    ):
        """ET-DET: M-008 always calls record_event; incomplete data rejected."""
        config = _make_config(temp_vault_root, sample_config_dict)

        incomplete = {"agent_id": "test-agent"}
        with pytest.raises(ValueError, match="Missing required audit fields"):
            await record_event(incomplete, config)
