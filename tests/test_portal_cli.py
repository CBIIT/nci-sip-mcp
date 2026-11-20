"""Repository users can import evidence without an account or a shell command template."""

import io
import json
import subprocess
import sys
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from http.client import HTTPConnection
from http.server import HTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from scripts.portal import _parser, _remote, main
from scripts.portal_http import create_server
from scripts.portal_store import EvidenceStore

from nci_sip_mcp.config import Settings
from nci_sip_mcp.config_snapshot import capture
from test_portal_store import run_bundle


class PortalCLITest(unittest.TestCase):
    def test_remote_profile_requires_both_explicit_opt_in_and_valid_exact_target(self):
        parser = _parser()
        for options in (
            ["--allow-remote"],
            ["--remote-target", "https://approved.example/mcp"],
            ["--allow-remote", "--remote-target", "https://PRIVATE-CANARY@example/mcp"],
        ):
            with self.subTest(options=options), redirect_stderr(io.StringIO()) as errors:
                args = parser.parse_args(["serve", *options])
                with self.assertRaises(SystemExit) as raised:
                    _remote(parser, args)
                self.assertEqual(raised.exception.code, 2)
                self.assertNotIn("PRIVATE-CANARY", errors.getvalue())
        args = parser.parse_args(
            ["serve", "--allow-remote", "--remote-target", "https://approved.example/mcp"]
        )
        with patch.dict("os.environ", {"NCI_SIP_BENCHMARK_AUTHORIZATION": "Bearer PRIVATE-CANARY"}):
            probe = _remote(parser, args)
        self.assertEqual(probe.target, "https://approved.example/mcp")
        self.assertEqual(probe.authorization, "Bearer PRIVATE-CANARY")
        self.assertNotIn("PRIVATE-CANARY", repr(probe))

    def test_serve_enables_fixed_controls_and_releases_workspace_on_interrupt(self):
        with TemporaryDirectory() as temporary:
            database = Path(temporary) / "history.sqlite"
            observed = []

            def inspect(server):
                thread = threading.Thread(target=server.handle_request)
                thread.start()
                connection = HTTPConnection("127.0.0.1", server.server_port, timeout=3)
                try:
                    connection.request("GET", "/jobs")
                    response = connection.getresponse()
                    observed.append((response.status, response.read().decode()))
                finally:
                    connection.close()
                    thread.join(timeout=3)
                raise KeyboardInterrupt

            with (
                patch("sys.argv", ["portal", "--store", str(database), "serve", "--port", "0"]),
                patch.object(HTTPServer, "serve_forever", inspect),
                redirect_stdout(io.StringIO()),
            ):
                main()
                main()
            self.assertEqual([status for status, _ in observed], [200, 200])
            self.assertIn("benchmark-http-fixture", observed[0][1])
            self.assertNotIn("benchmark-http-remote", observed[0][1])

    def test_selected_configuration_snapshot_is_served_without_companion_environment_inference(
        self,
    ):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            snapshot = root / "target.json"
            snapshot.write_text(json.dumps(capture(Settings(timeout_seconds=17), set())))
            observed = []

            def inspect(server):
                thread = threading.Thread(target=server.handle_request)
                thread.start()
                connection = HTTPConnection("127.0.0.1", server.server_port, timeout=3)
                try:
                    connection.request("GET", "/configuration")
                    response = connection.getresponse()
                    observed.append((response.status, response.read().decode()))
                finally:
                    connection.close()
                    thread.join(timeout=3)
                raise KeyboardInterrupt

            with (
                patch(
                    "sys.argv",
                    [
                        "portal",
                        "--store",
                        str(root / "store.sqlite"),
                        "serve",
                        "--port",
                        "0",
                        "--configuration-snapshot",
                        str(snapshot),
                    ],
                ),
                patch.object(HTTPServer, "serve_forever", inspect),
                patch.dict("os.environ", {"NCI_SIP_TIMEOUT_SECONDS": "91"}),
                redirect_stdout(io.StringIO()),
            ):
                main()
            self.assertEqual(observed[0][0], 200)
            self.assertIn("Recorded startup configuration", observed[0][1])
            self.assertIn("<td>17</td>", observed[0][1])
            self.assertNotIn("<td>91</td>", observed[0][1])

    def test_serve_command_closes_its_listener_on_local_interrupt(self):
        with TemporaryDirectory() as temporary:
            database = Path(temporary) / "history.sqlite"
            server = create_server(EvidenceStore(database), port=0)
            output = io.StringIO()
            with (
                patch("sys.argv", ["portal", "--store", str(database), "serve", "--port", "0"]),
                patch("scripts.portal.create_server", return_value=server),
                patch.object(server, "serve_forever", side_effect=KeyboardInterrupt),
                redirect_stdout(output),
            ):
                main()
            self.assertEqual(server.fileno(), -1)
            self.assertIn("http://127.0.0.1:", output.getvalue())

    def test_legacy_symlink_is_rejected_without_reading_target(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "private.json"
            target.write_text("PRIVATE-CANARY")
            alias = root / "alias.json"
            alias.symlink_to(target)
            with (
                patch(
                    "sys.argv",
                    [
                        "portal",
                        "--store",
                        str(root / "history.sqlite"),
                        "legacy",
                        str(alias),
                        "--kind",
                        "acceptance",
                    ],
                ),
                self.assertRaisesRegex(ValueError, "symlink"),
            ):
                main()
            self.assertEqual(EvidenceStore(root / "history.sqlite").history(), [])

    def test_import_command_keeps_original_binding_and_prints_only_run_id(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            bundle = root / "bundle"
            bundle.mkdir()
            for name, raw in run_bundle().items():
                (bundle / (name + ".json")).write_bytes(raw)
            database = root / "history.sqlite"
            result = subprocess.run(  # noqa: S603 - fixed module and owned fixture paths
                [
                    sys.executable,
                    "-m",
                    "scripts.portal",
                    "--store",
                    str(database),
                    "import",
                    str(bundle),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "1" * 32)
            self.assertEqual(
                EvidenceStore(database).get("1" * 32)["evidence"]["kind"], "acceptance"
            )

    def test_legacy_command_does_not_interpret_claimed_success(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            report = root / "old.json"
            report.write_text('{"outcome":"PASS","secret":"PRIVATE-CANARY"}')
            database = root / "history.sqlite"
            result = subprocess.run(  # noqa: S603 - fixed module and owned fixture paths
                [
                    sys.executable,
                    "-m",
                    "scripts.portal",
                    "--store",
                    str(database),
                    "legacy",
                    str(report),
                    "--kind",
                    "benchmark",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            record = EvidenceStore(database).get(result.stdout.strip())
            self.assertEqual(record["evidence"]["state"], "unverified")
            self.assertNotIn("PRIVATE-CANARY", json.dumps(record) + result.stdout + result.stderr)
