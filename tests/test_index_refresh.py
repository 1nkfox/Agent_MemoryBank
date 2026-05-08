"""Module-local tests for M-014 IndexRefreshService."""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path

from memory_mcp.config import load_config
from memory_mcp.index_repo import initialize_schema, upsert_note_index
from memory_mcp import index_refresh
from memory_mcp.index_refresh import refresh_index, refresh_paths, refresh_wiki_folder, startup_reconcile
from memory_mcp.vault_fs import compute_revision

PROFILE = "admin"


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
    config.index.db_path = str(temp_vault_root / ".obsidian" / "index.db")
    return config


def _index_rows(db_path: str) -> dict[str, str]:
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute("SELECT path, revision FROM index_state ORDER BY path").fetchall()
    return {path: revision for path, revision in rows}


def _note_metadata(db_path: str, path: str) -> dict:
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT metadata_json FROM notes WHERE path = ?",
            (path,),
        ).fetchone()
    return json.loads(row[0])


def test_refresh_paths_updates_affected_notes_only_and_emits_markers(
    temp_vault_root,
    sample_config_dict,
    caplog,
):
    caplog.set_level(logging.DEBUG)
    config = _make_config(temp_vault_root, sample_config_dict)
    stale_content = "# Stale\n\nIndexed content."
    changed_content = "---\ntags: [fresh]\n---\n\n# Changed\n\nUpdated body #live."
    (temp_vault_root / "memory" / "changed.md").write_text(changed_content, encoding="utf-8")
    (temp_vault_root / "memory" / "ignored.md").write_text("# Ignored\n", encoding="utf-8")

    initialize_schema(config.index.db_path)
    upsert_note_index(config.index.db_path, "memory/changed.md", stale_content, "stale-rev")
    upsert_note_index(config.index.db_path, "memory/ignored.md", "# Ignored\n", "ignored-rev")

    result = refresh_paths(PROFILE, ["memory/changed.md"], config)

    rows = _index_rows(config.index.db_path)
    assert result.success is True
    assert result.updated_paths == ["memory/changed.md"]
    assert result.deleted_paths == []
    assert rows["memory/changed.md"] == compute_revision(changed_content)
    assert rows["memory/ignored.md"] == "ignored-rev"
    assert _note_metadata(config.index.db_path, "memory/changed.md")["tags"] == ["fresh", "live"]

    events = [entry.get("event") for entry in _parse_logs(caplog)]
    assert "index.refresh.started" in events
    assert "index.refresh.completed" in events


def test_refresh_paths_deletes_missing_markdown_paths(temp_vault_root, sample_config_dict):
    config = _make_config(temp_vault_root, sample_config_dict)
    initialize_schema(config.index.db_path)
    upsert_note_index(config.index.db_path, "memory/missing.md", "# Missing\n", "missing-rev")

    result = refresh_paths(PROFILE, ["memory/missing.md"], config)

    assert result.success is True
    assert result.updated_paths == []
    assert result.deleted_paths == ["memory/missing.md"]
    assert "memory/missing.md" not in _index_rows(config.index.db_path)


def test_refresh_wiki_folder_indexes_wiki_markdown_files(
    temp_vault_root,
    sample_config_dict,
    caplog,
):
    caplog.set_level(logging.DEBUG)
    config = _make_config(temp_vault_root, sample_config_dict)
    root_wiki = "# Wiki Root\n\nKnowledge base #wiki."
    nested_wiki = "# Nested Wiki\n\nLinked [[Wiki Root]]."
    (temp_vault_root / "70_Wiki" / "Root.md").write_text(root_wiki, encoding="utf-8")
    (temp_vault_root / "70_Wiki" / "nested").mkdir()
    (temp_vault_root / "70_Wiki" / "nested" / "Nested.md").write_text(nested_wiki, encoding="utf-8")
    (temp_vault_root / "70_Wiki" / "ignored.txt").write_text("not markdown", encoding="utf-8")

    result = refresh_wiki_folder(PROFILE, config)

    rows = _index_rows(config.index.db_path)
    assert result.success is True
    assert result.updated_paths == ["70_Wiki/Root.md", "70_Wiki/nested/Nested.md"]
    assert result.deleted_paths == []
    assert rows["70_Wiki/Root.md"] == compute_revision(root_wiki)
    assert rows["70_Wiki/nested/Nested.md"] == compute_revision(nested_wiki)
    assert "70_Wiki/ignored.txt" not in rows

    events = [entry.get("event") for entry in _parse_logs(caplog)]
    assert "index.refresh.started" in events
    assert "index.refresh.completed" in events


def test_index_refresh_does_not_import_direct_write_functions():
    assert not hasattr(index_refresh, "write_file_atomic")
    assert not hasattr(index_refresh, "move_file_atomic")


def test_startup_reconcile_detects_external_change_and_updates_index_state(
    temp_vault_root,
    sample_config_dict,
):
    config = _make_config(temp_vault_root, sample_config_dict)
    path = "memory/note.md"
    original = (temp_vault_root / path).read_text(encoding="utf-8")
    initialize_schema(config.index.db_path)
    upsert_note_index(config.index.db_path, path, original, compute_revision(original))

    changed = original + "\n\nExternal edit."
    (temp_vault_root / path).write_text(changed, encoding="utf-8")

    result = startup_reconcile(PROFILE, config)

    rows = _index_rows(config.index.db_path)
    assert result.success is True
    assert path in result.updated_paths
    assert rows[path] == compute_revision(changed)


def test_refresh_index_treats_vault_as_source_of_truth_not_cached_index_state(
    temp_vault_root,
    sample_config_dict,
):
    config = _make_config(temp_vault_root, sample_config_dict)
    path = "memory/note.md"
    current = (temp_vault_root / path).read_text(encoding="utf-8")
    initialize_schema(config.index.db_path)
    upsert_note_index(config.index.db_path, path, "# Cached\n\nOld index content.", "cached-rev")

    result = refresh_index(PROFILE, config)

    rows = _index_rows(config.index.db_path)
    assert result.success is True
    assert path in result.updated_paths
    assert rows[path] == compute_revision(current)
