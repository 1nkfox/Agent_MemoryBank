"""Module-local tests for M-002 AuthIdentityService."""

import json
import logging

import pytest

from memory_mcp.auth import AgentIdentity, AuthError, resolve_identity
from memory_mcp.config import load_config


def _parse_logs(caplog) -> list[dict]:
    entries = []
    for record in caplog.records:
        try:
            entries.append(json.loads(record.message))
        except json.JSONDecodeError:
            pass
    return entries


# --- Scenario 1: Valid API key resolves to AgentIdentity ---

def test_valid_api_key_resolves_to_agent_identity(sample_config_dict, caplog, trace_assert):
    caplog.set_level(logging.DEBUG)
    config = load_config(sample_config_dict)

    identity, profile = resolve_identity("test-key-readonly-001", config)

    assert isinstance(identity, AgentIdentity)
    assert identity.key_id == "test...-001"
    assert identity.profile == "readonly_agent"
    assert profile == "readonly_agent"

    entries = _parse_logs(caplog)
    trace_assert(entries, "auth.identity.resolved")

    resolved_entry = next(e for e in entries if e.get("event") == "auth.identity.resolved")
    assert resolved_entry["level"] == "INFO"
    assert resolved_entry["function"] == "resolve_identity"
    assert resolved_entry["block"] == "M-002"
    assert resolved_entry["data"]["key_id"] == "test...-001"
    assert resolved_entry["data"]["profile"] == "readonly_agent"


# --- Scenario 2: Invalid API key returns error and emits rejected ---

def test_invalid_api_key_returns_error_and_emits_rejected(sample_config_dict, caplog, trace_assert):
    caplog.set_level(logging.DEBUG)
    config = load_config(sample_config_dict)

    with pytest.raises(AuthError) as exc_info:
        resolve_identity("bad-key", config)

    assert exc_info.value.error_code == "AUTH_UNKNOWN_KEY"

    entries = _parse_logs(caplog)
    trace_assert(entries, "auth.identity.rejected")

    rejected_entry = next(e for e in entries if e.get("event") == "auth.identity.rejected")
    assert rejected_entry["level"] == "WARNING"
    assert rejected_entry["error_code"] == "AUTH_UNKNOWN_KEY"
    assert rejected_entry["function"] == "resolve_identity"
    assert rejected_entry["block"] == "M-002"


# --- Scenario 3: API key value never logged ---

def _auth_entries(caplog) -> list[dict]:
    return [e for e in _parse_logs(caplog) if e.get("module") == "memory_mcp.auth"]


def test_api_key_value_never_logged(sample_config_dict, caplog):
    caplog.set_level(logging.DEBUG)
    config = load_config(sample_config_dict)

    resolve_identity("test-key-readonly-001", config)

    entries = _auth_entries(caplog)
    raw_log_text = json.dumps(entries)

    assert "test-key-readonly-001" not in raw_log_text


def test_api_key_value_never_logged_on_failure(sample_config_dict, caplog):
    caplog.set_level(logging.DEBUG)
    config = load_config(sample_config_dict)

    try:
        resolve_identity("bad-key", config)
    except AuthError:
        pass

    entries = _auth_entries(caplog)
    raw_log_text = json.dumps(entries)

    assert "bad-key" not in raw_log_text


# --- Scenario 4: All five profiles map from config ---

def test_all_five_profiles_map_from_config(sample_config_dict, caplog):
    caplog.set_level(logging.DEBUG)
    config = load_config(sample_config_dict)

    expected = {
        "test-key-readonly-001": "readonly_agent",
        "test-key-trusted-001": "trusted_writer",
        "test-key-wiki-001": "wiki_maintainer",
        "test-key-cron-001": "cron_summarizer",
        "test-key-admin-001": "admin",
    }

    for api_key, expected_profile in expected.items():
        identity, profile = resolve_identity(api_key, config)
        assert identity.profile == expected_profile, f"Expected {expected_profile}, got {identity.profile} for {api_key}"
        assert profile == expected_profile


# --- Scenario 5: Expired or revoked key rejected ---

def test_expired_or_revoked_key_rejected(sample_config_dict, caplog, trace_assert):
    caplog.set_level(logging.DEBUG)

    revoked_config_dict = dict(sample_config_dict)
    revoked_config_dict["auth"]["api_keys"]["test-key-revoked-001"] = "revoked"
    config = load_config(revoked_config_dict)

    with pytest.raises(AuthError) as exc_info:
        resolve_identity("test-key-revoked-001", config)

    assert exc_info.value.error_code == "AUTH_INVALID_KEY"

    entries = _parse_logs(caplog)
    trace_assert(entries, "auth.identity.rejected")

    rejected_entry = next(e for e in entries if e.get("event") == "auth.identity.rejected")
    assert rejected_entry["level"] == "WARNING"
    assert rejected_entry["error_code"] == "AUTH_INVALID_KEY"
    assert rejected_entry["function"] == "resolve_identity"
    assert rejected_entry["block"] == "M-002"
