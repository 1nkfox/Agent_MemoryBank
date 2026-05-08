"""Module-local tests for M-017 BackupService."""

from __future__ import annotations

import hashlib
import json
import logging
import subprocess
from pathlib import Path

import memory_mcp.backup_service as backup_service
from memory_mcp.backup_service import backup_vault
from memory_mcp.config import load_config


def _parse_logs(caplog) -> list[dict]:
    entries: list[dict] = []
    for record in caplog.records:
        try:
            entries.append(json.loads(record.message))
        except json.JSONDecodeError:
            pass
    return entries


def _make_config(temp_vault_root: Path, sample_config_dict: dict):
    config = load_config(sample_config_dict)
    config.vault.root = str(temp_vault_root)
    config.backup.git_path = str(temp_vault_root / ".git")
    return config


def _hash_vault_files(root: Path) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for path in sorted(p for p in root.rglob("*") if p.is_file() and ".git" not in p.parts):
        hashes[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return hashes


def test_manual_backup_snapshot_completed(monkeypatch, temp_vault_root, sample_config_dict, caplog):
    caplog.set_level(logging.DEBUG)
    config = _make_config(temp_vault_root, sample_config_dict)
    calls: list[list[str]] = []

    def fake_run(args, cwd, capture_output, text, check):
        calls.append(args)
        if args == ["git", "rev-parse", "HEAD"]:
            return subprocess.CompletedProcess(args, 0, stdout="abc123\n", stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="ok\n", stderr="")

    monkeypatch.setattr(backup_service.subprocess, "run", fake_run)

    result = backup_vault(config, "manual backup test")

    assert result.success is True
    assert result.commit_hash == "abc123"
    assert ["git", "add", "-A"] in calls
    assert ["git", "commit", "-m", "manual backup test"] in calls
    assert "backup.snapshot.completed" in [entry.get("event") for entry in _parse_logs(caplog)]


def test_backup_does_not_mutate_vault_content(monkeypatch, temp_vault_root, sample_config_dict):
    config = _make_config(temp_vault_root, sample_config_dict)
    before = _hash_vault_files(temp_vault_root)

    def fake_run(args, cwd, capture_output, text, check):
        if args == ["git", "rev-parse", "HEAD"]:
            return subprocess.CompletedProcess(args, 0, stdout="abc123\n", stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="ok\n", stderr="")

    monkeypatch.setattr(backup_service.subprocess, "run", fake_run)

    result = backup_vault(config)

    after = _hash_vault_files(temp_vault_root)

    assert result.success is True
    assert after == before


def test_rollback_api_absent_in_mvp():
    assert not hasattr(backup_service, "restore_note_revision")
