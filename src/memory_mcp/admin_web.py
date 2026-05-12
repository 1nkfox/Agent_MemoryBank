"""Admin web interface -- M-028 AdminWebInterface."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from aiohttp import web

from memory_mcp.admin_auth import (
    AdminSession,
    AuthResult,
    authenticate_admin,
    create_admin_session,
    validate_admin_session,
)
from memory_mcp.admin_dashboard_service import (
    DashboardSummary,
    Event,
    get_dashboard_summary,
    get_error_timeline,
    get_recent_events,
    request_log_export,
)
from memory_mcp.log_report_service import LogReportRequest
from memory_mcp.observability import (
    log_trace_anchor,
    new_trace_id,
)

logger = logging.getLogger(__name__)

MODULE = "memory_mcp.admin_web"
MODULE_BLOCK = "M-028"

SESSION_COOKIE = "admin_session"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _trace_id_from_request(_request: web.Request) -> str:
    return new_trace_id()


def _log_request_received(request: web.Request, trace_id: str) -> None:
    log_trace_anchor(
        level="INFO",
        event="admin_web.request.received",
        trace_id=trace_id,
        module=MODULE,
        function=str(request.rel_url),
        block=MODULE_BLOCK,
        data={"method": request.method, "path": str(request.rel_url)},
    )


def _log_access_denied(trace_id: str, reason: str) -> None:
    log_trace_anchor(
        level="WARNING",
        event="admin_web.access.denied",
        trace_id=trace_id,
        module=MODULE,
        function="_check_session",
        block=MODULE_BLOCK,
        data={"reason": reason},
    )


def _get_session_id(request: web.Request) -> str:
    return request.cookies.get(SESSION_COOKIE, "")


def _check_session(request: web.Request, trace_id: str) -> tuple[bool, str]:
    session_id = _get_session_id(request)
    if not session_id:
        _log_access_denied(trace_id, "no_session_cookie")
        return False, ""
    if not validate_admin_session(session_id):
        _log_access_denied(trace_id, "invalid_or_expired_session")
        return False, ""
    return True, session_id


def _require_session(request: web.Request, trace_id: str) -> str:
    ok, session_id = _check_session(request, trace_id)
    if not ok:
        raise web.HTTPFound("/admin/login")
    return session_id


# ---------------------------------------------------------------------------
# HTML templates (inline, no external CSS/JS for MVP)
# ---------------------------------------------------------------------------

_LOGIN_HTML = (
    '<!DOCTYPE html>'
    '<html lang="en">'
    '<head>'
    '<meta charset="utf-8">'
    '<meta name="viewport" content="width=device-width, initial-scale=1">'
    '<title>Admin Login -- Memory Bank</title>'
    '<style>'
    'body{{font-family:system-ui,sans-serif;max-width:480px;margin:80px auto;padding:0 16px}}'
    'h1{{font-size:1.5rem}}'
    'label{{display:block;margin:12px 0 4px;font-weight:600}}'
    'input{{width:100%;padding:8px;box-sizing:border-box;font-size:1rem}}'
    'button{{margin-top:16px;padding:10px 24px;font-size:1rem;cursor:pointer}}'
    '.error{{color:#c00;margin-top:12px;font-weight:600}}'
    '</style>'
    '</head>'
    '<body>'
    '<h1>Memory Bank Admin</h1>'
    '<form method="post" action="/admin/login">'
    '<label for="username">Username</label>'
    '<input type="text" id="username" name="username" required autocomplete="username">'
    '<label for="password">Password</label>'
    '<input type="password" id="password" name="password" required autocomplete="current-password">'
    '<button type="submit">Sign in</button>'
    '</form>'
    '{error_html}'
    '</body>'
    '</html>'
)

_LOGIN_ERROR_HTML = '<div class="error">{message}</div>'


_DASHBOARD_HTML = (
    '<!DOCTYPE html>'
    '<html lang="en">'
    '<head>'
    '<meta charset="utf-8">'
    '<meta name="viewport" content="width=device-width, initial-scale=1">'
    '<title>Admin Dashboard -- Memory Bank</title>'
    '<style>'
    'body{{font-family:system-ui,sans-serif;max-width:1200px;margin:0 auto;padding:16px}}'
    'h1{{font-size:1.5rem}}'
    'h2{{font-size:1.2rem;margin-top:24px}}'
    'table{{width:100%;border-collapse:collapse;margin:12px 0;font-size:0.9rem}}'
    'th,td{{padding:8px;text-align:left;border-bottom:1px solid #ddd}}'
    'th{{background:#f5f5f5}}'
    '.ok{{color:#2a7d2a;font-weight:600}}'
    '.warn{{color:#b85c00;font-weight:600}}'
    '.err{{color:#c00;font-weight:600}}'
    '.sev-info{{color:#333}}'
    '.sev-warn{{color:#b85c00}}'
    '.sev-error{{color:#c00;font-weight:600}}'
    'form{{margin:16px 0;display:flex;gap:8px;flex-wrap:wrap;align-items:end}}'
    'form label{{font-size:0.85rem;font-weight:600;display:block}}'
    'form input,form select{{padding:6px;font-size:0.9rem}}'
    '.filter-row{{display:flex;flex-direction:column;gap:2px}}'
    '.actions{{margin:16px 0}}'
    '.actions a,.actions button{{padding:8px 16px;text-decoration:none;font-size:0.95rem}}'
    '.badge{{display:inline-block;padding:2px 8px;border-radius:4px;font-size:0.8rem;font-weight:600}}'
    '.badge-degraded{{background:#ffe0b0;color:#b85c00}}'
    '.badge-ok{{background:#d4edda;color:#2a7d2a}}'
    '</style>'
    '</head>'
    '<body>'
    '<h1>Memory Bank Dashboard</h1>'
    '<div>'
    '<span class="badge {degraded_class}">{degraded_text}</span>'
    '<span>Generated: {generated_at}</span>'
    '</div>'
    '<h2>System Status</h2>'
    '<table>'
    '<thead><tr><th>Component</th><th>Status</th></tr></thead>'
    '<tbody>'
    '{status_rows}'
    '</tbody>'
    '</table>'
    '<h2>Recent Events</h2>'
    '<table>'
    '<thead><tr><th>Timestamp</th><th>Severity</th><th>Category</th>'
    '<th>Type</th><th>Agent</th><th>Path</th><th>Message</th></tr></thead>'
    '<tbody>'
    '{event_rows}'
    '</tbody>'
    '</table>'
    '<h2>Filters</h2>'
    '<form method="get" action="/admin/events">'
    '<div class="filter-row"><label>Severity</label>'
    '<select name="severity"><option value="">All</option>'
    '<option value="INFO">INFO</option><option value="WARN">WARN</option>'
    '<option value="ERROR">ERROR</option></select></div>'
    '<div class="filter-row"><label>Category</label>'
    '<input type="text" name="category" placeholder="e.g. mutation"></div>'
    '<div class="filter-row"><label>Agent ID</label>'
    '<input type="text" name="agent_id" placeholder="e.g. readonly_agent"></div>'
    '<div class="filter-row"><label>From</label>'
    '<input type="text" name="from_ts" placeholder="ISO timestamp"></div>'
    '<div class="filter-row"><label>To</label>'
    '<input type="text" name="to_ts" placeholder="ISO timestamp"></div>'
    '<div class="filter-row"><label>&nbsp;</label>'
    '<button type="submit">Apply Filters</button></div>'
    '</form>'
    '<div class="actions">'
    '<a href="/admin/export">Download .log Report</a>'
    '</div>'
    '</body>'
    '</html>'
)


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------


async def render_login_page(request: web.Request) -> web.Response:
    """GET /admin/login -- render login form."""
    trace_id = _trace_id_from_request(request)
    _log_request_received(request, trace_id)

    error = request.query.get("error", "")
    error_html = ""
    if error:
        error_html = _LOGIN_ERROR_HTML.format(message=error)

    html = _LOGIN_HTML.format(error_html=error_html)
    return web.Response(text=html, content_type="text/html")


async def _handle_login(request: web.Request) -> web.Response:
    """POST /admin/login -- authenticate and set session cookie."""
    trace_id = _trace_id_from_request(request)
    _log_request_received(request, trace_id)

    if request.content_type and "json" in request.content_type:
        data = await request.json()
        username = str(data.get("username", "")).strip()
        password = str(data.get("password", "")).strip()
    else:
        post_data = await request.post()
        username = str(post_data.get("username", "")).strip()
        password = str(post_data.get("password", "")).strip()

    result: AuthResult = authenticate_admin(username, password)

    if not result.success:
        return web.HTTPFound("/admin/login?error=Invalid+credentials")

    session: AdminSession = create_admin_session(username)

    response = web.HTTPFound("/admin/dashboard")
    max_age = max(1, int(session.expires_at - session.created_at))
    response.set_cookie(
        SESSION_COOKIE,
        session.session_id,
        httponly=True,
        secure=False,
        samesite="Strict",
        max_age=max_age,
    )
    return response


def _render_status_row(label: str, value: str) -> str:
    cls = "ok" if value == "healthy" else "warn"
    return '<tr><td>{label}</td><td class="{cls}">{value}</td></tr>'.format(
        label=label, cls=cls, value=value
    )


def _render_event_row(e: Event) -> str:
    return (
        "<tr>"
        "<td>{ts}</td>"
        '<td><span class="sev-{sevlow}">{sev}</span></td>'
        "<td>{cat}</td>"
        "<td>{etype}</td>"
        "<td>{agent}</td>"
        "<td>{path}</td>"
        "<td>{msg}</td>"
        "</tr>"
    ).format(
        ts=e.timestamp,
        sev=e.severity,
        sevlow=e.severity.lower(),
        cat=e.category,
        etype=e.event_type,
        agent=e.agent_id,
        path=e.path,
        msg=e.message,
    )


async def render_dashboard_page(request: web.Request) -> web.Response:
    """GET /admin/dashboard -- session-gated dashboard HTML."""
    trace_id = _trace_id_from_request(request)
    _log_request_received(request, trace_id)

    _require_session(request, trace_id)

    db = request.app.get("admin_db", "")
    vault_root = request.app.get("admin_vault_root", "")
    directory_contract = request.app.get("admin_directory_contract", None)
    config = request.app.get("admin_config", None)

    audit_db = request.app.get("admin_audit_db", "")
    try:
        summary: DashboardSummary = get_dashboard_summary(
            db, vault_root, directory_contract, trace_id, config
        )
        recent_events: list[Event] = get_recent_events(
            db, limit=50, trace_id=trace_id, audit_db=audit_db,
        )
    except Exception:
        summary = DashboardSummary(
            server_status="error",
            vault_status="error",
            index_status="error",
            backup_status="error",
            layout_status="error",
            total_events=0,
            recent_errors=0,
            generated_at=datetime.now(timezone.utc).isoformat(),
            degraded=True,
        )
        recent_events = []

    status_rows = "".join([
        _render_status_row("Server", summary.server_status),
        _render_status_row("Vault", summary.vault_status),
        _render_status_row("Index", summary.index_status),
        _render_status_row("Backup", summary.backup_status),
        _render_status_row("Layout", summary.layout_status),
        _render_status_row("Total Events", str(summary.total_events)),
        _render_status_row("Recent Errors", str(summary.recent_errors)),
    ])

    event_rows = "".join(_render_event_row(e) for e in recent_events)
    if not event_rows:
        event_rows = '<tr><td colspan="7">No events found.</td></tr>'

    degraded_class = "badge-degraded" if summary.degraded else "badge-ok"
    degraded_text = "DEGRADED" if summary.degraded else "HEALTHY"

    html = _DASHBOARD_HTML.format(
        degraded_class=degraded_class,
        degraded_text=degraded_text,
        generated_at=summary.generated_at,
        status_rows=status_rows,
        event_rows=event_rows,
    )

    log_trace_anchor(
        level="INFO",
        event="admin_web.dashboard.rendered",
        trace_id=trace_id,
        module=MODULE,
        function="render_dashboard_page",
        block=MODULE_BLOCK,
        data={
            "total_events": summary.total_events,
            "degraded": summary.degraded,
        },
    )

    return web.Response(text=html, content_type="text/html")


async def _handle_events(request: web.Request) -> web.Response:
    """GET /admin/events -- session-gated JSON event list."""
    trace_id = _trace_id_from_request(request)
    _log_request_received(request, trace_id)

    _require_session(request, trace_id)

    db = request.app.get("admin_db", "")
    audit_db = request.app.get("admin_audit_db", "")
    limit = request.query.get("limit", "50")
    try:
        limit_int = int(limit)
    except (ValueError, TypeError):
        limit_int = 50

    events = get_recent_events(db, limit=limit_int, trace_id=trace_id, audit_db=audit_db)

    result = [
        {
            "id": e.id,
            "event_type": e.event_type,
            "category": e.category,
            "severity": e.severity,
            "agent_id": e.agent_id,
            "timestamp": e.timestamp,
            "path": e.path,
            "message": e.message,
        }
        for e in events
    ]

    return web.json_response(result)


async def _handle_errors(request: web.Request) -> web.Response:
    """GET /admin/errors -- session-gated JSON error timeline."""
    trace_id = _trace_id_from_request(request)
    _log_request_received(request, trace_id)

    _require_session(request, trace_id)

    db = request.app.get("admin_db", "")
    from_ts = request.query.get("from_ts", "")
    to_ts = request.query.get("to_ts", "")

    timeline = get_error_timeline(
        db,
        from_ts=from_ts,
        to_ts=to_ts,
        trace_id=trace_id,
    )

    result = [
        {
            "timestamp": e.timestamp,
            "error_code": e.error_code,
            "event_type": e.event_type,
            "count": e.count,
        }
        for e in timeline
    ]

    return web.json_response(result)


async def handle_log_export(request: web.Request) -> web.Response:
    """GET /admin/export -- session-gated .log report download."""
    trace_id = _trace_id_from_request(request)
    _log_request_received(request, trace_id)

    _require_session(request, trace_id)

    db = request.app.get("admin_db", "")

    def _query_int(name: str, default: int) -> int:
        try:
            return int(request.query.get(name, str(default)))
        except (ValueError, TypeError):
            return default

    lr = LogReportRequest(
        from_timestamp=request.query.get("from_ts", "") or "",
        to_timestamp=request.query.get("to_ts", "") or "",
        severity=request.query.get("severity", "") or "",
        category=request.query.get("category", "") or "",
        event_type=request.query.get("event_type", "") or "",
        agent_id=request.query.get("agent_id", "") or "",
        admin_user="",
        path_prefix=request.query.get("path_prefix", "") or "",
        trace_id=request.query.get("trace_id", "") or "",
        status=request.query.get("status", "") or "",
        error_code=request.query.get("error_code", "") or "",
        max_lines=_query_int("max_lines", 10000),
    )

    result = request_log_export(db, lr, trace_id)

    log_trace_anchor(
        level="INFO",
        event="admin_web.report.downloaded",
        trace_id=trace_id,
        module=MODULE,
        function="handle_log_export",
        block=MODULE_BLOCK,
        data={
            "total_events": getattr(result, "total_events", 0),
            "filtered_events": getattr(result, "filtered_events", 0),
        },
    )

    content = "\n".join(getattr(result, "lines", []))
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    filename = "memory-bank-report-{}.log".format(ts)

    return web.Response(
        text=content,
        content_type="text/plain",
        charset="utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="{}"'.format(filename),
        },
    )


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------


def create_admin_routes(
    app: web.Application,
    admin_auth_service: Any = None,
    dashboard_service: Any = None,
    log_report_service: Any = None,
    config: Any = None,
) -> list[web.AbstractRoute]:
    app.router.add_get("/admin/login", render_login_page)
    app.router.add_post("/admin/login", _handle_login)
    app.router.add_get("/admin/dashboard", render_dashboard_page)
    app.router.add_get("/admin/events", _handle_events)
    app.router.add_get("/admin/errors", _handle_errors)
    app.router.add_get("/admin/export", handle_log_export)

    if config is not None:
        app["admin_config"] = config
        app["admin_db"] = getattr(config, "process_event_db_path", "") or ""
        app["admin_audit_db"] = getattr(config, "audit", None) and getattr(config.audit, "audit_db_path", "") or ""
        if hasattr(config, "vault"):
            app["admin_vault_root"] = config.vault.root
        else:
            vault = getattr(config, "vault_root", "")
            app["admin_vault_root"] = vault
        dc = getattr(config, "directory_contract", None)
        app["admin_directory_contract"] = dc

    return list(app.router.routes())
