"""Module-local tests for M-022 VaultLayoutService."""

import json
import logging
import os
from pathlib import Path

import pytest

from memory_mcp.vault_layout import (
    MODULE_BLOCK,
    CanonicalPath,
    CanonicalPathOutsideVaultError,
    DeprecatedAliasError,
    DirectoryContract,
    DirectoryDriftReport,
    DirectoryRule,
    VaultLayoutError,
    VaultLayoutReport,
    detect_directory_drift,
    detect_vault_layout,
    resolve_destination_for_intent,
    resolve_directory_contract,
)


def _parse_logs(caplog) -> list[dict]:
    entries = []
    for record in caplog.records:
        try:
            entries.append(json.loads(record.message))
        except json.JSONDecodeError:
            pass
    return entries


# --- Fixtures ---

@pytest.fixture
def obsidian_vault_root(tmp_path: Path) -> str:
    vault = tmp_path / "test_vault"
    (vault / ".obsidian").mkdir(parents=True)
    (vault / "00_Inbox").mkdir(parents=True)
    (vault / "memory").mkdir(parents=True)
    (vault / "summaries").mkdir(parents=True)
    (vault / "70_Wiki").mkdir(parents=True)
    (vault / "00_Log").mkdir(parents=True)
    (vault / ".system").mkdir(parents=True)
    return str(vault)


@pytest.fixture
def non_obsidian_vault_root(tmp_path: Path) -> str:
    vault = tmp_path / "plain_vault"
    (vault / "inbox").mkdir(parents=True)
    return str(vault)


@pytest.fixture
def sample_directory_config() -> dict[str, str]:
    return {
        "inbox": "00_Inbox",
        "memory": "memory",
        "summaries": "summaries",
        "wiki": "70_Wiki",
        "log": "00_Log",
        "system": ".system",
    }


@pytest.fixture
def resolved_contract(obsidian_vault_root, sample_directory_config) -> DirectoryContract:
    return resolve_directory_contract(sample_directory_config, obsidian_vault_root)


# --- M-022-S-001: detects_obsidian_vault_marker ---

def test_detects_obsidian_vault_marker(obsidian_vault_root, caplog, trace_assert):
    caplog.set_level(logging.DEBUG)

    report = detect_vault_layout(obsidian_vault_root, require_obsidian_marker=True)

    assert isinstance(report, VaultLayoutReport)
    assert report.obsidian_marker_found is True
    assert report.vault_root == obsidian_vault_root
    assert report.trace_id is not None

    entries = _parse_logs(caplog)
    trace_assert(entries, "vault_layout.detected")

    detected_entry = next(e for e in entries if e.get("event") == "vault_layout.detected")
    assert detected_entry["level"] == "INFO"
    assert detected_entry["function"] == "detect_vault_layout"
    assert detected_entry["block"] == MODULE_BLOCK
    assert detected_entry["data"]["vault_root"] == obsidian_vault_root


# --- M-022-S-002: resolves_initial_directory_keys ---

def test_resolves_initial_directory_keys(obsidian_vault_root, sample_directory_config, caplog, trace_assert):
    caplog.set_level(logging.DEBUG)

    contract = resolve_directory_contract(sample_directory_config, obsidian_vault_root)

    assert isinstance(contract, DirectoryContract)
    assert set(contract.keys.keys()) == {"inbox", "memory", "summaries", "wiki", "log", "system"}

    for key in sample_directory_config:
        assert key in contract.keys
        cp = contract.keys[key]
        assert isinstance(cp, CanonicalPath)
        assert cp.semantic_key == key
        assert cp.relative_path == sample_directory_config[key]
        assert cp.absolute_path.startswith(obsidian_vault_root)

    entries = _parse_logs(caplog)
    trace_assert(entries, "vault_layout.canonical_paths.resolved")

    resolved_entry = next(e for e in entries if e.get("event") == "vault_layout.canonical_paths.resolved")
    assert resolved_entry["level"] == "INFO"
    assert resolved_entry["function"] == "resolve_directory_contract"
    assert resolved_entry["block"] == MODULE_BLOCK
    assert set(resolved_entry["data"]["keys"]) == {"inbox", "memory", "summaries", "wiki", "log", "system"}


# --- M-022-S-003: generic_capture_resolves_to_canonical_inbox ---

def test_generic_capture_resolves_to_canonical_inbox(obsidian_vault_root, sample_directory_config, caplog):
    caplog.set_level(logging.DEBUG)
    contract = resolve_directory_contract(sample_directory_config, obsidian_vault_root)

    unknown_intent = "random_unstructured_thought"
    destination = resolve_destination_for_intent(unknown_intent, contract)

    inbox_cp = contract.keys["inbox"]
    assert destination == inbox_cp.absolute_path

    entries = _parse_logs(caplog)
    capture_entries = [e for e in entries if e.get("event") == "vault_layout.destination.generic_capture"]
    assert len(capture_entries) >= 1
    capture_entry = capture_entries[-1]
    assert capture_entry["level"] == "INFO"
    assert capture_entry["data"]["intent"] == unknown_intent
    assert capture_entry["data"]["fallback"] == "inbox"


# --- M-022-F-001: ambiguous_alias_conflict_reports_drift ---

def test_ambiguous_alias_conflict_reports_drift(obsidian_vault_root, caplog, trace_assert):
    caplog.set_level(logging.DEBUG)

    config_map = {
        "inbox": "00_Inbox",
        "memory": "memory",
        "wiki": "70_Wiki",
    }
    contract = resolve_directory_contract(config_map, obsidian_vault_root)

    actual_dirs = [".obsidian", "00_Inbox", "memory", "70_Wiki"]
    report = detect_directory_drift(contract, obsidian_vault_root, actual_dirs)

    assert isinstance(report, DirectoryDriftReport)

    entries = _parse_logs(caplog)
    trace_assert(entries, "vault_layout.drift_detected")

    drift_entry = next(e for e in entries if e.get("event") == "vault_layout.drift_detected")
    assert drift_entry["level"] == "WARNING"
    assert drift_entry["function"] == "detect_directory_drift"
    assert drift_entry["block"] == MODULE_BLOCK


def test_deprecated_alias_denied_as_write_target(obsidian_vault_root, sample_directory_config, caplog):
    caplog.set_level(logging.DEBUG)
    contract = resolve_directory_contract(sample_directory_config, obsidian_vault_root)

    with pytest.raises(DeprecatedAliasError) as exc_info:
        resolve_destination_for_intent("capture", contract)

    assert exc_info.value.error_code == "DEPRECATED_ALIAS_DENIED"
    assert exc_info.value.alias == "capture"
    assert exc_info.value.canonical_key == "inbox"

    entries = _parse_logs(caplog)
    denied_entries = [e for e in entries if e.get("event") == "vault_layout.destination.denied"]
    assert len(denied_entries) >= 1
    denied = denied_entries[-1]
    assert denied["level"] == "WARNING"
    assert denied["error_code"] == "DEPRECATED_ALIAS_DENIED"
    assert denied["data"]["alias"] == "capture"
    assert denied["data"]["canonical_key"] == "inbox"


# --- M-022-F-002: missing_obsidian_marker_warns_or_fails_by_config ---

def test_missing_obsidian_marker_raises_when_required(non_obsidian_vault_root, caplog, trace_assert):
    caplog.set_level(logging.DEBUG)

    with pytest.raises(VaultLayoutError) as exc_info:
        detect_vault_layout(non_obsidian_vault_root, require_obsidian_marker=True)

    assert exc_info.value.error_code == "OBSIDIAN_MARKER_MISSING"
    assert "No .obsidian marker found" in str(exc_info.value)

    entries = _parse_logs(caplog)
    trace_assert(entries, "vault_layout.obsidian_marker_missing")

    missing_entry = next(e for e in entries if e.get("event") == "vault_layout.obsidian_marker_missing")
    assert missing_entry["level"] == "WARNING"
    assert missing_entry["function"] == "detect_vault_layout"
    assert missing_entry["block"] == MODULE_BLOCK
    assert missing_entry["data"]["vault_root"] == non_obsidian_vault_root


def test_missing_obsidian_marker_passes_when_not_required(non_obsidian_vault_root, caplog):
    caplog.set_level(logging.DEBUG)

    report = detect_vault_layout(non_obsidian_vault_root, require_obsidian_marker=False)

    assert report.obsidian_marker_found is False
    assert report.vault_root == non_obsidian_vault_root


# --- M-022-F-003: canonical_path_outside_vault_rejected ---

def test_canonical_path_outside_vault_rejected(obsidian_vault_root, caplog):
    caplog.set_level(logging.DEBUG)

    config_map = {"escape": "../../etc/passwd"}

    with pytest.raises(CanonicalPathOutsideVaultError) as exc_info:
        resolve_directory_contract(config_map, obsidian_vault_root)

    assert exc_info.value.error_code == "CANONICAL_PATH_OUTSIDE_VAULT"
    assert exc_info.value.semantic_key == "escape"

    entries = _parse_logs(caplog)
    rejected_entries = [e for e in entries if e.get("event") == "vault_layout.canonical_path.rejected"]
    assert len(rejected_entries) >= 1
    rejected = rejected_entries[-1]
    assert rejected["level"] == "ERROR"
    assert rejected["error_code"] == "CANONICAL_PATH_OUTSIDE_VAULT"
    assert rejected["function"] == "resolve_directory_contract"


# --- Edge case: resolve canonical key directly ---

def test_resolve_canonical_key_returns_absolute_path(obsidian_vault_root, sample_directory_config, caplog):
    caplog.set_level(logging.DEBUG)
    contract = resolve_directory_contract(sample_directory_config, obsidian_vault_root)

    result = resolve_destination_for_intent("memory", contract)

    assert result == contract.keys["memory"].absolute_path
    assert result.startswith(obsidian_vault_root)

    entries = _parse_logs(caplog)
    resolved = next(e for e in entries if e.get("event") == "vault_layout.destination.resolved")
    assert resolved["data"]["intent"] == "memory"
    assert resolved["data"]["semantic_key"] == "memory"


# --- Edge case: resolve via non-deprecated alias ---

def test_resolve_via_alias_returns_correct_destination(obsidian_vault_root, sample_directory_config):
    contract = resolve_directory_contract(sample_directory_config, obsidian_vault_root)

    result = resolve_destination_for_intent("inbox", contract)

    assert result == contract.keys["inbox"].absolute_path


# --- Edge case: intent with whitespace is normalized ---

def test_intent_with_whitespace_is_normalized(obsidian_vault_root, sample_directory_config, caplog):
    caplog.set_level(logging.DEBUG)
    contract = resolve_directory_contract(sample_directory_config, obsidian_vault_root)

    result = resolve_destination_for_intent("  memory  ", contract)

    assert result == contract.keys["memory"].absolute_path


# --- Edge case: case-insensitive intent matching ---

def test_case_insensitive_intent_matching(obsidian_vault_root, sample_directory_config):
    contract = resolve_directory_contract(sample_directory_config, obsidian_vault_root)

    result = resolve_destination_for_intent("MEMORY", contract)

    assert result == contract.keys["memory"].absolute_path


# --- Edge case: DirectoryContract has created_at timestamp ---

def test_directory_contract_has_created_at_timestamp(obsidian_vault_root, sample_directory_config):
    contract = resolve_directory_contract(sample_directory_config, obsidian_vault_root)

    assert contract.created_at is not None
    assert isinstance(contract.created_at, str)
    assert len(contract.created_at) > 0


# --- Edge case: DirectoryDriftReport.ok when no drift ---

def test_directory_drift_report_ok_when_no_drift(obsidian_vault_root, caplog):
    caplog.set_level(logging.DEBUG)
    config_map = {
        "inbox": "00_Inbox",
        "summaries": "summaries",
        "wiki": "70_Wiki",
        "log": "00_Log",
        "system": ".system",
    }
    contract = resolve_directory_contract(config_map, obsidian_vault_root)

    report = detect_directory_drift(contract, obsidian_vault_root, [])

    assert report.ok is True
    assert report.missing_canonical == []
    assert report.ambiguous_aliases == []
    assert report.unknown_dirs == []


# --- Edge case: DirectoryDriftReport not ok when missing canonical ---

def test_directory_drift_report_not_ok_when_canonical_missing(tmp_path: Path, caplog, trace_assert):
    caplog.set_level(logging.DEBUG)
    vault = tmp_path / "partial_vault"
    (vault / ".obsidian").mkdir(parents=True)
    (vault / "00_Inbox").mkdir(parents=True)
    vault_root = str(vault)

    config_map = {
        "inbox": "00_Inbox",
        "memory": "memory",
    }
    contract = resolve_directory_contract(config_map, vault_root)

    report = detect_directory_drift(contract, vault_root, ["00_Inbox", ".obsidian"])

    assert report.ok is False
    assert "memory" in report.missing_canonical
    assert len(report.missing_canonical) == 1

    entries = _parse_logs(caplog)
    trace_assert(entries, "vault_layout.drift_detected")


# --- Edge case: unknown directories reported in drift ---

def test_unknown_directories_reported_in_drift(obsidian_vault_root, sample_directory_config, caplog):
    caplog.set_level(logging.DEBUG)
    contract = resolve_directory_contract(sample_directory_config, obsidian_vault_root)

    actual_dirs = ["00_Inbox", "memory", "summaries", "70_Wiki", "00_Log", ".obsidian", "random_junk"]

    report = detect_directory_drift(contract, obsidian_vault_root, actual_dirs)

    assert report.ok is False
    assert "random_junk" in report.unknown_dirs


# --- Edge case: DirectoryRule construction ---

def test_directory_rule_construction(obsidian_vault_root, sample_directory_config):
    contract = resolve_directory_contract(sample_directory_config, obsidian_vault_root)
    inbox_cp = contract.keys["inbox"]

    rule = DirectoryRule(
        semantic_key="inbox",
        canonical_path=inbox_cp,
        purpose="Default capture destination",
        deprecated_aliases=["Inbox", "capture"],
    )

    assert rule.semantic_key == "inbox"
    assert rule.canonical_path == inbox_cp
    assert rule.purpose == "Default capture destination"
    assert rule.deprecated_aliases == ["Inbox", "capture"]


# --- Edge case: resolve_destination raises when no inbox fallback ---

def test_resolve_destination_raises_when_no_inbox_fallback(tmp_path: Path, caplog):
    caplog.set_level(logging.DEBUG)
    vault = tmp_path / "not_inbox_vault"
    (vault / ".obsidian").mkdir(parents=True)
    (vault / "memory").mkdir(parents=True)
    vault_root = str(vault)

    config_map = {"memory": "memory"}
    contract = resolve_directory_contract(config_map, vault_root)

    with pytest.raises(VaultLayoutError) as exc_info:
        resolve_destination_for_intent("unknown_thing", contract)

    assert exc_info.value.error_code == "UNRESOLVABLE_INTENT"


# --- Edge case: resolve resolves via alias in contract aliases ---

def test_resolve_via_contract_alias(obsidian_vault_root, sample_directory_config):
    contract = resolve_directory_contract(sample_directory_config, obsidian_vault_root)

    result = resolve_destination_for_intent("inbox", contract)

    assert result == contract.keys["inbox"].absolute_path


# --- Edge case: deprecated aliases are rejected as write targets ---

def test_deprecated_alias_rejected_as_write_target(obsidian_vault_root, caplog):
    caplog.set_level(logging.DEBUG)
    config_map = {
        "inbox": "00_Inbox",
        "memory": "memory",
    }
    contract = resolve_directory_contract(config_map, obsidian_vault_root)

    with pytest.raises(DeprecatedAliasError) as exc_info:
        resolve_destination_for_intent("mem", contract)

    assert exc_info.value.error_code == "DEPRECATED_ALIAS_DENIED"
    assert exc_info.value.alias == "mem"
    assert exc_info.value.canonical_key == "memory"

    entries = _parse_logs(caplog)
    denied = next(e for e in entries if e.get("event") == "vault_layout.destination.denied")
    assert denied["level"] == "WARNING"
    assert denied["error_code"] == "DEPRECATED_ALIAS_DENIED"


# --- Edge case: non-deprecated key resolves correctly ---

def test_canonical_key_resolves_directly(obsidian_vault_root):
    config_map = {
        "inbox": "00_Inbox",
        "memory": "memory",
    }
    contract = resolve_directory_contract(config_map, obsidian_vault_root)

    result = resolve_destination_for_intent("inbox", contract)
    assert result == contract.keys["inbox"].absolute_path

    result = resolve_destination_for_intent("memory", contract)
    assert result == contract.keys["memory"].absolute_path
