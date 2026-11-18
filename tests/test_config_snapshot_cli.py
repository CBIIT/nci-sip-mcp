"""The optional snapshot reflects the settings used by the selected serving process."""

import io
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from nci_sip_mcp.cli import main
from nci_sip_mcp.config_snapshot import read_snapshot


class ConfigurationSnapshotCLITest(unittest.TestCase):
    def test_snapshot_captures_cli_override_and_is_removed_after_serving(self):
        observed = []
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "target.json"

            def serve(settings, _context):
                snapshot = read_snapshot(path)
                observed.append(snapshot)
                self.assertEqual(snapshot["settings"]["transport"]["value"], settings.transport)
                self.assertEqual(
                    snapshot["settings"]["timeout_seconds"]["value"], settings.timeout_seconds
                )
                return 0

            with (
                patch.dict(os.environ, {"NCI_SIP_TIMEOUT_SECONDS": "17"}, clear=True),
                patch(
                    "sys.argv",
                    [
                        "nci-sip-mcp",
                        "serve",
                        "--transport",
                        "streamable-http",
                        "--configuration-snapshot",
                        str(path),
                    ],
                ),
                patch("nci_sip_mcp.cli.Context"),
                patch("nci_sip_mcp.cli._serve", side_effect=serve),
                patch("nci_sip_mcp.cli.configure_logging"),
            ):
                self.assertEqual(main(), 0)
            self.assertFalse(path.exists())
        self.assertEqual(observed[0]["settings"]["transport"]["origin"], "cli")
        self.assertEqual(observed[0]["settings"]["timeout_seconds"]["origin"], "environment")

    def test_existing_snapshot_is_preserved_and_server_does_not_start(self):
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "target.json"
            path.write_text("existing target")
            output = io.StringIO()
            with (
                patch.dict(os.environ, {}, clear=True),
                patch("sys.argv", ["nci-sip-mcp", "serve", "--configuration-snapshot", str(path)]),
                patch("nci_sip_mcp.cli.Context"),
                patch("nci_sip_mcp.cli._serve", side_effect=AssertionError("must not start")),
                patch("nci_sip_mcp.cli.configure_logging"),
                redirect_stderr(output),
                redirect_stdout(output),
            ):
                self.assertEqual(main(), 1)
            self.assertEqual(path.read_text(), "existing target")
            self.assertIn("internal_error", output.getvalue())
