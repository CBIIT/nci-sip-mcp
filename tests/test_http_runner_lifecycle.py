"""The HTTP acceptance gate cannot reuse stale evidence or leave an owned child alive."""

import io
import json
import shlex
import subprocess
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from scripts import acceptance_http as runner


class HTTPRunnerLifecycleTest(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.report = self.directory / "report.json"
        self.valid = {
            "mode": "fixture",
            "run": {"exit_status": 0, "selected": 5, "finished": 5, "worker_crashes": 0},
            "transport": "streamable-http",
            "failed_gates": [],
            "tests": {name: {"outcome": "skipped"} for name in runner.UNPREPARED}
            | {"required-case": {"outcome": "passed"}},
        }
        expected = self.directory / "expected"
        expected.mkdir()
        (expected / "fixture.json").write_text(
            json.dumps(dict.fromkeys(self.valid["tests"], "passed"))
        )
        for name, value in (("REPORT", self.report), ("ACCEPTANCE", self.directory)):
            replacement = patch.object(runner, name, value)
            replacement.start()
            self.addCleanup(replacement.stop)

    def run_with(self, report, exit_code):
        return self.run_capturing(report, exit_code)[0]

    def run_capturing(self, report, exit_code):
        def wait():
            if report is not None:
                self.report.write_text(json.dumps(report))
            return exit_code

        process = Mock(wait=Mock(side_effect=wait), poll=Mock(return_value=exit_code))
        diagnostics = io.StringIO()
        with (
            patch.object(runner.subprocess, "Popen", return_value=process),
            redirect_stdout(io.StringIO()),
            redirect_stderr(diagnostics),
        ):
            return runner.run_suite(8000, self.directory / "control"), diagnostics.getvalue()

    def test_old_success_is_removed_before_a_run_that_writes_no_report(self):
        self.report.write_text(json.dumps(self.valid))
        self.assertEqual(
            self.run_capturing(None, 0), (2, "The suite exited 0 and wrote no report.\n")
        )
        self.assertFalse(self.report.exists())

    def test_a_complete_report_cannot_override_a_failed_process(self):
        self.assertEqual(
            self.run_capturing(self.valid, 1),
            (1, "The suite exited 1 although every outcome is the expected one.\n"),
        )
        self.assertEqual(json.loads(self.report.read_text()), self.valid)

    def test_zero_exit_still_requires_the_complete_remote_verdict(self):
        incomplete = self.valid | {
            "tests": {"required-case": {"outcome": "passed"}},
            "run": {"exit_status": 0, "selected": 1, "finished": 1, "worker_crashes": 0},
        }
        self.assertEqual(self.run_with(incomplete, 0), 1)
        self.assertEqual(self.run_with(self.valid, 0), 0)

    def test_zero_exit_cannot_override_an_incomplete_report_or_a_worker_crash(self):
        for run in (
            {"exit_status": 0, "selected": 6, "finished": 5, "worker_crashes": 0},
            {"exit_status": 0, "selected": 5, "finished": 5, "worker_crashes": 1},
        ):
            with self.subTest(run=run):
                code, diagnostics = self.run_capturing(self.valid | {"run": run}, 0)
                self.assertEqual(code, 2)
                self.assertTrue(diagnostics.endswith(" is an incomplete run; re-run it\n"))

    def test_a_suite_that_crashed_is_not_compared_and_says_so(self):
        for status in (2, 3, 5):  # interrupted, internal error, no tests collected
            with self.subTest(status=status):
                self.assertEqual(
                    self.run_capturing(self.valid, status),
                    (2, f"The suite exited {status} without a usable run.\n"),
                )

    def test_a_verdict_mismatch_names_each_difference(self):
        failed = json.loads(json.dumps(self.valid))
        failed["tests"]["required-case"]["outcome"] = "failed"
        failed |= {"transport": "stdio", "failed_gates": ["inventory"]}
        del failed["tests"][sorted(runner.UNPREPARED)[0]]
        failed["run"] |= {"selected": 4, "finished": 4}
        self.assertEqual(
            self.run_capturing(failed, 1),
            (
                1,
                "required-case: expected passed, observed failed\n"
                f"{sorted(runner.UNPREPARED)[0]}: expected skipped, observed missing\n"
                "transport: expected streamable-http, observed stdio\n"
                "failed gates: inventory\n",
            ),
        )

    def test_operator_report_and_environment_are_isolated_from_default_ci_output(self):
        private = self.directory / "operator.json"
        self.report.write_text("Keep existing engineering result")
        observed = []

        def launch(command, **options):
            observed.append((command, options["env"]))
            private.write_text(json.dumps(self.valid))
            return Mock(wait=Mock(return_value=0), poll=Mock(return_value=0))

        with (
            patch.object(runner.subprocess, "Popen", side_effect=launch),
            redirect_stdout(io.StringIO()),
        ):
            result = runner.run_suite(
                8000,
                self.directory / "control",
                report=private,
                environment={
                    "PATH": "controlled-path",
                    "HOME": "isolated-home",
                    "NCI_SIP_ACCEPTANCE_REGISTRY_RELEASE": "optional",
                    "NCI_SIP_DATA_DIR": "inherited-index",
                },
            )
        self.assertEqual(result, 0)
        self.assertEqual(self.report.read_text(), "Keep existing engineering result")
        command, environment = observed[0]
        self.assertIn("--report=" + str(private), command)
        self.assertEqual(
            environment,
            {
                "PATH": "controlled-path",
                "HOME": "isolated-home",
                "NCI_SIP_ACCEPTANCE_URL": "http://127.0.0.1:8000/mcp",
                "NCI_SIP_ACCEPTANCE_MODE": "fixture",
                "NCI_SIP_ACCEPTANCE_PREPARED": "1",
                "NCI_SIP_ACCEPTANCE_REGISTRY_RELEASE": "required",
                "NCI_SIP_ACCEPTANCE_SECURITY_SERVER": shlex.join(
                    [sys.executable, str(runner.ROOT / "scripts/permissions_fixture.py")]
                ),
                "NCI_SIP_ACCEPTANCE_STATE_HOOK": shlex.join(
                    [
                        sys.executable,
                        "-m",
                        "scripts.acceptance_http",
                        "state",
                        str(self.directory / "control"),
                    ]
                ),
                "PYTHONPATH": str(runner.ROOT),
            },
        )

    def test_cancellation_reaps_the_owned_suite_process_before_propagating(self):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
        wait = child.wait

        def interrupted_wait(*, timeout=None):
            if timeout is None:
                raise KeyboardInterrupt
            return wait(timeout=timeout)

        try:
            with (
                patch.object(runner.subprocess, "Popen", return_value=child),
                patch.object(child, "wait", side_effect=interrupted_wait),
                self.assertRaises(KeyboardInterrupt),
            ):
                runner.run_suite(8000, self.directory / "control")
            self.assertIsNotNone(child.poll())
            self.assertFalse(self.report.exists())
        finally:
            if child.poll() is None:
                child.kill()
            wait(timeout=5)


class HTTPRunnerServerLogTest(unittest.TestCase):
    HEADING = "Owned server log, last 65536 bytes:\n"

    def run_with_status(self, status, content='{"event": "call_completed"}\n', *, error=None):
        class Server:
            port = 8000

            def __init__(self, _environment, log, **_options):
                self.log = log

            def __enter__(self):
                self.log.write_text(content)
                return self

            def __exit__(self, *_exception):
                return False

        diagnostics = io.StringIO()
        with (
            patch.object(runner, "prepare"),
            patch.object(runner, "OwnedServer", Server),
            patch.object(runner, "run_suite", return_value=status, side_effect=error),
            redirect_stderr(diagnostics),
        ):
            if error is None:
                self.assertEqual(runner.run(), status)
            else:
                with self.assertRaises(type(error)):
                    runner.run()
        return diagnostics.getvalue()

    def test_a_failed_run_ends_with_the_tail_of_the_owned_server_log(self):
        self.assertEqual(self.run_with_status(1), self.HEADING + '{"event": "call_completed"}\n')
        long = "a" * 10 + "b" * 65536
        self.assertEqual(self.run_with_status(1, long), self.HEADING + "b" * 65536)

    def test_an_interrupted_run_still_ends_with_the_server_log_tail(self):
        diagnostics = self.run_with_status(None, error=KeyboardInterrupt())
        self.assertEqual(diagnostics, self.HEADING + '{"event": "call_completed"}\n')

    def test_a_successful_run_prints_no_server_log(self):
        self.assertEqual(self.run_with_status(0), "")
