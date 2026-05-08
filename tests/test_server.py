"""Module-local tests for M-001 MCPServerEntrypoint."""

import json
import logging

import memory_mcp.server as server
from memory_mcp.config import load_config
from memory_mcp.server import ToolResult, handle_tool_call, health_check, tools

READONLY_KEY = "test-key-readonly-001"
TRUSTED_KEY = "test-key-trusted-001"
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
    config_dict["index"]["db_path"] = str(temp_vault_root / ".obsidian" / "index.db")
    config_dict["audit"]["audit_db_path"] = str(temp_vault_root / ".obsidian" / "audit.db")
    config_dict["audit"]["log_md_path"] = str(temp_vault_root / "LOG.md")
    config_dict["backup"]["git_path"] = str(temp_vault_root / ".git")
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


# ── New: Tool registration completeness ──────────────────────────────


class TestToolRegistration:
    """Verify all 12 MCP tools from the architecture contract are registered."""

    REQUIRED_TOOLS = {
        "read_note",
        "search_notes",
        "create_note",
        "append_note",
        "edit_note",
        "write_note",
        "promote_note",
        "propose_wiki_update",
        "refresh_paths",
        "health_check",
        "backup_vault",
        "backup_health",
    }

    def test_all_tools_registered(self):
        registered = set(tools.keys())
        assert registered == self.REQUIRED_TOOLS, (
            f"Missing tools: {self.REQUIRED_TOOLS - registered}, "
            f"Extra tools: {registered - self.REQUIRED_TOOLS}"
        )

    def test_search_notes_registered(self):
        assert "search_notes" in tools

    def test_mutation_tools_registered(self):
        assert "create_note" in tools
        assert "append_note" in tools
        assert "edit_note" in tools
        assert "write_note" in tools

    def test_wiki_and_refresh_registered(self):
        assert "propose_wiki_update" in tools
        assert "refresh_paths" in tools

    def test_health_check_registered(self):
        assert "health_check" in tools


# ── New: Search notes retrieval ───────────────────────────────────────


class TestSearchNotesTool:
    def test_search_notes_returns_results(
        self, temp_vault_root, sample_config_dict
    ):
        config = _make_config(temp_vault_root, sample_config_dict)
        from memory_mcp.index_refresh import refresh_paths

        refresh_paths("admin", ["memory/note.md"], config)

        result = handle_tool_call(
            "search_notes", READONLY_KEY, {"query": "test"}, config
        )

        assert result.success is True
        assert isinstance(result.data, dict)
        assert "results" in result.data

    def test_search_notes_returns_empty_for_no_match(
        self, temp_vault_root, sample_config_dict
    ):
        config = _make_config(temp_vault_root, sample_config_dict)
        from memory_mcp.index_refresh import refresh_paths

        refresh_paths("admin", ["memory/note.md"], config)

        result = handle_tool_call(
            "search_notes", READONLY_KEY, {"query": "xyznonexistent"}, config
        )

        assert result.success is True
        assert result.data["results"] == []

    def test_search_notes_respects_allowlist_roots(
        self, temp_vault_root, sample_config_dict
    ):
        config = _make_config(temp_vault_root, sample_config_dict)
        from memory_mcp.index_refresh import refresh_paths

        refresh_paths("admin", ["memory/note.md", "inbox/draft.md"], config)

        result = handle_tool_call(
            "search_notes", READONLY_KEY, {"query": "test OR draft"}, config
        )

        assert result.success is True
        paths = [r["path"] for r in result.data["results"]]
        for p in paths:
            assert not p.startswith("private/"), (
                f"Denylisted path '{p}' must not appear in search results"
            )


# ── New: Mutation orchestration ────────────────────────────────────────


class TestMutationOrchestration:
    def test_create_note_returns_affected_paths(
        self, temp_vault_root, sample_config_dict
    ):
        config = _make_config(temp_vault_root, sample_config_dict)

        result = handle_tool_call(
            "create_note",
            TRUSTED_KEY,
            {"path": "memory/new_note.md", "content": "# New"},
            config,
        )

        assert result.success is True
        assert "affected_paths" in result.data
        assert result.data["affected_paths"] == ["memory/new_note.md"]

    def test_create_note_dry_run_returns_affected_paths(
        self, temp_vault_root, sample_config_dict
    ):
        config = _make_config(temp_vault_root, sample_config_dict)

        result = handle_tool_call(
            "create_note",
            TRUSTED_KEY,
            {"path": "memory/dry_note.md", "content": "# Dry", "dry_run": True},
            config,
        )

        assert result.success is True
        assert result.data["dry_run"] is True
        assert result.data["affected_paths"] == ["memory/dry_note.md"]

    def test_append_note_returns_affected_paths(
        self, temp_vault_root, sample_config_dict
    ):
        config = _make_config(temp_vault_root, sample_config_dict)
        from memory_mcp.vault_fs import compute_revision, read_file

        revision = compute_revision(read_file(config.vault.root, "memory/note.md"))

        result = handle_tool_call(
            "append_note",
            TRUSTED_KEY,
            {
                "path": "memory/note.md",
                "patch_spec": {
                    "operation": "append_under_heading",
                    "heading": "Test Note",
                    "text": "Appended content.",
                },
                "expected_revision": revision,
            },
            config,
        )

        assert result.success is True
        assert "affected_paths" in result.data
        assert result.data["affected_paths"] == ["memory/note.md"]

    def test_mutation_triggers_index_refresh(
        self, temp_vault_root, sample_config_dict
    ):
        config = _make_config(temp_vault_root, sample_config_dict)

        refresh_calls = []

        def _capture_refresh(profile, paths, cfg):
            refresh_calls.append({"profile": profile, "paths": list(paths)})
            from memory_mcp.index_refresh import RefreshResult
            return RefreshResult(success=True, updated_paths=list(paths), deleted_paths=[])

        import memory_mcp.server as srv
        srv._svc_refresh_paths = _capture_refresh

        try:
            result = handle_tool_call(
                "create_note",
                TRUSTED_KEY,
                {"path": "memory/refreshed.md", "content": "# Refresh", "dry_run": False},
                config,
            )

            assert result.success is True
            assert len(refresh_calls) == 1, (
                f"Expected 1 refresh call, got {len(refresh_calls)}"
            )
            assert refresh_calls[0]["paths"] == ["memory/refreshed.md"]
        finally:
            from memory_mcp.index_refresh import refresh_paths as real_refresh

            srv._svc_refresh_paths = real_refresh

    def test_mutation_dry_run_does_not_trigger_refresh(
        self, temp_vault_root, sample_config_dict
    ):
        config = _make_config(temp_vault_root, sample_config_dict)

        refresh_calls = []

        def _capture_refresh(profile, paths, cfg):
            refresh_calls.append(paths)
            from memory_mcp.index_refresh import RefreshResult
            return RefreshResult(success=True, updated_paths=[], deleted_paths=[])

        import memory_mcp.server as srv
        srv._svc_refresh_paths = _capture_refresh

        try:
            handle_tool_call(
                "create_note",
                TRUSTED_KEY,
                {"path": "memory/skip_refresh.md", "content": "# Skip", "dry_run": True},
                config,
            )

            assert len(refresh_calls) == 0, (
                "Index refresh should not be called on dry_run"
            )
        finally:
            from memory_mcp.index_refresh import refresh_paths as real_refresh

            srv._svc_refresh_paths = real_refresh

    def test_trace_order_mutation_before_refresh(
        self, temp_vault_root, sample_config_dict, caplog
    ):
        caplog.set_level(logging.DEBUG)
        config = _make_config(temp_vault_root, sample_config_dict)

        handle_tool_call(
            "create_note",
            TRUSTED_KEY,
            {"path": "memory/trace_order.md", "content": "# Trace", "dry_run": False},
            config,
        )

        entries = _parse_logs(caplog)
        events = [e.get("event", "") for e in entries]

        mutation_idx = -1
        refresh_idx = -1
        for i, evt in enumerate(events):
            if evt == "mutation.affected_paths.returned":
                mutation_idx = i
            if evt == "index.refresh.started" and mutation_idx >= 0:
                refresh_idx = i
                break

        assert mutation_idx >= 0, (
            f"Expected 'mutation.affected_paths.returned' event not found. "
            f"Events: {events}"
        )
        assert refresh_idx >= 0, (
            f"Expected 'index.refresh.started' after mutation not found. "
            f"Events: {events}"
        )
        assert mutation_idx < refresh_idx, (
            f"mutation.affected_paths.returned (idx {mutation_idx}) must appear "
            f"before index.refresh.started (idx {refresh_idx})"
        )


# ── New: Wiki proposal ─────────────────────────────────────────────────


class TestWikiProposal:
    def test_propose_wiki_update_returns_proposal(
        self, temp_vault_root, sample_config_dict
    ):
        config = _make_config(temp_vault_root, sample_config_dict)
        from memory_mcp.index_refresh import refresh_paths

        refresh_paths("admin", ["memory/note.md"], config)

        (temp_vault_root / "70_Wiki" / "wiki.md").write_text("# Old Wiki\n")

        result = handle_tool_call(
            "propose_wiki_update",
            READONLY_KEY,
            {
                "wiki_path": "70_Wiki/wiki.md",
                "query": "test",
                "proposed_content": "# New Wiki\n",
            },
            config,
        )

        assert result.success is True
        data = result.data
        assert "diff" in data
        assert "source_paths" in data
        assert data["dry_run"] is True
        assert isinstance(data["diff"], str)
        assert len(data["diff"]) > 0

    def test_direct_wiki_write_is_policy_denied(
        self, temp_vault_root, sample_config_dict
    ):
        config = _make_config(temp_vault_root, sample_config_dict)

        result = handle_tool_call(
            "create_note",
            TRUSTED_KEY,
            {"path": "70_Wiki/direct.md", "content": "# Direct Wiki Write"},
            config,
        )

        assert result.success is False, (
            "Direct create in 70_Wiki must be deny"
        )
        assert result.error_code == "SERVICE_ERROR", (
            f"Got error_code={result.error_code}"
        )

    def test_wiki_write_with_admin_also_denied(
        self, temp_vault_root, sample_config_dict
    ):
        config = _make_config(temp_vault_root, sample_config_dict)

        result = handle_tool_call(
            "write_note",
            ADMIN_KEY,
            {
                "path": "70_Wiki/forbidden.md",
                "content": "# Forbidden",
                "expected_revision": "any_placeholder_revision",
            },
            config,
        )

        assert result.success is False, (
            "Admin direct write to 70_Wiki must be policy-denied"
        )


# ── New: Health check tool ─────────────────────────────────────────────


class TestHealthCheckTool:
    REQUIRED_MODULES = {
        "config",
        "observability",
        "auth",
        "policy",
        "read_service",
        "mutation_service",
        "retrieval_service",
        "index_repo",
        "index_refresh",
        "wiki_update_service",
        "backup_service",
        "draft_promotion_service",
        "vault_fs",
        "concurrency",
        "change_planner",
        "audit_log",
        "markdown_parser",
        "vector_adapter",
    }

    def test_health_check_returns_all_modules(
        self, temp_vault_root, sample_config_dict
    ):
        config = _make_config(temp_vault_root, sample_config_dict)
        result = health_check(config)

        assert result["status"] == "ok"
        assert set(result["modules"]) == self.REQUIRED_MODULES, (
            f"Missing: {self.REQUIRED_MODULES - set(result['modules'])}, "
            f"Extra: {set(result['modules']) - self.REQUIRED_MODULES}"
        )

    def test_health_check_accessible_as_tool(
        self, temp_vault_root, sample_config_dict
    ):
        config = _make_config(temp_vault_root, sample_config_dict)

        result = handle_tool_call("health_check", READONLY_KEY, {}, config)

        assert result.success is True
        assert result.data["status"] == "ok"
        assert "mutation_service" in result.data["modules"]
        assert "retrieval_service" in result.data["modules"]
        assert "index_refresh" in result.data["modules"]
        assert "wiki_update_service" in result.data["modules"]


# ── New: Refresh paths tool ────────────────────────────────────────────


class TestRefreshPathsTool:
    def test_refresh_paths_tool_returns_updated_deleted(
        self, temp_vault_root, sample_config_dict
    ):
        config = _make_config(temp_vault_root, sample_config_dict)

        result = handle_tool_call(
            "refresh_paths",
            ADMIN_KEY,
            {"paths": ["memory/note.md"]},
            config,
        )

        assert result.success is True
        assert "updated_paths" in result.data
        assert "deleted_paths" in result.data
        assert result.data["updated_paths"] == ["memory/note.md"]
