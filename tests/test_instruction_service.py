"""Module-local tests for M-021 InstructionService."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any
from unittest.mock import patch

import pytest

from memory_mcp.auth import AgentIdentity
from memory_mcp.instruction_service import (
    InstructionPacket,
    PRODUCT_ALIASES,
    SAFE_WORKFLOWS_BY_PROFILE,
    compose_directory_guidance,
    compose_role_guidance,
    get_instructions,
)
from memory_mcp.policy import ALWAYS_DENIED, PERMISSION_MATRIX
from memory_mcp.vault_layout import (
    CanonicalPath,
    DirectoryContract,
    DirectoryDriftReport,
)


def _parse_logs(caplog) -> list[dict]:
    entries = []
    for record in caplog.records:
        try:
            entries.append(json.loads(record.message))
        except json.JSONDecodeError:
            pass
    return entries


def build_identity(profile: str, key_id: str = "test...-001") -> AgentIdentity:
    return AgentIdentity(key_id=key_id, profile=profile)


def build_directory_contract() -> DirectoryContract:
    keys = {
        "inbox": CanonicalPath(semantic_key="inbox", relative_path="00_Inbox", absolute_path="/tmp/vault/00_Inbox"),
        "memory": CanonicalPath(semantic_key="memory", relative_path="memory", absolute_path="/tmp/vault/memory"),
        "summaries": CanonicalPath(semantic_key="summaries", relative_path="summaries", absolute_path="/tmp/vault/summaries"),
        "wiki": CanonicalPath(semantic_key="wiki", relative_path="70_Wiki", absolute_path="/tmp/vault/70_Wiki"),
        "log": CanonicalPath(semantic_key="log", relative_path="00_Log", absolute_path="/tmp/vault/00_Log"),
        "system": CanonicalPath(semantic_key="system", relative_path=".system", absolute_path="/tmp/vault/.system"),
    }
    return DirectoryContract(
        keys=keys,
        aliases={k: k for k in keys},
        created_at=datetime.now(timezone.utc).isoformat(),
    )


def build_ok_drift_report() -> DirectoryDriftReport:
    return DirectoryDriftReport(
        missing_canonical=[],
        ambiguous_aliases=[],
        unknown_dirs=[],
    )


def build_drift_report_with_conflicts() -> DirectoryDriftReport:
    return DirectoryDriftReport(
        missing_canonical=["system"],
        ambiguous_aliases=["notes"],
        unknown_dirs=["tmp_junk"],
    )


class TestInstructionPacket:
    """M-021-S-001: get_instructions returns product aliases, role, capabilities,
    all directory keys, and trace anchors TA-021 + TA-022."""

    def test_get_instructions_returns_product_aliases_role_and_directory_contract(
        self, caplog, trace_assert
    ):
        caplog.set_level(logging.DEBUG)
        identity = build_identity("readonly_agent")
        contract = build_directory_contract()
        drift = build_ok_drift_report()

        packet = get_instructions(identity, "readonly_agent", contract, drift)

        assert isinstance(packet, InstructionPacket)

        assert packet.product_aliases == PRODUCT_ALIASES
        assert "AgentMemBank" in packet.product_aliases
        assert "Agent Memory Bank" in packet.product_aliases
        assert "Memory Bank MCP" in packet.product_aliases

        assert packet.role == "readonly_agent"
        assert "read" in packet.capabilities
        assert "search" in packet.capabilities

        expected_keys = ["inbox", "log", "memory", "summaries", "system", "wiki"]
        assert packet.directory_contract_keys == expected_keys

        assert packet.canonical_paths["inbox"] == "00_Inbox"
        assert packet.canonical_paths["memory"] == "memory"
        assert packet.canonical_paths["summaries"] == "summaries"
        assert packet.canonical_paths["wiki"] == "70_Wiki"
        assert packet.canonical_paths["log"] == "00_Log"
        assert packet.canonical_paths["system"] == ".system"

        assert isinstance(packet.generated_at, str)
        assert len(packet.generated_at) > 0

        entries = _parse_logs(caplog)
        trace_assert(entries, "instructions.role_context.included", "instructions.generated")

    def test_instruction_packet_excludes_secrets_and_raw_tokens(
        self, caplog
    ):
        caplog.set_level(logging.DEBUG)
        identity = build_identity("readonly_agent", key_id="test-key-secret-999")
        contract = build_directory_contract()
        drift = build_ok_drift_report()

        packet = get_instructions(identity, "readonly_agent", contract, drift)

        packet_dict = {
            "product_aliases": packet.product_aliases,
            "role": packet.role,
            "capabilities": packet.capabilities,
            "directory_contract_keys": packet.directory_contract_keys,
            "canonical_paths": packet.canonical_paths,
            "safe_workflows": packet.safe_workflows,
            "drift_warnings": packet.drift_warnings,
            "forbidden_writes": packet.forbidden_writes,
            "generated_at": packet.generated_at,
        }

        serialized = json.dumps(packet_dict)
        assert "test-key-secret-999" not in serialized
        assert "test-key-readonly-001" not in serialized

        log_entries = _parse_logs(caplog)
        log_text = json.dumps(log_entries)
        assert "test-key-secret-999" not in log_text
        assert "api_key" not in json.dumps(packet_dict).lower()

    def test_drift_warning_included_when_layout_reports_conflict(
        self, caplog, trace_assert
    ):
        caplog.set_level(logging.DEBUG)
        identity = build_identity("trusted_writer")
        contract = build_directory_contract()
        drift = build_drift_report_with_conflicts()

        assert drift.ok is False

        packet = get_instructions(identity, "trusted_writer", contract, drift)

        assert len(packet.drift_warnings) > 0

        missing_warning = any(
            "Missing canonical directory: 'system'" in w for w in packet.drift_warnings
        )
        ambiguous_warning = any(
            "ambiguous" in w.lower() for w in packet.drift_warnings
        )
        unknown_warning = any(
            "tmp_junk" in w for w in packet.drift_warnings
        )
        assert missing_warning, f"Expected missing canonical warning in: {packet.drift_warnings}"
        assert ambiguous_warning, f"Expected ambiguous alias warning in: {packet.drift_warnings}"
        assert unknown_warning, f"Expected unknown dir warning in: {packet.drift_warnings}"

        has_deprecated_alias_warning = any(
            "deprecated" in w.lower() for w in packet.drift_warnings
        )
        assert has_deprecated_alias_warning, (
            f"Expected deprecated alias warnings in: {packet.drift_warnings}"
        )

        entries = _parse_logs(caplog)
        trace_assert(
            entries,
            "instructions.layout_warning.included",
            "instructions.role_context.included",
            "instructions.generated",
        )

        layout_warning_entries = [
            e for e in entries if e.get("event") == "instructions.layout_warning.included"
        ]
        assert len(layout_warning_entries) >= 1
        assert layout_warning_entries[0]["level"] == "WARNING"

    def test_no_drift_warning_when_drift_report_ok(self, caplog):
        caplog.set_level(logging.DEBUG)
        identity = build_identity("admin")
        contract = build_directory_contract()
        drift = build_ok_drift_report()

        assert drift.ok is True

        packet = get_instructions(identity, "admin", contract, drift)

        deprecated_aliases_count = sum(
            1 for w in packet.drift_warnings if "deprecated" in w.lower()
        )
        drift_specific = [
            w for w in packet.drift_warnings
            if "deprecated" not in w.lower()
        ]
        assert len(drift_specific) == 0, (
            f"Expected no drift-specific warnings when drift is ok, got: {drift_specific}"
        )

        entries = _parse_logs(caplog)
        layout_warning_entries = [
            e for e in entries if e.get("event") == "instructions.layout_warning.included"
        ]
        assert len(layout_warning_entries) == 0

    def test_drift_warning_none_does_not_emit_layout_warning(self, caplog):
        caplog.set_level(logging.DEBUG)
        identity = build_identity("readonly_agent")
        contract = build_directory_contract()

        packet = get_instructions(identity, "readonly_agent", contract, None)

        assert isinstance(packet, InstructionPacket)

        entries = _parse_logs(caplog)
        layout_warning_entries = [
            e for e in entries if e.get("event") == "instructions.layout_warning.included"
        ]
        assert len(layout_warning_entries) == 0

    def test_instruction_packet_is_json_serializable(self):
        identity = build_identity("readonly_agent")
        contract = build_directory_contract()
        drift = build_ok_drift_report()

        packet = get_instructions(identity, "readonly_agent", contract, drift)

        packet_dict = {
            "product_aliases": packet.product_aliases,
            "role": packet.role,
            "capabilities": packet.capabilities,
            "directory_contract_keys": packet.directory_contract_keys,
            "canonical_paths": packet.canonical_paths,
            "safe_workflows": packet.safe_workflows,
            "drift_warnings": packet.drift_warnings,
            "forbidden_writes": packet.forbidden_writes,
            "generated_at": packet.generated_at,
        }

        serialized = json.dumps(packet_dict)
        deserialized = json.loads(serialized)

        assert deserialized["role"] == "readonly_agent"
        assert deserialized["product_aliases"] == PRODUCT_ALIASES
        assert "inbox" in deserialized["canonical_paths"]


class TestRoleGuidance:
    """Compose role guidance tests for all five profiles."""

    def test_readonly_agent_role_guidance(self):
        guidance = compose_role_guidance("readonly_agent")
        assert "Role: readonly_agent" in guidance
        assert "read" in guidance
        assert "search" in guidance
        assert "propose" in guidance
        assert "search + read only" in guidance

    def test_trusted_writer_role_guidance(self):
        guidance = compose_role_guidance("trusted_writer")
        assert "Role: trusted_writer" in guidance
        assert "create" in guidance
        assert "write" in guidance
        assert "dry_run" in guidance
        assert "create/append/edit/write with required dry_run" in guidance

    def test_wiki_maintainer_role_guidance(self):
        guidance = compose_role_guidance("wiki_maintainer")
        assert "Role: wiki_maintainer" in guidance
        assert "read" in guidance
        assert "search" in guidance
        assert "propose" in guidance
        assert "find_wiki_sources + propose_wiki_update" in guidance

    def test_cron_summarizer_role_guidance(self):
        guidance = compose_role_guidance("cron_summarizer")
        assert "Role: cron_summarizer" in guidance
        assert "read" in guidance
        assert "search" in guidance
        assert "append" in guidance
        assert "read + append to summaries only" in guidance

    def test_admin_role_guidance(self):
        guidance = compose_role_guidance("admin")
        assert "Role: admin" in guidance
        assert "read" in guidance
        assert "search" in guidance
        assert "create" in guidance
        assert "backup" in guidance
        assert "all operations except delete/bulk_move" in guidance

    def test_all_five_profiles_produce_distinct_guidance(self):
        profiles = ["readonly_agent", "trusted_writer", "wiki_maintainer", "cron_summarizer", "admin"]
        guidances = {}
        for p in profiles:
            guidances[p] = compose_role_guidance(p)

        for p in profiles:
            assert f"Role: {p}" in guidances[p], f"Missing role marker for {p}"

        for i, p1 in enumerate(profiles):
            for j, p2 in enumerate(profiles):
                if i >= j:
                    continue
                assert guidances[p1] != guidances[p2], (
                    f"Guidance for '{p1}' and '{p2}' should be distinct"
                )


class TestDirectoryGuidance:
    """M-021-S-002: directory_guidance identifies exact canonical write destinations."""

    def test_directory_guidance_identifies_exact_canonical_write_destinations(self):
        contract = build_directory_contract()

        guidance = compose_directory_guidance(contract)

        assert "00_Inbox" in guidance
        assert "memory" in guidance
        assert "summaries" in guidance
        assert "70_Wiki" in guidance
        assert "00_Log" in guidance
        assert ".system" in guidance

        for sk in contract.keys:
            cp = contract.keys[sk]
            assert cp.relative_path in guidance
            assert cp.absolute_path in guidance

    def test_empty_directory_contract_produces_reasonable_default_guidance(self):
        contract = DirectoryContract(
            keys={},
            aliases={},
            created_at=datetime.now(timezone.utc).isoformat(),
        )

        guidance = compose_directory_guidance(contract)

        assert len(guidance) > 0
        assert "No directory contract entries available" in guidance

    def test_directory_guidance_includes_deprecated_alias_warnings(self):
        contract = build_directory_contract()
        guidance = compose_directory_guidance(contract)

        assert "Deprecated aliases" in guidance
        assert "inbox" in guidance.lower()

    def test_safe_workflows_are_profile_appropriate(self):
        for profile, expected_workflows in SAFE_WORKFLOWS_BY_PROFILE.items():
            packet = get_instructions(
                build_identity(profile),
                profile,
                build_directory_contract(),
                build_ok_drift_report(),
            )

            assert packet.safe_workflows == expected_workflows, (
                f"Safe workflows for {profile}: expected {expected_workflows}, got {packet.safe_workflows}"
            )

    def test_forbidden_writes_match_always_denied(self):
        packet = get_instructions(
            build_identity("admin"),
            "admin",
            build_directory_contract(),
            build_ok_drift_report(),
        )

        assert sorted(packet.forbidden_writes) == sorted(ALWAYS_DENIED)

    def test_capabilities_match_permission_matrix(self):
        for profile, expected_caps in PERMISSION_MATRIX.items():
            packet = get_instructions(
                build_identity(profile),
                profile,
                build_directory_contract(),
                build_ok_drift_report(),
            )

            assert sorted(packet.capabilities) == sorted(expected_caps), (
                f"Capabilities for {profile}: expected {sorted(expected_caps)}, got {sorted(packet.capabilities)}"
            )

    def test_unknown_profile_produces_empty_capabilities(self):
        identity = AgentIdentity(key_id="test...-001", profile="unknown_role")

        packet = get_instructions(
            identity,
            "unknown_role",
            build_directory_contract(),
            None,
        )

        assert packet.capabilities == []
        assert packet.safe_workflows == []
