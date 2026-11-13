"""Explicit single-node native admission and authenticated furnished-child attachment."""

from __future__ import annotations

import inspect
import json
import os
import sys
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from nci_si_acceptance.environments import (
    LAUNCHER,
    NATIVE_HANDOFF,
    Environment,
    _record_map,
    _selected,
    environment_record,
)
from nci_si_acceptance.request_authorization import RunDescriptor
from nci_si_acceptance.request_budget import ControlledRun
from nci_si_acceptance.request_catalogue import compile_policy
from nci_si_acceptance.request_supervision import SupervisedRun
from nci_si_mcp.errors import RequestPolicyError
from nci_si_mcp.request_policy import using_request_policy

if TYPE_CHECKING:
    from collections.abc import Iterator

    import pytest

    from nci_si_acceptance.client import Target
    from nci_si_acceptance.denied_surfaces import DeniedSurfaces
    from nci_si_acceptance.request_children import CleanupDeadline

NODE = "tests/test_evs.py::test_a_discovered_release_identifies_the_concept_and_its_provenance"
REFUSAL = "controlled run requires the exact supported serial selection"
PARTICIPANT = "native-serial"


def _selection(config: pytest.Config, target: Target) -> Environment:
    # Inspection/setup modes preserve items without executing them; debuggers can wait for input.
    blocked = (
        "collectonly",
        "keyword",
        "markexpr",
        "deselect",
        "numprocesses",
        "tx",
        "pyargs",
        "setuponly",
        "setupplan",
        "showfixtures",
        "show_fixtures_per_test",
        "usepdb",
        "trace",
    )
    valid = (
        target.mode == "live",
        target.command == [*LAUNCHER, "serve"],
        not any((target.url, target.prepare, target.prepared, target.security_server)),
        config.args == [NODE],
        bool(config.getoption("report")),
        not any(config.getoption(name, None) for name in blocked),
        config.getoption("dist", "no") in (None, "no"),
        not hasattr(config, "workerinput"),
    )
    if not all(valid):
        raise ValueError(REFUSAL)
    if target.environment is None:
        raise ValueError(REFUSAL)
    return target.environment


class NativeRun:
    name = "controlled-native-refusal"

    def __init__(self, config: pytest.Config, target: Target) -> None:
        self.config, self.target = config, target
        self.controller: SupervisedRun | None = None
        self.listener: DeniedSurfaces | None = None
        self.session: pytest.Session | None = None
        self.errors: list[BaseException] = []
        self.entered = self.done = self.ready = self.report_failed = False
        config.add_cleanup(self.fallback)
        self.environment = _selection(config, target)
        directory = config.getoption("controlled_run_dir")
        if not isinstance(directory, str) or not directory:
            raise ValueError(REFUSAL)
        self.directory = Path(directory)
        self._capture_identity()
        self._publish()

    def _capture_identity(self) -> None:
        from nci_si_acceptance.report import COLLECTOR, SUITE, suite_state  # noqa: PLC0415

        self.config.stash[SUITE] = suite_state(self.config)
        self.config.stash[COLLECTOR].environment = self.environment.to_record()
        self.ready = True

    def _publish(self) -> None:
        from nci_si_acceptance.report import write_report  # noqa: PLC0415

        try:
            write_report(self.config, "live", atomic=True)
        except BaseException:
            self.report_failed = True
            raise

    def start(self, session: pytest.Session) -> Target:
        from nci_si_acceptance.denied_surfaces import DeniedSurfaces  # noqa: PLC0415

        self.session = session
        if [item.nodeid for item in session.items] != [NODE]:
            raise ValueError(REFUSAL)
        policy = compile_policy(self.environment)
        owner = SupervisedRun.create(policy.authorization, self.directory)
        self.controller = owner
        owner.__enter__()
        self.entered = True
        owner.register_completion(self, participant="native-refusal")
        self.listener = DeniedSurfaces()
        denied_url = self.listener.__enter__()
        policy.select_release(owner.run, channel="monthly")
        owner.run.authorize_participant(PARTICIPANT)
        path = owner.run.directory / "native.json"
        record = {
            "environment": self.environment.to_record(),
            "run": owner.run.descriptor.to_record(),
            "participant": PARTICIPANT,
        }
        with path.open("x", encoding="utf-8") as stream:
            path.chmod(0o600)
            json.dump(record, stream)
        self.target = replace(
            self.target, denied_url=denied_url, controller=owner, handoff=str(path)
        )
        return self.target

    def finish(self, deadline: CleanupDeadline) -> None:
        if self.listener is not None:
            self.listener.finish(deadline)

    def before_teardown(self) -> None:
        if self.entered and self.controller is not None:
            self.errors.extend(self.controller._collect([self.controller._close_admission]))

    def _finish_controller(self, errors: list[BaseException]) -> list[BaseException]:
        owner = self.controller
        if owner is None or not self.entered:
            return errors
        initiating = BaseExceptionGroup("Native lifecycle failed.", errors) if errors else None
        failures = owner._collect(
            [
                lambda: owner.__exit__(
                    type(initiating) if initiating is not None else None, initiating, None
                )
            ]
        )
        return [error for error in errors if not owner._contains(failures, error)] + failures

    def _synchronize(self, session: pytest.Session | None, failed: bool) -> None:
        from nci_si_acceptance.report import COLLECTOR  # noqa: PLC0415

        if session is None:
            return
        if failed and session.exitstatus == 0:
            session.exitstatus = 1
        run = self.config.stash[COLLECTOR].run
        if run is not None:
            run["exit_status"] = int(session.exitstatus)

    def complete(self, session: pytest.Session | None = None) -> None:
        if self.done:
            return
        self.done = True
        errors = self._finish_controller(self.errors.copy())
        self._synchronize(session, bool(errors))
        if self.ready and not self.report_failed:
            try:
                self._publish()
            except BaseException as error:  # noqa: BLE001 - preserve publication and initiating failures.
                errors.append(error)
        if errors:
            raise BaseExceptionGroup("Native controlled run failed.", errors)

    def fallback(self) -> None:
        self.complete(self.session)


def _furnished() -> None:
    root = Path(__file__).resolve().parents[3]
    if Path(sys.prefix).resolve() != (root / ".venv").resolve():
        raise RequestPolicyError("authorization")
    functions = (
        (_handoff_run, "acceptance/src/nci_si_acceptance/request_native.py"),
        (compile_policy, "acceptance/src/nci_si_acceptance/request_catalogue.py"),
        (ControlledRun.attach, "acceptance/src/nci_si_acceptance/request_budget.py"),
        (RunDescriptor.from_record, "acceptance/src/nci_si_acceptance/request_authorization.py"),
    )
    for function, relative in functions:
        module = inspect.getmodule(function)
        expected = root / relative
        if module is None or module.__file__ is None:
            raise RequestPolicyError("authorization")
        if (
            Path(module.__file__).resolve() != expected
            or Path(inspect.getfile(function)).resolve() != expected
        ):
            raise RequestPolicyError("authorization")


def _handoff_run(path: str) -> ControlledRun:
    _furnished()
    try:
        data = _record_map(
            json.loads(Path(path).read_text(encoding="utf-8")),
            {"environment", "run", "participant"},
        )
        record = environment_record(data["environment"])
        environment = _selected(record["name"])
        if record != environment.to_record() or data["participant"] != PARTICIPANT:
            raise RequestPolicyError("authorization")
        policy = compile_policy(environment)
        return ControlledRun.attach(
            RunDescriptor.from_record(data["run"]), policy.authorization, participant=PARTICIPANT
        )
    except OSError, ValueError, TypeError:
        raise RequestPolicyError("authorization") from None


@contextmanager
def native_policy() -> Iterator[None]:
    if (path := os.environ.get(NATIVE_HANDOFF)) is None:
        yield
        return
    run = _handoff_run(path)
    # Operational imports and client construction follow authenticated attachment.
    from nci_si_acceptance.request_transport import ControlledHttpPolicy  # noqa: PLC0415

    with using_request_policy(ControlledHttpPolicy(run)):
        yield
