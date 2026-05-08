"""Vault mutation orchestration — M-008 VaultMutationService."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from memory_mcp.audit_log import record_event, update_log_projection
from memory_mcp.change_planner import MutationPlan, prepare_change
from memory_mcp.concurrency import acquire_path_lock, compute_content_hash, recheck_revision
from memory_mcp.config import ServerConfig
from memory_mcp.observability import ErrorCode, log_trace_anchor, new_trace_id
from memory_mcp.policy import authorize_operation
from memory_mcp.vault_fs import write_file_atomic

MODULE = "memory_mcp.mutation_service"
MODULE_BLOCK = "M-008"


class MutationError(Exception):
    """Raised when a mutation request is invalid or cannot be authorized."""


class PolicyDeniedError(MutationError):
    """Raised when policy rejects a mutation request."""


class MutationConflictError(MutationError):
    """Raised when a mutation cannot proceed because the revision changed."""


@dataclass
class MutationResult:
    success: bool
    affected_paths: list[str]
    audit_event_id: str
    content_hash: str
    conflict: bool
    dry_run: bool


def _emit_started(trace_id: str, function: str, operation: str, path: str, dry_run: bool) -> None:
    log_trace_anchor(
        level="INFO", event="mutation.started", trace_id=trace_id,
        module=MODULE, function=function, block=MODULE_BLOCK,
        data={"operation": operation, "path": path, "dry_run": dry_run},
    )


def _emit_affected_paths(trace_id: str, function: str, affected_paths: list[str]) -> None:
    log_trace_anchor(
        level="INFO", event="mutation.affected_paths.returned", trace_id=trace_id,
        module=MODULE, function=function, block=MODULE_BLOCK,
        data={"affected_paths": affected_paths},
    )


def _emit_completed(trace_id: str, function: str, result: MutationResult) -> MutationResult:
    log_trace_anchor(
        level="INFO", event="mutation.completed", trace_id=trace_id,
        module=MODULE, function=function, block=MODULE_BLOCK,
        data={
            "success": result.success,
            "affected_paths": result.affected_paths,
            "audit_event_id": result.audit_event_id,
            "content_hash": result.content_hash,
            "conflict": result.conflict,
            "dry_run": result.dry_run,
        },
    )
    return result


def _emit_revision_mismatch(trace_id: str, function: str, path: str, current_revision: str, expected_revision: str) -> None:
    log_trace_anchor(
        level="WARNING", event="revision.mismatch", trace_id=trace_id,
        module=MODULE, function=function, block=MODULE_BLOCK,
        error_code=ErrorCode.REVISION_MISMATCH,
        data={
            "path": path,
            "current_revision": current_revision,
            "expected_revision": expected_revision,
        },
    )


def _authorize_or_raise(profile: str, path: str, operation: str, config: ServerConfig) -> None:
    decision = authorize_operation(profile, path, operation, True, config)
    if not decision.allowed:
        raise PolicyDeniedError(decision.reason)


def _require_expected_revision(expected_revision: str) -> None:
    if not expected_revision:
        raise MutationError("expected_revision is required")


def _new_audit_event_data(
    *,
    profile: str,
    operation: str,
    path: str,
    old_revision: str,
    new_revision: str,
    dry_run: bool,
    affected_paths: list[str],
    trace_id: str,
) -> dict[str, Any]:
    return {
        "event_id": str(uuid4()),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "agent_id": profile,
        "operation": operation,
        "path": path,
        "old_revision": old_revision,
        "new_revision": new_revision,
        "dry_run": dry_run,
        "policy_profile": profile,
        "result": "success",
        "affected_paths": affected_paths,
        "trace_id": trace_id,
    }


async def _audit_success(
    *,
    profile: str,
    operation: str,
    path: str,
    old_revision: str,
    new_revision: str,
    affected_paths: list[str],
    trace_id: str,
    config: ServerConfig,
) -> str:
    event = await record_event(
        _new_audit_event_data(
            profile=profile,
            operation=operation,
            path=path,
            old_revision=old_revision,
            new_revision=new_revision,
            dry_run=False,
            affected_paths=affected_paths,
            trace_id=trace_id,
        ),
        config,
    )
    await update_log_projection(event, config)
    return event.event_id


async def create_note(
    profile: str,
    path: str,
    content: str,
    config: ServerConfig,
    dry_run: bool = True,
) -> MutationResult:
    trace_id = new_trace_id()
    function = "create_note"
    _emit_started(trace_id, function, "create", path, dry_run)

    _authorize_or_raise(profile, path, "create", config)

    affected_paths = [path]
    content_hash = compute_content_hash(content)
    audit_event_id = ""

    if not dry_run:
        write_file_atomic(config.vault.root, path, content)
        audit_event_id = await _audit_success(
            profile=profile,
            operation="create",
            path=path,
            old_revision="",
            new_revision=content_hash,
            affected_paths=affected_paths,
            trace_id=trace_id,
            config=config,
        )

    _emit_affected_paths(trace_id, function, affected_paths)
    return _emit_completed(
        trace_id, function,
        MutationResult(
            success=True,
            affected_paths=affected_paths,
            audit_event_id=audit_event_id,
            content_hash=content_hash,
            conflict=False,
            dry_run=dry_run,
        ),
    )


async def append_note(
    profile: str,
    path: str,
    patch_spec: dict[str, Any],
    config: ServerConfig,
    expected_revision: str,
    dry_run: bool = True,
) -> MutationResult:
    return await _apply_planned_mutation(
        profile=profile,
        path=path,
        operation="append",
        patch_spec=patch_spec,
        config=config,
        expected_revision=expected_revision,
        dry_run=dry_run,
        function="append_note",
    )


async def edit_note(
    profile: str,
    path: str,
    patch_spec: dict[str, Any],
    config: ServerConfig,
    expected_revision: str,
    dry_run: bool = True,
) -> MutationResult:
    return await _apply_planned_mutation(
        profile=profile,
        path=path,
        operation="edit",
        patch_spec=patch_spec,
        config=config,
        expected_revision=expected_revision,
        dry_run=dry_run,
        function="edit_note",
    )


async def write_note(
    profile: str,
    path: str,
    content: str,
    config: ServerConfig,
    expected_revision: str,
    dry_run: bool = True,
) -> MutationResult:
    trace_id = new_trace_id()
    function = "write_note"
    _emit_started(trace_id, function, "write", path, dry_run)
    _require_expected_revision(expected_revision)
    _authorize_or_raise(profile, path, "write", config)

    new_revision = compute_content_hash(content)
    if dry_run:
        affected_paths = [path]
        _emit_affected_paths(trace_id, function, affected_paths)
        return _emit_completed(
            trace_id, function,
            MutationResult(True, affected_paths, "", new_revision, False, dry_run),
        )

    async with acquire_path_lock(path):
        revision_check = recheck_revision(config.vault.root, path, expected_revision)
        if not revision_check.ok:
            _emit_revision_mismatch(
                trace_id, function, path,
                revision_check.current_revision, revision_check.expected_revision,
            )
            return _emit_completed(
                trace_id, function,
                MutationResult(False, [], "", revision_check.current_revision, True, dry_run),
            )

        write_file_atomic(config.vault.root, path, content)

    affected_paths = [path]
    audit_event_id = await _audit_success(
        profile=profile,
        operation="write",
        path=path,
        old_revision=expected_revision,
        new_revision=new_revision,
        affected_paths=affected_paths,
        trace_id=trace_id,
        config=config,
    )
    _emit_affected_paths(trace_id, function, affected_paths)
    return _emit_completed(
        trace_id, function,
        MutationResult(True, affected_paths, audit_event_id, new_revision, False, dry_run),
    )


async def _apply_planned_mutation(
    *,
    profile: str,
    path: str,
    operation: str,
    patch_spec: dict[str, Any],
    config: ServerConfig,
    expected_revision: str,
    dry_run: bool,
    function: str,
) -> MutationResult:
    trace_id = new_trace_id()
    _emit_started(trace_id, function, operation, path, dry_run)
    _require_expected_revision(expected_revision)

    plan = prepare_change(
        profile,
        path,
        operation,
        patch_spec,
        config,
        expected_revision=expected_revision,
        dry_run=True,
    )
    new_revision = compute_content_hash(plan.new_content)

    if dry_run:
        affected_paths = [path]
        _emit_affected_paths(trace_id, function, affected_paths)
        return _emit_completed(
            trace_id, function,
            MutationResult(True, affected_paths, "", new_revision, False, dry_run),
        )

    conflict = await _write_plan_if_revision_matches(plan, config, trace_id, function, dry_run)
    if conflict is not None:
        return conflict

    affected_paths = [path]
    audit_event_id = await _audit_success(
        profile=profile,
        operation=operation,
        path=path,
        old_revision=expected_revision,
        new_revision=new_revision,
        affected_paths=affected_paths,
        trace_id=trace_id,
        config=config,
    )
    _emit_affected_paths(trace_id, function, affected_paths)
    return _emit_completed(
        trace_id, function,
        MutationResult(True, affected_paths, audit_event_id, new_revision, False, dry_run),
    )


async def _write_plan_if_revision_matches(
    plan: MutationPlan,
    config: ServerConfig,
    trace_id: str,
    function: str,
    dry_run: bool,
) -> MutationResult | None:
    async with acquire_path_lock(plan.path):
        revision_check = recheck_revision(config.vault.root, plan.path, plan.expected_revision)
        if not revision_check.ok:
            _emit_revision_mismatch(
                trace_id, function, plan.path,
                revision_check.current_revision, revision_check.expected_revision,
            )
            return _emit_completed(
                trace_id, function,
                MutationResult(False, [], "", revision_check.current_revision, True, dry_run),
            )

        write_file_atomic(config.vault.root, plan.path, plan.new_content)
    return None
