"""Controller-owned lifecycle and sanitized summary for explicit diagnostic callers."""

from __future__ import annotations

import subprocess
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Self

from nci_si_acceptance.owned_process import stop_child
from nci_si_acceptance.request_authorization import Authorization
from nci_si_acceptance.request_budget import ControlledRun
from nci_si_acceptance.request_summary import RunSummary, project, publish
from nci_si_mcp.http_client import RequestPolicyError
from nci_si_mcp.validation import ChildRole, SummaryState

# One missed pulse is tolerated by the ledger's two-second stale deadline.
PULSE_SECONDS = 1.0
JOIN_SECONDS = 2.0
STOP_SECONDS = 10.0


@dataclass(frozen=True, slots=True)
class OwnedChild:
    process: subprocess.Popen[bytes]
    participant: str
    role: ChildRole


class SupervisedRun:
    def __init__(self, run: ControlledRun) -> None:
        self.run = run
        self._state: SummaryState = "incomplete"
        self._entered = False
        self._initial_published = False
        self._finishing = False
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._children: list[OwnedChild] = []
        self._observer_error: RequestPolicyError | None = None
        self._observer = threading.Thread(target=self._observe, name="controlled-run-observer")

    @classmethod
    def create(cls, authorization: Authorization, owned_directory: Path) -> Self:
        run = ControlledRun.create(authorization, owned_directory)
        run.enable_supervision()
        return cls(run)

    def snapshot(self) -> RunSummary:
        return project(self.run.snapshot(), self.run.authorization.limits, self._state)

    def _publish(self) -> None:
        publish(self.run.directory / "summary.json", self.snapshot())

    def __enter__(self) -> Self:
        if self._entered:
            raise RequestPolicyError("closed")
        self._entered = True
        try:
            self._publish()
            self._initial_published = True
            self._observer.start()
            self.run.open()
        except BaseException as error:
            self._finish(error)
            raise
        return self

    def register_child(
        self, process: subprocess.Popen[bytes], *, participant: str, role: ChildRole
    ) -> None:
        with self._lock:
            if not self._entered or self._finishing or type(process) is not subprocess.Popen:
                raise RequestPolicyError("authorization")
            if role not in ("worker", "service") or self._registered(process, participant):
                raise RequestPolicyError("authorization")
            self.run.authorize_participant(participant)
            # The caller releases its start barrier only after this transfer succeeds.
            self._children.append(OwnedChild(process, participant, role))

    def _registered(self, process: subprocess.Popen[bytes], participant: str) -> bool:
        return any(
            child.process is process or child.participant == participant for child in self._children
        )

    def _observe(self) -> None:
        while not self._stop.wait(PULSE_SECONDS):
            try:
                self.run.heartbeat()
                with self._lock:
                    children = tuple(self._children)
                for child in children:
                    self._check_child(child)
            except RequestPolicyError as error:
                self._observer_error = error
                self._stop.set()

    def _check_child(self, child: OwnedChild) -> None:
        code = child.process.poll()
        if code is None:
            return
        if code != 0 or child.role == "service":
            self.run._deny("transport")
        if self.run.participant_pending(child.participant):
            self.run._deny("accounting")

    def _join(self) -> None:
        if self._observer.ident is not None:
            self._observer.join(timeout=JOIN_SECONDS)
            if self._observer.is_alive():
                raise RequestPolicyError("accounting")

    def _clean_child(self, child: OwnedChild) -> None:
        actions: list[Callable[[], None]] = [
            lambda: self._settle_child(child),
            lambda: stop_child(child.process, seconds=STOP_SECONDS),
        ]
        actions.extend(
            stream.close
            for stream in (child.process.stdin, child.process.stdout, child.process.stderr)
            if stream is not None
        )
        errors = self._collect(actions)
        if errors:
            raise BaseExceptionGroup("Controlled child cleanup failed.", errors) from None

    def _settle_child(self, child: OwnedChild) -> None:
        if child.role == "worker":
            try:
                child.process.wait(timeout=STOP_SECONDS)
            except subprocess.TimeoutExpired:
                self.run._deny("transport")
        self._check_child(child)

    @staticmethod
    def _collect(actions: list[Callable[[], None]]) -> list[BaseException]:
        errors: list[BaseException] = []
        for action in actions:
            try:
                action()
            except (BaseExceptionGroup, RequestPolicyError, KeyboardInterrupt, SystemExit) as error:
                errors.append(error)
            except OSError, RuntimeError, subprocess.SubprocessError:
                errors.append(RequestPolicyError("accounting"))
        return errors

    def _finish(self, initiating: BaseException | None) -> None:
        with self._lock:
            self._finishing = True
        # Observer never publishes: only the controller writes initial/final projections.
        self._stop.set()
        actions: list[Callable[[], None]] = [self.run.close, self._join]
        actions.extend(lambda child=child: self._clean_child(child) for child in self._children)
        errors = [initiating] if initiating is not None else []
        errors.extend(self._collect(actions))
        if self._observer_error is not None:
            errors.append(self._observer_error)
        errors.extend(self._collect([lambda: self._finalize(not errors)]))
        if len(errors) > (initiating is not None):
            raise BaseExceptionGroup("Controlled run failed.", errors) from None

    def _finalize(self, clean: bool) -> None:
        snapshot = self.run.snapshot()
        success = clean and snapshot.state == "closed" and snapshot.observation_complete
        self._state = "completed" if success else "failed"
        if self._initial_published:
            self._publish()

    def __exit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._finish(error)
