"""Module-local tests for M-016 VectorSearchAdapter."""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace

import pytest

from memory_mcp.config import load_config
from memory_mcp.vector_adapter import (
    NoopVectorAdapter,
    SqliteVecVectorAdapter,
    create_vector_adapter,
    query_vectors,
    upsert_vectors,
)


def _parsed_logs(caplog) -> list[dict]:
    entries: list[dict] = []
    for record in caplog.records:
        try:
            entries.append(json.loads(record.message))
        except (json.JSONDecodeError, TypeError):
            pass
    return entries


def _make_config(sample_config_dict: dict, backend: str):
    config_dict = {**sample_config_dict, "vector": {"backend": backend}}
    return load_config(config_dict)


def test_disabled_mode_has_no_external_dependency(monkeypatch, caplog, trace_assert):
    caplog.set_level(logging.DEBUG)

    def fail_import(module_name: str):
        raise AssertionError(f"disabled mode imported optional dependency: {module_name}")

    monkeypatch.setattr("memory_mcp.vector_adapter.importlib.import_module", fail_import)

    adapter = create_vector_adapter("disabled")
    upsert_vectors(adapter, [{"path": "memory/a.md", "vector": [1.0]}])
    results = query_vectors(adapter, [1.0], limit=2)

    assert isinstance(adapter, NoopVectorAdapter)
    assert results == []
    entries = _parsed_logs(caplog)
    assert trace_assert(entries, "vector.mode.selected")
    selected = next(entry for entry in entries if entry.get("event") == "vector.mode.selected")
    assert selected["data"] == {"requested_mode": "disabled", "selected_mode": "disabled"}


def test_sqlite_vec_mode_enabled_when_available(monkeypatch, sample_config_dict, caplog, trace_assert):
    caplog.set_level(logging.DEBUG)
    fake_sqlite_vec = SimpleNamespace(__name__="sqlite_vec")

    def fake_import(module_name: str):
        if module_name == "sqlite_vec":
            return fake_sqlite_vec
        raise ImportError(module_name)

    def fake_query_similar(db_path, embedding, limit=5):
        return []

    monkeypatch.setattr("memory_mcp.vector_adapter.importlib.import_module", fake_import)
    monkeypatch.setattr("memory_mcp.index_repo.query_similar", fake_query_similar)
    config = _make_config(sample_config_dict, "sqlite_vec")

    adapter = create_vector_adapter(config=config)

    assert isinstance(adapter, SqliteVecVectorAdapter)
    assert query_vectors(adapter, "semantic query") == []
    entries = _parsed_logs(caplog)
    assert trace_assert(entries, "vector.mode.selected")
    assert not any(entry.get("event") == "vector.degraded_to_fts" for entry in entries)


@pytest.mark.parametrize("mode", ["sqlite_vec", "qdrant_optional"])
def test_vector_failure_degrading_to_fts(monkeypatch, mode, caplog, trace_assert):
    caplog.set_level(logging.DEBUG)

    def missing_optional_dependency(module_name: str):
        raise ImportError(module_name)

    monkeypatch.setattr("memory_mcp.vector_adapter.importlib.import_module", missing_optional_dependency)

    adapter = create_vector_adapter(mode)
    results = query_vectors(adapter, [0.1, 0.2, 0.3], limit=5)

    assert isinstance(adapter, NoopVectorAdapter)
    assert results == []
    entries = _parsed_logs(caplog)
    assert trace_assert(entries, "vector.degraded_to_fts", "vector.mode.selected")
    degraded = next(entry for entry in entries if entry.get("event") == "vector.degraded_to_fts")
    assert degraded["data"]["requested_mode"] == mode


def test_unsupported_mode_rejected():
    with pytest.raises(ValueError, match="Unsupported vector adapter mode"):
        create_vector_adapter("unsupported")


def test_sqlite_vec_upsert_delegates_to_index_repo(monkeypatch, tmp_path):
    upsert_calls = []

    def fake_upsert(db_path, path, chunks):
        upsert_calls.append((db_path, path, chunks))

    monkeypatch.setattr("memory_mcp.index_repo.upsert_note_chunks", fake_upsert)

    adapter = SqliteVecVectorAdapter(sqlite_vec=SimpleNamespace(__name__="sqlite_vec"), db_path=str(tmp_path / "test.db"))
    items = [{"path": "memory/a.md", "chunk_id": "h1", "text": "test", "revision": "r1", "embedding": [0.1] * 1536}]
    upsert_vectors(adapter, items)

    assert len(upsert_calls) == 1
    assert upsert_calls[0][1] == "memory/a.md"
    assert len(upsert_calls[0][2]) == 1


def test_sqlite_vec_query_delegates_to_index_repo(monkeypatch, tmp_path):
    query_calls = []

    def fake_query(db_path, embedding, limit):
        query_calls.append((db_path, embedding, limit))
        return []
    monkeypatch.setattr("memory_mcp.index_repo.query_similar", fake_query)

    adapter = SqliteVecVectorAdapter(sqlite_vec=SimpleNamespace(__name__="sqlite_vec"), db_path=str(tmp_path / "test.db"))
    query_vectors(adapter, [0.1] * 1536, limit=5)

    assert len(query_calls) == 1
    assert query_calls[0][2] == 5


def test_sqlite_vec_noop_without_db_path(monkeypatch):
    adapter = SqliteVecVectorAdapter(sqlite_vec=SimpleNamespace(__name__="sqlite_vec"), db_path="")
    upsert_vectors(adapter, [{"path": "memory/a.md"}])
    results = query_vectors(adapter, [0.1] * 1536, limit=5)
    assert results == []
