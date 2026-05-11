"""SQLite index repository - M-013 IndexRepository."""

from __future__ import annotations

import json
import sqlite3
import struct
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


@dataclass
class VectorSearchResult:
    path: str
    chunk_id: str
    chunk_text: str
    score: float
    revision: str


def _vec0_supported(conn: sqlite3.Connection) -> bool:
    return _table_exists(conn, "note_chunks_vec")


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


def _create_vec_table(conn: sqlite3.Connection) -> bool:
    try:
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS note_chunks_vec "
            "USING vec0(chunk_embedding float[1536])"
        )
    except (sqlite3.OperationalError, Exception):
        return False
    return True


def initialize_schema(db_path: str) -> None:
    trace_id = new_trace_id()
    vec_enabled = False
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
            "CREATE TABLE IF NOT EXISTS note_chunks ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "path TEXT NOT NULL, "
            "chunk_id TEXT NOT NULL, "
            "chunk_text TEXT NOT NULL, "
            "revision TEXT NOT NULL"
            ")"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_note_chunks_path ON note_chunks(path)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS index_state ("
            "path TEXT PRIMARY KEY, "
            "revision TEXT NOT NULL, "
            "updated_at TEXT NOT NULL"
            ")"
        )
        fts_enabled = _create_fts_table(conn)
        vec_enabled = _create_vec_table(conn)

    log_trace_anchor(
        level="INFO",
        event="index.schema.ready",
        trace_id=trace_id,
        module=MODULE,
        function="initialize_schema",
        block=MODULE_BLOCK,
        data={"db_path": db_path, "fts_enabled": fts_enabled, "vec_enabled": vec_enabled},
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


def upsert_note_chunks(
    db_path: str,
    path: str,
    chunks: list[dict[str, Any]],
) -> None:
    with _connect(db_path) as conn:
        conn.execute("DELETE FROM note_chunks WHERE path = ?", (path,))
        if _vec0_supported(conn):
            chunk_rows = conn.execute(
                "SELECT id FROM note_chunks_vec WHERE rowid IN "
                "(SELECT id FROM note_chunks WHERE path = ?)",
                (path,),
            ).fetchall()
            for row in chunk_rows:
                conn.execute("DELETE FROM note_chunks_vec WHERE rowid = ?", (row["id"],))

        for chunk in chunks:
            embedding = chunk.get("embedding", [])
            chunk_text = chunk.get("chunk_text", chunk.get("text", ""))
            chunk_id = chunk.get("chunk_id", "")
            revision = chunk.get("revision", "")

            cursor = conn.execute(
                "INSERT INTO note_chunks (path, chunk_id, chunk_text, revision) "
                "VALUES (?, ?, ?, ?)",
                (path, chunk_id, chunk_text, revision),
            )
            chunk_row_id = cursor.lastrowid

            if _vec0_supported(conn) and embedding and len(embedding) == 1536:
                vec_blob = struct.pack(f"{len(embedding)}f", *embedding)
                conn.execute(
                    "INSERT INTO note_chunks_vec (rowid, chunk_embedding) VALUES (?, ?)",
                    (chunk_row_id, vec_blob),
                )

    trace_id = new_trace_id()
    log_trace_anchor(
        level="INFO",
        event="index.vector.upsert.completed",
        trace_id=trace_id,
        module=MODULE,
        function="upsert_note_chunks",
        block=MODULE_BLOCK,
        data={"path": path, "chunk_count": len(chunks)},
    )


def delete_note_chunks(db_path: str, path: str) -> None:
    with _connect(db_path) as conn:
        if _vec0_supported(conn):
            chunk_ids = conn.execute(
                "SELECT id FROM note_chunks WHERE path = ?",
                (path,),
            ).fetchall()
            for row in chunk_ids:
                conn.execute("DELETE FROM note_chunks_vec WHERE rowid = ?", (row["id"],))
        conn.execute("DELETE FROM note_chunks WHERE path = ?", (path,))

    trace_id = new_trace_id()
    log_trace_anchor(
        level="INFO",
        event="index.vector.delete.completed",
        trace_id=trace_id,
        module=MODULE,
        function="delete_note_chunks",
        block=MODULE_BLOCK,
        data={"path": path},
    )


def query_similar(
    db_path: str,
    embedding: list[float],
    limit: int = 5,
) -> list[VectorSearchResult]:
    trace_id = new_trace_id()
    results: list[VectorSearchResult] = []

    with _connect(db_path) as conn:
        if not _vec0_supported(conn):
            log_trace_anchor(
                level="INFO",
                event="index.vector.query.completed",
                trace_id=trace_id,
                module=MODULE,
                function="query_similar",
                block=MODULE_BLOCK,
                data={"result_count": 0, "vec_available": False},
            )
            return results

        if not embedding:
            return results

        vec_blob = struct.pack(f"{len(embedding)}f", *embedding)
        try:
            rows = conn.execute(
                "SELECT nc.path, nc.chunk_id, nc.chunk_text, nc.revision, v.distance "
                "FROM note_chunks_vec v "
                "JOIN note_chunks nc ON nc.id = v.rowid "
                "WHERE v.chunk_embedding MATCH ? "
                "ORDER BY v.distance "
                "LIMIT ?",
                (vec_blob, limit),
            ).fetchall()
        except sqlite3.OperationalError:
            rows = []

        for row in rows:
            results.append(VectorSearchResult(
                path=row["path"],
                chunk_id=row["chunk_id"],
                chunk_text=row["chunk_text"],
                score=float(1.0 - row["distance"]) if row["distance"] is not None else 0.0,
                revision=row["revision"],
            ))

    log_trace_anchor(
        level="INFO",
        event="index.vector.query.completed",
        trace_id=trace_id,
        module=MODULE,
        function="query_similar",
        block=MODULE_BLOCK,
        data={"result_count": len(results), "vec_available": True},
    )

    return results


def search_fts(db_path: str, query: str) -> list[SearchResult]:
    trace_id = new_trace_id()
    initialize_schema(db_path)
    with _connect(db_path) as conn:
        fts_used = False
        rows: list[sqlite3.Row] = []
        if _fts_supported(conn):
            try:
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
            except sqlite3.OperationalError:
                pass  # FTS5 query parse failure → fall through to LIKE

        if not fts_used:
            like_query = f"%{query}%"
            rows = conn.execute(
                "SELECT path, content AS snippet, 0.0 AS rank, revision "
                "FROM notes WHERE content LIKE ? ORDER BY path",
                (like_query,),
            ).fetchall()

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
