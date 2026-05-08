"""Central MCP server entrypoint — M-001 MCPServerEntrypoint."""

from __future__ import annotations

import asyncio
import inspect
import logging
from dataclasses import dataclass
from typing import Any, Callable

from memory_mcp.auth import AgentIdentity, AuthError, resolve_identity
from memory_mcp.backup_service import backup_health as _svc_backup_health
from memory_mcp.backup_service import backup_vault as _svc_backup_vault
from memory_mcp.config import ServerConfig
from memory_mcp.draft_promotion_service import promote_note as _svc_promote_note
from memory_mcp.observability import (
    ErrorCode,
    log_trace_anchor,
    new_trace_id,
)
from memory_mcp.policy import authorize_operation
from memory_mcp.read_service import read_note as _svc_read_note

logger = logging.getLogger(__name__)

MODULE = "memory_mcp.server"
MODULE_BLOCK = "M-001"

ToolHandler = Callable[..., Any]

tools: dict[str, ToolHandler] = {}


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
            "vault_fs",
            "backup_service",
            "draft_promotion_service",
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
register_tool("backup_vault", _handler_backup_vault)
register_tool("backup_health", _handler_backup_health)
register_tool("promote_note", _handler_promote_note)
