"""Module-local tests for M-013 IndexRepository."""

from __future__ import annotations

import json
import logging
import sqlite3

from memory_mcp.index_repo import (
    initialize_schema,
    search_fts,
    upsert_note_index,
)


def _parse_logs(caplog) -> list[dict]:
    entries: list[dict] = []
    for record in caplog.records:
        try:
            entries.append(json.loads(record.message))
        except json.JSONDecodeError:
            pass
    return entries


def test_schema_initializes_with_wal_and_busy_timeout(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    db_path = str(tmp_path / "index.db")

    initialize_schema(db_path)

    with sqlite3.connect(db_path) as conn:
        journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        busy_timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'virtual table')"
            )
        }

    assert journal_mode.lower() == "wal"
    assert busy_timeout == 5000
    assert "notes" in tables
    assert "index_state" in tables
    assert "index.schema.ready" in [entry.get("event") for entry in _parse_logs(caplog)]


def test_upsert_note_index_stores_revision_in_notes_and_index_state(tmp_path):
    db_path = str(tmp_path / "index.db")
    initialize_schema(db_path)

    upsert_note_index(
        db_path,
        "memory/note.md",
        "# Note\n\nBody text.",
        "rev-001",
        {"tags": ["demo"]},
    )

    with sqlite3.connect(db_path) as conn:
        note = conn.execute(
            "SELECT revision, metadata_json FROM notes WHERE path = ?",
            ("memory/note.md",),
        ).fetchone()
        state = conn.execute(
            "SELECT revision FROM index_state WHERE path = ?",
            ("memory/note.md",),
        ).fetchone()

    assert note[0] == "rev-001"
    assert json.loads(note[1]) == {"tags": ["demo"]}
    assert state[0] == "rev-001"


def test_search_fts_returns_matching_rows(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    db_path = str(tmp_path / "index.db")
    initialize_schema(db_path)
    upsert_note_index(db_path, "memory/apples.md", "Fresh apple pie recipe.", "rev-a")
    upsert_note_index(db_path, "memory/bananas.md", "Yellow banana bread.", "rev-b")

    results = search_fts(db_path, "apple")

    assert [result.path for result in results] == ["memory/apples.md"]
    assert results[0].revision == "rev-a"
    assert "apple" in results[0].snippet.lower()
    assert "index.fts.query.completed" in [entry.get("event") for entry in _parse_logs(caplog)]


def test_stale_index_state_detectable_when_revision_differs(tmp_path):
    db_path = str(tmp_path / "index.db")
    initialize_schema(db_path)
    upsert_note_index(db_path, "memory/note.md", "Current indexed content.", "rev-old")

    vault_revision = "rev-new"
    with sqlite3.connect(db_path) as conn:
        indexed_revision = conn.execute(
            "SELECT revision FROM index_state WHERE path = ?",
            ("memory/note.md",),
        ).fetchone()[0]

    assert indexed_revision != vault_revision
