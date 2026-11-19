"""One lazy release decision per invocation, optionally held by its MCP session."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from threading import Lock
from typing import TYPE_CHECKING

from . import audit
from .caching import select_cache_hint
from .errors import InputValidationError, PlatformError
from .evs import EVSReleaseNotFoundError
from .release import ReleaseContext, resolve_evs_release
from .validation import (
    RELEASE_FORM,
    validate_identifier,
    validate_terminology,
)

if TYPE_CHECKING:
    from .context import Context


@dataclass
class SessionRelease:
    """Only the implicit NCIt pin survives calls; the lock serializes first discovery.

    The lock is held across the EVS discovery request of a session's first implicit call, so
    two concurrent first calls resolve once and agree on one pin. The cost is that a slow EVS
    (see the 120 s IPv6 case in AGENTS.md) makes every other implicit call of that session wait
    behind it until the pin is set; explicit releases never take the lock.
    """

    selected: ReleaseContext | None = None
    lock: Lock = field(default_factory=Lock)


@dataclass
class _Selection:
    selected: ReleaseContext | None = None
    implicit: bool = False


_session: ContextVar[SessionRelease | None] = ContextVar("release_session", default=None)
_call: ContextVar[_Selection | None] = ContextVar("release_selection", default=None)


@contextmanager
def session_scope(state: SessionRelease | None) -> Iterator[None]:
    token = _session.set(state)
    try:
        yield
    finally:
        _session.reset(token)


@contextmanager
def selection_scope() -> Iterator[None]:
    selection = _Selection()
    token = _call.set(selection)
    try:
        yield
    except EVSReleaseNotFoundError:
        if not selection.implicit or selection.selected is None:
            raise
        raise PlatformError(
            "release_not_available",
            "EVS no longer serves the selected implicit release. "
            "Start a new session or name a release.",
            requested=selection.selected.version,
            source="evs",
        ) from None
    finally:
        _call.reset(token)


def implicit_selection() -> bool:
    selection = _call.get()
    return selection is not None and selection.implicit


def select(context: Context, terminology: str, release: str | None) -> ReleaseContext:
    """Called only after content arguments have been validated, before content access."""
    validate_terminology(terminology)
    if release is not None:
        validate_identifier(release, RELEASE_FORM, "release")
        selected = ReleaseContext(
            terminology, context.settings.release_channel, release, None, f"{terminology}_{release}"
        )
        audit.release_selection("explicit", selected.to_dict())
        return selected
    if terminology != "ncit":
        raise InputValidationError("A non-NCIt terminology requires an explicit release", "release")
    # The caller's arguments do not key the effective release, even when session-held.
    select_cache_hint(resolution=False, implicit=True)
    selection = _call.get()
    if selection is not None and selection.selected is not None:
        return selection.selected
    selected = _implicit(context)
    if selection is not None:
        selection.selected, selection.implicit = selected, True
    return selected


def _implicit(context: Context) -> ReleaseContext:
    state = _session.get()
    if state is None:
        return _discover(context)
    with state.lock:
        if state.selected is None:
            state.selected = _discover(context)
        else:
            audit.release_selection("session-held", state.selected.to_dict())
        return state.selected


def _discover(context: Context) -> ReleaseContext:
    audit.release_selection("freshly-resolved", None)
    selected = resolve_evs_release(context.evs, "ncit", context.settings.release_channel)
    audit.release_selection("freshly-resolved", selected.to_dict())
    return selected
