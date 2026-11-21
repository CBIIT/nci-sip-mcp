"""Request-scoped telemetry, with redaction before a record reaches logging (A6)."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import math
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from .errors import correlated, current_correlation_id, is_error_record
from .validation import ReleaseSelection

type AuditClass = Literal["plain", "hash"]
logger = logging.getLogger(__name__)


def compact(value: Any) -> str:
    return json.dumps(
        _finite_numbers(value),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    )


def _finite_numbers(value: Any) -> Any:
    # Valid JSON exponents can overflow during parsing. Preserve their diagnostic
    # meaning as strings instead of emitting invalid JSON or losing the audit record.
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if isinstance(value, dict):
        return {key: _finite_numbers(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_finite_numbers(item) for item in value]
    return value


def hashed(value: Any) -> dict[str, str]:
    return {"sha256": hashlib.sha256(compact(value).encode("utf-8")).hexdigest()}


def timestamp() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def secrets(license_key: str | None, credential: str | None) -> tuple[str, ...]:
    values = [license_key, credential]
    if credential:
        values.append(base64.b64encode(credential.encode()).decode())
        values.append(credential.partition(":")[2])
    return tuple(sorted((value for value in values if value), key=len, reverse=True))


def _redact(value: Any, hidden: tuple[str, ...]) -> Any:
    if isinstance(value, str):
        for secret in hidden:
            value = value.replace(secret, "[redacted]")
        return value
    if isinstance(value, dict):
        return {_redact(key, hidden): _redact(item, hidden) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_redact(item, hidden) for item in value]
    return value


def parameters(arguments: Mapping[str, Any], classes: Mapping[str, AuditClass]) -> dict[str, Any]:
    # Unknown fields are untrusted too: neither their names nor values become free text.
    return {
        name if name in classes else compact(hashed(name)): (
            value if classes.get(name) == "plain" else hashed(value)
        )
        for name, value in arguments.items()
    }


def emit(destination: logging.Logger, level: int, event: str, **fields: Any) -> None:
    active = _active.get()
    record = {
        "timestamp": timestamp(),
        "level": logging.getLevelName(level),
        "event": event,
        "correlationId": current_correlation_id(),
        **fields,
    }
    record = _redact(record, active.hidden if active else ())
    destination.log(level, compact(record), extra={"structured": record})


class JsonFormatter(logging.Formatter):
    """External diagnostics share the format without exposing their arbitrary messages."""

    def format(self, record: logging.LogRecord) -> str:
        structured = record.__dict__.get("structured")
        if structured is not None:
            return compact(structured)
        active = _active.get()
        fallback = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "event": "diagnostic",
            "correlationId": current_correlation_id(),
            "messageHash": hashed(record.getMessage())["sha256"],
        }
        return compact(_redact(fallback, active.hidden if active else ()))


def _releases(value: Any) -> list[dict[str, Any]]:
    return list({compact(item): item for item in _release_items(value)}.values())


def _release_items(value: Any) -> Iterator[dict[str, Any]]:
    if isinstance(value, list):
        for item in value:
            yield from _release_items(item)
    elif isinstance(value, dict):
        yield from _object_releases(value)


def _object_releases(value: dict[str, Any]) -> Iterator[dict[str, Any]]:
    if own := _release_ref(value.get("provenance")):
        yield own
    for key, item in value.items():
        # Raw upstream payloads are not platform envelopes and may contain arbitrary fields.
        if key not in {"raw", "provenance"}:
            yield from _release_items(item)


def _release_ref(provenance: Any) -> dict[str, Any] | None:
    if not isinstance(provenance, dict):
        return None
    release = provenance.get("release")
    return release if isinstance(release, dict) else None


def _outcome(result: dict[str, Any] | None, size: int | None) -> dict[str, Any]:
    if result is None:
        return {"responseCode": "internal_error", "resultSize": None, "truncation": None}
    return {
        "responseCode": result["error"]["code"] if is_error_record(result) else "ok",
        "resultSize": len(compact(result).encode("utf-8")) if size is None else size,
        "truncation": result.get("truncation"),
    }


@dataclass
class Audit:
    tool: str
    arguments: Mapping[str, Any]
    classes: Mapping[str, AuditClass]
    hidden: tuple[str, ...]
    started: float = field(default_factory=time.perf_counter)
    outbound_requests: int = 0
    result: dict[str, Any] | None = None
    result_size: int | None = None
    error_type: str | None = None
    release_source: ReleaseSelection | None = None
    selected_release: dict[str, Any] | None = None

    def finish(self) -> None:
        outcome = _outcome(self.result, self.result_size)
        safe = parameters(self.arguments, self.classes)
        emit(
            logger,
            logging.INFO,
            "call_completed",
            tool=self.tool,
            parameters=safe,
            target={key: safe[key] for key in ("terminology", "context") if key in safe},
            release={
                "requested": safe.get("release", safe.get("registryRelease")),
                "resolved": _releases(self.result),
                **(
                    {"selection": self.release_source, "selected": self.selected_release}
                    if self.release_source
                    else {}
                ),
            },
            status="ok" if outcome["responseCode"] == "ok" else "error",
            errorType=self.error_type,
            outboundRequests=self.outbound_requests,
            **outcome,
            elapsedMs=(time.perf_counter() - self.started) * 1000,
        )


_active: ContextVar[Audit | None] = ContextVar("audit_call", default=None)


def release_selection(source: ReleaseSelection, selected: dict[str, Any] | None) -> None:
    """Retain the effective release even when the content request fails."""
    if active := _active.get():
        active.release_source, active.selected_release = source, selected


def result_text(text: str) -> None:
    """Take the result size from the compact text an adapter sends; it is not serialised again."""
    if active := _active.get():
        active.result_size = len(text.encode("utf-8"))


def requested() -> None:
    """Count one completed HTTP attempt, independently of the traversal allowance."""

    if active := _active.get():
        active.outbound_requests += 1


@contextmanager
def audited(
    tool: str,
    arguments: Mapping[str, Any],
    classes: Mapping[str, AuditClass],
    hidden: tuple[str, ...],
    correlation_id: object = None,
) -> Iterator[Audit]:
    # MCP owns the scope before validation; its registry invocation shares that scope.
    active = _active.get()
    inherited = current_correlation_id() if correlation_id is None else correlation_id
    with nullcontext() if active else correlated(inherited):
        record = active or Audit(tool, arguments, classes, hidden)
        token = _active.set(record)
        try:
            yield record
        except BaseException as exc:
            record.result = record.result_size = None
            record.error_type = type(exc).__name__
            raise
        finally:
            try:
                if active is None:
                    record.finish()
            finally:
                _active.reset(token)
