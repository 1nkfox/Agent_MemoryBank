"""Module-local tests for M-010 RetrievalService."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from memory_mcp.config import load_config
from memory_mcp.index_repo import initialize_schema, search_fts, upsert_note_index
from memory_mcp.retrieval_service import explain_ranking, find_similar_notes, search_notes
from memory_mcp.vector_adapter import VectorQueryResult


def _parsed_logs(caplog) -> list[dict]:
    entries: list[dict] = []
    for record in caplog.records:
        try:
            entries.append(json.loads(record.message))
        except (json.JSONDecodeError, TypeError):
            pass
    return entries


def _make_config(tmp_path: Path, sample_config_dict: dict, *, vector_backend: str = "disabled"):
    config_dict = {
        **sample_config_dict,
        "index": {
            **sample_config_dict["index"],
            "db_path": str(tmp_path / "index.db"),
        },
        "vector": {"backend": vector_backend},
    }
    return load_config(config_dict)


def _seed_index(db_path: str) -> None:
    initialize_schema(db_path)
    upsert_note_index(
        db_path,
        "memory/apples.md",
        "# Apples\n\nFresh apple pie recipe with cinnamon.",
        "rev-apple",
    )
    upsert_note_index(
        db_path,
        "private/secret-apples.md",
        "# Secret Apples\n\nPrivate apple storage notes.",
        "rev-secret",
    )
    upsert_note_index(
        db_path,
        "memory/bananas.md",
        "# Bananas\n\nYellow banana bread.",
        "rev-banana",
    )


def test_fts_search_returns_matches_with_snippets(tmp_path, sample_config_dict, caplog, trace_assert):
    caplog.set_level(logging.DEBUG)
    config = _make_config(tmp_path, sample_config_dict)
    _seed_index(config.index.db_path)

    results = search_notes("readonly_agent", "apple", config)

    assert len(results) > 0
    assert results[0].path == "memory/apples.md"
    assert "apple" in results[0].snippet.lower()
    entries = _parsed_logs(caplog)
    assert trace_assert(entries, "retrieval.query.completed")


def test_ranking_is_explainable(tmp_path, sample_config_dict):
    config = _make_config(tmp_path, sample_config_dict)
    _seed_index(config.index.db_path)

    result = search_notes("readonly_agent", "apple", config)[0]
    explanation = explain_ranking(result)

    assert explanation["source"] == "fts"
    assert explanation["source_scores"] == {"fts": result.rank}
    assert explanation["scoring"]["fts_rank"] == result.rank
    assert explanation["scoring"]["final_rank"] == result.rank


def test_stale_denylist_row_filtered_from_response(tmp_path, sample_config_dict, caplog, trace_assert):
    caplog.set_level(logging.DEBUG)
    config = _make_config(tmp_path, sample_config_dict)
    _seed_index(config.index.db_path)

    results = search_notes("readonly_agent", "apple", config)

    assert "private/secret-apples.md" not in {result.path for result in results}
    entries = _parsed_logs(caplog)
    assert trace_assert(entries, "retrieval.denied_path.filtered")


def test_fts_works_when_vector_backend_disabled(tmp_path, sample_config_dict):
    config = _make_config(tmp_path, sample_config_dict, vector_backend="disabled")
    _seed_index(config.index.db_path)

    results = search_notes("readonly_agent", "apple", config)

    assert [result.path for result in results] == ["memory/apples.md"]
    assert config.vector.backend == "disabled"


def test_disabled_vector_mode_search_still_returns_fts_results(tmp_path, sample_config_dict):
    config = _make_config(tmp_path, sample_config_dict, vector_backend="disabled")
    _seed_index(config.index.db_path)

    results = search_notes("readonly_agent", "banana", config)

    assert [result.path for result in results] == ["memory/bananas.md"]
    assert results[0].explanation["source"] == "fts"


def test_find_similar_notes_returns_empty_in_disabled_mode(tmp_path, sample_config_dict):
    config = _make_config(tmp_path, sample_config_dict, vector_backend="disabled")
    _seed_index(config.index.db_path)

    results = find_similar_notes("readonly_agent", "memory/apples.md", config)

    assert results == []


def test_find_similar_notes_uses_vector_results_when_available(monkeypatch, tmp_path, sample_config_dict):
    config = _make_config(tmp_path, sample_config_dict, vector_backend="sqlite_vec")
    _seed_index(config.index.db_path)

    monkeypatch.setattr("memory_mcp.retrieval_service.create_vector_adapter", lambda config: object())
    monkeypatch.setattr(
        "memory_mcp.retrieval_service.query_vectors",
        lambda adapter, query, limit: [
            VectorQueryResult(
                path="memory/apples.md",
                score=0.92,
                metadata={"snippet": "Similar apple note", "revision": "rev-apple"},
            )
        ],
    )

    results = find_similar_notes("readonly_agent", "memory/bananas.md", config)

    assert len(results) == 1
    assert results[0].path == "memory/apples.md"
    assert results[0].snippet == "Similar apple note"
    assert results[0].revision == "rev-apple"
    assert explain_ranking(results[0])["source_scores"] == {"vector": 0.92}


def test_vector_failure_degrades_to_fts(monkeypatch, tmp_path, sample_config_dict):
    config = _make_config(tmp_path, sample_config_dict, vector_backend="sqlite_vec")
    _seed_index(config.index.db_path)

    def raise_vector_error(config):
        raise RuntimeError("vector unavailable")

    monkeypatch.setattr("memory_mcp.retrieval_service.create_vector_adapter", raise_vector_error)

    results = search_notes("readonly_agent", "apple", config)

    assert [result.path for result in results] == ["memory/apples.md"]
    assert results[0].explanation["source"] == "fts"


def test_vector_empty_results_degrade_to_fts(monkeypatch, tmp_path, sample_config_dict):
    config = _make_config(tmp_path, sample_config_dict, vector_backend="sqlite_vec")
    _seed_index(config.index.db_path)

    monkeypatch.setattr("memory_mcp.retrieval_service.create_vector_adapter", lambda config: object())
    monkeypatch.setattr("memory_mcp.retrieval_service.query_vectors", lambda adapter, query, limit: [])

    results = search_notes("readonly_agent", "apple", config)

    assert [result.path for result in results] == ["memory/apples.md"]
    assert results[0].explanation["source"] == "fts"


def test_search_without_policy_filter_fails(tmp_path, sample_config_dict):
    config = _make_config(tmp_path, sample_config_dict)
    _seed_index(config.index.db_path)

    raw_results = search_fts(config.index.db_path, "apple")
    filtered_results = search_notes("readonly_agent", "apple", config)

    assert "private/secret-apples.md" in {result.path for result in raw_results}
    assert "private/secret-apples.md" not in {result.path for result in filtered_results}
