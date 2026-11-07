"""Explicit opt-in exchange contracts; no harness configuration or transport activation."""

from collections.abc import Callable, Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Literal, Protocol
from urllib.request import Request

type PolicyReason = Literal[
    "authorization",
    "logical_limit",
    "attempt_limit",
    "concurrency_limit",
    "deadline",
    "response_limit",
    "byte_limit",
    "accounting",
    "closed",
    "transport",
]
type BodyState = Literal["complete", "proven_empty", "unreadable"]


@dataclass(frozen=True, slots=True)
class Exchange:
    """Framed bytes only; private response fields never appear in representations."""

    status: int
    headers: Mapping[str, str] = field(repr=False)
    body: bytes = field(repr=False)
    body_state: BodyState = "complete"

    def __post_init__(self) -> None:
        object.__setattr__(self, "headers", MappingProxyType(dict(self.headers)))


class ControlledRequest(Protocol):
    def check_wait(self, delay_seconds: float) -> None:
        """Refuse a retry wait that cannot fit within the shared run allowance."""
        ...

    def exchange(
        self, *, timeout_seconds: float, response_limit: int, on_start: Callable[[], None]
    ) -> Exchange:
        """Reserve, enter transport, read bounded bytes and release the attempt lease.

        Call on_start exactly at transport entry, before I/O; if it raises, do not send.
        Error bodies are at most4096 bytes; success bodies at most response_limit+1.
        Refusals must not be normalized into retriable transport exceptions.
        """
        ...


class RequestPolicy(Protocol):
    def request(self, request: Request) -> AbstractContextManager[ControlledRequest]:
        """Validate and snapshot the exact request, then reserve one logical admission."""
        ...


_active: ContextVar[RequestPolicy | None] = ContextVar("request_policy", default=None)


def current_request_policy() -> RequestPolicy | None:
    return _active.get()


@contextmanager
def using_request_policy(policy: RequestPolicy) -> Iterator[None]:
    """Clients capture this explicit policy at construction, before the scope is reset."""
    if policy is None:
        raise ValueError("an explicit request policy is required")
    token = _active.set(policy)
    try:
        yield
    finally:
        _active.reset(token)
