"""Central MCP server entrypoint — M-001 MCPServerEntrypoint."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable

from memory_mcp.auth import AgentIdentity, AuthError, resolve_identity
from memory_mcp.config import ServerConfig
from memory_mcp.observability import (
    log_trace_anchor,
    new_trace_id,
)
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
    except Exception as exc:
        error_code = getattr(exc, "error_code", "SERVICE_ERROR")
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
