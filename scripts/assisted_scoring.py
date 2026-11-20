"""Offline task evaluation mechanics; no planner, model or public MCP capability."""

import json
from copy import deepcopy
from math import ceil, isfinite
from statistics import median
from typing import Any

from nci_sip_mcp.registry import SPECS


def validate_plan(task: dict[str, Any], proposed: Any) -> list[dict[str, Any]]:
    """The offline double may emit only its preregistered recipe, never new commands/URLs."""
    if json.dumps(proposed, sort_keys=True) != json.dumps(task["calls"], sort_keys=True):
        raise ValueError("Proposed plan differs from the approved offline recipe")
    allowed = {spec.name for spec in SPECS if spec.name} - {"ask"}
    if not proposed or any(call.get("name") not in allowed for call in proposed):
        raise ValueError("Plan includes an unsupported operation")
    return deepcopy(proposed)


def _at(value: Any, path: str) -> Any:
    for key in path.split("/"):
        if isinstance(value, list) and key.isdecimal() and int(key) < len(value):
            value = value[int(key)]
        elif isinstance(value, dict) and key in value:
            value = value[key]
        else:
            return _MISSING
    return value


_MISSING = object()


def _number(value: Any) -> bool:
    return type(value) in (int, float) and isfinite(value) and value >= 0


def _metrics_valid(record: dict[str, Any]) -> bool:
    return _number(record.get("elapsed_ms")) and all(
        type(record.get(key)) is int and record[key] >= 0 for key in ("requests", "bytes")
    )


def _equal(actual: Any, expected: Any) -> bool:
    return actual is not _MISSING and json.dumps(actual, sort_keys=True) == json.dumps(
        expected, sort_keys=True
    )


def _complete_content(result: Any) -> bool:
    return (
        isinstance(result, dict)
        and "error" not in result
        and not result.get("truncation", {}).get("occurred", False)
    )


def _protocol_consistent(results: list[Any], flags: Any) -> bool:
    if not isinstance(flags, list) or len(flags) != len(results):
        return False
    return all(
        isinstance(result, dict) and flag is ("error" in result)
        for result, flag in zip(results, flags, strict=True)
    )


def _checks(task: dict[str, Any], record: dict[str, Any]) -> dict[str, bool]:
    checks = {
        path: _equal(_at(record.get("results", []), path), expected)
        for path, expected in task["expect"].items()
    }
    checks["complete_evidence"] = len(record.get("results", [])) == len(task["calls"])
    unmatched = record.get("unmatched_requests")
    checks["recorded_upstream"] = type(unmatched) is int and unmatched == 0
    checks["protocol_consistent"] = _protocol_consistent(
        record.get("results", []), record.get("protocol_errors")
    )
    if task["outcome"] == "content":
        checks["complete_content"] = all(
            _complete_content(row) for row in record.get("results", [])
        )
    return checks


def assess(task: dict[str, Any], record: dict[str, Any]) -> dict[str, Any]:
    """Evaluate actual structured results, retaining every failure and missing measurement."""
    checks = _checks(task, record)
    metrics_valid = _metrics_valid(record)
    bounded = metrics_valid and record["requests"] <= task["max_requests"]
    correct = (
        record.get("status") == "completed"
        and bool(task["expect"])
        and all(checks.values())
        and bounded
    )
    return {
        "task": task["id"],
        "status": record.get("status", "incomplete"),
        "correct": bool(correct),
        "content_obtained": bool(correct and task["outcome"] == "content"),
        "expected_outcome": task["outcome"],
        "checks": checks,
        "within_request_bound": bool(bounded),
        **_measurements(record),
    }


def _measurements(record: dict[str, Any]) -> dict[str, Any]:
    return {
        key: record.get(key) if _number(record.get(key)) else None
        for key in ("elapsed_ms", "requests", "bytes")
    }


def _latency(times: list[float]) -> dict[str, Any]:
    return {
        "samples": len(times),
        "p50": median(times) if times else None,
        "p95": times[ceil(len(times) * 0.95) - 1] if times else None,
    }


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Nearest-rank p95 and median include measured failures, not only successful calls."""
    times = sorted(row["elapsed_ms"] for row in rows if row["elapsed_ms"] is not None)
    correct = sum(row["correct"] for row in rows)
    return {
        "runs": len(rows),
        "correct": correct,
        "correct_rate": correct / len(rows) if rows else None,
        "content_obtained": sum(row["content_obtained"] for row in rows),
        "latency_ms": _latency(times),
    }
