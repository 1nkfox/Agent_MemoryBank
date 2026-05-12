"""Vault layout detection and Directory Contract resolution — M-022 VaultLayoutService."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from memory_mcp.observability import (
    ErrorCode,
    log_trace_anchor,
    new_trace_id,
)
from memory_mcp.vault_fs import resolve_vault_path

MODULE = "vault_layout"
MODULE_BLOCK = "M-022"

INITIAL_SEMANTIC_KEY_MAP: dict[str, str] = {
    "inbox": "00_Inbox",
    "memory": "memory",
    "summaries": "summaries",
    "wiki": "70_Wiki",
    "log": "00_Log",
    "system": ".system",
}

DEFAULT_DEPRECATED_ALIASES: dict[str, list[str]] = {
    "inbox": ["Inbox", "capture", "incoming", "notes"],
    "memory": ["Memory", "notes", "mem"],
    "summaries": ["Summaries", "summary", "digest"],
    "wiki": ["Wiki", "articles", "knowledge"],
    "log": ["Log", "journal", "diary", "activity"],
    "system": ["System", "meta", "internal"],
}

_INBOX_KEY = "inbox"


class VaultLayoutError(Exception):
    def __init__(self, message: str, error_code: str = ErrorCode.CONFIG_INVALID):
        self.error_code = error_code
        super().__init__(message)


class DeprecatedAliasError(VaultLayoutError):
    def __init__(self, alias: str, canonical_key: str):
        self.alias = alias
        self.canonical_key = canonical_key
        super().__init__(
            f"Deprecated alias '{alias}' is denied as a write target. "
            f"Use canonical key '{canonical_key}' instead.",
            "DEPRECATED_ALIAS_DENIED",
        )


class CanonicalPathOutsideVaultError(VaultLayoutError):
    def __init__(self, semantic_key: str, relative_path: str):
        self.semantic_key = semantic_key
        self.relative_path = relative_path
        super().__init__(
            f"Canonical path '{relative_path}' for key '{semantic_key}' "
            f"is outside or invalid relative to vault root.",
            "CANONICAL_PATH_OUTSIDE_VAULT",
        )


# --- Data types ---

@dataclass
class CanonicalPath:
    semantic_key: str
    relative_path: str
    absolute_path: str


@dataclass
class DirectoryRule:
    semantic_key: str
    canonical_path: CanonicalPath
    purpose: str
    deprecated_aliases: list[str] = field(default_factory=list)


@dataclass
class DirectoryContract:
    keys: dict[str, CanonicalPath]
    aliases: dict[str, str]
    created_at: str


@dataclass
class DirectoryDriftReport:
    missing_canonical: list[str]
    ambiguous_aliases: list[str]
    unknown_dirs: list[str]

    @property
    def ok(self) -> bool:
        return not (self.missing_canonical or self.ambiguous_aliases or self.unknown_dirs)


@dataclass
class VaultLayoutReport:
    vault_root: str
    obsidian_marker_found: bool
    canonical_paths: list[CanonicalPath]
    trace_id: str


# --- Private helpers ---

def _normalize_dir_entries(dir_entries: list[str]) -> frozenset[str]:
    return frozenset(p.lower().rstrip("/\\") for p in dir_entries if p.strip())


def _build_deprecated_map() -> dict[str, str]:
    result: dict[str, str] = {}
    for key_name, aliases in DEFAULT_DEPRECATED_ALIASES.items():
        for alias in aliases:
            lowered = alias.lower()
            if lowered not in result:
                result[lowered] = key_name
    return result


# --- Public API ---

def detect_vault_layout(
    vault_root: str,
    require_obsidian_marker: bool = True,
) -> VaultLayoutReport:
    trace_id = new_trace_id()

    obsidian_rel = ".obsidian"
    obsidian_found = False

    try:
        obsidian_path = resolve_vault_path(vault_root, obsidian_rel)
        obsidian_found = os.path.isdir(obsidian_path)
    except Exception:
        obsidian_found = False

    if require_obsidian_marker and not obsidian_found:
        log_trace_anchor(
            level="WARNING",
            event="vault_layout.obsidian_marker_missing",
            trace_id=trace_id,
            module=MODULE,
            function="detect_vault_layout",
            block=MODULE_BLOCK,
            data={"vault_root": vault_root},
        )
        raise VaultLayoutError(
            f"No .obsidian marker found in vault root: {vault_root}",
            "OBSIDIAN_MARKER_MISSING",
        )

    if obsidian_found:
        log_trace_anchor(
            level="INFO",
            event="vault_layout.detected",
            trace_id=trace_id,
            module=MODULE,
            function="detect_vault_layout",
            block=MODULE_BLOCK,
            data={"vault_root": vault_root},
        )

    return VaultLayoutReport(
        vault_root=vault_root,
        obsidian_marker_found=obsidian_found,
        canonical_paths=[],
        trace_id=trace_id,
    )


def resolve_directory_contract(
    config_key_map: dict[str, str],
    vault_root: str,
) -> DirectoryContract:
    trace_id = new_trace_id()

    effective_map = dict(config_key_map) if config_key_map else dict(INITIAL_SEMANTIC_KEY_MAP)

    keys: dict[str, CanonicalPath] = {}
    for semantic_key in sorted(effective_map.keys()):
        relative_path = effective_map[semantic_key]
        try:
            abs_path = resolve_vault_path(vault_root, relative_path)
        except Exception as exc:
            log_trace_anchor(
                level="ERROR",
                event="vault_layout.canonical_path.rejected",
                trace_id=trace_id,
                module=MODULE,
                function="resolve_directory_contract",
                block=MODULE_BLOCK,
                error_code="CANONICAL_PATH_OUTSIDE_VAULT",
                data={
                    "semantic_key": semantic_key,
                    "relative_path": relative_path,
                    "error": str(exc),
                },
            )
            raise CanonicalPathOutsideVaultError(semantic_key, relative_path) from exc

        keys[semantic_key] = CanonicalPath(
            semantic_key=semantic_key,
            relative_path=relative_path,
            absolute_path=abs_path,
        )

    primary_aliases: dict[str, str] = {}
    for sk in keys:
        primary_aliases[sk.lower()] = sk

    for sk in keys:
        for alias in DEFAULT_DEPRECATED_ALIASES.get(sk, []):
            lowered = alias.lower()
            if lowered not in primary_aliases:
                primary_aliases[lowered] = sk

    log_trace_anchor(
        level="INFO",
        event="vault_layout.canonical_paths.resolved",
        trace_id=trace_id,
        module=MODULE,
        function="resolve_directory_contract",
        block=MODULE_BLOCK,
        data={"keys": list(keys.keys()), "vault_root": vault_root},
    )

    return DirectoryContract(
        keys=keys,
        aliases=primary_aliases,
        created_at=datetime.now(timezone.utc).isoformat(),
    )


def resolve_destination_for_intent(
    intent: str,
    directory_contract: DirectoryContract,
) -> str:
    trace_id = new_trace_id()
    lowered = intent.lower().strip()

    if lowered in directory_contract.keys:
        canonical = directory_contract.keys[lowered]
        log_trace_anchor(
            level="DEBUG",
            event="vault_layout.destination.resolved",
            trace_id=trace_id,
            module=MODULE,
            function="resolve_destination_for_intent",
            block=MODULE_BLOCK,
            data={"intent": intent, "semantic_key": lowered, "path": canonical.absolute_path},
        )
        return canonical.absolute_path

    deprecated_map = _build_deprecated_map()
    if lowered in deprecated_map:
        canonical_key = deprecated_map[lowered]
        log_trace_anchor(
            level="WARNING",
            event="vault_layout.destination.denied",
            trace_id=trace_id,
            module=MODULE,
            function="resolve_destination_for_intent",
            block=MODULE_BLOCK,
            error_code="DEPRECATED_ALIAS_DENIED",
            data={"intent": intent, "alias": lowered, "canonical_key": canonical_key},
        )
        raise DeprecatedAliasError(intent, canonical_key)

    if lowered in directory_contract.aliases:
        semantic_key = directory_contract.aliases[lowered]
        canonical = directory_contract.keys[semantic_key]
        log_trace_anchor(
            level="DEBUG",
            event="vault_layout.destination.resolved",
            trace_id=trace_id,
            module=MODULE,
            function="resolve_destination_for_intent",
            block=MODULE_BLOCK,
            data={"intent": intent, "alias": lowered, "semantic_key": semantic_key, "path": canonical.absolute_path},
        )
        return canonical.absolute_path

    if _INBOX_KEY in directory_contract.keys:
        inbox = directory_contract.keys[_INBOX_KEY]
        log_trace_anchor(
            level="INFO",
            event="vault_layout.destination.generic_capture",
            trace_id=trace_id,
            module=MODULE,
            function="resolve_destination_for_intent",
            block=MODULE_BLOCK,
            data={"intent": intent, "fallback": _INBOX_KEY, "path": inbox.absolute_path},
        )
        return inbox.absolute_path

    raise VaultLayoutError(
        f"Cannot resolve destination for intent '{intent}': no matching key, alias, or fallback inbox.",
        "UNRESOLVABLE_INTENT",
    )


def detect_directory_drift(
    directory_contract: DirectoryContract,
    vault_root: str,
    actual_dirs: list[str],
) -> DirectoryDriftReport:
    trace_id = new_trace_id()

    actual_set = _normalize_dir_entries(actual_dirs)

    known_relative_set: frozenset[str] = frozenset(
        cp.relative_path.lower().rstrip("/\\") for cp in directory_contract.keys.values()
    )

    missing_canonical: list[str] = []
    for cp in directory_contract.keys.values():
        expected_path = cp.absolute_path
        if not os.path.isdir(expected_path):
            missing_canonical.append(cp.semantic_key)

    canonical_lowered = frozenset(k.lower() for k in directory_contract.keys)

    alias_to_keys: dict[str, set[str]] = {}
    for key_name, aliases in DEFAULT_DEPRECATED_ALIASES.items():
        if key_name not in directory_contract.keys:
            continue
        for alias in aliases:
            lowered = alias.lower()
            if lowered not in alias_to_keys:
                alias_to_keys[lowered] = set()
            alias_to_keys[lowered].add(key_name)

    ambiguous_aliases: list[str] = []
    for alias_lowered, target_keys in alias_to_keys.items():
        if len(target_keys) > 1:
            ambiguous_aliases.append(alias_lowered)

    for alias_lowered, target_keys in alias_to_keys.items():
        if alias_lowered in canonical_lowered:
            for tk in target_keys:
                if tk.lower() != alias_lowered:
                    if alias_lowered not in ambiguous_aliases:
                        ambiguous_aliases.append(alias_lowered)

    unknown_aliased = set(ambiguous_aliases)
    deprecated_lowered = set(alias_to_keys.keys())
    unknown_dirs: list[str] = sorted(
        d for d in actual_set
        if d not in known_relative_set
        and d not in canonical_lowered
        and d not in deprecated_lowered
        and d not in unknown_aliased
        and d not in {".obsidian", ".git", ".trash"}
    )

    drift_found = bool(missing_canonical or ambiguous_aliases or unknown_dirs)

    if drift_found:
        log_trace_anchor(
            level="WARNING",
            event="vault_layout.drift_detected",
            trace_id=trace_id,
            module=MODULE,
            function="detect_directory_drift",
            block=MODULE_BLOCK,
            data={
                "missing_canonical": missing_canonical,
                "ambiguous_aliases": ambiguous_aliases,
                "unknown_dirs": unknown_dirs,
            },
        )

    return DirectoryDriftReport(
        missing_canonical=missing_canonical,
        ambiguous_aliases=ambiguous_aliases,
        unknown_dirs=unknown_dirs,
    )
