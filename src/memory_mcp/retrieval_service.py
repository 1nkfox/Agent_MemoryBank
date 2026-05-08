"""Explainable note retrieval over the SQLite index - M-010 RetrievalService."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from memory_mcp.config import ServerConfig
from memory_mcp.index_repo import search_fts
from memory_mcp.observability import log_trace_anchor, new_trace_id
from memory_mcp.policy import filter_search_results

MODULE = "retrieval_service"
MODULE_BLOCK = "M-010"


@dataclass
class SearchResult:
    path: str
    snippet: str
    rank: float
    revision: str
    explanation: dict[str, Any]


def _result_explanation(rank: float) -> dict[str, Any]:
    return {
        "source": "fts",
        "sources": {"fts": rank},
        "scoring": {
            "fts_rank": rank,
            "final_rank": rank,
        },
    }


def search_notes(profile: str, query: str, config: ServerConfig) -> list[SearchResult]:
    trace_id = new_trace_id()
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

    filtered_rows = filter_search_results(
        [asdict(result) for result in retrieval_rows],
        config,
    )
    results = [SearchResult(**row) for row in filtered_rows]

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


def explain_ranking(result: SearchResult) -> dict[str, Any]:
    return {
        "path": result.path,
        "rank": result.rank,
        "source_scores": {"fts": result.explanation["scoring"]["fts_rank"]},
        "scoring": {
            "fts_rank": result.explanation["scoring"]["fts_rank"],
            "final_rank": result.rank,
        },
        "source": result.explanation["source"],
    }
