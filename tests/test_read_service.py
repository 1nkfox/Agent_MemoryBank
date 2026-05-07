"""Module-local tests for M-004 VaultReadService."""

import hashlib
import json
import logging

import pytest

from memory_mcp.config import load_config
from memory_mcp.read_service import (
    NoteMetadata,
    PolicyDeniedError,
    ReadResult,
    check_file_exists,
    get_note_metadata,
    list_allowed_paths,
    read_note,
)
from memory_mcp.vault_fs import compute_revision

PROFILE = "readonly_agent"


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
    return load_config(config_dict)


class TestReadNote:
    def test_read_returns_content_and_sha256_revision(self, temp_vault_root, sample_config_dict, caplog, trace_assert):
        caplog.set_level(logging.DEBUG)
        config = _make_config(temp_vault_root, sample_config_dict)

        path = "memory/note.md"
        expected_text = (temp_vault_root / path).read_text()

        result = read_note(PROFILE, path, config)

        assert isinstance(result, ReadResult)
        assert result.content == expected_text
        assert result.revision == hashlib.sha256(expected_text.encode("utf-8")).hexdigest()
        assert result.revision == compute_revision(expected_text)
        assert result.metadata.path == path
        assert result.metadata.revision == result.revision
        assert result.metadata.size == (temp_vault_root / path).stat().st_size
        assert result.metadata.exists is True

        entries = _parse_logs(caplog)
        assert trace_assert(entries, "read.started", "read.completed")


class TestGetNoteMetadata:
    def test_read_returns_metadata(self, temp_vault_root, sample_config_dict):
        config = _make_config(temp_vault_root, sample_config_dict)

        metadata = get_note_metadata(PROFILE, "memory/note.md", config)

        assert isinstance(metadata, NoteMetadata)
        assert metadata.path == "memory/note.md"
        assert metadata.exists is True
        assert metadata.size > 0

        expected_text = (temp_vault_root / "memory" / "note.md").read_text()
        expected_revision = hashlib.sha256(expected_text.encode("utf-8")).hexdigest()
        assert metadata.revision == expected_revision
        assert metadata.size == (temp_vault_root / "memory" / "note.md").stat().st_size

    def test_metadata_for_missing_file(self, temp_vault_root, sample_config_dict):
        config = _make_config(temp_vault_root, sample_config_dict)

        metadata = get_note_metadata(PROFILE, "memory/nonexistent.md", config)

        assert metadata.path == "memory/nonexistent.md"
        assert metadata.exists is False
        assert metadata.revision == ""
        assert metadata.size == 0


class TestDenylistRejection:
    def test_denylisted_path_not_readable(self, temp_vault_root, sample_config_dict, caplog, trace_assert):
        caplog.set_level(logging.DEBUG)
        config = _make_config(temp_vault_root, sample_config_dict)

        with pytest.raises(PolicyDeniedError) as exc_info:
            read_note(PROFILE, "private/secret.md", config)

        assert "private" in str(exc_info.value).lower()
        assert exc_info.value.error_code == "POLICY_DENIED"

        entries = _parse_logs(caplog)
        assert trace_assert(entries, "read.started", "policy.denied")
        path_resolved_events = [e for e in entries if e.get("event") == "path.resolved"]
        assert len(path_resolved_events) == 0, "M-005 should not be called for denied paths"


class TestTraceOrder:
    def test_cannot_call_filesystem_before_policy_approval(self, temp_vault_root, sample_config_dict, caplog, trace_assert):
        caplog.set_level(logging.DEBUG)
        config = _make_config(temp_vault_root, sample_config_dict)

        read_note(PROFILE, "memory/note.md", config)

        entries = _parse_logs(caplog)
        assert trace_assert(entries, "policy.decision", "path.resolved")


class TestCheckFileExists:
    def test_file_exists_returns_true(self, temp_vault_root, sample_config_dict):
        config = _make_config(temp_vault_root, sample_config_dict)

        assert check_file_exists(PROFILE, "memory/note.md", config) is True

    def test_file_exists_returns_false(self, temp_vault_root, sample_config_dict):
        config = _make_config(temp_vault_root, sample_config_dict)

        assert check_file_exists(PROFILE, "inbox/nonexistent.md", config) is False

    def test_file_exists_denied_path_raises(self, temp_vault_root, sample_config_dict):
        config = _make_config(temp_vault_root, sample_config_dict)

        with pytest.raises(PolicyDeniedError):
            check_file_exists(PROFILE, "private/secret.md", config)


class TestListAllowedPaths:
    def test_list_allowed_paths_yields_allowlisted_md_files(self, temp_vault_root, sample_config_dict):
        config = _make_config(temp_vault_root, sample_config_dict)

        paths = list_allowed_paths(PROFILE, config)

        assert isinstance(paths, list)
        assert "memory/note.md" in paths
        assert "inbox/draft.md" in paths
        assert "private/secret.md" not in paths

    def test_list_allowed_paths_excludes_propose_only_paths(self, temp_vault_root, sample_config_dict):
        config = _make_config(temp_vault_root, sample_config_dict)
        (temp_vault_root / "70_Wiki" / "WikiPage.md").write_text("# Wiki content")

        paths = list_allowed_paths(PROFILE, config)

        assert "70_Wiki/WikiPage.md" not in paths
