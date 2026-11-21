"""Atomic installer state and ordered step reconciliation."""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal, Protocol, cast

STATE_VERSION = 2
STATE_NAME = "installer-state.json"
LOCK_NAME = "installer-state.lock"
STATUSES = frozenset({"completed", "blocked", "failed"})
TARGETS = frozenset({"local"})
COMPONENTS = ("server",)
type Target = Literal["local"]
type Component = Literal["server"]
type StepStatus = Literal["completed", "blocked", "failed"]


class InstallerStateError(Exception):
    pass


class StepActions(Protocol):
    @property
    def step_names(self) -> tuple[str, ...]:
        """Return the executable steps in order."""
        ...

    def missing_inputs(self, step: str) -> tuple[str, ...]:
        """Return the missing inputs for one step."""
        ...

    def apply(self, step: str) -> None:
        """Apply one executable step."""
        ...

    def verify(self, step: str) -> bool:
        """Return whether one step is complete."""
        ...


@dataclass(frozen=True)
class StepOutcome:
    step: str
    status: StepStatus
    missing: tuple[str, ...] = ()


@dataclass(frozen=True)
class InstallationState:
    version: int
    target: Target
    components: tuple[Component, ...]
    outcomes: tuple[StepOutcome, ...]
    transport: str = "stdio"
    profile: str = "unified"


def _state_path(data_dir: Path) -> Path:
    return data_dir / STATE_NAME


def _malformed_state() -> InstallerStateError:
    return InstallerStateError("Installer state is malformed; inspect or replace the state file.")


def _string_list(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise _malformed_state()
    return tuple(value)


def _valid_missing(status: object, missing: tuple[str, ...]) -> bool:
    return all(isinstance(item, str) and item for item in missing) and (
        (status == "blocked") == bool(missing)
    )


def _parse_outcome(value: object) -> StepOutcome:
    if not isinstance(value, dict) or set(value) - {"step", "status", "missing"}:
        raise _malformed_state()
    step, status = value.get("step"), value.get("status")
    if not isinstance(step, str) or status not in STATUSES:
        raise _malformed_state()
    missing = _string_list(value.get("missing", []))
    if not _valid_missing(status, missing):
        raise _malformed_state()
    return StepOutcome(step, cast("StepStatus", status), missing)


def _valid_components(state: InstallationState) -> bool:
    canonical = tuple(item for item in COMPONENTS if item in state.components)
    return (
        state.target in TARGETS
        and state.components == canonical
        and state.components[:1] == ("server",)
    )


def _valid_local_selection(state: InstallationState) -> bool:
    return state.transport in ("stdio", "http") and state.profile in (
        "evs",
        "cadsr",
        "unified",
    )


def _valid_identity(state: InstallationState) -> bool:
    return type(state.version) is int and _valid_components(state) and _valid_local_selection(state)


def _decode_state(content: str) -> InstallationState:
    def unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
        value = dict(pairs)
        if len(value) != len(pairs):
            raise _malformed_state()
        return value

    value = json.loads(content, object_pairs_hook=unique)
    if not isinstance(value, dict):
        raise _malformed_state()
    version = value.get("version")
    expected = {"version", "target", "components", "outcomes"}
    if version != 1:
        expected |= {"transport", "profile"}
    if set(value) != expected:
        raise _malformed_state()
    outcomes = value["outcomes"]
    if not isinstance(outcomes, list):
        raise _malformed_state()
    state = InstallationState(
        version=value["version"],
        target=cast("Target", value["target"]),
        components=cast("tuple[Component, ...]", _string_list(value["components"])),
        outcomes=tuple(_parse_outcome(item) for item in outcomes),
        transport=cast("str", value.get("transport", "stdio")),
        profile=cast("str", value.get("profile", "unified")),
    )
    if not _valid_identity(state):
        raise _malformed_state()
    return state


def _read_state_text(path: Path, data_dir: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise _malformed_state() from None
    except FileNotFoundError:
        if path.is_symlink():
            raise InstallerStateError(f"Cannot read {path}; Fix access and retry.") from None
        raise InstallerStateError(
            f"There is no installation recorded in {data_dir}; run `run` first."
        ) from None
    except OSError:
        raise InstallerStateError(f"Cannot read {path}; Fix access and retry.") from None


def read_state(
    data_dir: Path,
    target: str,
    components: tuple[str, ...],
    *,
    transport: str = "stdio",
    profile: str = "unified",
) -> InstallationState:
    path = _state_path(data_dir)
    content = _read_state_text(path, data_dir)
    try:
        state = _decode_state(content)
    except json.JSONDecodeError, ValueError, InstallerStateError:
        raise _malformed_state() from None
    if state.version != STATE_VERSION:
        raise InstallerStateError(
            f"Installer state has unsupported version {state.version}; use a compatible installer."
        )
    if (
        state.target != target
        or state.components != components
        or state.transport != transport
        or state.profile != profile
    ):
        raise InstallerStateError(
            "Installer state belongs to another selection; use a new --data-dir."
        )
    return state


def _state_record(state: InstallationState) -> dict[str, object]:
    if (
        state.version != STATE_VERSION
        or not _valid_identity(state)
        or not all(
            _valid_outcome(outcome, index, len(state.outcomes) - 1)
            for index, outcome in enumerate(state.outcomes)
        )
    ):
        raise _malformed_state()
    outcomes = []
    for outcome in state.outcomes:
        record: dict[str, object] = {"step": outcome.step, "status": outcome.status}
        if outcome.missing:
            record["missing"] = list(outcome.missing)
        outcomes.append(record)
    return {
        "version": state.version,
        "target": state.target,
        "components": list(state.components),
        "transport": state.transport,
        "profile": state.profile,
        "outcomes": outcomes,
    }


def write_state(data_dir: Path, state: InstallationState) -> None:
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(
            prefix=".installer-state.", suffix=".tmp", dir=data_dir
        )
    except OSError:
        raise InstallerStateError(
            f"Cannot write state in {data_dir}; fix access and retry."
        ) from None
    temporary_path = Path(temporary)
    try:
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(_state_record(state), handle, separators=(",", ":"))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, _state_path(data_dir))
            directory = os.open(data_dir, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except OSError:
            raise InstallerStateError(
                f"Cannot write state in {data_dir}; fix access and retry."
            ) from None
    finally:
        with suppress(OSError):
            temporary_path.unlink(missing_ok=True)


@contextmanager
def state_lock(data_dir: Path) -> Iterator[None]:
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        handle = (data_dir / LOCK_NAME).open("a+b")
    except OSError:
        raise InstallerStateError(
            f"Cannot lock state in {data_dir}; fix access and retry."
        ) from None
    with handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise InstallerStateError(
                "Another installer is already running for this data directory."
            ) from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _initial_state(
    target: str, components: tuple[str, ...], transport: str, profile: str
) -> InstallationState:
    return InstallationState(
        STATE_VERSION,
        cast("Target", target),
        cast("tuple[Component, ...]", components),
        (),
        transport,
        profile,
    )


def _load_or_create(
    data_dir: Path, target: str, components: tuple[str, ...], transport: str, profile: str
) -> InstallationState:
    if os.path.lexists(_state_path(data_dir)):
        return read_state(data_dir, target, components, transport=transport, profile=profile)
    state = _initial_state(target, components, transport, profile)
    write_state(data_dir, state)
    return state


def _verified_prefix(state: InstallationState, actions: StepActions) -> tuple[StepOutcome, ...]:
    verified = []
    for outcome in state.outcomes:
        if outcome.status != "completed" or not actions.verify(outcome.step):
            break
        verified.append(outcome)
    return tuple(verified)


def _valid_outcome(outcome: StepOutcome, index: int, last: int) -> bool:
    terminal = outcome.status in ("blocked", "failed")
    return (
        bool(outcome.step)
        and outcome.status in STATUSES
        and _valid_missing(outcome.status, outcome.missing)
        and (not terminal or index == last)
    )


def validate_outcomes(state: InstallationState, step_names: tuple[str, ...]) -> None:
    names = tuple(outcome.step for outcome in state.outcomes)
    if len(set(step_names)) != len(step_names) or names != step_names[: len(names)]:
        raise _malformed_state()
    last = len(state.outcomes) - 1
    for index, outcome in enumerate(state.outcomes):
        if not _valid_outcome(outcome, index, last):
            raise _malformed_state()


def reconcile(
    data_dir: Path,
    target: str,
    components: tuple[str, ...],
    actions: StepActions,
    *,
    restart: bool = False,
    transport: str = "stdio",
    profile: str = "unified",
) -> InstallationState:
    with state_lock(data_dir):
        state = _load_or_create(data_dir, target, components, transport, profile)
        validate_outcomes(state, actions.step_names)
        outcomes = () if restart else _verified_prefix(state, actions)
        state = replace(state, outcomes=outcomes)
        write_state(data_dir, state)
        for step in actions.step_names[len(outcomes) :]:
            missing = actions.missing_inputs(step)
            if missing:
                state = replace(
                    state, outcomes=(*state.outcomes, StepOutcome(step, "blocked", missing))
                )
                write_state(data_dir, state)
                return state
            actions.apply(step)
            if not actions.verify(step):
                missing = actions.missing_inputs(step)
                status: StepStatus = "blocked" if missing else "failed"
                outcome = StepOutcome(step, status, missing)
                state = replace(state, outcomes=(*state.outcomes, outcome))
                write_state(data_dir, state)
                return state
            state = replace(state, outcomes=(*state.outcomes, StepOutcome(step, "completed")))
            write_state(data_dir, state)
        return state


def reverify(state: InstallationState, actions: StepActions) -> InstallationState:
    outcomes: tuple[StepOutcome, ...] = ()
    for previous in state.outcomes:
        if previous.status == "completed" and actions.verify(previous.step):
            outcomes = (*outcomes, previous)
            continue
        missing = actions.missing_inputs(previous.step)
        if missing:
            outcomes = (*outcomes, StepOutcome(previous.step, "blocked", missing))
            break
        valid = actions.verify(previous.step)
        outcomes = (*outcomes, StepOutcome(previous.step, "completed" if valid else "failed"))
        if not valid:
            break
    return replace(state, outcomes=outcomes)


def _next_action(
    blocked: list[dict[str, object]], failed: list[str], pending: list[str]
) -> str | None:
    if failed:
        return f"Retry failed step {failed[0]}."
    if blocked:
        missing = blocked[0]["missing"]
        if isinstance(missing, list):
            return "Resolve the failed prerequisites: " + "; ".join(missing)
    if pending:
        return f"Run {pending[0]}."
    return None


def _steps_with(state: InstallationState, status: str) -> list[str]:
    return [outcome.step for outcome in state.outcomes if outcome.status == status]


def _blocked_steps(state: InstallationState) -> list[dict[str, object]]:
    return [
        {"step": outcome.step, "missing": list(outcome.missing)}
        for outcome in state.outcomes
        if outcome.status == "blocked"
    ]


def current_status(state: InstallationState, actions: StepActions) -> dict[str, object]:
    completed = _steps_with(state, "completed")
    failed = _steps_with(state, "failed")
    blocked = _blocked_steps(state)
    recorded = {outcome.step for outcome in state.outcomes}
    pending = [step for step in actions.step_names if step not in recorded]
    return {
        "components": list(state.components),
        "completed": completed,
        "failed": failed,
        "blocked": blocked,
        "pending": pending,
        "nextAction": _next_action(blocked, failed, pending),
    }
