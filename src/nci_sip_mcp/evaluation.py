"""Small retrieval evaluation helper for ranker/model comparisons."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import asdict, dataclass
from typing import Any

from ncit_index.embeddings import EmbeddingProvider
from ncit_index.errors import IndexEvaluationError
from ncit_index.index import LocalIndex, evaluation_identity, evaluation_next_step
from ncit_index.records import IndexManifest

from .evaluation_sets import EvaluationSet, GoldQuery, production_set
from .models import utc_now_iso
from .validation import MAX_INDEX_SEARCH_LIMIT, validate_search

HIT_CUTOFF = 5


@dataclass(frozen=True, slots=True)
class QueryResult:
    query: str
    category: str
    expected_codes: tuple[str, ...]
    ranked_codes: tuple[str, ...]
    first_expected_rank: int | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self) | {
            "expected_codes": list(self.expected_codes),
            "ranked_codes": list(self.ranked_codes),
        }


@dataclass(frozen=True, slots=True)
class EvaluationResult:
    mode: str
    query_count: int
    hit_at_1: float
    hit_at_5: float
    mean_reciprocal_rank: float
    limit: int
    per_query: tuple[QueryResult, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self) | {"per_query": [query.to_dict() for query in self.per_query]}


def _first_expected_rank(ranked_codes: tuple[str, ...], expected: tuple[str, ...]) -> int | None:
    """First relevant rank; an absent concept contributes zero without leaving the denominator."""

    for rank, code in enumerate(ranked_codes, start=1):
        if code in expected:
            return rank
    return None


def _evaluate_query(
    index: LocalIndex,
    provider: EmbeddingProvider,
    gold: GoldQuery,
    mode: str,
    limit: int,
    build_id: str | None,
) -> QueryResult:
    query, limit, mode = validate_search(gold.query, limit, mode, maximum=MAX_INDEX_SEARCH_LIMIT)
    if build_id is None:
        hits = index.search(query, provider, limit=limit, mode=mode)
    else:
        hits, _ = index.search_build(build_id, query, provider, limit=limit, mode=mode)
    codes = tuple(hit.concept.code for hit in hits)
    return QueryResult(
        gold.query,
        gold.category,
        gold.expected_codes,
        codes,
        _first_expected_rank(codes, gold.expected_codes),
    )


def _evaluate_mode(
    index: LocalIndex,
    embedding_provider: EmbeddingProvider,
    queries: list[GoldQuery],
    mode: str,
    limit: int,
    build_id: str | None,
) -> EvaluationResult:
    per_query = tuple(
        _evaluate_query(index, embedding_provider, gold, mode, limit, build_id) for gold in queries
    )
    ranks = [item.first_expected_rank for item in per_query]
    hit_1, hit_5, mrr = _metrics(ranks)
    return EvaluationResult(
        mode=mode,
        query_count=len(queries),
        hit_at_1=hit_1,
        hit_at_5=hit_5,
        mean_reciprocal_rank=mrr,
        limit=limit,
        per_query=per_query,
    )


def _metrics(ranks: list[int | None]) -> tuple[float, float, float]:
    denominator = len(ranks)
    return (
        sum(rank == 1 for rank in ranks) / denominator,
        sum(rank is not None and rank <= HIT_CUTOFF for rank in ranks) / denominator,
        sum(1 / rank if rank else 0 for rank in ranks) / denominator,
    )


def evaluate_retrieval(
    index: LocalIndex,
    embedding_provider: EmbeddingProvider,
    gold_queries: Iterable[GoldQuery] | None = None,
    modes: Iterable[str] = ("bm25", "vector", "hybrid"),
    limit: int = 10,
    *,
    build_id: str | None = None,
) -> list[EvaluationResult]:
    queries = list(production_set().queries if gold_queries is None else gold_queries)
    selected_modes = list(modes)
    if not queries:
        raise ValueError("Evaluation queries must be nonempty")
    if not selected_modes:
        raise ValueError("Evaluation modes must be nonempty")
    if type(limit) is not int or limit < HIT_CUTOFF:
        raise ValueError("Evaluation limit must be an integer of at least five for Hit@5")
    return [
        _evaluate_mode(index, embedding_provider, queries, mode, limit, build_id)
        for mode in selected_modes
    ]


def evaluate_build(
    index: LocalIndex,
    provider: EmbeddingProvider,
    dataset: EvaluationSet,
    build_id: str,
) -> IndexManifest:
    """Evaluate an immutable candidate and store its complete report before activation."""
    codes = {code for query in dataset.queries for code in query.expected_codes}
    manifest, missing = index.evaluation_inputs(build_id, codes)
    if manifest.build_kind not in {"sample", "legacy", "production"}:
        raise IndexEvaluationError(
            f"Build {build_id} has an unknown classification; rebuild and evaluate it"
        )
    calibrated = (
        dataset.embedding_provider,
        dataset.embedding_model,
        dataset.embedding_dimensions,
        dataset.release,
    )
    actual = (
        manifest.embedding_provider,
        manifest.embedding_model,
        manifest.embedding_dimensions,
        manifest.release_version,
    )
    if calibrated != actual:
        raise IndexEvaluationError(
            "Evaluation calibration does not match the build provider, model, "
            "dimensions or release. " + evaluation_next_step(manifest)
        )
    results = evaluate_retrieval(index, provider, dataset.queries, build_id=build_id)
    report = _build_report(manifest, dataset, results, missing)
    return index.record_evaluation(build_id, report)


def _build_report(
    manifest: IndexManifest,
    dataset: EvaluationSet,
    results: list[EvaluationResult],
    missing: list[str],
) -> dict[str, Any]:
    by_mode = {result.mode: result for result in results}
    floors = {"vector": dataset.semantic, "hybrid": dataset.hybrid}
    passed = not missing and all(
        by_mode[mode].hit_at_5 >= floor.hit_at_5
        and by_mode[mode].mean_reciprocal_rank >= floor.mrr_at_10
        for mode, floor in floors.items()
    )
    return evaluation_identity(manifest) | {
        "evaluation_version": dataset.version,
        "evaluation_score": min(by_mode[mode].mean_reciprocal_rank for mode in floors),
        "evaluated_at": utc_now_iso(),
        "passed": passed,
        "test_only": dataset.test_only,
        "gold_codes_not_indexed": missing,
        "thresholds": {"vector": asdict(dataset.semantic), "hybrid": asdict(dataset.hybrid)},
        "results": [result.to_dict() for result in results],
    }
