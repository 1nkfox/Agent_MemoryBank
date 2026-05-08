"""Module-local tests for M-001 MCPServerEntrypoint."""

import json
import logging

import memory_mcp.server as server
from memory_mcp.config import load_config
from memory_mcp.server import ToolResult, handle_tool_call, health_check, tools

READONLY_KEY = "test-key-readonly-001"
ADMIN_KEY = "test-key-admin-001"


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
        self, temp_vault_root, sample_config_dict, caplog
    ):
        caplog.set_level(logging.DEBUG)
        config = _make_config(temp_vault_root, sample_config_dict)

        result = handle_tool_call("nonexistent_tool", READONLY_KEY, {}, config)

        assert isinstance(result, ToolResult)
        assert result.success is False
        assert result.error_code == "UNKNOWN_TOOL"
        assert result.trace_id != ""
        assert "request.failed" in [entry.get("event") for entry in _parse_logs(caplog)]


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


class TestAdminOpsTools:
    def test_admin_tools_are_registered(self):
        assert "backup_vault" in tools
        assert "backup_health" in tools
        assert "promote_note" in tools

    def test_backup_health_tool_returns_health_data(
        self, monkeypatch, temp_vault_root, sample_config_dict
    ):
        config = _make_config(temp_vault_root, sample_config_dict)

        monkeypatch.setattr(
            server,
            "_svc_backup_health",
            lambda config: {
                "git_available": True,
                "repo_exists": True,
                "healthy": True,
            },
        )

        result = handle_tool_call("backup_health", ADMIN_KEY, {}, config)

        assert isinstance(result, ToolResult)
        assert result.success is True
        assert result.data == {
            "git_available": True,
            "repo_exists": True,
            "healthy": True,
        }

    def test_admin_tool_auth_failure_logs_request_failed(
        self, temp_vault_root, sample_config_dict, caplog
    ):
        caplog.set_level(logging.DEBUG)
        config = _make_config(temp_vault_root, sample_config_dict)

        result = handle_tool_call("backup_health", "unknown-admin-key", {}, config)

        assert result.success is False
        assert result.error_code == "AUTH_UNKNOWN_KEY"
        assert "request.failed" in [entry.get("event") for entry in _parse_logs(caplog)]

    def test_promote_note_tool_dry_run_returns_affected_paths(
        self, temp_vault_root, sample_config_dict
    ):
        config = _make_config(temp_vault_root, sample_config_dict)
        source = "inbox/draft.md"
        dest = "memory/promoted.md"

        result = handle_tool_call(
            "promote_note",
            ADMIN_KEY,
            {"source_path": source, "dest_path": dest},
            config,
        )

        assert result.success is True
        assert result.data["dry_run"] is True
        assert result.data["affected_paths"] == [source, dest]
        assert (temp_vault_root / source).exists()
        assert not (temp_vault_root / dest).exists()

    def test_health_check_includes_admin_ops_modules(
        self, temp_vault_root, sample_config_dict
    ):
        config = _make_config(temp_vault_root, sample_config_dict)

        result = health_check(config)

        assert "backup_service" in result["modules"]
        assert "draft_promotion_service" in result["modules"]
