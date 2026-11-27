"""The read-side index port and its single local adapter."""

import logging
from typing import Any

from ncit_index.embeddings import EmbeddingProvider
from ncit_index.index import LocalIndex
from ncit_index.records import IndexManifest as StoredManifest

from .audit import emit
from .models import IndexManifest, NcitConcept, SearchHit, Truncation
from .validation import MAX_INDEX_SEARCH_LIMIT, validate_search

# Events an operator must act on, not just read.
_WARNING_EVENTS = frozenset(
    {
        "index_build_removal_pending",
        "index_model_revision_mismatch",
        "index_model_revision_unknown",
    }
)


def index_event(name: str, details: dict[str, Any]) -> None:
    """Preserve the existing audit logger and event fields for local operations."""
    level = logging.WARNING if name in _WARNING_EVENTS else logging.INFO
    emit(logging.getLogger("nci_sip_mcp.index"), level, name, **details)


def manifest_record(manifest: StoredManifest) -> IndexManifest:
    return IndexManifest(**manifest.to_dict())


def results_truncation(scored: int, limit: int) -> Truncation:
    if scored <= limit:
        return Truncation(occurred=False)
    return Truncation(
        occurred=True,
        bound="results",
        limit=limit,
        reached=limit,
        omitted=scored - limit,
        exact=True,
    )


class LocalIndexAdapter:
    def __init__(self, index: LocalIndex) -> None:
        self.local = index

    def get_active_manifest(self) -> IndexManifest | None:
        manifest = self.local.get_active_manifest()
        return manifest_record(manifest) if manifest else None

    def verify_active(self, provider: EmbeddingProvider) -> IndexManifest:
        return manifest_record(self.local.verify_active(provider))

    def get_concept_snapshot(self, code: str) -> tuple[IndexManifest | None, NcitConcept | None]:
        manifest, concept = self.local.get_concept_snapshot(code)
        return (
            manifest_record(manifest) if manifest else None,
            NcitConcept(**concept.to_stored()) if concept else None,
        )

    def search_page(
        self,
        query: str,
        embedding_provider: EmbeddingProvider,
        limit: int = 10,
        mode: str = "hybrid",
        *,
        requested_release: str | None = None,
        offset: int = 0,
        build_id: str | None = None,
        retired_status: str | None = None,
    ) -> tuple[list[SearchHit], int, IndexManifest]:
        query, limit, mode = validate_search(query, limit, mode, maximum=MAX_INDEX_SEARCH_LIMIT)
        hits, total, manifest = self.local.search_page(
            query,
            embedding_provider,
            limit,
            mode,
            requested_release=requested_release,
            offset=offset,
            build_id=build_id,
            retired_status=retired_status,
        )
        return (
            [
                SearchHit(
                    NcitConcept(**hit.concept.to_stored()),
                    hit.score,
                    hit.rank,
                    hit.matched_on,
                    hit.score_components,
                )
                for hit in hits
            ],
            total,
            manifest_record(manifest),
        )
