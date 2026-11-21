"""Classify what an upstream platform answered with a success status.

Platforms mask failures as successful responses: a webMethods error envelope in
an HTTP 200, a FHIR OperationOutcome, an HTML page from a proxy where JSON was
asked for. `parse_upstream_json` is the one place that recognises them, so no
caller parses such a body as content.
"""

from __future__ import annotations

import json
from typing import Any

from .errors import PlatformError

_NEXT_STEP = "Retry later; if it persists, check that the platform's base URL is the right one."
_FHIR_ERROR_SEVERITIES = {"error", "fatal"}


class MaskedSuccessError(PlatformError):
    """`upstream_unavailable` for a platform error masked as a success response, so a caller
    that knows what such an answer means can word it without reading the message."""


def _webmethods_error(data: dict[str, Any]) -> str | None:
    envelope = data.get("apiResponse")
    if not isinstance(envelope, dict) or envelope.get("type") != "E":
        return None
    return "a webMethods error envelope"


def _fhir_error(data: dict[str, Any]) -> str | None:
    issues = data.get("issue")
    if data.get("resourceType") != "OperationOutcome" or not isinstance(issues, list):
        return None
    severities = {
        severity
        for issue in issues
        if isinstance(issue, dict)
        if isinstance(severity := issue.get("severity"), str)
    }
    if severities & _FHIR_ERROR_SEVERITIES:
        return "a FHIR OperationOutcome that reports an error"
    return None


def _masked_error(data: Any) -> str | None:
    """Describe the failure a parsed JSON body carries, or None when it is content."""

    if not isinstance(data, dict):
        return None
    return _webmethods_error(data) or _fhir_error(data)


def _surface(source: str) -> str:
    """The platform a request went to: the first word of `source`, lower-cased."""

    return source.split(maxsplit=1)[0].lower()


def parse_upstream_json(payload: bytes, source: str) -> Any:
    """Parse a response body as JSON content, or raise `upstream_unavailable`.

    `source` names the request in the message, for example "EVS /api/v1/version".
    """

    try:
        data = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        what = "an HTML page" if payload.lstrip().startswith(b"<") else "invalid JSON"
        raise PlatformError(
            "upstream_unavailable",
            f"{source} answered with {what} instead of JSON. {_NEXT_STEP}",
            surface=_surface(source),
        ) from exc
    masked = _masked_error(data)
    if masked:
        raise MaskedSuccessError(
            "upstream_unavailable",
            f"{source} answered with {masked} in a success response. {_NEXT_STEP}",
            surface=_surface(source),
        )
    return data
