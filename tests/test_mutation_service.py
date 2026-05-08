"""Module-local tests for M-008 VaultMutationService."""

from __future__ import annotations

import inspect
import json
import logging
from pathlib import Path

import pytest

import memory_mcp.mutation_service as mutation_service
from memory_mcp.config import load_config
from memory_mcp.concurrency import compute_content_hash
from memory_mcp.mutation_service import (
    MutationError,
    append_note,
    create_note,
    edit_note,
)


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
    return config


@pytest.mark.asyncio
async def test_create_note_goes_through_full_pipeline(temp_vault_root, sample_config_dict, caplog):
    caplog.set_level(logging.DEBUG)
    config = _make_config(temp_vault_root, sample_config_dict)
    path = "memory/created.md"

    result = await create_note(
        "trusted_writer",
        path,
        "# Created\n\nCreated through M-008.",
        config,
        dry_run=False,
    )

    assert result.success is True
    assert path in result.affected_paths
    assert result.audit_event_id
    assert (temp_vault_root / path).exists()

    events = [entry.get("event") for entry in _parse_logs(caplog)]
    assert "policy.decision" in events
    assert "vault.atomic_write.completed" in events
    assert "audit.authoritative_event.appended" in events
    assert "mutation.affected_paths.returned" in events


@pytest.mark.asyncio
async def test_append_note_returns_affected_paths(temp_vault_root, sample_config_dict, caplog):
    caplog.set_level(logging.DEBUG)
    config = _make_config(temp_vault_root, sample_config_dict)
    path = "memory/note.md"
    content = (temp_vault_root / path).read_text(encoding="utf-8")
    expected_revision = compute_content_hash(content)

    result = await append_note(
        "trusted_writer",
        path,
        {"operation": "append_under_heading", "heading": "Test Note", "text": "Appended line."},
        config,
        expected_revision,
        dry_run=False,
    )

    assert result.success is True
    assert path in result.affected_paths
    assert "Appended line." in (temp_vault_root / path).read_text(encoding="utf-8")
    assert "mutation.affected_paths.returned" in [entry.get("event") for entry in _parse_logs(caplog)]


@pytest.mark.asyncio
async def test_edit_note_with_replace_exact_block_succeeds_with_matching_revision(temp_vault_root, sample_config_dict):
    config = _make_config(temp_vault_root, sample_config_dict)
    path = "memory/block_test.md"
    file_path = temp_vault_root / path
    file_path.write_text(
        "# Block Test\n\nBefore.\n\n<!-- BEGIN:REPLACE -->\nOld block.\n<!-- END:REPLACE -->\n\nAfter.\n",
        encoding="utf-8",
    )
    expected_revision = compute_content_hash(file_path.read_text(encoding="utf-8"))

    result = await edit_note(
        "trusted_writer",
        path,
        {
            "operation": "replace_exact_block",
            "start_marker": "<!-- BEGIN:REPLACE -->",
            "end_marker": "<!-- END:REPLACE -->",
            "new_content": "<!-- BEGIN:REPLACE -->\nNew block.\n<!-- END:REPLACE -->",
        },
        config,
        expected_revision,
        dry_run=False,
    )

    current = file_path.read_text(encoding="utf-8")
    assert result.success is True
    assert "New block." in current
    assert "Old block." not in current


@pytest.mark.asyncio
async def test_edit_without_dry_run_rejected(temp_vault_root, sample_config_dict):
    config = _make_config(temp_vault_root, sample_config_dict)
    path = "memory/note.md"
    file_path = temp_vault_root / path
    original = file_path.read_text(encoding="utf-8")
    expected_revision = compute_content_hash(original)

    result = await edit_note(
        "trusted_writer",
        path,
        {"operation": "replace_exact_text", "old_text": "This is a test note.", "new_text": "Changed."},
        config,
        expected_revision,
        dry_run=True,
    )

    assert result.success is True
    assert result.dry_run is True
    assert result.audit_event_id == ""
    assert file_path.read_text(encoding="utf-8") == original


@pytest.mark.asyncio
async def test_edit_without_expected_revision_rejected(temp_vault_root, sample_config_dict):
    config = _make_config(temp_vault_root, sample_config_dict)

    with pytest.raises(MutationError, match="expected_revision is required"):
        await edit_note(
            "trusted_writer",
            "memory/note.md",
            {"operation": "replace_exact_text", "old_text": "This", "new_text": "That"},
            config,
            "",
            dry_run=True,
        )


def test_M_008_does_NOT_import_M_014():
    source = inspect.getsource(mutation_service)
    assert "index_refresh" not in source
    import_lines = [line for line in source.splitlines() if line.startswith("import ") or line.startswith("from ")]
    assert all("M-014" not in line for line in import_lines)
