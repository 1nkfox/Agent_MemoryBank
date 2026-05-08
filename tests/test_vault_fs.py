"""Module-local tests for M-005 VaultFilesystemRepository."""

import json
import logging
import os
import shutil
from pathlib import Path

import pytest

from memory_mcp.vault_fs import (
    PathTraversalError,
    PathValidationError,
    SymlinkDeniedError,
    compute_revision,
    move_file_atomic,
    read_file,
    resolve_vault_path,
    write_file_atomic,
)


def _parse_logs(caplog) -> list[dict]:
    entries = []
    for record in caplog.records:
        try:
            entries.append(json.loads(record.message))
        except json.JSONDecodeError:
            pass
    return entries


# --- Scenario 1: Normalized path stays under vault root ---

def test_normalized_path_stays_under_vault_root(temp_vault_root, caplog, trace_assert):
    caplog.set_level(logging.DEBUG)
    vault_root = str(temp_vault_root)

    result = resolve_vault_path(vault_root, "./memory/note.md")

    assert result.startswith(vault_root + os.sep)
    assert result.endswith("note.md")

    entries = _parse_logs(caplog)
    trace_assert(entries, "path.resolved")

    resolved_entry = next(e for e in entries if e.get("event") == "path.resolved")
    assert resolved_entry["level"] == "DEBUG"
    assert resolved_entry["function"] == "resolve_vault_path"
    assert resolved_entry["block"] == "M-005"
    assert resolved_entry["data"]["vault_root"] == vault_root
    assert resolved_entry["data"]["relative_path"] == "./memory/note.md"


# --- Scenario 2: Path traversal rejected ---

def test_path_traversal_rejected(temp_vault_root, caplog, trace_assert):
    caplog.set_level(logging.DEBUG)
    vault_root = str(temp_vault_root)

    with pytest.raises(PathTraversalError) as exc_info:
        resolve_vault_path(vault_root, "../../../etc/passwd")

    assert exc_info.value.error_code == "FS_PATH_TRAVERSAL"

    entries = _parse_logs(caplog)
    trace_assert(entries, "path.denied")

    denied_entry = next(e for e in entries if e.get("event") == "path.denied")
    assert denied_entry["level"] == "ERROR"
    assert denied_entry["error_code"] == "FS_PATH_TRAVERSAL"
    assert denied_entry["function"] == "resolve_vault_path"


# --- Scenario 3: Windows traversal rejected (platform-aware) ---

def test_windows_traversal_rejected(temp_vault_root, caplog):
    caplog.set_level(logging.DEBUG)
    vault_root = str(temp_vault_root)

    traversal_path = os.path.join("..", "windows", "system32")

    with pytest.raises(PathTraversalError):
        resolve_vault_path(vault_root, traversal_path)


# --- Scenario 4: Symlink file rejected ---

def test_symlink_file_rejected(temp_vault_root, caplog, trace_assert):
    if os.name == "nt":
        pytest.skip("Symlink creation requires admin privileges on Windows")

    caplog.set_level(logging.DEBUG)
    vault_root = str(temp_vault_root)
    vault_path = Path(vault_root)

    link_path = vault_path / "memory" / "link.md"
    target = vault_path / "private" / "secret.md"
    link_path.symlink_to(os.path.relpath(target, link_path.parent))

    with pytest.raises(SymlinkDeniedError) as exc_info:
        resolve_vault_path(vault_root, "memory/link.md")

    assert exc_info.value.error_code == "FS_SYMLINK_DENIED"

    entries = _parse_logs(caplog)
    trace_assert(entries, "path.denied")

    denied_entry = next(e for e in entries if e.get("event") == "path.denied")
    assert denied_entry["error_code"] == "FS_SYMLINK_DENIED"
    assert denied_entry["function"] == "resolve_vault_path"


# --- Scenario 5: Symlink parent component rejected ---

def test_symlink_parent_component_rejected(temp_vault_root, caplog):
    if os.name == "nt":
        pytest.skip("Symlink creation requires admin privileges on Windows")

    caplog.set_level(logging.DEBUG)
    vault_root = str(temp_vault_root)
    vault_path = Path(vault_root)

    other_dir = vault_path / "other"
    other_dir.mkdir(exist_ok=True)
    (other_dir / "note.md").write_text("# Other note")

    memory_dir = vault_path / "memory"
    shutil.rmtree(memory_dir)
    memory_dir.symlink_to("other")

    with pytest.raises(SymlinkDeniedError) as exc_info:
        resolve_vault_path(vault_root, "memory/note.md")

    assert exc_info.value.error_code == "FS_SYMLINK_DENIED"

    entries = _parse_logs(caplog)
    denied_entry = next(e for e in entries if e.get("event") == "path.denied")
    assert denied_entry["error_code"] == "FS_SYMLINK_DENIED"


# --- Scenario 6: Atomic write uses temp then rename ---

def test_atomic_write_uses_temp_then_rename(temp_vault_root, caplog, trace_assert):
    caplog.set_level(logging.DEBUG)
    vault_root = str(temp_vault_root)

    write_file_atomic(vault_root, "inbox/new_note.md", "# New Note\n\nContent.")

    target_path = Path(vault_root) / "inbox" / "new_note.md"
    assert target_path.exists()
    assert target_path.read_text() == "# New Note\n\nContent."

    tmp_files = list(target_path.parent.glob("*.tmp"))
    assert len(tmp_files) == 0, f"Temp files left behind: {tmp_files}"

    entries = _parse_logs(caplog)
    trace_assert(entries, "path.resolved", "vault.atomic_write.completed")

    write_entry = next(e for e in entries if e.get("event") == "vault.atomic_write.completed")
    assert write_entry["level"] == "INFO"
    assert write_entry["function"] == "write_file_atomic"
    assert write_entry["block"] == "M-005"


# --- Scenario 7: compute_revision returns sha256 ---

def test_compute_revision_returns_sha256():
    import hashlib

    expected = hashlib.sha256("hello world\n".encode("utf-8")).hexdigest()
    assert compute_revision("hello world\n") == expected
    assert len(compute_revision("hello world\n")) == 64


# --- Scenario 8: Null byte in path rejected ---

def test_null_byte_in_path_rejected(temp_vault_root, caplog):
    caplog.set_level(logging.DEBUG)
    vault_root = str(temp_vault_root)

    with pytest.raises(PathValidationError):
        resolve_vault_path(vault_root, "memory/\0hidden.md")

    entries = _parse_logs(caplog)
    denied_entry = next(e for e in entries if e.get("event") == "path.denied")
    assert denied_entry["error_code"] == "PATH_VALIDATION_ERROR"
    assert denied_entry["function"] == "resolve_vault_path"


# --- Additional: read_file returns content ---

def test_read_file_returns_content(temp_vault_root, caplog):
    caplog.set_level(logging.DEBUG)
    vault_root = str(temp_vault_root)

    content = read_file(vault_root, "memory/note.md")

    assert "Test Note" in content
    assert "tags: [demo, test]" in content

    entries = _parse_logs(caplog)
    assert any(e.get("event") == "path.resolved" for e in entries)


# --- Additional: move_file_atomic succeeds ---

def test_move_file_atomic_succeeds(temp_vault_root, caplog, trace_assert):
    caplog.set_level(logging.DEBUG)
    vault_root = str(temp_vault_root)
    vault_path = Path(vault_root)

    src_path = vault_path / "inbox" / "draft.md"
    assert src_path.exists()

    move_file_atomic(vault_root, "inbox/draft.md", "memory/archived.md")

    assert not src_path.exists()

    dest_path = vault_path / "memory" / "archived.md"
    assert dest_path.exists()
    assert "Work in progress" in dest_path.read_text()

    entries = _parse_logs(caplog)
    trace_assert(entries, "path.resolved", "vault.atomic_write.completed")

    write_entry = next(e for e in entries if e.get("event") == "vault.atomic_write.completed")
    assert write_entry["function"] == "move_file_atomic"


# --- Additional: write_file_atomic to nested non-existent directory ---

def test_write_file_atomic_creates_parent_dirs(temp_vault_root, caplog):
    caplog.set_level(logging.DEBUG)
    vault_root = str(temp_vault_root)

    write_file_atomic(vault_root, "summaries/2025/q1.md", "# Q1 Summary\n\nDone.")

    target = Path(vault_root) / "summaries" / "2025" / "q1.md"
    assert target.exists()
    assert target.read_text() == "# Q1 Summary\n\nDone."


# --- Additional: resolve_vault_path allows root itself ---

def test_resolve_vault_path_allows_root_resolution(temp_vault_root, caplog):
    caplog.set_level(logging.DEBUG)
    vault_root = str(temp_vault_root)

    result = resolve_vault_path(vault_root, ".")

    assert result == os.path.normpath(vault_root)

    entries = _parse_logs(caplog)
    assert any(e.get("event") == "path.resolved" for e in entries)
