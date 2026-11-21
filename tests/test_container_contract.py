"""Image declarations drive the actual entry point and executable HTTP probes."""

import ast
import io
import json
import os
import re
import shlex
import socket
import subprocess
import sys
import time
import unittest
import urllib.request
from contextlib import contextmanager
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from typing import ClassVar
from unittest.mock import call, patch

import yaml
from scripts import container_smoke

from fakes import concept
from ncit_index.embeddings import HashingEmbeddingProvider
from ncit_index.index import LocalIndex
from test_index import normalized


def image_environment():
    environment = {}
    for line in Path("Dockerfile").read_text().replace("\\\n", " ").splitlines():
        if line.startswith("ENV "):
            for field in shlex.split(line.removeprefix("ENV ")):
                if not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*=.*", field):
                    raise ValueError(f"Unsupported Docker ENV declaration: {line}")
                name, value = field.split("=", 1)
                environment[name] = value
    return environment


def free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def listening(port):
    with socket.socket() as connection:
        connection.settimeout(0.1)
        return connection.connect_ex(("127.0.0.1", port)) == 0


def startup(process, port):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if process.poll() is not None or listening(port):
            return
        time.sleep(0.05)
    raise TimeoutError("Entry point neither exited nor opened its listener")


@contextmanager
def entry_point(environment):
    with TemporaryDirectory() as directory:
        index = LocalIndex(Path(directory))
        index.upsert_concepts(normalized([concept("C1")]), None, HashingEmbeddingProvider())
        port = free_port()
        controlled = environment | {
            "PATH": os.environ["PATH"],
            "NCI_SIP_DATA_DIR": directory,
            "NCI_SIP_EMBEDDING_PROVIDER": "hashing",
            "NCI_SIP_EMBEDDING_MODEL": "hashing",
            "NCI_SIP_HTTP_PORT": str(port),
        }
        with Path(directory, "stderr").open("w+") as stderr:
            process = subprocess.Popen(
                [sys.executable, "-m", "nci_sip_mcp.container_entry"],
                env=controlled,
                stdout=subprocess.DEVNULL,
                stderr=stderr,
            )
            try:
                startup(process, port)
                yield process, port, stderr
            finally:
                if process.poll() is None:
                    process.terminate()
                process.wait(timeout=10)


class ImageStartupTest(unittest.TestCase):
    def test_image_without_auth_integration_refuses_before_listening(self):
        with entry_point(image_environment()) as (process, port, stderr):
            self.assertIsNotNone(
                process.poll(), "Image defaults opened an unauthenticated listener"
            )
            self.assertNotEqual(process.returncode, 0)
            self.assertFalse(listening(port))
            stderr.seek(0)
            self.assertIn(
                "Required HTTP authentication needs an installed integration", stderr.read()
            )

    def test_explicit_local_consumers_serve_health_and_readiness(self):
        compose = yaml.safe_load(Path("container/compose.local.yaml").read_text())["services"][
            "mcp"
        ]
        command = container_smoke.mounted("image", Path("assets"))
        smoke = dict(command[i + 1].split("=", 1) for i, arg in enumerate(command) if arg == "-e")
        self.assertTrue(all(port.startswith("127.0.0.1:") for port in compose["ports"]))
        for consumer in (compose["environment"], smoke):
            with self.subTest(consumer=consumer):
                self.assertEqual(consumer.get("NCI_SIP_HTTP_AUTH_MODE"), "trusted-local")
                with entry_point(image_environment() | consumer) as (process, port, stderr):
                    stderr.seek(0)
                    self.assertIsNone(process.poll(), stderr.read())
                    for path, status in (("health", "ok"), ("ready", "ready")):
                        with urllib.request.urlopen(
                            f"http://127.0.0.1:{port}/{path}", timeout=3
                        ) as reply:
                            self.assertEqual(json.load(reply), {"status": status})


class ProbeReply(BaseHTTPRequestHandler):
    status = HTTPStatus.OK
    requested: ClassVar[list[str]] = []

    def do_GET(self):
        self.requested.append(self.path)
        self.send_response(self.status)
        self.end_headers()

    def log_message(self, *_args):
        pass


class ImageProbeTest(unittest.TestCase):
    @staticmethod
    def image_probe_flags():
        declaration = next(
            line
            for line in Path("Dockerfile").read_text().splitlines()
            if line.startswith("HEALTHCHECK ")
        )
        return dict(
            token.removeprefix("--").replace("-", "_").split("=", 1)
            for token in shlex.split(declaration.partition(" CMD ")[0])[1:]
        )

    def test_probe_budgets_cover_cold_start_and_allow_transient_failures(self):
        flags = self.image_probe_flags()
        compose = yaml.safe_load(Path("container/compose.local.yaml").read_text())["services"][
            "mcp"
        ]["healthcheck"]
        for key in ("interval", "timeout", "start_period", "retries"):
            self.assertEqual(str(compose[key]), flags[key], key)
        seconds = {
            key: float(flags[key].removesuffix("s"))
            for key in ("interval", "timeout", "start_period")
        }
        self.assertGreaterEqual(seconds["start_period"], container_smoke.STARTUP_SECONDS)
        self.assertGreaterEqual(int(flags["retries"]), 2)
        self.assertGreater(seconds["interval"], seconds["timeout"])
        self.assertLessEqual(seconds["interval"], container_smoke.STARTUP_SECONDS)
        for _path, command in self.commands():
            request = self.probe_request(command)
            options = {value.arg: ast.literal_eval(value.value) for value in request.keywords}
            self.assertLess(options["timeout"], seconds["timeout"])

    @staticmethod
    def probe_request(command):
        calls = [node for node in ast.walk(ast.parse(command[-1])) if isinstance(node, ast.Call)]
        return next(
            node
            for node in calls
            if isinstance(node.func, ast.Attribute) and node.func.attr == "urlopen"
        )

    def test_probes_without_port_override_use_the_exposed_image_port(self):
        exposed = next(
            line.removeprefix("EXPOSE ")
            for line in Path("Dockerfile").read_text().splitlines()
            if line.startswith("EXPOSE ")
        )
        for expected, command in self.commands():
            response = io.BytesIO()
            with (
                self.subTest(path=expected),
                patch.dict(os.environ, {}, clear=True),
                patch.object(urllib.request, "urlopen", return_value=response) as opened,
            ):
                # Execute the tracked declaration with only its network boundary controlled.
                exec(compile(command[-1], "<declared-image-probe>", "exec"), {})  # noqa: S102
                self.assertEqual(
                    opened.call_args_list,
                    [call(f"http://127.0.0.1:{exposed}{expected}", timeout=3)],
                )
                self.assertTrue(response.closed)

    def commands(self):
        declaration = next(
            (
                line
                for line in Path("Dockerfile").read_text().splitlines()
                if line.startswith("HEALTHCHECK ")
            ),
            "",
        )
        self.assertIn(" CMD ", declaration, "The image must declare an executable health probe")
        image = json.loads(declaration.partition(" CMD ")[2])
        compose = yaml.safe_load(Path("container/compose.local.yaml").read_text())["services"][
            "mcp"
        ]
        self.assertIn("healthcheck", compose, "The local MCP service must declare readiness")
        ready = compose["healthcheck"]["test"]
        self.assertEqual(ready[0], "CMD")
        return (("/health", image), ("/ready", ready[1:]))

    def test_declared_probes_distinguish_healthy_unavailable_and_unreachable(self):
        for expected, command in self.commands():
            with (
                self.subTest(path=expected),
                ThreadingHTTPServer(("127.0.0.1", 0), ProbeReply) as server,
            ):
                thread = Thread(target=server.serve_forever)
                thread.start()
                port = server.server_port
                try:
                    for status in (HTTPStatus.OK, HTTPStatus.SERVICE_UNAVAILABLE):
                        ProbeReply.status, ProbeReply.requested = status, []
                        result = self.probe(command, port)
                        self.assertEqual(
                            result.returncode == 0, status == HTTPStatus.OK, result.stderr
                        )
                        self.assertEqual(ProbeReply.requested, [expected])
                finally:
                    server.shutdown()
                    thread.join()
            self.assertNotEqual(self.probe(command, port).returncode, 0)

    @staticmethod
    def probe(command, port):
        environment = {"PATH": os.environ["PATH"]}
        if port is not None:
            environment["NCI_SIP_HTTP_PORT"] = str(port)
        return subprocess.run(  # noqa: S603 - declared local probe with this interpreter
            [sys.executable, *command[1:]],
            env=environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=8,
        )
