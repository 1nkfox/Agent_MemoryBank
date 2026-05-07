"""Agent identity and API key resolution — M-002 AuthIdentityService."""

from __future__ import annotations

from dataclasses import dataclass

from memory_mcp.config import ServerConfig
from memory_mcp.observability import (
    ErrorCode,
    log_trace_anchor,
    new_trace_id,
)

VALID_PROFILES = frozenset({
    "readonly_agent",
    "trusted_writer",
    "wiki_maintainer",
    "cron_summarizer",
    "admin",
})

PolicyProfile = str


class AuthError(Exception):
    def __init__(self, message: str, error_code: str):
        self.error_code = error_code
        super().__init__(message)


@dataclass
class AgentIdentity:
    key_id: str
    profile: str


def _fingerprint(api_key: str) -> str:
    if len(api_key) > 8:
        return f"{api_key[:4]}...{api_key[-4:]}"
    return api_key[:4] + "...****"


def resolve_identity(api_key: str, config: ServerConfig) -> tuple[AgentIdentity, PolicyProfile]:
    trace_id = new_trace_id()
    key_id = _fingerprint(api_key)

    profile = config.auth.api_keys.get(api_key)

    if profile is None:
        log_trace_anchor(
            "WARNING", "auth.identity.rejected",
            trace_id=trace_id, module="memory_mcp.auth",
            function="resolve_identity", block="M-002",
            data={"key_id": key_id},
            error_code=ErrorCode.AUTH_UNKNOWN_KEY,
        )
        raise AuthError(
            f"No matching identity for API key {key_id}",
            ErrorCode.AUTH_UNKNOWN_KEY,
        )

    if profile == "revoked":
        log_trace_anchor(
            "WARNING", "auth.identity.rejected",
            trace_id=trace_id, module="memory_mcp.auth",
            function="resolve_identity", block="M-002",
            data={"key_id": key_id, "profile": profile},
            error_code=ErrorCode.AUTH_INVALID_KEY,
        )
        raise AuthError(
            f"API key {key_id} has been revoked",
            ErrorCode.AUTH_INVALID_KEY,
        )

    identity = AgentIdentity(key_id=key_id, profile=profile)

    log_trace_anchor(
        "INFO", "auth.identity.resolved",
        trace_id=trace_id, module="memory_mcp.auth",
        function="resolve_identity", block="M-002",
        data={"key_id": key_id, "profile": profile},
    )

    return identity, profile
