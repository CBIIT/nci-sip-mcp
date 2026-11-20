"""One local owner, one active validation worker and a bounded durable job queue."""

from __future__ import annotations

import fcntl
import json
import logging
import re
import shutil
import subprocess
import threading
from collections.abc import Callable
from contextlib import ExitStack
from copy import deepcopy
from dataclasses import asdict, dataclass, fields
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast, get_args

from scripts.evidence_envelope import MAX_EXIT_CODE
from scripts.formats import JOB_ID, SHA1_HEX
from scripts.operator_files import read_bytes, write_json
from scripts.operator_process import MAX_WORKER_LOG_BYTES, WORKER_LOG
from scripts.portal_store import MAX_RETENTION
from scripts.vocabulary import (
    ACTIVE,
    JOB_REASONS,
    TERMINAL,
    ActiveJobState,
    JobOutcome,
    JobReason,
    TerminalJobState,
)

from nci_sip_mcp.audit import emit

Profile = Literal["acceptance-http-fixture", "benchmark-http-fixture", "benchmark-http-remote"]
PROFILES = get_args(Profile)
MAX_ADMITTED = 3
MAX_STATE_BYTES = 2 * 1024 * 1024
MAX_TIMESTAMP_LENGTH = 40
Executor = Callable[[dict[str, Any], Path, threading.Event], JobOutcome]


class JobRecordError(ValueError):
    """A persisted field is invalid; the message contains only a declared field path."""


def _require(condition: bool, path: str) -> None:
    if not condition:
        raise JobRecordError(f"Invalid job field: {path}")


def _shape(value: Any, record: type, path: str) -> None:
    _require(isinstance(value, dict), path or "state")
    names = {item.name for item in fields(record)}
    for name in sorted(names):
        _require(name in value, f"{path}.{name}".lstrip("."))
    _require(set(value) == names, path or "state")


@dataclass(frozen=True)
class Job:
    run_id: str
    profile: Profile
    state: ActiveJobState | TerminalJobState
    sequence: int
    commit: str
    submitted_at: str
    started_at: str | None
    finished_at: str | None
    exit_code: int | None
    reason: JobReason | None

    @classmethod
    def from_dict(cls, value: Any, path: str) -> Job:
        _shape(value, cls, path)
        _format(value["run_id"], JOB_ID, f"{path}.run_id")
        _require(value["profile"] in PROFILES, f"{path}.profile")
        _require(
            isinstance(value["state"], str) and value["state"] in ACTIVE | TERMINAL, f"{path}.state"
        )
        _require(type(value["sequence"]) is int and value["sequence"] >= 1, f"{path}.sequence")
        _format(value["commit"], SHA1_HEX, f"{path}.commit")
        _validate_execution(value, path)
        return cls(**value)


@dataclass(frozen=True)
class JobState:
    next: int
    jobs: list[Job]

    @classmethod
    def from_dict(cls, value: Any) -> JobState:
        _shape(value, cls, "")
        _require(type(value["next"]) is int, "next")
        _require(isinstance(value["jobs"], list), "jobs")
        jobs = [Job.from_dict(row, f"jobs[{index}]") for index, row in enumerate(value["jobs"])]
        _validate_sequences(value)
        return cls(value["next"], jobs)


def _format(value: Any, pattern: str, path: str) -> None:
    _require(isinstance(value, str) and re.fullmatch(pattern, value) is not None, path)


class JobConflictError(ValueError):
    """A retained submission ID already identifies a different intent."""


class QueueFullError(RuntimeError):
    """The local worker and queue have reached their bounded admission count."""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _identifier(value: str) -> None:
    if re.fullmatch(JOB_ID, value) is None:
        raise ValueError("Invalid local job identifier")


def _validate_sequences(value: dict[str, Any]) -> None:
    for name in ("run_id", "sequence"):
        seen = set()
        for index, row in enumerate(value["jobs"]):
            _require(row[name] not in seen, f"jobs[{index}].{name}")
            seen.add(row[name])
    _require(value["next"] > max((row["sequence"] for row in value["jobs"]), default=0), "next")
    _require(len(value["jobs"]) <= MAX_RETENTION + MAX_ADMITTED, "jobs")


def _validate_execution(row: dict[str, Any], path: str) -> None:
    _timestamp(row["submitted_at"], f"{path}.submitted_at")
    for name in ("started_at", "finished_at"):
        if row[name] is not None:
            _timestamp(row[name], f"{path}.{name}")
    _validate_outcome(row, path)
    _validate_phase(row, path)


def _validate_phase(row: dict[str, Any], path: str) -> None:
    state = row["state"]
    if state in ACTIVE:
        for name in ("finished_at", "exit_code", "reason"):
            _require(row[name] is None, f"{path}.{name}")
        _require((row["started_at"] is not None) == (state == "running"), f"{path}.started_at")
    else:
        _require(row["finished_at"] is not None, f"{path}.finished_at")
    if state == "completed":
        _require(row["exit_code"] == 0, f"{path}.exit_code")
        _require(row["reason"] is None, f"{path}.reason")
        _require(row["started_at"] is not None, f"{path}.started_at")


def _timestamp(value: Any, path: str) -> None:
    _require(isinstance(value, str) and len(value) <= MAX_TIMESTAMP_LENGTH, path)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise JobRecordError(f"Invalid job field: {path}") from None
    _require(parsed.utcoffset() == UTC.utcoffset(None), path)


def _validate_outcome(row: dict[str, Any], path: str) -> None:
    code = row["exit_code"]
    _require(
        code is None or (type(code) is int and 0 <= code <= MAX_EXIT_CODE), f"{path}.exit_code"
    )
    reason = row["reason"]
    _require(
        reason is None or (isinstance(reason, str) and reason in JOB_REASONS), f"{path}.reason"
    )


def _kept(log: Path) -> bool:
    """A worker log the run wrote: a regular file, never a link, as runs create it exclusively."""
    return log.is_file() and not log.is_symlink()


class JobController:
    def __init__(
        self,
        root: Path,
        *,
        execute: Executor,
        commit: Callable[[], str],
        remote: bool = False,
        retention: int = 100,
    ) -> None:
        if type(retention) is not int or not 1 <= retention <= MAX_RETENTION:
            raise ValueError("Job retention must be between 1 and 1000")
        if root.is_symlink():
            raise ValueError("Job workspace cannot be a symlink")
        root.mkdir(parents=True, exist_ok=True)
        self.root, self.execute, self.commit, self.retention = root, execute, commit, retention
        self.profiles = PROFILES if remote else PROFILES[:2]
        self.condition = threading.Condition()
        self.cancelled = threading.Event()
        self.stopping = False
        self.failed = False
        with ExitStack() as startup:
            self.lease = (root / ".owner").open("a+")
            startup.callback(self.lease.close)
            self._claim()
            self.state = self._load()
            self._recover()
            self.thread = threading.Thread(target=self._work, name="local-validation", daemon=True)
            self.thread.start()
            startup.pop_all()

    def _claim(self) -> None:
        try:
            fcntl.flock(self.lease.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Local validation workspace is already owned") from None

    def _load(self) -> dict[str, Any]:
        path = self.root / "jobs.json"
        if not path.exists():
            return asdict(JobState(1, []))
        try:
            raw = read_bytes(path, MAX_STATE_BYTES)
            value = json.loads(raw)
            state = JobState.from_dict(value)
        except JobRecordError as error:
            raise OSError(f"{error}; preserve jobs.json for inspection") from None
        except ValueError:
            raise OSError("Local job state is unavailable; preserve it for inspection") from None
        return asdict(state)

    def _save(self) -> None:
        state = JobState(self.state["next"], [Job(**row) for row in self.state["jobs"]])
        write_json(self.root / "jobs.json", asdict(state))

    def _recover(self) -> None:
        for row in self.state["jobs"]:
            if row["state"] in ACTIVE:
                row.update(state="interrupted", reason="owner_restart", finished_at=_now())
        self._save()

    def history(self) -> list[dict[str, Any]]:
        with self.condition:
            self._available()
            return deepcopy(
                sorted(self.state["jobs"], key=lambda row: row["sequence"], reverse=True)
            )

    def _get(self, run_id: str) -> dict[str, Any]:
        _identifier(run_id)
        for row in self.state["jobs"]:
            if row["run_id"] == run_id:
                return row
        raise KeyError("No such local job")

    def get(self, run_id: str) -> dict[str, Any]:
        with self.condition:
            self._available()
            return deepcopy(self._get(run_id))

    def _worker_log_path(self, run_id: str) -> Path:
        # The path is built from the recorded job's id, never from the request's value.
        with self.condition:
            self._available()
            recorded = self._get(run_id)["run_id"]
        return self.root / recorded / WORKER_LOG

    def has_worker_log(self, run_id: str) -> bool:
        """Whether the run kept a worker log."""
        return _kept(self._worker_log_path(run_id))

    def worker_log(self, run_id: str) -> bytes | None:
        """The job's worker log as recorded so far, or None when the run kept none."""
        path = self._worker_log_path(run_id)
        if not _kept(path):
            return None
        try:
            return read_bytes(path, MAX_WORKER_LOG_BYTES)
        except ValueError:
            # Larger than any copy writes: unusable local evidence, not the caller's bad request.
            raise OSError("Worker log exceeds its size bound") from None

    def submit(self, run_id: str, profile: str) -> dict[str, Any]:
        _identifier(run_id)
        if profile not in self.profiles:
            raise ValueError("Choose an enabled reviewed profile")
        with self.condition:
            self._available()
            if self.stopping:
                raise OSError("Local validation controller is stopping")
            existing = next((row for row in self.state["jobs"] if row["run_id"] == run_id), None)
            if existing is not None:
                return self._repeated(existing, profile)
            return self._admit(run_id, profile)

    def _repeated(self, row: dict[str, Any], profile: str) -> dict[str, Any]:
        if row["profile"] != profile:
            raise JobConflictError("Submission ID already names another profile")
        return deepcopy(row)

    def _admit(self, run_id: str, profile: str) -> dict[str, Any]:
        if sum(row["state"] in ACTIVE for row in self.state["jobs"]) >= MAX_ADMITTED:
            raise QueueFullError("One worker and two queued jobs are already admitted")
        row = asdict(
            Job(
                run_id=run_id,
                profile=cast("Profile", profile),
                state="pending",
                commit=self.commit(),
                sequence=self.state["next"],
                submitted_at=_now(),
                started_at=None,
                finished_at=None,
                exit_code=None,
                reason=None,
            )
        )
        Job.from_dict(row, "job")
        self.state["next"] += 1
        self.state["jobs"].append(row)
        try:
            self._save()
        except OSError:
            self.state["jobs"].remove(row)
            self.state["next"] -= 1
            raise
        self.condition.notify_all()
        return deepcopy(row)

    def cancel(self, run_id: str) -> dict[str, Any]:
        with self.condition:
            self._available()
            row = self._get(run_id)
            if row["state"] == "pending":
                row.update(state="cancelled", reason="cancelled", finished_at=_now())
                self._prune()
                self._save()
            elif row["state"] == "running":
                self.cancelled.set()
            return deepcopy(row)

    def _next(self) -> dict[str, Any] | None:
        return next((row for row in self.state["jobs"] if row["state"] == "pending"), None)

    def _work(self) -> None:
        try:
            self._dispatch()
        except Exception as error:  # noqa: BLE001 - fail the background controller closed
            with self.condition:
                self.failed = self.stopping = True
                self.cancelled.set()
            emit(
                logging.getLogger(__name__),
                logging.ERROR,
                "validation_controller_failed",
                errorType=type(error).__name__,
            )

    def _available(self) -> None:
        if self.failed:
            raise OSError("Local validation controller is unavailable; inspect its storage")

    def _dispatch(self) -> None:
        while True:
            with self.condition:
                self.condition.wait_for(lambda: self.stopping or self._next() is not None)
                if self.stopping:
                    return
                row = cast("dict[str, Any]", self._next())
                self.cancelled.clear()
                row.update(state="running", started_at=_now())
                self._save()
            self._execute(row)

    def _execute(self, row: dict[str, Any]) -> None:
        try:
            result = self.execute(deepcopy(row), self.root / row["run_id"], self.cancelled)
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            emit(
                logging.getLogger(__name__),
                logging.ERROR,
                "validation_worker_failed",
                errorType=type(error).__name__,
            )
            result: JobOutcome = {
                "state": "unavailable",
                "exit_code": None,
                "reason": "worker_error",
            }
        with self.condition:
            # The executor owns cancellation and has already saved the terminal evidence.
            row.update(result, finished_at=_now())
            self._prune()
            self._save()

    def _prune(self) -> None:
        terminal = [row for row in self.state["jobs"] if row["state"] in TERMINAL]
        for row in terminal[: -self.retention]:
            path = self.root / row["run_id"]
            if path.exists() and not path.is_symlink():
                shutil.rmtree(path)
            self.state["jobs"].remove(row)

    def close(self) -> None:
        if self.lease.closed:
            return
        with self.condition:
            self.stopping = True
            self.cancelled.set()
            for row in self.state["jobs"]:
                if row["state"] == "pending":
                    row.update(state="cancelled", reason="owner_shutdown", finished_at=_now())
            self.condition.notify_all()
        try:
            with self.condition:
                self._save()
        finally:
            self.thread.join(timeout=30)
            if self.thread.is_alive():
                raise RuntimeError("Owned validation worker has not stopped")
            self.lease.close()
