"""Traversal limits and the request allowance shared by one call (A5)."""

from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from time import monotonic

from .errors import PlatformError


class CallDeadlineError(PlatformError):
    """The invocation cannot start more work within its elapsed-time allowance."""

    def __init__(self, attempts: int, surface: str | None, figures: tuple[int, int, int]) -> None:
        self.figures = figures
        super().__init__(
            "upstream_unavailable",
            "The call exhausted its elapsed-time budget. Retry later or ask for less work.",
            **({"surface": surface} if surface else {}),
            attempts=attempts,
        )


@dataclass(slots=True)
class Deadline:
    seconds: float
    # Resolve the shared clock at invocation time, not when the class is imported.
    started: float = field(default_factory=lambda: monotonic())  # noqa: PLW0108
    attempts: int = 0
    surface: str | None = None

    def remaining(self) -> float:
        return self.seconds - (monotonic() - self.started)

    def check(self, wait: float = 0) -> None:
        if self.remaining() <= wait:
            raise CallDeadlineError(self.attempts, self.surface, self.figures())

    def request(self, surface: str) -> None:
        self.surface = surface
        self.check()
        self.attempts += 1

    def figures(self) -> tuple[int, int, int]:
        return int(self.seconds * 1000), int((monotonic() - self.started) * 1000), 0


_deadline: ContextVar[Deadline | None] = ContextVar("call_deadline", default=None)


def current_deadline() -> Deadline | None:
    return _deadline.get()


def check_deadline() -> None:
    if deadline := current_deadline():
        deadline.check()


def deadline_description(seconds: float) -> str:
    """One client-visible contract, shared by every deadline-bound tool description."""
    return (
        f"Call elapsed-time budget: {seconds:g} s by default, shared across upstream work. "
        "A stalled body read may overrun by its open-time socket timeout, capped by "
        "NCI_SI_TIMEOUT_SECONDS (NCI_SI_MATCH_TIMEOUT_SECONDS for matching); DNS/connection "
        "setup is not a hard real-time cutoff. Parent/child hierarchy and neighborhood walks "
        "preserve verified partial results without nextCursor: the first bound remains, else "
        "truncation.bound is deadline with limit/reached in ms; before a verified start, "
        "upstream_unavailable. Ask for less depth/fewer kinds or retry."
    )


@contextmanager
def deadline_scope(seconds: float) -> Iterator[None]:
    token = _deadline.set(current_deadline() or Deadline(seconds))
    try:
        yield
    finally:
        _deadline.reset(token)


DEFAULT_MAX_DEPTH = 2
DEFAULT_MAX_NODES = 200
DEFAULT_MAX_EDGES = 1000
HARD_MAX_DEPTH = 4
HARD_MAX_NODES = 1000
HARD_MAX_EDGES = 5000
MAX_TRAVERSAL_REQUESTS = 200
HARD_MAX_PER_KIND = 1000
# EVS accepts 1,000 codes, but its deployed URL ceiling is lower (see ARCHITECTURE.md).
HARD_MAX_BATCH_CODES = 650
MAX_BATCH_TARGET_BYTES = 7000


def clamp_limits(max_depth: int, max_nodes: int) -> tuple[int, int]:
    return min(max(max_depth, 0), HARD_MAX_DEPTH), min(max(max_nodes, 1), HARD_MAX_NODES)


def clamp_edge_limit(max_edges: int) -> int:
    return min(max(max_edges, 1), HARD_MAX_EDGES)


class RequestBudgetError(RuntimeError):
    """Another request would exceed the call's allowance; that request was not sent."""

    def __init__(self, limit: int, reached: int) -> None:
        super().__init__("The traversal exhausted its outbound request allowance")
        self.details = {"bound": "requests", "limit": limit, "reached": reached}


@dataclass
class Budget:
    depth: int = DEFAULT_MAX_DEPTH
    requests: int = MAX_TRAVERSAL_REQUESTS
    nodes: int = DEFAULT_MAX_NODES
    edges: int = DEFAULT_MAX_EDGES
    per_kind: int | None = None
    # Hierarchy uses the node allowance as a page window, not an omission bound.
    paged: bool = False
    attempts: int = field(default=0, init=False)
    added_by_kind: Counter[str] = field(default_factory=Counter, init=False)
    exhausted: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        self.depth, self.nodes = clamp_limits(self.depth, self.nodes)
        self.edges = clamp_edge_limit(self.edges)
        if self.per_kind is not None:
            self.per_kind = min(self.per_kind, HARD_MAX_PER_KIND)

    def check_request(self) -> None:
        if self.attempts >= self.requests:
            self.exhausted = True
            raise RequestBudgetError(self.requests, self.attempts)

    def request(self) -> None:
        self.check_request()
        self.attempts += 1

    def node_bound(self, total: int, kind: str) -> str | None:
        if total >= self.nodes:
            return "nodes"
        if self.per_kind is not None and self.added_by_kind[kind] >= self.per_kind:
            return "kind_budget"
        return None


_budget: ContextVar[Budget | None] = ContextVar("traversal_budget", default=None)


def current_budget() -> Budget | None:
    return _budget.get()


@contextmanager
def budgeted(budget: Budget) -> Iterator[None]:
    token = _budget.set(budget)
    try:
        yield
    finally:
        _budget.reset(token)
