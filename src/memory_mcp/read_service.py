"""Safe read operations over policy-guarded Vault paths — M-004 VaultReadService."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from memory_mcp.config import ServerConfig
from memory_mcp.observability import (
    ErrorCode,
    log_trace_anchor,
    new_trace_id,
)
from memory_mcp.policy import authorize_operation
from memory_mcp.vault_fs import (
    PathTraversalError,
    PathValidationError,
    SymlinkDeniedError,
    compute_revision,
    read_file,
    resolve_vault_path,
)

MODULE = "read_service"
MODULE_BLOCK = "M-004"

logger = logging.getLogger(__name__)


class PolicyDeniedError(Exception):
    def __init__(self, message: str, error_code: str = ErrorCode.POLICY_DENIED):
        self.error_code = error_code
        super().__init__(message)


class ReadError(Exception):
    def __init__(self, message: str, error_code: str = "READ_ERROR"):
        self.error_code = error_code
        super().__init__(message)


@dataclass
class NoteMetadata:
    path: str
    revision: str
    size: int
    exists: bool


@dataclass
class ReadResult:
    content: str
    revision: str
    metadata: NoteMetadata


def read_note(profile: str, path: str, config: ServerConfig) -> ReadResult:
    trace_id = new_trace_id()

    log_trace_anchor(
        "INFO", "read.started",
        trace_id=trace_id, module=MODULE, function="read_note",
        block=MODULE_BLOCK, data={"profile": profile, "path": path},
    )

    decision = authorize_operation(profile, path, "read", False, config)
    if not decision.allowed:
        log_trace_anchor(
            "WARNING", "read.denied",
            trace_id=trace_id, module=MODULE, function="read_note",
            block=MODULE_BLOCK,
            data={"profile": profile, "path": path, "reason": decision.reason},
            error_code=ErrorCode.POLICY_DENIED,
        )
        raise PolicyDeniedError(
            f"Read denied for '{path}': {decision.reason}",
            ErrorCode.POLICY_DENIED,
        )

    try:
        resolved = resolve_vault_path(config.vault.root, path)
    except (PathTraversalError, SymlinkDeniedError, PathValidationError) as exc:
        raise ReadError(
            f"Path resolution failed for '{path}': {exc}",
            getattr(exc, "error_code", "READ_ERROR"),
        )

    if not os.path.exists(resolved):
        raise ReadError(f"File not found: '{path}'")

    try:
        content = read_file(config.vault.root, path)
    except (PathTraversalError, SymlinkDeniedError, PathValidationError) as exc:
        raise ReadError(
            f"Filesystem error reading '{path}': {exc}",
            getattr(exc, "error_code", "READ_ERROR"),
        )

    revision = compute_revision(content)
    metadata = NoteMetadata(
        path=path,
        revision=revision,
        size=os.stat(resolved).st_size,
        exists=True,
    )

    log_trace_anchor(
        "INFO", "read.completed",
        trace_id=trace_id, module=MODULE, function="read_note",
        block=MODULE_BLOCK,
        data={
            "profile": profile, "path": path,
            "revision": revision, "size": metadata.size,
        },
    )

    return ReadResult(content=content, revision=revision, metadata=metadata)


def check_file_exists(profile: str, path: str, config: ServerConfig) -> bool:
    decision = authorize_operation(profile, path, "read", False, config)
    if not decision.allowed:
        raise PolicyDeniedError(
            f"Access denied for '{path}': {decision.reason}",
            ErrorCode.POLICY_DENIED,
        )

    try:
        resolved = resolve_vault_path(config.vault.root, path)
    except (PathTraversalError, SymlinkDeniedError, PathValidationError):
        return False

    return os.path.exists(resolved)


def list_allowed_paths(profile: str, config: ServerConfig) -> list[str]:
    vault_root = config.vault.root
    allowlist = config.policy.allowlist_roots
    result: list[str] = []

    for root in allowlist:
        root_path = os.path.join(vault_root, root)
        if not os.path.isdir(root_path):
            continue
        for dirpath, dirnames, filenames in os.walk(root_path):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for fname in filenames:
                if not fname.endswith(".md"):
                    continue
                rel_path = os.path.relpath(
                    os.path.join(dirpath, fname), vault_root
                ).replace("\\", "/")
                decision = authorize_operation(profile, rel_path, "read", False, config)
                if decision.allowed:
                    result.append(rel_path)

    return sorted(result)


def get_note_metadata(profile: str, path: str, config: ServerConfig) -> NoteMetadata:
    decision = authorize_operation(profile, path, "read", False, config)
    if not decision.allowed:
        raise PolicyDeniedError(
            f"Access denied for '{path}': {decision.reason}",
            ErrorCode.POLICY_DENIED,
        )

    try:
        resolved = resolve_vault_path(config.vault.root, path)
    except (PathTraversalError, SymlinkDeniedError, PathValidationError):
        return NoteMetadata(path=path, revision="", size=0, exists=False)

    if not os.path.exists(resolved):
        return NoteMetadata(path=path, revision="", size=0, exists=False)

    try:
        content = read_file(config.vault.root, path)
    except (PathTraversalError, SymlinkDeniedError, PathValidationError):
        return NoteMetadata(path=path, revision="", size=0, exists=False)

    revision = compute_revision(content)
    size = os.stat(resolved).st_size

    return NoteMetadata(path=path, revision=revision, size=size, exists=True)
