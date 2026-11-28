"""Fixed local validation profiles do not inherit production secrets or serving data."""

import io
import json
import os
import signal
import subprocess
import sys
import unittest
from contextlib import redirect_stderr, suppress
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from scripts.benchmark_limits import Limits
from scripts.operator_worker import _server, clean_environment, fixture_benchmark
from scripts.processes import wait_until

OWNED_SERVER_LOG = "Owned server log, last 65536 bytes:\n"  # the heading of the log tail on failure


def children_of(pid: int) -> list[int]:
    found = subprocess.run(  # noqa: S603 - fixed argv
        ["/usr/bin/pgrep", "-P", str(pid)], capture_output=True, text=True, check=False
    )
    return [int(child) for child in found.stdout.split()]


def kill_tree(worker: subprocess.Popen[bytes], children: list[int]) -> None:
    """Leave no server behind when the test fails; an orphaned server is no longer a child."""
    for pid in (*children, *children_of(worker.pid), worker.pid):
        with suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)
    worker.wait(timeout=10)


# Test-only bootstrap: the same worker module and argv, plus a nonfatal on-request stack dump
# (SIGUSR1). The worker still installs its own SIGTERM handler in its `__main__` block (#661).
WORKER_BOOTSTRAP = (
    "import faulthandler, runpy, signal, sys; "
    "faulthandler.register(signal.SIGUSR1, all_threads=True); "
    "runpy.run_module('scripts.operator_worker', run_name='__main__', alter_sys=True)"
)
EXCERPT = 8192  # bytes kept from each diagnostic file, so a failure report stays bounded


def _tail(path: Path) -> str:
    """The last EXCERPT bytes of `path`, read without loading the rest of the file."""
    with path.open("rb") as stream:
        size = stream.seek(0, os.SEEK_END)
        start = max(0, size - EXCERPT)
        stream.seek(start)
        data = stream.read(EXCERPT)
    clipped = f"[first {start} bytes omitted]\n" if start else ""
    return clipped + data.decode(errors="replace")


def stack_note(worker: subprocess.Popen[bytes], stderr: Path, directory: Path) -> str:
    """Ask the owned worker for its thread stacks and collect bounded excerpts."""
    before = stderr.stat().st_size
    os.kill(worker.pid, signal.SIGUSR1)
    grew = wait_until(lambda: stderr.stat().st_size > before, 5, interval=0.05)
    # Growth shows only that something was written; the excerpt shows whether it is a stack.
    lines = [
        "worker stacks requested (SIGUSR1); stderr "
        + ("grew afterwards (possibly partial)" if grew else "did not grow within 5 s"),
        "worker stderr:\n" + _tail(stderr),
    ]
    lines += [
        f"{log.name}:\n" + _tail(log) for log in sorted(directory.glob("worker-*/server.log"))
    ]
    return "\n".join(lines)


def wait_or_diagnose(
    worker: subprocess.Popen[bytes], seconds: float, stderr: Path, directory: Path
) -> int:
    """Wait as before; on timeout, attach what the diagnostic found and re-raise that timeout."""
    try:
        return worker.wait(timeout=seconds)
    except subprocess.TimeoutExpired as timeout:
        try:
            note = stack_note(worker, stderr, directory)
        except Exception as error:  # noqa: BLE001 - the original timeout stays the failure
            note = f"worker diagnostic unavailable: {type(error).__name__}: {error}"
        timeout.add_note(note)
        raise


class OperatorEnvironmentTest(unittest.TestCase):
    def test_child_imports_come_from_the_owned_source_root(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = root / "src/nci_sip_mcp"
            package.mkdir(parents=True)
            (package / "__init__.py").write_text("SOURCE_MARKER = 'owned snapshot'\n")
            with patch("scripts.operator_worker.acceptance_http.ROOT", root):
                environment = clean_environment(root)
            result = subprocess.run(
                [sys.executable, "-c", "import nci_sip_mcp; print(nci_sip_mcp.SOURCE_MARKER)"],
                env=environment,
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "owned snapshot")

    def test_readiness_timeout_reaps_owned_child(self):
        spawn = subprocess.Popen
        children = []

        def sleeping(_command, **options):
            child = spawn([sys.executable, "-c", "import time; time.sleep(300)"], **options)
            children.append(child)
            return child

        with TemporaryDirectory() as directory:
            with (
                patch("nci_sip_acceptance.client.subprocess.Popen", side_effect=sleeping),
                patch("nci_sip_acceptance.client.time.monotonic", side_effect=(0, 16, 16)),
                patch("nci_sip_acceptance.client.socket.create_connection", side_effect=OSError),
                self.assertRaisesRegex(TimeoutError, "did not become ready"),
                _server(Path(directory), {}),
            ):
                self.fail("Unready process must not be measured")
            self.assertIsNotNone(children[0].poll())

    def test_owned_server_does_not_require_an_index_in_the_child(self):
        spawn = subprocess.Popen
        environments = []
        listens = (
            "import os, socket, time; listener = socket.socket(); "
            "listener.bind(('127.0.0.1', int(os.environ['NCI_SIP_HTTP_PORT']))); "
            "listener.listen(); time.sleep(300)"
        )

        def listening(_command, **options):
            environments.append(options["env"])
            return spawn([sys.executable, "-c", listens], **options)

        with (
            TemporaryDirectory() as directory,
            patch("nci_sip_acceptance.client.subprocess.Popen", side_effect=listening),
            _server(Path(directory), {}),
        ):
            pass
        self.assertEqual(environments[0]["NCI_SIP_HTTP_REQUIRE_INDEX"], "0")

    def test_sigterm_ends_a_running_worker_and_reaps_its_owned_server(self):
        with TemporaryDirectory() as directory:
            output = Path(directory) / "report.json"
            stderr = Path(directory) / "worker.stderr"
            with stderr.open("wb") as stream:
                worker = subprocess.Popen(  # noqa: S603 - fixed project worker in an owned directory
                    [
                        sys.executable,
                        "-c",
                        WORKER_BOOTSTRAP,
                        "benchmark-http-fixture",
                        "--output",
                        str(output),
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=stream,
                )
            children: list[int] = []
            try:
                # server.log is opened before the server starts, so wait for the child itself.
                self.assertTrue(wait_until(lambda: children_of(worker.pid), 30, interval=0.05))
                children = children_of(worker.pid)
                worker.send_signal(signal.SIGTERM)
                # Unwound as by Ctrl-C: Python then re-raises SIGINT for its own exit status.
                code = wait_or_diagnose(worker, 30, stderr, Path(directory))
                self.assertEqual(code, -signal.SIGINT)
            except BaseException:
                kill_tree(worker, children)
                raise
        self.assertEqual(len(children), 1)
        with self.assertRaises(ProcessLookupError):
            os.kill(children[0], 0)

    def registered_child(self, directory: Path, stderr: Path) -> subprocess.Popen[bytes]:
        """A small owned child that announces readiness only after registering its handler."""
        ready = directory / "ready"
        code = (
            "import faulthandler, signal, sys, time; "
            "faulthandler.register(signal.SIGUSR1, all_threads=True); "
            "open(sys.argv[1], 'w').close(); time.sleep(300)"
        )
        with stderr.open("wb") as stream:
            child = subprocess.Popen([sys.executable, "-c", code, str(ready)], stderr=stream)  # noqa: S603
        self.addCleanup(child.wait, timeout=10)
        self.addCleanup(child.kill)
        self.assertTrue(wait_until(ready.exists, 10, interval=0.05))
        return child

    def test_a_worker_timeout_keeps_its_exception_and_carries_the_workers_stacks(self):
        with TemporaryDirectory() as temporary:
            directory = Path(temporary)
            stderr = directory / "child.stderr"
            child = self.registered_child(directory, stderr)
            with self.assertRaises(subprocess.TimeoutExpired) as raised:
                wait_or_diagnose(child, 0.1, stderr, directory)
            self.assertEqual(raised.exception.timeout, 0.1)
            [note] = raised.exception.__notes__
            self.assertIn("stderr grew afterwards (possibly partial)", note)
            self.assertIn("most recent call first", note)  # faulthandler's own header
            self.assertIsNone(child.poll())  # the stack request is not fatal

    def test_a_diagnostic_excerpt_keeps_the_tail_and_says_what_it_omitted(self):
        with TemporaryDirectory() as temporary:
            log = Path(temporary) / "server.log"
            log.write_bytes(b"h" * 10 + b"t" * EXCERPT)
            self.assertEqual(_tail(log), "[first 10 bytes omitted]\n" + "t" * EXCERPT)
            log.write_bytes(b"short")
            self.assertEqual(_tail(log), "short")

    def test_a_failed_diagnostic_still_raises_the_original_timeout(self):
        with TemporaryDirectory() as temporary:
            directory = Path(temporary)
            stderr = directory / "child.stderr"
            child = self.registered_child(directory, stderr)
            missing = directory / "absent.stderr"
            cases = {
                "stderr missing": (missing, None, "FileNotFoundError: [Errno 2] No such file "
                                   f"or directory: '{missing}'"),
                "log listing fails": (stderr, PermissionError("listing denied"),
                                      "PermissionError: listing denied"),
            }  # fmt: skip
            for name, (path, listing, expected) in cases.items():
                with self.subTest(name), patch.object(Path, "glob", side_effect=listing):
                    with self.assertRaises(subprocess.TimeoutExpired) as raised:
                        wait_or_diagnose(child, 0.1, path, directory)
                    self.assertEqual(raised.exception.timeout, 0.1)
                    self.assertEqual(
                        raised.exception.__notes__, [f"worker diagnostic unavailable: {expected}"]
                    )
                    self.assertIsNone(child.poll())

    def test_fixed_benchmark_command_writes_report_and_returns_success(self):
        with TemporaryDirectory() as directory:
            output = Path(directory) / "report.json"
            result = subprocess.run(  # noqa: S603 - fixed project worker in an owned directory
                [
                    sys.executable,
                    "-m",
                    "scripts.operator_worker",
                    "benchmark-http-fixture",
                    "--output",
                    str(output),
                ],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(output.is_file())
            report = json.loads(output.read_text())
            self.assertTrue(report["complete"])
            self.assertEqual(len(report["cases"]), 10)

    def test_unrecorded_fixture_request_invalidates_completed_report(self):
        with TemporaryDirectory() as directory:
            output = Path(directory) / "report.json"
            diagnostics = io.StringIO()
            with (
                patch("scripts.operator_worker.unmatched_requests", return_value=["missing"]),
                self.assertRaisesRegex(RuntimeError, "unrecorded"),
                redirect_stderr(diagnostics),
            ):
                fixture_benchmark(
                    output, limits=Limits(repetitions=1, warmups=0), names=["get_concept"]
                )
            _, heading, tail = diagnostics.getvalue().partition(OWNED_SERVER_LOG)
            self.assertEqual(heading, OWNED_SERVER_LOG)
            self.assertIn('"call_completed"', tail)
            written = output.read_text()
            report = json.loads(written)
            self.assertFalse(report["complete"])
            self.assertEqual(report["state"], "failed")
            self.assertEqual(report["stopReason"], "runner_error")
            self.assertEqual(written, json.dumps(report, indent=2) + "\n")

    def test_fixture_benchmark_runs_owned_http_server_and_removes_working_data(self):
        with TemporaryDirectory() as directory:
            output = Path(directory) / "report.json"
            diagnostics = io.StringIO()
            with (
                patch.dict(
                    "os.environ",
                    {
                        "NCI_SIP_DATA_DIR": "/production/index",
                        "AWS_SECRET_ACCESS_KEY": "PRIVATE-CANARY",
                    },
                ),
                redirect_stderr(diagnostics),
            ):
                result = fixture_benchmark(
                    output,
                    limits=Limits(repetitions=1, warmups=0),
                    names=["get_concept"],
                )
            self.assertTrue(result["complete"])
            self.assertEqual(result["mode"], "fixture")
            self.assertEqual(len(result["cases"]), 1)
            self.assertTrue(
                all(case["warm"]["summary"]["errorRate"] == 0 for case in result["cases"])
            )
            self.assertEqual(json.loads(output.read_text()), result)
            self.assertEqual([path.name for path in Path(directory).iterdir()], ["report.json"])
            self.assertNotIn("PRIVATE-CANARY", output.read_text())
            self.assertNotIn(OWNED_SERVER_LOG, diagnostics.getvalue())

    def test_child_environment_uses_owned_home_and_drops_credentials_proxies_and_settings(self):
        parent = {
            "PATH": "/runtime/bin",
            "HOME": "/human/home",
            "LANG": "en_US.UTF-8",
            "AWS_SECRET_ACCESS_KEY": "PRIVATE-CANARY",
            "HTTPS_PROXY": "http://proxy",
            "NCI_SIP_DATA_DIR": "/production/index",
            "NCI_SIP_CADSR_API_KEY": "PRIVATE-CANARY",
            "PYTHONPATH": "/untrusted/imports",
            "SSH_AUTH_SOCK": "/agent/socket",
        }
        with TemporaryDirectory() as directory, patch.dict("os.environ", parent, clear=True):
            environment = clean_environment(Path(directory))
        self.assertEqual(environment["PATH"], "/runtime/bin")
        self.assertEqual(environment["HOME"], directory)
        self.assertEqual(environment["TMPDIR"], directory)
        self.assertEqual(environment["LANG"], "en_US.UTF-8")
        self.assertNotIn("PRIVATE-CANARY", str(environment))
        self.assertNotIn("HTTPS_PROXY", environment)
        self.assertNotIn("/untrusted/imports", environment.get("PYTHONPATH", ""))
        self.assertNotIn("SSH_AUTH_SOCK", environment)
        self.assertNotIn("NCI_SIP_DATA_DIR", environment)
