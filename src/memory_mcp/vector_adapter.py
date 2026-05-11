"""Optional vector search adapter boundary - M-016 VectorSearchAdapter."""

from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from typing import Any, Protocol

from memory_mcp.config import ServerConfig
from memory_mcp.observability import log_trace_anchor, new_trace_id

MODULE = "vector_adapter"
MODULE_BLOCK = "M-016"
VALID_VECTOR_MODES = {"disabled", "sqlite_vec", "qdrant_optional"}


@dataclass(frozen=True)
class VectorQueryResult:
    path: str
    score: float
    metadata: dict[str, Any] = field(default_factory=dict)


class VectorAdapter(Protocol):
    mode: str

    def upsert_vectors(self, items: list[dict[str, Any]]) -> None:
        """Persist vector items for later semantic retrieval."""

    def query_vectors(self, query: Any, limit: int = 5) -> list[VectorQueryResult]:
        """Return vector matches ordered by descending relevance."""


@dataclass
class NoopVectorAdapter:
    requested_mode: str = "disabled"
    mode: str = "disabled"

    def upsert_vectors(self, items: list[dict[str, Any]]) -> None:
        return None

    def query_vectors(self, query: Any, limit: int = 5) -> list[VectorQueryResult]:
        return []


@dataclass
class SqliteVecVectorAdapter:
    sqlite_vec: Any
    mode: str = "sqlite_vec"
    db_path: str = ""

    def upsert_vectors(self, items: list[dict[str, Any]]) -> None:
        if not self.db_path or not items:
            return None
        from memory_mcp.index_repo import upsert_note_chunks

        by_path: dict[str, list[dict[str, Any]]] = {}
        for item in items:
            path = item.get("path", "")
            if path not in by_path:
                by_path[path] = []
            by_path[path].append(item)

        for path, chunks in by_path.items():
            upsert_note_chunks(self.db_path, path, chunks)

    def query_vectors(self, query: Any, limit: int = 5) -> list[VectorQueryResult]:
        if not self.db_path or not query:
            return []
        from memory_mcp.index_repo import query_similar

        if isinstance(query, list):
            embedding = query
        elif hasattr(query, "embedding"):
            embedding = query.embedding
        else:
            return []
        results = query_similar(self.db_path, embedding, limit=limit)

        return [
            VectorQueryResult(
                path=r.path,
                score=r.score,
                metadata={"chunk_id": r.chunk_id, "chunk_text": r.chunk_text, "revision": r.revision},
            )
            for r in results
        ]


@dataclass
class QdrantOptionalVectorAdapter:
    qdrant_client: Any
    mode: str = "qdrant_optional"

    def upsert_vectors(self, items: list[dict[str, Any]]) -> None:
        return None

    def query_vectors(self, query: Any, limit: int = 5) -> list[VectorQueryResult]:
        return []


def _emit_mode_selected(trace_id: str, requested_mode: str, selected_mode: str) -> None:
    log_trace_anchor(
        "INFO",
        "vector.mode.selected",
        trace_id=trace_id,
        module=MODULE,
        function="create_vector_adapter",
        block=MODULE_BLOCK,
        data={"requested_mode": requested_mode, "selected_mode": selected_mode},
    )


def _emit_degraded_to_fts(trace_id: str, requested_mode: str, reason: str) -> None:
    log_trace_anchor(
        "WARNING",
        "vector.degraded_to_fts",
        trace_id=trace_id,
        module=MODULE,
        function="create_vector_adapter",
        block=MODULE_BLOCK,
        data={"requested_mode": requested_mode, "reason": reason},
    )


def _resolve_mode(mode: str | None, config: ServerConfig | None) -> str:
    selected = mode if mode is not None else config.vector.backend if config is not None else "disabled"
    if selected not in VALID_VECTOR_MODES:
        raise ValueError(f"Unsupported vector adapter mode: {selected}")
    return selected


def _optional_import(module_name: str) -> Any | None:
    try:
        return importlib.import_module(module_name)
    except ImportError:
        return None


def create_vector_adapter(mode: str | None = None, config: ServerConfig | None = None) -> VectorAdapter:
    trace_id = new_trace_id()
    requested_mode = _resolve_mode(mode, config)

    if requested_mode == "disabled":
        adapter: VectorAdapter = NoopVectorAdapter(requested_mode=requested_mode)
        _emit_mode_selected(trace_id, requested_mode, adapter.mode)
        return adapter

    if requested_mode == "sqlite_vec":
        sqlite_vec = _optional_import("sqlite_vec")
        if sqlite_vec is None:
            _emit_degraded_to_fts(trace_id, requested_mode, "sqlite_vec_unavailable")
            adapter = NoopVectorAdapter(requested_mode=requested_mode)
        else:
            db_path = config.index.db_path if config and hasattr(config, "index") else ""
            adapter = SqliteVecVectorAdapter(sqlite_vec=sqlite_vec, db_path=db_path)
        _emit_mode_selected(trace_id, requested_mode, adapter.mode)
        return adapter

    qdrant_client = _optional_import("qdrant_client")
    if qdrant_client is None:
        _emit_degraded_to_fts(trace_id, requested_mode, "qdrant_client_unavailable")
        adapter = NoopVectorAdapter(requested_mode=requested_mode)
    else:
        adapter = QdrantOptionalVectorAdapter(qdrant_client=qdrant_client)
    _emit_mode_selected(trace_id, requested_mode, adapter.mode)
    return adapter


def upsert_vectors(adapter: VectorAdapter, items: list[dict[str, Any]]) -> None:
    adapter.upsert_vectors(items)


def query_vectors(adapter: VectorAdapter, query: Any, limit: int = 5) -> list[VectorQueryResult]:
    return adapter.query_vectors(query, limit=limit)
