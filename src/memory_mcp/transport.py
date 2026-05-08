"""Streamable HTTP MCP transport — M-020 MCPTransport."""

from __future__ import annotations

import json
import logging
from typing import Any

from aiohttp import web

from memory_mcp.config import ServerConfig
from memory_mcp.observability import (
    log_trace_anchor,
    new_trace_id,
)

logger = logging.getLogger(__name__)

MODULE = "memory_mcp.transport"
MODULE_BLOCK = "M-020"

JSONRPC_VERSION = "2.0"
MCP_VERSION = "2024-11-05"

STANDARD_ERROR_CODES: dict[int, str] = {
    -32700: "Parse error",
    -32600: "Invalid Request",
    -32601: "Method not found",
    -32602: "Invalid params",
    -32603: "Internal error",
    -32000: "Server error",
}


def _jsonrpc_response(result: Any, rpc_id: Any) -> dict[str, Any]:
    return {"jsonrpc": JSONRPC_VERSION, "result": result, "id": rpc_id}


def _jsonrpc_error(code: int, message: str, rpc_id: Any, data: Any = None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": JSONRPC_VERSION, "error": error, "id": rpc_id}


def _extract_bearer(request: web.Request) -> str:
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:]
    return ""


async def _handle_mcp_request(
    rpc_request: dict[str, Any],
    config: ServerConfig,
) -> dict[str, Any]:
    from memory_mcp.server import handle_tool_call_async, tools, tool_schemas

    method = rpc_request.get("method", "")
    params = rpc_request.get("params", {})
    rpc_id = rpc_request.get("id")

    if method == "initialize":
        return _jsonrpc_response(
            {
                "protocolVersion": MCP_VERSION,
                "serverInfo": {
                    "name": "memory-mcp",
                    "version": "0.1.0",
                },
                "capabilities": {
                    "tools": {},
                },
            },
            rpc_id,
        )

    if method == "tools/list":
        tool_list = []
        for name in sorted(tools.keys()):
            schema = tool_schemas.get(name, {})
            tool_list.append({
                "name": name,
                "description": schema.get("description", ""),
                "inputSchema": schema.get("inputSchema", {
                    "type": "object",
                    "properties": {},
                }),
            })
        return _jsonrpc_response({"tools": tool_list}, rpc_id)

    if method == "tools/call":
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})
        api_key = params.get("_meta", {}).get("api_key", "")

        if tool_name not in tools:
            return _jsonrpc_error(-32601, f"Tool not found: {tool_name}", rpc_id)

        result = await handle_tool_call_async(tool_name, api_key, arguments, config)

        if result.success:
            return _jsonrpc_response(
                {"content": [{"type": "text", "text": json.dumps(result.data)}]},
                rpc_id,
            )
        return _jsonrpc_error(-32000, result.error_code, rpc_id, str(result.data))

    if method == "ping":
        return _jsonrpc_response({}, rpc_id)

    return _jsonrpc_error(-32601, f"Method not found: {method}", rpc_id)


async def _message_handler(request: web.Request) -> web.Response:
    config: ServerConfig = request.app["config"]

    body = await request.text()
    if not body:
        rpc_id = None
        return web.json_response(
            _jsonrpc_error(-32600, "Empty request body", rpc_id),
            status=400,
        )

    try:
        rpc_request = json.loads(body)
    except json.JSONDecodeError:
        rpc_id = None
        return web.json_response(
            _jsonrpc_error(-32700, "Invalid JSON", rpc_id),
            status=400,
        )

    if not isinstance(rpc_request, dict) or rpc_request.get("jsonrpc") != JSONRPC_VERSION:
        return web.json_response(
            _jsonrpc_error(-32600, "Invalid JSON-RPC request", rpc_request.get("id")),
            status=400,
        )

    api_key = _extract_bearer(request)
    if isinstance(rpc_request.get("params"), dict):
        rpc_request["params"]["_meta"] = {"api_key": api_key}

    try:
        response = await _handle_mcp_request(rpc_request, config)
    except Exception:
        logger.exception("Unhandled error in MCP request")
        rpc_id = rpc_request.get("id")
        return web.json_response(
            _jsonrpc_error(-32603, "Internal error", rpc_id),
            status=500,
        )

    return web.json_response(response)


async def _health_handler(request: web.Request) -> web.Response:
    config: ServerConfig = request.app["config"]
    from memory_mcp.server import health_check

    return web.json_response(health_check(config))


def create_app(config: ServerConfig) -> web.Application:
    app = web.Application()
    app["config"] = config

    app.router.add_post("/message", _message_handler)
    app.router.add_get("/health", _health_handler)

    return app


def run_server(config: ServerConfig) -> None:
    trace_id = new_trace_id()
    log_trace_anchor(
        "INFO", "transport.starting",
        trace_id=trace_id, module=MODULE, function="run_server", block=MODULE_BLOCK,
        data={"host": config.server.host, "port": config.server.port},
    )

    app = create_app(config)
    web.run_app(
        app,
        host=config.server.host,
        port=config.server.port,
        print=lambda msg: logger.info(f"aiohttp: {msg}"),
    )
