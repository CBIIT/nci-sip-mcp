"""Bounded owned process groups with a parent-liveness pipe; no detached validation work."""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import threading
from pathlib import Path
from typing import BinaryIO

from scripts.evidence_envelope import MAX_REPORT_BYTES
from scripts.processes import stop, wait_until
from scripts.vocabulary import JobOutcome, JobReason

from nci_sip_mcp.audit import emit

LIFELINE = "NCI_SIP_OPERATOR_LIFELINE"
WORKER_LOG = "worker.log"
LOG_PART_BYTES = 512 * 1024
LOG_JOIN_SECONDS = 10
# The head, the omission line and the tail.
MAX_WORKER_LOG_BYTES = 2 * LOG_PART_BYTES + 64
_CHUNK = 64 * 1024


class _LogCopy:
    """Copy a worker's output into its job log, keeping its first and last LOG_PART_BYTES.

    The pipe is read to its end whatever happens to the log, so the worker never blocks on it.
    """

    def __init__(self, source: int, target: Path) -> None:
        self.source, self.target = source, target
        self.thread = threading.Thread(target=self._run, name="validation-worker-log", daemon=True)
        self.thread.start()

    def _run(self) -> None:
        try:
            descriptor = os.open(self.target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as log:
                self._copy(log)
        except OSError as error:
            _diagnostic("worker_log_failed", error)
        finally:
            while os.read(self.source, _CHUNK):
                pass
            os.close(self.source)

    def _copy(self, log: BinaryIO) -> None:
        written = omitted = 0
        tail = bytearray()
        while chunk := os.read(self.source, _CHUNK):
            head = chunk[: max(0, LOG_PART_BYTES - written)]
            if head:
                # Written as it arrives, so the start of a run is on disk while it runs.
                log.write(head)
                log.flush()
                written += len(head)
            tail += chunk[len(head) :]
            excess = len(tail) - LOG_PART_BYTES
            if excess > 0:
                omitted += excess
                del tail[:excess]
        if omitted:
            log.write(f"\n[worker.log: {omitted} bytes omitted]\n".encode())
        log.write(tail)

    def join(self) -> None:
        self.thread.join(LOG_JOIN_SECONDS)
        if self.thread.is_alive():
            # A process outside the stopped group still holds the pipe; the copy drains it.
            _diagnostic("worker_log_open", None)


def _diagnostic(event: str, error: OSError | None) -> None:
    fields = {} if error is None else {"errorType": type(error).__name__}
    emit(logging.getLogger(__name__), logging.ERROR, event, **fields)


def _output_problem(directory: Path) -> JobReason | None:
    report = directory / "bundle/report.json"
    if report.is_symlink():
        return "invalid_evidence"
    if report.exists() and report.stat().st_size > MAX_REPORT_BYTES:
        return "output_limit"
    return None


def _wait(
    process: subprocess.Popen[bytes],
    directory: Path,
    seconds: float,
    cancelled: threading.Event,
) -> JobReason | None:
    reason: JobReason | None = None

    def settled() -> bool:
        nonlocal reason
        if process.poll() is not None:
            return True
        reason = "cancelled" if cancelled.is_set() else _output_problem(directory)
        return reason is not None

    if not wait_until(settled, seconds, interval=0.05):
        return "deadline"
    return reason or _output_problem(directory)


def _start(
    command: list[str], environment: dict[str, str], lifeline: int, log: Path
) -> tuple[subprocess.Popen[bytes], _LogCopy]:
    output, log_input = os.pipe()
    try:
        process = subprocess.Popen(  # noqa: S603 - caller supplies only fixed reviewed worker commands
            command,
            cwd=Path(__file__).resolve().parents[1],
            env=environment | {LIFELINE: str(lifeline)},
            stdin=subprocess.DEVNULL,
            stdout=log_input,
            stderr=log_input,
            start_new_session=True,
            pass_fds=(lifeline,),
        )
    except BaseException:
        os.close(output)
        raise
    finally:
        os.close(log_input)
    try:
        return process, _LogCopy(output, log)
    except BaseException:
        # Without its copier the worker must not outlive this call (a thread that cannot start).
        stop(process, group=True)
        os.close(output)
        raise


def run_owned(
    command: list[str],
    *,
    directory: Path,
    environment: dict[str, str],
    seconds: float,
    cancelled: threading.Event,
) -> JobOutcome:
    if cancelled.is_set():
        return {"state": "cancelled", "exit_code": None, "reason": "cancelled"}
    read_fd, write_fd = os.pipe()
    with os.fdopen(read_fd, "rb"), os.fdopen(write_fd, "wb"):
        process, log = _start(command, environment, read_fd, directory / WORKER_LOG)
        try:
            reason = _wait(process, directory, seconds, cancelled)
        finally:
            stop(process, group=True)
            log.join()
    code = process.returncode if process.returncode >= 0 else 128 - process.returncode
    if reason is not None:
        return {
            "state": "cancelled" if reason == "cancelled" else "failed",
            "exit_code": code,
            "reason": reason,
        }
    return {
        "state": "completed" if code == 0 else "failed",
        "exit_code": code,
        "reason": None if code == 0 else "worker_failed",
    }


def watch_parent() -> None:
    """The group leader kills its owned session if the controller's pipe disappears."""
    if os.getpgrp() != os.getpid():
        raise RuntimeError("Validation worker must lead its own process group")
    descriptor = int(os.environ.pop(LIFELINE))
    os.set_inheritable(descriptor, False)

    def parent_lost() -> None:
        try:
            os.read(descriptor, 1)
        finally:
            os.close(descriptor)
            # No controller remains to escalate graceful shutdown if a child ignores it.
            os.killpg(os.getpgrp(), signal.SIGKILL)

    threading.Thread(target=parent_lost, name="validation-owner-liveness", daemon=True).start()
