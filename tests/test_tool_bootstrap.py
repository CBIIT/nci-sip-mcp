"""The shipped bootstrap guard rejects an unsafe or unrelated tool environment."""

import json
import os
import re
import subprocess
import sys
import unittest
import venv
from pathlib import Path
from tempfile import TemporaryDirectory

import yaml

ROOT = Path(__file__).resolve().parents[1]


class ToolBootstrapTest(unittest.TestCase):
    def guard(self):
        setup = yaml.safe_load((ROOT / ".github/actions/setup/action.yml").read_text())
        found = [
            s for s in setup["runs"]["steps"] if s.get("name") == "Verify PDM tool environment"
        ]
        self.assertEqual(
            len(found), 1, "The actual bootstrap must run its guard before installation"
        )
        return found[0]["run"]

    def execute(
        self,
        versions=None,
        *,
        wrong_executable=False,
        missing_interpreter=False,
        wrong_interpreter=False,
    ):
        with TemporaryDirectory() as directory:
            home = Path(directory)
            environment = home / "venv"
            venv.EnvBuilder(with_pip=False).create(environment)
            site = next((environment / "lib").glob("python*/site-packages"))
            for name, value in (versions or {}).items():
                metadata = site / f"{name}-{value}.dist-info"
                metadata.mkdir()
                (metadata / "METADATA").write_text(f"Name: {name}\nVersion: {value}\n")
            (home / "bin").mkdir()
            (home / "bin/pdm").write_text("#!/bin/sh\nexit 0\n")
            (home / "bin/pdm").chmod(0o700)
            if missing_interpreter:
                (environment / "bin/python").unlink()
            if wrong_interpreter:
                (environment / "pyvenv.cfg").unlink()
            path = str(home / "bin") if not wrong_executable else "/usr/bin"
            return subprocess.run(  # noqa: S603 - the actual repository-owned guard in an isolated environment.
                ["/bin/bash", "-euo", "pipefail", "-c", self.guard()],
                env=os.environ | {"PDM_TOOL_HOME": str(home), "PATH": path + ":/usr/bin:/bin"},
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )

    def supported(self):
        return {"pdm": "2.29.2", "hishel": "1.2.1", "unearth": "0.18.2", "virtualenv": "21.15.2"}

    def test_supported_tool_reports_the_complete_actual_inventory(self):
        result = self.execute(self.supported() | {"pip": "26.2.2"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout),
            [
                ["hishel", "1.2.1"],
                ["pdm", "2.29.2"],
                ["pip", "26.2.2"],
                ["unearth", "0.18.2"],
                ["virtualenv", "21.15.2"],
            ],
        )

    def test_vulnerable_or_incompatible_distribution_fails_before_installation(self):
        for name, value in (
            ("virtualenv", "21.3.1"),
            ("pdm", "2.29.6"),
            ("hishel", "2.0.0"),
            ("unearth", "0.17.5"),
        ):
            with self.subTest(name=name):
                result = self.execute(self.supported() | {name: value})
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stderr, "Unsupported PDM tool distribution\n")

    def test_missing_distribution_fails_before_installation(self):
        for name in self.supported():
            with self.subTest(name=name):
                versions = self.supported()
                del versions[name]
                result = self.execute(versions)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stderr, "Missing PDM tool distribution\n")

    def test_unrelated_pdm_executable_fails_before_installation(self):
        result = self.execute(self.supported(), wrong_executable=True)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stderr, "Unexpected PDM executable\n")

    def test_missing_interpreter_fails_before_installation(self):
        result = self.execute(self.supported(), missing_interpreter=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("/venv/bin/python: No such file or directory", result.stderr)

    def test_unrelated_interpreter_fails_before_installation(self):
        result = self.execute(self.supported(), wrong_interpreter=True)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stderr, "Unexpected PDM tool interpreter\n")


class BootstrapPipEvidenceTest(unittest.TestCase):
    def execute(self, content):
        setup = yaml.safe_load((ROOT / ".github/actions/setup/action.yml").read_text())
        found = [
            s for s in setup["runs"]["steps"] if s.get("name") == "Record bootstrap pip versions"
        ]
        self.assertEqual(
            len(found), 1, "Bootstrap pip evidence must be recorded before installation"
        )
        with TemporaryDirectory() as directory:
            home = Path(directory)
            (home / "venv/bin").mkdir(parents=True)
            (home / "venv/bin/python").symlink_to(sys.executable)
            log = home / "pip.log"
            if content is not None:
                log.write_text(content)
            return subprocess.run(  # noqa: S603 - execute the actual selective reporter against owned local records.
                ["/bin/bash", "-euo", "pipefail", "-c", found[0]["run"]],
                env=os.environ | {"PDM_TOOL_HOME": str(home), "PDM_TOOL_PIP_LOG": str(log)},
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )

    def test_only_executing_pip_versions_are_reported(self):
        result = self.execute(
            "private-index-or-credential-canary\n"
            "2026-11-05T12:13:19,528 Using pip 25.3 from /tool/site-packages/pip (python 3.14)\n"
            "unrelated installation details\n"
            "2026-11-05T12:13:20,528 Using pip 26.2.2 from /tool/site-packages/pip (python 3.14)\n"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, '["25.3", "26.2.2"]\n')
        self.assertEqual(result.stderr, "")

    def test_missing_empty_or_incomplete_evidence_fails_visibly(self):
        for content, reason in (
            (None, "Missing"),
            ("", "Incomplete"),
            ("unrelated installation details\n", "Incomplete"),
        ):
            with self.subTest(content=content):
                result = self.execute(content)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stderr, reason + " bootstrap pip version evidence\n")
                self.assertEqual(result.stdout, "")

    def test_one_record_from_each_actual_invocation_is_enough(self):
        result = self.execute("time Using pip 26.2.2 from /tool/pip (python 3.14)\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, '["26.2.2"]\n')

    def test_unrecognized_pip_evidence_is_not_silently_dropped(self):
        result = self.execute(
            "time Using pip 26.2.2 from /tool/pip (python 3.14)\n"
            "time Using pip unknown private-canary\n"
            "time Using pip 26.2.2 from /tool/pip (python 3.14)\n"
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stderr, "Unrecognized bootstrap pip version evidence\n")
        self.assertEqual(result.stdout, "")


BOOTSTRAP_ROOTS = {"pdm": "2.29.2", "hishel": "1.2.1", "unearth": "0.18.2", "virtualenv": "21.15.2"}


class BootstrapRequirementsTest(unittest.TestCase):
    def requirements(self):
        text = (ROOT / "container/pdm-bootstrap-requirements.txt").read_text()
        joined = text.replace("\\\n", " ")
        lines = [line for line in joined.splitlines() if line.strip() and line[0] != "#"]
        return [" ".join(line.split()) for line in lines]

    def test_the_four_roots_equal_every_pin_the_bootstrap_is_checked_against(self):
        pins = {}
        for line in self.requirements():
            name, _, rest = line.partition("==")
            pins[name] = rest.split()[0]
        self.assertEqual({name: pins.get(name) for name in BOOTSTRAP_ROOTS}, BOOTSTRAP_ROOTS)
        setup = (ROOT / ".github/actions/setup/action.yml").read_text()
        guard = dict(re.findall(r'"(\w+)": "([\d.]+)"', setup))
        self.assertEqual(guard, BOOTSTRAP_ROOTS)
        local = (ROOT / ".github/workflows/local-run.yml").read_text()
        installed = re.findall(r"--with '(\w+)==([\d.]+)'", local) + re.findall(
            r"'(pdm)==([\d.]+)'", local
        )
        self.assertEqual(dict(installed), BOOTSTRAP_ROOTS)

    def test_every_requirement_is_an_exact_pin_with_only_sha256_hashes(self):
        lines = self.requirements()
        self.assertGreater(len(lines), len(BOOTSTRAP_ROOTS))
        for line in lines:
            with self.subTest(line=line.split()[0]):
                self.assertRegex(line, r"^[A-Za-z0-9_.-]+==[^\s=]+( --hash=sha256:[0-9a-f]{64})+$")
