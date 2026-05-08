"""Module-local tests for M-018 WikiUpdateService."""

from __future__ import annotations

import inspect
import json
import logging
from pathlib import Path

from memory_mcp.config import load_config
from memory_mcp.index_repo import initialize_schema, upsert_note_index
import memory_mcp.wiki_update_service as wiki_update_service
from memory_mcp.wiki_update_service import propose_wiki_update


def _parse_logs(caplog) -> list[dict]:
    entries: list[dict] = []
    for record in caplog.records:
        try:
            entries.append(json.loads(record.message))
        except json.JSONDecodeError:
            pass
    return entries


def _make_config(tmp_path: Path, temp_vault_root: Path, sample_config_dict: dict):
    config_dict = {
        **sample_config_dict,
        "vault": {"root": str(temp_vault_root)},
        "index": {**sample_config_dict["index"], "db_path": str(tmp_path / "index.db")},
    }
    return load_config(config_dict)


def _seed_index(db_path: str) -> None:
    initialize_schema(db_path)
    upsert_note_index(
        db_path,
        "memory/source-note.md",
        "# Source Note\n\nWidget lifecycle details for the wiki.",
        "rev-source",
    )


def test_propose_wiki_update_returns_source_linked_diff(tmp_path, temp_vault_root, sample_config_dict, caplog):
    caplog.set_level(logging.DEBUG)
    config = _make_config(tmp_path, temp_vault_root, sample_config_dict)
    _seed_index(config.index.db_path)
    (temp_vault_root / "70_Wiki" / "Widget.md").write_text("# Widget\n\nOld notes.\n", encoding="utf-8")

    proposal = propose_wiki_update(
        "wiki_maintainer",
        "70_Wiki/Widget.md",
        "widget",
        "# Widget\n\nUpdated lifecycle notes.",
        config,
    )

    assert proposal.wiki_path == "70_Wiki/Widget.md"
    assert proposal.dry_run is True
    assert proposal.source_paths == ["memory/source-note.md"]
    assert "Updated lifecycle notes." in proposal.diff
    assert "[[memory/source-note.md]]" in proposal.diff

    events = [entry.get("event") for entry in _parse_logs(caplog)]
    assert "wiki.sources.selected" in events
    assert "wiki.diff.generated" in events


def test_no_direct_write_to_70_wiki():
    source = inspect.getsource(wiki_update_service)

    assert "write_file_atomic" not in source
    assert "move_file_atomic" not in source
    assert not hasattr(wiki_update_service, "write_file_atomic")
    assert not hasattr(wiki_update_service, "move_file_atomic")


def test_no_autonomous_write_call_path_exists():
    public_names = [name for name in dir(wiki_update_service) if not name.startswith("_")]

    assert "propose_wiki_update" in public_names
    assert "find_wiki_sources" in public_names
    assert "generate_wiki_diff" in public_names
    assert "write_wiki" not in public_names
