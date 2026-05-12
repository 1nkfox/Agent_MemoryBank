"""Admin dashboard session authentication — M-024 AdminAuthService."""

from __future__ import annotations

import hashlib
import secrets
import time
import uuid
from dataclasses import dataclass
from typing import Optional

from memory_mcp.observability import (
    ErrorCode,
    log_trace_anchor,
    new_trace_id,
)

SESSION_TIMEOUT_DEFAULT = 1800.0

_admin_username: str = ""
_admin_password_hash: str = ""
_session_timeout: float = SESSION_TIMEOUT_DEFAULT
_sessions: dict[str, AdminSession] = {}


class AdminAuthError(Exception):
    def __init__(self, message: str, error_code: str):
        self.error_code = error_code
        super().__init__(message)


@dataclass
class AuthResult:
    success: bool
    reason: Optional[str] = None


@dataclass
class AdminSession:
    session_id: str
    username: str
    created_at: float
    expires_at: float


def _fingerprint(value: str) -> str:
    if len(value) > 8:
        return f"{value[:4]}...{value[-4:]}"
    return value[:4] + "...****"


def _hash_password(password: str) -> str:
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def init_admin_auth(
    username: str,
    password_hash: str,
    session_timeout: float = SESSION_TIMEOUT_DEFAULT,
) -> None:
    global _admin_username, _admin_password_hash, _session_timeout
    _admin_username = username
    _admin_password_hash = password_hash
    _session_timeout = session_timeout


def _clear_sessions() -> None:
    """Reset session store (intended for tests)."""
    global _sessions
    _sessions = {}


def authenticate_admin(username: str, password: str) -> AuthResult:
    trace_id = new_trace_id()
    user_fingerprint = _fingerprint(username)

    if _admin_username == "" or _admin_password_hash == "":
        log_trace_anchor(
            "ERROR", "admin_auth.login.failed",
            trace_id=trace_id, module="memory_mcp.admin_auth",
            function="authenticate_admin", block="M-024",
            data={"reason": "admin auth not initialized", "username_fp": user_fingerprint},
            error_code=ErrorCode.CONFIG_INVALID,
        )
        return AuthResult(success=False, reason="Admin authentication not configured")

    if username != _admin_username:
        log_trace_anchor(
            "WARNING", "admin_auth.login.failed",
            trace_id=trace_id, module="memory_mcp.admin_auth",
            function="authenticate_admin", block="M-024",
            data={"reason": "invalid credentials", "username_fp": user_fingerprint},
            error_code=ErrorCode.AUTH_INVALID_KEY,
        )
        return AuthResult(success=False, reason="Invalid credentials")

    input_hash = _hash_password(password)

    if not secrets.compare_digest(input_hash, _admin_password_hash):
        log_trace_anchor(
            "WARNING", "admin_auth.login.failed",
            trace_id=trace_id, module="memory_mcp.admin_auth",
            function="authenticate_admin", block="M-024",
            data={"reason": "invalid credentials", "username_fp": user_fingerprint},
            error_code=ErrorCode.AUTH_INVALID_KEY,
        )
        return AuthResult(success=False, reason="Invalid credentials")

    log_trace_anchor(
        "INFO", "admin_auth.login.success",
        trace_id=trace_id, module="memory_mcp.admin_auth",
        function="authenticate_admin", block="M-024",
        data={"username_fp": user_fingerprint},
    )

    return AuthResult(success=True)


def create_admin_session(username: str) -> AdminSession:
    now = time.time()
    session = AdminSession(
        session_id=str(uuid.uuid4()),
        username=username,
        created_at=now,
        expires_at=now + _session_timeout,
    )
    _sessions[session.session_id] = session
    return session


def validate_admin_session(session_id: str) -> bool:
    trace_id = new_trace_id()

    session = _sessions.get(session_id)
    if session is None:
        return False

    now = time.time()
    if now >= session.expires_at:
        del _sessions[session_id]
        return False

    log_trace_anchor(
        "INFO", "admin_auth.session.validated",
        trace_id=trace_id, module="memory_mcp.admin_auth",
        function="validate_admin_session", block="M-024",
        data={"session_id_fp": _fingerprint(session_id)},
    )

    return True


def revoke_admin_session(session_id: str) -> None:
    trace_id = new_trace_id()

    _sessions.pop(session_id, None)

    log_trace_anchor(
        "INFO", "admin_auth.session.revoked",
        trace_id=trace_id, module="memory_mcp.admin_auth",
        function="revoke_admin_session", block="M-024",
        data={"session_id_fp": _fingerprint(session_id)},
    )
