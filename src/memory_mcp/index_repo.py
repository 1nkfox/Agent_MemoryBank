"""SQLite index repository - M-013 IndexRepository."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from memory_mcp.observability import log_trace_anchor, new_trace_id

MODULE = "index_repo"
MODULE_BLOCK = "M-013"
BUSY_TIMEOUT_MS = 5000


@dataclass
class SearchResult:
    path: str
    snippet: str
    rank: float
    revision: str


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    return conn


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name = ? LIMIT 1",
        (table_name,),
    ).fetchone()
    return row is not None


def _create_fts_table(conn: sqlite3.Connection) -> bool:
    try:
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts "
            "USING fts5(path UNINDEXED, content)"
        )
    except sqlite3.OperationalError:
        return False
    return True


def _fts_supported(conn: sqlite3.Connection) -> bool:
    return _table_exists(conn, "notes_fts")


def initialize_schema(db_path: str) -> None:
    trace_id = new_trace_id()
    with _connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS notes ("
            "path TEXT PRIMARY KEY, "
            "content TEXT NOT NULL, "
            "revision TEXT NOT NULL, "
            "metadata_json TEXT, "
            "updated_at TEXT NOT NULL"
            ")"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS index_state ("
            "path TEXT PRIMARY KEY, "
            "revision TEXT NOT NULL, "
            "updated_at TEXT NOT NULL"
            ")"
        )
        fts_enabled = _create_fts_table(conn)

    log_trace_anchor(
        level="INFO",
        event="index.schema.ready",
        trace_id=trace_id,
        module=MODULE,
        function="initialize_schema",
        block=MODULE_BLOCK,
        data={"db_path": db_path, "fts_enabled": fts_enabled},
    )


def upsert_note_index(
    db_path: str,
    path: str,
    content: str,
    revision: str,
    metadata: dict[str, Any] | None = None,
) -> None:
    updated_at = _utc_now()
    metadata_json = json.dumps(metadata or {}, sort_keys=True)

    with _connect(db_path) as conn:
        conn.execute(
            "INSERT INTO notes(path, content, revision, metadata_json, updated_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(path) DO UPDATE SET "
            "content = excluded.content, "
            "revision = excluded.revision, "
            "metadata_json = excluded.metadata_json, "
            "updated_at = excluded.updated_at",
            (path, content, revision, metadata_json, updated_at),
        )
        conn.execute(
            "INSERT INTO index_state(path, revision, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(path) DO UPDATE SET "
            "revision = excluded.revision, updated_at = excluded.updated_at",
            (path, revision, updated_at),
        )
        if _fts_supported(conn):
            conn.execute("DELETE FROM notes_fts WHERE path = ?", (path,))
            conn.execute(
                "INSERT INTO notes_fts(path, content) VALUES (?, ?)",
                (path, content),
            )


def delete_note_index(db_path: str, path: str) -> None:
    with _connect(db_path) as conn:
        conn.execute("DELETE FROM notes WHERE path = ?", (path,))
        conn.execute("DELETE FROM index_state WHERE path = ?", (path,))
        if _fts_supported(conn):
            conn.execute("DELETE FROM notes_fts WHERE path = ?", (path,))


def search_fts(db_path: str, query: str) -> list[SearchResult]:
    trace_id = new_trace_id()
    with _connect(db_path) as conn:
        if _fts_supported(conn):
            rows = conn.execute(
                "SELECT notes.path, "
                "snippet(notes_fts, 1, '<mark>', '</mark>', '...', 16) AS snippet, "
                "bm25(notes_fts) AS rank, notes.revision "
                "FROM notes_fts "
                "JOIN notes ON notes.path = notes_fts.path "
                "WHERE notes_fts MATCH ? "
                "ORDER BY rank",
                (query,),
            ).fetchall()
            fts_used = True
        else:
            like_query = f"%{query}%"
            rows = conn.execute(
                "SELECT path, content AS snippet, 0.0 AS rank, revision "
                "FROM notes WHERE content LIKE ? ORDER BY path",
                (like_query,),
            ).fetchall()
            fts_used = False

    results = [
        SearchResult(
            path=row["path"],
            snippet=row["snippet"],
            rank=float(row["rank"]),
            revision=row["revision"],
        )
        for row in rows
    ]
    log_trace_anchor(
        level="INFO",
        event="index.fts.query.completed",
        trace_id=trace_id,
        module=MODULE,
        function="search_fts",
        block=MODULE_BLOCK,
        data={"query": query, "result_count": len(results), "fts_used": fts_used},
    )
    return results
