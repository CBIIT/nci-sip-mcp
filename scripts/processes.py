"""Process helpers shared by the scripts: reaping, bounded waits, SIGTERM and log tails."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from contextlib import suppress
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

STOP_SECONDS = 10
SERVER_LOG_TAIL_BYTES = 64 * 1024


def print_server_log_tail(log: Path) -> None:
    """Print the end of an owned server's log to stderr, so that a failed run says why."""
    try:
        with log.open("rb") as stream:
            stream.seek(max(0, stream.seek(0, os.SEEK_END) - SERVER_LOG_TAIL_BYTES))
            tail = stream.read()
    except FileNotFoundError:
        return  # the server never started, so it has nothing to say
    sys.stderr.write(f"Owned server log, last {SERVER_LOG_TAIL_BYTES} bytes:\n")
    sys.stderr.write(tail.decode("utf-8", "replace"))


def _send(process: subprocess.Popen[bytes], sig: signal.Signals, *, group: bool) -> None:
    if group:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, sig)
    else:
        process.send_signal(sig)


def stop(process: subprocess.Popen[bytes], *, group: bool = False) -> None:
    """Terminate, wait, then kill and reap `process`; with `group` its whole process group,
    which the process must lead.

    The plain path returns at once for a process that has finished. With `group` the sequence
    always ends with a SIGKILL sweep of the group, even when the leader has already exited.
    """

    # The same terminate, wait, kill sequence as OwnedServer.stop in nci_sip_acceptance, kept
    # twice because that package must not import scripts.

    if not group and process.poll() is not None:
        return
    _send(process, signal.SIGTERM, group=group)
    try:
        process.wait(timeout=STOP_SECONDS)
    except subprocess.TimeoutExpired:
        _send(process, signal.SIGKILL, group=group)
        process.wait(timeout=STOP_SECONDS)
    if group:
        # A group leader can exit while an owned descendant ignores graceful termination.
        _send(process, signal.SIGKILL, group=True)


def wait_until(predicate: Callable[[], bool], seconds: float, *, interval: float) -> bool:
    """True once `predicate` holds, tried every `interval` seconds; False when `seconds` pass
    first. An exception of the predicate propagates."""

    deadline = time.monotonic() + seconds
    while not predicate():
        if time.monotonic() >= deadline:
            return False
        time.sleep(interval)
    return True


def _interrupt(_signum: int, _frame: Any) -> None:
    # The first SIGTERM starts stack unwinding. A supervisor can forward the same signal again;
    # do not let that second delivery interrupt owned cleanup already in progress.
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    raise KeyboardInterrupt


def exit_on_sigterm() -> None:
    """Make SIGTERM unwind the stack as Ctrl-C does, so owned processes and temporary
    directories are cleaned up on a CI cancellation too."""

    signal.signal(signal.SIGTERM, _interrupt)
