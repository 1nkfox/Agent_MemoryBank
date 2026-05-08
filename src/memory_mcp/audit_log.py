"""Audit log service — M-009 AuditLogService."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from memory_mcp.config import ServerConfig
from memory_mcp.concurrency import acquire_path_lock
from memory_mcp.observability import (
    ErrorCode,
    log_trace_anchor,
    new_trace_id,
)
from memory_mcp.vault_fs import read_file, write_file_atomic

MODULE = "audit_log"
MODULE_BLOCK = "M-009"

_AUDIT_NDJSON_REL = ".obsidian/audit.ndjson"
_LOG_MD_REL = "LOG.md"
_OBSIDIAN_PREFIX = ".obsidian/"

REQUIRED_FIELDS = frozenset({
    "event_id", "timestamp", "agent_id", "operation", "path",
    "old_revision", "new_revision", "dry_run", "policy_profile",
    "result", "affected_paths", "trace_id",
})


@dataclass
class AuditEvent:
    event_id: str
    timestamp: str
    agent_id: str
    operation: str
    path: str
    old_revision: str
    new_revision: str
    dry_run: bool
    policy_profile: str
    result: str
    affected_paths: list[str]
    trace_id: str


async def record_event(event_data: dict[str, Any], config: ServerConfig) -> AuditEvent:
    trace_id = new_trace_id()
    vault_root = config.vault.root

    missing = REQUIRED_FIELDS - set(event_data.keys())
    if missing:
        log_trace_anchor(
            level="ERROR", event="audit.record_event.failed", trace_id=trace_id,
            module=MODULE, function="record_event",
            block=MODULE_BLOCK, error_code=ErrorCode.AUDIT_WRITE_FAILED,
            data={"reason": f"Missing required fields: {sorted(missing)}"},
        )
        raise ValueError(f"Missing required audit fields: {sorted(missing)}")

    event = AuditEvent(
        event_id=event_data["event_id"],
        timestamp=event_data["timestamp"],
        agent_id=event_data["agent_id"],
        operation=event_data["operation"],
        path=event_data["path"],
        old_revision=event_data["old_revision"],
        new_revision=event_data["new_revision"],
        dry_run=event_data["dry_run"],
        policy_profile=event_data["policy_profile"],
        result=event_data["result"],
        affected_paths=event_data["affected_paths"],
        trace_id=event_data["trace_id"],
    )

    if event.path.startswith(_OBSIDIAN_PREFIX):
        log_trace_anchor(
            level="DEBUG", event="audit.record_event.skipped", trace_id=trace_id,
            module=MODULE, function="record_event",
            block=MODULE_BLOCK,
            data={"reason": "Path in .obsidian/ excluded from audit", "path": event.path},
        )
        return event

    audit_lock_path = str(Path(vault_root) / _AUDIT_NDJSON_REL)
    serialized = json.dumps({
        "event_id": event.event_id,
        "timestamp": event.timestamp,
        "agent_id": event.agent_id,
        "operation": event.operation,
        "path": event.path,
        "old_revision": event.old_revision,
        "new_revision": event.new_revision,
        "dry_run": event.dry_run,
        "policy_profile": event.policy_profile,
        "result": event.result,
        "affected_paths": event.affected_paths,
        "trace_id": event.trace_id,
    })

    async with acquire_path_lock(audit_lock_path):
        existing = ""
        try:
            existing = read_file(vault_root, _AUDIT_NDJSON_REL)
        except (FileNotFoundError, OSError):
            pass

        new_content = existing + serialized + "\n" if existing else serialized + "\n"
        write_file_atomic(vault_root, _AUDIT_NDJSON_REL, new_content)

    log_trace_anchor(
        level="INFO", event="audit.authoritative_event.appended", trace_id=trace_id,
        module=MODULE, function="record_event",
        block=MODULE_BLOCK,
        data={"event_id": event.event_id, "path": event.path, "operation": event.operation},
    )

    return event


async def update_log_projection(event: AuditEvent, config: ServerConfig) -> None:
    trace_id = new_trace_id()
    vault_root = config.vault.root

    line = (
        f"- [{event.timestamp}] {event.agent_id} "
        f"{event.operation} {event.path} → {event.result}"
    )

    log_lock_path = str(Path(vault_root) / _LOG_MD_REL)

    async with acquire_path_lock(log_lock_path):
        existing = ""
        try:
            existing = read_file(vault_root, _LOG_MD_REL)
        except (FileNotFoundError, OSError):
            pass

        if existing and not existing.endswith("\n"):
            existing += "\n"
        new_content = existing + line + "\n"
        write_file_atomic(vault_root, _LOG_MD_REL, new_content)

    log_trace_anchor(
        level="INFO", event="audit.log_projection.updated", trace_id=trace_id,
        module=MODULE, function="update_log_projection",
        block=MODULE_BLOCK,
        data={"event_id": event.event_id, "path": event.path},
    )
