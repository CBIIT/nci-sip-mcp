"""Controller-owned lifecycle and sanitized summary for explicit diagnostic callers."""

from __future__ import annotations

import subprocess
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from pathlib import Path
from types import TracebackType
from typing import Self, get_args

from nci_si_acceptance.owned_process import stop_child
from nci_si_acceptance.request_authorization import Authorization
from nci_si_acceptance.request_budget import ControlledRun
from nci_si_acceptance.request_children import (
    CleanupDeadline,
    CompletionOwner,
    Completions,
    CompletionToken,
)
from nci_si_acceptance.request_summary import RunSummary, project, publish
from nci_si_mcp.errors import RequestPolicyError
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
        self._recording_errors: list[RequestPolicyError] = []
        self._observer = threading.Thread(target=self._observe, name="controlled-run-observer")
        self._completions = Completions(run.descriptor.run_id, run.descriptor.policy_sha256)
        self._controlled_cleanup = False
        self._deadline: CleanupDeadline | None = None
        self._deadline_lock = threading.Lock()
        self._resources_complete = False
        self._closing_threads: list[threading.Thread] = []
        self._pending_children: set[str] = set()

    @classmethod
    def create(cls, authorization: Authorization, owned_directory: Path) -> Self:
        run = ControlledRun.create(authorization, owned_directory)
        run.enable_supervision()
        return cls(run)

    def snapshot(self) -> RunSummary:
        snapshot = self.run.snapshot()
        if self._controlled_cleanup:
            snapshot = replace(
                snapshot,
                observation_complete=(
                    snapshot.observation_complete
                    and self._resources_complete
                    and not self.pending_owners
                ),
            )
        return project(snapshot, self.run.authorization.limits, self._state)

    @property
    def pending_owners(self) -> tuple[str, ...]:
        return self._completions.pending + tuple(sorted(self._pending_children))

    def register_completion(self, owner: CompletionOwner, *, participant: str) -> CompletionToken:
        if not self._entered or self._finishing:
            raise RequestPolicyError("authorization")
        self.run.authorize_participant(participant)
        token = self._completions.register(owner, participant)
        self._controlled_cleanup = True
        return token

    def cleanup_deadline(self) -> CleanupDeadline:
        with self._deadline_lock:
            if self._deadline is None:
                self._deadline = CleanupDeadline()
            return self._deadline

    def _remaining(self, seconds: float) -> float:
        return seconds if self._deadline is None else min(seconds, self._deadline.remaining())

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
        self._record_failure(self._observer_error, "accounting")
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
        try:
            self.run._mark_failed(reason, event=error)
        except RequestPolicyError as recording_error:
            self._recording_errors.append(recording_error)

    def _join(self) -> None:
        if self._observer.ident is not None:
            self._observer.join(timeout=self._remaining(JOIN_SECONDS))
            if self._observer.is_alive():
                raise RequestPolicyError("accounting")

    def _clean_child(self, child: OwnedChild) -> None:
        actions: list[Callable[[], None]] = [
            lambda: self._settle_child(child),
            lambda: (
                stop_child(child.process, seconds=STOP_SECONDS, deadline=self._deadline)
                if self._controlled_cleanup
                else stop_child(child.process, seconds=STOP_SECONDS)
            ),
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
                child.process.wait(timeout=self._remaining(STOP_SECONDS))
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
            except BaseException as error:  # noqa: BLE001 - preserve unexpected initiating/cleanup events.
                errors.append(error)
        return errors

    def _finish(self, initiating: BaseException | None) -> None:
        self._finishing = True
        # Observer never publishes: only the controller writes initial/final projections.
        errors = [initiating] if initiating is not None else []
        self._cleanup(errors, terminal=initiating is None)
        errors.extend(self._collect([lambda: self._finalize(not errors)]))
        if errors:
            self._state = "failed"
        if len(errors) > (initiating is not None):
            if len(errors) == 1:
                raise errors[0] from None
            raise BaseExceptionGroup("Controlled run failed.", errors) from None

    def _cleanup(self, errors: list[BaseException], *, terminal: bool) -> None:
        if self._controlled_cleanup:
            self.cleanup_deadline()
        errors.extend(self._collect([self._close_admission]))
        errors.extend(self._collect([self._seal_registry]))
        self._stop.set()
        resources = self._collect([self._join])
        if terminal:
            known = self._collect([self._terminal_failure])
            errors[:0] = [error for error in known if not self._contains(errors, error)]
        actions: list[Callable[[], None]] = [
            lambda child=child: self._finish_child(child) for child in self._children
        ]
        resources.extend(self._collect(actions))
        if self._controlled_cleanup:
            resources.extend(self._settle_completions())
            self._resources_complete = not resources
        errors.extend(resources)
        errors.extend(self._observed_failures(errors))

    def _settle_completions(self) -> list[BaseException]:
        deadline = self.cleanup_deadline()
        return self._collect(
            lambda token=token: self._completions.settle(token, deadline)
            for token in self._completions.seal()
        )

    def _finish_child(self, child: OwnedChild) -> None:
        if not self._controlled_cleanup:
            self._clean_child(child)
            return
        errors: list[BaseException] = []
        thread = threading.Thread(
            target=lambda: self._capture_child_cleanup(child, errors),
            name=f"controlled-child-close-{child.participant}",
        )
        self._closing_threads.append(thread)
        self._pending_children.add(thread.name)
        thread.start()
        thread.join(self.cleanup_deadline().remaining())
        if thread.is_alive():
            raise RequestPolicyError("accounting")
        if errors:
            raise BaseExceptionGroup("Controlled child cleanup failed.", errors)
        self._pending_children.remove(thread.name)

    def _capture_child_cleanup(self, child: OwnedChild, errors: list[BaseException]) -> None:
        try:
            self._clean_child(child)
        except BaseException as error:  # noqa: BLE001 - outside owner re-raises thread failures.
            errors.append(error)

    def _observed_failures(self, errors: list[BaseException]) -> list[BaseException]:
        observed = list(self._child_errors.values())
        if self._observer_error is not None:
            observed.append(self._observer_error)
        observed.extend(self._recording_errors)
        return [error for error in observed if not self._contains(errors, error)]

    def _seal_registry(self) -> None:
        self._registrations.append(None)
        self._completions.seal()

    def _terminal_failure(self) -> None:
        self.run.snapshot()  # Validate private identity and counters before selecting the event.
        failure, event = self.run._failure_record()
        if failure is not None:
            raise event or RequestPolicyError(failure)

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
        complete = self.snapshot()["observationComplete"]
        success = all((clean, snapshot.state == "closed", complete))
        self._state = "completed" if success else "failed"
        if self._initial_published:
            self._publish()
        if clean and (snapshot.state == "failed" or not complete):
            raise RequestPolicyError(snapshot.failure or "accounting")

    def __exit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._finish(error)
