"""The registry's one expected-error path, inside the audit correlation scope."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from ncit_index.errors import (
    IndexCapabilityError,
    IndexCompatibilityError,
    IndexCursorError,
    IndexEvaluationError,
    IndexModelRevisionError,
    IndexReleaseError,
    IndexStateError,
    IndexStorageError,
    NoActiveIndexError,
)

from .audit import emit
from .bounds import (
    RequestBudgetError,
)
from .errors import (
    ErrorCode,
    InputValidationError,
    PlatformError,
    serialise,
    with_next_step,
)
from .evs import (
    EVSError,
    EVSNotFoundError,
    EVSReleaseMismatchError,
    EVSReleaseNotFoundError,
    EVSResponseError,
)
from .http_client import (
    UpstreamError,
    UpstreamRejectedError,
    UpstreamTimeoutError,
    UpstreamTooLargeError,
)
from .release import RegistryMetadataError

logger = logging.getLogger(__name__)

# Expected failures: each exception type, the error code it is reported under, and the
# caller's next step, which is appended to the exception's message. An exception gets the
# entry of its nearest listed class, and its `details` attribute, if any, goes into the
# record. Anything else is a bug and propagates. A `PlatformError` is reported as it is,
# with the message its raiser wrote.
_ERROR_CODES: dict[type[Exception], tuple[ErrorCode, str]] = {
    IndexCapabilityError: ("capability_unavailable", ""),
    IndexCursorError: ("cursor_expired", ""),
    IndexReleaseError: ("release_mismatch", ""),
    RequestBudgetError: ("bound_exceeded", "Ask for fewer start codes or a smaller traversal."),
    InputValidationError: ("invalid_request", "Correct the argument and call again."),
    EVSNotFoundError: (
        "not_found",
        "Check the code against the current release, which `release-info` names.",
    ),
    EVSReleaseNotFoundError: (
        "release_not_available",
        "Retry later: EVS no longer serves the release the request was pinned to.",
    ),
    EVSReleaseMismatchError: (
        "release_mismatch",
        "Retry later; no content of another release is used.",
    ),
    UpstreamTooLargeError: (
        "bound_exceeded",
        "Ask for a smaller response or check the upstream response limit.",
    ),
    EVSResponseError: (
        "upstream_unavailable",
        "Retry later; if it persists, check NCI_SIP_EVS_BASE_URL.",
    ),
    UpstreamTimeoutError: ("timeout", "Retry later, or raise NCI_SIP_TIMEOUT_SECONDS."),
    EVSError: ("upstream_unavailable", "Retry later."),
    UpstreamError: ("upstream_unavailable", "Retry later."),
    UpstreamRejectedError: (
        "upstream_unavailable",
        "Check the configured base URL and credentials for this upstream.",
    ),
    RegistryMetadataError: (
        "upstream_unavailable",
        "Retry later; no registry state is used without usable metadata.",
    ),
    NoActiveIndexError: ("internal_error", "Build the index with `index-sample` first."),
    IndexEvaluationError: (
        "internal_error",
        "Inspect `index-builds` and resolve the reported evaluation problem before retrying.",
    ),
    IndexStateError: (
        "internal_error",
        "Inspect `index-builds` and retry against an available completed build.",
    ),
    IndexModelRevisionError: (
        "internal_error",
        "Serve the model files this build was embedded with, or run `index-rebuild` with the "
        "serving model and activate its evaluated build.",
    ),
    IndexCompatibilityError: (
        "internal_error",
        "Use the original embedding settings, or run `index-rebuild` and activate its "
        "evaluated build with `index-activate`.",
    ),
    IndexStorageError: (
        "internal_error",
        "Check that the index file is readable and writable and that nothing else holds it.",
    ),
}
_EXPECTED_ERRORS = (*_ERROR_CODES, PlatformError)


def _platform_error(exc: Exception) -> PlatformError:
    if isinstance(exc, PlatformError):
        return exc
    code, next_step = next(_ERROR_CODES[cls] for cls in type(exc).__mro__ if cls in _ERROR_CODES)
    details = getattr(exc, "details", {})
    message = with_next_step(str(exc), next_step) if next_step else str(exc)
    return PlatformError(code, message, **details)


def error_record(exc: Exception) -> dict[str, Any]:
    """The error record of an expected failure, for a result that embeds it (no audit warning)."""

    return serialise(_platform_error(exc))


def call(operation: str, action: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    """Serialize expected failures using the correlation scope already established."""

    try:
        return action()
    except _EXPECTED_ERRORS as exc:
        error = _platform_error(exc)
        emit(logger, logging.WARNING, "call_failed", tool=operation, responseCode=error.code)
        return serialise(error)
