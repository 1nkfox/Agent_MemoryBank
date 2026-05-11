"""Explainable note retrieval with hybrid search (FTS + vector) - M-010 RetrievalService."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from memory_mcp.config import ServerConfig
from memory_mcp.embedding_service import embed_text
from memory_mcp.index_repo import search_fts
from memory_mcp.observability import log_trace_anchor, new_trace_id
from memory_mcp.policy import filter_search_results
from memory_mcp.vector_adapter import VectorQueryResult, create_vector_adapter, query_vectors

MODULE = "retrieval_service"
MODULE_BLOCK = "M-010"

HYBRID_LIMIT = 10


@dataclass
class SearchResult:
    path: str
    snippet: str
    rank: float
    revision: str
    explanation: dict[str, Any]


def _hybrid_explanation(vector_score: float | None, fts_rank: float | None, final_rank: float) -> dict[str, Any]:
    sources: dict[str, float] = {}
    if vector_score is not None:
        sources["vector"] = vector_score
    if fts_rank is not None:
        sources["fts"] = fts_rank
    source_label = "hybrid" if len(sources) > 1 else (list(sources.keys())[0] if sources else "none")
    return {
        "source": source_label,
        "sources": sources,
        "scoring": {
            "vector_score": vector_score,
            "fts_rank": fts_rank,
            "final_rank": final_rank,
        },
    }


def _result_explanation(rank: float, source: str = "fts") -> dict[str, Any]:
    score_key = "vector_score" if source == "vector" else "fts_rank"
    return {
        "source": source,
        "sources": {source: rank},
        "scoring": {
            score_key: rank,
            "final_rank": rank,
        },
    }


def _vector_search_results(vector_results: list[VectorQueryResult]) -> list[SearchResult]:
    return [
        SearchResult(
            path=result.path,
            snippet=str(result.metadata.get("chunk_text", result.metadata.get("snippet", ""))),
            rank=result.score,
            revision=str(result.metadata.get("revision", "")),
            explanation=_result_explanation(result.score, source="vector"),
        )
        for result in vector_results
    ]


def _filtered_results(results: list[SearchResult], config: ServerConfig) -> list[SearchResult]:
    filtered_rows = filter_search_results(
        [asdict(result) for result in results],
        config,
    )
    return [SearchResult(**row) for row in filtered_rows]


def _try_vector_query(query: Any, config: ServerConfig, limit: int) -> list[SearchResult]:
    if config.vector.backend == "disabled":
        return []

    try:
        adapter = create_vector_adapter(config=config)
        return _vector_search_results(query_vectors(adapter, query, limit=limit))
    except Exception:
        return []


def _try_query_embedding(query: str, config: ServerConfig) -> list[float]:
    if config.vector.backend == "disabled":
        return []
    try:
        return embed_text(
            query,
            config.embedding.api_base,
            config.embedding.api_key_env,
            config.embedding.model,
            config.embedding.dimensions,
        )
    except Exception:
        return []


def _union_merge(
    vector_results: list[SearchResult],
    fts_results: list[SearchResult],
) -> list[SearchResult]:
    merged: dict[str, SearchResult] = {}

    for r in fts_results:
        merged[r.path] = SearchResult(
            path=r.path,
            snippet=r.snippet,
            rank=r.rank,
            revision=r.revision,
            explanation=_hybrid_explanation(None, r.rank, r.rank),
        )

    for r in vector_results:
        if r.path in merged:
            existing = merged[r.path]
            fts_rank = existing.explanation["scoring"].get("fts_rank")
            best_rank = max(existing.rank, r.rank)
            merged[r.path] = SearchResult(
                path=r.path,
                snippet=r.snippet if r.snippet else existing.snippet,
                rank=best_rank,
                revision=r.revision or existing.revision,
                explanation=_hybrid_explanation(r.rank, fts_rank, best_rank),
            )
        else:
            merged[r.path] = SearchResult(
                path=r.path,
                snippet=r.snippet,
                rank=r.rank,
                revision=r.revision,
                explanation=_hybrid_explanation(r.rank, None, r.rank),
            )

    return sorted(merged.values(), key=lambda x: x.rank, reverse=True)


def search_notes(profile: str, query: str, config: ServerConfig) -> list[SearchResult]:
    trace_id = new_trace_id()

    vector_results: list[SearchResult] = []
    fts_results: list[SearchResult] = []
    query_source = "fts"

    query_embedding = _try_query_embedding(query, config)
    if query_embedding:
        vector_results = _try_vector_query(query_embedding, config, limit=HYBRID_LIMIT)

    index_results = search_fts(config.index.db_path, query)
    fts_results = [
        SearchResult(
            path=result.path,
            snippet=result.snippet,
            rank=result.rank,
            revision=result.revision,
            explanation=_result_explanation(result.rank),
        )
        for result in index_results
    ]

    if vector_results and fts_results:
        merged = _union_merge(vector_results, fts_results)
        query_source = "hybrid"
    elif vector_results:
        merged = vector_results
        query_source = "vector"
    else:
        merged = fts_results
        query_source = "fts"

    results = _filtered_results(merged, config)

    log_trace_anchor(
        "INFO",
        "retrieval.query.completed",
        trace_id=trace_id,
        module=MODULE,
        function="search_notes",
        block=MODULE_BLOCK,
        data={
            "profile": profile,
            "query": query,
            "raw_vector_count": len(vector_results),
            "raw_fts_count": len(fts_results),
            "result_count": len(results),
            "source": query_source,
        },
    )
    return results


def find_similar_notes(profile: str, path: str, config: ServerConfig, limit: int = 5) -> list[SearchResult]:
    trace_id = new_trace_id()
    vector_rows = _try_vector_query(path, config, limit=limit)
    results = _filtered_results(vector_rows, config)

    log_trace_anchor(
        "INFO",
        "retrieval.query.completed",
        trace_id=trace_id,
        module=MODULE,
        function="find_similar_notes",
        block=MODULE_BLOCK,
        data={
            "profile": profile,
            "path": path,
            "raw_result_count": len(vector_rows),
            "result_count": len(results),
            "source": "vector" if vector_rows else "none",
        },
    )
    return results


def explain_ranking(result: SearchResult) -> dict[str, Any]:
    scoring = result.explanation["scoring"]
    source_scores = result.explanation.get("sources") or {
        key.removesuffix("_rank").removesuffix("_score"): value
        for key, value in scoring.items()
        if key != "final_rank"
    }
    return {
        "path": result.path,
        "rank": result.rank,
        "source_scores": source_scores,
        "scoring": {
            **scoring,
            "final_rank": result.rank,
        },
        "source": result.explanation["source"],
    }
