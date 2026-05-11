"""OpenAI-compatible embedding service — M-029 EmbeddingService."""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Any

import httpx

from memory_mcp.observability import log_trace_anchor, new_trace_id

logger = logging.getLogger(__name__)

MODULE = "embedding_service"
MODULE_BLOCK = "M-029"

MAX_RETRIES = 3
BASE_RETRY_DELAY = 1.0

@dataclass
class ChunkEmbedding:
    path: str
    chunk_id: str
    chunk_text: str
    embedding: list[float]
    revision: str


def _resolve_api_key(api_key_env: str) -> str:
    return os.environ.get(api_key_env, "")


def _redact_api_key(key: str) -> str:
    if len(key) <= 8:
        return "<redacted>"
    return f"{key[:4]}...{key[-4:]}"


def _build_headers(api_key: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }


def _build_request_body(texts: list[str], model: str, dimensions: int) -> dict[str, Any]:
    return {
        "input": texts,
        "model": model,
        "dimensions": dimensions,
    }


def _call_embedding_api(
    texts: list[str],
    api_base: str,
    api_key: str,
    model: str,
    dimensions: int,
) -> list[list[float]]:
    url = f"{api_base.rstrip('/')}/embeddings"
    headers = _build_headers(api_key)
    body = _build_request_body(texts, model, dimensions)

    last_error: Exception | None = None
    for attempt in range(MAX_RETRIES):
        try:
            response = httpx.post(url, headers=headers, json=body, timeout=30.0)
            response.raise_for_status()
            data = response.json()
            data_embeddings = data.get("data", [])
            sorted_embeddings = sorted(data_embeddings, key=lambda x: x.get("index", 0))
            return [item["embedding"] for item in sorted_embeddings]
        except httpx.HTTPStatusError as exc:
            last_error = exc
            if attempt < MAX_RETRIES - 1:
                delay = BASE_RETRY_DELAY * (2 ** attempt)
                time.sleep(delay)
        except (httpx.RequestError, KeyError, ValueError) as exc:
            last_error = exc
            if attempt < MAX_RETRIES - 1:
                delay = BASE_RETRY_DELAY * (2 ** attempt)
                time.sleep(delay)

    raise RuntimeError(f"Embedding API call failed after {MAX_RETRIES} attempts") from last_error


def embed_text(text: str, api_base: str, api_key_env: str, model: str, dimensions: int) -> list[float]:
    trace_id = new_trace_id()
    api_key = _resolve_api_key(api_key_env)

    if not api_key:
        log_trace_anchor(
            level="ERROR",
            event="embedding.failed",
            trace_id=trace_id,
            module=MODULE,
            function="embed_text",
            block=MODULE_BLOCK,
            data={"reason": "api_key_not_found", "api_key_env": api_key_env},
        )
        return []

    try:
        result = _call_embedding_api([text], api_base, api_key, model, dimensions)
    except RuntimeError:
        log_trace_anchor(
            level="ERROR",
            event="embedding.failed",
            trace_id=trace_id,
            module=MODULE,
            function="embed_text",
            block=MODULE_BLOCK,
            data={"reason": "api_error_after_retries"},
        )
        return []

    log_trace_anchor(
        level="INFO",
        event="embedding.generated",
        trace_id=trace_id,
        module=MODULE,
        function="embed_text",
        block=MODULE_BLOCK,
        data={
            "dimensions": dimensions,
            "model": model,
            "api_key_fingerprint": _redact_api_key(api_key),
        },
    )

    return result[0] if result else []


def embed_batch(
    texts: list[str],
    api_base: str,
    api_key_env: str,
    model: str,
    dimensions: int,
    batch_size: int = 100,
) -> list[list[float]]:
    trace_id = new_trace_id()
    api_key = _resolve_api_key(api_key_env)

    if not api_key:
        log_trace_anchor(
            level="ERROR",
            event="embedding.failed",
            trace_id=trace_id,
            module=MODULE,
            function="embed_batch",
            block=MODULE_BLOCK,
            data={"reason": "api_key_not_found", "api_key_env": api_key_env},
        )
        return []

    all_embeddings: list[list[float]] = []
    for i in range(0, len(texts), batch_size):
        batch = texts[i:i + batch_size]
        try:
            batch_results = _call_embedding_api(batch, api_base, api_key, model, dimensions)
            all_embeddings.extend(batch_results)
        except RuntimeError:
            for _text in batch:
                all_embeddings.append([])

    log_trace_anchor(
        level="INFO",
        event="embedding.batch.completed",
        trace_id=trace_id,
        module=MODULE,
        function="embed_batch",
        block=MODULE_BLOCK,
        data={
            "total_texts": len(texts),
            "returned": len(all_embeddings),
            "dimensions": dimensions,
            "model": model,
            "batch_size": batch_size,
            "api_key_fingerprint": _redact_api_key(api_key),
        },
    )

    return all_embeddings


def _embed_chunks_impl(
    chunks: list[dict[str, Any]],
    api_base: str,
    api_key_env: str,
    model: str,
    dimensions: int,
    batch_size: int,
) -> list[ChunkEmbedding]:
    texts = [c["text"] for c in chunks]
    embeddings = embed_batch(texts, api_base, api_key_env, model, dimensions, batch_size)

    results: list[ChunkEmbedding] = []
    for chunk, emb in zip(chunks, embeddings):
        results.append(ChunkEmbedding(
            path=chunk["path"],
            chunk_id=chunk["chunk_id"],
            chunk_text=chunk["text"],
            embedding=emb,
            revision=chunk.get("revision", ""),
        ))
    return results


def embed_chunks(
    chunks: list[dict[str, Any]],
    api_base: str,
    api_key_env: str,
    model: str,
    dimensions: int,
    batch_size: int = 100,
) -> list[ChunkEmbedding]:
    return _embed_chunks_impl(chunks, api_base, api_key_env, model, dimensions, batch_size)
