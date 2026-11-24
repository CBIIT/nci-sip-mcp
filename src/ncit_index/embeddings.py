"""Embedding provider abstraction.

The hashing provider is intentionally simple and deterministic so tests and
local smoke runs do not require model downloads. Production evaluation can use
SentenceTransformersProvider for SapBERT, MiniLM, or internal models.
"""

from __future__ import annotations

import hashlib
import math
import re
from abc import ABC, abstractmethod
from collections.abc import Iterable, Sequence
from importlib import import_module
from pathlib import Path

from .errors import IndexCompatibilityError


class EmbeddingProvider(ABC):
    name: str
    model: str
    # The commit of the model files in use; None when a provider cannot tell.
    revision: str | None = None

    @abstractmethod
    def embed(self, texts: Iterable[str]) -> Sequence[Sequence[float]]:
        """Return one vector per text, all of one length."""


class HashingEmbeddingProvider(EmbeddingProvider):
    # Names the algorithm of `_embed_one`; change it whenever those vectors change.
    revision = "hashing-v1"

    def __init__(self, dimensions: int = 128) -> None:
        if dimensions < 1:
            raise ValueError("Hashing embedding dimensions must be at least 1")
        self.name = "hashing"
        self.model = f"hashing-{dimensions}"
        self.dimensions = dimensions

    def embed(self, texts: Iterable[str]) -> Sequence[Sequence[float]]:
        return [self._embed_one(text) for text in texts]

    def _embed_one(self, text: str) -> list[float]:
        vector = [0.0] * self.dimensions
        for token in text.lower().split():
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            idx = int.from_bytes(digest[:4], "big") % self.dimensions
            sign = -1.0 if digest[4] % 2 else 1.0
            vector[idx] += sign
        norm = math.sqrt(sum(value * value for value in vector))
        if norm:
            vector = [value / norm for value in vector]
        return vector


class SentenceTransformersProvider(EmbeddingProvider):
    def __init__(self, model_name: str, *, local_files_only: bool = False) -> None:
        resolved_name = _local_model(model_name) if local_files_only else model_name
        # An optional extra with heavy dependencies, imported only when configured.
        try:
            from sentence_transformers import (  # pyright: ignore[reportMissingImports]
                SentenceTransformer,
            )
        except ImportError as exc:
            raise RuntimeError(
                "The sentence-transformers provider needs the 'embeddings' extra "
                f"(pdm install -G embeddings). Import failed: {exc}"
            ) from exc
        self.name = "sentence-transformers"
        self.model = model_name
        self._model = (
            SentenceTransformer(resolved_name, local_files_only=True)
            if local_files_only
            else SentenceTransformer(model_name)
        )
        self.revision = _cache_revision(model_name)

    def embed(self, texts: Iterable[str]) -> Sequence[Sequence[float]]:
        vectors = self._model.encode(
            list(texts), normalize_embeddings=True, show_progress_bar=False
        )
        # float32 rows go to storage as they are; vector_bytes packs them without Python floats.
        return list(import_module("numpy").asarray(vectors, dtype="<f4"))


def _local_model(model_name: str) -> str:
    """Check supplied assets before the embedding library emits startup diagnostics."""
    path = Path(model_name)
    if path.is_dir():
        return str(path)
    if path.is_absolute():
        raise FileNotFoundError("Supply the external model directory")
    from huggingface_hub import snapshot_download  # pyright: ignore[reportMissingImports]

    return snapshot_download(model_name, local_files_only=True)


def _cache_revision(model_name: str) -> str | None:
    """The commit of a Hub model's cached snapshot, read from the local cache without traffic.

    A model supplied as a directory is not identified by its path, so it has no revision.
    """
    if Path(model_name).is_dir():
        return None
    from huggingface_hub import snapshot_download  # pyright: ignore[reportMissingImports]

    commit = Path(snapshot_download(model_name, local_files_only=True)).name
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise IndexCompatibilityError(
            f"The cached snapshot of model {model_name} is not a commit folder; "
            "refresh the model cache from the Hub."
        )
    return commit


def normalize_embedding_settings(provider: str, model: str) -> tuple[str, str]:
    """Return the normalized provider and model, or raise if they do not go together."""

    normalized_provider = provider.strip().lower()
    normalized_model = model.strip()
    if normalized_provider == "hashing":
        if normalized_model not in {"hashing", "hashing-128"}:
            raise ValueError(
                "NCI_SIP_EMBEDDING_MODEL must be 'hashing' when "
                "NCI_SIP_EMBEDDING_PROVIDER is 'hashing'"
            )
    elif normalized_provider == "sentence-transformers":
        if not normalized_model or normalized_model == "hashing":
            raise ValueError(
                "NCI_SIP_EMBEDDING_MODEL must name a model when NCI_SIP_EMBEDDING_PROVIDER "
                "is 'sentence-transformers'"
            )
    else:
        raise ValueError(
            "NCI_SIP_EMBEDDING_PROVIDER must be 'hashing' or 'sentence-transformers', "
            f"not {provider!r}"
        )
    return normalized_provider, normalized_model


def create_embedding_provider(provider: str, model: str) -> EmbeddingProvider:
    provider, model = normalize_embedding_settings(provider, model)
    if provider == "hashing":
        return HashingEmbeddingProvider()
    return SentenceTransformersProvider(model)
