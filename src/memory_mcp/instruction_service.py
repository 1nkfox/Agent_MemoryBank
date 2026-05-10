"""Role-aware agent instructions from identity, policy profile, and Directory Contract — M-021 InstructionService."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from memory_mcp.auth import AgentIdentity, PolicyProfile
from memory_mcp.observability import log_trace_anchor, new_trace_id
from memory_mcp.policy import ALWAYS_DENIED, DRY_RUN_REQUIRED, PERMISSION_MATRIX
from memory_mcp.vault_layout import (
    DEFAULT_DEPRECATED_ALIASES,
    DirectoryContract,
    DirectoryDriftReport,
)

MODULE = "instruction_service"
MODULE_BLOCK = "M-021"

PRODUCT_ALIASES: list[str] = ["AgentMemBank", "Agent Memory Bank", "Memory Bank MCP"]

ALL_PROFILES: frozenset[str] = frozenset({
    "readonly_agent",
    "trusted_writer",
    "wiki_maintainer",
    "cron_summarizer",
    "admin",
})

SAFE_WORKFLOWS_BY_PROFILE: dict[str, list[str]] = {
    "readonly_agent": [
        "search + read only",
    ],
    "trusted_writer": [
        "create/append/edit/write with required dry_run",
    ],
    "wiki_maintainer": [
        "find_wiki_sources + propose_wiki_update",
    ],
    "cron_summarizer": [
        "read + append to summaries only",
    ],
    "admin": [
        "all operations except delete/bulk_move",
    ],
}


@dataclass
class InstructionPacket:
    product_aliases: list[str] = field(default_factory=lambda: list(PRODUCT_ALIASES))
    role: str = ""
    capabilities: list[str] = field(default_factory=list)
    directory_contract_keys: list[str] = field(default_factory=list)
    canonical_paths: dict[str, str] = field(default_factory=dict)
    safe_workflows: list[str] = field(default_factory=list)
    drift_warnings: list[str] = field(default_factory=list)
    forbidden_writes: list[str] = field(default_factory=list)
    generated_at: str = ""


def compose_role_guidance(profile: PolicyProfile) -> str:
    caps = sorted(PERMISSION_MATRIX.get(profile, set()))
    workflows = SAFE_WORKFLOWS_BY_PROFILE.get(profile, [])

    forbidden = sorted(ALWAYS_DENIED)
    dry_run_ops = sorted(DRY_RUN_REQUIRED & PERMISSION_MATRIX.get(profile, set()))

    parts: list[str] = []
    parts.append(f"Role: {profile}")
    parts.append(f"Capabilities: {', '.join(caps) if caps else 'none'}")

    if dry_run_ops:
        parts.append(f"Operations requiring dry_run: {', '.join(dry_run_ops)}")

    parts.append(f"Always denied operations: {', '.join(forbidden) if forbidden else 'none'}")

    if workflows:
        parts.append("Safe workflows:")
        for wf in workflows:
            parts.append(f"  - {wf}")
    else:
        parts.append("Safe workflows: none")

    return "\n".join(parts)


def compose_directory_guidance(directory_contract: DirectoryContract) -> str:
    parts: list[str] = []

    if not directory_contract.keys:
        parts.append("No directory contract entries available.")
        parts.append("Refer to the service documentation for target directory conventions.")
        return "\n".join(parts)

    parts.append("Directory Contract:")
    for sk in sorted(directory_contract.keys.keys()):
        cp = directory_contract.keys[sk]
        parts.append(f"  {sk}: {cp.relative_path} ({cp.absolute_path})")

    deprecated = DEFAULT_DEPRECATED_ALIASES
    if deprecated:
        parts.append("")
        parts.append("Deprecated aliases (do not use as write targets):")
        for sk in sorted(deprecated.keys()):
            if sk in directory_contract.keys:
                alias_list = deprecated[sk]
                parts.append(f"  {sk}: {', '.join(alias_list)}")

    return "\n".join(parts)


def get_instructions(
    identity: AgentIdentity,
    profile: PolicyProfile,
    directory_contract: DirectoryContract,
    directory_drift: DirectoryDriftReport | None = None,
) -> InstructionPacket:
    trace_id = new_trace_id()

    profile = identity.profile

    caps = sorted(PERMISSION_MATRIX.get(profile, set()))

    contract_keys = sorted(directory_contract.keys.keys())

    canonical_paths: dict[str, str] = {}
    for sk in contract_keys:
        canonical_paths[sk] = directory_contract.keys[sk].relative_path

    safe_workflows = list(SAFE_WORKFLOWS_BY_PROFILE.get(profile, []))

    forbidden_writes = sorted(ALWAYS_DENIED)

    drift_warnings: list[str] = []

    deprecated = DEFAULT_DEPRECATED_ALIASES
    for sk in contract_keys:
        aliases = deprecated.get(sk, [])
        for alias in aliases:
            drift_warnings.append(
                f"Alias '{alias}' for '{sk}' is deprecated — use canonical key '{sk}'"
            )

    if directory_drift is not None and not directory_drift.ok:
        if directory_drift.missing_canonical:
            for key in directory_drift.missing_canonical:
                drift_warnings.append(
                    f"Missing canonical directory: '{key}'"
                )

        if directory_drift.ambiguous_aliases:
            for alias in directory_drift.ambiguous_aliases:
                drift_warnings.append(
                    f"Ambiguous alias detected: '{alias}'"
                )

        if directory_drift.unknown_dirs:
            for dirname in directory_drift.unknown_dirs:
                drift_warnings.append(
                    f"Unknown directory detected in vault: '{dirname}'"
                )

        log_trace_anchor(
            level="WARNING",
            event="instructions.layout_warning.included",
            trace_id=trace_id,
            module=MODULE,
            function="get_instructions",
            block=MODULE_BLOCK,
            data={
                "profile": profile,
                "drift_warnings_count": len(drift_warnings),
            },
        )

    log_trace_anchor(
        level="INFO",
        event="instructions.role_context.included",
        trace_id=trace_id,
        module=MODULE,
        function="get_instructions",
        block=MODULE_BLOCK,
        data={"profile": profile, "capabilities": caps},
    )

    packet = InstructionPacket(
        product_aliases=list(PRODUCT_ALIASES),
        role=profile,
        capabilities=caps,
        directory_contract_keys=contract_keys,
        canonical_paths=canonical_paths,
        safe_workflows=safe_workflows,
        drift_warnings=drift_warnings,
        forbidden_writes=forbidden_writes,
        generated_at=datetime.now(timezone.utc).isoformat(),
    )

    log_trace_anchor(
        level="INFO",
        event="instructions.generated",
        trace_id=trace_id,
        module=MODULE,
        function="get_instructions",
        block=MODULE_BLOCK,
        data={
            "profile": profile,
            "contract_keys": contract_keys,
            "drift_warnings_count": len(drift_warnings),
        },
    )

    return packet
