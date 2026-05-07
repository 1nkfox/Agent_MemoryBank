"""Configuration loader for the memory MCP server — M-011 ConfigService."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

UNSAFE_VAULT_ROOTS = {
    "/", "/etc", "/bin", "/sbin", "/usr", "/var", "/boot",
    "/dev", "/proc", "/sys", "/root", "/opt", "/lib", "/lib64",
}

VALID_VECTOR_BACKENDS = {"disabled", "sqlite_vec", "qdrant_optional"}

REQUIRED_SECTIONS = [
    "vault", "server", "auth", "policy", "index", "vector", "audit", "backup",
]


class ConfigError(Exception):
    def __init__(self, message: str, error_code: str):
        self.error_code = error_code
        super().__init__(message)


# --- Typed configuration dataclasses ---

@dataclass
class VaultSettings:
    root: str


@dataclass
class NetworkSettings:
    host: str
    port: int


@dataclass
class AuthSettings:
    api_keys: dict[str, str]


@dataclass
class PolicySettings:
    allowlist_roots: list[str]
    denylist_roots: list[str]
    propose_only_roots: list[str]
    allowed_operations: list[str]


@dataclass
class IndexSettings:
    db_path: str
    fts_enabled: bool
    fts_language: str


@dataclass
class VectorSettings:
    backend: str


@dataclass
class AuditSettings:
    enabled: bool
    audit_db_path: str
    log_md_path: str


@dataclass
class BackupSettings:
    enabled: bool
    git_path: str


@dataclass
class ServerConfig:
    vault: VaultSettings
    server: NetworkSettings
    auth: AuthSettings
    policy: PolicySettings
    index: IndexSettings
    vector: VectorSettings
    audit: AuditSettings
    backup: BackupSettings


# --- Structured logging helpers ---

def _log_structured(level: int, event: str, *, error_code: str | None = None, data: dict[str, Any] | None = None, function: str = "", block: str = "") -> None:
    entry: dict[str, Any] = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "level": logging.getLevelName(level),
        "module": "memory_mcp.config",
        "function": function,
        "block": block,
        "event": event,
    }
    if error_code:
        entry["error_code"] = error_code
    if data is not None:
        entry["data"] = data
    logger.log(level, json.dumps(entry))


def _sanitize_config(raw: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in raw.items():
        if key == "auth" and isinstance(value, dict):
            auth_copy = dict(value)
            if "api_keys" in auth_copy and isinstance(auth_copy["api_keys"], dict):
                auth_copy["api_keys"] = {k: "<redacted>" for k in auth_copy["api_keys"]}
            result[key] = auth_copy
        else:
            result[key] = value
    return result


# --- Public API ---

def validate_config(config_dict: dict[str, Any]) -> None:
    for section in REQUIRED_SECTIONS:
        if section not in config_dict:
            _log_structured(
                logging.ERROR, "config.invalid",
                error_code="CONFIG_MISSING_SECTION",
                function="validate_config", block="M-011",
                data={"missing_section": section},
            )
            raise ConfigError(
                f"Missing required config section: {section}",
                "CONFIG_MISSING_SECTION",
            )

    vault_root = config_dict.get("vault", {}).get("root", "")
    if not vault_root:
        _log_structured(
            logging.ERROR, "config.invalid",
            error_code="CONFIG_MISSING_VAULT_ROOT",
            function="validate_config", block="M-011",
        )
        raise ConfigError("vault.root is required", "CONFIG_MISSING_VAULT_ROOT")

    normalized = os.path.normpath(vault_root)
    if vault_root in UNSAFE_VAULT_ROOTS or normalized in UNSAFE_VAULT_ROOTS:
        _log_structured(
            logging.ERROR, "config.invalid",
            error_code="CONFIG_UNSAFE_VAULT_ROOT",
            function="validate_config", block="M-011",
            data={"vault_root": vault_root},
        )
        raise ConfigError(f"Unsafe vault root: {vault_root}", "CONFIG_UNSAFE_VAULT_ROOT")

    vector_backend = config_dict.get("vector", {}).get("backend", "")
    if vector_backend not in VALID_VECTOR_BACKENDS:
        _log_structured(
            logging.ERROR, "config.invalid",
            error_code="CONFIG_INVALID_VECTOR_BACKEND",
            function="validate_config", block="M-011",
            data={"backend": vector_backend},
        )
        raise ConfigError(
            f"Invalid vector backend: {vector_backend}",
            "CONFIG_INVALID_VECTOR_BACKEND",
        )


def load_config(config_dict: dict[str, Any]) -> ServerConfig:
    validate_config(config_dict)

    vault = VaultSettings(root=config_dict["vault"]["root"])
    server = NetworkSettings(
        host=config_dict["server"]["host"],
        port=config_dict["server"]["port"],
    )
    auth = AuthSettings(api_keys=config_dict["auth"]["api_keys"])
    policy = PolicySettings(
        allowlist_roots=config_dict["policy"]["allowlist_roots"],
        denylist_roots=config_dict["policy"]["denylist_roots"],
        propose_only_roots=config_dict["policy"]["propose_only_roots"],
        allowed_operations=config_dict["policy"]["allowed_operations"],
    )
    index = IndexSettings(
        db_path=config_dict["index"]["db_path"],
        fts_enabled=config_dict["index"]["fts_enabled"],
        fts_language=config_dict["index"]["fts_language"],
    )
    vector = VectorSettings(backend=config_dict["vector"]["backend"])
    audit = AuditSettings(
        enabled=config_dict["audit"]["enabled"],
        audit_db_path=config_dict["audit"]["audit_db_path"],
        log_md_path=config_dict["audit"]["log_md_path"],
    )
    backup = BackupSettings(
        enabled=config_dict["backup"]["enabled"],
        git_path=config_dict["backup"]["git_path"],
    )

    config = ServerConfig(
        vault=vault, server=server, auth=auth, policy=policy,
        index=index, vector=vector, audit=audit, backup=backup,
    )

    _log_structured(
        logging.INFO, "config.loaded",
        function="load_config", block="M-011",
        data=_sanitize_config(config_dict),
    )

    return config
