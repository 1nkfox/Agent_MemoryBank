"""Module-local tests for M-019 DraftPromotionService."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from memory_mcp.config import load_config
from memory_mcp.draft_promotion_service import (
    PromotionConflictError,
    PromotionError,
    PromotionPolicyDeniedError,
    promote_note,
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
async def test_promote_single_note_from_inbox_to_memory(temp_vault_root, sample_config_dict, caplog):
    caplog.set_level(logging.DEBUG)
    config = _make_config(temp_vault_root, sample_config_dict)
    source = "inbox/draft.md"
    dest = "memory/promoted.md"

    result = await promote_note("admin", source, dest, config, dry_run=False)

    assert result.success is True
    assert result.dry_run is False
    assert result.conflict is False
    assert result.audit_event_id
    assert result.affected_paths == [source, dest]
    assert not (temp_vault_root / source).exists()
    assert (temp_vault_root / dest).read_text(encoding="utf-8") == "# Draft\n\nWork in progress."

    events = [entry.get("event") for entry in _parse_logs(caplog)]
    assert "promotion.plan.created" in events
    assert "vault.atomic_write.completed" in events
    assert "audit.authoritative_event.appended" in events
    assert "audit.log_projection.updated" in events
    assert "promotion.completed" in events


@pytest.mark.asyncio
async def test_promote_to_existing_target_rejected(temp_vault_root, sample_config_dict):
    config = _make_config(temp_vault_root, sample_config_dict)

    with pytest.raises(PromotionConflictError, match="Destination already exists"):
        await promote_note("admin", "inbox/draft.md", "memory/note.md", config, dry_run=False)

    assert (temp_vault_root / "inbox/draft.md").exists()
    assert (temp_vault_root / "memory/note.md").exists()


@pytest.mark.asyncio
async def test_bulk_move_rejected(temp_vault_root, sample_config_dict):
    config = _make_config(temp_vault_root, sample_config_dict)

    with pytest.raises(PromotionError, match="source_path must be a single"):
        await promote_note("admin", ["inbox/draft.md"], "memory/promoted.md", config, dry_run=False)

    with pytest.raises(PromotionError, match="dest_path must be a single"):
        await promote_note("admin", "inbox/draft.md", "memory/a.md,memory/b.md", config, dry_run=False)

    assert (temp_vault_root / "inbox/draft.md").exists()
    assert not (temp_vault_root / "memory" / "promoted.md").exists()


@pytest.mark.asyncio
async def test_readonly_agent_promotion_rejected(temp_vault_root, sample_config_dict):
    config = _make_config(temp_vault_root, sample_config_dict)

    with pytest.raises(PromotionPolicyDeniedError, match="not permitted"):
        await promote_note("readonly_agent", "inbox/draft.md", "memory/promoted.md", config, dry_run=False)

    assert (temp_vault_root / "inbox/draft.md").exists()


@pytest.mark.asyncio
async def test_promote_to_denylisted_folder_rejected(temp_vault_root, sample_config_dict):
    config = _make_config(temp_vault_root, sample_config_dict)

    with pytest.raises(PromotionPolicyDeniedError, match="denylist"):
        await promote_note("admin", "inbox/draft.md", "private/promoted.md", config, dry_run=False)

    assert (temp_vault_root / "inbox/draft.md").exists()
    assert not (temp_vault_root / "private" / "promoted.md").exists()
