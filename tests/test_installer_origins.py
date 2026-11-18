"""Repository renames preserve exact release identity at the installer CLI boundary."""

import importlib.metadata
import io
import json
import shutil
import subprocess
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from nci_si_mcp import installer

OLD = "https://github.com/CBIIT/nci-si-mcp"
NEW = "https://github.com/CBIIT/nci-sip-mcp"
COMMIT = "b" * 40


def direct_url(origin):
    return {"url": origin, "vcs_info": {"vcs": "git", "commit_id": COMMIT}}


def write_distribution(record, direct, *, version="0.29.0"):
    record.mkdir(parents=True, exist_ok=True)
    (record / "METADATA").write_text(
        f"Metadata-Version: 2.4\nName: nci-si-mcp\nVersion: {version}\n", encoding="utf-8"
    )
    (record / "direct_url.json").write_text(json.dumps(direct), encoding="utf-8")


class InstallerOriginsTest(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.data = self.root / "installation"
        self.bootstrap = self.root / "bootstrap/nci_si_mcp-0.29.0.dist-info"
        self.installed = self.data / "venv/lib/python3.14/site-packages/nci_si_mcp-0.29.0.dist-info"
        self.lock = self.root / "server-requirements.txt"
        self.lock.write_text("demo==1 --hash=sha256:" + "a" * 64 + "\n", encoding="utf-8")
        self.commands = []
        self.installed_origin = NEW
        write_distribution(self.bootstrap, direct_url(OLD))
        for boundary in (
            patch.object(installer, "__version__", "0.29.0"),
            patch.object(installer, "SERVER_REQUIREMENTS", self.lock),
            patch.object(
                installer.importlib.metadata,
                "distribution",
                return_value=importlib.metadata.PathDistribution(self.bootstrap),
            ),
            patch.object(
                installer,
                "local_prerequisites",
                return_value=(installer.Prerequisite("offline", "Repair fixture.", lambda: True),),
            ),
            patch.object(installer.subprocess, "run", side_effect=self.command),
        ):
            boundary.start()
            self.addCleanup(boundary.stop)

    def command(self, argv, **_kwargs):
        self.commands.append(argv)
        if argv[1:3] == ["-m", "venv"]:
            python = self.data / "venv/bin/python"
            python.parent.mkdir(parents=True, exist_ok=True)
            python.touch()
        if argv[1:4] == ["-m", "pip", "install"] and "--no-deps" in argv:
            write_distribution(self.installed, direct_url(self.installed_origin))
        return subprocess.CompletedProcess(argv, 0)

    def invoke(self, command):
        output, error = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(error):
            code = installer.main(
                [command, "--target", "local", "--data-dir", str(self.data)]
                + (["--non-interactive"] if command == "run" else [])
            )
        return code, json.loads(output.getvalue()) if output.getvalue() else {}, error.getvalue()

    def assert_completed(self, result):
        code, record, error = result
        self.assertEqual((code, error), (0, ""))
        self.assertEqual(record["completed"], list(installer.INSTALL_STAGES))
        self.assertFalse((self.data / "venv" / installer.INSTALL_INCOMPLETE).exists())

    def test_cli_install_accepts_both_canonical_origins(self):
        for bootstrap in (OLD, NEW):
            for installed in (OLD, NEW):
                with self.subTest(bootstrap=bootstrap, installed=installed):
                    write_distribution(self.bootstrap, direct_url(bootstrap))
                    self.installed_origin = installed
                    self.commands.clear()
                    self.assert_completed(self.invoke("run"))
                    self.assertEqual(self.commands[2][-1], f"nci-sip-mcp @ git+{NEW}@{COMMIT}")
                shutil.rmtree(self.data, ignore_errors=True)

    def test_status_reverifies_both_canonical_installed_origins(self):
        self.installed_origin = OLD
        self.assert_completed(self.invoke("run"))
        for origin in (OLD, NEW):
            with self.subTest(origin=origin):
                write_distribution(self.installed, direct_url(origin))
                self.assert_completed(self.invoke("status"))

    def test_cli_blocks_untrusted_bootstrap_origins(self):
        self.data.mkdir()
        sentinel = self.data / "unowned"
        sentinel.write_text("preserve", encoding="utf-8")
        for origin in (
            "https://github.com/foreign/nci-si-mcp",
            "https://github.com/CBIIT/other",
            "https://github.com.evil/CBIIT/nci-sip-mcp",
            "http://github.com/CBIIT/nci-sip-mcp",
            NEW + ".git",
            NEW + "/",
            NEW + "?ref=main",
            NEW + "#main",
            "https://user@github.com/CBIIT/nci-sip-mcp",
            [NEW],
            None,
        ):
            with self.subTest(origin=origin):
                write_distribution(self.bootstrap, direct_url(origin))
                code, record, error = self.invoke("run")
                self.assertEqual((code, error), (1, ""))
                self.assertEqual([row["step"] for row in record["blocked"]], ["install"])
                self.assertEqual(sentinel.read_text(encoding="utf-8"), "preserve")
                self.assertFalse((self.data / "venv").exists())

    def test_status_rejects_untrusted_installed_origins(self):
        for direct in (
            direct_url("https://github.com/foreign/nci-sip-mcp"),
            direct_url("https://example.org/CBIIT/nci-si-mcp"),
            {"url": NEW, "vcs_info": {"vcs": "hg", "commit_id": COMMIT}},
            {"url": NEW, "vcs_info": {"vcs": "git", "commit_id": "b" * 7}},
        ):
            with self.subTest(direct=direct):
                self.installed_origin = OLD
                self.assert_completed(self.invoke("run"))
                write_distribution(self.installed, direct)
                code, record, error = self.invoke("status")
                self.assertEqual((code, error), (1, ""))
                self.assertEqual(record["completed"], ["check_prerequisites"])
                self.assertEqual(self.invoke("start")[0], 2)
                shutil.rmtree(self.data)
