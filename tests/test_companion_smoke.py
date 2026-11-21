"""Container verification waits for actual queue states and requires readable results."""

import html
import json
import re
import subprocess
import threading
import unittest
from http.client import HTTPConnection
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from scripts import companion_smoke, processes
from scripts.portal_http import create_server
from scripts.portal_store import EvidenceStore
from scripts.site_assets import load_footer


class CompanionSmokeTest(unittest.TestCase):
    def inspect(self, **changes):
        record = {
            "HostConfig": {"ReadonlyRootfs": True, "Memory": 1, "PidsLimit": 1},
            "Config": {"User": "65532:65532"},
            "Mounts": [{"Type": "volume", "Destination": "/state"}],
            "NetworkSettings": {"Networks": {"internal": {}}},
        }
        for key, value in changes.items():
            if isinstance(value, dict) and isinstance(record.get(key), dict):
                record[key].update(value)
            else:
                record[key] = value
        replies = ["None", json.dumps([record]), json.dumps([{"Internal": True}])]
        with patch.object(companion_smoke, "docker", side_effect=replies):
            companion_smoke.inspect_boundary("admin")

    def test_companion_boundary_refuses_writable_root_and_extra_network(self):
        cases = (
            ("Companion root is writable", {"HostConfig": {"ReadonlyRootfs": False}}),
            (
                "Companion has an extra network",
                {"NetworkSettings": {"Networks": {"internal": {}, "extra": {}}}},
            ),
        )
        for message, changes in cases:
            with self.subTest(message), self.assertRaisesRegex(RuntimeError, message):
                self.inspect(**changes)

    def test_companion_boundary_refuses_external_connectivity(self):
        with (
            patch.object(companion_smoke, "docker", side_effect=["1", "0"]),
            self.assertRaisesRegex(RuntimeError, "can reach an external network"),
        ):
            companion_smoke.blocked_connectivity("admin", "10.0.0.2")

    def test_every_profile_plan_has_the_exact_services(self):
        replies = [
            "",
            "documentation",
            "administration\nadmin-ingress",
            "documentation\nadministration\nadmin-ingress",
            "mcp",
        ]
        commands = []

        def docker(*args):
            commands.append(args)
            return replies[len(commands) - 1]

        with patch.object(companion_smoke, "docker", side_effect=docker):
            companion_smoke.verify_plans(("compose",))
        self.assertEqual(
            commands,
            [
                ("compose", "config", "--services"),
                ("compose", "--profile", "docs", "config", "--services"),
                ("compose", "--profile", "companion", "config", "--services"),
                (
                    "compose",
                    "--profile",
                    "docs",
                    "--profile",
                    "companion",
                    "config",
                    "--services",
                ),
                ("compose", "--profile", "mcp", "config", "--services"),
            ],
        )

    def test_serving_probe_explicitly_opts_into_local_auth(self):
        record = {
            "NetworkSettings": {
                "Ports": {"8000/tcp": [{"HostPort": "12345"}]},
                "Networks": {"owned-network": {"IPAddress": "10.0.0.2"}},
            }
        }
        commands = []

        def docker(*args):
            commands.append(args)
            return json.dumps([record])

        with patch.object(companion_smoke, "docker", docker):
            self.assertEqual(
                companion_smoke._serving_probe("owned-network", "owned-serving"),
                ("10.0.0.2", 12345),
            )
        command = next(args for args in commands if args[0] == "run")
        environment = dict(
            command[i + 1].split("=", 1) for i, arg in enumerate(command) if arg == "-e"
        )
        self.assertEqual(environment.get("NCI_SIP_HTTP_AUTH_MODE"), "trusted-local")

    def test_cleanup_failure_is_reported_after_other_owned_resources_are_removed(self):
        resources = {"container": {"owned-serving"}, "network": {"owned-network"}}

        def docker(*args):
            if args[0] == "compose":
                raise subprocess.CalledProcessError(1, args)
            kind, action = args[:2]
            if action == "ls":
                return "\n".join(resources[kind])
            resources[kind].remove(args[-1])
            return ""

        with (
            patch.object(companion_smoke, "docker", side_effect=docker),
        ):
            errors = companion_smoke.cleanup(("compose",), "owned-serving", "owned-network")
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], subprocess.CalledProcessError)
        self.assertEqual(resources, {"container": set(), "network": set()})

    def test_primary_failure_survives_diagnostic_and_cleanup_failures(self):
        primary = RuntimeError("primary verification failure")
        secondary = subprocess.TimeoutExpired("docker", 60)
        with (
            patch.object(companion_smoke.sys, "exception", return_value=primary),
            patch.object(companion_smoke, "docker_result", side_effect=secondary),
            patch.object(companion_smoke, "cleanup", return_value=[]),
            self.assertRaisesRegex(RuntimeError, "primary verification failure") as raised,
        ):
            try:
                raise primary
            finally:
                companion_smoke._finish(("compose",), "serving", "network")
        self.assertTrue(any("also failed" in note for note in raised.exception.__notes__))

    def test_cleanup_failure_after_success_is_not_silent(self):
        failure = subprocess.CalledProcessError(1, ["docker", "compose", "down"])
        with (
            patch.object(companion_smoke.sys, "exception", return_value=None),
            patch.object(companion_smoke, "cleanup", return_value=[failure]),
            self.assertRaises(ExceptionGroup) as raised,
        ):
            companion_smoke._finish(("compose",), "serving", "network")
        self.assertEqual(raised.exception.exceptions, (failure,))

    def test_nonzero_diagnostic_status_is_attached_to_the_primary_failure(self):
        primary = RuntimeError("primary verification failure")
        failed = subprocess.CompletedProcess(
            ["docker"], 7, stdout="partial", stderr="daemon unavailable"
        )
        with (
            patch.object(companion_smoke.sys, "exception", return_value=primary),
            patch.object(companion_smoke, "docker_result", return_value=failed),
            patch.object(companion_smoke, "cleanup", return_value=[]),
            self.assertRaisesRegex(RuntimeError, "primary verification failure") as raised,
        ):
            try:
                raise primary
            finally:
                companion_smoke._finish(("compose",), "serving", "network")
        self.assertEqual(len(raised.exception.__notes__), 2)
        self.assertTrue(all("status 7" in note for note in raised.exception.__notes__))

    def test_pending_job_is_polled_until_completed_and_results_are_required(self):
        run_id = "a" * 32
        history = [
            json.dumps({"jobs": [{"run_id": run_id, "state": state}]})
            for state in ("pending", "running", "completed")
        ]
        with (
            patch.object(companion_smoke.uuid, "uuid4", return_value=SimpleNamespace(hex=run_id)),
            patch.object(companion_smoke, "docker", side_effect=history),
            patch.object(companion_smoke, "request", side_effect=[(303, ""), (503, "Unavailable")]),
            patch.object(processes.time, "sleep"),
            self.assertRaisesRegex(RuntimeError, "Benchmark results are unavailable"),
        ):
            companion_smoke.benchmark("owned-admin")

    def test_cancelled_benchmark_is_not_accepted_as_completed(self):
        run_id = "a" * 32
        jobs = json.dumps({"jobs": [{"run_id": run_id, "state": "cancelled"}]})
        with (
            patch.object(companion_smoke.uuid, "uuid4", return_value=SimpleNamespace(hex=run_id)),
            patch.object(companion_smoke, "docker", return_value=jobs),
            patch.object(companion_smoke, "request", return_value=(303, "")),
            self.assertRaisesRegex(RuntimeError, "ended as cancelled"),
        ):
            companion_smoke.benchmark("owned-admin")


DOCS_PORT, PORTAL_PORT = 8080, 8081


class PortalStylingTest(unittest.TestCase):
    """The smoke run against a real portal: styled page and footer link are required of it."""

    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.server = create_server(EvidenceStore(Path(directory.name) / "results.sqlite"), port=0)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def smoke(self, override=None):
        """Run the smoke's `main`, the portal on 8081 being the real server; `override` maps a
        path to the (status, body) the portal is made to answer instead."""
        override = override or {}
        asked = []

        def request(port, path, body=None):
            if port == DOCS_PORT:
                return 200, "National Cancer Institute"
            if port != PORTAL_PORT:
                return 200, ""
            asked.append(path)
            if path in override:
                return override[path]
            connection = HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
            try:
                connection.request("GET", path)
                response = connection.getresponse()
                return response.status, response.read().decode()
            finally:
                connection.close()

        with (
            patch.object(companion_smoke, "docker", return_value=""),
            patch.object(companion_smoke, "request", request),
            patch.object(companion_smoke, "ready"),
            patch.object(companion_smoke, "inspect_boundary"),
            patch.object(companion_smoke, "_serving_probe", return_value=("10.0.0.2", 1)),
            patch.object(companion_smoke, "blocked_connectivity"),
            patch.object(companion_smoke, "benchmark"),
            patch.object(companion_smoke, "verify_plans"),
            patch.object(companion_smoke, "remove_component"),
            patch.object(companion_smoke, "cleanup"),
        ):
            companion_smoke.main()
        return asked

    def test_a_correctly_styled_portal_with_its_footer_passes(self):
        self.assertEqual(self.smoke(), ["/", "/assets/ncids.css", "/assets/portal.css", "/health"])

    def test_a_contact_url_that_needs_escaping_still_passes_on_a_correct_page(self):
        footer = load_footer()._replace(contact_url='https://example.test/contact?a=1&b="2"')
        with (
            patch("scripts.portal_views.FOOTER", footer),
            patch.object(companion_smoke, "load_footer", return_value=footer),
        ):
            self.assertEqual(
                self.smoke(), ["/", "/assets/ncids.css", "/assets/portal.css", "/health"]
            )

    def test_a_contact_url_present_only_as_text_is_not_a_footer_link(self):
        connection = HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        self.addCleanup(connection.close)
        connection.request("GET", "/")
        page = connection.getresponse().read().decode()
        contact = html.escape(load_footer().contact_url, quote=True)
        text_only = page.replace(f'href="{contact}"', f'title="x">{contact}<a')
        self.assertNotEqual(text_only, page)
        with self.assertRaisesRegex(RuntimeError, "Portal footer link missing"):
            self.smoke({"/": (200, text_only)})

    def test_a_missing_stylesheet_or_footer_contact_link_fails_the_smoke(self):
        connection = HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        self.addCleanup(connection.close)
        connection.request("GET", "/")
        page = connection.getresponse().read().decode()
        unstyled = re.sub(r"<link[^>]*stylesheet[^>]*>", "", page)
        self.assertNotEqual(unstyled, page)
        for case, path, replacement in (
            ("ncids.css missing", "/assets/ncids.css", (404, "")),
            ("portal.css missing", "/assets/portal.css", (404, "")),
            ("wrong contact link", "/", (200, page.replace("/issues", "/elsewhere"))),
            ("no stylesheet links", "/", (200, unstyled)),
            ("page unavailable", "/", (503, page)),
        ):
            with self.subTest(case), self.assertRaises(RuntimeError):
                self.smoke({path: replacement})
