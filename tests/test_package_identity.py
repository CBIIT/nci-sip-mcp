"""Installed wheel names and executable entry points are public interfaces."""

import importlib.metadata
import os
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]


def install_wheel(project, target):
    wheelhouse = target.parent / "wheels"
    wheelhouse.mkdir(parents=True)
    subprocess.run(  # noqa: S603 - fixed local wheel build command
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            "--no-deps",
            "--no-build-isolation",
            "--wheel-dir",
            str(wheelhouse),
            str(project),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    wheel = next(wheelhouse.glob("*.whl"))
    subprocess.run(  # noqa: S603 - installs the locally built wheel
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--no-deps",
            "--target",
            str(target),
            str(wheel),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return target


class PackageIdentityTest(unittest.TestCase):
    def test_core_wheel_installs_named_distribution_and_entrypoints(self):
        with TemporaryDirectory() as temporary:
            target = install_wheel(ROOT, Path(temporary) / "core" / "installed")
            installed = {
                item.metadata["Name"]
                for item in importlib.metadata.distributions(path=[str(target)])
            }
            self.assertEqual(installed, {"nci-sip-mcp"})
            self.assertTrue((target / "nci_sip_mcp" / "__init__.py").is_file())
            environment = os.environ | {"PYTHONPATH": str(target)}
            for script in ("nci-sip-mcp", "nci-sip-install"):
                result = subprocess.run(  # noqa: S603 - fixed installed entry point
                    [str(target / "bin" / script), "--help"],
                    env=environment,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_acceptance_wheel_installs_named_distribution_and_module(self):
        with TemporaryDirectory() as temporary:
            target = install_wheel(
                ROOT / "acceptance", Path(temporary) / "acceptance" / "installed"
            )
            installed = {
                item.metadata["Name"]
                for item in importlib.metadata.distributions(path=[str(target)])
            }
            self.assertEqual(installed, {"nci-sip-acceptance"})
            self.assertTrue((target / "nci_sip_acceptance" / "__init__.py").is_file())
            result = subprocess.run(
                [sys.executable, "-c", "import nci_sip_acceptance"],
                env=os.environ | {"PYTHONPATH": str(target)},
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
