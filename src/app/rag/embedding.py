"""Turn text into embedding vectors, batched, behind one interface.

The same embedder must index the corpus and embed a query, so both go through
get_embedder(). The dev default is a deterministic hashing embedder that needs no
external service; a real provider slots in as another branch of the factory
without touching the storage or retrieval code.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from app.config import get_settings
from app.db.models.rag import EMBEDDING_DIM

_WORD = re.compile(r"[a-z0-9]+")


@runtime_checkable
class Embedder(Protocol):
    model: str
    dim: int

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


class HashingEmbedder:
    """Deterministic, dependency-free embedder for dev and tests.

    Tokens are hashed into fixed buckets and the vector is L2-normalized, so the
    same text always embeds identically and texts sharing words sit closer.
    """

    def __init__(self, dim: int = EMBEDDING_DIM, model: str = "dev-hashing") -> None:
        self.dim = dim
        self.model = model

    def _one(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for token in _WORD.findall(text.lower()):
            bucket = int.from_bytes(hashlib.blake2b(token.encode(), digest_size=8).digest(), "big")
            vec[bucket % self.dim] += 1.0
        norm = math.sqrt(sum(v * v for v in vec))
        return [v / norm for v in vec] if norm else vec

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._one(t) for t in texts]


def get_embedder(model: str | None = None) -> Embedder:
    """Build the configured embedder. The one place a provider is chosen."""
    settings = get_settings()
    provider = settings.embedding_provider
    if provider == "hashing":
        return HashingEmbedder(model=model or settings.embedding_model)
    raise ValueError(f"unknown embedding provider: {provider!r}")


def embed_batched(embedder: Embedder, texts: Sequence[str], batch_size: int) -> list[list[float]]:
    """Embed in fixed-size batches so a large corpus stays within provider limits."""
    vectors: list[list[float]] = []
    for start in range(0, len(texts), max(1, batch_size)):
        vectors.extend(embedder.embed(list(texts[start : start + batch_size])))
    return vectors
