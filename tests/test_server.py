"""Module-local tests for M-001 MCPServerEntrypoint."""

import json
import logging

import pytest

from memory_mcp.config import load_config
from memory_mcp.server import ToolResult, handle_tool_call, register_tool

READONLY_KEY = "test-key-readonly-001"


def _parse_logs(caplog):
    entries = []
    for record in caplog.records:
        try:
            entries.append(json.loads(record.message))
        except (json.JSONDecodeError, TypeError):
            pass
    return entries


def _make_config(temp_vault_root, sample_config_dict):
    config_dict = dict(sample_config_dict)
    config_dict["vault"]["root"] = str(temp_vault_root)
    return load_config(config_dict)


class TestToolRoutingAuthPolicyBeforeService:
    def test_tool_routing_applies_auth_policy_before_service(
        self, temp_vault_root, sample_config_dict, caplog, trace_assert
    ):
        caplog.set_level(logging.DEBUG)
        config = _make_config(temp_vault_root, sample_config_dict)

        handle_tool_call("read_note", READONLY_KEY, {"path": "memory/note.md"}, config)

        entries = _parse_logs(caplog)
        assert trace_assert(
            entries,
            "auth.identity.resolved",
            "policy.decision",
            "request.completed",
        )


class TestTraceIdEmission:
    def test_emits_trace_id_on_every_request(
        self, temp_vault_root, sample_config_dict, caplog
    ):
        caplog.set_level(logging.DEBUG)
        config = _make_config(temp_vault_root, sample_config_dict)

        result = handle_tool_call(
            "read_note", READONLY_KEY, {"path": "memory/note.md"}, config
        )

        assert isinstance(result, ToolResult)
        assert result.trace_id != ""

        entries = _parse_logs(caplog)
        server_entries = [
            e for e in entries if e.get("module") == "memory_mcp.server"
        ]
        assert len(server_entries) >= 1

        for entry in server_entries:
            assert "trace_id" in entry, (
                f"Server log entry missing trace_id: {entry}"
            )
            assert entry["trace_id"] == result.trace_id


class TestUnknownToolError:
    def test_unknown_tool_returns_error_with_error_code(
        self, temp_vault_root, sample_config_dict
    ):
        config = _make_config(temp_vault_root, sample_config_dict)

        result = handle_tool_call("nonexistent_tool", READONLY_KEY, {}, config)

        assert isinstance(result, ToolResult)
        assert result.success is False
        assert result.error_code == "UNKNOWN_TOOL"
        assert result.trace_id != ""


class TestReadNoteToolResult:
    def test_read_note_tool_returns_content(
        self, temp_vault_root, sample_config_dict
    ):
        config = _make_config(temp_vault_root, sample_config_dict)

        result = handle_tool_call(
            "read_note", READONLY_KEY, {"path": "memory/note.md"}, config
        )

        assert isinstance(result, ToolResult)
        assert result.success is True
        assert result.trace_id != ""

        data = result.data
        assert isinstance(data, dict)
        assert "content" in data
        assert "revision" in data
        assert isinstance(data["content"], str)
        assert isinstance(data["revision"], str)
        assert len(data["content"]) > 0
        assert len(data["revision"]) > 0
