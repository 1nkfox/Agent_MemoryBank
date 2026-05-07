"""Lock and revision concurrency control — M-007 LockAndRevisionService."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import AsyncIterator

from memory_mcp.observability import (
    ErrorCode,
    log_trace_anchor,
    new_trace_id,
)
from memory_mcp.vault_fs import compute_revision as _fs_compute_revision
from memory_mcp.vault_fs import read_file

MODULE = "concurrency"
MODULE_BLOCK = "M-007"

_locks: dict[str, asyncio.Lock] = {}


def _get_lock(path: str) -> asyncio.Lock:
    if path not in _locks:
        _locks[path] = asyncio.Lock()
    return _locks[path]


@dataclass
class RevisionCheckResult:
    ok: bool
    current_revision: str
    expected_revision: str


@asynccontextmanager
async def acquire_path_lock(path: str) -> AsyncIterator[None]:
    trace_id = new_trace_id()
    lock = _get_lock(path)
    await lock.acquire()
    log_trace_anchor(
        level="DEBUG", event="lock.acquired", trace_id=trace_id,
        module=MODULE, function="acquire_path_lock",
        block=MODULE_BLOCK, data={"path": path},
    )
    try:
        yield
    finally:
        lock.release()
        log_trace_anchor(
            level="DEBUG", event="lock.released", trace_id=trace_id,
            module=MODULE, function="acquire_path_lock",
            block=MODULE_BLOCK, data={"path": path},
        )


def recheck_revision(vault_root: str, relative_path: str, expected_revision: str) -> RevisionCheckResult:
    trace_id = new_trace_id()
    content = read_file(vault_root, relative_path)
    current_revision = _fs_compute_revision(content)

    if current_revision == expected_revision:
        log_trace_anchor(
            level="DEBUG", event="revision.rechecked", trace_id=trace_id,
            module=MODULE, function="recheck_revision",
            block=MODULE_BLOCK, data={
                "vault_root": vault_root,
                "relative_path": relative_path,
                "current_revision": current_revision,
                "expected_revision": expected_revision,
            },
        )
        return RevisionCheckResult(ok=True, current_revision=current_revision, expected_revision=expected_revision)

    log_trace_anchor(
        level="WARNING", event="revision.mismatch", trace_id=trace_id,
        module=MODULE, function="recheck_revision",
        block=MODULE_BLOCK, error_code=ErrorCode.REVISION_MISMATCH,
        data={
            "vault_root": vault_root,
            "relative_path": relative_path,
            "current_revision": current_revision,
            "expected_revision": expected_revision,
        },
    )
    return RevisionCheckResult(ok=False, current_revision=current_revision, expected_revision=expected_revision)


def compute_content_hash(content: str) -> str:
    return _fs_compute_revision(content)
