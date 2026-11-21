"""Versioned retrieval judgments and measured regression floors.

These engineering judgments are separate from the specification acceptance suite.
Test-only sets exercise the gate without claiming deterministic embeddings measure quality.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .validation import NCIT_CODE_RE


@dataclass(frozen=True, slots=True)
class GoldQuery:
    query: str
    expected_codes: tuple[str, ...]
    category: str = "furnished"


@dataclass(frozen=True, slots=True)
class MetricFloor:
    hit_at_5: float
    mrr_at_10: float


@dataclass(frozen=True, slots=True)
class EvaluationSet:
    version: str
    queries: tuple[GoldQuery, ...]
    semantic: MetricFloor
    hybrid: MetricFloor
    embedding_provider: str
    embedding_model: str
    embedding_dimensions: int
    release: str
    test_only: bool


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Evaluation {label} must be nonempty text")
    return value


def _object(value: Any, keys: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError(f"Evaluation {label} must contain exactly {sorted(keys)}")
    return value


def _fraction(value: Any, label: str) -> float:
    if type(value) not in (float, int):
        raise ValueError(f"Evaluation {label} must be a finite fraction")
    if not 0 <= value <= 1:
        raise ValueError(f"Evaluation {label} must be between zero and one")
    return float(value)


def _dimensions(value: Any) -> int:
    if type(value) is not int or value < 1:
        raise ValueError("Evaluation embedding dimensions must be a positive integer")
    return value


def _floor(value: Any) -> MetricFloor:
    record = _object(value, {"hit_at_5", "mrr_at_10"}, "floor")
    return MetricFloor(
        _fraction(record["hit_at_5"], "Hit@5 floor"),
        _fraction(record["mrr_at_10"], "MRR@10 floor"),
    )


def _codes(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise ValueError("Evaluation expected_codes must be a nonempty list")
    if any(not isinstance(code, str) or not NCIT_CODE_RE.fullmatch(code) for code in value):
        raise ValueError("Evaluation expected_codes must contain NCIt concept codes")
    if len(set(value)) != len(value):
        raise ValueError("Evaluation expected_codes must be distinct")
    return tuple(value)


def _queries(value: Any) -> tuple[GoldQuery, ...]:
    if not isinstance(value, list) or not value:
        raise ValueError("Evaluation queries must be a nonempty list")
    queries = []
    for item in value:
        row = _object(item, {"query", "expected_codes", "category"}, "query")
        queries.append(
            GoldQuery(
                _text(row["query"], "query"),
                _codes(row["expected_codes"]),
                _text(row["category"], "category"),
            )
        )
    if len({query.query for query in queries}) != len(queries):
        raise ValueError("Evaluation queries must be distinct")
    return tuple(queries)


def parse_set(value: Any) -> EvaluationSet:
    """Reject incomplete or mistyped judgments instead of scoring a smaller denominator."""
    row = _object(
        value,
        {
            "version",
            "queries",
            "thresholds",
            "embedding_provider",
            "embedding_model",
            "embedding_dimensions",
            "release",
            "test_only",
        },
        "set",
    )
    thresholds = _object(row["thresholds"], {"semantic", "hybrid"}, "thresholds")
    if type(row["test_only"]) is not bool:
        raise ValueError("Evaluation test_only must be a boolean")
    return EvaluationSet(
        _text(row["version"], "version"),
        _queries(row["queries"]),
        _floor(thresholds["semantic"]),
        _floor(thresholds["hybrid"]),
        _text(row["embedding_provider"], "embedding provider"),
        _text(row["embedding_model"], "embedding model"),
        _dimensions(row["embedding_dimensions"]),
        _text(row["release"], "release"),
        row["test_only"],
    )


def load_set(path: Path) -> EvaluationSet:
    """Load an explicitly versioned set; malformed JSON never becomes an empty evaluation."""
    return parse_set(json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_keys))


def production_set() -> EvaluationSet:
    """The shipped engineering calibration, measured on full NCIt with SapBERT."""
    return load_set(Path(__file__).with_name("data") / "ncit-26.09d-sapbert-v1.json")


def _unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Evaluation JSON repeats key {key!r}")
        result[key] = value
    return result
