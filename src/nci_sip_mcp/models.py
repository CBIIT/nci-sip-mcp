"""Shared data models.

The MCP layer returns dictionaries produced from these dataclasses. Keeping the
core models dependency-free makes the indexing and EVS behavior easy to test
without importing the optional `mcp` package.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from datetime import UTC, datetime
from typing import Any

from ncit_index.records import Concept as StoredConcept
from ncit_index.records import IndexManifest as StoredManifest

from .errors import PlatformError, call_correlation_id
from .validation import Polarity


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True, slots=True, kw_only=True)
class ProvenanceEnvelope:
    """The provenance record of one returned item (A4.4), field for field.

    Index items carry neither `source_uri` nor `upstream`. Live answers retain their source
    facts when cached; `upstream` describes the item or the resource actually read, even when
    a filter leaves no items, and is absent when the answer supplied no origin (A4.3).
    """

    release: dict[str, str]
    source: str
    served_by: str
    retrieved_at: str
    correlation_id: str
    source_uri: str | None = None
    upstream: dict[str, Any] | None = None
    attribution: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "release": self.release,
            "source": self.source,
            "servedBy": self.served_by,
            "retrievedAt": self.retrieved_at,
            "correlationId": self.correlation_id,
        }
        if self.source_uri:
            data["sourceUri"] = self.source_uri
        if self.upstream:
            data["upstream"] = self.upstream
        if self.attribution:
            data["attribution"] = self.attribution
        return data


def live_provenance(
    release: dict[str, str],
    source: str,
    *,
    uri: str | None = None,
    upstream: dict[str, Any] | None = None,
    attribution: str | None = None,
) -> dict[str, Any]:
    """The provenance record of an item read live from upstream in this call (A4.4).

    The retrieval time and correlation id are the server's own; everything else is carried
    forward from the call or from what upstream supplied, and a missing one stays missing.
    """

    return ProvenanceEnvelope(
        release=release,
        source=source,
        served_by="live",
        retrieved_at=utc_now_iso(),
        correlation_id=call_correlation_id(),
        source_uri=uri,
        upstream=upstream,
        attribution=attribution,
    ).to_dict()


def attribution_of(raw: dict[str, Any], surface: str, key: str = "licenseText") -> str | None:
    """The licence or attribution text upstream sent with an item (A7.3), or None.

    The text is passed through as sent: the server keeps no licence text of its own, so an item
    upstream sent none for has none. Text of another type is a malformed response, not coerced.
    """

    text = raw.get(key)
    if text is not None and not isinstance(text, str):
        raise PlatformError(
            "upstream_unavailable",
            f"The {surface} licence text is malformed. Ask the provider to correct it.",
            surface=surface,
        )
    return text or None


def with_attribution(
    provenance: dict[str, Any], raw: dict[str, Any], surface: str, key: str = "licenseText"
) -> dict[str, Any]:
    """A provenance record shared by several items, with the licence text this item carries."""

    text = attribution_of(raw, surface, key)
    return provenance | {"attribution": text} if text else provenance


@dataclass(frozen=True, slots=True, kw_only=True)
class TraversalProvenance(ProvenanceEnvelope):
    """The provenance of an item reached by traversal (A4.2).

    The concept asked about has depth 0 and no relationship, direction or polarity.
    """

    depth: int
    relationship: dict[str, str] | None = None
    direction: str | None = None
    polarity: Polarity | None = None
    qualifiers: Any = None
    evidence: Any = None

    def to_dict(self) -> dict[str, Any]:
        data = super().to_dict()
        data["depth"] = self.depth
        how_reached = {
            "relationship": self.relationship,
            "direction": self.direction,
            "polarity": self.polarity,
            "qualifiers": self.qualifiers,
            "evidence": self.evidence,
        }
        return data | {name: value for name, value in how_reached.items() if value is not None}


@dataclass(frozen=True, slots=True)
class Truncation:
    """The truncation record of a bounded result (A5.4).

    When a bound was reached `omitted` is always a number, and `exact` is False where it is
    only a lower bound or an estimate.
    """

    occurred: bool
    bound: str | None = None
    limit: int | None = None
    reached: int | None = None
    omitted: int | None = None
    exact: bool | None = None
    per_kind: dict[str, Truncation] | None = None

    def to_dict(self) -> dict[str, Any]:
        data = {
            "occurred": self.occurred,
            "bound": self.bound,
            "limit": self.limit,
            "reached": self.reached,
            "omitted": self.omitted,
            "exact": self.exact,
        }
        if self.per_kind is not None:
            data["perKind"] = {kind: record.to_dict() for kind, record in self.per_kind.items()}
        return {key: value for key, value in data.items() if value is not None}


def results_cut(
    count: int, limit: int, *, exact: bool = False, skipped: bool = False
) -> dict[str, Any]:
    """The truncation record of a list cut at `limit` results (A5.4); `skipped` marks a cut even
    when nothing is left over, for a list that was itself assembled from a bounded read."""

    if count <= limit and not skipped:
        return {"occurred": False}
    return Truncation(
        occurred=True,
        bound="results",
        limit=limit,
        reached=limit,
        omitted=max(0, count - limit),
        exact=exact,
    ).to_dict()


# The surface and the way of serving that each stored `source` stands for.
_ORIGINS = {"live_evs": ("evs_rest", "live"), "active_cache": ("evs_index", "index")}


def release_ref(terminology: str, identifier: str, date: str | None) -> dict[str, str]:
    """The terminology form of the provenance record's release; a date EVS gave none is left out."""

    ref = {"terminology": terminology, "identifier": identifier}
    return ref | {"date": date} if date else ref


def upstream_origin(raw: dict[str, Any]) -> dict[str, Any]:
    """What EVS REST says of an item's origin, under its own names: its terminology and version."""

    return {key: raw[key] for key in ("terminology", "version") if key in raw}


# NcitConcept and IndexManifest are stored as JSON in the index. A new field
# needs a default, or a schema migration that rewrites the stored payloads.
@dataclass(frozen=True, slots=True)
class NcitConcept(StoredConcept):
    def provenance(self, source_uri: str | None) -> ProvenanceEnvelope:
        surface, served_by = _ORIGINS[self.source]
        return ProvenanceEnvelope(
            release=release_ref(self.terminology, self.release_version, self.release_date),
            source=surface,
            served_by=served_by,
            retrieved_at=self.retrieved_at,
            correlation_id=call_correlation_id(),
            source_uri=source_uri if served_by != "index" else None,
            upstream=upstream_origin(self.raw) if served_by != "index" else None,
        )

    def to_dict(self, source_uri: str | None, include_raw: bool = False) -> dict[str, Any]:
        """The concept as an item of a result: its provenance replaces the release fields.

        The full EVS payload is added only when `include_raw` asks for it, which the CLI does.
        """

        data = {
            "code": self.code,
            "preferred_name": self.preferred_name,
            "source_vocabulary": self.source_vocabulary,
            "terminology": self.terminology,
            "evidence": self.evidence,
            "provenance": self.provenance(source_uri).to_dict(),
        }
        return data | {"raw": self.raw} if include_raw else data


@dataclass(frozen=True, slots=True)
class IndexManifest(StoredManifest):
    def to_result(self) -> dict[str, Any]:
        """The manifest as the index_manifest record: its fields and its provenance."""

        return {
            "terminology": self.terminology,
            "version": self.release_version,
            "concepts": self.concept_count,
            "embedding": {
                "provider": self.embedding_provider,
                "model": self.embedding_model,
                "dimensions": self.embedding_dimensions,
            },
            "builtAt": self.built_at,
            "provenance": self.provenance().to_dict(),
        }

    def provenance(self) -> ProvenanceEnvelope:
        """The provenance of what the index serves: the release it holds, as built."""

        return ProvenanceEnvelope(
            release=release_ref(self.terminology, self.release_version, self.release_date),
            source="evs_index",
            served_by="index",
            retrieved_at=self.built_at,
            correlation_id=call_correlation_id(),
        )


@dataclass(frozen=True, slots=True)
class SearchHit:
    concept: NcitConcept
    score: float
    rank: int
    matched_on: str
    score_components: dict[str, float] = field(default_factory=dict)

    def to_dict(self, source_uri: str | None, include_raw: bool = False) -> dict[str, Any]:
        return {
            "concept": self.concept.to_dict(source_uri, include_raw=include_raw),
            "score": self.score,
            "rank": self.rank,
            "matched_on": self.matched_on,
            "score_components": dict(self.score_components),
        }


def _with_provenance(item: Any) -> dict[str, Any]:
    """The fields of a traversal item, with its provenance as the record names its fields."""

    data = {f.name: getattr(item, f.name) for f in fields(item) if f.name != "provenance"}
    return data | {"provenance": item.provenance.to_dict()}


@dataclass(frozen=True, slots=True)
class TraversalNode:
    code: str
    preferred_name: str
    terminology: str
    provenance: TraversalProvenance
    source_vocabulary: str = "NCI Thesaurus"

    def to_dict(self) -> dict[str, Any]:
        return _with_provenance(self)


@dataclass(frozen=True, slots=True)
class TraversalEdge:
    source_code: str
    target_code: str
    edge_type: str
    relationship_name: str
    provenance: TraversalProvenance
    target_name: str = ""
    source_name: str = ""

    def to_dict(self) -> dict[str, Any]:
        result = _with_provenance(self)
        if not self.relationship_name:
            result.pop("relationship_name")
        return result


@dataclass(frozen=True, slots=True)
class TraversalResult:
    start_codes: list[str]
    nodes: list[TraversalNode]
    edges: list[TraversalEdge]
    truncation: Truncation
    max_depth: int
    max_nodes: int
    max_edges: int
    # Minimal payloads already fetched by the walk, for the public concept projection.
    concepts: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Independent of the first reported bound; exhausted work must not resume.
    deadline_exhausted: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "start_codes": list(self.start_codes),
            "nodes": [node.to_dict() for node in self.nodes],
            "edges": [edge.to_dict() for edge in self.edges],
            "truncation": self.truncation.to_dict(),
            "max_depth": self.max_depth,
            "max_nodes": self.max_nodes,
            "max_edges": self.max_edges,
        }
