"""Installed wheel names and executable entry points are public interfaces."""

import importlib.metadata
import os
import shutil
import subprocess
import sys
import tarfile
import unittest
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
BUILD_TIMEOUT_SECONDS = 300
CARRIED_TOGETHER = (
    "tests/fakes.py",
    "tests/test_pdm_isolation.py",
    "test_support/pdm_launch.py",
    "test_support/pdm_stand_in.py",
)


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


def tracked_copy(project, stage):
    """Copy the project's tracked files (working-tree bytes) into an owned build directory."""
    listing = subprocess.run(  # noqa: S603 - fixed git listing of this checkout
        ["git", "-C", str(project), "ls-files", "-z"],  # noqa: S607
        check=True,
        capture_output=True,
    )
    names = [name for name in listing.stdout.decode().split("\0") if name]
    for name in names:
        source = project / name
        if source.is_file():
            (stage / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, stage / name)
    return names


def build_both(stage, output):
    """Build the sdist and wheel of `stage` in a child, leaving this process's cwd and env alone."""
    built = subprocess.run(  # noqa: S603 - the locked build backend on an owned copy
        [
            sys.executable,
            "-c",
            "import sys, pdm.backend as b; b.build_sdist(sys.argv[1]); b.build_wheel(sys.argv[1])",
            str(output),
        ],
        cwd=stage,
        env=os.environ | {"PDM_BUILD_SCM_VERSION": "0.0.0"},
        check=False,
        capture_output=True,
        text=True,
        # A finite, generous bound, not a measured duration; a timeout alone does not say why.
        timeout=BUILD_TIMEOUT_SECONDS,
    )
    if built.returncode != 0:
        raise AssertionError(
            f"build exited {built.returncode}\nstdout:\n{built.stdout}\nstderr:\n{built.stderr}"
        )
    sdist = next(output.glob("*.tar.gz"))
    wheel = next(output.glob("*.whl"))
    with tarfile.open(sdist) as archive:
        prefix = archive.getnames()[0].split("/")[0]
        members = {
            name.removeprefix(prefix + "/"): archive.extractfile(name).read()
            for name in archive.getnames()
            if archive.getmember(name).isfile()
        }
    with zipfile.ZipFile(wheel) as archive:
        return members, archive.namelist()


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

    def test_source_distribution_carries_the_test_helpers_and_the_wheel_carries_no_tests(self):
        with TemporaryDirectory() as temporary:
            stage, output = Path(temporary) / "stage", Path(temporary) / "dist"
            stage.mkdir()
            output.mkdir()
            tracked = tracked_copy(ROOT, stage)
            sdist, wheel = build_both(stage, output)
            tests = {name for name in tracked if name.startswith("tests/") and name.endswith(".py")}
            self.assertEqual(tests - set(sdist), set())
            # The importing tests and their helpers, named here rather than read from the archive.
            for carried in CARRIED_TOGETHER:
                with self.subTest(carried=carried):
                    self.assertEqual(sdist.get(carried), (ROOT / carried).read_bytes())
            self.assertEqual(
                [name for name in wheel if name.startswith(("tests/", "test_support/"))], []
            )
