"""Policy enforcement for Vault operations — M-003 VaultPolicyGuard."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from memory_mcp.config import ServerConfig
from memory_mcp.observability import (
    ErrorCode,
    log_trace_anchor,
    new_trace_id,
)

PERMISSION_MATRIX: dict[str, set[str]] = {
    "readonly_agent": {"read", "search", "propose"},
    "trusted_writer": {"read", "search", "create", "append", "edit", "write", "propose"},
    "wiki_maintainer": {"read", "search", "propose"},
    "cron_summarizer": {"read", "search", "append", "propose"},
    "admin": {"read", "search", "create", "append", "edit", "write", "promote", "propose", "refresh", "backup"},
}

DRY_RUN_REQUIRED: frozenset[str] = frozenset({"create", "append", "edit", "write", "promote"})

ALWAYS_DENIED: frozenset[str] = frozenset({"delete", "bulk_move"})


@dataclass
class PolicyDecision:
    allowed: bool
    reason: str = ""


def _emit_denied(trace_id: str, path: str, operation: str, profile: str, reason: str) -> PolicyDecision:
    log_trace_anchor(
        "WARNING", "policy.denied",
        trace_id=trace_id, module="memory_mcp.policy",
        function="authorize_operation", block="M-003",
        data={"path": path, "operation": operation, "profile": profile, "reason": reason},
        error_code=ErrorCode.POLICY_DENIED,
    )
    return PolicyDecision(allowed=False, reason=reason)


def _emit_allowed(trace_id: str, path: str, operation: str, profile: str) -> PolicyDecision:
    log_trace_anchor(
        "INFO", "policy.decision",
        trace_id=trace_id, module="memory_mcp.policy",
        function="authorize_operation", block="M-003",
        data={"path": path, "operation": operation, "profile": profile, "allowed": True},
    )
    return PolicyDecision(allowed=True)


def authorize_operation(
    profile: str,
    path: str,
    operation: str,
    dry_run: bool,
    config: ServerConfig,
) -> PolicyDecision:
    trace_id = new_trace_id()
    policy = config.policy

    for root in policy.denylist_roots:
        if path.startswith(root):
            return _emit_denied(
                trace_id, path, operation, profile,
                f"Path '{path}' matches denylist root '{root}'",
            )

    if operation in ALWAYS_DENIED:
        return _emit_denied(
            trace_id, path, operation, profile,
            f"Operation '{operation}' is always denied (MVP constraint)",
        )

    for root in policy.propose_only_roots:
        if path.startswith(root) and operation != "propose":
            return _emit_denied(
                trace_id, path, operation, profile,
                f"Path '{path}' is in propose_only root '{root}'; only 'propose' operation allowed",
            )

    allowed_ops = PERMISSION_MATRIX.get(profile, set())
    if operation not in allowed_ops:
        return _emit_denied(
            trace_id, path, operation, profile,
            f"Profile '{profile}' is not permitted to perform '{operation}'",
        )

    if operation in DRY_RUN_REQUIRED and not dry_run:
        return _emit_denied(
            trace_id, path, operation, profile,
            f"Operation '{operation}' requires dry_run=True",
        )

    return _emit_allowed(trace_id, path, operation, profile)


def check_path_policy(path: str, config: ServerConfig, is_symlink: bool = False) -> PolicyDecision:
    trace_id = new_trace_id()
    policy = config.policy

    if is_symlink:
        log_trace_anchor(
            "WARNING", "policy.denied",
            trace_id=trace_id, module="memory_mcp.policy",
            function="check_path_policy", block="M-003",
            data={"path": path, "reason": "symlinks_forbidden"},
            error_code=ErrorCode.POLICY_DENIED,
        )
        return PolicyDecision(allowed=False, reason="symlinks_forbidden")

    for root in policy.denylist_roots:
        if path.startswith(root):
            log_trace_anchor(
                "WARNING", "policy.denied",
                trace_id=trace_id, module="memory_mcp.policy",
                function="check_path_policy", block="M-003",
                data={"path": path, "reason": f"matches denylist root '{root}'"},
                error_code=ErrorCode.POLICY_DENIED,
            )
            return PolicyDecision(allowed=False, reason=f"Path '{path}' matches denylist root '{root}'")

    for root in policy.propose_only_roots:
        if path.startswith(root):
            log_trace_anchor(
                "INFO", "policy.decision",
                trace_id=trace_id, module="memory_mcp.policy",
                function="check_path_policy", block="M-003",
                data={"path": path, "restricted": True, "restriction": "propose_only"},
            )
            return PolicyDecision(allowed=True, reason=f"Path '{path}' is in propose_only root '{root}'")

    log_trace_anchor(
        "INFO", "policy.decision",
        trace_id=trace_id, module="memory_mcp.policy",
        function="check_path_policy", block="M-003",
        data={"path": path, "allowed": True},
    )
    return PolicyDecision(allowed=True)


def filter_search_results(search_results: list[dict[str, Any]], config: ServerConfig) -> list[dict[str, Any]]:
    trace_id = new_trace_id()
    denylist = config.policy.denylist_roots

    filtered: list[dict[str, Any]] = []
    for result in search_results:
        path = result.get("path", "")
        if any(path.startswith(root) for root in denylist):
            log_trace_anchor(
                "INFO", "retrieval.denied_path.filtered",
                trace_id=trace_id, module="memory_mcp.policy",
                function="filter_search_results", block="M-003",
                data={"path": path},
            )
        else:
            filtered.append(result)

    return filtered
