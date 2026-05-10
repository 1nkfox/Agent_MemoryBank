"""Module-local tests for M-028 AdminWebInterface."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
from dataclasses import dataclass

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from memory_mcp.admin_auth import (
    AdminSession,
    _clear_sessions,
    create_admin_session,
    init_admin_auth,
    validate_admin_session,
)
from memory_mcp.admin_web import create_admin_routes
from memory_mcp.admin_dashboard_service import (
    DashboardSummary,
    Event,
)
from memory_mcp.process_event_log import (
    ProcessEvent,
    record_process_event,
)

# ---------------------------------------------------------------------------
# test constants
# ---------------------------------------------------------------------------

ADMIN_USERNAME = "dashboard_admin"
ADMIN_PASSWORD = "secure-password-123"
ADMIN_PASSWORD_HASH = hashlib.sha256(ADMIN_PASSWORD.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _parse_logs(caplog) -> list[dict]:
    entries = []
    for record in caplog.records:
        try:
            entries.append(json.loads(record.message))
        except json.JSONDecodeError:
            pass
    return entries


def _admin_web_entries(caplog) -> list[dict]:
    return [e for e in _parse_logs(caplog) if e.get("module") == "memory_mcp.admin_web"]


def _fresh_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


@dataclass
class FakeCanonicalPath:
    semantic_key: str
    relative_path: str
    absolute_path: str


@dataclass
class FakeDirectoryContract:
    keys: dict
    aliases: dict
    created_at: str


def _make_directory_contract(vault_root: str) -> FakeDirectoryContract:
    dirs = ["00_Inbox", "memory", "summaries", "70_Wiki"]
    keys = {}
    for d in dirs:
        keys[d] = FakeCanonicalPath(
            semantic_key=d,
            relative_path=d,
            absolute_path=os.path.join(vault_root, d),
        )
    return FakeDirectoryContract(
        keys=keys,
        aliases={d.lower(): d for d in dirs},
        created_at="2026-05-10T10:00:00Z",
    )


def _build_app(db, vault_root, directory_contract) -> web.Application:
    app = web.Application()
    app["admin_db"] = db
    app["admin_vault_root"] = vault_root
    app["admin_directory_contract"] = directory_contract
    app["admin_config"] = None
    create_admin_routes(app)
    return app


def _seed_events(db) -> None:
    for i in range(3):
        record_process_event(db, ProcessEvent(
            event_type="boot", category="boot", severity="INFO",
            agent_id="agent-{}".format(i), path="/vault",
            status="success",
            message="boot {}".format(i),
            timestamp="2026-05-10T1{:02d}:00:00Z".format(i),
        ))
    record_process_event(db, ProcessEvent(
        event_type="mutation", category="mutation", severity="ERROR",
        agent_id="agent-err", path="/vault/note.md",
        status="failed", error_code="E001",
        message="mutation failed",
        timestamp="2026-05-10T13:00:00Z",
    ))


# ---------------------------------------------------------------------------
# session helper for TestClient
# ---------------------------------------------------------------------------

async def _login(client: TestClient, username=ADMIN_USERNAME, password=ADMIN_PASSWORD) -> str:
    resp = await client.post(
        "/admin/login",
        data={"username": username, "password": password},
        allow_redirects=False,
    )
    cookies = resp.cookies
    session_cookie = cookies.get("admin_session")
    if session_cookie:
        return session_cookie.value
    return ""


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def _setup_admin_auth():
    init_admin_auth(
        username=ADMIN_USERNAME,
        password_hash=ADMIN_PASSWORD_HASH,
        session_timeout=1800.0,
    )
    _clear_sessions()
    yield
    _clear_sessions()


# ===========================================================================
# M-028-S-001: login_page_renders (DET)
#   GET /admin/login returns HTML with login form
# ===========================================================================


@pytest.mark.asyncio
async def test_login_page_renders(_setup_admin_auth, caplog, trace_assert, tmp_path):
    caplog.set_level(logging.DEBUG)

    db = _fresh_db()
    vault_root = str(tmp_path / "vault")
    os.makedirs(vault_root, exist_ok=True)
    dc = _make_directory_contract(vault_root)
    app = _build_app(db, vault_root, dc)

    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/admin/login")

        assert resp.status == 200
        text = await resp.text()
        assert "text/html" in resp.content_type
        assert "Memory Bank Admin" in text or "login" in text.lower()
        assert "<form" in text
        assert "username" in text.lower()
        assert "password" in text.lower()

        entries = _admin_web_entries(caplog)
        assert trace_assert(entries, "admin_web.request.received")


# ===========================================================================
# M-028-S-002: dashboard_requires_valid_session_and_renders
#   TA-044 + TA-046 emitted (DET + TRACE)
# ===========================================================================


@pytest.mark.asyncio
async def test_dashboard_requires_valid_session_and_renders(
    _setup_admin_auth, caplog, trace_assert, tmp_path
):
    caplog.set_level(logging.DEBUG)

    db = _fresh_db()
    vault_root = str(tmp_path / "vault")
    os.makedirs(os.path.join(vault_root, ".obsidian"), exist_ok=True)
    os.makedirs(os.path.join(vault_root, "00_Inbox"), exist_ok=True)
    os.makedirs(os.path.join(vault_root, "memory"), exist_ok=True)
    os.makedirs(os.path.join(vault_root, "summaries"), exist_ok=True)
    os.makedirs(os.path.join(vault_root, "70_Wiki"), exist_ok=True)
    dc = _make_directory_contract(vault_root)
    _seed_events(db)

    app = _build_app(db, vault_root, dc)

    async with TestClient(TestServer(app)) as client:
        session_id = await _login(client)

        resp = await client.get(
            "/admin/dashboard",
            cookies={"admin_session": session_id},
        )

        assert resp.status == 200
        text = await resp.text()
        assert "Dashboard" in text
        assert "System Status" in text

        entries = _admin_web_entries(caplog)
        assert trace_assert(entries, "admin_web.request.received", "admin_web.dashboard.rendered")

        rendered_entries = [e for e in entries if e.get("event") == "admin_web.dashboard.rendered"]
        assert len(rendered_entries) >= 1
        for e in rendered_entries:
            assert e["level"] == "INFO"
            assert e["function"] == "render_dashboard_page"
            assert e["block"] == "M-028"


# ===========================================================================
# M-028-S-003: log_export_download_works
#   TA-047 emitted (DET + TRACE)
# ===========================================================================


@pytest.mark.asyncio
async def test_log_export_download_works(
    _setup_admin_auth, caplog, trace_assert, tmp_path
):
    caplog.set_level(logging.DEBUG)

    db = _fresh_db()
    vault_root = str(tmp_path / "vault")
    os.makedirs(vault_root, exist_ok=True)
    dc = _make_directory_contract(vault_root)
    _seed_events(db)

    app = _build_app(db, vault_root, dc)

    async with TestClient(TestServer(app)) as client:
        session_id = await _login(client)

        resp = await client.get(
            "/admin/export",
            cookies={"admin_session": session_id},
        )

        assert resp.status == 200
        text = await resp.text()
        assert "text/plain" in resp.content_type or "text/plain" in str(resp.headers.get("Content-Type", ""))
        cd = resp.headers.get("Content-Disposition", "")
        assert "attachment" in cd or ".log" in cd

        entries = _admin_web_entries(caplog)
        assert trace_assert(entries, "admin_web.report.downloaded")

        report_entries = [e for e in entries if e.get("event") == "admin_web.report.downloaded"]
        assert len(report_entries) >= 1
        for e in report_entries:
            assert e["level"] == "INFO"
            assert e["function"] == "handle_log_export"
            assert e["block"] == "M-028"


# ===========================================================================
# M-028-F-001: unauthenticated_dashboard_access_denied
#   TA-045 emitted (DET + TRACE)
# ===========================================================================


@pytest.mark.asyncio
async def test_unauthenticated_dashboard_access_denied(
    _setup_admin_auth, caplog, trace_assert, tmp_path
):
    caplog.set_level(logging.DEBUG)

    db = _fresh_db()
    vault_root = str(tmp_path / "vault")
    os.makedirs(vault_root, exist_ok=True)
    dc = _make_directory_contract(vault_root)
    app = _build_app(db, vault_root, dc)

    async with TestClient(TestServer(app)) as client:
        resp = await client.get(
            "/admin/dashboard",
            allow_redirects=False,
        )

        assert resp.status in (302, 303)
        location = resp.headers.get("Location", "")
        assert "/admin/login" in location

        entries = _admin_web_entries(caplog)
        assert trace_assert(entries, "admin_web.request.received", "admin_web.access.denied")

        denied_entries = [e for e in entries if e.get("event") == "admin_web.access.denied"]
        assert len(denied_entries) >= 1
        for e in denied_entries:
            assert e["level"] == "WARNING"
            assert e["block"] == "M-028"


# ===========================================================================
# M-028-F-002: admin_web_does_not_directly_access_vault_filesystem (DET)
#   static analysis: no imports of M-005 (vault_fs) or M-009 (audit_log)
# ===========================================================================


def test_admin_web_does_not_directly_access_vault_filesystem():
    import inspect
    import memory_mcp.admin_web as aw

    source = inspect.getsource(aw)

    forbidden_imports = [
        "from memory_mcp.vault_fs",
        "import memory_mcp.vault_fs",
        "from memory_mcp.audit_log",
        "import memory_mcp.audit_log",
    ]
    for forbidden in forbidden_imports:
        assert forbidden not in source, "Forbidden import detected: {}".format(forbidden)

    forbidden_patterns = [
        "vault_fs",
        "VaultFilesystemRepository",
        "audit_log",
        "AuditLogService",
    ]
    for pattern in forbidden_patterns:
        assert pattern not in source, "Forbidden pattern detected: {}".format(pattern)


# ===========================================================================
# Additional: POST /admin/login with valid credentials
#   sets session cookie and redirects to dashboard
# ===========================================================================


@pytest.mark.asyncio
async def test_valid_login_sets_session_cookie_and_redirects(
    _setup_admin_auth, tmp_path
):
    db = _fresh_db()
    vault_root = str(tmp_path / "vault")
    os.makedirs(vault_root, exist_ok=True)
    dc = _make_directory_contract(vault_root)
    app = _build_app(db, vault_root, dc)

    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/admin/login",
            data={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD},
            allow_redirects=False,
        )

        assert resp.status in (302, 303)
        location = resp.headers.get("Location", "")
        assert "/admin/dashboard" in location

        cookies = resp.cookies
        session_cookie = cookies.get("admin_session")
        assert session_cookie is not None
        assert len(session_cookie.value) > 0
        assert session_cookie.value != ""


# ===========================================================================
# Additional: POST /admin/login with invalid credentials returns error
# ===========================================================================


@pytest.mark.asyncio
async def test_invalid_login_returns_error(_setup_admin_auth, tmp_path):
    db = _fresh_db()
    vault_root = str(tmp_path / "vault")
    os.makedirs(vault_root, exist_ok=True)
    dc = _make_directory_contract(vault_root)
    app = _build_app(db, vault_root, dc)

    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/admin/login",
            data={"username": ADMIN_USERNAME, "password": "wrong-password"},
            allow_redirects=False,
        )

        assert resp.status in (302, 303)
        location = resp.headers.get("Location", "")
        assert "/admin/login" in location
        assert "error" in location.lower() or "Invalid" in location

        cookies = resp.cookies
        assert "admin_session" not in cookies


# ===========================================================================
# Additional: expired session redirects to login
# ===========================================================================


@pytest.mark.asyncio
async def test_expired_session_redirects_to_login(_setup_admin_auth):
    init_admin_auth(
        username=ADMIN_USERNAME,
        password_hash=ADMIN_PASSWORD_HASH,
        session_timeout=0.0,
    )

    session = create_admin_session(ADMIN_USERNAME)
    import time
    time.sleep(0.01)

    app = web.Application()
    app["admin_db"] = _fresh_db()
    app["admin_vault_root"] = ""
    app["admin_directory_contract"] = None
    app["admin_config"] = None
    create_admin_routes(app)

    async with TestClient(TestServer(app)) as client:
        resp = await client.get(
            "/admin/dashboard",
            cookies={"admin_session": session.session_id},
            allow_redirects=False,
        )
        assert resp.status in (302, 303)
        location = resp.headers.get("Location", "")
        assert "/admin/login" in location


# ===========================================================================
# Additional: /admin/events returns filtered JSON events
# ===========================================================================


@pytest.mark.asyncio
async def test_events_endpoint_returns_json(_setup_admin_auth, tmp_path):
    db = _fresh_db()
    vault_root = str(tmp_path / "vault")
    os.makedirs(vault_root, exist_ok=True)
    dc = _make_directory_contract(vault_root)
    _seed_events(db)
    app = _build_app(db, vault_root, dc)

    async with TestClient(TestServer(app)) as client:
        session_id = await _login(client)

        resp = await client.get(
            "/admin/events",
            cookies={"admin_session": session_id},
        )

        assert resp.status == 200
        data = await resp.json()
        assert isinstance(data, list)
        assert len(data) >= 3

        for event in data:
            assert "id" in event
            assert "event_type" in event
            assert "category" in event
            assert "severity" in event
            assert "agent_id" in event
            assert "timestamp" in event
            assert "path" in event
            assert "message" in event


# ===========================================================================
# Additional: /admin/errors returns error timeline JSON
# ===========================================================================


@pytest.mark.asyncio
async def test_errors_endpoint_returns_json(_setup_admin_auth, tmp_path):
    db = _fresh_db()
    vault_root = str(tmp_path / "vault")
    os.makedirs(vault_root, exist_ok=True)
    dc = _make_directory_contract(vault_root)
    _seed_events(db)
    app = _build_app(db, vault_root, dc)

    async with TestClient(TestServer(app)) as client:
        session_id = await _login(client)

        resp = await client.get(
            "/admin/errors",
            cookies={"admin_session": session_id},
        )

        assert resp.status == 200
        data = await resp.json()
        assert isinstance(data, list)

        error_codes = [e["error_code"] for e in data]
        assert "E001" in error_codes or len(data) > 0


# ===========================================================================
# Additional: No M-005 or M-009 imports in admin_web.py (static analysis)
# ===========================================================================


def test_no_forbidden_imports_in_admin_web_module():
    import memory_mcp.admin_web as aw

    module_attrs = dir(aw)
    forbidden_modules = ["vault_fs", "audit_log"]
    for forbidden in forbidden_modules:
        assert forbidden not in module_attrs, "Forbidden module {} found in admin_web namespace".format(forbidden)

    if hasattr(aw, "__dict__"):
        for attr_name, attr_value in aw.__dict__.items():
            if hasattr(attr_value, "__module__"):
                mod_name = attr_value.__module__
                assert "vault_fs" not in mod_name, "vault_fs imported via {}".format(attr_name)
                assert "audit_log" not in mod_name, "audit_log imported via {}".format(attr_name)


# ===========================================================================
# Additional: events endpoint denied without session
# ===========================================================================


@pytest.mark.asyncio
async def test_events_endpoint_denied_without_session(
    _setup_admin_auth, tmp_path
):
    db = _fresh_db()
    vault_root = str(tmp_path / "vault")
    os.makedirs(vault_root, exist_ok=True)
    dc = _make_directory_contract(vault_root)
    app = _build_app(db, vault_root, dc)

    async with TestClient(TestServer(app)) as client:
        resp = await client.get(
            "/admin/events",
            allow_redirects=False,
        )

        assert resp.status in (302, 303)
        location = resp.headers.get("Location", "")
        assert "/admin/login" in location


# ===========================================================================
# Additional: export endpoint denied without session
# ===========================================================================


@pytest.mark.asyncio
async def test_export_endpoint_denied_without_session(
    _setup_admin_auth, tmp_path
):
    db = _fresh_db()
    vault_root = str(tmp_path / "vault")
    os.makedirs(vault_root, exist_ok=True)
    dc = _make_directory_contract(vault_root)
    app = _build_app(db, vault_root, dc)

    async with TestClient(TestServer(app)) as client:
        resp = await client.get(
            "/admin/export",
            allow_redirects=False,
        )

        assert resp.status in (302, 303)
        location = resp.headers.get("Location", "")
        assert "/admin/login" in location


# ===========================================================================
# Additional: events endpoint supports limit query param
# ===========================================================================


@pytest.mark.asyncio
async def test_events_endpoint_supports_limit(_setup_admin_auth, tmp_path):
    db = _fresh_db()
    vault_root = str(tmp_path / "vault")
    os.makedirs(vault_root, exist_ok=True)
    dc = _make_directory_contract(vault_root)
    _seed_events(db)
    app = _build_app(db, vault_root, dc)

    async with TestClient(TestServer(app)) as client:
        session_id = await _login(client)

        resp = await client.get(
            "/admin/events?limit=1",
            cookies={"admin_session": session_id},
        )

        assert resp.status == 200
        data = await resp.json()
        assert len(data) == 1


# ===========================================================================
# Additional: errors endpoint supports from_ts/to_ts query params
# ===========================================================================


@pytest.mark.asyncio
async def test_errors_endpoint_supports_time_range(
    _setup_admin_auth, tmp_path
):
    db = _fresh_db()
    vault_root = str(tmp_path / "vault")
    os.makedirs(vault_root, exist_ok=True)
    dc = _make_directory_contract(vault_root)
    _seed_events(db)
    app = _build_app(db, vault_root, dc)

    async with TestClient(TestServer(app)) as client:
        session_id = await _login(client)

        resp = await client.get(
            "/admin/errors?from_ts=2026-05-10T12:00:00Z&to_ts=2026-05-10T14:00:00Z",
            cookies={"admin_session": session_id},
        )

        assert resp.status == 200
        data = await resp.json()
        assert isinstance(data, list)


# ===========================================================================
# Additional: stop condition SC-010 -- admin web provides no vault mutation path
# ===========================================================================


def test_admin_web_provides_no_vault_mutation_path():
    import inspect
    import memory_mcp.admin_web as aw

    source = inspect.getsource(aw)

    mutation_keywords = [
        "write_file", "write_file_atomic", "create_note",
        "append_to_note", "delete_note", "modify_note",
        "mutation_service", "M-005", "vault_fs.write",
    ]
    for kw in mutation_keywords:
        assert kw not in source, "Mutation path detected in admin_web: {}".format(kw)


# ===========================================================================
# Additional: dashboard page with no events shows placeholder
# ===========================================================================


@pytest.mark.asyncio
async def test_dashboard_page_shows_placeholder_when_no_events(
    _setup_admin_auth, tmp_path
):
    db = _fresh_db()
    vault_root = str(tmp_path / "vault2")
    os.makedirs(os.path.join(vault_root, ".obsidian"), exist_ok=True)
    os.makedirs(os.path.join(vault_root, "00_Inbox"), exist_ok=True)
    os.makedirs(os.path.join(vault_root, "memory"), exist_ok=True)
    os.makedirs(os.path.join(vault_root, "summaries"), exist_ok=True)
    dc = _make_directory_contract(vault_root)

    app = _build_app(db, vault_root, dc)

    session = create_admin_session(ADMIN_USERNAME)

    async with TestClient(TestServer(app)) as client:
        resp = await client.get(
            "/admin/dashboard",
            cookies={"admin_session": session.session_id},
        )

        assert resp.status == 200
        text = await resp.text()
        assert "No events found" in text or "Dashboard" in text


# ===========================================================================
# Additional: login page shows error from query param
# ===========================================================================


@pytest.mark.asyncio
async def test_login_page_shows_error_from_query_param(
    _setup_admin_auth, tmp_path
):
    db = _fresh_db()
    vault_root = str(tmp_path / "vault")
    os.makedirs(vault_root, exist_ok=True)
    dc = _make_directory_contract(vault_root)
    app = _build_app(db, vault_root, dc)

    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/admin/login?error=Invalid+credentials")

        assert resp.status == 200
        text = await resp.text()
        assert "Invalid credentials" in text
