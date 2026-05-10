"""Module-local tests for M-023 MemBankInitializationService."""

import json
import logging
import os
from pathlib import Path

import pytest

from memory_mcp.config import load_config
from memory_mcp.membank_init_service import (
    MODULE_BLOCK,
    PROJECTION_FILE,
    InitAction,
    MemBankInitPlan,
    MemBankInitResult,
    apply_init_plan,
    create_init_plan,
    membank_init,
)
from memory_mcp.observability import new_trace_id
from memory_mcp.vault_layout import resolve_directory_contract


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


def _list_dirs(vault_root: Path) -> set[str]:
    return {d.name for d in vault_root.iterdir() if d.is_dir()}


def _list_files(vault_root: Path) -> set[str]:
    return {f.name for f in vault_root.iterdir() if f.is_file()}


# --- M-023-S-001: check_only_returns_report_without_writes (DET) ---

@pytest.mark.asyncio
async def test_check_only_returns_report_without_writes(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    vault = tmp_path / "vault"
    (vault / ".obsidian").mkdir(parents=True)
    (vault / "00_Inbox").mkdir(parents=True)
    (vault / "memory").mkdir(parents=True)
    vault_root = str(vault)

    config_map = {
        "inbox": "00_Inbox",
        "memory": "memory",
        "summaries": "summaries",
        "wiki": "70_Wiki",
    }
    directory_contract = resolve_directory_contract(config_map, vault_root)

    dirs_before = set(os.listdir(vault_root))
    files_before = {f for f in os.listdir(vault_root) if os.path.isfile(os.path.join(vault_root, f))}

    result = await membank_init(
        vault_root=vault_root,
        mode="check_only",
        agent_id="test-agent",
        trace_id=new_trace_id(),
        directory_contract=directory_contract,
        profile="trusted_writer",
    )

    dirs_after = set(os.listdir(vault_root))
    files_after = {f for f in os.listdir(vault_root) if os.path.isfile(os.path.join(vault_root, f))}

    assert isinstance(result, MemBankInitResult)
    assert "missing_canonical" in result.message
    assert "summaries" in result.message
    assert "wiki" in result.message

    assert dirs_before == dirs_after, "check_only should not create any directories"
    assert files_before == files_after, "check_only should not create any files"


# --- M-023-S-002: dry_run_returns_init_plan_without_writes — TA-028 emitted (DET + TRACE) ---

@pytest.mark.asyncio
async def test_dry_run_returns_init_plan_without_writes(tmp_path, caplog, trace_assert):
    caplog.set_level(logging.DEBUG)
    vault = tmp_path / "vault"
    (vault / ".obsidian").mkdir(parents=True)
    (vault / "00_Inbox").mkdir(parents=True)
    vault_root = str(vault)

    config_map = {
        "inbox": "00_Inbox",
        "memory": "memory",
        "summaries": "summaries",
    }
    directory_contract = resolve_directory_contract(config_map, vault_root)

    dirs_before = _list_dirs(vault)

    result = await membank_init(
        vault_root=vault_root,
        mode="dry_run",
        agent_id="test-agent",
        trace_id=new_trace_id(),
        directory_contract=directory_contract,
        profile="trusted_writer",
    )

    dirs_after = _list_dirs(vault)

    assert result.success is True
    assert "missing" in result.message.lower()
    assert len(result.actions_taken) > 0

    assert dirs_before == dirs_after, "dry_run should not create any directories"
    assert not (vault / "memory").is_dir()
    assert not (vault / "summaries").is_dir()

    entries = _parse_logs(caplog)
    trace_assert(entries, "membank_init.plan.created")

    ta_entry = next(e for e in entries if e.get("event") == "membank_init.plan.created")
    assert ta_entry["level"] == "INFO"
    assert ta_entry["function"] == "create_init_plan"
    assert ta_entry["block"] == MODULE_BLOCK


# --- M-023-S-003: apply_creates_missing_canonical_directories_and_audit_event —
#     TA-029 TA-030 and TA-008 emitted (DET + TRACE) ---

@pytest.mark.asyncio
async def test_apply_creates_missing_canonical_directories_and_audit_event(
    tmp_path, caplog, sample_config_dict
):
    caplog.set_level(logging.DEBUG)
    vault = tmp_path / "vault"
    (vault / ".obsidian").mkdir(parents=True)
    (vault / "00_Inbox").mkdir(parents=True)
    vault_root = str(vault)
    config = _make_config(vault, sample_config_dict)

    config_map = {
        "inbox": "00_Inbox",
        "memory": "memory",
        "summaries": "summaries",
    }
    directory_contract = resolve_directory_contract(config_map, vault_root)

    result = await membank_init(
        vault_root=vault_root,
        mode="apply",
        agent_id="test-agent",
        trace_id=new_trace_id(),
        directory_contract=directory_contract,
        profile="trusted_writer",
        config=config,
    )

    assert result.success is True
    assert (vault / "memory").is_dir()
    assert (vault / "summaries").is_dir()

    created_types = {a.type for a in result.actions_taken if a.status == "completed"}
    assert "CREATE_DIR" in created_types

    assert result.audit_event_id is not None

    audit_file = vault / ".obsidian" / "audit.ndjson"
    assert audit_file.exists()
    audit_content = audit_file.read_text(encoding="utf-8")
    assert "init" in audit_content
    assert "test-agent" in audit_content

    entries = _parse_logs(caplog)
    events_found = {e.get("event") for e in entries}
    assert "membank_init.apply.started" in events_found
    assert "membank_init.apply.completed" in events_found
    assert "audit.authoritative_event.appended" in events_found

    started = next(e for e in entries if e.get("event") == "membank_init.apply.started")
    assert started["level"] == "INFO"
    assert started["block"] == MODULE_BLOCK

    completed = next(e for e in entries if e.get("event") == "membank_init.apply.completed")
    assert completed["level"] == "INFO"
    assert completed["block"] == MODULE_BLOCK

    audit_entry = next(e for e in entries if e.get("event") == "audit.authoritative_event.appended")
    assert audit_entry["block"] == "M-009"


# --- M-023-S-004: apply_is_idempotent — running twice produces same result (DET) ---

@pytest.mark.asyncio
async def test_apply_is_idempotent(tmp_path, caplog, sample_config_dict):
    caplog.set_level(logging.DEBUG)
    vault = tmp_path / "vault"
    (vault / ".obsidian").mkdir(parents=True)
    (vault / "00_Inbox").mkdir(parents=True)
    vault_root = str(vault)
    config = _make_config(vault, sample_config_dict)

    config_map = {
        "inbox": "00_Inbox",
        "memory": "memory",
    }
    directory_contract = resolve_directory_contract(config_map, vault_root)

    result1 = await membank_init(
        vault_root=vault_root,
        mode="apply",
        agent_id="test-agent",
        trace_id=new_trace_id(),
        directory_contract=directory_contract,
        profile="trusted_writer",
        config=config,
    )
    assert result1.success is True
    assert (vault / "memory").is_dir()

    dirs_after_first = _list_dirs(vault)
    files_after_first = _list_files(vault)

    result2 = await membank_init(
        vault_root=vault_root,
        mode="apply",
        agent_id="test-agent",
        trace_id=new_trace_id(),
        directory_contract=directory_contract,
        profile="trusted_writer",
        config=config,
    )
    assert result2.success is True

    dirs_after_second = _list_dirs(vault)
    files_after_second = _list_files(vault)

    assert dirs_after_first == dirs_after_second
    assert files_after_first == files_after_second

    assert (vault / "memory").is_dir()


# --- M-023-F-001: readonly_profile_cannot_initialize (DET) ---

@pytest.mark.asyncio
async def test_readonly_profile_cannot_initialize(tmp_path, caplog, sample_config_dict):
    caplog.set_level(logging.DEBUG)
    vault = tmp_path / "vault"
    (vault / ".obsidian").mkdir(parents=True)
    (vault / "00_Inbox").mkdir(parents=True)
    vault_root = str(vault)
    config = _make_config(vault, sample_config_dict)

    config_map = {"inbox": "00_Inbox", "memory": "memory"}
    directory_contract = resolve_directory_contract(config_map, vault_root)

    result = await membank_init(
        vault_root=vault_root,
        mode="apply",
        agent_id="test-agent",
        trace_id=new_trace_id(),
        directory_contract=directory_contract,
        profile="readonly_agent",
        config=config,
    )

    assert result.success is False
    assert "readonly_agent" in result.message
    assert not (vault / "memory").is_dir()

    entries = _parse_logs(caplog)
    denied_entries = [e for e in entries if e.get("event") == "membank_init.apply.denied"]
    assert len(denied_entries) >= 1


# --- M-023-F-002: ambiguous_drift_blocks_apply_without_explicit_config —
#     TA-031 emitted (DET + TRACE) ---

@pytest.mark.asyncio
async def test_ambiguous_drift_blocks_apply_without_explicit_config(
    tmp_path, caplog, trace_assert, sample_config_dict
):
    caplog.set_level(logging.DEBUG)
    vault = tmp_path / "vault"
    (vault / ".obsidian").mkdir(parents=True)
    (vault / "00_Inbox").mkdir(parents=True)
    (vault / "memory").mkdir(parents=True)
    (vault / "random_junk").mkdir(parents=True)
    (vault / "capture").mkdir(parents=True)
    vault_root = str(vault)
    config = _make_config(vault, sample_config_dict)

    config_map = {"inbox": "00_Inbox", "memory": "memory"}
    directory_contract = resolve_directory_contract(config_map, vault_root)

    result = await membank_init(
        vault_root=vault_root,
        mode="apply",
        agent_id="test-agent",
        trace_id=new_trace_id(),
        directory_contract=directory_contract,
        profile="trusted_writer",
        config=config,
    )

    assert result.success is False
    assert "Ambiguous drift blocks apply" in result.message or "drift" in result.message.lower()

    entries = _parse_logs(caplog)
    trace_assert(entries, "membank_init.drift_blocked")

    drift_entry = next(e for e in entries if e.get("event") == "membank_init.drift_blocked")
    assert drift_entry["level"] == "WARNING"
    assert drift_entry["block"] == MODULE_BLOCK
    assert "unknown_dirs" in drift_entry.get("data", {})
    assert "random_junk" in drift_entry.get("data", {}).get("unknown_dirs", [])


# --- Additional: Init creates AgentMemBank.md projection file ---

@pytest.mark.asyncio
async def test_apply_creates_projection_file(tmp_path, caplog, sample_config_dict):
    caplog.set_level(logging.DEBUG)
    vault = tmp_path / "vault"
    (vault / ".obsidian").mkdir(parents=True)
    (vault / "00_Inbox").mkdir(parents=True)
    (vault / "memory").mkdir(parents=True)
    (vault / "summaries").mkdir(parents=True)
    (vault / "70_Wiki").mkdir(parents=True)
    vault_root = str(vault)
    config = _make_config(vault, sample_config_dict)

    config_map = {
        "inbox": "00_Inbox",
        "memory": "memory",
        "summaries": "summaries",
        "wiki": "70_Wiki",
    }
    directory_contract = resolve_directory_contract(config_map, vault_root)

    projection_path = vault / PROJECTION_FILE
    assert not projection_path.exists()

    result = await membank_init(
        vault_root=vault_root,
        mode="apply",
        agent_id="test-agent",
        trace_id=new_trace_id(),
        directory_contract=directory_contract,
        profile="trusted_writer",
        config=config,
    )

    assert result.success is True
    assert projection_path.exists()
    content = projection_path.read_text(encoding="utf-8")
    assert "# Agent MemBank Directory" in content
    assert "00_Inbox" in content
    assert "memory" in content
    assert "summaries" in content
    assert "70_Wiki" in content


# --- Additional: Dry_run plan shows correct missing directories ---

@pytest.mark.asyncio
async def test_dry_run_plan_shows_correct_missing_directories(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    vault = tmp_path / "vault"
    (vault / ".obsidian").mkdir(parents=True)
    (vault / "00_Inbox").mkdir(parents=True)
    vault_root = str(vault)

    config_map = {
        "inbox": "00_Inbox",
        "memory": "memory",
        "summaries": "summaries",
        "wiki": "70_Wiki",
    }
    directory_contract = resolve_directory_contract(config_map, vault_root)

    plan = create_init_plan(vault_root, directory_contract)

    assert isinstance(plan, MemBankInitPlan)
    assert "memory" in plan.missing_directories
    assert "summaries" in plan.missing_directories
    assert "wiki" in plan.missing_directories
    assert "inbox" not in plan.missing_directories
    assert plan.projection_needed is True
    assert plan.is_idempotent is False

    create_actions = [a for a in plan.actions if a.type == "CREATE_DIR"]
    skip_actions = [a for a in plan.actions if a.type == "SKIP_EXISTS"]
    assert len(create_actions) == 3
    assert len(skip_actions) == 1


# --- Additional: Apply with no missing directories is no-op ---

@pytest.mark.asyncio
async def test_apply_with_no_missing_directories_is_noop(tmp_path, caplog, sample_config_dict):
    caplog.set_level(logging.DEBUG)
    vault = tmp_path / "vault"
    (vault / ".obsidian").mkdir(parents=True)
    (vault / "00_Inbox").mkdir(parents=True)
    (vault / "memory").mkdir(parents=True)
    (vault / "summaries").mkdir(parents=True)
    (vault / "70_Wiki").mkdir(parents=True)
    vault_root = str(vault)
    config = _make_config(vault, sample_config_dict)

    config_map = {
        "inbox": "00_Inbox",
        "memory": "memory",
        "summaries": "summaries",
        "wiki": "70_Wiki",
    }
    directory_contract = resolve_directory_contract(config_map, vault_root)

    dirs_before = _list_dirs(vault)

    result = await membank_init(
        vault_root=vault_root,
        mode="apply",
        agent_id="test-agent",
        trace_id=new_trace_id(),
        directory_contract=directory_contract,
        profile="trusted_writer",
        config=config,
    )

    dirs_after = _list_dirs(vault)

    assert result.success is True
    assert dirs_before == dirs_after

    create_actions = [a for a in result.actions_taken if a.type == "CREATE_DIR" and a.status == "completed"]
    assert len(create_actions) == 0, "Should not create directories that already exist"


# --- Edge case: apply_init_plan directly with readonly profile ---

@pytest.mark.asyncio
async def test_apply_init_plan_rejects_readonly_directly(tmp_path, caplog, sample_config_dict):
    caplog.set_level(logging.DEBUG)
    vault = tmp_path / "vault"
    (vault / ".obsidian").mkdir(parents=True)
    vault_root = str(vault)

    config_map = {"inbox": "00_Inbox", "memory": "memory"}
    directory_contract = resolve_directory_contract(config_map, vault_root)
    plan = create_init_plan(vault_root, directory_contract)

    result = await apply_init_plan(
        plan=plan,
        vault_root=vault_root,
        agent_id="test-agent",
        trace_id=new_trace_id(),
        config=None,
        profile="readonly_agent",
    )

    assert result.success is False
    assert "readonly_agent" in result.message


# --- Edge case: unknown mode returns error ---

@pytest.mark.asyncio
async def test_unknown_mode_returns_error(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    vault = tmp_path / "vault"
    (vault / ".obsidian").mkdir(parents=True)
    vault_root = str(vault)

    config_map = {"inbox": "00_Inbox"}
    directory_contract = resolve_directory_contract(config_map, vault_root)

    result = await membank_init(
        vault_root=vault_root,
        mode="invalid_mode",
        agent_id="test-agent",
        trace_id=new_trace_id(),
        directory_contract=directory_contract,
        profile="trusted_writer",
    )

    assert result.success is False
    assert "Invalid" in result.message or "Unknown" in result.message or "unknown" in result.message.lower()


# --- Edge case: create_init_plan when all dirs and projection exist (idempotent) ---

def test_create_init_plan_idempotent_when_all_exist(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    vault = tmp_path / "vault"
    (vault / ".obsidian").mkdir(parents=True)
    (vault / "00_Inbox").mkdir(parents=True)
    (vault / "memory").mkdir(parents=True)
    (vault / PROJECTION_FILE).write_text("# Agent MemBank Directory\n", encoding="utf-8")
    vault_root = str(vault)

    config_map = {"inbox": "00_Inbox", "memory": "memory"}
    directory_contract = resolve_directory_contract(config_map, vault_root)

    plan = create_init_plan(vault_root, directory_contract)

    assert plan.is_idempotent is True
    assert plan.missing_directories == []
    assert plan.projection_needed is False
    assert all(a.type == "SKIP_EXISTS" for a in plan.actions)
