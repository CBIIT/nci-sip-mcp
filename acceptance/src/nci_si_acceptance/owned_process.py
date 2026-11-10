"""Bounded stop for an explicitly owned direct child; never discover processes by PID."""

import subprocess


def stop_child(process: subprocess.Popen[bytes], *, seconds: float) -> None:
    """Preserve the harness's TERM/wait/KILL/wait contract."""
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=seconds)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=seconds)
