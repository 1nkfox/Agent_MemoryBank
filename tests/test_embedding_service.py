"""Module-local tests for M-029 EmbeddingService."""

from __future__ import annotations

import json
import logging
import os

import pytest

from memory_mcp.embedding_service import (
    ChunkEmbedding,
    embed_text,
    embed_batch,
    embed_chunks,
)


def _parsed_logs(caplog) -> list[dict]:
    entries: list[dict] = []
    for record in caplog.records:
        try:
            entries.append(json.loads(record.message))
        except (json.JSONDecodeError, TypeError):
            pass
    return entries


def test_embed_text_returns_empty_on_missing_api_key(monkeypatch, caplog, trace_assert):
    caplog.set_level(logging.DEBUG)
    monkeypatch.delenv("TEST_EMBED_KEY", raising=False)

    result = embed_text("hello world", "http://localhost:9999", "TEST_EMBED_KEY", "text-embedding-3-small", 1536)

    assert result == []
    entries = _parsed_logs(caplog)
    trace_assert(entries, "embedding.failed")


def test_embed_text_with_mock_server(monkeypatch, caplog, trace_assert):
    caplog.set_level(logging.DEBUG)
    monkeypatch.setenv("TEST_EMBED_KEY", "sk-test-key-12345")

    call_count = 0

    class FakeResponse:
        def __init__(self, status_code=200):
            self.status_code = status_code

        def raise_for_status(self):
            if self.status_code >= 400:
                raise Exception(f"HTTP {self.status_code}")

        def json(self):
            return {
                "object": "list",
                "data": [
                    {"object": "embedding", "index": 0, "embedding": [0.1] * 1536},
                ],
                "model": "text-embedding-3-small",
                "usage": {"prompt_tokens": 2, "total_tokens": 2},
            }

    original_post = None
    import httpx

    def fake_post(url, headers=None, json=None, timeout=None):
        nonlocal call_count
        call_count += 1
        assert "Authorization" in headers
        assert headers["Authorization"] == "Bearer sk-test-key-12345"
        assert json["input"] == ["hello world"]
        assert json["model"] == "text-embedding-3-small"
        assert json["dimensions"] == 1536
        return FakeResponse(200)

    monkeypatch.setattr(httpx, "post", fake_post)

    result = embed_text("hello world", "http://localhost:9999", "TEST_EMBED_KEY", "text-embedding-3-small", 1536)

    assert len(result) == 1536
    assert all(v == 0.1 for v in result)
    assert call_count == 1
    entries = _parsed_logs(caplog)
    trace_assert(entries, "embedding.generated")

    generated = next(e for e in entries if e.get("event") == "embedding.generated")
    assert generated["data"]["dimensions"] == 1536
    assert generated["data"]["model"] == "text-embedding-3-small"
    assert "sk-t" in generated["data"]["api_key_fingerprint"]
    assert "2345" in generated["data"]["api_key_fingerprint"]
    assert "sk-test-key-12345" not in generated["data"]["api_key_fingerprint"]


def test_embed_batch_returns_correct_count(monkeypatch, caplog, trace_assert):
    caplog.set_level(logging.DEBUG)
    monkeypatch.setenv("TEST_EMBED_KEY", "sk-test-key-67890")

    call_count = 0

    class FakeResponse:
        def __init__(self, status_code=200):
            self.status_code = status_code

        def raise_for_status(self):
            if self.status_code >= 400:
                raise Exception(f"HTTP {self.status_code}")

        def json(self):
            nonlocal call_count
            data = [
                {"object": "embedding", "index": i, "embedding": [float(i)] * 1536}
                for i in range(2 if call_count == 1 else 1)
            ]
            call_count += 1
            return {
                "object": "list",
                "data": data,
                "model": "text-embedding-3-small",
                "usage": {"prompt_tokens": 4, "total_tokens": 4},
            }

    import httpx

    monkeypatch.setattr(httpx, "post", lambda url, headers=None, json=None, timeout=None: FakeResponse())

    results = embed_batch(
        ["text a", "text b", "text c"],
        "http://localhost:9999",
        "TEST_EMBED_KEY",
        "text-embedding-3-small",
        1536,
        batch_size=2,
    )

    assert len(results) == 3
    entries = _parsed_logs(caplog)
    trace_assert(entries, "embedding.batch.completed")


def test_embed_chunks_returns_chunk_embeddings(monkeypatch, caplog, trace_assert):
    caplog.set_level(logging.DEBUG)
    monkeypatch.setenv("TEST_EMBED_KEY", "sk-test-key-chunks")

    chunks = [
        {"path": "memory/a.md", "chunk_id": "h1_intro", "text": "Introduction text", "revision": "abc123"},
        {"path": "memory/a.md", "chunk_id": "h2_detail", "text": "Detail section text", "revision": "abc123"},
    ]

    import httpx

    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            data_count = len(self.json_input)
            return {
                "object": "list",
                "data": [
                    {"object": "embedding", "index": i, "embedding": [float(i + 1)] * 1536}
                    for i in range(data_count)
                ],
                "model": "text-embedding-3-small",
            }

        def __init__(self, json_input=None):
            self.json_input = json_input or []

    def fake_post(url, headers=None, json=None, timeout=None):
        resp = FakeResponse(json.get("input", []))
        resp.json_input = json.get("input", [])
        return resp

    monkeypatch.setattr(httpx, "post", fake_post)

    results = embed_chunks(chunks, "http://localhost:9999", "TEST_EMBED_KEY", "text-embedding-3-small", 1536, batch_size=10)

    assert len(results) == 2
    assert isinstance(results[0], ChunkEmbedding)
    assert results[0].path == "memory/a.md"
    assert results[0].chunk_id == "h1_intro"
    assert results[0].chunk_text == "Introduction text"
    assert results[0].revision == "abc123"
    assert len(results[0].embedding) == 1536
    assert results[0].embedding[0] == 1.0

    assert results[1].chunk_id == "h2_detail"
    assert results[1].embedding[0] == 2.0


def test_embed_api_error_returns_empty(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    monkeypatch.setenv("TEST_EMBED_KEY", "sk-test-key-error")

    import httpx

    call_count = 0

    def fake_post(url, headers=None, json=None, timeout=None):
        nonlocal call_count
        call_count += 1
        raise httpx.HTTPStatusError("500 Server Error", request=None, response=None)

    monkeypatch.setattr(httpx, "post", fake_post)

    result = embed_text("hello", "http://localhost:9999", "TEST_EMBED_KEY", "text-embedding-3-small", 1536)

    assert result == []
    assert call_count == 3  # retried 3 times
