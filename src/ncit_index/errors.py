"""Failures of local storage, build selection and retrieval."""

from typing import Any


class IndexCompatibilityError(RuntimeError):
    """A schema, release or embedding space is incompatible."""


class IndexModelRevisionError(IndexCompatibilityError):
    """The serving model is not the revision the active index was embedded with."""


class IndexBuildError(RuntimeError):
    """Invalid build input: a caller bug, not a recoverable storage failure."""


class IndexEvaluationError(IndexBuildError):
    """An operator build violates evaluation or sample-isolation policy."""


class IndexStateError(IndexBuildError):
    """An unavailable build or a concurrent activation."""


class NoActiveIndexError(RuntimeError):
    """No active search build exists."""


class IndexStorageError(RuntimeError):
    """The database file cannot be opened, read or written."""


class IndexCapabilityError(RuntimeError):
    """The installed index cannot serve the requested operation."""

    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message)
        self.details = details


class IndexReleaseError(RuntimeError):
    def __init__(self, requested: str, served: str) -> None:
        super().__init__("The index holds another release. Rebuild it for the requested release.")
        self.details: dict[str, Any] = {
            "requested": requested,
            "served": [served],
            "source": "index",
        }


class IndexCursorError(RuntimeError):
    def __init__(self, requested: str, served: str) -> None:
        super().__init__(
            "The active index build changed. Restart the search against the active build."
        )
        self.details = {"cursorRelease": requested, "currentRelease": served}
