"""Client-observed HTTP measurements; server telemetry is never inferred from elapsed time."""

from __future__ import annotations

import json
from typing import Any, Literal, cast, get_args

from mcp import types
from scripts.benchmark_records import Sample
from scripts.vocabulary import HttpStop, RunStop

from nci_sip_mcp.errors import ErrorCode


class MeasurementError(ValueError):
    """A reply cannot support a trustworthy client-observed measurement."""


def _response_code(
    content: dict[str, Any], is_error: bool, correlation: str
) -> Literal["ok"] | ErrorCode:
    if is_error != ("error" in content):
        raise MeasurementError("Protocol and structured result disagree")
    if not is_error:
        return "ok"
    error = content["error"]
    if not isinstance(error, dict) or error.get("code") not in get_args(ErrorCode):
        raise MeasurementError("Invalid structured error code")
    if error.get("correlationId") != correlation:
        raise MeasurementError("Error correlation does not match the measured call")
    return cast("ErrorCode", error["code"])


def measure_reply(
    reply: types.CallToolResult,
    elapsed_ms: float,
    correlation: str,
    *,
    http_requests: int,
) -> dict[str, Any]:
    content = reply.structured_content
    if not isinstance(content, dict):
        raise MeasurementError("Measured reply has no structured object")
    response_code = _response_code(content, reply.is_error, correlation)
    if "provenance" in content:
        provenance = content["provenance"]
        if not isinstance(provenance, dict) or provenance.get("correlationId") != correlation:
            raise MeasurementError("Provenance correlation does not match the measured call")
    size = len(json.dumps(content, ensure_ascii=False, separators=(",", ":")).encode())
    return _sample(elapsed_ms, correlation, http_requests, size, response_code, reply.is_error)


def _sample(
    elapsed_ms: float,
    correlation: str,
    http_requests: int,
    size: int | None,
    response_code: Literal["ok"] | ErrorCode | RunStop | HttpStop,
    error: bool,
) -> dict[str, Any]:
    return Sample(
        elapsedMs=elapsed_ms,
        resultBytes=size,
        httpRequests=http_requests,
        responseCode=response_code,
        error=error,
        correlationId=correlation,
    ).to_dict("streamable-http")


def failure_sample(
    reason: RunStop | HttpStop, elapsed_ms: float, correlation: str, *, http_requests: int
) -> dict[str, Any]:
    return _sample(elapsed_ms, correlation, http_requests, None, reason, True)
