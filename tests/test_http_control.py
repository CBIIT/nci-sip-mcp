"""The remote fixture control channel isolates settings and owns restarted children."""

import json
import os
import socket
import socketserver
import subprocess
import sys
import unittest
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from types import SimpleNamespace
from unittest.mock import patch

from scripts import acceptance_http

from nci_sip_acceptance.client import OwnedServer


def listening(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


def owned_server(directory):
    return OwnedServer(
        os.environ,
        directory / "server.log",
        data_dir=directory / "index",
        require_index=True,
    )


@contextmanager
def answering_hook(directory, server):
    """The control socket of the runner, serving one hook request for `server`."""
    socket_path = str(directory / "control")
    with socketserver.UnixStreamServer(socket_path, acceptance_http.hook_handler(server)) as hook:
        hook.timeout = 40
        thread = Thread(target=hook.handle_request)
        thread.start()
        try:
            yield socket_path
        finally:
            thread.join(timeout=45)


LISTENS = (
    "import os, socket, time; listener = socket.socket(); "
    "listener.bind(('127.0.0.1', int(os.environ['NCI_SIP_HTTP_PORT']))); "
    "listener.listen(); time.sleep(300)"
)


class StateHookReadinessTest(unittest.TestCase):
    """Given a runner whose server is slow to listen or dies, when the suite calls the state
    hook, then the hook answers only for a server that accepts connections."""

    @contextmanager
    def hooked(self, script):
        spawn = subprocess.Popen

        def launch(_command, **options):
            return spawn([sys.executable, "-c", script], **options)

        with TemporaryDirectory() as directory:
            root = Path(directory)
            server = owned_server(root)
            try:
                with (
                    patch.object(acceptance_http.subprocess, "Popen", launch),
                    answering_hook(root, server) as socket_path,
                    patch.dict(os.environ, {"NCI_SIP_PROFILE": "fixture-profile"}, clear=True),
                ):
                    yield server, root, lambda: acceptance_http.state(socket_path)
            finally:
                server.stop()

    def test_state_hook_acknowledges_only_a_server_that_accepts_connections(self):
        with self.hooked("import time; time.sleep(1); " + LISTENS) as (server, _root, call):
            call()
            self.assertTrue(listening(server.port), "The hook answered before the server listened")

    def test_state_hook_fails_naming_the_log_of_a_server_that_exits_first(self):
        exits = "import sys; print('boom', file=sys.stderr); sys.exit(3)"
        with self.hooked(exits) as (server, root, call):
            with self.assertRaisesRegex(RuntimeError, str(root / "server.log")):
                call()
            self.assertFalse(listening(server.port))


class StateHookFailureTest(unittest.TestCase):
    def test_a_server_that_is_not_ready_in_time_is_reported_to_the_suite(self):
        def restart(_settings):
            raise TimeoutError("The owned server did not become ready; see server.log")

        with (
            TemporaryDirectory() as directory,
            answering_hook(Path(directory), SimpleNamespace(restart=restart)) as socket_path,
            self.assertRaises(RuntimeError) as caught,
        ):
            acceptance_http.state(socket_path)
        self.assertEqual(
            str(caught.exception),
            "The fixture server restart failed: "
            "The owned server did not become ready; see server.log",
        )

    def test_the_runner_waits_for_readiness_inside_the_suites_thirty_second_hook_window(self):
        # The suite's state hook gives up after 30 s, so a restart must be answered sooner.
        options = []

        class Recorded:
            port = 8123

            def __init__(self, *_arguments, **keywords):
                options.append(keywords)

            def __enter__(self):
                return self

            def __exit__(self, *_exception):
                pass

        with (
            TemporaryDirectory() as directory,
            patch.object(acceptance_http, "ROOT", Path(directory)),
            patch.object(acceptance_http, "prepare"),
            patch.object(acceptance_http, "OwnedServer", Recorded),
            patch.object(acceptance_http, "run_suite", return_value=0),
        ):
            acceptance_http.run()
        self.assertEqual(options[0]["ready_seconds"], 25)


class HTTPControlTest(unittest.TestCase):
    def test_deep_worker_workspace_still_has_a_working_private_control_channel(self):
        settings = []
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / ("nested-" * 20)
            root.mkdir()

            def suite(_port, socket_path, **_options):
                with patch.dict(os.environ, {"NCI_SIP_PROFILE": "fixture-profile"}, clear=True):
                    acceptance_http.state(str(socket_path))
                return 0

            class Restarts:
                port = 8123

                def __init__(self, *_arguments, **_options):
                    pass

                def __enter__(self):
                    return self

                def __exit__(self, *_exception):
                    pass

                def restart(self, new):
                    settings.append(new)

            with (
                patch.object(acceptance_http, "ROOT", root),
                patch.object(acceptance_http, "prepare"),
                patch.object(acceptance_http, "OwnedServer", Restarts),
                patch.object(acceptance_http, "run_suite", side_effect=suite),
            ):
                result = acceptance_http.run()
            self.assertEqual(result, 0)
            self.assertEqual(settings, [{"NCI_SIP_PROFILE": "fixture-profile"}])
            self.assertEqual(list((root / "tmp").iterdir()), [])

    def test_preparation_uses_supplied_environment_without_inherited_secrets(self):
        observed = []

        def prepared(_command, *, env, **_options):
            observed.append(env)

        with (
            TemporaryDirectory() as directory,
            patch.dict(os.environ, {"AWS_SECRET_ACCESS_KEY": "PRIVATE-CANARY"}),
            patch.object(acceptance_http.subprocess, "run", prepared),
        ):
            acceptance_http.prepare(Path(directory), environment={"HOME": directory})
        self.assertEqual(observed[0]["HOME"], directory)
        self.assertNotIn("PRIVATE-CANARY", str(observed))
        self.assertTrue(observed[0]["NCI_SIP_EVS_BASE_URL"].startswith("http://127.0.0.1:"))

    def test_unrecorded_preparation_request_cannot_be_treated_as_prepared(self):
        def prepare_with_an_unrecorded_request(_command, *, env, **_options):
            try:
                url = env["NCI_SIP_EVS_BASE_URL"] + "/unrecorded"
                with urllib.request.urlopen(url, timeout=5) as response:  # noqa: S310 - local fixture
                    response.read()
            except urllib.error.HTTPError as error:
                error.close()

        with (
            TemporaryDirectory() as directory,
            patch.object(acceptance_http.subprocess, "run", prepare_with_an_unrecorded_request),
            self.assertRaisesRegex(RuntimeError, "unrecorded upstream response"),
        ):
            acceptance_http.prepare(Path(directory))

    def test_interrupted_runner_reaps_its_server_and_removes_control_directory(self):
        ports = []

        def suite(port, socket_path, **_options):
            ports.append(port)
            with patch.dict(os.environ, {"NCI_SIP_PROFILE": "fixture-profile"}, clear=True):
                acceptance_http.state(str(socket_path))
            self.assertTrue(listening(port))
            raise KeyboardInterrupt

        spawn = subprocess.Popen

        def launch(_command, **options):
            return spawn([sys.executable, "-c", LISTENS], **options)

        with TemporaryDirectory() as directory:
            root = Path(directory)
            with (
                patch.object(acceptance_http, "ROOT", root),
                patch.object(acceptance_http, "prepare"),
                patch.object(acceptance_http, "run_suite", side_effect=suite),
                patch.object(acceptance_http.subprocess, "Popen", launch),
                self.assertRaises(KeyboardInterrupt),
            ):
                acceptance_http.run()
            self.assertEqual(list((root / "tmp").iterdir()), [])
            self.assertFalse(listening(ports[0]))

    def test_state_hook_sends_only_server_settings_and_requires_acknowledgement(self):
        with TemporaryDirectory() as directory:
            socket_path = str(Path(directory) / "control")
            settings = []
            process = SimpleNamespace(restart=settings.append)
            handler = acceptance_http.hook_handler(process)
            with socketserver.UnixStreamServer(socket_path, handler) as server:
                server.timeout = 2
                thread = Thread(target=server.handle_request)
                thread.start()
                try:
                    with patch.dict(
                        os.environ,
                        {
                            "NCI_SIP_EVS_BASE_URL": "http://fixture.invalid",
                            "NCI_SIP_ACCEPTANCE_URL": "http://control.invalid",
                            "UNRELATED": "not a server setting",
                        },
                        clear=True,
                    ):
                        acceptance_http.state(socket_path)
                finally:
                    thread.join(timeout=35)
                self.assertFalse(thread.is_alive())
            self.assertEqual(settings, [{"NCI_SIP_EVS_BASE_URL": "http://fixture.invalid"}])

    def test_state_hook_rejects_a_failed_restart(self):
        class FailedRestart(socketserver.StreamRequestHandler):
            def handle(self):
                self.rfile.readline()
                self.wfile.write(b"failed\n")

        with TemporaryDirectory() as directory:
            socket_path = str(Path(directory) / "control")
            with socketserver.UnixStreamServer(socket_path, FailedRestart) as server:
                server.timeout = 2
                thread = Thread(target=server.handle_request)
                thread.start()
                try:
                    with self.assertRaisesRegex(RuntimeError, "restart failed"):
                        acceptance_http.state(socket_path)
                finally:
                    thread.join(timeout=35)
                self.assertFalse(thread.is_alive())

    def test_restart_replaces_owned_child_and_forces_isolated_http_settings(self):
        spawn = subprocess.Popen
        children = []
        script = (
            "import json, os, sys; "
            "print(json.dumps({k:v for k,v in os.environ.items() if k.startswith('NCI_SIP_')}), "
            "file=sys.stderr, flush=True); " + LISTENS
        )

        def launch(_command, **options):
            children.append(spawn([sys.executable, "-c", script], **options))
            return children[-1]

        with TemporaryDirectory() as directory:
            root = Path(directory)
            server = owned_server(root)
            try:
                with (
                    patch.dict(os.environ, {"NCI_SIP_PROFILE": "inherited-profile"}),
                    patch.object(acceptance_http.subprocess, "Popen", launch),
                ):
                    server.restart({"NCI_SIP_EVS_BASE_URL": "http://first.invalid"})
                    server.restart({"NCI_SIP_EVS_BASE_URL": "http://second.invalid"})
                    running = listening(server.port)
                server.stop()
                statuses = [child.poll() for child in children]
            finally:
                server.stop()
                for child in children:
                    if child.poll() is None:
                        child.kill()
                        child.wait(timeout=5)
            observed = [json.loads(line) for line in (root / "server.log").read_text().splitlines()]
            forced = {
                "NCI_SIP_DATA_DIR": str(root / "index"),
                "NCI_SIP_TRANSPORT": "streamable-http",
                "NCI_SIP_HTTP_PORT": str(server.port),
                "NCI_SIP_HTTP_SESSIONS": "stateful",
                "NCI_SIP_HTTP_REQUIRE_INDEX": "1",
            }
            self.assertEqual(
                observed,
                [
                    forced | {"NCI_SIP_EVS_BASE_URL": f"http://{name}.invalid"}
                    for name in ("first", "second")
                ],
            )
            self.assertEqual(statuses, [-15, -15])
            self.assertTrue(running)
