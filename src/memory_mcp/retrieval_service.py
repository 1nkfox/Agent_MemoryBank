"""Explainable note retrieval over the SQLite index - M-010 RetrievalService."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from memory_mcp.config import ServerConfig
from memory_mcp.index_repo import search_fts
from memory_mcp.observability import log_trace_anchor, new_trace_id
from memory_mcp.policy import filter_search_results
from memory_mcp.vector_adapter import VectorQueryResult, create_vector_adapter, query_vectors

MODULE = "retrieval_service"
MODULE_BLOCK = "M-010"


@dataclass
class SearchResult:
    path: str
    snippet: str
    rank: float
    revision: str
    explanation: dict[str, Any]


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
            snippet=str(result.metadata.get("snippet", "")),
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


def search_notes(profile: str, query: str, config: ServerConfig) -> list[SearchResult]:
    trace_id = new_trace_id()
    vector_rows = _try_vector_query(query, config, limit=5)
    if vector_rows:
        results = _filtered_results(vector_rows, config)
        if results:
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
                    "raw_result_count": len(vector_rows),
                    "result_count": len(results),
                    "source": "vector",
                },
            )
            return results

    index_results = search_fts(config.index.db_path, query)
    retrieval_rows = [
        SearchResult(
            path=result.path,
            snippet=result.snippet,
            rank=result.rank,
            revision=result.revision,
            explanation=_result_explanation(result.rank),
        )
        for result in index_results
    ]

    results = _filtered_results(retrieval_rows, config)

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
            "raw_result_count": len(index_results),
            "result_count": len(results),
            "source": "fts",
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
