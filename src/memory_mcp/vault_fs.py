"""Vault filesystem repository — M-005 VaultFilesystemRepository."""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path

from memory_mcp.observability import (
    ErrorCode,
    log_trace_anchor,
    new_trace_id,
)

MODULE = "vault_fs"
MODULE_BLOCK = "M-005"


class PathTraversalError(Exception):
    def __init__(self, message: str, error_code: str = ErrorCode.FS_PATH_TRAVERSAL):
        self.error_code = error_code
        super().__init__(message)


class SymlinkDeniedError(Exception):
    def __init__(self, message: str, error_code: str = ErrorCode.FS_SYMLINK_DENIED):
        self.error_code = error_code
        super().__init__(message)


class PathValidationError(Exception):
    def __init__(self, message: str, error_code: str = "PATH_VALIDATION_ERROR"):
        self.error_code = error_code
        super().__init__(message)


def _check_symlinks(full_path: str, norm_root: str, vault_root: str, relative_path: str, trace_id: str) -> None:
    path_obj = Path(full_path)
    norm_root_path = Path(norm_root)

    if path_obj == norm_root_path:
        return

    if path_obj.is_symlink():
        log_trace_anchor(
            level="ERROR", event="path.denied", trace_id=trace_id,
            module=MODULE, function="resolve_vault_path",
            block=MODULE_BLOCK, error_code=ErrorCode.FS_SYMLINK_DENIED,
            data={"symlink_component": str(path_obj), "vault_root": vault_root, "relative_path": relative_path},
        )
        raise SymlinkDeniedError(
            f"Symlink detected in path component: {path_obj}",
            ErrorCode.FS_SYMLINK_DENIED,
        )

    current = path_obj.parent
    while current != norm_root_path:
        if current.is_symlink():
            log_trace_anchor(
                level="ERROR", event="path.denied", trace_id=trace_id,
                module=MODULE, function="resolve_vault_path",
                block=MODULE_BLOCK, error_code=ErrorCode.FS_SYMLINK_DENIED,
                data={"symlink_component": str(current), "vault_root": vault_root, "relative_path": relative_path},
            )
            raise SymlinkDeniedError(
                f"Symlink detected in parent component: {current}",
                ErrorCode.FS_SYMLINK_DENIED,
            )
        current = current.parent


def resolve_vault_path(vault_root: str, relative_path: str) -> str:
    trace_id = new_trace_id()

    if "\0" in relative_path or "\0" in vault_root:
        log_trace_anchor(
            level="ERROR", event="path.denied", trace_id=trace_id,
            module=MODULE, function="resolve_vault_path",
            block=MODULE_BLOCK, error_code="PATH_VALIDATION_ERROR",
            data={"vault_root": vault_root, "relative_path": relative_path},
        )
        raise PathValidationError("Path contains null bytes")

    norm_root = os.path.normpath(vault_root)
    full_path = os.path.normpath(os.path.join(norm_root, relative_path))

    if not full_path.startswith(norm_root + os.sep) and full_path != norm_root:
        log_trace_anchor(
            level="ERROR", event="path.denied", trace_id=trace_id,
            module=MODULE, function="resolve_vault_path",
            block=MODULE_BLOCK, error_code=ErrorCode.FS_PATH_TRAVERSAL,
            data={"vault_root": vault_root, "requested_path": relative_path, "resolved_path": full_path},
        )
        raise PathTraversalError(
            f"Path traversal detected: {relative_path} resolves outside vault root",
            ErrorCode.FS_PATH_TRAVERSAL,
        )

    _check_symlinks(full_path, norm_root, vault_root, relative_path, trace_id)

    log_trace_anchor(
        level="DEBUG", event="path.resolved", trace_id=trace_id,
        module=MODULE, function="resolve_vault_path",
        block=MODULE_BLOCK, data={"vault_root": vault_root, "relative_path": relative_path, "resolved_path": full_path},
    )

    return full_path


def read_file(vault_root: str, relative_path: str) -> str:
    resolved = resolve_vault_path(vault_root, relative_path)
    with open(resolved, "r", encoding="utf-8") as f:
        return f.read()


def write_file_atomic(vault_root: str, relative_path: str, content: str) -> None:
    trace_id = new_trace_id()
    resolved = resolve_vault_path(vault_root, relative_path)

    os.makedirs(os.path.dirname(resolved), exist_ok=True)

    fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(resolved), suffix=".tmp", prefix=".vault_write_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp_path, resolved)
    except BaseException:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise

    log_trace_anchor(
        level="INFO", event="vault.atomic_write.completed", trace_id=trace_id,
        module=MODULE, function="write_file_atomic",
        block=MODULE_BLOCK, data={"vault_root": vault_root, "relative_path": relative_path},
    )


def move_file_atomic(vault_root: str, source_rel: str, dest_rel: str) -> None:
    trace_id = new_trace_id()
    resolved_src = resolve_vault_path(vault_root, source_rel)
    resolved_dst = resolve_vault_path(vault_root, dest_rel)

    os.makedirs(os.path.dirname(resolved_dst), exist_ok=True)
    os.replace(resolved_src, resolved_dst)

    log_trace_anchor(
        level="INFO", event="vault.atomic_write.completed", trace_id=trace_id,
        module=MODULE, function="move_file_atomic",
        block=MODULE_BLOCK, data={"vault_root": vault_root, "source": source_rel, "dest": dest_rel},
    )


def compute_revision(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()
