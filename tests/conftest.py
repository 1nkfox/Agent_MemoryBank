"""Shared fixtures for GRACE module-local tests."""

import logging
import pytest
from pathlib import Path


@pytest.fixture
def temp_vault_root(tmp_path: Path) -> Path:
    """Isolated Vault root with standard subdirs and sample .md files."""
    vault = tmp_path / "vault"
    (vault / "memory").mkdir(parents=True)
    (vault / "inbox").mkdir(parents=True)
    (vault / "summaries").mkdir(parents=True)
    (vault / "70_Wiki").mkdir(parents=True)
    (vault / "private").mkdir(parents=True)
    (vault / ".obsidian").mkdir(parents=True)
    (vault / ".git").mkdir(parents=True)

    (vault / "memory" / "note.md").write_text(
        "---\ntags: [demo, test]\n---\n\n# Test Note\n\nThis is a test note."
    )
    (vault / "private" / "secret.md").write_text("# Secret\n\nConfidential content.")
    (vault / "inbox" / "draft.md").write_text("# Draft\n\nWork in progress.")
    return vault


@pytest.fixture
def log_capture(caplog):
    """Capture structured JSON log output. Returns list of parsed log entries."""
    caplog.set_level(logging.DEBUG)
    yield caplog


@pytest.fixture
def trace_assert():
    """Helper that asserts trace markers appear in expected order."""

    def _assert_markers(log_entries: list[dict], *expected_markers: str):
        events = [entry.get("event", "") for entry in log_entries]
        idx = -1
        for marker in expected_markers:
            try:
                idx = events.index(marker, idx + 1)
            except ValueError:
                available = ", ".join(events)
                raise AssertionError(
                    f"Expected marker '{marker}' not found in order. Found: [{available}]"
                )
        return True

    return _assert_markers


@pytest.fixture
def sample_config_dict() -> dict:
    """Valid config dict with all sections populated."""
    return {
        "vault": {"root": "/tmp/test-vault"},
        "server": {"host": "127.0.0.1", "port": 8080},
        "auth": {
            "api_keys": {
                "test-key-readonly-001": "readonly_agent",
                "test-key-trusted-001": "trusted_writer",
                "test-key-wiki-001": "wiki_maintainer",
                "test-key-cron-001": "cron_summarizer",
                "test-key-admin-001": "admin",
            }
        },
        "policy": {
            "allowlist_roots": ["memory/", "inbox/", "summaries/", "70_Wiki/"],
            "denylist_roots": ["private/", "secrets/"],
            "propose_only_roots": ["70_Wiki/"],
            "allowed_operations": [
                "read",
                "create",
                "append",
                "edit",
                "write",
                "promote",
                "propose",
                "search",
                "refresh",
                "backup",
            ],
        },
        "index": {
            "db_path": "/tmp/test-vault/.obsidian/index.db",
            "fts_enabled": True,
            "fts_language": "english",
        },
        "vector": {"backend": "disabled"},
        "audit": {
            "enabled": True,
            "audit_db_path": "/tmp/test-vault/.obsidian/audit.db",
            "log_md_path": "/tmp/test-vault/LOG.md",
        },
        "backup": {"enabled": True, "git_path": "/tmp/test-vault/.git"},
    }
