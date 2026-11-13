"""Run-local resource custody; completion is an owner's work, never a success boolean."""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass, field
from typing import Protocol

from nci_si_mcp.errors import RequestPolicyError

# A diagnostic cleanup gets the existing ten-second TERM and final-wait allowances once.
# Re-measure with retained owned-child evidence before widening this shared run allowance.
CLEANUP_SECONDS = 20.0


@dataclass(frozen=True, slots=True)
class CleanupDeadline:
    end: float | None = field(default_factory=lambda: time.monotonic() + CLEANUP_SECONDS)

    def remaining(self) -> float:
        if self.end is None:
            return 0.0  # Failed clock acquisition never receives a replacement allowance.
        now = time.monotonic()
        if not math.isfinite(now) or not math.isfinite(self.end):
            raise RequestPolicyError("accounting")
        return max(0.0, self.end - now)


class CompletionOwner(Protocol):
    name: str

    def finish(self, deadline: CleanupDeadline) -> None:
        """Positively settle the actual resources, or retain custody and raise."""
        ...


@dataclass(frozen=True, slots=True, eq=False)
class CompletionToken:
    run_id: str
    policy_sha256: str
    participant: str


class Completions:
    def __init__(self, run_id: str, policy_sha256: str) -> None:
        self._identity = run_id, policy_sha256
        self._lock = threading.Lock()
        self._sealed = False
        self._used = False
        self._owners: dict[CompletionToken, tuple[CompletionOwner, str]] = {}
        self._settling: set[CompletionToken] = set()

    @property
    def used(self) -> bool:
        with self._lock:
            return self._used

    @property
    def pending(self) -> tuple[str, ...]:
        with self._lock:
            owners = tuple(self._owners.values())
        return tuple(owner.name for owner, _participant in owners)

    def register(self, owner: CompletionOwner, participant: str) -> CompletionToken:
        with self._lock:
            if self._sealed or any(known is owner for known, _participant in self._owners.values()):
                raise RequestPolicyError("authorization")
            token = CompletionToken(*self._identity, participant)
            self._owners[token] = owner, participant
            self._used = True
            return token

    def seal(self) -> tuple[CompletionToken, ...]:
        with self._lock:
            self._sealed = True
            return tuple(self._owners)

    def settle(self, token: CompletionToken, deadline: CleanupDeadline) -> None:
        if type(token) is not CompletionToken:
            raise RequestPolicyError("authorization")
        with self._lock:
            if token not in self._owners or token in self._settling:
                raise RequestPolicyError("authorization")
            owner, participant = self._owners[token]
            if (token.run_id, token.policy_sha256, token.participant) != (
                *self._identity,
                participant,
            ):
                raise RequestPolicyError("authorization")
            self._settling.add(token)
        try:
            owner.finish(deadline)
            with self._lock:
                del self._owners[token]
        except BaseException as error:  # noqa: BLE001 - owner re-raises every captured failure.
            raise BaseExceptionGroup("Owned resource completion failed.", [error]) from None
        finally:
            with self._lock:
                self._settling.remove(token)
