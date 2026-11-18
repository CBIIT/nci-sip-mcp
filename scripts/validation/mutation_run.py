"""Owned scratch snapshots and deterministic child-pytest evidence classification."""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from threading import current_thread, main_thread
from time import monotonic, sleep
from types import FrameType

from coverage import Coverage, CoverageData
from pytest import ExitCode
from scripts.processes import STOP_SECONDS, stop
from scripts.validation.mutation_child_trace import (
    ChildTrace,
    Producer,
    prepare_run,
    read_traces,
)

# An ordinary runner has STOP_SECONDS of grace plus STOP_SECONDS for final KILL/reap.
# A scheduler must let that entire nested cleanup finish before its own final KILL.
# Leave one second for unwinding after those waits; remeasure if nested cleanup changes.
WORKER_CLEANUP_SECONDS = 2 * STOP_SECONDS + 1
# Reserve a final bounded KILL/reap interval after nested workers' cleanup grace.
OWNED_CLEANUP_SECONDS = WORKER_CLEANUP_SECONDS + STOP_SECONDS
# ps is diagnostic, never allowed to consume the whole remaining cleanup budget.
INSPECTION_SECONDS = 1.0
# Responsive cancellation without busy-waiting; remeasure for much larger worker counts.
OBSERVE_INTERVAL = 0.01


@dataclass(frozen=True, slots=True)
class _Owner:
    pid: int
    uid: int
    ruid: int


@dataclass(slots=True)
class _Cancellation:
    requested: KeyboardInterrupt | None = None


@dataclass(frozen=True, slots=True)
class _Member:
    pid: int
    ppid: int
    pgid: int
    sid: int | None
    uid: int
    ruid: int
    state: str


@dataclass(frozen=True, slots=True)
class CoverageEvidence:
    root: Path
    lines: dict
    children: tuple[ChildTrace, ...]


def _observe(pid: int) -> int | None:
    """Observe without consuming the PID/PGID anchor or losing signal status."""
    row = os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
    if row is None:
        return None
    return row.si_status if row.si_code == os.CLD_EXITED else -row.si_status


def _wait_exit(
    pid: int, deadline: float, cancel: Callable[[], KeyboardInterrupt | None] | None = None
) -> int | None:
    while True:
        if cancel is not None and (interruption := cancel()) is not None:
            raise interruption
        status = _observe(pid)
        if status is not None or monotonic() >= deadline:
            return status
        sleep(min(OBSERVE_INTERVAL, max(0, deadline - monotonic())))


def _remaining(deadline: float) -> float:
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise TimeoutError("Owned process cleanup deadline exhausted")
    return remaining


def _member(line: str, *, linux: bool) -> _Member:
    fields = line.split()
    if len(fields) != (7 if linux else 6):
        raise ValueError(f"Incomplete owned-group process row: {line!r}")
    numbers = fields[:-1]
    if not all(value.isascii() and value.isdecimal() for value in numbers):
        raise ValueError(f"Invalid owned-group process identity: {line!r}")
    pid, ppid, pgid = map(int, numbers[:3])
    uid, ruid = map(int, numbers[-2:])
    sid = int(numbers[3]) if linux else None
    return _Member(pid, ppid, pgid, sid, uid, ruid, fields[-1])


def _inspection_command(pid: int) -> tuple[list[str], bool]:
    linux = sys.platform.startswith("linux")
    if sys.platform != "darwin" and not linux:
        raise ValueError("Owned-group inspection requires Darwin or Linux ps")
    fields = "pid=,ppid=,pgid=," + ("sid=," if linux else "") + "uid=,ruid=,state="
    return ["/bin/ps", "-s" if linux else "-g", str(pid), "-o", fields], linux


def _group_snapshot(owner: _Owner, deadline: float) -> list[_Member]:
    command, linux = _inspection_command(owner.pid)
    # Darwin uid is effective (ps(1)); Linux uid aliases euid (procps ps(1)).
    # LC_ALL stabilizes numeric output; COMMAND_MODE excludes Darwin legacy -g.
    result = subprocess.run(  # noqa: S603 - fixed read-only ps, original owned identity only
        command,
        capture_output=True,
        text=True,
        timeout=min(INSPECTION_SECONDS, _remaining(deadline)),
        check=True,
        env={**os.environ, "LC_ALL": "C", "COMMAND_MODE": "unix2003"},
    )
    _remaining(deadline)
    if result.stderr or not result.stdout.endswith("\n"):
        raise ValueError("Owned-group inspection has stderr or incomplete output")
    rows = [_member(line, linux=linux) for line in result.stdout.splitlines()]
    if linux:
        rows = [row for row in rows if row.pgid == owner.pid]
    return rows


def _validate_anchor(rows: list[_Member], owner: _Owner) -> None:
    anchors = [row for row in rows if row.pid == owner.pid]
    if len(anchors) != 1 or anchors[0].ppid != os.getpid():
        raise ChildProcessError(f"Missing owned-group anchor {owner.pid}")
    if len({row.pid for row in rows}) != len(rows):
        raise ValueError("Duplicate owned-group process identities")


def _terminal_group(owner: _Owner, deadline: float) -> bool:
    # A live anchor proves nonterminal state without spawning ps every 10ms.
    # Only early leader exit needs a snapshot to distinguish live descendants.
    if _observe(owner.pid) is None:
        return False
    rows = _group_snapshot(owner, deadline)
    _validate_anchor(rows, owner)
    return all(_terminal_member(row, owner) for row in rows)


def _terminal_member(row: _Member, owner: _Owner) -> bool:
    # An unreaped session leader reserves PID/PGID; POSIX groups cannot cross
    # sessions. Darwin zombie SID is unavailable, so the exact anchored PGID
    # binds membership. ps is non-atomic: this proves only that signals may stop,
    # never durable containment or that every descendant has been reaped.
    return (
        (row.pgid, row.uid, row.ruid) == (owner.pid, owner.uid, owner.ruid)
        and row.sid in (None, owner.pid)
        and row.state == "Z"
    )


def _signal_owned(owner: _Owner, sig: signal.Signals, deadline: float) -> bool:
    _observe(owner.pid)
    _remaining(deadline)
    try:
        os.killpg(owner.pid, sig)
    except PermissionError as denial:
        try:
            terminal = _terminal_group(owner, deadline)
        except (OSError, ValueError, subprocess.SubprocessError) as inspection:
            raise denial from inspection
        if not terminal:
            raise
        return False
    return True


def _grace_complete(owner: _Owner, deadline: float, grace: float) -> bool:
    while monotonic() < grace:
        if _terminal_group(owner, deadline):
            return True
        sleep(min(OBSERVE_INTERVAL, max(0, grace - monotonic())))
    return False


def _kill_owned(owner: _Owner, deadline: float) -> None:
    if _signal_owned(owner, signal.SIGKILL, deadline) and _wait_exit(owner.pid, deadline) is None:
        raise TimeoutError("Owned child survived final SIGKILL")


def _cleanup_group(owner: _Owner, deadline: float, grace_seconds: float) -> None:
    # Denied TERM is not permission to escalate; only successful TERM opens grace.
    if not _signal_owned(owner, signal.SIGTERM, deadline):
        return
    grace = min(deadline, monotonic() + grace_seconds)
    try:
        terminal = _grace_complete(owner, deadline, grace)
    except (OSError, ValueError, subprocess.SubprocessError) as inspection:
        # Inspection cannot forfeit final cleanup of the original anchored group.
        # _signal_owned rechecks the unreaped anchor; no discovered PID is signalled.
        try:
            # A worker may still be reaping its separate-session runner. Failed
            # inspection cannot shorten its original cooperative grace deadline.
            while monotonic() < grace:
                sleep(min(OBSERVE_INTERVAL, max(0, grace - monotonic())))
            _kill_owned(owner, deadline)
        except BaseException as final:  # noqa: BLE001 - retain both original cleanup failures
            raise BaseExceptionGroup(
                "Owned inspection and final cleanup failed", [inspection, final]
            ) from None
        raise
    if not terminal:
        _kill_owned(owner, deadline)


def _finish_owned(
    process: subprocess.Popen[bytes], owner: _Owner, *, worker_cleanup: bool = False
) -> None:
    grace = WORKER_CLEANUP_SECONDS if worker_cleanup else STOP_SECONDS
    budget = OWNED_CLEANUP_SECONDS if worker_cleanup else 2 * STOP_SECONDS
    deadline = monotonic() + budget
    failure = None
    try:
        _cleanup_group(owner, deadline, grace)
    except BaseException as error:  # noqa: BLE001 - retain cleanup failure through actual-parent reap
        failure = error
    try:
        # The sole consuming operation: no Popen poll/send_signal before here.
        process.wait(timeout=max(0, deadline - monotonic()))
    except BaseException as reap_error:
        if failure is not None:
            raise BaseExceptionGroup(
                "Owned cleanup and reap failed", [failure, reap_error]
            ) from None
        raise
    if failure is not None:
        raise failure


DOMAINS = {
    "unit": (".", "tests"),
    "selftest": ("acceptance", "selftests"),
    "fixture": ("acceptance", "tests"),
}
FIXTURE = {
    "NCI_SI_ACCEPTANCE_MODE": "fixture",
    "NCI_SI_ACCEPTANCE_REGISTRY_RELEASE": "required",
    "NCI_SI_ACCEPTANCE_SECURITY_SERVER": "python ../scripts/permissions_fixture.py",
    "NCI_SI_ACCEPTANCE_PREPARE": 'nci-si-mcp index-sample $(cat "$NCI_SI_ACCEPTANCE_INDEX_CODES")',
}


@contextmanager
def snapshot(root: Path):
    with tempfile.TemporaryDirectory(prefix="mutation-") as temporary:
        scratch = Path(temporary).resolve() / "source"
        if (root / ".git").exists():
            subprocess.run(  # noqa: S603 - fixed git command, owned paths
                ["git", "clone", "--quiet", "--no-hardlinks", str(root), str(scratch)],  # noqa: S607
                check=True,
                capture_output=True,
            )
        shutil.copytree(
            root,
            scratch,
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns(
                ".git",
                ".venv",
                "__pycache__",
                ".coverage*",
                ".pytest_cache",
                ".nci-si-mcp",
                "tmp",
                "node_modules",
                ".ruff_cache",
            ),
        )
        # A separate object database/index prevents child git operations reaching the caller.
        subprocess.run(["git", "init", "-q", str(scratch)], check=True, capture_output=True)  # noqa: S603,S607
        yield scratch


def environment(scratch: Path, work: Path) -> dict[str, str]:
    prefixes = ("COVERAGE", "PYTEST", "GIT_", "NCI_SI_", "PDM_", "MUTATION_", "SELFTEST_")
    env = {key: value for key, value in os.environ.items() if not key.startswith(prefixes)}
    env.update(
        {
            "PYTHONDONTWRITEBYTECODE": "1",
            "NCI_SI_DATA_DIR": str(work / "data"),
            # tempfile owns unique child directories; nesting its root under evidence breaks
            # macOS AF_UNIX's 104-byte socket path limit in HTTP integration tests.
            "TMPDIR": tempfile.gettempdir(),
            "TMP": tempfile.gettempdir(),
            "TEMP": tempfile.gettempdir(),
            "PATH": os.pathsep.join((str(Path(sys.executable).parent), env.get("PATH", ""))),
        }
    )
    # PYTHONPATH reaches both imports in the pytest interpreter and installed console children.
    paths = [scratch / "src", scratch / "acceptance/src", scratch, Path(__file__).parents[2]]
    env["PYTHONPATH"] = os.pathsep.join(map(str, paths))
    return env


@contextmanager
def runner_termination():
    # Nested pytest workers normally die on SIGTERM without unwinding. Their child
    # sessions escape the scheduler's group, so the waiting owner must clean them.
    cancellation = _Cancellation()
    if current_thread() is not main_thread():
        yield cancellation
        return
    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}

    def defer(_signum: int, _frame: FrameType | None) -> None:
        # Record once; launch, resource closure and bounded cleanup never unwind
        # asynchronously. Execution observes this same exception at a checkpoint.
        if cancellation.requested is None:
            cancellation.requested = KeyboardInterrupt()

    failures: list[BaseException] = []
    try:
        for sig in previous:
            signal.signal(sig, defer)
        yield cancellation
    except BaseException as error:  # noqa: BLE001 - retain original failure through handler restoration
        failures.append(error)
    finally:
        for sig, disposition in previous.items():
            try:
                signal.signal(sig, disposition)
            except BaseException as error:  # noqa: BLE001 - attempt both restores and retain each failure
                failures.append(error)
    # Restoration still belongs to this scope: reconcile only after its latch
    # handlers have handed control back to the caller's signal dispositions.
    _raise_cancelled_failure(failures, cancellation.requested)


def _raise_cancelled_failure(
    failures: list[BaseException], interruption: KeyboardInterrupt | None
) -> None:
    if interruption is not None and not any(
        _contains_exception(error, interruption) for error in failures
    ):
        failures.append(interruption)
    if len(failures) == 1:
        raise failures[0]
    if failures:
        raise BaseExceptionGroup("Runner scope failed", failures) from None


def _contains_exception(error: BaseException, target: BaseException) -> bool:
    if isinstance(error, BaseExceptionGroup):
        return any(_contains_exception(nested, target) for nested in error.exceptions)
    return error is target


def _finish_process(
    process: subprocess.Popen[bytes] | None, owner: _Owner | None, worker_cleanup: bool
) -> None:
    if process is None:
        return
    if owner is None:
        stop(process)  # Broken launch: only the direct child belongs to us.
    elif worker_cleanup:
        _finish_owned(process, owner, worker_cleanup=True)
    else:
        _finish_owned(process, owner)


def _cleanup_process(
    process: subprocess.Popen[bytes] | None,
    owner: _Owner | None,
    worker_cleanup: bool,
    failure: BaseException | None,
) -> None:
    try:
        _finish_process(process, owner, worker_cleanup)
    except BaseException as cleanup:
        if failure is not None:
            raise BaseExceptionGroup(
                "Runner and owned cleanup failed", [failure, cleanup]
            ) from None
        raise
    if failure is not None:
        raise failure


def program(
    command: list[str],
    cwd: Path,
    env: dict[str, str],
    timeout: float,
    log: Path,
    *,
    worker_cleanup: bool = False,
):
    """Run an owned session; scheduler callers allow nested workers' full cleanup budget."""
    with runner_termination() as cancellation, log.open("wb") as stream:
        process = None
        owner = None
        failure = None
        try:
            process = subprocess.Popen(  # noqa: S603 - fixed Python executable and argument arrays
                command,
                cwd=cwd,
                env=env,
                stdout=stream,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            if (os.getpgid(process.pid), os.getsid(process.pid)) != (process.pid, process.pid):
                raise ValueError(f"Child {process.pid} does not own its process group/session")
            owner = _Owner(process.pid, os.geteuid(), os.getuid())
            # Only execution checks the latch; final KILL waits never cancel.
            status = _wait_exit(process.pid, monotonic() + timeout, lambda: cancellation.requested)
        except BaseException as error:  # noqa: BLE001 - preserve cancellation while cleanup runs
            failure = error
            status = None
        _cleanup_process(process, owner, worker_cleanup, failure)
        return status


def classify(
    status: int | None, evidence: Path, mutated_file: Path | None = None
) -> dict[str, object]:
    if status is None:
        return {"outcome": "TIMEOUT"}
    rows = (
        [json.loads(line) for line in evidence.read_text().splitlines()]
        if evidence.exists()
        else []
    )
    return evidence_outcome(status, rows, mutated_file)


def evidence_outcome(
    status: int, rows: list[dict], mutated_file: Path | None = None
) -> dict[str, object]:
    failed = [row for row in rows if row["outcome"] == "failed"]
    kill = call_kill(status, failed) or import_kill(status, failed, mutated_file)
    if kill:
        return kill
    if status not in (0, 5) or failed:
        return {"outcome": "INFRA", "status": status, "failures": failed}
    return successful_calls(rows)


def successful_calls(rows: list[dict]) -> dict[str, object]:
    calls = [row for row in rows if (row["phase"], row["outcome"]) == ("call", "passed")]
    return {"outcome": "SURVIVED" if calls else "NO_TESTS"}


def call_kill(status: int, failed: list[dict]) -> dict:
    if status == 1 and len(failed) == 1 and failed[0]["phase"] == "call":
        return {
            "outcome": "KILLED",
            "node": failed[0]["node"],
            "phase": "call",
            "kind": "assertion" if failed[0]["assertion"] else "error",
        }
    return {}


def import_kill(status: int, failed: list[dict], mutated_file: Path | None) -> dict:
    # pytest -x reports its own stop after the import failure as a further collection failure
    # of the session; the probe marks it, and only notices after the failure are set aside.
    # import_failure also refuses a marked first row, so a run led by a notice is never a kill.
    notices = [row for row in failed[1:] if stop_notice(row)]
    if (
        status not in (ExitCode.INTERRUPTED, ExitCode.USAGE_ERROR)
        or len(failed) != 1 + len(notices)
        or not import_failure(failed[0], mutated_file)
    ):
        return {}
    kill = {"outcome": "KILLED", "kind": "import", "phase": "collection", "node": failed[0]["node"]}
    return kill | ({"stop_notices": notices} if notices else {})


def import_failure(row: dict, mutated_file: Path | None) -> bool:
    # A primary failure carries no stop marker of any value: a notice never stands in for it.
    return (
        mutated_file is not None
        and "stop_notice" not in row
        and row["phase"] == "collection"
        and not row.get("exception", "").startswith("SystemExit")
        and str(mutated_file.resolve()) in row.get("traceback_paths", [])
    )


def stop_notice(row: dict) -> bool:
    return row["phase"] == "collection" and row.get("stop_notice") is True


def run_domain(
    scratch: Path,
    domain: str,
    selected: list[str],
    timeout: float,
    *,
    baseline: bool = False,
    mutated_file: Path | None = None,
    mutated_span: tuple[int, int] | None = None,
    expected_children: tuple[ChildTrace, ...] = (),
) -> tuple[dict, Path]:
    args, env, data_file, evidence, log = prepare_domain_run(
        scratch, domain, selected, baseline, collect_coverage=bool(expected_children)
    )
    started = monotonic()
    status = program(args, scratch / DOMAINS[domain][0], env, timeout, log)
    elapsed = monotonic() - started
    result: dict[str, object] = classify(status, evidence, mutated_file)
    try:
        if status is not None:
            owners = validate_domain_traces(
                data_file,
                env,
                domain,
                expected_children,
                mutated_file,
                mutated_span,
                scratch,
            )
            if expected_children:
                owners.update(
                    native_candidate_owners(data_file, scratch, mutated_file, mutated_span)
                )
                result = attribute_child_result(result, evidence, owners)
    except (OSError, ValueError) as error:
        result = {"outcome": "INFRA", "message": str(error)}
    if baseline and result["outcome"] == "KILLED":
        result["outcome"] = "INFRA"
    result.update(domain=domain, log=log.read_text(errors="replace"), elapsed_seconds=elapsed)
    return result, data_file


def prepare_domain_run(
    scratch: Path,
    domain: str,
    selected: list[str],
    baseline: bool,
    *,
    collect_coverage: bool = False,
) -> tuple[list[str], dict[str, str], Path, Path, Path]:
    directory, default = DOMAINS[domain]
    work = Path(tempfile.mkdtemp(prefix=f"evidence-{domain}-", dir=scratch.parent))
    # Keep instrumentation independent of the subject, including probe self-mutations.
    shutil.copyfile(Path(__file__).with_name("mutation_probe.py"), work / "_mutation_evidence.py")
    env = environment(scratch, work)
    evidence = work / "events.jsonl"
    env["MUTATION_EVIDENCE"] = str(evidence)
    data_file = work / ".coverage"
    env["COVERAGE_FILE"] = str(data_file)
    trace_env = prepare_run(
        data_file,
        work / "child-hook",
        domain,
        "baseline" if baseline else "candidate",
        "local",
    )
    env.update(trace_env)
    args = [
        sys.executable,
        "-c",
        f"import sys; sys.path.insert(0, {str(work)!r}); "
        "import pytest; raise SystemExit(pytest.main(sys.argv[1:]))",
        "-p",
        "_mutation_evidence",
        "-n",
        "4" if baseline and domain in ("selftest", "fixture") else "0",
        "-x",
        "--rootdir",
        str(scratch / directory),
        *(selected or [default]),
    ]
    args += coverage_args(scratch, work, baseline or collect_coverage, env)
    if domain == "fixture":
        env.update(FIXTURE)
        args += ["--report=fixture.json"]
    log = work / "pytest.log"
    return args, env, data_file, evidence, log


def validate_domain_traces(
    data_file: Path,
    environment: dict[str, str],
    domain: str,
    expected: tuple[ChildTrace, ...],
    mutated_file: Path | None,
    mutated_span: tuple[int, int] | None,
    scratch: Path,
) -> set[str]:
    producer, traces = read_traces(data_file)
    expected_phase = environment["MUTATION_CHILD_TRACE_PHASE"]
    if not producer_matches_run(producer, environment, domain, expected_phase):
        raise ValueError("Child trace belongs to another run")
    if expected:
        return verify_child_attempts(traces, expected, mutated_file, mutated_span, scratch)
    return set()


def native_candidate_owners(
    data_file: Path, scratch: Path, target: Path | None, span: tuple[int, int] | None
) -> set[str]:
    if target is None or span is None:
        raise ValueError("Installed child mutation lacks its source span")
    if not data_file.exists():
        coverage = Coverage(data_file=str(data_file), config_file=False)
        coverage.combine(strict=True, keep=True)
        coverage.save()
    lines = coverage_map(data_file, scratch).get(target.relative_to(scratch).as_posix(), {})
    return set(context_tests(line_contexts(lines, *span)))


def attribute_child_result(result: dict, evidence: Path, owners: set[str]) -> dict:
    if result.get("kind") == "import":
        return result
    if result["outcome"] == "KILLED":
        return result if result["node"] in owners else {"outcome": "NO_TESTS"}
    if result["outcome"] == "SURVIVED":
        return result if passed_call_nodes(evidence) & owners else {"outcome": "NO_TESTS"}
    return result


def passed_call_nodes(evidence: Path) -> set[str]:
    calls = [json.loads(line) for line in evidence.read_text().splitlines()]
    return {row["node"] for row in calls if (row["phase"], row["outcome"]) == ("call", "passed")}


def producer_matches_run(
    producer: Producer, environment: dict[str, str], domain: str, phase: str
) -> bool:
    return (
        producer.run_id == environment["MUTATION_CHILD_TRACE_RUN"]
        and producer.domain == domain
        and producer.shard == "local"
        and producer.phase == phase
    )


def coverage_args(
    scratch: Path,
    work: Path,
    baseline: bool,
    env: dict[str, str],
    *,
    data_file: Path | None = None,
) -> list[str]:
    if not baseline:
        return ["--no-cov"]
    config = work / ".coveragerc"
    env["COVERAGE_PROCESS_START"] = str(config)
    sources = [str(scratch / name) for name in ("src", "scripts", "acceptance/src")]
    # Children reread this file and resolve a relative data file against their own working
    # directory; an absolute one keeps the data of children started elsewhere.
    destination = f"data_file = {data_file}\n" if data_file else ""
    config.write_text(
        "[run]\nbranch = True\ncore = ctrace\npatch = subprocess\n"
        "context = ${MUTATION_COVERAGE_CONTEXT-}\n"
        + "source =\n    "
        + "\n    ".join(sources)
        + "\n"
        + destination
    )
    return [
        f"--cov-config={config}",
        *(f"--cov={source}" for source in sources),
        "--cov-context=test",
        "--cov-fail-under=0",
        "--cov-report=",
    ]


def coverage_map(data_file: Path, scratch: Path) -> dict[str, dict[int, list[str]]]:
    if not data_file.exists():
        raise ValueError(f"Missing baseline coverage: {data_file}")
    data = CoverageData(basename=str(data_file))
    data.read()
    result = {}
    for name in data.measured_files():
        path = Path(name).resolve()
        if path.is_relative_to(scratch):
            result[path.relative_to(scratch).as_posix()] = data.contexts_by_lineno(name)
    return result


def baseline(root: Path, timeout: float, *, timings: list[dict] | None = None) -> CoverageEvidence:
    result = {}
    children = []
    with snapshot(root) as scratch:
        for domain in ("unit", "selftest", "fixture"):
            outcome, data = run_domain(scratch, domain, [], timeout, baseline=True)
            if timings is not None:
                timings.append(
                    {key: outcome[key] for key in ("domain", "outcome", "elapsed_seconds")}
                )
            if outcome["outcome"] != "SURVIVED":
                raise ValueError(f"Baseline {domain} failed: {outcome}")
            result[domain] = coverage_map(data, scratch)
            _, traces = read_traces(data)
            for trace in traces:
                source = scratch / trace.source
                if sha256(source.read_bytes()).hexdigest() != trace.source_digest:
                    raise ValueError(
                        f"Installed child source differs from baseline: {trace.source}"
                    )
            children.extend(traces)
    return CoverageEvidence(root.resolve(), result, tuple(children))


def selection(
    mapping: dict | CoverageEvidence, file: str, line: int, end_line: int
) -> dict[str, list[str]]:
    if isinstance(mapping, CoverageEvidence):
        mapping = mapping.lines
    result = {}
    for domain, files in mapping.items():
        lines = files.get(file, {})
        contexts = line_contexts(lines, line, end_line)
        if not contexts:
            continue
        if "" in contexts:
            # Collection imports cannot identify every consumer of constants or decorators.
            contexts = {context for values in lines.values() for context in values}
        result[domain] = context_tests(contexts)
    return result


def child_selection(
    evidence: CoverageEvidence, file: str, line: int, end_line: int
) -> list[ChildTrace]:
    selected = []
    for trace in evidence.children:
        if child_trace_owns_span(trace, evidence, file, line, end_line):
            selected.append(trace)
    return selected


def child_trace_owns_span(
    trace: ChildTrace, evidence: CoverageEvidence, file: str, line: int, end_line: int
) -> bool:
    if not (
        trace.source == file
        and trace.outcome == "reached"
        and trace.test_phase == "run"
        and trace.phase in {"baseline", "producer"}
    ):
        return False
    if sha256((evidence.root / file).read_bytes()).hexdigest() != trace.source_digest:
        raise ValueError(f"Installed child source differs from baseline: {file}")
    return any(line <= number <= end_line for number in trace.lines)


def verify_child_attempts(
    traces: list[ChildTrace],
    expected: tuple[ChildTrace, ...],
    mutated_file: Path | None,
    mutated_span: tuple[int, int] | None,
    scratch: Path,
) -> set[str]:
    if mutated_file is None or mutated_span is None:
        raise ValueError("Installed child mutation lacks its source span")
    source = mutated_file.read_bytes()
    expected_digest = sha256(source).hexdigest()
    source_name = mutated_file.relative_to(scratch).as_posix()
    return {
        route.node
        for route in expected
        if verify_child_route_attempt(traces, route, source_name, expected_digest, mutated_span)
    }


def verify_child_route_attempt(
    traces: list[ChildTrace],
    route: ChildTrace,
    source_name: str,
    expected_digest: str,
    span: tuple[int, int],
) -> bool:
    attempts = child_attempts(traces, route)
    if route.source != source_name:
        raise ValueError("Expected installed child route names another source")
    if any(
        trace.outcome == "reached" and trace.source_digest != expected_digest for trace in attempts
    ):
        raise ValueError("Installed child source differs from the mutation")
    return any(child_attempt_reaches_mutation(trace, expected_digest, span) for trace in attempts)


def child_attempts(traces: list[ChildTrace], route: ChildTrace) -> list[ChildTrace]:
    return [
        trace
        for trace in traces
        if (trace.node, trace.source, trace.module, trace.test_phase)
        == (route.node, route.source, route.module, "run")
    ]


def child_attempt_reaches_mutation(
    trace: ChildTrace, expected_digest: str, span: tuple[int, int]
) -> bool:
    return (
        trace.outcome == "reached"
        and trace.source_digest == expected_digest
        and any(span[0] <= line <= span[1] for line in trace.lines)
    )


def context_tests(contexts: set[str]) -> list[str]:
    tests = set()
    for context in contexts:
        if not context:
            continue
        node, _, phase = context.rpartition("|")
        if "::" not in node or phase not in {"run", "setup", "teardown"}:
            raise ValueError(f"Unrecognized coverage context: {context!r}")
        if phase == "run":
            tests.add(node)
    # Import/setup/teardown alone have no call owner and retain the whole-domain fallback.
    return sorted(tests)


def line_contexts(lines: dict, start: int, end: int) -> set[str]:
    return {
        context
        for number, values in lines.items()
        if start <= int(number) <= end
        for context in values
    }
