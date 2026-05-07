"""Module-local tests for M-011 ConfigService."""

import json
import logging

import pytest

from memory_mcp.config import (
    ConfigError,
    ServerConfig,
    VaultSettings,
    NetworkSettings,
    AuthSettings,
    PolicySettings,
    IndexSettings,
    VectorSettings,
    AuditSettings,
    BackupSettings,
    load_config,
    validate_config,
)


def _parse_logs(caplog) -> list[dict]:
    entries = []
    for record in caplog.records:
        try:
            entries.append(json.loads(record.message))
        except json.JSONDecodeError:
            pass
    return entries


# --- Scenario 1: Valid config returns typed settings ---

def test_load_valid_config_returns_typed_settings(sample_config_dict, caplog, trace_assert):
    caplog.set_level(logging.DEBUG)

    config = load_config(sample_config_dict)

    assert isinstance(config, ServerConfig)
    assert isinstance(config.vault, VaultSettings)
    assert config.vault.root == "/tmp/test-vault"
    assert isinstance(config.server, NetworkSettings)
    assert config.server.host == "127.0.0.1"
    assert config.server.port == 8080
    assert isinstance(config.auth, AuthSettings)
    assert config.auth.api_keys["test-key-admin-001"] == "admin"
    assert isinstance(config.policy, PolicySettings)
    assert "memory/" in config.policy.allowlist_roots
    assert "read" in config.policy.allowed_operations
    assert isinstance(config.index, IndexSettings)
    assert config.index.fts_enabled is True
    assert config.index.fts_language == "english"
    assert isinstance(config.vector, VectorSettings)
    assert config.vector.backend == "disabled"
    assert isinstance(config.audit, AuditSettings)
    assert config.audit.enabled is True
    assert isinstance(config.backup, BackupSettings)
    assert config.backup.enabled is True

    entries = _parse_logs(caplog)
    trace_assert(entries, "config.loaded")

    loaded_entry = next(e for e in entries if e.get("event") == "config.loaded")
    assert loaded_entry["level"] == "INFO"
    assert loaded_entry["function"] == "load_config"
    assert loaded_entry["block"] == "M-011"


# --- Scenario 2: Missing vault root fails closed ---

def test_missing_vault_root_fails_closed(sample_config_dict, caplog, trace_assert):
    caplog.set_level(logging.DEBUG)
    bad_config = dict(sample_config_dict)
    del bad_config["vault"]["root"]

    with pytest.raises(ConfigError) as exc_info:
        load_config(bad_config)

    assert exc_info.value.error_code == "CONFIG_MISSING_VAULT_ROOT"
    entries = _parse_logs(caplog)
    trace_assert(entries, "config.invalid")

    invalid_entry = next(e for e in entries if e.get("event") == "config.invalid")
    assert invalid_entry["level"] == "ERROR"
    assert invalid_entry["error_code"] == "CONFIG_MISSING_VAULT_ROOT"


# --- Scenario 3: Unsafe vault root rejected ---

@pytest.mark.parametrize("unsafe_root", ["/", "/etc"])
def test_unsafe_vault_root_rejected(sample_config_dict, caplog, trace_assert, unsafe_root):
    caplog.set_level(logging.DEBUG)
    bad_config = dict(sample_config_dict)
    bad_config["vault"]["root"] = unsafe_root

    with pytest.raises(ConfigError) as exc_info:
        validate_config(bad_config)

    assert exc_info.value.error_code == "CONFIG_UNSAFE_VAULT_ROOT"
    entries = _parse_logs(caplog)
    trace_assert(entries, "config.invalid")

    invalid_entry = next(e for e in entries if e.get("event") == "config.invalid")
    assert invalid_entry["error_code"] == "CONFIG_UNSAFE_VAULT_ROOT"


# --- Scenario 4: Secrets not present in sanitized log ---

def test_secrets_not_present_in_sanitized_log(sample_config_dict, caplog):
    caplog.set_level(logging.DEBUG)

    load_config(sample_config_dict)

    entries = _parse_logs(caplog)
    loaded_entry = next(e for e in entries if e.get("event") == "config.loaded")

    raw_log_text = json.dumps(loaded_entry)
    assert "test-key-readonly-001" in raw_log_text
    assert "test-key-trusted-001" in raw_log_text
    assert "test-key-wiki-001" in raw_log_text
    assert "test-key-cron-001" in raw_log_text
    assert "test-key-admin-001" in raw_log_text

    api_keys_section = loaded_entry["data"]["auth"]["api_keys"]
    for key_id, value in api_keys_section.items():
        assert value == "<redacted>", f"Key {key_id} value was not redacted, got: {value}"


# --- Scenario 5: Invalid vector backend fails ---

def test_config_with_invalid_backend_fails(sample_config_dict, caplog, trace_assert):
    caplog.set_level(logging.DEBUG)
    bad_config = dict(sample_config_dict)
    bad_config["vector"]["backend"] = "unsupported_backend"

    with pytest.raises(ConfigError) as exc_info:
        validate_config(bad_config)

    assert exc_info.value.error_code == "CONFIG_INVALID_VECTOR_BACKEND"
    entries = _parse_logs(caplog)
    trace_assert(entries, "config.invalid")

    invalid_entry = next(e for e in entries if e.get("event") == "config.invalid")
    assert invalid_entry["error_code"] == "CONFIG_INVALID_VECTOR_BACKEND"
