"""The acceptance identity command prints literal labels and fixed errors, in process."""

import contextlib
import io
import os
import runpy
import sys
import tempfile
import unittest
import warnings
from pathlib import Path
from unittest.mock import patch
from uuid import UUID

from nci_sip_acceptance import traffic_identity
from nci_sip_acceptance.traffic_identity import INSTALLATION_ID_ENV, main

TOKEN = "b" * 32
SUFFIX = f" (installation={TOKEN}; +https://github.com/CBIIT/nci-sip-mcp)"
ID_ERROR = (
    "Set NCI_SIP_ACCEPTANCE_INSTALLATION_ID to exactly 32 lowercase hexadecimal characters "
    "and retry."
)
VERSION_ERROR = (
    "The installed nci-sip-acceptance version cannot form a safe label. "
    "Reinstall the acceptance package with valid metadata and retry."
)


def _installed(version):
    def installed(name):
        if name != "nci-sip-acceptance":
            raise AssertionError(name)
        if isinstance(version, Exception):
            raise version
        return version

    return installed


def run(*arguments, configured=None, version="1.2.3"):
    environment = {} if configured is None else {INSTALLATION_ID_ENV: configured}
    out = io.StringIO()
    err = io.StringIO()
    with (
        patch.dict(os.environ, environment, clear=True),
        patch.object(traffic_identity, "version", side_effect=_installed(version)),
        contextlib.redirect_stdout(out),
        contextlib.redirect_stderr(err),
    ):
        code = main(list(arguments))
    return code, out.getvalue(), err.getvalue()


class AcceptanceIdentityMainTest(unittest.TestCase):
    def test_generate_prints_a_uuid4_and_ignores_configuration_and_metadata(self):
        for configured in (None, "PRIVATE-ID-CANARY"):
            for version in ("1.2.3", UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad")):
                with self.subTest(configured=configured, version=version):
                    code, out, err = run("generate", configured=configured, version=version)

                    self.assertRegex(out, r"^[0-9a-f]{32}\n$")
                    self.assertEqual((code, err), (0, ""))
                    self.assertEqual(UUID(out.strip()).version, 4)

    def test_show_prints_the_exact_label(self):
        self.assertEqual(
            run("show", configured=TOKEN), (0, f"nci-sip-acceptance/1.2.3{SUFFIX}\n", "")
        )

    def test_missing_and_invalid_tokens_get_the_fixed_error_without_echo(self):
        for configured in (
            None,
            "PRIVATE-ID-CANARY",
            "A" * 32,
            "a" * 31,
            "a" * 33,
            "g" * 32,
            "é" * 32,
        ):
            with self.subTest(configured=configured):
                self.assertEqual(run("show", configured=configured), (1, "", ID_ERROR + "\n"))

    def test_unsafe_installed_versions_get_the_fixed_error_without_echo(self):
        undecodable = UnicodeDecodeError("utf-8", b"private-\xff", 8, 9, "invalid start byte")
        for version in (
            "bad version",
            "bad); installation=other",
            "bad\tversion",
            "bad-é",
            "bad\x01version",
            "",
            "-1",
            None,
            undecodable,
        ):
            with self.subTest(version=version):
                self.assertEqual(
                    run("show", configured=TOKEN, version=version), (1, "", VERSION_ERROR + "\n")
                )

    def test_the_complete_label_is_capped_at_256_bytes(self):
        fixed = len("nci-sip-acceptance/") + len(SUFFIX)
        accepted = "v" * (256 - fixed)

        code, out, err = run("show", configured=TOKEN, version=accepted)

        self.assertEqual((code, out, err), (0, f"nci-sip-acceptance/{accepted}{SUFFIX}\n", ""))
        self.assertEqual(len(out.rstrip("\n").encode("ascii")), 256)
        self.assertEqual(
            run("show", configured=TOKEN, version="v" * (257 - fixed)),
            (1, "", VERSION_ERROR + "\n"),
        )

    def test_safe_version_characters_are_kept_verbatim(self):
        version = "1.0.0+local.build_2!x-y"

        self.assertEqual(
            run("show", configured=TOKEN, version=version),
            (0, f"nci-sip-acceptance/{version}{SUFFIX}\n", ""),
        )

    def test_help_and_usage_failures_do_not_evaluate_configuration(self):
        for configured in (None, "PRIVATE-ID-CANARY"):
            with self.subTest(configured=configured):
                environment = {} if configured is None else {INSTALLATION_ID_ENV: configured}
                out = io.StringIO()
                err = io.StringIO()
                with (
                    patch.dict(os.environ, environment, clear=True),
                    patch.object(traffic_identity, "version", side_effect=AssertionError),
                    contextlib.redirect_stdout(out),
                    contextlib.redirect_stderr(err),
                ):
                    with self.assertRaises(SystemExit) as helped:
                        main(["--help"])
                    with self.assertRaises(SystemExit) as refused:
                        main(["not-a-command"])

                self.assertEqual((helped.exception.code, refused.exception.code), (0, 2))
                self.assertIn("generate", out.getvalue())
                self.assertIn("show", out.getvalue())
                self.assertIn("usage:", err.getvalue())
                self.assertNotIn("PRIVATE-ID-CANARY", out.getvalue() + err.getvalue())

    def test_commands_write_no_files(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            with (
                patch.dict(os.environ, {"HOME": directory, "XDG_CONFIG_HOME": directory}),
                contextlib.chdir(home),
            ):
                run("generate")
                run("show", configured=TOKEN)
                run("show", configured="bad")

            self.assertEqual(list(home.iterdir()), [])

    def test_a_missing_command_is_a_usage_error(self):
        err = io.StringIO()
        with (
            patch.dict(os.environ, {}, clear=True),
            contextlib.redirect_stderr(err),
            self.assertRaises(SystemExit) as refused,
        ):
            main([])

        self.assertEqual(refused.exception.code, 2)
        self.assertIn("usage:", err.getvalue())

    def run_as_script(self, command, environment):
        with (
            patch.dict(os.environ, environment, clear=True),
            patch.object(sys, "argv", ["identity", command]),
            contextlib.redirect_stdout(io.StringIO()) as out,
            contextlib.redirect_stderr(io.StringIO()),
            warnings.catch_warnings(),
            self.assertRaises(SystemExit) as exited,
        ):
            warnings.simplefilter("ignore", RuntimeWarning)
            runpy.run_module("nci_sip_acceptance.traffic_identity", run_name="__main__")
        return exited.exception.code, out.getvalue()

    def test_running_the_module_exits_with_the_command_status(self):
        generated = self.run_as_script("generate", {})
        refused = self.run_as_script("show", {})
        shown = self.run_as_script("show", {INSTALLATION_ID_ENV: TOKEN})

        self.assertEqual(generated[0], 0)
        self.assertRegex(generated[1], r"^[0-9a-f]{32}\n$")
        self.assertEqual(refused[0], 1)
        self.assertEqual(shown[0], 0)
