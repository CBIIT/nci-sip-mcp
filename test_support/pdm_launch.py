"""Real `pdm run` for tests, bound to an owned project instead of the suite's own.

Test-only support shared by the unit tests and the acceptance self-tests through their pytest
`pythonpath`; it imports no product or harness package and is not part of any wheel.
"""

import contextlib
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INHERITED = (
    "PATH",
    "LANG",
    "LC_ALL",
    "TMPDIR",
    "TMP",
    "TEMP",
    "PYTHONPATH",
    "PYTHONDONTWRITEBYTECODE",
    "NCI_SIP_DATA_DIR",
    # A baseline run (mutation_run) maps the lines that only a nested PDM child executes through
    # exactly these: the child's coverage start-up, its data file and per-test context.
    "COVERAGE_PROCESS_START",
    "COVERAGE_PROCESS_CONFIG",
    "COVERAGE_FILE",
    "MUTATION_COVERAGE_CONTEXT",
)


UNAVAILABLE = "unavailable"


def create_project(
    project: Path, *, interpreter: Path | str = sys.executable, pyproject: str | None = None
) -> Path:
    """An owned PDM project: `pyproject` (default the checkout's) and the interpreter it binds."""
    project.mkdir(parents=True, exist_ok=True)
    text = (ROOT / "pyproject.toml").read_text() if pyproject is None else pyproject
    (project / "pyproject.toml").write_text(text)
    (project / ".pdm-python").write_text(str(interpreter))
    return project


def child_environment(home: Path, source: Mapping[str, str]) -> dict[str, str]:
    env = {key: value for key, value in source.items() if key in INHERITED}
    env.update(
        HOME=str(home),
        XDG_CONFIG_HOME=str(home / "config"),
        XDG_CACHE_HOME=str(home / "cache"),
        XDG_DATA_HOME=str(home / "data"),
        PDM_CONFIG_FILE=str(home / "pdm.toml"),
        PDM_IGNORE_ACTIVE_VENV="1",
        PDM_NON_INTERACTIVE="1",
        PDM_CHECK_UPDATE="0",
    )
    return env


def pdm_run(
    *command: str,
    timeout: int,
    cwd: Path | None = None,
    project: Path | None = None,
    environment: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run `pdm run command` with `project` (default a fresh owned copy) selected.

    `cwd` overrides the directory the command runs in; without it PDM decides, as it does for a
    script's own `working_dir`, relative to the owned project. The child sees only the
    allowlisted variables of `environment` (default: this process's).
    """
    with tempfile.TemporaryDirectory() as scratch:
        base = Path(scratch).resolve()
        selected = project or create_project(base / "project")
        argv = ["pdm", "run", "--project", str(selected)]
        if cwd is not None:
            argv += ["--working-dir", str(cwd)]
        source = os.environ if environment is None else environment
        env = child_environment(base / "home", source)
        argv[0] = shutil.which("pdm", path=env.get("PATH", "")) or UNAVAILABLE
        return _launch([*argv, *command], cwd or selected, env, timeout, selected)


def _launch(
    argv: Sequence[str], cwd: Path, env: dict[str, str], timeout: int, project: Path
) -> subprocess.CompletedProcess[str]:
    started = _now()
    before = _interpreter_file(project)
    outcome, stdout, stderr = "not started", "", ""
    try:
        result = _run(argv, cwd, env, timeout)
    except subprocess.TimeoutExpired as error:
        outcome, stdout, stderr = f"timeout after {timeout}s", error.output, error.stderr
        raise
    except OSError as error:
        outcome = f"{type(error).__name__}: {error}"
        raise
    else:
        outcome = f"returncode={result.returncode}"
        stdout, stderr = result.stdout, result.stderr
        return result
    finally:
        fields = {
            "argv": list(argv),
            "cwd": cwd,
            "project": project,
            "interpreter": before[0],
            "interpreter-file": {"before": before[1], "after": _interpreter_file(project)[1]},
            "started": started,
            "ended": _now(),
            "outcome": outcome,
            "selectors": {k: env[k] for k in sorted(env) if k.startswith("PDM_") or k == "HOME"},
            "stdout": stdout,
            "stderr": stderr,
        }
        _emit(fields)


def _run(
    argv: Sequence[str], cwd: Path, env: dict[str, str], timeout: int
) -> subprocess.CompletedProcess[str]:
    if argv[0] == UNAVAILABLE:
        raise FileNotFoundError("pdm executable not found on the launch PATH")
    with subprocess.Popen(  # noqa: S603 - fixed PDM command, owned project and paths
        argv,
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    ) as process:
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_group(process)
            stdout, stderr = process.communicate()
            raise subprocess.TimeoutExpired(list(argv), timeout, stdout, stderr) from None
        except BaseException:
            _kill_group(process)
            raise
    return subprocess.CompletedProcess(list(argv), process.returncode, stdout, stderr)


def _kill_group(process: subprocess.Popen[str]) -> None:
    """Kill the process group this Popen created (`start_new_session`), by its pid only.

    A descendant that leaves the group (another session, or `setpgid`) is not reached; nothing
    else is signalled.
    """
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _interpreter_file(project: Path) -> tuple[str, str]:
    """The saved interpreter and its identity; a file that cannot be read is reported as such."""
    try:
        data = (project / ".pdm-python").read_bytes()
    except FileNotFoundError:
        return "absent", "absent"
    except OSError as error:
        unavailable = f"unavailable: {type(error).__name__}"
        return unavailable, unavailable
    return data.decode(errors="replace").strip(), hashlib.sha256(data).hexdigest()[:12]


def _emit(fields: dict[str, object]) -> None:
    text = "".join(
        f"pdm-launch {name}: {json.dumps(value, default=str)}\n" for name, value in fields.items()
    )
    # Evidence must never replace the launch's own outcome, so a closed stream is ignored.
    with contextlib.suppress(OSError, ValueError):
        sys.stderr.write(text)
