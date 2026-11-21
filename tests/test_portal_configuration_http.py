"""Real loopback forms expose only safe recorded settings and advisory diffs."""

import json
import threading
import unittest
from http.client import HTTPConnection
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlencode

from scripts.portal_configuration import LocalConfiguration
from scripts.portal_http import create_server
from scripts.portal_store import EvidenceStore

from nci_sip_mcp.config import Settings
from nci_sip_mcp.config_snapshot import capture


class ConfigurationHTTPTest(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.path = root / "PRIVATE-target.json"
        self.snapshot = capture(
            Settings(timeout_seconds=17, cadsr_credential="PRIVATE:SECRET-VALUE"), set()
        )
        self.path.write_text(json.dumps(self.snapshot))
        self.server = create_server(
            EvidenceStore(root / "store.sqlite"),
            port=0,
            configuration=LocalConfiguration(self.path),
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)
        self.origin = f"http://127.0.0.1:{self.server.server_port}"
        self.form = {
            "instance": self.snapshot["instance"],
            "revision": self.snapshot["revision"],
            "setting": "timeout_seconds",
            "value": "23",
        }

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(self, path="/configuration", *, fields=None, origin=None):
        connection = HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        self.addCleanup(connection.close)
        headers = {
            "Origin": self.origin if origin is None else origin,
            "Content-Type": "application/x-www-form-urlencoded",
        }
        connection.request(
            "GET" if fields is None else "POST",
            path,
            body=None if fields is None else urlencode(fields),
            headers=headers,
        )
        response = connection.getresponse()
        return response.status, response.read().decode()

    def test_local_user_can_read_and_prepare_a_proposal_without_changing_the_target(self):
        before = self.path.read_bytes()
        status, page = self.request()
        self.assertEqual(status, 200)
        self.assertIn("Recorded startup configuration", page)
        self.assertIn('href="/help#configuration"', page)
        self.assertNotIn("PRIVATE", page)
        status, page = self.request("/configuration/proposals", fields=self.form)
        self.assertEqual(status, 200)
        self.assertIn("Nothing has been applied", page)
        self.assertIn("23.0", page)
        self.assertEqual(self.path.read_bytes(), before)

    def test_stale_foreign_unknown_and_duplicate_intents_are_distinct_failures(self):
        for fields, origin, expected in (
            (self.form | {"revision": "0" * 64}, None, 409),
            (self.form, "https://foreign.invalid", 403),
            (self.form | {"setting": "http_auth_mode"}, None, 400),
            ({key: value for key, value in self.form.items() if key != "value"}, None, 400),
            ([*self.form.items(), ("value", "24")], None, 400),
        ):
            with self.subTest(expected=expected):
                status, _ = self.request("/configuration/proposals", fields=fields, origin=origin)
                self.assertEqual(status, expected)

    def test_unavailable_snapshot_is_unknown_not_the_companion_defaults(self):
        self.path.unlink()
        status, page = self.request()
        self.assertEqual(status, 503)
        self.assertIn("Configuration unavailable", page)
        self.assertNotIn("PRIVATE", page)
        self.assertNotIn('name="value"', page)
        status, _ = self.request("/configuration/proposals", fields=self.form)
        self.assertEqual(status, 503)

    def test_a_missing_and_a_malformed_snapshot_are_told_apart_without_their_path_or_body(self):
        for write, message in (
            (self.path.unlink, "No target startup snapshot is readable."),
            (
                lambda: self.path.write_text("<html>PRIVATE-BODY</html>"),
                "The selected target startup snapshot is malformed.",
            ),
        ):
            with self.subTest(message=message):
                write()
                status, page = self.request()
                self.assertEqual(status, 503)
                self.assertIn(f"<p>{message} Remote configuration is unknown.</p>", page)
                self.assertNotIn("PRIVATE", page)

    def test_configuration_help_explains_snapshot_age_and_no_apply_authority(self):
        status, page = self.request("/help")
        self.assertEqual(status, 200)
        self.assertIn('id="configuration"', page)
        self.assertIn("startup", page)
        self.assertIn("restart", page)
