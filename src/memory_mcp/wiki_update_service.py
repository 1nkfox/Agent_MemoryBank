"""Propose-only wiki update planning - M-018 WikiUpdateService."""

from __future__ import annotations

import difflib
import os
from dataclasses import dataclass

from memory_mcp.config import ServerConfig
from memory_mcp.observability import log_trace_anchor, new_trace_id
from memory_mcp.policy import authorize_operation
from memory_mcp.retrieval_service import SearchResult, search_notes
from memory_mcp.vault_fs import read_file, resolve_vault_path

MODULE = "memory_mcp.wiki_update_service"
MODULE_BLOCK = "M-018"


@dataclass
class WikiProposal:
    wiki_path: str
    diff: str
    source_paths: list[str]
    dry_run: bool = True


def find_wiki_sources(profile: str, query: str, config: ServerConfig) -> list[SearchResult]:
    trace_id = new_trace_id()
    results = search_notes(profile, query, config)
    source_paths = [result.path for result in results]

    log_trace_anchor(
        level="INFO",
        event="wiki.sources.selected",
        trace_id=trace_id,
        module=MODULE,
        function="find_wiki_sources",
        block=MODULE_BLOCK,
        data={"profile": profile, "query": query, "source_paths": source_paths},
    )
    return results


def _source_reference_block(source_paths: list[str]) -> str:
    if not source_paths:
        return ""
    refs = "\n".join(f"- [[{path}]]" for path in source_paths)
    return f"\n\n## Sources\n\n{refs}\n"


def _with_source_references(proposed_content: str, source_paths: list[str]) -> str:
    refs = _source_reference_block(source_paths)
    if not refs:
        return proposed_content
    content = proposed_content.rstrip()
    return f"{content}{refs}"


def generate_wiki_diff(existing_content: str, proposed_content: str, source_paths: list[str]) -> str:
    proposed_with_sources = _with_source_references(proposed_content, source_paths)
    old_lines = existing_content.splitlines(keepends=True)
    new_lines = proposed_with_sources.splitlines(keepends=True)
    diff_lines = difflib.unified_diff(
        old_lines,
        new_lines,
        fromfile="a/70_Wiki/wiki.md",
        tofile="b/70_Wiki/wiki.md",
    )
    return "".join(diff_lines)


def _read_existing_or_empty(config: ServerConfig, wiki_path: str) -> str:
    resolved = resolve_vault_path(config.vault.root, wiki_path)
    if not os.path.exists(resolved):
        return ""
    return read_file(config.vault.root, wiki_path)


def propose_wiki_update(
    profile: str,
    wiki_path: str,
    query: str,
    proposed_content: str,
    config: ServerConfig,
) -> WikiProposal:
    trace_id = new_trace_id()
    decision = authorize_operation(profile, wiki_path, "propose", True, config)
    if not decision.allowed:
        raise PermissionError(decision.reason)

    sources = find_wiki_sources(profile, query, config)
    source_paths = [source.path for source in sources]
    existing_content = _read_existing_or_empty(config, wiki_path)
    diff = generate_wiki_diff(existing_content, proposed_content, source_paths)

    log_trace_anchor(
        level="INFO",
        event="wiki.diff.generated",
        trace_id=trace_id,
        module=MODULE,
        function="propose_wiki_update",
        block=MODULE_BLOCK,
        data={"wiki_path": wiki_path, "source_paths": source_paths, "dry_run": True},
    )

    return WikiProposal(wiki_path=wiki_path, diff=diff, source_paths=source_paths, dry_run=True)
