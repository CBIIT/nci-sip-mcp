"""The smoke command checks retention and reaps its container-owned assets."""

import asyncio
import errno
import io
import json
import os
import shutil
import subprocess
import unittest
import urllib.error
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from scripts import container_smoke, processes


class SmokeLifecycle:
    """A stand-in container engine for the whole command. Every step but the two that talk to
    Docker directly is replaced; of those, retained_builds runs first (its container prints the
    builds it finds in the assets) and the reap after it (its container empties the assets)."""

    def __init__(self, changes_build):
        self.changes_build = changes_build
        self.builds = ["prepared"]
        self.listed = False
        self.cleaned = False

    def prepare(self, _image, assets):
        self.assets = assets
        (assets / "model").mkdir()
        (assets / "builds.json").write_text(json.dumps(self.builds))

    def serve(self, _image, _assets):
        if self.changes_build:
            self.builds = ["unexpected replacement"]

    def docker(self, *args):
        if args[0] != "run":
            raise AssertionError(f"Unexpected Docker verb {args[0]}")
        if not self.listed:
            self.listed = True
            return SimpleNamespace(stdout=json.dumps(self.builds))
        for path in self.assets.iterdir():
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
        self.cleaned = True


class SmokeCommandTest(unittest.TestCase):
    def test_command_exercises_auth_default_after_preparing_assets(self):
        events = []

        def prepare(_image, assets):
            events.append("prepare")
            (assets / "model").mkdir()

        def refusal(_image, _assets):
            events.append("auth default")

        with (
            TemporaryDirectory() as directory,
            patch.object(container_smoke, "ROOT", Path(directory)),
            patch("sys.argv", ["container_smoke.py", "test-image"]),
            patch.object(container_smoke, "prepare", prepare),
            patch.object(container_smoke, "default_auth_refusal", refusal),
            patch.object(container_smoke, "failure"),
            patch.object(container_smoke, "serve", side_effect=lambda *_: events.append("serve")),
            patch.object(container_smoke, "retained_builds"),
            patch.object(container_smoke, "docker"),
            redirect_stdout(io.StringIO()),
        ):
            container_smoke.main()
        self.assertEqual(events, ["prepare", "auth default", "serve"])

    def test_changed_build_fails_the_command_and_reaps_assets(self):
        for changed in (False, True):
            with self.subTest(changed=changed), TemporaryDirectory() as directory:
                lifecycle = SmokeLifecycle(changed)
                output = io.StringIO()
                with (
                    patch.object(container_smoke, "ROOT", Path(directory)),
                    patch("sys.argv", ["container_smoke.py", "test-image"]),
                    patch.object(container_smoke, "failure"),
                    patch.object(container_smoke, "prepare", lifecycle.prepare),
                    patch.object(container_smoke, "default_auth_refusal"),
                    patch.object(container_smoke, "serve", lifecycle.serve),
                    patch.object(container_smoke, "docker", lifecycle.docker),
                    redirect_stdout(output),
                ):
                    if changed:
                        with self.assertRaisesRegex(RuntimeError, "Serving changed"):
                            container_smoke.main()
                    else:
                        container_smoke.main()
                self.assertEqual("Image smoke passed" in output.getvalue(), not changed)
                self.assertTrue(lifecycle.cleaned)
                self.assertFalse(lifecycle.assets.exists())


def failing_engine(status, message):
    """A stand-in for subprocess.run: every docker call exits with `status` and `message`."""

    def run(command, check, **_options):
        result = subprocess.CompletedProcess(command, status, "", message)
        if check:
            result.check_returncode()
        return result

    return run


class DockerFailureReportTest(unittest.TestCase):
    def smoke(self, engine=None, **steps):
        """Run the command with every step passing except the ones given; `engine` is docker."""
        assets = {"prepare": lambda _image, assets: (assets / "model").mkdir()}
        quiet = {
            name: patch.object(container_smoke, name, side_effect=assets.get(name))
            for name in ("failure", "prepare", "default_auth_refusal", "serve", "retained_builds")
            if name not in steps
        }
        failing = {
            name: patch.object(container_smoke, name, side_effect=error)
            for name, error in steps.items()
        }
        with (
            TemporaryDirectory() as directory,
            patch.object(container_smoke, "ROOT", Path(directory)),
            patch("sys.argv", ["container_smoke.py", "test-image"]),
            patch.object(
                container_smoke.subprocess, "run", engine or failing_engine(1, "cleanup boom")
            ),
            redirect_stdout(io.StringIO()),
            redirect_stderr(io.StringIO()) as stderr,
        ):
            for patcher in (*quiet.values(), *failing.values()):
                patcher.start()
                self.addCleanup(patcher.stop)
            try:
                container_smoke.main()
            except Exception as error:  # noqa: BLE001 - the test reports whichever error escapes
                return error, stderr.getvalue()
        return None, stderr.getvalue()

    def test_a_failed_docker_call_shows_its_stderr(self):
        with (
            patch.object(container_smoke.subprocess, "run", failing_engine(125, "no such image")),
            self.assertRaises(Exception) as raised,
        ):
            container_smoke.docker("run", "image")
        self.assertEqual(str(raised.exception), "docker run failed (exit 125): no such image")

    def test_a_failed_cleanup_does_not_hide_the_first_failure(self):
        error, stderr = self.smoke(failure=RuntimeError("primary index failure"))
        self.assertEqual(str(error), "primary index failure")
        self.assertEqual(
            stderr,
            "Cleanup of the smoke assets failed: docker run failed (exit 1): cleanup boom\n",
        )

    def test_a_cleanup_that_times_out_is_reported_like_any_other_failure(self):
        def hangs(command, **_options):
            raise subprocess.TimeoutExpired(command, 300)

        error, stderr = self.smoke(engine=hangs, failure=RuntimeError("primary index failure"))
        self.assertEqual(str(error), "primary index failure")
        self.assertRegex(
            stderr,
            r"^Cleanup of the smoke assets failed: Command .* timed out after 300 seconds\n$",
        )

    def test_a_directory_that_cannot_be_removed_does_not_hide_the_first_failure(self):
        # Native Linux leaves container-owned files the host user cannot remove; the removal
        # reports each such path to the caller's onexc handler, as shutil.rmtree does.
        remove = shutil.rmtree

        def rmtree(path, *args, onexc=None, **options):
            if Path(path).name.startswith("container-smoke-"):
                try:
                    raise OSError(errno.EBUSY, "Device or resource busy")
                except OSError as error:  # as in shutil.rmtree, the handler runs in the except
                    onexc(os.rmdir, str(path), error)
                return None
            return remove(path, *args, onexc=onexc, **options)

        with patch.object(shutil, "rmtree", rmtree):
            error, stderr = self.smoke(failure=RuntimeError("primary index failure"))
        self.assertEqual(str(error), "primary index failure")
        self.assertRegex(stderr, "^Cleanup of the smoke assets failed: .*cleanup boom\n$")

    def test_a_failed_cleanup_alone_fails_the_command_and_says_why(self):
        error, stderr = self.smoke()
        self.assertEqual(
            str(error),
            "Cleanup of the smoke assets failed: docker run failed (exit 1): cleanup boom",
        )
        self.assertEqual(stderr, "")


class AuthRefusalEngine:
    def __init__(self, status, message):
        self.status, self.message = status, message
        self.commands = []

    def docker(self, *args, check=True):
        self.commands.append(args)
        result = subprocess.CompletedProcess(args, self.status, "", self.message)
        if check:
            result.check_returncode()
        return result


class DefaultAuthRefusalTest(unittest.TestCase):
    def test_only_nonzero_with_auth_diagnostic_counts_as_refusal(self):
        message = "Required HTTP authentication needs an installed integration"
        for status, diagnostic, accepted in (
            (1, message, True),
            (0, message, False),
            (1, "wrong", False),
        ):
            with self.subTest(status=status, diagnostic=diagnostic):
                engine = AuthRefusalEngine(status, diagnostic)
                with patch.object(container_smoke, "docker", engine.docker):
                    if accepted:
                        self.assert_valid_refusal_passes()
                    else:
                        with self.assertRaisesRegex(
                            RuntimeError,
                            "^Image did not refuse startup without its auth integration$",
                        ):
                            container_smoke.default_auth_refusal("image", Path("assets"))
                self.assert_default_isolated_and_reaped(engine.commands)

    def assert_valid_refusal_passes(self):
        try:
            result = container_smoke.default_auth_refusal("image", Path("assets"))
        except (RuntimeError, subprocess.CalledProcessError) as error:
            self.fail(f"Valid authentication refusal was rejected: {error}")
        self.assertIsNone(result)

    def assert_default_isolated_and_reaped(self, commands):
        self.assertEqual(len(commands), 2, "The refusal container must be reaped")
        command, cleanup = commands
        self.assertIn("--network", command)
        self.assertEqual(command[command.index("--network") + 1], "none")
        environment = [command[i + 1] for i, arg in enumerate(command) if arg == "-e"]
        self.assertFalse(any(value.startswith("NCI_SIP_HTTP_AUTH_MODE=") for value in environment))
        self.assertEqual(cleanup, ("rm", "-f", command[command.index("--name") + 1]))

    def test_command_failure_still_reaps_owned_container(self):
        commands = []

        def docker(*args, **_options):
            commands.append(args)
            if args[0] == "run":
                raise TimeoutError("engine timeout")

        with (
            patch.object(container_smoke, "docker", docker),
            self.assertRaisesRegex(TimeoutError, "^engine timeout$"),
        ):
            container_smoke.default_auth_refusal("image", Path("assets"))
        command, cleanup = commands
        self.assertEqual(cleanup, ("rm", "-f", command[command.index("--name") + 1]))


class DockerHealthTest(unittest.TestCase):
    def test_starting_is_polled_until_healthy(self):
        replies = [SimpleNamespace(stdout=status) for status in ("starting", "starting", "healthy")]
        with (
            patch.object(container_smoke, "docker", side_effect=replies) as docker,
            patch.object(processes.time, "monotonic", side_effect=[0, 1, 2, 3]),
            patch.object(processes.time, "sleep") as sleep,
        ):
            try:
                result = container_smoke.wait_healthy("owned")
            except RuntimeError as error:
                self.fail(f"Container became healthy within the startup bound: {error}")
            self.assertIsNone(result)
        self.assertEqual(docker.call_count, 3)
        self.assertEqual(sleep.call_args_list, [((1,),), ((1,),)])

    def test_unhealthy_is_rejected_without_waiting_for_timeout(self):
        with (
            patch.object(
                container_smoke, "docker", return_value=SimpleNamespace(stdout="unhealthy")
            ),
            patch.object(processes.time, "monotonic", side_effect=[0, 1, 181]),
            patch.object(processes.time, "sleep") as sleep,
            self.assertRaisesRegex(RuntimeError, "^Docker health is unhealthy, not healthy$"),
        ):
            container_smoke.wait_healthy("owned")
        sleep.assert_not_called()

    def test_starting_forever_reports_timeout(self):
        with (
            patch.object(
                container_smoke, "docker", return_value=SimpleNamespace(stdout="starting")
            ) as docker,
            patch.object(processes.time, "monotonic", side_effect=[0, 1, 181]),
            patch.object(processes.time, "sleep"),
            self.assertRaisesRegex(
                RuntimeError, "^Docker health did not become healthy within 180 seconds$"
            ),
        ):
            container_smoke.wait_healthy("owned")
        self.assertEqual(docker.call_count, 2)  # the last probe falls at the deadline


class SmokeProbeTest(unittest.TestCase):
    def test_preparation_requires_success_and_json_diagnostics(self):
        for status, stderr, error in (
            (1, "failed to prepare", RuntimeError),
            (0, "not JSON", json.JSONDecodeError),
        ):
            with (
                self.subTest(status=status),
                patch.object(
                    container_smoke,
                    "docker",
                    return_value=SimpleNamespace(returncode=status, stderr=stderr),
                ),
                self.assertRaises(error),
            ):
                container_smoke.prepare("image", Path("assets"))

    def test_missing_assets_require_one_matching_failure_and_exit_one(self):
        failure = {"event": "container_startup_failed", "asset": "index"}
        cases = [
            (0, [failure]),
            (2, [failure]),
            (1, []),
            (1, [failure, failure]),
            (1, [failure | {"asset": "model"}]),
            (1, [failure | {"event": "unexpected_failure"}]),
        ]
        for status, records in cases:
            with (
                self.subTest(status=status, records=records),
                patch.object(
                    container_smoke,
                    "docker",
                    return_value=SimpleNamespace(
                        returncode=status, stderr="\n".join(map(json.dumps, records))
                    ),
                ),
                self.assertRaisesRegex(RuntimeError, "Expected one index startup error"),
            ):
                container_smoke.failure("image", Path("assets"), "index")

    def test_readiness_exhaustion_fails_even_after_transient_network_errors(self):
        for response in ({"status": "starting"}, urllib.error.URLError("not yet listening")):
            with (
                self.subTest(response=response),
                patch.object(processes.time, "monotonic", side_effect=[0, 1, 181]),
                patch.object(processes.time, "sleep"),
                patch.object(container_smoke, "request", side_effect=[response, response]),
                self.assertRaisesRegex(RuntimeError, "never became ready"),
            ):
                container_smoke.wait_ready(8000)

    def test_readiness_recovers_from_transient_failures(self):
        replies = [TimeoutError(), ConnectionError(), {"status": "ready"}]
        with (
            patch.object(processes.time, "sleep"),
            patch.object(container_smoke, "request", side_effect=replies),
        ):
            # The positive control makes rejecting every image fail this same test.
            self.assertIsNone(container_smoke.wait_ready(8000))


class SmokeClient:
    def __init__(self, *, tools=None, version="26.09d", concepts=1, error="invalid_request"):
        self.tools = container_smoke.MCP_TOOLS if tools is None else tools
        self.manifest = {"version": version, "concepts": concepts}
        self.error = error

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def list_tools(self):
        return SimpleNamespace(tools=[SimpleNamespace(name=name) for name in self.tools])

    async def read_resource(self, _uri):
        return SimpleNamespace(
            model_dump=lambda **_kwargs: {"contents": [{"text": json.dumps(self.manifest)}]}
        )

    async def call_tool(self, _name, _arguments):
        return SimpleNamespace(structured_content={"error": {"code": self.error}})


class SmokeSurfaceTest(unittest.TestCase):
    def probe(self, manifest, client):
        with TemporaryDirectory() as directory:
            assets = Path(directory)
            (assets / "manifest.json").write_text(json.dumps(manifest))
            with patch.object(container_smoke, "Client", return_value=client):
                asyncio.run(container_smoke.surface(8000, assets))

    def test_protocol_probes_reject_wrong_profile_manifest_and_validation(self):
        cases = (
            ({"tools": set()}, "unified profile"),
            ({"version": "older"}, "another index manifest"),
            ({"concepts": 0}, "another index manifest"),
            ({"error": "upstream_unavailable"}, "before upstream I/O"),
        )
        manifest = {"release_version": "26.09d", "embedding_revision": "sha256:" + "0" * 64}
        for changes, message in cases:
            with self.subTest(changes=changes), self.assertRaisesRegex(RuntimeError, message):
                self.probe(manifest, SmokeClient(**changes))

    def test_the_directory_model_must_be_identified_by_its_content_hash(self):
        for revision in (None, "a" * 40):
            manifest = {"release_version": "26.09d", "embedding_revision": revision}
            with self.subTest(revision=revision), self.assertRaisesRegex(RuntimeError, "hash"):
                self.probe(manifest, SmokeClient())


class SmokeEngine:
    def __init__(self, exit_code="0"):
        self.exit_code = exit_code
        self.running = False
        self.removed = False

    def docker(self, *args, **_kwargs):
        operation = args[0]
        if operation == "run":
            self.running = True
        elif operation == "stop":
            self.running = False
        elif operation == "rm":
            self.running, self.removed = False, True
        output = self.exit_code if operation == "inspect" else '{"event":"diagnostic"}\n'
        return SimpleNamespace(stdout=output, stderr="")


class SmokeCleanupTest(unittest.TestCase):
    def test_failed_docker_health_blocks_smoke_and_reaps_the_container(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "tmp").mkdir()
            engine = SmokeEngine()

            def docker(*args, **options):
                if "{{.State.Health.Status}}" in args:
                    return SimpleNamespace(stdout="unhealthy", stderr="")
                return engine.docker(*args, **options)

            with (
                patch.object(container_smoke, "ROOT", root),
                patch.object(container_smoke, "docker", docker),
                patch.object(container_smoke, "wait_ready"),
                patch.object(container_smoke, "request", return_value={"status": "ok"}),
                patch.object(container_smoke, "surface"),
                self.assertRaisesRegex(RuntimeError, "Docker health"),
            ):
                container_smoke.serve("image", root)
            self.assertTrue(engine.removed)
            self.assertFalse(engine.running)

    def test_failed_diagnostics_still_remove_the_owned_container(self):
        for failure_at in ("collect", "write"):
            with self.subTest(failure_at=failure_at), TemporaryDirectory() as directory:
                root = Path(directory)
                engine = SmokeEngine()

                def docker(*args, failure_at=failure_at, engine=engine, **options):
                    if args[0] == "logs" and failure_at == "collect":
                        raise TimeoutError("Cannot collect diagnostics")
                    return engine.docker(*args, **options)

                with (
                    patch.object(container_smoke, "ROOT", root),
                    patch.object(container_smoke, "docker", docker),
                    patch.object(
                        container_smoke, "wait_ready", side_effect=RuntimeError("startup")
                    ),
                    patch.object(Path, "write_text", side_effect=PermissionError("Cannot write")),
                    self.assertRaises((TimeoutError, PermissionError)) as raised,
                ):
                    container_smoke.serve("image", root)
                self.assertEqual(str(raised.exception.__context__), "startup")
                self.assertFalse(engine.running)
                self.assertTrue(engine.removed)

    def test_failed_health_and_shutdown_keep_diagnostics_and_remove_owned_container(self):
        cases = (({"status": "bad"}, "0", "Health endpoint"), ({"status": "ok"}, "1", "gracefully"))
        for health, exit_code, message in cases:
            with self.subTest(message=message), TemporaryDirectory() as directory:
                root = Path(directory)
                (root / "tmp").mkdir()
                engine = SmokeEngine(exit_code)
                with (
                    patch.object(container_smoke, "ROOT", root),
                    patch.object(container_smoke, "docker", engine.docker),
                    patch.object(container_smoke, "wait_ready"),
                    patch.object(container_smoke, "wait_healthy"),
                    patch.object(container_smoke, "request", return_value=health),
                    patch.object(container_smoke, "surface"),
                    self.assertRaisesRegex(RuntimeError, message),
                ):
                    container_smoke.serve("image", root)
                self.assertFalse(engine.running)
                self.assertTrue(engine.removed)
                self.assertEqual(
                    json.loads((root / "tmp/container-smoke.log").read_text()),
                    {"event": "diagnostic"},
                )
