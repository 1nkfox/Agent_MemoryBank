"""Manual Git backup integration - M-017 BackupService."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from memory_mcp.config import ServerConfig
from memory_mcp.observability import log_trace_anchor, new_trace_id

MODULE = "memory_mcp.backup_service"
MODULE_BLOCK = "M-017"


@dataclass
class BackupResult:
    success: bool
    commit_hash: str | None
    message: str


def _repo_git_dir(config: ServerConfig) -> Path:
    vault_git_dir = Path(config.vault.root) / ".git"
    if vault_git_dir.exists():
        return vault_git_dir
    return Path(config.backup.git_path)


def _run_git(args: list[str], config: ServerConfig) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=config.vault.root,
        capture_output=True,
        text=True,
        check=False,
    )


def backup_health(config: ServerConfig) -> dict[str, Any]:
    trace_id = new_trace_id()
    repo_exists = _repo_git_dir(config).exists()
    git_available = False
    error = ""

    try:
        version = _run_git(["--version"], config)
        git_available = version.returncode == 0
        if not git_available:
            error = (version.stderr or version.stdout).strip()
    except OSError as exc:
        error = str(exc)

    if not repo_exists:
        error = error or ".git repository missing"

    health: dict[str, Any] = {
        "git_available": git_available,
        "repo_exists": repo_exists,
        "healthy": git_available and repo_exists,
    }
    if error:
        health["error"] = error

    log_trace_anchor(
        level="INFO",
        event="backup.health.checked",
        trace_id=trace_id,
        module=MODULE,
        function="backup_health",
        block=MODULE_BLOCK,
        data=health,
    )
    return health


def backup_vault(config: ServerConfig, message: str = "manual backup") -> BackupResult:
    trace_id = new_trace_id()
    health = backup_health(config)
    if not health["healthy"]:
        return BackupResult(False, None, str(health.get("error", "backup unavailable")))

    add_result = _run_git(["add", "-A"], config)
    if add_result.returncode != 0:
        return BackupResult(False, None, (add_result.stderr or add_result.stdout).strip())

    commit_result = _run_git(["commit", "-m", message], config)
    if commit_result.returncode != 0:
        return BackupResult(False, None, (commit_result.stderr or commit_result.stdout).strip())

    rev_parse_result = _run_git(["rev-parse", "HEAD"], config)
    if rev_parse_result.returncode != 0:
        return BackupResult(False, None, (rev_parse_result.stderr or rev_parse_result.stdout).strip())

    commit_hash = rev_parse_result.stdout.strip()
    result = BackupResult(True, commit_hash, message)
    log_trace_anchor(
        level="INFO",
        event="backup.snapshot.completed",
        trace_id=trace_id,
        module=MODULE,
        function="backup_vault",
        block=MODULE_BLOCK,
        data={"commit_hash": result.commit_hash, "message": result.message},
    )
    return result
