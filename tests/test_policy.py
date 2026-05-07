"""Module-local tests for M-003 VaultPolicyGuard."""

import json
import logging

from memory_mcp.config import load_config
from memory_mcp.policy import (
    ALWAYS_DENIED,
    DRY_RUN_REQUIRED,
    PERMISSION_MATRIX,
    PolicyDecision,
    authorize_operation,
    check_path_policy,
    filter_search_results,
)

ALL_PROFILES = ["readonly_agent", "trusted_writer", "wiki_maintainer", "cron_summarizer", "admin"]


def _parsed_logs(caplog):
    entries = []
    for record in caplog.records:
        try:
            entries.append(json.loads(record.message))
        except (json.JSONDecodeError, TypeError):
            pass
    return entries


class TestAuthorizeOperation:
    def test_allowlisted_memory_path_accepted_for_read(self, sample_config_dict, log_capture, trace_assert):
        config = load_config(sample_config_dict)
        result = authorize_operation("readonly_agent", "memory/note.md", "read", False, config)
        assert result == PolicyDecision(allowed=True)
        entries = _parsed_logs(log_capture)
        assert trace_assert(entries, "policy.decision")

    def test_denylisted_private_path_rejected(self, sample_config_dict, log_capture, trace_assert):
        config = load_config(sample_config_dict)
        result = authorize_operation("readonly_agent", "private/secret.md", "read", False, config)
        assert result.allowed is False
        assert "denylist" in result.reason
        entries = _parsed_logs(log_capture)
        assert trace_assert(entries, "policy.denied")

    def test_delete_operation_rejected_all_profiles(self, sample_config_dict, log_capture):
        config = load_config(sample_config_dict)
        for profile in ALL_PROFILES:
            result = authorize_operation(profile, "memory/note.md", "delete", False, config)
            assert result.allowed is False, f"delete should be denied for {profile}"
            assert "always denied" in result.reason

    def test_bulk_move_rejected(self, sample_config_dict, log_capture):
        config = load_config(sample_config_dict)
        result = authorize_operation("admin", "memory/note.md", "bulk_move", False, config)
        assert result.allowed is False
        assert "bulk_move" in result.reason or "always denied" in result.reason

    def test_70_wiki_direct_write_rejected_even_trusted(self, sample_config_dict, log_capture):
        config = load_config(sample_config_dict)
        result = authorize_operation("trusted_writer", "70_Wiki/SomePage.md", "write", True, config)
        assert result.allowed is False
        assert "propose_only" in result.reason

    def test_propose_only_paths_allow_propose_operation(self, sample_config_dict, log_capture, trace_assert):
        config = load_config(sample_config_dict)
        result = authorize_operation("wiki_maintainer", "70_Wiki/SomePage.md", "propose", False, config)
        assert result == PolicyDecision(allowed=True)
        entries = _parsed_logs(log_capture)
        assert trace_assert(entries, "policy.decision")

    def test_edit_without_dry_run_rejected(self, sample_config_dict, log_capture):
        config = load_config(sample_config_dict)
        result = authorize_operation("trusted_writer", "memory/note.md", "edit", False, config)
        assert result.allowed is False
        assert "dry_run" in result.reason

    def test_readonly_agent_write_rejected(self, sample_config_dict, log_capture):
        config = load_config(sample_config_dict)
        result = authorize_operation("readonly_agent", "memory/note.md", "write", True, config)
        assert result.allowed is False
        assert "profile" in result.reason.lower() or "not permitted" in result.reason

    def test_full_profile_tool_permission_matrix(self, sample_config_dict):
        config = load_config(sample_config_dict)
        all_operations = [
            "read", "search", "create", "append", "edit", "write",
            "promote", "propose", "refresh", "backup",
        ]

        for profile in ALL_PROFILES:
            allowed_ops = PERMISSION_MATRIX.get(profile, set())
            for op in all_operations:
                dry_run = True if op in DRY_RUN_REQUIRED else False
                result = authorize_operation(profile, "memory/note.md", op, dry_run, config)

                expected_allowed = op in allowed_ops
                assert result.allowed == expected_allowed, (
                    f"Profile '{profile}' operation '{op}': "
                    f"expected allowed={expected_allowed}, got allowed={result.allowed}, "
                    f"reason='{result.reason}'"
                )


class TestCheckPathPolicy:
    def test_symlink_path_rejected_by_policy(self, sample_config_dict, log_capture):
        config = load_config(sample_config_dict)
        result = check_path_policy("memory/link.md", config, is_symlink=True)
        assert result.allowed is False
        assert "symlinks_forbidden" in result.reason

    def test_denylist_path_rejected(self, sample_config_dict, log_capture):
        config = load_config(sample_config_dict)
        result = check_path_policy("private/secret.md", config)
        assert result.allowed is False
        assert "denylist" in result.reason

    def test_propose_only_path_accepted_with_restriction(self, sample_config_dict, log_capture):
        config = load_config(sample_config_dict)
        result = check_path_policy("70_Wiki/SomePage.md", config)
        assert result.allowed is True
        assert "propose_only" in result.reason

    def test_allowlisted_path_accepted(self, sample_config_dict, log_capture, trace_assert):
        config = load_config(sample_config_dict)
        result = check_path_policy("memory/note.md", config)
        assert result == PolicyDecision(allowed=True)
        entries = _parsed_logs(log_capture)
        assert trace_assert(entries, "policy.decision")


class TestFilterSearchResults:
    def test_filter_search_results_removes_denylisted_entries(self, sample_config_dict, log_capture, trace_assert):
        config = load_config(sample_config_dict)
        search_results = [
            {"path": "memory/note.md", "score": 0.9},
            {"path": "private/secret.md", "score": 0.8},
            {"path": "inbox/draft.md", "score": 0.7},
            {"path": "secrets/tokens.md", "score": 0.6},
        ]
        filtered = filter_search_results(search_results, config)
        assert len(filtered) == 2
        paths = {r["path"] for r in filtered}
        assert paths == {"memory/note.md", "inbox/draft.md"}
        entries = _parsed_logs(log_capture)
        assert trace_assert(entries, "retrieval.denied_path.filtered")

    def test_filter_search_results_all_clean(self, sample_config_dict):
        config = load_config(sample_config_dict)
        search_results = [
            {"path": "memory/note.md", "score": 0.9},
            {"path": "inbox/draft.md", "score": 0.7},
        ]
        filtered = filter_search_results(search_results, config)
        assert len(filtered) == 2
        assert filtered == search_results

    def test_filter_search_results_empty(self, sample_config_dict):
        config = load_config(sample_config_dict)
        filtered = filter_search_results([], config)
        assert filtered == []

    def test_filter_search_results_entry_missing_path(self, sample_config_dict):
        config = load_config(sample_config_dict)
        search_results = [
            {"score": 0.9},
            {"path": "memory/note.md", "score": 0.7},
        ]
        filtered = filter_search_results(search_results, config)
        assert len(filtered) == 2
