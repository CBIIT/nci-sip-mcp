"""Embedding provider abstraction.

The hashing provider is intentionally simple and deterministic so tests and
local smoke runs do not require model downloads. Production evaluation can use
SentenceTransformersProvider for SapBERT, MiniLM, or internal models.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, Sequence
from importlib import import_module
from pathlib import Path
from typing import Any

from .errors import IndexCompatibilityError


class EmbeddingProvider(ABC):
    name: str
    model: str
    # Commit, content hash or algorithm name of the model in use; None when unknown.
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
        resolved = _local_model(model_name, local_files_only)
        self.name = "sentence-transformers"
        self.model = model_name
        # Load from the path the revision is taken from, so the record names what was loaded.
        self._model = _sentence_transformer()(resolved, local_files_only=True)
        self.revision = _loaded_revision(model_name, resolved)

    def embed(self, texts: Iterable[str]) -> Sequence[Sequence[float]]:
        vectors = self._model.encode(
            list(texts), normalize_embeddings=True, show_progress_bar=False
        )
        # float32 rows go to storage as they are; vector_bytes packs them without Python floats.
        return list(import_module("numpy").asarray(vectors, dtype="<f4"))


def _sentence_transformer() -> Any:
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
    return SentenceTransformer


def _local_model(model_name: str, local_files_only: bool) -> str:
    """The one place a model name becomes a path: a directory as given, else the cached snapshot.

    A cached snapshot wins; updating a Hub model is an explicit cache refresh. Only when online
    and nothing is cached does the library's own load by name download it, once.
    """
    path = Path(model_name)
    if path.is_dir():
        return str(path)
    if path.is_absolute():
        raise FileNotFoundError("Supply the external model directory")
    from huggingface_hub import snapshot_download  # pyright: ignore[reportMissingImports]

    try:
        return snapshot_download(model_name, local_files_only=True)
    except FileNotFoundError:
        if local_files_only:
            raise
    _sentence_transformer()(model_name)  # downloads into the cache; the object is discarded
    return snapshot_download(model_name, local_files_only=True)


def _loaded_revision(model_name: str, resolved: str) -> str:
    """Identify what was loaded: a directory by its hash, a Hub snapshot by its commit."""
    if Path(model_name).is_dir():
        return model_directory_revision(Path(resolved))
    commit = Path(resolved).name
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise IndexCompatibilityError(
            f"The cached snapshot of model {model_name} is not a commit folder; "
            "refresh the model cache from the Hub."
        )
    return commit


def model_directory_revision(path: Path) -> str:
    """Identify a model directory by its content: `sha256:` plus a hash of sorted file lines.

    Each line is `<relative POSIX path>\\0<file SHA-256 hex>\\n`. A file link counts as its
    target's content under the link's own path, because Hub snapshot folders hold links into
    `blobs/`. Location, modification time and ordering on disk do not count; a changed byte, a
    rename or an added file does. Anything that cannot be hashed as a file is refused by path.
    """
    lines = sorted(
        f"{relative}\0{_file_hash(file, relative)}\n" for relative, file in _model_files(path)
    )
    return "sha256:" + hashlib.sha256("".join(lines).encode()).hexdigest()


def _model_files(root: Path) -> Iterable[tuple[str, Path]]:
    for current, directories, names in os.walk(root, onerror=_unlistable(root)):
        here = Path(current)
        if not directories and not names:
            raise IndexCompatibilityError(f"Model directory {_relative(here, root)} is empty")
        for name in directories:
            _require_real_directory(here / name, root)
        for name in names:
            yield _relative(here / name, root), _require_file(here / name, root)


def _unlistable(root: Path) -> Callable[[OSError], None]:
    """os.walk skips a directory it cannot list unless told otherwise; hashing must not."""

    def refuse(error: OSError) -> None:
        raise IndexCompatibilityError(
            f"Model directory entry {_relative(Path(str(error.filename)), root)} cannot be listed"
        ) from error

    return refuse


def _require_real_directory(path: Path, root: Path) -> None:
    if path.is_symlink():
        raise IndexCompatibilityError(
            f"Model directory entry {_relative(path, root)} is a directory link"
        )


def _require_file(path: Path, root: Path) -> Path:
    if not path.is_file():
        raise IndexCompatibilityError(
            f"Model directory entry {_relative(path, root)} is not a regular file"
        )
    return path


def _relative(path: Path, root: Path) -> str:
    return "." if path == root else path.relative_to(root).as_posix()


def _file_hash(path: Path, relative: str) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            while chunk := stream.read(1 << 20):
                digest.update(chunk)
    except OSError as error:
        raise IndexCompatibilityError(
            f"Model directory entry {relative} cannot be read: {error.strerror}"
        ) from error
    return digest.hexdigest()


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
