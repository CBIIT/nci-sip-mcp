import io
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from nci_sip_mcp import installer, installer_state


class Input(io.StringIO):
    def __init__(self, content, *, tty):
        super().__init__(content)
        self.tty = tty
        self.reads = 0

    def isatty(self):
        return self.tty

    def readline(self, *args, **kwargs):
        self.reads += 1
        return super().readline(*args, **kwargs)


class InstallerGuidanceTest(unittest.TestCase):
    def run_main(self, stream, observations, *extra):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        values = iter(observations)
        check = installer.Prerequisite(
            "required_input", "Repair the required input.", lambda: next(values)
        )
        stdout, stderr = io.StringIO(), io.StringIO()
        release = installer.ReleaseIdentity("0.21.0", "a" * 40, Path(directory.name) / "lock")
        with (
            patch.object(installer.sys, "stdin", stream),
            patch.object(installer, "local_prerequisites", return_value=(check,)),
            patch.object(installer, "__version__", "0.21.0"),
            patch.object(installer, "_release_identity", return_value=release),
            patch.object(installer, "_installed_release", return_value=("0.21.0", "a" * 40)),
            patch.object(installer, "_verify_stdio", return_value=None),
            patch.object(
                installer.subprocess,
                "run",
                return_value=subprocess.CompletedProcess([], 0),
            ),
            patch.dict(os.environ, {"PRIVATE_VALUE": "PRIVATE-CANARY"}),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            code = installer.main(
                ["run", "--target", "local", "--data-dir", directory.name, *extra]
            )
        state = installer_state.read_state(Path(directory.name), "local", ("server",))
        return code, json.loads(stdout.getvalue()), stderr.getvalue(), state

    def test_tty_confirmation_only_rechecks_and_advances_after_verification(self):
        stream = Input("\n\n", tty=True)
        checks = 0
        passes_on_check = 3

        def observe():
            nonlocal checks
            checks += 1
            return checks >= passes_on_check

        code, result, prompt, state = self.run_main(stream, iter(observe, None))
        self.assertEqual((code, stream.reads), (0, 2))
        self.assertEqual(prompt.count("Repair the required input."), 2)
        self.assertNotIn("PRIVATE-CANARY", prompt + json.dumps(result))
        self.assertEqual(state.outcomes[0].status, "completed")

    def test_tty_eof_keeps_the_step_blocked(self):
        code, result, _, state = self.run_main(Input("", tty=True), (False,))
        self.assertEqual(code, 1)
        self.assertEqual(result["blocked"][0]["missing"], ["Repair the required input."])
        self.assertEqual(state.outcomes[0].status, "blocked")

    def test_non_tty_and_explicit_non_interactive_never_read_stdin(self):
        for tty, extra in ((False, ()), (True, ("--non-interactive",))):
            with self.subTest(tty=tty, extra=extra):
                stream = Input("\n", tty=tty)
                code, result, prompt, _ = self.run_main(stream, (False,), *extra)
                self.assertEqual((code, stream.reads, prompt), (1, 0, ""))
                self.assertEqual(len(result["blocked"]), 1)

    def test_missing_input_after_apply_is_blocked_with_its_fix(self):
        code, result, _, state = self.run_main(Input("", tty=False), (True, True, False, False))
        self.assertEqual(code, 1)
        self.assertEqual(result["blocked"][0]["missing"], ["Repair the required input."])
        self.assertEqual(state.outcomes[0].status, "blocked")

    def test_tty_failed_or_pending_status_does_not_enter_blocked_prompt(self):
        records = (
            {"blocked": [], "failed": ["step"], "pending": [], "nextAction": "Retry."},
            {"blocked": [], "failed": [], "pending": ["step"], "nextAction": "Run."},
        )
        for record in records:
            stream = Input("\n", tty=True)
            stdout, stderr = io.StringIO(), io.StringIO()
            with (
                tempfile.TemporaryDirectory() as directory,
                patch.object(installer.sys, "stdin", stream),
                patch.object(installer, "_state_command", return_value=(1, record)),
                redirect_stdout(stdout),
                redirect_stderr(stderr),
            ):
                code = installer.main(["run", "--target", "local", "--data-dir", directory])
            self.assertEqual((code, json.loads(stdout.getvalue()), stream.reads), (1, record, 0))
            self.assertEqual(stderr.getvalue(), "")
