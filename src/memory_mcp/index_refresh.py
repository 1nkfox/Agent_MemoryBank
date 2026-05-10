"""Index refresh orchestration - M-014 IndexRefreshService."""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from typing import Any

from memory_mcp.config import ServerConfig
from memory_mcp.index_repo import delete_note_index, initialize_schema, upsert_note_index
from memory_mcp.markdown_parser import parse_markdown
from memory_mcp.observability import ErrorCode, log_trace_anchor, new_trace_id
from memory_mcp.policy import authorize_operation
from memory_mcp.vault_fs import (
    PathTraversalError,
    PathValidationError,
    SymlinkDeniedError,
    compute_revision,
    read_file,
    resolve_vault_path,
)

MODULE = "index_refresh"
MODULE_BLOCK = "M-014"


@dataclass
class RefreshResult:
    success: bool
    updated_paths: list[str]
    deleted_paths: list[str]


class RefreshError(Exception):
    def __init__(self, message: str, error_code: str = "REFRESH_ERROR"):
        self.error_code = error_code
        super().__init__(message)


def _log_started(trace_id: str, function: str, profile: str, paths: list[str] | None = None) -> None:
    data: dict[str, Any] = {"profile": profile}
    if paths is not None:
        data["paths"] = paths
    log_trace_anchor(
        "INFO",
        "index.refresh.started",
        trace_id=trace_id,
        module=MODULE,
        function=function,
        block=MODULE_BLOCK,
        data=data,
    )


def _log_completed(trace_id: str, function: str, profile: str, result: RefreshResult) -> None:
    log_trace_anchor(
        "INFO",
        "index.refresh.completed",
        trace_id=trace_id,
        module=MODULE,
        function=function,
        block=MODULE_BLOCK,
        data={
            "profile": profile,
            "success": result.success,
            "updated_paths": result.updated_paths,
            "deleted_paths": result.deleted_paths,
        },
    )


def _metadata_from_content(content: str) -> dict[str, Any]:
    parsed = parse_markdown(content)
    return {
        "frontmatter": parsed.frontmatter,
        "tags": parsed.tags,
        "links": parsed.links,
        "headings": parsed.headings,
    }


def _authorize_refresh(profile: str, path: str, config: ServerConfig) -> None:
    decision = authorize_operation(profile, path, "refresh", dry_run=False, config=config)
    if not decision.allowed:
        raise RefreshError(
            f"Refresh denied for '{path}': {decision.reason}",
            ErrorCode.POLICY_DENIED,
        )


def _path_exists(config: ServerConfig, path: str) -> bool:
    try:
        resolved = resolve_vault_path(config.vault.root, path)
    except (PathTraversalError, PathValidationError, SymlinkDeniedError) as exc:
        raise RefreshError(
            f"Path resolution failed for '{path}': {exc}",
            getattr(exc, "error_code", "REFRESH_ERROR"),
        ) from exc
    return os.path.exists(resolved)


def _upsert_path(profile: str, path: str, config: ServerConfig) -> None:
    _authorize_refresh(profile, path, config)
    try:
        content = read_file(config.vault.root, path)
    except (PathTraversalError, PathValidationError, SymlinkDeniedError) as exc:
        raise RefreshError(
            f"Filesystem error reading '{path}': {exc}",
            getattr(exc, "error_code", "REFRESH_ERROR"),
        ) from exc
    revision = compute_revision(content)
    upsert_note_index(
        config.index.db_path,
        path,
        content,
        revision,
        _metadata_from_content(content),
    )


def _walk_allowlist_markdown_paths(config: ServerConfig) -> list[str]:
    result: list[str] = []
    for root in config.policy.allowlist_roots:
        root_path = os.path.join(config.vault.root, root)
        if not os.path.isdir(root_path):
            continue
        for dirpath, dirnames, filenames in os.walk(root_path):
            dirnames[:] = [dirname for dirname in dirnames if not dirname.startswith(".")]
            for filename in filenames:
                if not filename.endswith(".md"):
                    continue
                rel_path = os.path.relpath(
                    os.path.join(dirpath, filename),
                    config.vault.root,
                ).replace("\\", "/")
                result.append(rel_path)
    return sorted(set(result))


def _walk_wiki_markdown_paths(config: ServerConfig) -> list[str]:
    wiki_root = os.path.join(config.vault.root, "70_Wiki")
    if not os.path.isdir(wiki_root):
        return []

    result: list[str] = []
    for dirpath, dirnames, filenames in os.walk(wiki_root):
        dirnames[:] = [dirname for dirname in dirnames if not dirname.startswith(".")]
        for filename in filenames:
            if not filename.endswith(".md"):
                continue
            rel_path = os.path.relpath(
                os.path.join(dirpath, filename),
                config.vault.root,
            ).replace("\\", "/")
            result.append(rel_path)
    return sorted(result)


def _wiki_derivative_refresh_config(config: ServerConfig) -> ServerConfig:
    policy = replace(
        config.policy,
        propose_only_roots=[
            root for root in config.policy.propose_only_roots if root.rstrip("/") != "70_Wiki"
        ],
    )
    return replace(config, policy=policy)


def refresh_paths(profile: str, paths: list[str], config: ServerConfig) -> RefreshResult:
    trace_id = new_trace_id()
    _log_started(trace_id, "refresh_paths", profile, paths)
    initialize_schema(config.index.db_path)

    updated_paths: list[str] = []
    deleted_paths: list[str] = []

    for path in paths:
        if not path.endswith(".md"):
            continue
        _authorize_refresh(profile, path, config)
        if _path_exists(config, path):
            _upsert_path(profile, path, config)
            updated_paths.append(path)
        else:
            delete_note_index(config.index.db_path, path)
            deleted_paths.append(path)

    result = RefreshResult(success=True, updated_paths=updated_paths, deleted_paths=deleted_paths)
    _log_completed(trace_id, "refresh_paths", profile, result)
    return result


def refresh_wiki_folder(profile: str, config: ServerConfig) -> RefreshResult:
    trace_id = new_trace_id()
    _log_started(trace_id, "refresh_wiki_folder", profile)

    result = refresh_paths(
        profile,
        _walk_wiki_markdown_paths(config),
        _wiki_derivative_refresh_config(config),
    )

    _log_completed(trace_id, "refresh_wiki_folder", profile, result)
    return result


def startup_reconcile(profile: str, config: ServerConfig) -> RefreshResult:
    trace_id = new_trace_id()
    _log_started(trace_id, "startup_reconcile", profile)
    initialize_schema(config.index.db_path)

    updated_paths: list[str] = []
    wiki_config = _wiki_derivative_refresh_config(config)
    for path in _walk_allowlist_markdown_paths(config):
        effective_config = wiki_config if path.startswith("70_Wiki/") else config
        _upsert_path(profile, path, effective_config)
        updated_paths.append(path)

    result = RefreshResult(success=True, updated_paths=updated_paths, deleted_paths=[])
    _log_completed(trace_id, "startup_reconcile", profile, result)
    return result


def refresh_index(profile: str, config: ServerConfig) -> RefreshResult:
    return startup_reconcile(profile, config)
