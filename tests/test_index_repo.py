"""Module-local tests for M-013 IndexRepository."""

from __future__ import annotations

import json
import logging
import sqlite3

import memory_mcp.index_repo as repo
from memory_mcp.index_repo import (
    VectorSearchResult,
    initialize_schema,
    query_similar,
    search_fts,
    upsert_note_chunks,
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


def test_search_fts_fallback_on_fts_unavailable(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    db_path = str(tmp_path / "index.db")
    initialize_schema(db_path)
    upsert_note_index(db_path, "memory/test.md", "Apple banana cherry.", "rev-a")

    fts_supported_original = repo._fts_supported
    repo._fts_supported = lambda conn: False
    try:
        results = search_fts(db_path, "banana")
    finally:
        repo._fts_supported = fts_supported_original

    assert len(results) == 1
    assert results[0].path == "memory/test.md"
    assert "banana" in results[0].snippet.lower()
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


def test_upsert_note_chunks_stores_and_retrieves(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    db_path = str(tmp_path / "index.db")
    initialize_schema(db_path)

    chunks = [
        {"chunk_id": "intro", "text": "Introduction content", "revision": "r1", "embedding": [0.1] * 1536},
        {"chunk_id": "details", "text": "Details content", "revision": "r1", "embedding": [0.2] * 1536},
    ]

    upsert_note_chunks(db_path, "memory/test.md", chunks)

    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT chunk_id, chunk_text, revision FROM note_chunks WHERE path = ? ORDER BY id",
            ("memory/test.md",),
        ).fetchall()

    assert len(rows) == 2
    assert rows[0][0] == "intro"
    assert rows[0][1] == "Introduction content"
    assert rows[1][0] == "details"
    assert "index.vector.upsert.completed" in [entry.get("event") for entry in _parse_logs(caplog)]


def test_upsert_note_chunks_replaces_prior_chunks(tmp_path):
    db_path = str(tmp_path / "index.db")
    initialize_schema(db_path)

    chunks_v1 = [
        {"chunk_id": "h1", "text": "Version 1", "revision": "r1", "embedding": [0.1] * 1536},
    ]
    upsert_note_chunks(db_path, "memory/test.md", chunks_v1)

    chunks_v2 = [
        {"chunk_id": "h1", "text": "Version 2", "revision": "r2", "embedding": [0.3] * 1536},
    ]
    upsert_note_chunks(db_path, "memory/test.md", chunks_v2)

    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT chunk_text, revision FROM note_chunks WHERE path = ?",
            ("memory/test.md",),
        ).fetchall()

    assert len(rows) == 1
    assert rows[0][0] == "Version 2"
    assert rows[0][1] == "r2"


def test_delete_note_chunks_removes_chunks(tmp_path):
    db_path = str(tmp_path / "index.db")
    initialize_schema(db_path)

    chunks = [
        {"chunk_id": "h1", "text": "Content", "revision": "r1", "embedding": [0.1] * 1536},
    ]
    upsert_note_chunks(db_path, "memory/test.md", chunks)
    repo.delete_note_chunks(db_path, "memory/test.md")

    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT COUNT(*) FROM note_chunks WHERE path = ?",
            ("memory/test.md",),
        ).fetchone()
    assert rows[0] == 0


def test_query_similar_returns_empty_when_no_vec_table(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    db_path = str(tmp_path / "index.db")
    initialize_schema(db_path)

    results = query_similar(db_path, [0.1] * 1536, limit=5)

    assert results == []
    assert isinstance(results, list)


def test_query_similar_returns_empty_for_empty_embedding(tmp_path):
    db_path = str(tmp_path / "index.db")
    initialize_schema(db_path)

    results = query_similar(db_path, [], limit=5)

    assert results == []


def test_vector_search_result_dataclass():
    result = VectorSearchResult(
        path="memory/test.md",
        chunk_id="intro",
        chunk_text="Introduction text",
        score=0.95,
        revision="abc123",
    )

    assert result.path == "memory/test.md"
    assert result.chunk_id == "intro"
    assert result.chunk_text == "Introduction text"
    assert result.score == 0.95
    assert result.revision == "abc123"
