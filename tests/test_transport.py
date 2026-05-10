"""Module-local tests for M-020 MCPTransport."""

from __future__ import annotations

import hashlib
import json
import logging

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from memory_mcp.admin_auth import _clear_sessions
from memory_mcp.config import ServerConfig, load_config
from memory_mcp.transport import (
    JSONRPC_VERSION,
    MCP_VERSION,
    create_app,
)

ADMIN_USERNAME = "transport_admin"
ADMIN_PASSWORD = "admin-secret-123"
ADMIN_PASSWORD_HASH = hashlib.sha256(ADMIN_PASSWORD.encode("utf-8")).hexdigest()

READONLY_KEY = "test-key-readonly-001"


def _parse_logs(caplog):
    entries = []
    for record in caplog.records:
        try:
            entries.append(json.loads(record.message))
        except (json.JSONDecodeError, TypeError):
            pass
    return entries


def _make_config(sample_config_dict, temp_vault_root) -> ServerConfig:
    config_dict = dict(sample_config_dict)
    config_dict["vault"]["root"] = str(temp_vault_root)
    config_dict["index"]["db_path"] = str(temp_vault_root / ".obsidian" / "index.db")
    config_dict["audit"]["audit_db_path"] = str(temp_vault_root / ".obsidian" / "audit.db")
    config_dict["audit"]["log_md_path"] = str(temp_vault_root / "LOG.md")
    config_dict["backup"]["git_path"] = str(temp_vault_root / ".git")
    return load_config(config_dict)


def _jsonrpc_request(method: str, params: dict | None = None, rpc_id: int | str = 1) -> dict:
    return {"jsonrpc": JSONRPC_VERSION, "method": method, "params": params or {}, "id": rpc_id}


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def transport_config(sample_config_dict, temp_vault_root):
    return _make_config(sample_config_dict, temp_vault_root)


@pytest.fixture
def transport_app(transport_config):
    return create_app(transport_config)


@pytest.fixture
def transport_app_with_admin(transport_config):
    transport_config.admin_enabled = True
    transport_config.admin_username = ADMIN_USERNAME
    transport_config.admin_password_hash = ADMIN_PASSWORD_HASH
    _clear_sessions()
    app = create_app(transport_config)
    yield app
    _clear_sessions()


# ---------------------------------------------------------------------------
# M-020-S-001: jsonrpc_initialize_returns_capabilities
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_jsonrpc_initialize_returns_capabilities(transport_app):
    async with TestClient(TestServer(transport_app)) as client:
        resp = await client.post(
            "/message",
            json=_jsonrpc_request("initialize"),
        )

        assert resp.status == 200
        data = await resp.json()

        assert data["jsonrpc"] == "2.0"
        assert data["id"] == 1
        assert "result" in data
        assert data["result"]["protocolVersion"] == MCP_VERSION
        assert data["result"]["serverInfo"]["name"] == "memory-mcp"
        assert data["result"]["capabilities"]["tools"] == {}


# ---------------------------------------------------------------------------
# M-020-S-002: tools_list_returns_tools (15 tools)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tools_list_returns_tools(transport_app):
    async with TestClient(TestServer(transport_app)) as client:
        resp = await client.post(
            "/message",
            json=_jsonrpc_request("tools/list"),
        )

        assert resp.status == 200
        data = await resp.json()

        assert data["jsonrpc"] == "2.0"
        assert data["id"] == 1
        assert "result" in data
        assert "tools" in data["result"]
        tools_list = data["result"]["tools"]
        assert len(tools_list) == 15, (
            f"Expected 15 tools, got {len(tools_list)}: "
            f"{[t['name'] for t in tools_list]}"
        )

        tool_names = {t["name"] for t in tools_list}
        assert "read_note" in tool_names
        assert "get_instructions" in tool_names
        assert "membank_init" in tool_names


# ---------------------------------------------------------------------------
# M-020-S-003: tools_call_routes_to_server_handler
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tools_call_routes_to_server_handler(monkeypatch, transport_app):
    async def mock_handle(tool_name, api_key, arguments, config):
        from memory_mcp.server import ToolResult

        return ToolResult(success=True, data={"status": "ok"}, trace_id="trace-001")

    monkeypatch.setattr(
        "memory_mcp.server.handle_tool_call_async", mock_handle
    )

    async with TestClient(TestServer(transport_app)) as client:
        resp = await client.post(
            "/message",
            json=_jsonrpc_request(
                "tools/call",
                {"name": "health_check", "arguments": {}},
            ),
        )

        assert resp.status == 200
        data = await resp.json()
        assert data["jsonrpc"] == "2.0"
        assert data["id"] == 1
        assert "result" in data
        content = data["result"]["content"]
        assert len(content) == 1
        assert content[0]["type"] == "text"
        inner = json.loads(content[0]["text"])
        assert inner["status"] == "ok"


# ---------------------------------------------------------------------------
# M-020-F-001: bearer_auth_passed_to_server
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bearer_auth_passed_to_server(monkeypatch, transport_app):
    captured_api_key = []

    async def mock_handle(tool_name, api_key, arguments, config):
        captured_api_key.append(api_key)
        from memory_mcp.server import ToolResult

        return ToolResult(success=True, data={"key_received": api_key}, trace_id="trace-002")

    monkeypatch.setattr(
        "memory_mcp.server.handle_tool_call_async", mock_handle
    )

    async with TestClient(TestServer(transport_app)) as client:
        resp = await client.post(
            "/message",
            json=_jsonrpc_request(
                "tools/call",
                {"name": "whoami", "arguments": {}},
            ),
            headers={"Authorization": f"Bearer {READONLY_KEY}"},
        )

        assert resp.status == 200
        data = await resp.json()
        content_text = json.loads(data["result"]["content"][0]["text"])
        assert content_text["key_received"] == READONLY_KEY

    assert len(captured_api_key) == 1
    assert captured_api_key[0] == READONLY_KEY


# ---------------------------------------------------------------------------
# M-020-F-002: invalid_json_returns_parse_error
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invalid_json_returns_parse_error(transport_app):
    async with TestClient(TestServer(transport_app)) as client:
        resp = await client.post(
            "/message",
            data="not valid json {{{",
            headers={"Content-Type": "application/json"},
        )

        assert resp.status == 400
        data = await resp.json()
        assert data["jsonrpc"] == "2.0"
        assert "error" in data
        assert data["error"]["code"] == -32700
        assert "Parse error" in data["error"]["message"] or "JSON" in data["error"]["message"]


# ---------------------------------------------------------------------------
# Additional: empty body returns invalid request
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_body_returns_invalid_request(transport_app):
    async with TestClient(TestServer(transport_app)) as client:
        resp = await client.post("/message", data="")

        assert resp.status == 400
        data = await resp.json()
        assert data["error"]["code"] == -32600


# ---------------------------------------------------------------------------
# Additional: unknown method returns method not found
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unknown_method_returns_method_not_found(transport_app):
    async with TestClient(TestServer(transport_app)) as client:
        resp = await client.post(
            "/message",
            json=_jsonrpc_request("nonexistent/method"),
        )

        assert resp.status == 200
        data = await resp.json()
        assert "error" in data
        assert data["error"]["code"] == -32601


# ---------------------------------------------------------------------------
# Additional: non-jsonrpc version returns invalid request
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_jsonrpc_version_returns_invalid_request(transport_app):
    async with TestClient(TestServer(transport_app)) as client:
        resp = await client.post(
            "/message",
            json={"method": "initialize", "id": 1},
        )

        assert resp.status == 400
        data = await resp.json()
        assert data["error"]["code"] == -32600


# ---------------------------------------------------------------------------
# Additional: ping returns empty result
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ping_returns_empty_result(transport_app):
    async with TestClient(TestServer(transport_app)) as client:
        resp = await client.post(
            "/message",
            json=_jsonrpc_request("ping"),
        )

        assert resp.status == 200
        data = await resp.json()
        assert data["jsonrpc"] == "2.0"
        assert data["result"] == {}


# ---------------------------------------------------------------------------
# Additional: GET /health works
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_endpoint_works(transport_app):
    async with TestClient(TestServer(transport_app)) as client:
        resp = await client.get("/health")

        assert resp.status == 200
        data = await resp.json()
        assert data["status"] == "ok"
        assert "modules" in data


# ---------------------------------------------------------------------------
# M-020: admin routes registered on app
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_admin_routes_registered_on_app(transport_app_with_admin):
    routes = list(transport_app_with_admin.router.routes())
    paths = {r.resource.canonical for r in routes if hasattr(r.resource, 'canonical')}

    assert "/admin/login" in paths, f"Admin routes missing. Available: {sorted(paths)}"
    assert "/admin/dashboard" in paths
    assert "/admin/events" in paths
    assert "/admin/errors" in paths
    assert "/admin/export" in paths


# ---------------------------------------------------------------------------
# M-020: health endpoint still works alongside admin routes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_endpoint_still_works_with_admin_routes(transport_app_with_admin):
    async with TestClient(TestServer(transport_app_with_admin)) as client:
        resp = await client.get("/health")

        assert resp.status == 200
        data = await resp.json()
        assert data["status"] == "ok"
        assert "modules" in data


# ---------------------------------------------------------------------------
# M-020: admin routes login page accessible via HTTP GET
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_admin_login_page_accessible(transport_app_with_admin):
    async with TestClient(TestServer(transport_app_with_admin)) as client:
        resp = await client.get("/admin/login")

        assert resp.status == 200
        text = await resp.text()
        assert "Memory Bank Admin" in text or "login" in text.lower()


# ---------------------------------------------------------------------------
# M-020: admin routes not accessible via JSON-RPC
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_admin_routes_not_accessible_via_jsonrpc(transport_app_with_admin):
    async with TestClient(TestServer(transport_app_with_admin)) as client:
        resp = await client.post(
            "/message",
            json=_jsonrpc_request("admin/dashboard"),
        )

        assert resp.status == 200
        data = await resp.json()
        assert "error" in data
        assert data["error"]["code"] == -32601
        assert "Method not found" in data["error"]["message"]


# ---------------------------------------------------------------------------
# M-020: MCP tools/list still returns 15 with admin enabled
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tools_list_returns_15_with_admin_enabled(transport_app_with_admin):
    async with TestClient(TestServer(transport_app_with_admin)) as client:
        resp = await client.post(
            "/message",
            json=_jsonrpc_request("tools/list"),
        )

        assert resp.status == 200
        data = await resp.json()
        tools_list = data["result"]["tools"]
        assert len(tools_list) == 15


# ---------------------------------------------------------------------------
# Additional: tools/call with unknown tool returns error
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tools_call_unknown_tool_returns_error(monkeypatch, transport_app):
    async with TestClient(TestServer(transport_app)) as client:
        resp = await client.post(
            "/message",
            json=_jsonrpc_request(
                "tools/call",
                {"name": "nonexistent_tool", "arguments": {}},
            ),
        )

        assert resp.status == 200
        data = await resp.json()
        assert "error" in data
        assert data["error"]["code"] == -32601
        assert "Tool not found" in data["error"]["message"]


# ---------------------------------------------------------------------------
# Additional: POST to /admin/login with valid credentials
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_admin_login_auth_works(transport_app_with_admin):
    async with TestClient(TestServer(transport_app_with_admin)) as client:
        resp = await client.post(
            "/admin/login",
            data={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD},
            allow_redirects=False,
        )

        assert resp.status in (302, 303)
        location = resp.headers.get("Location", "")
        assert "/admin/dashboard" in location

        session_cookie = resp.cookies.get("admin_session")
        assert session_cookie is not None
        assert len(session_cookie.value) > 0


# ---------------------------------------------------------------------------
# Additional: MCP tools/call still works alongside admin routes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_tools_call_works_with_admin_routes(monkeypatch, transport_app_with_admin):
    async def mock_handle(tool_name, api_key, arguments, config):
        from memory_mcp.server import ToolResult

        return ToolResult(success=True, data={"result": "ok"}, trace_id="trace-003")

    monkeypatch.setattr(
        "memory_mcp.server.handle_tool_call_async", mock_handle
    )

    async with TestClient(TestServer(transport_app_with_admin)) as client:
        resp = await client.post(
            "/message",
            json=_jsonrpc_request(
                "tools/call",
                {"name": "read_note", "arguments": {"path": "test.md"}},
            ),
            headers={"Authorization": f"Bearer {READONLY_KEY}"},
        )

        assert resp.status == 200
        data = await resp.json()
        assert "result" in data
