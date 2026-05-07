"""Module-local tests for M-007 LockAndRevisionService."""

import json
import logging
from pathlib import Path

import pytest

from memory_mcp.concurrency import (
    RevisionCheckResult,
    acquire_path_lock,
    compute_content_hash,
    recheck_revision,
)


def _parse_logs(caplog) -> list[dict]:
    entries = []
    for record in caplog.records:
        try:
            entries.append(json.loads(record.message))
        except json.JSONDecodeError:
            pass
    return entries


class TestLockAndRevisionService:

    def test_compute_content_hash_is_sha256(self):
        result = compute_content_hash("hello")
        expected = "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
        assert result == expected

    @pytest.mark.asyncio
    async def test_acquires_lock_and_rechecks_revision(self, temp_vault_root, caplog, trace_assert):
        """ET-TRACE: TA-005 then TA-006 in order."""
        caplog.set_level(logging.DEBUG)
        vault_root = str(temp_vault_root)
        path = str(temp_vault_root / "memory" / "note.md")
        actual_hash = compute_content_hash((temp_vault_root / "memory" / "note.md").read_text())

        async with acquire_path_lock(path):
            recheck_revision(vault_root, "memory/note.md", actual_hash)

        entries = _parse_logs(caplog)
        trace_assert(entries, "lock.acquired", "revision.rechecked", "lock.released")

    @pytest.mark.asyncio
    async def test_matching_revision_allows_proceed(self, temp_vault_root):
        """ET-DET: RevisionCheckResult.ok=true."""
        vault_root = str(temp_vault_root)
        content = (temp_vault_root / "memory" / "note.md").read_text()
        actual_hash = compute_content_hash(content)

        result = recheck_revision(vault_root, "memory/note.md", actual_hash)

        assert result.ok is True
        assert result.current_revision == actual_hash
        assert result.expected_revision == actual_hash

    @pytest.mark.asyncio
    async def test_revision_mismatch_blocks_write(self, temp_vault_root, caplog, trace_assert):
        """ET-DET ET-TRACE: TA-006b emitted; RevisionCheckResult.ok=false."""
        caplog.set_level(logging.DEBUG)
        vault_root = str(temp_vault_root)
        wrong_hash = "deadbeef" * 8

        result = recheck_revision(vault_root, "memory/note.md", wrong_hash)

        assert result.ok is False
        assert result.expected_revision == wrong_hash
        assert result.current_revision != wrong_hash

        entries = _parse_logs(caplog)
        trace_assert(entries, "revision.mismatch")

        mismatch_entry = next(e for e in entries if e.get("event") == "revision.mismatch")
        assert mismatch_entry["level"] == "WARNING"
        assert mismatch_entry["error_code"] == "REVISION_MISMATCH"
        assert mismatch_entry["function"] == "recheck_revision"
        assert mismatch_entry["block"] == "M-007"

    @pytest.mark.asyncio
    async def test_external_sync_changes_file_between_plan_and_write(self, temp_vault_root, caplog, trace_assert):
        """ET-TRACE: recompute before write catches changed hash; conflict returned."""
        caplog.set_level(logging.DEBUG)
        vault_root = str(temp_vault_root)
        file_path = temp_vault_root / "memory" / "note.md"

        original_content = file_path.read_text()
        stale_hash = compute_content_hash(original_content)

        file_path.write_text("modified by external sync")

        result = recheck_revision(vault_root, "memory/note.md", stale_hash)

        assert result.ok is False
        assert result.expected_revision == stale_hash
        assert result.current_revision != stale_hash
        assert result.current_revision == compute_content_hash("modified by external sync")

        entries = _parse_logs(caplog)
        trace_assert(entries, "revision.mismatch")

    @pytest.mark.asyncio
    async def test_concurrent_write_silently_overwriting_newer_hash_is_impossible(
        self, temp_vault_root, caplog, trace_assert
    ):
        """ET-DET: SC-003 validated; no write proceeds with stale revision."""
        caplog.set_level(logging.DEBUG)
        vault_root = str(temp_vault_root)
        file_path = temp_vault_root / "memory" / "note.md"

        original = file_path.read_text()
        hash1 = compute_content_hash(original)

        file_path.write_text("writer B content")
        hash2 = compute_content_hash("writer B content")

        async with acquire_path_lock(str(file_path)):
            result = recheck_revision(vault_root, "memory/note.md", hash1)
            assert result.ok is False, "Must not allow write with stale revision"
            assert result.current_revision == hash2

        async with acquire_path_lock(str(file_path)):
            result2 = recheck_revision(vault_root, "memory/note.md", hash2)
            assert result2.ok is True

        current = file_path.read_text()
        assert current == "writer B content", "SC-003 violation: newer hash was silently overwritten"

        entries = _parse_logs(caplog)
        mismatch_entries = [e for e in entries if e.get("event") == "revision.mismatch"]
        assert len(mismatch_entries) >= 1, "Expected at least one revision.mismatch event"
