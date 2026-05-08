"""Central MCP server entrypoint — M-001 MCPServerEntrypoint."""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Coroutine
from dataclasses import dataclass
from typing import Any, Callable

from memory_mcp.auth import AgentIdentity, AuthError, resolve_identity
from memory_mcp.backup_service import backup_health as _svc_backup_health
from memory_mcp.backup_service import backup_vault as _svc_backup_vault
from memory_mcp.config import ServerConfig
from memory_mcp.draft_promotion_service import promote_note as _svc_promote_note
from memory_mcp.index_refresh import refresh_paths as _svc_refresh_paths
from memory_mcp.mutation_service import append_note as _svc_append_note
from memory_mcp.mutation_service import create_note as _svc_create_note
from memory_mcp.mutation_service import edit_note as _svc_edit_note
from memory_mcp.mutation_service import write_note as _svc_write_note
from memory_mcp.observability import (
    ErrorCode,
    log_trace_anchor,
    new_trace_id,
)
from memory_mcp.policy import authorize_operation
from memory_mcp.read_service import read_note as _svc_read_note
from memory_mcp.retrieval_service import search_notes as _svc_search_notes
from memory_mcp.wiki_update_service import propose_wiki_update as _svc_propose_wiki_update

logger = logging.getLogger(__name__)

MODULE = "memory_mcp.server"
MODULE_BLOCK = "M-001"

ToolHandler = Callable[..., Any]

tools: dict[str, ToolHandler] = {}

tool_schemas: dict[str, dict[str, Any]] = {
    "read_note": {
        "description": "Read a note by vault-relative path",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Vault-relative path to the note"},
            },
            "required": ["path"],
        },
    },
    "search_notes": {
        "description": "Full-text search across indexed notes",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query string"},
            },
            "required": ["query"],
        },
    },
    "create_note": {
        "description": "Create a new note",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Vault-relative path for the new note"},
                "content": {"type": "string", "description": "Markdown content"},
                "dry_run": {"type": "boolean", "description": "Preview only, no write"},
            },
            "required": ["path", "content"],
        },
    },
    "append_note": {
        "description": "Append content to an existing note via constrained patch",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Vault-relative path to the note"},
                "patch_spec": {"type": "object", "description": "Patch specification with operation and fields"},
                "expected_revision": {"type": "string", "description": "Current revision for optimistic concurrency"},
                "dry_run": {"type": "boolean", "description": "Preview only, no write"},
            },
            "required": ["path", "patch_spec", "expected_revision"],
        },
    },
    "edit_note": {
        "description": "Edit a note via constrained patch",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Vault-relative path to the note"},
                "patch_spec": {"type": "object", "description": "Patch specification with operation and fields"},
                "expected_revision": {"type": "string", "description": "Current revision for optimistic concurrency"},
                "dry_run": {"type": "boolean", "description": "Preview only, no write"},
            },
            "required": ["path", "patch_spec", "expected_revision"],
        },
    },
    "write_note": {
        "description": "Full overwrite of a note",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Vault-relative path to the note"},
                "content": {"type": "string", "description": "Full new markdown content"},
                "expected_revision": {"type": "string", "description": "Current revision for optimistic concurrency"},
                "dry_run": {"type": "boolean", "description": "Preview only, no write"},
            },
            "required": ["path", "content", "expected_revision"],
        },
    },
    "promote_note": {
        "description": "Move a draft note from inbox to memory",
        "inputSchema": {
            "type": "object",
            "properties": {
                "source_path": {"type": "string", "description": "Source path in inbox"},
                "dest_path": {"type": "string", "description": "Destination path in memory"},
                "dry_run": {"type": "boolean", "description": "Preview only, no move"},
            },
            "required": ["source_path", "dest_path"],
        },
    },
    "propose_wiki_update": {
        "description": "Generate a diff proposal for a wiki page (read-only, no direct wiki write)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "wiki_path": {"type": "string", "description": "Path to wiki page (default: 70_Wiki/wiki.md)"},
                "query": {"type": "string", "description": "Query to find relevant sources"},
                "proposed_content": {"type": "string", "description": "Proposed new content for the wiki page"},
            },
            "required": ["query", "proposed_content"],
        },
    },
    "refresh_paths": {
        "description": "Re-index specified note paths",
        "inputSchema": {
            "type": "object",
            "properties": {
                "paths": {"type": "array", "items": {"type": "string"}, "description": "List of vault-relative paths to re-index"},
            },
            "required": ["paths"],
        },
    },
    "health_check": {
        "description": "Server health check and module listing",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
    "backup_vault": {
        "description": "Trigger a git-backed vault backup",
        "inputSchema": {
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "Commit message for the backup"},
            },
        },
    },
    "backup_health": {
        "description": "Check backup repository health",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
}


@dataclass
class ToolResult:
    success: bool
    data: Any = None
    error_code: str = ""
    trace_id: str = ""


def register_tool(name: str, handler: ToolHandler) -> None:
    tools[name] = handler


def health_check(config: ServerConfig) -> dict[str, Any]:
    return {
        "status": "ok",
        "modules": [
            "config",
            "observability",
            "auth",
            "policy",
            "read_service",
            "mutation_service",
            "retrieval_service",
            "index_repo",
            "index_refresh",
            "wiki_update_service",
            "backup_service",
            "draft_promotion_service",
            "vault_fs",
            "concurrency",
            "change_planner",
            "audit_log",
            "markdown_parser",
            "vector_adapter",
        ],
    }


def _handler_read_note(
    identity: AgentIdentity,
    profile: str,
    params: dict[str, Any],
    config: ServerConfig,
    trace_id: str,
) -> dict[str, Any]:
    path = params.get("path", "")
    result = _svc_read_note(profile, path, config)
    return {
        "content": result.content,
        "revision": result.revision,
        "metadata": {
            "path": result.metadata.path,
            "size": result.metadata.size,
        },
    }


def _authorize_backup(profile: str, config: ServerConfig) -> None:
    decision = authorize_operation(profile, "", "backup", True, config)
    if not decision.allowed:
        raise PermissionError(decision.reason)


def _handler_backup_vault(
    identity: AgentIdentity,
    profile: str,
    params: dict[str, Any],
    config: ServerConfig,
    trace_id: str,
) -> dict[str, Any]:
    _authorize_backup(profile, config)
    result = _svc_backup_vault(config, message=params.get("message", "manual backup"))
    return {
        "success": result.success,
        "commit_hash": result.commit_hash,
        "message": result.message,
    }


def _handler_backup_health(
    identity: AgentIdentity,
    profile: str,
    params: dict[str, Any],
    config: ServerConfig,
    trace_id: str,
) -> dict[str, Any]:
    _authorize_backup(profile, config)
    return _svc_backup_health(config)


async def _handler_promote_note(
    identity: AgentIdentity,
    profile: str,
    params: dict[str, Any],
    config: ServerConfig,
    trace_id: str,
) -> dict[str, Any]:
    result = await _svc_promote_note(
        profile,
        params.get("source_path", ""),
        params.get("dest_path", ""),
        config,
        dry_run=params.get("dry_run", True),
    )
    return {
        "success": result.success,
        "affected_paths": result.affected_paths,
        "audit_event_id": result.audit_event_id,
        "dry_run": result.dry_run,
        "conflict": result.conflict,
    }


def _handler_search_notes(
    identity: AgentIdentity,
    profile: str,
    params: dict[str, Any],
    config: ServerConfig,
    trace_id: str,
) -> dict[str, Any]:
    query = params.get("query", "")
    results = _svc_search_notes(profile, query, config)
    return {
        "results": [
            {
                "path": r.path,
                "snippet": r.snippet,
                "rank": r.rank,
                "revision": r.revision,
                "explanation": r.explanation,
            }
            for r in results
        ],
    }


async def _handler_create_note(
    identity: AgentIdentity,
    profile: str,
    params: dict[str, Any],
    config: ServerConfig,
    trace_id: str,
) -> dict[str, Any]:
    path = params.get("path", "")
    content = params.get("content", "")
    dry_run = params.get("dry_run", True)

    result = await _svc_create_note(profile, path, content, config, dry_run=dry_run)

    if result.success and not dry_run and result.affected_paths:
        _svc_refresh_paths(profile, result.affected_paths, config)

    return {
        "success": result.success,
        "affected_paths": result.affected_paths,
        "audit_event_id": result.audit_event_id,
        "content_hash": result.content_hash,
        "conflict": result.conflict,
        "dry_run": result.dry_run,
    }


async def _handler_append_note(
    identity: AgentIdentity,
    profile: str,
    params: dict[str, Any],
    config: ServerConfig,
    trace_id: str,
) -> dict[str, Any]:
    path = params.get("path", "")
    patch_spec = params.get("patch_spec", {})
    expected_revision = params.get("expected_revision", "")
    dry_run = params.get("dry_run", True)

    result = await _svc_append_note(
        profile, path, patch_spec, config, expected_revision, dry_run=dry_run
    )

    if result.success and not dry_run and result.affected_paths:
        _svc_refresh_paths(profile, result.affected_paths, config)

    return {
        "success": result.success,
        "affected_paths": result.affected_paths,
        "audit_event_id": result.audit_event_id,
        "content_hash": result.content_hash,
        "conflict": result.conflict,
        "dry_run": result.dry_run,
    }


async def _handler_edit_note(
    identity: AgentIdentity,
    profile: str,
    params: dict[str, Any],
    config: ServerConfig,
    trace_id: str,
) -> dict[str, Any]:
    path = params.get("path", "")
    patch_spec = params.get("patch_spec", {})
    expected_revision = params.get("expected_revision", "")
    dry_run = params.get("dry_run", True)

    result = await _svc_edit_note(
        profile, path, patch_spec, config, expected_revision, dry_run=dry_run
    )

    if result.success and not dry_run and result.affected_paths:
        _svc_refresh_paths(profile, result.affected_paths, config)

    return {
        "success": result.success,
        "affected_paths": result.affected_paths,
        "audit_event_id": result.audit_event_id,
        "content_hash": result.content_hash,
        "conflict": result.conflict,
        "dry_run": result.dry_run,
    }


async def _handler_write_note(
    identity: AgentIdentity,
    profile: str,
    params: dict[str, Any],
    config: ServerConfig,
    trace_id: str,
) -> dict[str, Any]:
    path = params.get("path", "")
    content = params.get("content", "")
    expected_revision = params.get("expected_revision", "")
    dry_run = params.get("dry_run", True)

    result = await _svc_write_note(
        profile, path, content, config, expected_revision, dry_run=dry_run
    )

    if result.success and not dry_run and result.affected_paths:
        _svc_refresh_paths(profile, result.affected_paths, config)

    return {
        "success": result.success,
        "affected_paths": result.affected_paths,
        "audit_event_id": result.audit_event_id,
        "content_hash": result.content_hash,
        "conflict": result.conflict,
        "dry_run": result.dry_run,
    }


def _handler_propose_wiki_update(
    identity: AgentIdentity,
    profile: str,
    params: dict[str, Any],
    config: ServerConfig,
    trace_id: str,
) -> dict[str, Any]:
    wiki_path = params.get("wiki_path", "70_Wiki/wiki.md")
    query = params.get("query", "")
    proposed_content = params.get("proposed_content", "")

    proposal = _svc_propose_wiki_update(
        profile, wiki_path, query, proposed_content, config
    )

    return {
        "wiki_path": proposal.wiki_path,
        "diff": proposal.diff,
        "source_paths": proposal.source_paths,
        "dry_run": proposal.dry_run,
    }


def _handler_refresh_paths(
    identity: AgentIdentity,
    profile: str,
    params: dict[str, Any],
    config: ServerConfig,
    trace_id: str,
) -> dict[str, Any]:
    paths = params.get("paths", [])
    result = _svc_refresh_paths(profile, paths, config)
    return {
        "success": result.success,
        "updated_paths": result.updated_paths,
        "deleted_paths": result.deleted_paths,
    }


def _handler_health_check(
    identity: AgentIdentity,
    profile: str,
    params: dict[str, Any],
    config: ServerConfig,
    trace_id: str,
) -> dict[str, Any]:
    return health_check(config)


def _error_code_for_exception(exc: Exception) -> str:
    if isinstance(exc, PermissionError):
        return ErrorCode.POLICY_DENIED
    return getattr(exc, "error_code", "SERVICE_ERROR")


async def handle_tool_call_async(
    tool_name: str,
    api_key: str,
    params: dict[str, Any],
    config: ServerConfig,
) -> ToolResult:
    trace_id = new_trace_id()

    log_trace_anchor(
        "INFO",
        "request.received",
        trace_id=trace_id,
        module=MODULE,
        function="handle_tool_call_async",
        block=MODULE_BLOCK,
        data={"tool_name": tool_name},
    )

    if tool_name not in tools:
        log_trace_anchor(
            "ERROR",
            "request.failed",
            trace_id=trace_id,
            module=MODULE,
            function="handle_tool_call_async",
            block=MODULE_BLOCK,
            data={"tool_name": tool_name, "reason": "tool not registered"},
            error_code="UNKNOWN_TOOL",
        )
        return ToolResult(success=False, error_code="UNKNOWN_TOOL", trace_id=trace_id)

    try:
        identity, profile = resolve_identity(api_key, config)
    except AuthError as exc:
        log_trace_anchor(
            "ERROR",
            "request.failed",
            trace_id=trace_id,
            module=MODULE,
            function="handle_tool_call_async",
            block=MODULE_BLOCK,
            data={"tool_name": tool_name, "reason": str(exc)},
            error_code=exc.error_code,
        )
        return ToolResult(success=False, error_code=exc.error_code, trace_id=trace_id)

    handler = tools[tool_name]

    try:
        result_data = handler(identity, profile, params, config, trace_id)
        if inspect.isawaitable(result_data):
            result_data = await result_data
    except Exception as exc:
        error_code = _error_code_for_exception(exc)
        log_trace_anchor(
            "ERROR",
            "request.failed",
            trace_id=trace_id,
            module=MODULE,
            function="handle_tool_call_async",
            block=MODULE_BLOCK,
            data={"tool_name": tool_name, "reason": str(exc)},
            error_code=error_code,
        )
        return ToolResult(success=False, error_code=error_code, trace_id=trace_id)

    log_trace_anchor(
        "INFO",
        "request.completed",
        trace_id=trace_id,
        module=MODULE,
        function="handle_tool_call_async",
        block=MODULE_BLOCK,
        data={"tool_name": tool_name},
    )

    return ToolResult(success=True, data=result_data, trace_id=trace_id)


def handle_tool_call(
    tool_name: str,
    api_key: str,
    params: dict[str, Any],
    config: ServerConfig,
) -> ToolResult:
    trace_id = new_trace_id()

    log_trace_anchor(
        "INFO",
        "request.received",
        trace_id=trace_id,
        module=MODULE,
        function="handle_tool_call",
        block=MODULE_BLOCK,
        data={"tool_name": tool_name},
    )

    if tool_name not in tools:
        log_trace_anchor(
            "ERROR",
            "request.failed",
            trace_id=trace_id,
            module=MODULE,
            function="handle_tool_call",
            block=MODULE_BLOCK,
            data={"tool_name": tool_name, "reason": "tool not registered"},
            error_code="UNKNOWN_TOOL",
        )
        return ToolResult(success=False, error_code="UNKNOWN_TOOL", trace_id=trace_id)

    try:
        identity, profile = resolve_identity(api_key, config)
    except AuthError as exc:
        log_trace_anchor(
            "ERROR",
            "request.failed",
            trace_id=trace_id,
            module=MODULE,
            function="handle_tool_call",
            block=MODULE_BLOCK,
            data={"tool_name": tool_name, "reason": str(exc)},
            error_code=exc.error_code,
        )
        return ToolResult(success=False, error_code=exc.error_code, trace_id=trace_id)

    handler = tools[tool_name]

    try:
        result_data = handler(identity, profile, params, config, trace_id)
        if inspect.isawaitable(result_data):
            if not isinstance(result_data, Coroutine):
                raise RuntimeError(
                    f"Tool '{tool_name}' returned a non-coroutine awaitable"
                )
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                result_data = asyncio.run(result_data)
            else:
                result_data.close()
                raise RuntimeError(
                    f"Tool '{tool_name}' requires handle_tool_call_async inside an event loop"
                )
    except Exception as exc:
        error_code = _error_code_for_exception(exc)
        log_trace_anchor(
            "ERROR",
            "request.failed",
            trace_id=trace_id,
            module=MODULE,
            function="handle_tool_call",
            block=MODULE_BLOCK,
            data={"tool_name": tool_name, "reason": str(exc)},
            error_code=error_code,
        )
        return ToolResult(success=False, error_code=error_code, trace_id=trace_id)

    log_trace_anchor(
        "INFO",
        "request.completed",
        trace_id=trace_id,
        module=MODULE,
        function="handle_tool_call",
        block=MODULE_BLOCK,
        data={"tool_name": tool_name},
    )

    return ToolResult(success=True, data=result_data, trace_id=trace_id)


register_tool("read_note", _handler_read_note)
register_tool("search_notes", _handler_search_notes)
register_tool("create_note", _handler_create_note)
register_tool("append_note", _handler_append_note)
register_tool("edit_note", _handler_edit_note)
register_tool("write_note", _handler_write_note)
register_tool("promote_note", _handler_promote_note)
register_tool("propose_wiki_update", _handler_propose_wiki_update)
register_tool("refresh_paths", _handler_refresh_paths)
register_tool("health_check", _handler_health_check)
register_tool("backup_vault", _handler_backup_vault)
register_tool("backup_health", _handler_backup_health)


def main() -> None:
    import json
    import os
    import sys

    config_path = os.environ.get("MEMORY_MCP_CONFIG")
    if not config_path:
        print("ERROR: MEMORY_MCP_CONFIG environment variable not set.", file=sys.stderr)
        print("Set MEMORY_MCP_CONFIG to the path of your config JSON file.", file=sys.stderr)
        sys.exit(1)

    try:
        with open(config_path, "r", encoding="utf-8") as fh:
            config_dict = json.load(fh)
    except FileNotFoundError:
        print(f"ERROR: Config file not found: {config_path}", file=sys.stderr)
        sys.exit(1)
    except json.JSONDecodeError as exc:
        print(f"ERROR: Invalid JSON in config file: {exc}", file=sys.stderr)
        sys.exit(1)

    from memory_mcp.config import load_config

    config = load_config(config_dict)

    transport_mode = os.environ.get("MEMORY_MCP_TRANSPORT", "http").lower()

    if transport_mode in ("check", "dry-run"):
        health = health_check(config)
        print(f"Status: {health['status']}")
        print(f"Registered tools ({len(tools)}): {', '.join(sorted(tools.keys()))}")
        print(f"Modules ({len(health['modules'])}): {', '.join(health['modules'])}")
        print()
        print("Transport skipped (check mode). Use MEMORY_MCP_TRANSPORT=http to start the server.")
        return

    try:
        from memory_mcp.transport import run_server

        print(f"Starting MCP server on {config.server.host}:{config.server.port}")
        print(f"Tools: {', '.join(sorted(tools.keys()))}")
        run_server(config)
    except ImportError:
        print("ERROR: aiohttp is required for HTTP transport.", file=sys.stderr)
        print("Install with: pip install aiohttp", file=sys.stderr)
        sys.exit(1)
