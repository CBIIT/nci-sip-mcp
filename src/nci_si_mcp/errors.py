"""Shared error types and serialization for CLI and MCP responses."""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any, Literal
from uuid import uuid4

if TYPE_CHECKING:
    from .validation import PolicyReason


class UpstreamError(RuntimeError):
    """Base error of a request that failed; `details` is what the error record carries."""

    def __init__(self, message: str, /, **details: Any) -> None:
        super().__init__(message)
        self.details = details


class RequestPolicyError(UpstreamError):
    """Terminal refusal: internal reason is never a public error detail or cache fallback."""

    def __init__(self, reason: PolicyReason) -> None:
        super().__init__("Controlled upstream request refused.")
        self.reason: PolicyReason = reason


# The closed set of error codes: the specification's error record (spec/records.yaml).
ErrorCode = Literal[
    "invalid_request",
    "permission_denied",
    "not_found",
    "release_not_available",
    "release_mismatch",
    "upstream_unavailable",
    "timeout",
    "bound_exceeded",
    "capability_unavailable",
    "cursor_expired",
    "internal_error",
]


class PlatformError(Exception):
    """A failure to report to the caller: its code, a message that names the caller's
    next step, and optionally the data that step needs (`details`)."""

    def __init__(self, code: ErrorCode, message: str, /, **details: Any) -> None:
        super().__init__(message)
        self.code: ErrorCode = code
        self.message = message
        self.details = details


class InputValidationError(ValueError):
    """Raised when a public API input is invalid; `parameter` names the argument."""

    def __init__(self, message: str, /, parameter: str | None = None) -> None:
        super().__init__(message)
        self.details: dict[str, Any] = (
            {"parameter": parameter, "reason": message} if parameter else {}
        )


def with_next_step(message: str, step: str) -> str:
    """Append the caller's next step to a message."""

    return message.rstrip(".") + ". " + step


# The identifier of the call in progress, set once per call by its adapter.
_correlation_id: ContextVar[str | None] = ContextVar("correlation_id", default=None)

# 128 printable header-safe characters bound telemetry amplification and prevent header
# injection/encoding errors. Owner-approved form; review caller/header needs before changing.
CORRELATION_ID_FORM = r"[A-Za-z0-9._:-]{1,128}"


def safe_correlation(value: object) -> str | None:
    return value if isinstance(value, str) and re.fullmatch(CORRELATION_ID_FORM, value) else None


def validate_correlation(value: object, *, supplied: bool) -> dict[str, object]:
    if supplied and safe_correlation(value) is None:
        raise InputValidationError(
            "Use 1 to 128 characters from A-Z, a-z, 0-9, dot, underscore, colon or hyphen",
            "_meta.correlationId",
        )
    return {}


def current_correlation_id() -> str | None:
    """The correlation identifier of the call in progress, or None outside any call."""

    return _correlation_id.get()


def call_correlation_id() -> str:
    """The correlation identifier of the call in progress, for the provenance of its items.

    Every handler runs inside the audit correlation scope, so none means a bug.
    """

    value = current_correlation_id()
    if value is None:
        raise RuntimeError("An item's provenance was built outside any call")
    return value


@contextmanager
def correlated(correlation_id: object = None) -> Iterator[str]:
    """Run one call under its correlation identifier (M7.1).

    The caller's identifier is used when it is a non-empty string, otherwise one
    is generated. The audit boundary opens this once per call; `serialise` reads it.
    """

    value = safe_correlation(correlation_id) or uuid4().hex
    token = _correlation_id.set(value)
    try:
        yield value
    finally:
        _correlation_id.reset(token)


def serialise(error: PlatformError) -> dict[str, Any]:
    """Return the error record `{"error": {code, message, details?, correlationId}}`.

    The one place a failure becomes a result. The record is the whole result: it
    has no other key, so it cannot be mistaken for an empty success. The
    correlation identifier is the one of the call in progress; a failure raised
    outside any call (a direct service call) gets a fresh one. CLI argument
    parsing (argparse) and unexpected exceptions are reported by the runtimes
    themselves; MCP argument validation is converted to `invalid_request` by
    `server._validate_inputs` and does come through here.
    """

    record: dict[str, Any] = {"code": error.code, "message": error.message}
    if error.details:
        record["details"] = error.details
    record["correlationId"] = current_correlation_id() or uuid4().hex
    return {"error": record}


def is_error_record(result: dict[str, Any]) -> bool:
    """Whether a result is an error record, as `serialise` builds it."""

    return set(result) == {"error"}
