"""Stored snapshot records, independent of transport serialization and correlation."""

from dataclasses import asdict, dataclass, field, fields
from datetime import UTC, datetime
from typing import Any, Literal, Self


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class Concept:
    code: str
    preferred_name: str
    source_vocabulary: str
    terminology: str
    release_version: str
    release_date: str | None
    retrieved_at: str
    source: str
    evidence: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    def to_stored(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class IndexManifest:
    terminology: str
    release_version: str
    release_date: str | None
    embedding_provider: str
    embedding_model: str
    concept_count: int
    built_at: str
    index_path: str
    embedding_dimensions: int | None = None
    active: bool = False
    build_id: str = ""
    needs_rebuild: bool = False
    evaluation_version: str | None = None
    evaluation_score: float | None = None
    build_kind: Literal["legacy", "sample", "production"] = "legacy"
    evaluation_report: dict[str, Any] | None = None
    unclassified_source_build: str | None = None
    # Commit, content hash or algorithm name the build was embedded with; None when unknown.
    embedding_revision: str | None = None

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> Self:
        """Ignore unknown stored fields, preserving legacy defaults."""
        known = {item.name for item in fields(cls)}
        return cls(**{key: value for key, value in payload.items() if key in known})

    def embedding_matches(self, provider: str, model: str, dimensions: int | None = None) -> bool:
        return (
            self.embedding_provider == provider
            and self.embedding_model == model
            and (
                dimensions is None
                or self.embedding_dimensions is None
                or self.embedding_dimensions == dimensions
            )
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class SearchHit:
    concept: Concept
    score: float
    rank: int
    matched_on: str
    score_components: dict[str, float] = field(default_factory=dict)
