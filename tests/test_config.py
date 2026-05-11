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
    EmbeddingSettings,
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


# --- Scenario 6: Env-based API keys ---

_MEMORY_MCP_API_KEYS_ENV = "MEMORY_MCP_TEST_API_KEYS"


def test_api_keys_from_env_loads_keys(sample_config_dict, monkeypatch):
    env_keys = '{"env-key-admin": "admin", "env-key-readonly": "readonly_agent"}'
    monkeypatch.setenv(_MEMORY_MCP_API_KEYS_ENV, env_keys)

    cfg = dict(sample_config_dict)
    del cfg["auth"]["api_keys"]
    cfg["auth"]["api_keys_from_env"] = _MEMORY_MCP_API_KEYS_ENV

    config = load_config(cfg)
    assert "env-key-admin" in config.auth.api_keys
    assert config.auth.api_keys["env-key-admin"] == "admin"
    assert "env-key-readonly" in config.auth.api_keys


def test_api_keys_from_env_missing_var_raises(sample_config_dict, monkeypatch):
    monkeypatch.delenv(_MEMORY_MCP_API_KEYS_ENV, raising=False)

    cfg = dict(sample_config_dict)
    del cfg["auth"]["api_keys"]
    cfg["auth"]["api_keys_from_env"] = _MEMORY_MCP_API_KEYS_ENV

    with pytest.raises(ConfigError) as exc_info:
        load_config(cfg)
    assert exc_info.value.error_code == "CONFIG_MISSING_API_KEYS_ENV"


def test_api_keys_from_env_invalid_json_raises(sample_config_dict, monkeypatch):
    monkeypatch.setenv(_MEMORY_MCP_API_KEYS_ENV, "not-json")

    cfg = dict(sample_config_dict)
    del cfg["auth"]["api_keys"]
    cfg["auth"]["api_keys_from_env"] = _MEMORY_MCP_API_KEYS_ENV

    with pytest.raises(ConfigError) as exc_info:
        load_config(cfg)
    assert exc_info.value.error_code == "CONFIG_INVALID_API_KEYS_JSON"


def test_api_keys_from_env_not_dict_raises(sample_config_dict, monkeypatch):
    monkeypatch.setenv(_MEMORY_MCP_API_KEYS_ENV, "[1, 2, 3]")

    cfg = dict(sample_config_dict)
    del cfg["auth"]["api_keys"]
    cfg["auth"]["api_keys_from_env"] = _MEMORY_MCP_API_KEYS_ENV

    with pytest.raises(ConfigError) as exc_info:
        load_config(cfg)
    assert exc_info.value.error_code == "CONFIG_INVALID_API_KEYS_JSON"


def test_no_api_keys_and_no_env_raises(sample_config_dict):
    cfg = dict(sample_config_dict)
    del cfg["auth"]["api_keys"]

    with pytest.raises(ConfigError) as exc_info:
        load_config(cfg)
    assert exc_info.value.error_code == "CONFIG_MISSING_API_KEYS"


def test_env_api_keys_are_redacted_in_log(sample_config_dict, caplog, monkeypatch):
    caplog.set_level(logging.DEBUG)
    env_keys = '{"secret-key-1": "admin"}'
    monkeypatch.setenv(_MEMORY_MCP_API_KEYS_ENV, env_keys)

    cfg = dict(sample_config_dict)
    del cfg["auth"]["api_keys"]
    cfg["auth"]["api_keys_from_env"] = _MEMORY_MCP_API_KEYS_ENV

    load_config(cfg)
    entries = _parse_logs(caplog)
    loaded_entry = next(e for e in entries if e.get("event") == "config.loaded")

    auth_data = loaded_entry["data"]["auth"]
    assert "api_keys_from_env" in auth_data
    assert auth_data["api_keys_from_env"] == "<redacted>"
    assert "api_keys" not in auth_data or all(
        v == "<redacted>" for v in auth_data["api_keys"].values()
    )


def test_valid_embedding_settings_loaded(sample_config_dict, caplog, trace_assert):
    caplog.set_level(logging.DEBUG)
    config = load_config(sample_config_dict)

    assert config.embedding.api_base == "http://localhost:11434/v1"
    assert config.embedding.api_key_env == "TEST_EMBED_KEY"
    assert config.embedding.model == "text-embedding-3-small"
    assert config.embedding.dimensions == 1536
    assert config.embedding.batch_size == 100


def test_missing_embedding_api_base_raises(sample_config_dict):
    cfg = dict(sample_config_dict)
    del cfg["embedding"]["api_base"]

    with pytest.raises(ConfigError) as exc_info:
        load_config(cfg)
    assert exc_info.value.error_code == "CONFIG_MISSING_EMBEDDING_API_BASE"


def test_missing_embedding_api_key_env_raises(sample_config_dict):
    cfg = dict(sample_config_dict)
    del cfg["embedding"]["api_key_env"]

    with pytest.raises(ConfigError) as exc_info:
        load_config(cfg)
    assert exc_info.value.error_code == "CONFIG_MISSING_EMBEDDING_API_KEY_ENV"


def test_invalid_embedding_model_raises(sample_config_dict):
    cfg = dict(sample_config_dict)
    cfg["embedding"]["model"] = "invalid-model"

    with pytest.raises(ConfigError) as exc_info:
        load_config(cfg)
    assert exc_info.value.error_code == "CONFIG_INVALID_EMBEDDING_MODEL"


def test_invalid_embedding_dimensions_raises(sample_config_dict):
    cfg = dict(sample_config_dict)
    cfg["embedding"]["dimensions"] = 999

    with pytest.raises(ConfigError) as exc_info:
        load_config(cfg)
    assert exc_info.value.error_code == "CONFIG_INVALID_EMBEDDING_DIMENSIONS"


def test_embedding_api_key_env_is_redacted_in_log(sample_config_dict, caplog, trace_assert):
    caplog.set_level(logging.DEBUG)

    load_config(sample_config_dict)
    entries = _parse_logs(caplog)
    loaded_entry = next(e for e in entries if e.get("event") == "config.loaded")

    emb_data = loaded_entry["data"]["embedding"]
    assert emb_data["api_key_env"] == "<redacted>"
    assert emb_data["api_base"] == "http://localhost:11434/v1"
