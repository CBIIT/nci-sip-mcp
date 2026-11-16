"""Serve externally prepared assets; never download, build or activate them."""

from __future__ import annotations

import logging
import re

from ncit_index.embeddings import (
    HashingEmbeddingProvider,
    SentenceTransformersProvider,
    create_embedding_provider,
)
from ncit_index.errors import IndexCapabilityError, IndexCompatibilityError, NoActiveIndexError
from ncit_index.index import LocalIndex

from .audit import emit
from .config import Settings, configure_logging, require_local_stdio_access
from .context import Context
from .errors import PlatformError
from .index_adapter import LocalIndexAdapter
from .server import create_mcp
from .transport import run_http

logger = logging.getLogger(__name__)


def startup_failure(asset: str, error: Exception) -> None:
    # Library exceptions can contain paths, URLs or secrets; retain only a setting's name.
    configure_logging("ERROR")
    logger.setLevel(logging.ERROR)  # A fatal startup diagnostic survives CRITICAL verbosity.
    parameter = re.search(r"NCI_SI_[A-Z_]+", str(error)) if asset == "settings" else None
    emit(
        logger,
        logging.ERROR,
        "container_startup_failed",
        asset=asset,
        errorType="PlatformError"
        if isinstance(error, IndexCapabilityError)
        else type(error).__name__,
        parameter=parameter.group() if parameter else None,
    )


def prepared_index(settings: Settings) -> LocalIndexAdapter:
    if settings.http_require_index and not (settings.data_dir / "nci_si.sqlite3").is_file():
        raise NoActiveIndexError("Supply the index in NCI_SI_DATA_DIR")
    index = LocalIndexAdapter(LocalIndex(settings.data_dir))
    manifest = index.get_active_manifest()
    if manifest is None:
        if settings.http_require_index:
            raise NoActiveIndexError("Supply a completed active index")
        return index
    # Checked by name before the model loads: an image holds no cache for a wrong model, and
    # that failure would otherwise be reported as the model's rather than the index's.
    model = (
        HashingEmbeddingProvider().model
        if settings.embedding_provider == "hashing"
        else settings.embedding_model
    )
    if not manifest.embedding_matches(settings.embedding_provider, model):
        raise IndexCompatibilityError("Configured model differs from the index manifest")
    return index


def main() -> int:
    asset = "settings"
    try:
        settings = Settings.from_env()
        require_local_stdio_access(settings)
        configure_logging(settings.log_level)
        asset = "index"
        index = prepared_index(settings)
        asset = "model"
        provider = (
            SentenceTransformersProvider(settings.embedding_model, local_files_only=True)
            if settings.embedding_provider == "sentence-transformers"
            else create_embedding_provider(settings.embedding_provider, settings.embedding_model)
        )
        asset = "index"
        if settings.http_require_index or index.get_active_manifest() is not None:
            index.verify_active(provider)
    except (RuntimeError, ValueError, OSError, PlatformError) as exc:
        startup_failure(asset, exc)
        return 1
    context = Context(settings, index=index, embedding_provider=provider)
    if settings.transport == "streamable-http":
        run_http(settings, context)
    else:
        create_mcp(settings, context=context).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
