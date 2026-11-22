"""The core identity command prints literal labels and fixed errors, in process."""

import contextlib
import io
import json
import os
import runpy
import sys
import tempfile
import unittest
import warnings
from pathlib import Path
from unittest.mock import patch
from uuid import UUID

from nci_sip_mcp import traffic_identity
from nci_sip_mcp.traffic_identity import INSTALLATION_ID_ENV, main

TOKEN = "a" * 32
SUFFIX = f" (installation={TOKEN}; +https://github.com/CBIIT/nci-sip-mcp)"
VERSION_REASON = (
    "must be a safe printable ASCII version and keep the complete label within 256 bytes"
)
VERSION_MESSAGE = (
    "The installed nci-sip-mcp version cannot form a safe label. "
    "Reinstall the package with valid metadata and retry."
)
ID_REASON = "must be exactly 32 lowercase hexadecimal characters"


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


def _installed(version):
    def installed(name):
        if name != "nci-sip-mcp":
            raise AssertionError(name)
        if isinstance(version, Exception):
            raise version
        return version

    return installed


class CoreIdentityMainTest(unittest.TestCase):
    def assert_error(self, result, parameter, reason, message):
        code, out, err = result
        self.assertEqual((code, err), (1, ""))
        record = json.loads(out)["error"]
        correlation = record.pop("correlationId")
        self.assertRegex(correlation, r"^[0-9a-f]{32}$")
        self.assertEqual(
            record,
            {
                "code": "invalid_request",
                "message": message,
                "details": {"parameter": parameter, "reason": reason},
            },
        )
        self.assertEqual(
            out,
            json.dumps(
                {"error": {**record, "correlationId": correlation}}, indent=2, sort_keys=True
            )
            + "\n",
        )

    def assert_version_refused(self, version, canary=None):
        result = run("show", configured=TOKEN, version=version)
        self.assert_error(result, "nci-sip-mcp installed version", VERSION_REASON, VERSION_MESSAGE)
        if canary:
            self.assertNotIn(canary, result[1] + result[2])

    def test_generate_prints_a_uuid4_and_ignores_configuration_and_metadata(self):
        for configured in (None, "PRIVATE-ID-CANARY"):
            for version in ("1.2.3", UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad")):
                with self.subTest(configured=configured, version=version):
                    code, out, err = run("generate", configured=configured, version=version)

                    self.assertRegex(out, r"^[0-9a-f]{32}\n$")
                    self.assertEqual((code, err), (0, ""))
                    self.assertEqual(UUID(out.strip()).version, 4)

    def test_show_prints_the_exact_label(self):
        self.assertEqual(run("show", configured=TOKEN), (0, f"nci-sip-mcp/1.2.3{SUFFIX}\n", ""))

    def test_show_without_a_token_names_the_missing_variable(self):
        self.assert_error(
            run("show"),
            INSTALLATION_ID_ENV,
            "is missing",
            "Set NCI_SIP_INSTALLATION_ID to exactly 32 lowercase hexadecimal characters and retry.",
        )

    def test_invalid_tokens_are_refused_without_echoing_them(self):
        for configured in ("PRIVATE-ID-CANARY", "A" * 32, "a" * 31, "a" * 33, "g" * 32, "é" * 32):
            with self.subTest(configured=configured):
                result = run("show", configured=configured)

                self.assert_error(
                    result,
                    INSTALLATION_ID_ENV,
                    ID_REASON,
                    "Set NCI_SIP_INSTALLATION_ID to exactly 32 lowercase hexadecimal "
                    "characters and retry.",
                )
                self.assertNotIn(configured, result[1])

    def test_unsafe_installed_versions_are_refused_without_echoing_them(self):
        for version in (
            "bad version",
            "bad); installation=other",
            "bad\tversion",
            "bad-é",
            "bad\x01version",
            "",
            "-1",
            None,
        ):
            with self.subTest(version=version):
                self.assert_version_refused(version, version or None)

    def test_undecodable_installed_metadata_gets_the_fixed_error(self):
        undecodable = UnicodeDecodeError("utf-8", b"private-\xff", 8, 9, "invalid start byte")

        self.assert_version_refused(undecodable, "private-")

    def test_the_complete_label_is_capped_at_256_bytes(self):
        fixed = len("nci-sip-mcp/") + len(SUFFIX)
        accepted = "v" * (256 - fixed)

        code, out, err = run("show", configured=TOKEN, version=accepted)

        self.assertEqual((code, out, err), (0, f"nci-sip-mcp/{accepted}{SUFFIX}\n", ""))
        self.assertEqual(len(out.rstrip("\n").encode("ascii")), 256)
        self.assert_version_refused("v" * (257 - fixed), "v" * 200)

    def test_safe_version_characters_are_kept_verbatim(self):
        version = "1.0.0+local.build_2!x-y"

        self.assertEqual(
            run("show", configured=TOKEN, version=version),
            (0, f"nci-sip-mcp/{version}{SUFFIX}\n", ""),
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
            runpy.run_module("nci_sip_mcp.traffic_identity", run_name="__main__")
        return exited.exception.code, out.getvalue()

    def test_running_the_module_exits_with_the_command_status(self):
        generated = self.run_as_script("generate", {})
        refused = self.run_as_script("show", {})
        shown = self.run_as_script("show", {INSTALLATION_ID_ENV: TOKEN})

        self.assertEqual(generated[0], 0)
        self.assertRegex(generated[1], r"^[0-9a-f]{32}\n$")
        self.assertEqual(refused[0], 1)
        self.assertEqual(shown[0], 0)
