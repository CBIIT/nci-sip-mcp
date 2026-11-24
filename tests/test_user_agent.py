"""The server's platform requests carry the product User-Agent (A6.2b).

Every test points all five upstream settings at a local receiver, so no request leaves the
machine, and reads the exact header the receiver was sent.
"""

import io
import json
import logging
import os
import sys
import tempfile
import threading
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, HTTPServer
from importlib.metadata import version
from pathlib import Path
from unittest.mock import patch

from nci_sip_mcp import cli, container_entry, installer, installer_state, traffic_identity
from nci_sip_mcp.audit import JsonFormatter
from nci_sip_mcp.cadsr import CaDSRClient
from nci_sip_mcp.config import Settings
from nci_sip_mcp.context import Context
from nci_sip_mcp.errors import correlated
from nci_sip_mcp.evs import EVSClient
from nci_sip_mcp.http_client import HttpClient
from nci_sip_mcp.index_adapter import LocalIndexAdapter
from nci_sip_mcp.ssis import SSISClient
from ncit_index.index import LocalIndex

INSTALLATION = "0123456789abcdef0123456789abcdef"
OTHER_INSTALLATION = "fedcba9876543210fedcba9876543210"
LICENCE_KEY = "licence-key-4711"
UPSTREAM_VARIABLES = (
    "NCI_SIP_EVS_BASE_URL",
    "NCI_SIP_EVS_FHIR_BASE_URL",
    "NCI_SIP_CADSR_BASE_URL",
    "NCI_SIP_CADSR_FTP_URL",
    "NCI_SIP_SSIS_SPARQL_URL",
)
# The reasons a configured value is refused: wrong case, length, characters, or empty.
MALFORMED_IDS = (
    INSTALLATION.upper(),
    INSTALLATION[:-1],
    INSTALLATION + "0",
    "g" * 32,
    "",
)


def user_agent(installation=None):
    """The label as the specification spells it, from the installed package's version."""

    detail = f"installation={installation}; +" if installation else "+"
    return f"nci-sip-mcp/{version('nci-sip-mcp')} ({detail}https://github.com/CBIIT/nci-sip-mcp)"


class Handler(BaseHTTPRequestHandler):
    """Record the path and headers of each request; answer with the next scripted status."""

    def do_GET(self):
        self.server.seen.append((self.path, {k.lower(): v for k, v in self.headers.items()}))
        status = self.server.statuses.pop(0) if self.server.statuses else 200
        body = b'{"ok": true}'
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        self.do_GET()

    def log_message(self, *_):
        pass


class ReceiverCase(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name)

    def serve(self, *statuses):
        """A local receiver that answers `statuses` in turn, then 200; returns it."""

        server = HTTPServer(("127.0.0.1", 0), Handler)
        server.seen, server.statuses = [], list(statuses)
        thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        server.url = f"http://127.0.0.1:{server.server_port}"
        return server

    def environment(self, server, **settings):
        return {
            "NCI_SIP_DATA_DIR": str(self.path),
            "NCI_SIP_EVS_RETRY_BACKOFF_SECONDS": "0",
            **dict.fromkeys(UPSTREAM_VARIABLES, server.url),
            **settings,
        }

    def user_agents(self, server):
        return [headers["user-agent"] for _, headers in server.seen]


class ContextTest(ReceiverCase):
    def settings(self, server, **fields):
        return Settings(
            data_dir=self.path,
            evs_base_url=server.url,
            evs_fhir_base_url=server.url,
            cadsr_base_url=server.url,
            cadsr_ftp_url=server.url,
            ssis_sparql_url=server.url,
            evs_retry_backoff_seconds=0,
            **fields,
        )

    def call_every_client(self, context):
        context.evs.get_api_version()
        context.fhir.get_json("/fhir")
        context.cadsr.http.get_json("/cadsr")
        context.cadsr.match_http.get_json("/match")
        context.cadsr.export_http.get_json("/export")
        context.ssis.sparql_http.post_form(
            "/sparql", {"query": "ASK {}"}, accept="application/sparql-results+json"
        )

    def test_every_client_sends_the_installation_label_when_one_is_configured(self):
        server = self.serve()
        with patch.dict(os.environ, {"NCI_SIP_INSTALLATION_ID": INSTALLATION}):
            self.call_every_client(Context(self.settings(server)))

        self.assertEqual(self.user_agents(server), [user_agent(INSTALLATION)] * 6)

    def test_every_client_sends_the_product_label_alone_when_no_id_is_configured(self):
        server = self.serve()

        self.call_every_client(Context(self.settings(server)))

        self.assertEqual(self.user_agents(server), [user_agent()] * 6)

    def test_the_label_is_the_one_captured_at_start_up_for_every_attempt_and_call(self):
        server = self.serve(503)
        with patch.dict(os.environ, {"NCI_SIP_INSTALLATION_ID": INSTALLATION}):
            context = Context(self.settings(server))
        with patch.dict(os.environ, {"NCI_SIP_INSTALLATION_ID": OTHER_INSTALLATION}):
            context.evs.get_api_version()
        context.evs.get_api_version()

        self.assertEqual(self.user_agents(server), [user_agent(INSTALLATION)] * 3)

    def test_the_label_sits_beside_the_credential_and_the_correlation_identifier(self):
        server = self.serve()
        context = Context(self.settings(server, evs_license_key=LICENCE_KEY))

        with correlated("call-17"):
            context.evs.get_api_version()

        (_, headers) = server.seen[0]
        self.assertEqual(
            (
                headers["user-agent"],
                headers["x-evsrestapi-license-key"],
                headers["x-correlation-id"],
            ),
            (user_agent(), LICENCE_KEY, "call-17"),
        )


class HttpClientTest(ReceiverCase):
    def test_a_given_user_agent_is_sent_as_given_even_when_empty(self):
        server = self.serve()
        client = HttpClient(
            server.url,
            label="EVS",
            timeout_seconds=5,
            max_attempts=1,
            retry_backoff_seconds=0,
            max_response_bytes=1000,
            size_bound="BOUND",
            user_agent="",
        )

        client.get_json("/x")

        self.assertEqual(self.user_agents(server), [""])


class InstallerProbeTest(ReceiverCase):
    def test_the_reachability_probe_sends_the_product_label_and_never_an_installation(self):
        server = self.serve()
        ids = {
            "NCI_SIP_INSTALLATION_ID": INSTALLATION,
            "NCI_SIP_ACCEPTANCE_INSTALLATION_ID": INSTALLATION,
        }

        with patch.dict(os.environ, ids):
            reachable = installer.reachability_prerequisite("x", server.url, "fix").observe()

        self.assertTrue(reachable)
        self.assertEqual(self.user_agents(server), [user_agent()])

    def test_unsafe_installed_metadata_stops_every_prerequisite_command_without_contact(self):
        message = (
            "The installed nci-sip-mcp version cannot form a safe label. "
            "Reinstall the package with valid metadata and retry.\n"
        )
        for command in ("check", "run", "status"):
            with self.subTest(command=command):
                server = self.serve()
                data_dir = self.path / command
                data_dir.mkdir()
                arguments = [command, "--target", "local"]
                if command != "check":
                    arguments += ["--data-dir", str(data_dir)]
                if command == "run":
                    arguments.append("--non-interactive")
                if command == "status":
                    installer_state.write_state(
                        data_dir,
                        installer_state.InstallationState(
                            2,
                            "local",
                            ("server",),
                            (installer_state.StepOutcome("check_prerequisites", "completed"),),
                        ),
                    )
                environment = {
                    "NCI_SIP_EVS_BASE_URL": server.url,
                    "NCI_SIP_CADSR_BASE_URL": server.url,
                }
                stdout, stderr = io.StringIO(), io.StringIO()
                with (
                    patch.dict(os.environ, environment),
                    patch.object(traffic_identity, "version", return_value="not a version"),
                    redirect_stdout(stdout),
                    redirect_stderr(stderr),
                ):
                    try:
                        outcome = installer.main(arguments)
                    except Exception as error:  # noqa: BLE001 - the observed outcome is the claim
                        outcome = type(error).__name__

                self.assertEqual((outcome, stdout.getvalue(), stderr.getvalue()), (2, "", message))
                self.assertEqual(server.seen, [])


def run_main(*argv, **environment):
    """Run the CLI with exactly `environment`; return (exit code, parsed stdout, stderr text)."""

    stdout, stderr = io.StringIO(), io.StringIO()
    with ExitStack() as stack:
        for item in (
            patch.object(sys, "argv", ["nci-sip-mcp", *argv]),
            patch.dict(os.environ, environment, clear=True),
            patch("nci_sip_mcp.cli.configure_logging"),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            stack.enter_context(item)
        code = cli.main()
    printed = stdout.getvalue() or stderr.getvalue()
    return code, json.loads(printed) if printed else {}, stderr.getvalue()


class CliTest(ReceiverCase):
    def test_a_command_sends_the_label_with_the_configured_id(self):
        server = self.serve()

        code, _, _ = run_main(
            "release-info", **self.environment(server, NCI_SIP_INSTALLATION_ID=INSTALLATION)
        )

        self.assertEqual(code, 0)
        self.assertTrue(server.seen)
        self.assertEqual(set(self.user_agents(server)), {user_agent(INSTALLATION)})

    def test_a_command_sends_the_product_label_alone_without_an_id(self):
        server = self.serve()

        code, _, _ = run_main("release-info", **self.environment(server))

        self.assertEqual(code, 0)
        self.assertTrue(server.seen)
        self.assertEqual(set(self.user_agents(server)), {user_agent()})

    def test_a_malformed_id_is_refused_before_any_contact_without_echoing_it(self):
        for value in MALFORMED_IDS:
            with self.subTest(value=value):
                server = self.serve()

                code, result, stderr = run_main(
                    "release-info", **self.environment(server, NCI_SIP_INSTALLATION_ID=value)
                )

                error = result.get("error", {})
                self.assertEqual((code, error.get("code")), (1, "invalid_request"))
                self.assertEqual(
                    error.get("details", {}).get("parameter"), "NCI_SIP_INSTALLATION_ID"
                )
                self.assertEqual(server.seen, [])
                if value:
                    self.assertNotIn(value, json.dumps(result) + stderr)

    def test_an_unsafe_installed_version_is_refused_before_any_contact(self):
        server = self.serve()

        with patch.object(traffic_identity, "version", return_value="not a version"):
            code, result, _ = run_main("release-info", **self.environment(server))

        error = result.get("error", {})
        self.assertEqual((code, error.get("code")), (1, "invalid_request"))
        self.assertEqual(error.get("details", {}).get("parameter"), "nci-sip-mcp installed version")
        self.assertEqual(server.seen, [])

    def test_serve_reports_a_malformed_id_on_stderr_and_does_not_start(self):
        server = self.serve()

        with patch.object(cli, "create_mcp") as create:
            code, _, stderr = run_main(
                "serve", **self.environment(server, NCI_SIP_INSTALLATION_ID="not-an-id")
            )

        error = json.loads(stderr)["error"]
        self.assertEqual(
            (code, error["code"], error["details"]["parameter"]),
            (1, "invalid_request", "NCI_SIP_INSTALLATION_ID"),
        )
        create.assert_not_called()
        self.assertEqual(server.seen, [])


class ContainerEntryTest(ReceiverCase):
    def test_a_malformed_id_stops_startup_naming_only_the_setting(self):
        logging.disable(logging.NOTSET)
        settings = Settings(data_dir=self.path, transport="streamable-http")
        index = LocalIndexAdapter(LocalIndex(self.path))
        with (
            patch.dict(os.environ, {"NCI_SIP_INSTALLATION_ID": "not-an-id-secret"}),
            patch.object(container_entry.Settings, "from_env", return_value=settings),
            patch.object(container_entry, "configure_logging"),
            patch.object(container_entry, "prepared_index", return_value=index),
            patch.object(container_entry, "run_http") as serve,
            self.assertLogs("nci_sip_mcp.container_entry", level="ERROR") as logs,
        ):
            code = container_entry.main()

        records = [json.loads(JsonFormatter().format(record)) for record in logs.records]
        self.assertEqual(code, 1)
        serve.assert_not_called()
        self.assertEqual(
            [(r["event"], r["asset"], r["errorType"], r["code"], r["parameter"]) for r in records],
            [
                (
                    "container_startup_failed",
                    "identity",
                    "PlatformError",
                    "invalid_request",
                    "NCI_SIP_INSTALLATION_ID",
                )
            ],
        )
        self.assertNotIn("secret", json.dumps(records))

    def test_an_unsafe_installed_version_stops_startup_naming_the_version(self):
        logging.disable(logging.NOTSET)
        settings = Settings(data_dir=self.path, transport="streamable-http")
        index = LocalIndexAdapter(LocalIndex(self.path))
        with (
            patch.object(container_entry.Settings, "from_env", return_value=settings),
            patch.object(container_entry, "configure_logging"),
            patch.object(container_entry, "prepared_index", return_value=index),
            patch.object(container_entry, "run_http") as serve,
            patch.object(traffic_identity, "version", return_value="not a version"),
            self.assertLogs("nci_sip_mcp.container_entry", level="ERROR") as logs,
        ):
            code = container_entry.main()

        records = [json.loads(JsonFormatter().format(record)) for record in logs.records]
        self.assertEqual(code, 1)
        serve.assert_not_called()
        self.assertEqual(
            [(r["asset"], r["code"], r["parameter"]) for r in records],
            [("identity", "invalid_request", "nci-sip-mcp installed version")],
        )


class EveryClientRequiresALabelTest(unittest.TestCase):
    """A client built without a User-Agent is a caller bug, never urllib's default (A6.2b)."""

    def test_no_platform_client_can_be_constructed_without_one(self):
        constructors = {
            "HttpClient": lambda: HttpClient(
                "https://example.invalid",
                label="EVS",
                timeout_seconds=5,
                max_attempts=1,
                retry_backoff_seconds=0,
                max_response_bytes=1000,
                size_bound="BOUND",
            ),
            "EVSClient": lambda: EVSClient("https://example.invalid"),
            "CaDSRClient": lambda: CaDSRClient(Settings()),
            "SSISClient": lambda: SSISClient(Settings()),
        }
        for name, construct in constructors.items():
            with self.subTest(name), self.assertRaisesRegex(TypeError, "user_agent"):
                construct()
