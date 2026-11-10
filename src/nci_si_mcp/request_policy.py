"""Explicit opt-in exchange contracts; no harness configuration or transport activation."""

from collections.abc import Callable, Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Protocol
from urllib.request import Request

from .validation import BodyState


@dataclass(frozen=True, slots=True)
class Exchange:
    """Framed bytes only; private response fields never appear in representations."""

    status: int
    headers: Mapping[str, str] = field(repr=False)
    body: bytes = field(repr=False)
    body_state: BodyState = "complete"

    def __post_init__(self) -> None:
        # HTTP fields are case-insensitive; retain the first occurrence, as HTTPMessage.get does.
        headers: dict[str, str] = {}
        for name, value in self.headers.items():
            headers.setdefault(name.lower(), value)
        object.__setattr__(self, "headers", MappingProxyType(headers))


class TransportStart(Protocol):
    @property
    def refused_before_entry(self) -> bool:
        """The latest invoke certified an ordinary core-limit refusal before entry."""
        ...

    def __call__(self) -> None:
        """Legacy providers declare entry immediately before opening transport."""
        ...

    def invoke[T](self, *, admit: Callable[[], None], operation: Callable[[], T]) -> T:
        """Finish admission before core entry accounting, then invoke transport."""
        ...


class ControlledRequest(Protocol):
    def check_wait(self, delay_seconds: float) -> None:
        """Refuse a retry wait that cannot fit within the shared run allowance."""
        ...

    def exchange(
        self, *, timeout_seconds: float, response_limit: int, on_start: TransportStart
    ) -> Exchange:
        """Reserve, enter transport, read bounded bytes and release the attempt lease.

        Call on_start exactly at transport entry, before I/O; if it raises, do not send.
        Error bodies are at most 4096 bytes; success bodies at most response_limit+1.
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
