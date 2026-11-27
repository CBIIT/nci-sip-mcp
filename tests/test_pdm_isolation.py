"""A real PDM launched by a test stays in its owned project and leaves evidence of what ran."""

import contextlib
import hashlib
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from subprocess import TimeoutExpired
from unittest.mock import patch
from xml.etree import ElementTree

from pdm_stand_in import SHOW, SUBJECT, changed, hostile_selectors, outer_stand_in, tree
from scripts.processes import wait_until

from fakes import create_project, pdm_run

CANARY = "canary-9f3a1c"
SUPPORT = Path(__file__).resolve().parents[1] / "test_support"


def launch_with_evidence(*command, **options):
    stream = io.StringIO()
    with contextlib.redirect_stderr(stream):
        try:
            outcome = pdm_run(*command, **options)
        except Exception as error:  # noqa: BLE001 - the launch's own outcome is the datum
            outcome = error
    return outcome, stream.getvalue()


def evidence_fields(text):
    fields = {}
    for line in text.splitlines():
        label, _, value = line.partition(": ")
        if label.startswith("pdm-launch "):
            fields[label.removeprefix("pdm-launch ")] = json.loads(value)
    return fields


class HostileSelectorsTest(unittest.TestCase):
    def test_launch_selects_only_the_owned_project_whatever_the_parent_selects(self):
        for case, interpreter in (("healthy", sys.executable), ("unusable", "missing-python")):
            with self.subTest(child=case), tempfile.TemporaryDirectory() as directory:
                base = Path(directory).resolve()
                outer = outer_stand_in(base)
                before = tree(outer)
                child = create_project(base / "child", interpreter=interpreter, pyproject=SUBJECT)
                work = base / "work"
                work.mkdir()
                with patch.dict(os.environ, hostile_selectors(outer)):
                    os.environ.pop("PDM_IGNORE_ACTIVE_VENV", None)
                    result = pdm_run("python", "-c", SHOW, cwd=work, timeout=120, project=child)
                self.assertEqual(changed(before, tree(outer)), {}, result.stdout + result.stderr)
                if case == "healthy":
                    self.assertEqual(
                        (result.returncode, result.stdout.split()),
                        (0, [str(child), sys.prefix]),
                        result.stderr,
                    )
                else:
                    self.assertNotIn(str(outer), result.stdout + result.stderr)
                    self.assertTrue((child / ".venv").is_dir(), result.stdout + result.stderr)


class ClosedStream(io.TextIOBase):
    def write(self, _text):
        raise OSError("evidence stream closed")


class FailureEvidenceTest(unittest.TestCase):
    def test_failed_launch_reports_its_context_without_secrets_and_keeps_its_outcome(self):
        code = "import os, sys; print('seen', os.environ.get('LEAK', 'absent')); sys.exit(3)"
        with tempfile.TemporaryDirectory() as directory:
            work = create_project(Path(directory).resolve() / "work", pyproject=SUBJECT)
            with patch.dict(os.environ, {"LEAK": CANARY}):
                result, text = launch_with_evidence("python", "-c", code, cwd=work, timeout=120)
            fields = evidence_fields(text)
            self.assertEqual(
                (result.returncode, result.stdout.strip()), (3, "seen absent"), result.stderr
            )
            self.assertNotIn(CANARY, text)
            self.assertEqual(fields.get("outcome"), "returncode=3", text)
            self.assertEqual(fields.get("cwd"), str(work), text)
            self.assertEqual(fields.get("interpreter"), sys.executable, text)
            self.assertEqual(fields.get("stdout"), "seen absent\n", text)
            self.assertEqual(fields.get("stderr"), result.stderr, text)
            project = fields.get("project")
            self.assertEqual(
                fields.get("argv"),
                [
                    shutil.which("pdm"),
                    "run",
                    "--project",
                    project,
                    "--working-dir",
                    str(work),
                    "python",
                    "-c",
                    code,
                ],
                text,
            )
            start, end = (datetime.fromisoformat(fields[name]) for name in ("started", "ended"))
            self.assertLessEqual(start, end)
            self.assertEqual(start.utcoffset().total_seconds(), 0)
            identity = hashlib.sha256(sys.executable.encode()).hexdigest()[:12]
            self.assertEqual(
                fields.get("interpreter-file"), {"before": identity, "after": identity}, text
            )
            with contextlib.redirect_stderr(ClosedStream()):
                unrecorded = pdm_run("python", "-c", code, cwd=work, timeout=120)
            self.assertEqual(
                (unrecorded.returncode, unrecorded.stdout.strip()), (3, "seen absent"), text
            )


class EvidenceCollectionTest(unittest.TestCase):
    def test_unreadable_interpreter_file_keeps_the_exit_code_and_is_reported_unavailable(self):
        code = (
            "import os, pathlib, sys\n"
            "p = pathlib.Path(os.environ['PDM_PROJECT_ROOT']) / '.pdm-python'\n"
            "p.unlink(); p.mkdir(); print('kept'); sys.exit(3)"
        )
        with tempfile.TemporaryDirectory() as directory:
            work = create_project(Path(directory).resolve() / "work", pyproject=SUBJECT)
            result, text = launch_with_evidence("python", "-c", code, cwd=work, timeout=120)
        self.assertIsInstance(result, subprocess.CompletedProcess, text)
        self.assertEqual((result.returncode, result.stdout.strip()), (3, "kept"), text)
        identity = evidence_fields(text).get("interpreter-file", {})
        self.assertEqual(
            identity.get("before"), hashlib.sha256(sys.executable.encode()).hexdigest()[:12]
        )
        self.assertEqual(identity.get("after"), "unavailable: IsADirectoryError", text)

    def test_the_launched_executable_is_the_resolved_pdm_of_the_child_path(self):
        with tempfile.TemporaryDirectory() as directory:
            work = create_project(Path(directory).resolve() / "work", pyproject=SUBJECT)
            _result, text = launch_with_evidence("python", "-c", "pass", cwd=work, timeout=120)
        argv = evidence_fields(text).get("argv", [""])
        self.assertEqual(argv[0], shutil.which("pdm"), text)
        self.assertTrue(Path(argv[0]).is_absolute(), text)

    def test_a_missing_pdm_fails_clearly_without_naming_the_path(self):
        with tempfile.TemporaryDirectory() as directory:
            empty = Path(directory).resolve()
            work = create_project(empty / "work", pyproject=SUBJECT)
            outcome, text = launch_with_evidence(
                "python", "-c", "pass", cwd=work, timeout=5, environment={"PATH": str(empty)}
            )
        self.assertIsInstance(outcome, FileNotFoundError)
        message = "pdm executable not found on the launch PATH"
        self.assertEqual(str(outcome), message)
        fields = evidence_fields(text)
        self.assertEqual(fields.get("outcome"), f"FileNotFoundError: {message}", text)
        self.assertEqual(fields.get("argv", [""])[0], "unavailable", text)
        self.assertEqual(fields.get("cwd"), str(work), text)
        self.assertIn("project", fields, text)
        self.assertNotIn(str(empty), text.replace(str(work), ""))

    def test_child_sees_only_the_coverage_keys_a_nested_baseline_needs(self):
        probe = (
            "import os; print(sorted(k for k in os.environ "
            "if k.startswith(('COVERAGE', 'MUTATION'))))"
        )
        inherited = {
            "COVERAGE_FILE": "/owned/.coverage",
            "COVERAGE_PROCESS_CONFIG": "{}",
            "MUTATION_COVERAGE_CONTEXT": "ctx",
            "COVERAGE_RCFILE": "/hostile/rc",
            "COVERAGE_DEBUG": "all",
            "MUTATION_EVIDENCE": "/hostile/events",
        }
        with tempfile.TemporaryDirectory() as directory:
            work = create_project(Path(directory).resolve() / "work", pyproject=SUBJECT)
            with patch.dict(os.environ, inherited):
                os.environ.pop("COVERAGE_PROCESS_START", None)
                result = pdm_run("python", "-c", probe, cwd=work, timeout=120)
        self.assertEqual(
            result.stdout.strip(),
            "['COVERAGE_FILE', 'COVERAGE_PROCESS_CONFIG', 'MUTATION_COVERAGE_CONTEXT']",
            result.stderr,
        )


SWAP_THEN_WAIT = """
import os, pathlib, time
root = pathlib.Path(os.environ["PDM_PROJECT_ROOT"])
(root / ".pdm-python").unlink()
(root / ".pdm-python").mkdir()
pathlib.Path({pid!r}).write_text(str(os.getpid()))
print("ready", flush=True)
os.close(1)
os.close(2)
release = pathlib.Path({release!r})
while not release.exists():
    time.sleep(0.05)
"""


def alive(pid):
    query = subprocess.run(  # noqa: S603 - fixed ps query of a pid this test learned
        ["ps", "-o", "stat=", "-p", str(pid)],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    )
    state = query.stdout.strip()
    if query.returncode == 1 and not state and not query.stderr:
        return False  # ps exits 1 with no output for a pid that does not exist
    if query.returncode != 0 or query.stderr or not state:
        raise AssertionError(f"the process query failed: {query.returncode} {query.stderr!r}")
    return not state.startswith("Z")


class ProcessQueryTest(unittest.TestCase):
    def query(self, returncode, stdout="", stderr=""):
        done = subprocess.CompletedProcess(["ps"], returncode, stdout, stderr)
        with patch.object(subprocess, "run", return_value=done):
            return alive(4242)

    def test_dead_is_no_such_pid_or_zombie_and_a_failed_query_is_not_death(self):
        self.assertTrue(self.query(0, "S+\n"))
        self.assertFalse(self.query(1))
        self.assertFalse(self.query(0, "Z+\n"))
        for failure in ((2, "", "ps: illegal option"), (1, "", "ps: boom"), (0, "", "")):
            with self.subTest(failure=failure), self.assertRaises(AssertionError):
                self.query(*failure)


class OwnedCleanupTest(unittest.TestCase):
    def test_timeout_stops_the_launched_process_not_only_the_launcher(self):
        # The child closes its pipes and waits for a release file, so a launcher-only kill ends
        # the launch with the child still alive; the release file is containment, not the oracle.
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            outer = outer_stand_in(base)
            before = tree(outer)
            work = create_project(base / "work", pyproject=SUBJECT)
            pid_file, release = base / "pid", base / "release"
            code = SWAP_THEN_WAIT.format(pid=str(pid_file), release=str(release))
            try:
                error, text = launch_with_evidence("python", "-c", code, cwd=work, timeout=10)
                self.assertTrue(pid_file.exists(), "the child never started: " + text)
                self.assertIsInstance(error, TimeoutExpired, text)
                pid = int(pid_file.read_text())
                self.assertTrue(
                    wait_until(lambda: not alive(pid), 10, interval=0.1),
                    "the launched process outlived the launch",
                )
            finally:
                release.touch()
                if pid_file.exists():
                    with contextlib.suppress(ProcessLookupError):
                        os.kill(int(pid_file.read_text()), signal.SIGKILL)
            fields = evidence_fields(text)
            self.assertEqual(fields.get("outcome"), "timeout after 10s", text)
            self.assertEqual(error.stdout.strip(), "ready", text)
            # The child replaced `.pdm-python` by a directory: the timeout still propagates.
            identity = fields.get("interpreter-file", {})
            self.assertTrue(str(identity.get("after")).startswith("unavailable"), text)
            self.assertFalse(Path(fields.get("project", str(base))).exists(), text)
            self.assertEqual(changed(before, tree(outer)), {})

    def test_successful_launch_removes_its_owned_project(self):
        with tempfile.TemporaryDirectory() as directory:
            work = create_project(Path(directory) / "work", pyproject=SUBJECT)
            result, text = launch_with_evidence("python", "-c", "pass", cwd=work, timeout=120)
            project = evidence_fields(text).get("project")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIsNotNone(project, text)
            self.assertFalse(Path(project or directory).exists(), text)


INNER_TESTS = """
import os
import unittest
from unittest.mock import patch

from pdm_launch import pdm_run

LEAK = {{"LEAK": {canary!r}}}


class Inner(unittest.TestCase):
    def test_expected_error(self):
        with patch.dict(os.environ, LEAK):
            result = pdm_run(
                "python", "-c",
                "import os, sys; print('ctx-out', os.environ.get('LEAK', 'absent')); sys.exit(2)",
                timeout=120,
            )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_timeout(self):
        with patch.dict(os.environ, LEAK):
            pdm_run("python", "-c", "import time; time.sleep(60)", timeout=3)
"""


class JunitReportTest(unittest.TestCase):
    def test_failed_launches_keep_their_evidence_in_the_projects_junit_report(self):
        # The pytest flags are those `pdm run test` and the CI unit job pass (pyproject.toml's
        # `test` script, ci.yml's `--junitxml`, `-vv` and `faulthandler_timeout`), nothing else.
        with tempfile.TemporaryDirectory() as directory:
            inner = create_project(Path(directory).resolve() / "inner")
            (inner / "tests").mkdir()
            (inner / "test_support").mkdir()
            shutil.copy(SUPPORT / "pdm_launch.py", inner / "test_support/pdm_launch.py")
            (inner / "tests/test_inner.py").write_text(INNER_TESTS.format(canary=CANARY))
            report = inner / "tmp/test-results/unit.xml"
            result = pdm_run(
                "test",
                "--cov=example",
                "--cov-fail-under=0",
                "--cov-report=",
                f"--junitxml={report}",
                "-vv",
                "-o",
                "faulthandler_timeout=60",
                project=inner,
                timeout=600,
            )
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            text = report.read_text()
        self.assertNotIn(CANARY, text)
        cases = {
            case.get("name"): "".join(
                part.text or ""
                for part in case.iter()
                if part.tag in ("failure", "error", "system-err")
            )
            for case in ElementTree.fromstring(text).iter("testcase")  # noqa: S314 - our own report
        }
        expected = cases.get("test_expected_error", "")
        self.assertIn("AssertionError: 2 != 0", expected)
        self.assertIn("pdm-launch argv", expected)
        self.assertIn('"returncode=2"', expected)
        self.assertIn(sys.executable, expected)
        self.assertIn("ctx-out absent", expected)
        timed_out = cases.get("test_timeout", "")
        self.assertIn("TimeoutExpired", timed_out)
        self.assertIn('pdm-launch outcome: "timeout after 3s"', timed_out)


if __name__ == "__main__":
    unittest.main()
