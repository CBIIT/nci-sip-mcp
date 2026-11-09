"""Controller-owned lifecycle and sanitized summary for explicit diagnostic callers."""

from __future__ import annotations

import subprocess
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Self, get_args

from nci_si_acceptance.owned_process import stop_child
from nci_si_acceptance.request_authorization import Authorization
from nci_si_acceptance.request_budget import ControlledRun
from nci_si_acceptance.request_summary import RunSummary, project, publish
from nci_si_mcp.http_client import RequestPolicyError
from nci_si_mcp.validation import ChildRole, PolicyReason, SummaryState

# One missed pulse is tolerated by the ledger's two-second stale deadline.
PULSE_SECONDS = 1.0
JOIN_SECONDS = 2.0
STOP_SECONDS = 10.0
ROLES = get_args(ChildRole.__value__)


@dataclass(frozen=True, slots=True)
class OwnedChild:
    process: subprocess.Popen[bytes]
    participant: str
    role: ChildRole


class SupervisedRun:
    def __init__(self, run: ControlledRun) -> None:
        if type(run) is not ControlledRun:
            raise RequestPolicyError("authorization")
        run.require_supervision_owner()
        self.run = run
        self._state: SummaryState = "incomplete"
        self._entered = False
        self._initial_published = False
        self._finishing = False
        self._stop = threading.Event()
        self._registrations: list[OwnedChild | None] = []
        self._child_errors: dict[subprocess.Popen[bytes], RequestPolicyError] = {}
        self._observer_error: RequestPolicyError | None = None
        self._observer_cleanup_error: RequestPolicyError | None = None
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
        self.run.require_supervision_owner()
        self._entered = True
        try:
            self._publish()
            self._initial_published = True
            self._start_observer()
            self.run.open()
        except BaseException as error:
            self._finish(error)
            raise
        return self

    def _start_observer(self) -> None:
        try:
            self._observer.start()
        except OSError, RuntimeError:
            raise RequestPolicyError("accounting") from None

    def register_child(
        self, process: subprocess.Popen[bytes], *, participant: str, role: ChildRole
    ) -> None:
        if any(
            (
                not self._entered,
                self._finishing,
                type(process) is not subprocess.Popen,
                role not in ROLES,
            )
        ):
            raise RequestPolicyError("authorization")
        self.run.authorize_participant(participant)
        child = OwnedChild(process, participant, role)
        if self._registered(process, participant):
            raise RequestPolicyError("authorization")
        # Atomic append orders a complete offer against shutdown's seal. No check/act gap.
        self._registrations.append(child)
        if not any(accepted is child for accepted in self._children):
            raise RequestPolicyError("authorization")

    @property
    def _children(self) -> tuple[OwnedChild, ...]:
        accepted: list[OwnedChild] = []
        # Python 3.14 list.copy/append are atomic; iteration over the live list is not.
        for child in self._registrations.copy():
            if child is None:
                break
            if not any(
                old.process is child.process or old.participant == child.participant
                for old in accepted
            ):
                accepted.append(child)
        return tuple(accepted)

    def _registered(self, process: subprocess.Popen[bytes], participant: str) -> bool:
        return any(
            child.process is process or child.participant == participant for child in self._children
        )

    def _observe(self) -> None:
        while not self._stop.wait(PULSE_SECONDS):
            if self._finishing:
                return
            try:
                self._pulse()
            except RequestPolicyError:
                # The ledger retains the original reason; a pulse is not a second event.
                self._stop.set()
            except OSError, RuntimeError, subprocess.SubprocessError:
                self._inspection_failed()

    def _pulse(self) -> None:
        self.run.heartbeat()
        for child in tuple(self._children):
            self._check_child(child)

    def _inspection_failed(self) -> None:
        self._observer_error = RequestPolicyError("accounting")
        try:
            self._record_failure(self._observer_error, "accounting")
        except RequestPolicyError as error:
            self._observer_cleanup_error = error
        self._stop.set()

    def _check_child(self, child: OwnedChild) -> None:
        code = child.process.poll()
        if code is None:
            return
        if code != 0 or child.role == "service":
            reason: PolicyReason = "transport"
        elif self.run.participant_pending(child.participant):
            reason = "accounting"
        else:
            return
        error = RequestPolicyError(reason)
        if self._child_errors.setdefault(child.process, error) is error:
            self._record_failure(error, reason)
            raise error

    def _record_failure(self, error: BaseException, reason: PolicyReason) -> None:
        # Exclusive marker creation proves which event owns the ledger's first failure.
        self.run._mark_failed(reason, event=error)

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
            if len(errors) == 1:
                raise errors[0] from None
            raise BaseExceptionGroup("Controlled child cleanup failed.", errors) from None

    def _settle_child(self, child: OwnedChild) -> None:
        if child.role == "worker":
            try:
                child.process.wait(timeout=STOP_SECONDS)
            except subprocess.TimeoutExpired:
                self.run._deny("transport")
        self._check_child(child)

    @staticmethod
    def _collect(actions: Iterable[Callable[[], object]]) -> list[BaseException]:
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
        self._finishing = True
        # Observer never publishes: only the controller writes initial/final projections.
        errors = [initiating] if initiating is not None else []
        self._cleanup(errors, terminal=initiating is None)
        errors.extend(self._collect([lambda: self._finalize(not errors)]))
        if len(errors) > (initiating is not None):
            if len(errors) == 1:
                raise errors[0] from None
            raise BaseExceptionGroup("Controlled run failed.", errors) from None

    def _cleanup(self, errors: list[BaseException], *, terminal: bool) -> None:
        errors.extend(self._collect([self._close_admission]))
        errors.extend(self._collect([self._seal_registry]))
        self._stop.set()
        errors.extend(self._collect([self._join]))
        if terminal:
            known = self._collect([self._terminal_failure])
            errors[:0] = [error for error in known if not self._contains(errors, error)]
        actions: list[Callable[[], None]] = [
            lambda child=child: self._clean_child(child) for child in self._children
        ]
        errors.extend(self._collect(actions))
        errors.extend(self._observed_failures(errors))

    def _observed_failures(self, errors: list[BaseException]) -> list[BaseException]:
        observed = list(self._child_errors.values())
        if self._observer_error is not None:
            observed.append(self._observer_error)
        if self._observer_cleanup_error is not None:
            observed.append(self._observer_cleanup_error)
        return [error for error in observed if not self._contains(errors, error)]

    def _seal_registry(self) -> None:
        self._registrations.append(None)

    def _terminal_failure(self) -> None:
        failure = self.run.snapshot().failure
        if failure is not None:
            raise self.run._first_failure or RequestPolicyError(failure)

    @staticmethod
    def _contains(
        errors: list[BaseException] | tuple[BaseException, ...], event: BaseException
    ) -> bool:
        return any(
            error is event
            or (
                isinstance(error, BaseExceptionGroup)
                and SupervisedRun._contains(error.exceptions, event)
            )
            for error in errors
        )

    def _close_admission(self) -> None:
        errors = self._collect([self.run.close])
        if errors:
            errors.extend(self._collect([lambda: self._record_failure(errors[0], "accounting")]))
            if len(errors) == 1:
                raise errors[0] from None
            raise BaseExceptionGroup("Controlled admission closure failed.", errors) from None

    def _finalize(self, clean: bool) -> None:
        snapshot = self.run.snapshot()
        success = all((clean, snapshot.state == "closed", snapshot.observation_complete))
        self._state = "completed" if success else "failed"
        if self._initial_published:
            self._publish()
        if clean and (snapshot.state == "failed" or not snapshot.observation_complete):
            raise RequestPolicyError(snapshot.failure or "accounting")

    def __exit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._finish(error)
