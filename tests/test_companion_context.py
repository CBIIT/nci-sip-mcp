"""Companion image inputs must exclude checkout credentials and operational evidence."""

import ast
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import unittest
import urllib.request
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from scripts.companion_context import prepare_context
from scripts.operator_source import head_commit


class CompanionContextTest(unittest.TestCase):
    def test_dependency_lock_drift_does_not_produce_an_image_context(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "container").mkdir()
            (root / "pdm.lock").write_bytes(Path("pdm.lock").read_bytes())
            (root / "container/companion-requirements.txt").write_text("stale dependencies")
            output = root / "context"
            with (
                patch("scripts.companion_context.clean_commit", return_value="a" * 40),
                self.assertRaisesRegex(ValueError, "lock drifted"),
            ):
                prepare_context(root, output)
            self.assertFalse(output.exists())

    @staticmethod
    def site(_root, output):
        output.mkdir()
        (output / "index.html").write_text("Public documentation")

    @staticmethod
    def wheels(_root, output):
        output.mkdir()
        (output / "runtime.whl").write_bytes(b"built package")

    def test_only_fixed_runtime_paths_and_public_site_enter_separate_contexts(self):
        root = Path.cwd()
        commit = head_commit(root)
        with TemporaryDirectory() as directory:
            output = Path(directory) / "context"
            with (
                patch("scripts.companion_context.clean_commit", return_value=commit),
                patch("scripts.companion_context.build_site", side_effect=self.site),
                patch("scripts.companion_context._wheels", side_effect=self.wheels),
            ):
                prepare_context(root, output)
            manifest = json.loads((output / "admin/bundle/operator-source.json").read_text())
            self.assertEqual(manifest["commit"], commit)
            self.assertTrue((output / "admin/app/src/nci_sip_mcp/cli.py").is_file())
            self.assertFalse((output / "admin/app/.git").exists())
            self.assertFalse((output / "admin/app/tmp").exists())
            self.assertEqual(
                (output / "admin/app/docs/site-assets/fonts/open-sans.ttf").read_bytes(),
                (root / "docs/site-assets/fonts/open-sans.ttf").read_bytes(),
            )
            site_assets = output / "admin/app/docs/site-assets"
            self.assertEqual(
                sorted(entry.name for entry in site_assets.iterdir() if entry.is_file()),
                ["ncids.css", "portal.css", "zensical.toml"],
            )
            for name in ("ncids.css", "portal.css", "zensical.toml"):
                self.assertEqual(
                    (site_assets / name).read_bytes(),
                    (root / "docs/site-assets" / name).read_bytes(),
                )
            self.assertFalse((output / "admin/app/docs/upstream").exists())
            self.assertEqual((output / "docs/site/index.html").read_text(), "Public documentation")
            self.assertEqual(
                {p.name for p in (output / "docs").iterdir()},
                {"site", "Dockerfile", "scripts"},
            )
            self.assertEqual(
                {p.name for p in (output / "docs/scripts").iterdir()},
                {"processes.py", "static_server.py", "companion_relay.py"},
            )

    def test_source_change_during_build_leaves_no_publishable_context(self):
        with TemporaryDirectory() as directory:
            output = Path(directory) / "context"
            with (
                patch("scripts.companion_context.clean_commit", side_effect=["a" * 40, "b" * 40]),
                patch("scripts.companion_context._populate"),
                self.assertRaisesRegex(ValueError, "changed"),
            ):
                prepare_context(Path.cwd(), output)
            self.assertFalse(output.exists())
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_dirty_checkout_is_rejected_before_context_creation(self):
        with TemporaryDirectory() as directory:
            output = Path(directory) / "context"
            with (
                patch("scripts.companion_context.clean_commit", side_effect=ValueError("dirty")),
                self.assertRaisesRegex(ValueError, "dirty"),
            ):
                prepare_context(Path.cwd(), output)
            self.assertFalse(output.exists())

    def test_existing_output_preserves_human_content(self):
        with TemporaryDirectory() as directory:
            output = Path(directory)
            (output / "keep").write_text("human")
            with self.assertRaises(FileExistsError):
                prepare_context(Path.cwd(), output)
            self.assertEqual((output / "keep").read_text(), "human")


ROOT = Path(__file__).resolve().parents[1]
DOCS_DOCKERFILE = ROOT / "container/Docs.Dockerfile"


def _dockerfile_lines(keyword):
    return [
        line.split(maxsplit=1)[1]
        for line in DOCS_DOCKERFILE.read_text().splitlines()
        if line.startswith(keyword + " ")
    ]


def _free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _locally(argument, image, port):
    """One entrypoint argument as it runs outside the image: paths under `image`."""
    if argument == "python3.14":
        return sys.executable
    if argument.startswith("/"):
        return str(image / argument.lstrip("/"))
    return {"0.0.0.0": "127.0.0.1", "8080": str(port)}.get(argument, argument)  # noqa: S104


def _health(image, port):
    arguments = json.loads(_dockerfile_lines("ENTRYPOINT")[0])
    command = [_locally(argument, image, port) for argument in arguments]
    workdir = image / (_dockerfile_lines("WORKDIR") or ["/"])[0].lstrip("/")
    with subprocess.Popen(  # noqa: S603 - the interpreter and entrypoint are fixed above
        command,
        cwd=workdir,
        env={"PATH": os.environ["PATH"]},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ) as server:
        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and server.poll() is None:
                try:
                    url = f"http://127.0.0.1:{port}/health"
                    return urllib.request.urlopen(url, timeout=1).read()
                except OSError:
                    time.sleep(0.1)
            return b""
        finally:
            server.terminate()


class DocsImageTest(unittest.TestCase):
    def test_docs_dockerfile_ships_the_scripts_package_and_runs_the_module(self):
        self.assertEqual(_dockerfile_lines("COPY"), ["site /site", "scripts /app/scripts"])
        self.assertEqual(_dockerfile_lines("WORKDIR"), ["/app"])
        self.assertEqual(
            _dockerfile_lines("ENTRYPOINT"),
            [
                '["python3.14", "-m", "scripts.static_server", "--directory", "/site", '
                '"--host", "0.0.0.0", "--port", "8080"]'
            ],
        )

    def test_compose_relay_runs_the_module_from_the_docs_image_workdir(self):
        compose = (ROOT / "container/compose.local.yaml").read_text()
        self.assertIn('entrypoint: ["python3.14", "-m", "scripts.companion_relay"]', compose)

    def test_scripts_processes_imports_only_the_standard_library(self):
        # The Docs image has no installed packages; processes.py ships beside the servers.
        nodes = list(ast.walk(ast.parse((ROOT / "scripts/processes.py").read_text())))
        modules = {
            a.name.split(".")[0] for n in nodes if isinstance(n, ast.Import) for a in n.names
        }
        modules |= {
            n.module.split(".")[0] for n in nodes if isinstance(n, ast.ImportFrom) and n.module
        }
        self.assertEqual(modules - set(sys.stdlib_module_names), set())

    def test_docs_image_entrypoint_serves_health_from_the_built_context(self):
        with TemporaryDirectory() as directory:
            context = Path(directory) / "context"
            image = Path(directory) / "image"
            with (
                patch("scripts.companion_context.clean_commit", return_value=head_commit(ROOT)),
                patch(
                    "scripts.companion_context.build_site", side_effect=CompanionContextTest.site
                ),
                patch("scripts.companion_context._wheels", side_effect=CompanionContextTest.wheels),
            ):
                prepare_context(ROOT, context)
            for copy in _dockerfile_lines("COPY"):
                source, target = copy.split()
                destination = image / target.lstrip("/")
                destination.parent.mkdir(parents=True, exist_ok=True)
                origin = context / "docs" / source
                if origin.is_dir():
                    shutil.copytree(origin, destination)
                else:
                    shutil.copyfile(origin, destination)
            self.assertEqual(
                _health(image, _free_port()), b'{"status":"ok","service":"documentation"}'
            )


class AdminImageTest(unittest.TestCase):
    def test_admin_dockerfile_copies_the_context_in_exactly_these_steps(self):
        lines = (ROOT / "container/Admin.Dockerfile").read_text().splitlines()
        self.assertEqual(
            [line for line in lines if line.startswith("COPY ")],
            [
                "COPY requirements.txt /build/requirements.txt",
                "COPY wheels/*.whl /build/",
                "COPY --from=dependencies /opt/venv /opt/venv",
                "COPY app /app",
                "COPY bundle/operator-source.* /app/",
            ],
        )
        self.assertIn("WORKDIR /app", lines)
