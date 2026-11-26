"""A real PDM launched by a test stays in its owned project and leaves evidence of what ran."""

import contextlib
import hashlib
import io
import json
import os
import signal
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from subprocess import TimeoutExpired
from unittest.mock import patch

from scripts.processes import wait_until

from test_pdm_launch import create_project, pdm_run

SUBJECT = '[project]\nname = "isolation-subject"\nversion = "0"\nrequires-python = ">=3.14"\n'
SHOW = "import os, sys; print(os.environ['PDM_PROJECT_ROOT'], sys.prefix)"
CANARY = "canary-9f3a1c"
STUB = '#!/bin/sh\ntouch "{marker}"\nexec "{real}" "$@"\n'


def outer_stand_in(base):
    """A sacrificial stand-in for the suite's project and environment; any use of it is recorded."""
    outer = base / "outer"
    marker = outer / "used"
    (outer / "bin").mkdir(parents=True)
    (outer / ".venv/bin").mkdir(parents=True)
    (outer / "home").mkdir()
    stub = outer / "bin/python"
    stub.write_text(STUB.format(marker=marker, real=sys.executable))
    stub.chmod(0o755)
    (outer / ".venv/bin/python").symlink_to(stub)
    (outer / ".venv/pyvenv.cfg").write_text("home = " + str(outer / "bin") + "\n")
    (outer / "home/config.toml").write_text("check_update = false\n")
    create_project(outer, interpreter=stub, pyproject=SUBJECT)
    return outer


def hostile_selectors(outer):
    return {
        "PDM_PROJECT": str(outer),
        "PDM_PYTHON": str(outer / "bin/python"),
        "VIRTUAL_ENV": str(outer / ".venv"),
        "HOME": str(outer / "home"),
        "XDG_CACHE_HOME": str(outer / "home/cache"),
        "XDG_CONFIG_HOME": str(outer / "home/config"),
        "PDM_CONFIG_FILE": str(outer / "home/config.toml"),
    }


def tree(root):
    found = {}
    for path in sorted(root.rglob("*")):
        relative = str(path.relative_to(root))
        if path.is_symlink():
            found[relative] = f"-> {os.readlink(path)}"
        elif path.is_file():
            found[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
        else:
            found[relative] = "dir"
    return found


def changed(before, after):
    return {
        name: "removed" if name not in after else "created" if name not in before else "modified"
        for name in sorted(before.keys() | after.keys())
        if before.get(name) != after.get(name)
    }


def launch_with_evidence(*command, **options):
    stream = io.StringIO()
    with contextlib.redirect_stderr(stream):
        try:
            outcome = pdm_run(*command, **options)
        except TimeoutExpired as error:
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
                    "pdm",
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


class OwnedCleanupTest(unittest.TestCase):
    def test_timeout_stops_owned_processes_and_removes_only_owned_scratch(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            outer = outer_stand_in(base)
            before = tree(outer)
            work = create_project(base / "work", pyproject=SUBJECT)
            pid_file = base / "pid"
            code = (
                f"import os, time; open({str(pid_file)!r}, 'w').write(str(os.getpid())); "
                "print('ready', flush=True); time.sleep(120)"
            )
            try:
                error, text = launch_with_evidence("python", "-c", code, cwd=work, timeout=10)
                self.assertIsInstance(error, TimeoutExpired, text)
                pid = int(pid_file.read_text())

                def gone():
                    try:
                        os.kill(pid, 0)
                    except ProcessLookupError:
                        return True
                    return False

                self.assertTrue(wait_until(gone, 10, interval=0.1), "grandchild outlived launch")
                self.assertEqual(error.stdout.strip(), "ready", text)
            finally:
                if pid_file.exists():
                    with contextlib.suppress(ProcessLookupError):
                        os.kill(int(pid_file.read_text()), signal.SIGKILL)
            fields = evidence_fields(text)
            self.assertEqual(fields.get("outcome"), "timeout after 10s", text)
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


if __name__ == "__main__":
    unittest.main()
