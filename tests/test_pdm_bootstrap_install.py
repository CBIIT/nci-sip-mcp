"""The setup action's PDM bootstrap installs hashed wheels or publishes nothing."""

import hashlib
import os
import subprocess
import sys
import unittest
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory

import yaml

ROOT = Path(__file__).resolve().parents[1]
SETUP = yaml.safe_load((ROOT / ".github/actions/setup/action.yml").read_text())
VERSIONS = {"pdm": "2.29.2", "hishel": "1.2.1", "unearth": "0.18.2", "virtualenv": "21.15.2"}
INSTALL = "Install PDM from hashed requirements"
PDM_SCRIPT = "import sys\nsys.stdout.write('PDM, version {version}\\n')\n"


def step(name):
    found = [s for s in SETUP["runs"]["steps"] if s.get("name") == name]
    if len(found) != 1:
        raise AssertionError(f"the setup action must have exactly one step named {name!r}")
    return found[0]


def write_wheel(directory, name, version, requires=(), scripts=None):
    path = directory / f"{name}-{version}-py3-none-any.whl"
    metadata = f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n"
    metadata += "".join(f"Requires-Dist: {requirement}\n" for requirement in requires)
    entry = f"[console_scripts]\n{name} = {name}_cli:main\n" if scripts else ""
    with zipfile.ZipFile(path, "w") as archive:
        info = f"{name}-{version}.dist-info"
        archive.writestr(
            f"{name}_cli.py", PDM_SCRIPT.format(version=version) + "def main():\n    pass\n"
        )
        archive.writestr(f"{info}/METADATA", metadata)
        archive.writestr(
            f"{info}/WHEEL", "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
        )
        if entry:
            archive.writestr(f"{info}/entry_points.txt", entry)
        archive.writestr(f"{info}/RECORD", "")
    return path


def requirement_lines(wheelhouse, *, pdm_requires=(), tamper=False):
    lines = []
    for name, version in VERSIONS.items():
        requires = pdm_requires if name == "pdm" else ()
        wheel = write_wheel(wheelhouse, name, version, requires, name == "pdm")
        digest = "0" * 64 if tamper and name == "unearth" else sha256(wheel)
        lines.append(f"{name}=={version} \\\n    --hash=sha256:{digest}\n")
    return lines


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def step_environment(root, wheelhouse):
    action = root / ".github/actions/setup"
    action.mkdir(parents=True)
    python = root / "bin/python"
    python.parent.mkdir()
    python.symlink_to(sys.executable)
    github_path = root / "github_path"
    github_path.write_text("")
    environment = {
        key: value.replace("${{ runner.temp }}", str(root / "temp"))
        for key, value in step(INSTALL)["env"].items()
    }
    return environment | {
        "PATH": f"{python.parent}:/usr/bin:/bin",
        "GITHUB_ACTION_PATH": str(action),
        "GITHUB_PATH": str(github_path),
        "PIP_NO_INDEX": "1",
        "PIP_FIND_LINKS": str(wheelhouse),
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
    }


class BootstrapInstallTest(unittest.TestCase):
    """Each case runs the action's actual step against a local wheelhouse (pip's own settings)."""

    def run_step(self, *, omit_file=False, drop=None, **wheel_options):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            wheelhouse = root / "wheelhouse"
            wheelhouse.mkdir()
            lines = requirement_lines(wheelhouse, **wheel_options)
            (root / "container").mkdir()
            if not omit_file:
                kept = [line for line in lines if not drop or not line.startswith(drop)]
                (root / "container/pdm-bootstrap-requirements.txt").write_text("".join(kept))
            environment = step_environment(root, wheelhouse)
            result = self.bash(step(INSTALL)["run"], environment)
            published = Path(environment["GITHUB_PATH"]).read_text().splitlines()
            home = root / "temp/pdm-tool"
            guard = self.guard(home, published) if result.returncode == 0 else None
            return result, guard, published, (home / "bin/pdm").exists()

    def guard(self, home, published):
        environment = {"PDM_TOOL_HOME": str(home), "PATH": f"{published[0]}:/usr/bin:/bin"}
        return self.bash(step("Verify PDM tool environment")["run"], environment)

    def bash(self, script, environment):
        return subprocess.run(  # noqa: S603 - the actual repository-owned step in a temporary tree.
            ["/bin/bash", "-euo", "pipefail", "-c", script],
            env={"HOME": os.environ["HOME"]} | environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )

    def test_complete_hashed_closure_is_installed_published_and_passes_the_guard(self):
        result, guard, published, executable = self.run_step()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(published), 1)
        self.assertTrue(published[0].endswith("/pdm-tool/bin"))
        self.assertTrue(executable)
        self.assertEqual(guard.returncode, 0, guard.stderr)

    def test_failures_exit_nonzero_and_publish_no_executable_or_path(self):
        cases = {
            "missing requirements file": {"omit_file": True},
            "hash mismatch": {"tamper": True},
            "incomplete closure": {"pdm_requires": ("absent-package",)},
            "pdm pin absent": {"drop": "pdm=="},
        }
        for name, options in cases.items():
            with self.subTest(name):
                result, _, published, executable = self.run_step(**options)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(published, [])
                self.assertFalse(executable)
