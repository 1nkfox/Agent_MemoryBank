"""Module-local tests for M-024 AdminAuthService."""

import hashlib
import json
import logging
import time

import pytest

from memory_mcp.admin_auth import (
    AdminSession,
    AuthResult,
    _clear_sessions,
    authenticate_admin,
    create_admin_session,
    init_admin_auth,
    revoke_admin_session,
    validate_admin_session,
)
from memory_mcp.config import load_config


ADMIN_USERNAME = "dashboard_admin"
ADMIN_PASSWORD = "secure-password-123"
ADMIN_PASSWORD_HASH = hashlib.sha256(ADMIN_PASSWORD.encode("utf-8")).hexdigest()


def _parse_logs(caplog) -> list[dict]:
    entries = []
    for record in caplog.records:
        try:
            entries.append(json.loads(record.message))
        except json.JSONDecodeError:
            pass
    return entries


def _admin_entries(caplog) -> list[dict]:
    return [e for e in _parse_logs(caplog) if e.get("module") == "memory_mcp.admin_auth"]


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


# --- M-024-S-001: valid_admin_login_creates_session (TA-032) ---

def test_valid_admin_login_creates_session(_setup_admin_auth, caplog, trace_assert):
    caplog.set_level(logging.DEBUG)

    result = authenticate_admin(ADMIN_USERNAME, ADMIN_PASSWORD)

    assert result.success is True
    assert result.reason is None

    session = create_admin_session(ADMIN_USERNAME)

    assert isinstance(session, AdminSession)
    assert session.username == ADMIN_USERNAME
    assert session.created_at > 0
    assert session.expires_at > session.created_at
    assert session.expires_at - session.created_at == pytest.approx(1800.0, abs=5)

    entries = _admin_entries(caplog)
    trace_assert(entries, "admin_auth.login.success")

    success_entry = next(e for e in entries if e.get("event") == "admin_auth.login.success")
    assert success_entry["level"] == "INFO"
    assert success_entry["function"] == "authenticate_admin"
    assert success_entry["block"] == "M-024"


# --- M-024-S-002: valid_session_accesses_dashboard (TA-034) ---

def test_valid_session_accesses_dashboard(_setup_admin_auth, caplog, trace_assert):
    caplog.set_level(logging.DEBUG)

    session = create_admin_session(ADMIN_USERNAME)

    assert validate_admin_session(session.session_id) is True
    assert validate_admin_session(session.session_id) is True

    entries = _admin_entries(caplog)
    trace_assert(entries, "admin_auth.session.validated")

    validated_entries = [e for e in entries if e.get("event") == "admin_auth.session.validated"]
    assert len(validated_entries) >= 2
    for entry in validated_entries:
        assert entry["level"] == "INFO"
        assert entry["function"] == "validate_admin_session"
        assert entry["block"] == "M-024"


# --- M-024-F-001: wrong_password_denied_and_not_logged (TA-033) ---

def test_wrong_password_denied_and_not_logged(_setup_admin_auth, caplog, trace_assert):
    caplog.set_level(logging.DEBUG)

    wrong_password = "wrong-password-value"
    result = authenticate_admin(ADMIN_USERNAME, wrong_password)

    assert result.success is False
    assert result.reason == "Invalid credentials"

    entries = _admin_entries(caplog)
    trace_assert(entries, "admin_auth.login.failed")

    failed_entry = next(e for e in entries if e.get("event") == "admin_auth.login.failed")
    assert failed_entry["level"] == "WARNING"
    assert failed_entry["error_code"] == "AUTH_INVALID_KEY"
    assert failed_entry["function"] == "authenticate_admin"
    assert failed_entry["block"] == "M-024"

    raw_log_text = json.dumps(entries)
    assert wrong_password not in raw_log_text
    assert "password" not in raw_log_text.lower() or "<redacted>" in raw_log_text.lower()

    data_str = json.dumps(failed_entry.get("data", {}))
    assert wrong_password not in data_str
    assert ADMIN_PASSWORD not in data_str
    assert "secure-password" not in data_str


# --- M-024-F-002: mcp_api_key_cannot_authenticate_admin_session ---

def test_mcp_api_key_cannot_authenticate_admin_session(
    _setup_admin_auth, sample_config_dict, caplog
):
    caplog.set_level(logging.DEBUG)
    config = load_config(sample_config_dict)

    mcp_api_key = "test-key-admin-001"
    assert "admin" == config.auth.api_keys.get(mcp_api_key)

    result = authenticate_admin(mcp_api_key, mcp_api_key)
    assert result.success is False

    result2 = authenticate_admin(mcp_api_key, ADMIN_PASSWORD)
    assert result2.success is False

    entries = _admin_entries(caplog)
    for entry in entries:
        data_str = json.dumps(entry.get("data", {}))
        assert mcp_api_key not in data_str


# --- Scenario 5: Session expiration after timeout ---

def test_session_expiration_after_timeout(_setup_admin_auth, caplog):
    caplog.set_level(logging.DEBUG)

    init_admin_auth(
        username=ADMIN_USERNAME,
        password_hash=ADMIN_PASSWORD_HASH,
        session_timeout=0.1,
    )

    session = create_admin_session(ADMIN_USERNAME)

    assert validate_admin_session(session.session_id) is True

    time.sleep(0.15)

    assert validate_admin_session(session.session_id) is False


# --- Scenario 6: Session revocation prevents subsequent validation ---

def test_session_revocation_prevents_subsequent_validation(_setup_admin_auth, caplog, trace_assert):
    caplog.set_level(logging.DEBUG)

    session = create_admin_session(ADMIN_USERNAME)
    assert validate_admin_session(session.session_id) is True

    revoke_admin_session(session.session_id)

    assert validate_admin_session(session.session_id) is False

    entries = _admin_entries(caplog)
    trace_assert(entries, "admin_auth.session.validated", "admin_auth.session.revoked")

    revoked_entry = next(e for e in entries if e.get("event") == "admin_auth.session.revoked")
    assert revoked_entry["level"] == "INFO"
    assert revoked_entry["function"] == "revoke_admin_session"
    assert revoked_entry["block"] == "M-024"


# --- Scenario 7: Unknown session_id rejected ---

def test_unknown_session_id_rejected(_setup_admin_auth, caplog):
    caplog.set_level(logging.DEBUG)

    assert validate_admin_session("nonexistent-session-uuid") is False

    entries = _admin_entries(caplog)
    validated = [e for e in entries if e.get("event") == "admin_auth.session.validated"]
    assert len(validated) == 0


# --- Scenario 8: Multiple sessions for same user are valid ---

def test_multiple_sessions_for_same_user_are_valid(_setup_admin_auth, caplog):
    caplog.set_level(logging.DEBUG)

    session1 = create_admin_session(ADMIN_USERNAME)
    session2 = create_admin_session(ADMIN_USERNAME)
    session3 = create_admin_session(ADMIN_USERNAME)

    assert session1.session_id != session2.session_id
    assert session2.session_id != session3.session_id
    assert session1.session_id != session3.session_id

    assert validate_admin_session(session1.session_id) is True
    assert validate_admin_session(session2.session_id) is True
    assert validate_admin_session(session3.session_id) is True

    entries = _admin_entries(caplog)
    validated = [e for e in entries if e.get("event") == "admin_auth.session.validated"]
    assert len(validated) == 3
