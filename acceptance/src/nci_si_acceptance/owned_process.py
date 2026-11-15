"""Bounded stop for an explicitly owned direct child; never discover processes by PID."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nci_si_acceptance.request_children import CleanupDeadline


def stop_child(
    process: subprocess.Popen[bytes], *, seconds: float, deadline: CleanupDeadline | None = None
) -> None:
    """Stop the owned child, optionally inside the run's original cleanup deadline."""
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(
            timeout=seconds if deadline is None else min(seconds, deadline.remaining() / 2)
        )
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=seconds if deadline is None else deadline.remaining())
