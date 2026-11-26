"""Real `pdm run` for tests, bound to an owned project instead of the suite's own."""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def create_project(
    project: Path, *, interpreter: Path | str = sys.executable, pyproject: str | None = None
) -> Path:
    """An owned PDM project: `pyproject` (default the checkout's) and the interpreter it binds."""
    project.mkdir(parents=True, exist_ok=True)
    text = (ROOT / "pyproject.toml").read_text() if pyproject is None else pyproject
    (project / "pyproject.toml").write_text(text)
    (project / ".pdm-python").write_text(str(interpreter))
    return project


def pdm_run(
    *command: str, cwd: Path, timeout: int, project: Path | None = None
) -> subprocess.CompletedProcess[str]:
    """The launch as the mutation tests made it: cwd alone, inherited environment."""
    return subprocess.run(  # noqa: S603 - fixed PDM command, owned paths
        ["pdm", "run", *command],  # noqa: S607
        cwd=project or cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
