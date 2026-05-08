"""Draft promotion orchestration - M-019 DraftPromotionService."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from memory_mcp.audit_log import record_event, update_log_projection
from memory_mcp.concurrency import acquire_path_lock, compute_content_hash
from memory_mcp.config import ServerConfig
from memory_mcp.observability import ErrorCode, log_trace_anchor, new_trace_id
from memory_mcp.policy import authorize_operation
from memory_mcp.vault_fs import move_file_atomic, read_file, resolve_vault_path

MODULE = "memory_mcp.draft_promotion_service"
MODULE_BLOCK = "M-019"


class PromotionError(Exception):
    """Raised when a promotion request is invalid or cannot be authorized."""


class PromotionPolicyDeniedError(PromotionError):
    """Raised when policy rejects a promotion request."""


class PromotionConflictError(PromotionError):
    """Raised when a promotion would overwrite an existing target."""


@dataclass
class PromotionResult:
    success: bool
    affected_paths: list[str]
    audit_event_id: str
    dry_run: bool
    conflict: bool


def _emit_plan_created(trace_id: str, function: str, source_path: str, dest_path: str, dry_run: bool) -> None:
    log_trace_anchor(
        level="INFO",
        event="promotion.plan.created",
        trace_id=trace_id,
        module=MODULE,
        function=function,
        block=MODULE_BLOCK,
        data={"source_path": source_path, "dest_path": dest_path, "dry_run": dry_run},
    )


def _emit_completed(trace_id: str, function: str, result: PromotionResult) -> PromotionResult:
    log_trace_anchor(
        level="INFO",
        event="promotion.completed",
        trace_id=trace_id,
        module=MODULE,
        function=function,
        block=MODULE_BLOCK,
        data={
            "success": result.success,
            "affected_paths": result.affected_paths,
            "audit_event_id": result.audit_event_id,
            "dry_run": result.dry_run,
            "conflict": result.conflict,
        },
    )
    return result


def _reject_bulk_path(path: object, field: str) -> str:
    if not isinstance(path, str) or not path.strip():
        raise PromotionError(f"{field} must be a single non-empty path string")
    if "," in path:
        raise PromotionError(f"{field} must be a single path")
    return path


def _validate_single_file(source_path: object, dest_path: object) -> tuple[str, str]:
    source = _reject_bulk_path(source_path, "source_path")
    dest = _reject_bulk_path(dest_path, "dest_path")
    if source == dest:
        raise PromotionError("source_path and dest_path must differ")
    if source.endswith("/") or dest.endswith("/"):
        raise PromotionError("promotion requires file paths, not directories")
    return source, dest


def _authorize_or_raise(profile: str, dest_path: str, config: ServerConfig) -> None:
    decision = authorize_operation(profile, dest_path, "promote", True, config)
    if not decision.allowed:
        raise PromotionPolicyDeniedError(decision.reason)


def _dest_exists(config: ServerConfig, dest_path: str) -> bool:
    resolved = resolve_vault_path(config.vault.root, dest_path)
    return os.path.exists(resolved)


def _source_lock_path(config: ServerConfig, source_path: str) -> str:
    return str(Path(config.vault.root) / source_path)


def _new_audit_event_data(
    *,
    profile: str,
    source_path: str,
    affected_paths: list[str],
    revision: str,
    trace_id: str,
) -> dict[str, Any]:
    return {
        "event_id": str(uuid4()),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "agent_id": profile,
        "operation": "promote",
        "path": source_path,
        "old_revision": revision,
        "new_revision": revision,
        "dry_run": False,
        "policy_profile": profile,
        "result": "success",
        "affected_paths": affected_paths,
        "trace_id": trace_id,
    }


async def _audit_success(
    *,
    profile: str,
    source_path: str,
    affected_paths: list[str],
    revision: str,
    trace_id: str,
    config: ServerConfig,
) -> str:
    event = await record_event(
        _new_audit_event_data(
            profile=profile,
            source_path=source_path,
            affected_paths=affected_paths,
            revision=revision,
            trace_id=trace_id,
        ),
        config,
    )
    await update_log_projection(event, config)
    return event.event_id


async def move_note_once(
    profile: str,
    source_path: object,
    dest_path: object,
    config: ServerConfig,
    dry_run: bool = True,
) -> PromotionResult:
    trace_id = new_trace_id()
    function = "move_note_once"
    source, dest = _validate_single_file(source_path, dest_path)
    _emit_plan_created(trace_id, function, source, dest, dry_run)

    _authorize_or_raise(profile, dest, config)
    affected_paths = [source, dest]

    async with acquire_path_lock(_source_lock_path(config, source)):
        if _dest_exists(config, dest):
            log_trace_anchor(
                level="WARNING",
                event="promotion.conflict",
                trace_id=trace_id,
                module=MODULE,
                function=function,
                block=MODULE_BLOCK,
                error_code=ErrorCode.POLICY_DENIED,
                data={"source_path": source, "dest_path": dest, "reason": "target_exists"},
            )
            raise PromotionConflictError(f"Destination already exists: {dest}")

        content = read_file(config.vault.root, source)
        revision = compute_content_hash(content)
        audit_event_id = ""

        if not dry_run:
            move_file_atomic(config.vault.root, source, dest)
            audit_event_id = await _audit_success(
                profile=profile,
                source_path=source,
                affected_paths=affected_paths,
                revision=revision,
                trace_id=trace_id,
                config=config,
            )

    return _emit_completed(
        trace_id,
        function,
        PromotionResult(
            success=True,
            affected_paths=affected_paths,
            audit_event_id=audit_event_id,
            dry_run=dry_run,
            conflict=False,
        ),
    )


async def promote_note(
    profile: str,
    source_path: object,
    dest_path: object,
    config: ServerConfig,
    dry_run: bool = True,
) -> PromotionResult:
    return await move_note_once(profile, source_path, dest_path, config, dry_run=dry_run)
