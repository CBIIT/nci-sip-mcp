"""Release execution stays locked, permission-bounded and independent of global Git identity."""

import os
import re
import shutil
import subprocess
import sys
import tomllib
import unittest
import venv
from pathlib import Path
from tempfile import TemporaryDirectory

import tomli_w
import yaml

ROOT = Path(__file__).resolve().parents[1]
RELEASE = ROOT / ".github/workflows/release.yml"
CLI = (
    "pdm run semantic-release -v version --no-commit --tag --push "
    "--no-changelog --vcs-release --skip-build"
)
INSTALL = (
    "pdm lock --check\n"
    "pdm sync --no-default --no-self -G release --without server --without index\n"
)


def workflow():
    return yaml.safe_load(RELEASE.read_text(encoding="utf-8"))


def git(root, *arguments, env=None):
    return subprocess.check_output(  # noqa: S603 - fixed Git operations in an owned repository.
        ["git", *arguments],  # noqa: S607 - Git is the tested developer boundary.
        cwd=root,
        env=env,
        text=True,
        stderr=subprocess.PIPE,
    ).strip()


GUARD_RUN = """"$PDM_TOOL_HOME/venv/bin/python" - <<'PYTHON'
import json
import os
from importlib.metadata import PackageNotFoundError, distributions, version
from pathlib import Path
import shutil
import sys

home = Path(os.environ["PDM_TOOL_HOME"])
selected = shutil.which("pdm")
if Path(sys.prefix).resolve() != (home / "venv").resolve():
    raise SystemExit("Unexpected PDM tool interpreter")
if selected is None or Path(selected).resolve() != (home / "bin/pdm").resolve():
    raise SystemExit("Unexpected PDM executable")
expected: dict[str, str] = {
    "pdm": "2.29.2",
    "hishel": "1.2.1",
    "unearth": "0.18.2",
    "virtualenv": "21.15.2",
}
for name, required in expected.items():
    try:
        installed = version(name)
    except PackageNotFoundError:
        raise SystemExit("Missing PDM tool distribution") from None
    if installed != required:
        raise SystemExit("Unsupported PDM tool distribution")
print(json.dumps(sorted((item.metadata["Name"], item.version) for item in distributions())))
PYTHON
"""
PIP_REPORT_RUN = r""""$PDM_TOOL_HOME/venv/bin/python" - <<'PYTHON'
import json
import os
import re
from pathlib import Path

versions = []
try:
    source = Path(os.environ["PDM_TOOL_PIP_LOG"]).open()
except FileNotFoundError:
    raise SystemExit("Missing bootstrap pip version evidence") from None
with source:
    for line in source:
        if " Using pip " not in line:
            continue
        match = re.search(
            r" Using pip ([0-9]+(?:\.[0-9]+)+) from .+ \(python [0-9]+\.[0-9]+\)\s*$",
            line,
        )
        if match is None:
            raise SystemExit("Unrecognized bootstrap pip version evidence")
        versions.append(match.group(1))
if len(versions) < 2:
    raise SystemExit("Incomplete bootstrap pip version evidence")
print(json.dumps(versions))
PYTHON
"""
GUARD_ENV = {"PDM_TOOL_HOME": "${{ runner.temp }}/pdm-tool"}


class ReleaseWorkflowTest(unittest.TestCase):
    def test_release_job_keeps_its_exact_security_and_output_contract(self):
        data = workflow()
        job = data["jobs"]["release"]
        self.assertEqual(data["permissions"], {})
        self.assertEqual(
            data["concurrency"], {"group": "release-main", "cancel-in-progress": False}
        )
        self.assertEqual(job["permissions"], {"contents": "write", "actions": "read"})
        self.assertEqual(
            job["outputs"],
            {
                "image_tag": "${{ steps.image.outputs.tag }}",
                "image_sha": "${{ steps.image.outputs.sha }}",
            },
        )
        self.assertEqual(
            job["if"],
            "github.event_name == 'workflow_dispatch' || "
            "(github.event.workflow_run.conclusion == 'success' &&\n "
            "github.event.workflow_run.event == 'push' &&\n "
            "github.event.workflow_run.head_branch == 'main')",
        )

    def test_release_steps_use_only_the_locked_cli(self):
        steps = workflow()["jobs"]["release"]["steps"]
        self.assertEqual(
            workflow()["jobs"]["image"]["steps"][0],
            {
                "uses": "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1",
                "with": {
                    "ref": "${{ needs.release.outputs.image_sha }}",
                    "fetch-depth": 0,
                    "persist-credentials": False,
                },
            },
        )
        self.assertEqual(
            steps[0],
            {
                "name": "Check out main",
                "uses": "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1",
                "with": {"ref": "main", "fetch-depth": 0, "persist-credentials": False},
            },
        )
        selected = [
            step
            for step in steps
            if step.get("name")
            in {"Set up release tooling", "Install locked release group", "Semantic release"}
        ]
        self.assertEqual(
            selected,
            [
                {
                    "name": "Set up release tooling",
                    "if": "steps.guard.outputs.skip == 'false'",
                    "uses": "$/.github/actions/setup",
                    "with": {"release-only": "true"},
                },
                {
                    "name": "Semantic release",
                    "id": "release",
                    "if": "steps.guard.outputs.skip == 'false'",
                    "env": {"GH_TOKEN": "${{ secrets.GITHUB_TOKEN }}"},
                    "run": CLI,
                },
            ],
        )
        self.assertEqual(
            [step["name"] for step in steps],
            [
                "Check out main",
                "Decide whether this commit is released",
                "Set up release tooling",
                "Semantic release",
                "Summary",
                "Select the exact released commit for image publication",
            ],
        )
        project = tomllib.loads((ROOT / "pyproject.toml").read_text())
        self.assertEqual(
            project["dependency-groups"]["release"], ["python-semantic-release==10.7.0"]
        )

    def test_setup_composite_isolates_release_installation_and_cache(self):
        setup = yaml.safe_load((ROOT / ".github/actions/setup/action.yml").read_text())
        self.assertEqual(
            setup["inputs"],
            {
                "release-only": {
                    "description": "Install only locked release tooling, without the project",
                    "default": "false",
                }
            },
        )
        self.assertEqual(
            setup["runs"],
            {
                "using": "composite",
                "steps": [
                    {
                        "uses": "pdm-project/setup-pdm@544d7237314ee09c256785bd360f6b30add38b37",
                        "env": {
                            "PDM_HOME": "${{ runner.temp }}/pdm-tool",
                            "PDM_NO_FROZEN_DEPS": "1",
                            "PDM_DEPS": "hishel==1.2.1,unearth==0.18.2,virtualenv==21.15.2",
                            "PIP_UPLOADED_PRIOR_TO": "P5D",
                            "PIP_LOG": "${{ runner.temp }}/pdm-bootstrap-pip.log",
                        },
                        "with": {
                            "python-version": "3.14",
                            "version": "2.29.2",
                            "cache": "${{ inputs.release-only != 'true' }}",
                            "cache-restore-exact-match": True,
                        },
                    },
                    {
                        "name": "Verify PDM tool environment",
                        "env": GUARD_ENV,
                        "run": GUARD_RUN,
                        "shell": "bash",
                    },
                    {
                        "name": "Record bootstrap pip versions",
                        "env": {
                            "PDM_TOOL_HOME": "${{ runner.temp }}/pdm-tool",
                            "PDM_TOOL_PIP_LOG": "${{ runner.temp }}/pdm-bootstrap-pip.log",
                        },
                        "shell": "bash",
                        "run": PIP_REPORT_RUN,
                    },
                    {
                        "if": "inputs.release-only != 'true'",
                        "run": "pdm install --check",
                        "shell": "bash",
                    },
                    {"if": "inputs.release-only == 'true'", "run": INSTALL, "shell": "bash"},
                ],
            },
        )

    def test_external_actions_are_approved_and_immutable(self):
        files = [*ROOT.glob(".github/workflows/*.yml"), *ROOT.glob(".github/actions/**/action.yml")]
        found = []
        for path in files:
            found.extend(re.findall(r"^\s*(?:- )?uses: (\S+)", path.read_text(), re.MULTILINE))
        self.assertTrue(found)
        for action in found:
            with self.subTest(action=action):
                if action.startswith(("./", "$/")):
                    continue
                if action.startswith(("ossf/", "github/")):
                    self.assertIn(
                        action,
                        {
                            "ossf/scorecard-action@fa7117db83c52d787ae5a89f350999701505d78e",
                            "github/codeql-action/upload-sarif@33d7b85da1a04171e211befe2c739303b9a4c066",
                        },
                    )
                    continue
                self.assertRegex(action, r"^(?:actions|pdm-project)/[^@]+@[a-f0-9]{40}$")


class LockedReleaseCliTest(unittest.TestCase):
    def test_release_sync_selects_no_project_or_unrelated_groups(self):
        setup = yaml.safe_load((ROOT / ".github/actions/setup/action.yml").read_text())
        command = setup["runs"]["steps"][-1]["run"].splitlines()[-1].split()
        with TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("pyproject.toml", "pdm.lock"):
                shutil.copyfile(ROOT / name, root / name)
            environment = root / ".venv"
            venv.EnvBuilder().create(environment)
            result = subprocess.run(  # noqa: S603 - repository-owned dry-run installation command.
                [*command, "--dry-run"],
                cwd=root,
                env=os.environ
                | {
                    "PDM_PYTHON": str(environment / "bin/python"),
                    "PDM_IGNORE_ACTIVE_VENV": "1",
                    "PDM_CHECK_UPDATE": "false",
                },
                capture_output=True,
                text=True,
                check=False,
                timeout=60,
            )
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, output)
        self.assertEqual(
            set(re.findall(r"(?m)^\s+- ([\w-]+) ", output)),
            {
                package["name"]
                for package in tomllib.loads((ROOT / "pdm.lock").read_text())["package"]
                if "release" in package["groups"]
            },
        )

    def test_titles_preserve_release_decisions_and_outputs(self):
        for title, tag, released in (
            ("fix: repair behavior", "v0.23.2", "true"),
            ("feat: add behavior", "v0.24.0", "true"),
            ("fix!: change the client contract", "v0.24.0", "true"),
            ("docs: clarify behavior", "v0.23.1", "false"),
        ):
            with self.subTest(title=title), TemporaryDirectory() as directory:
                self.assert_release(Path(directory), title, tag, released)

    def assert_release(self, root, title, tag, released):
        clean = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        environment = clean | {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
        git(root, "init", "-q", "-b", "main", env=environment)
        git(
            root,
            "remote",
            "add",
            "origin",
            "https://github.com/example/release-test.git",
            env=environment,
        )
        project = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["semantic_release"]
        config = tomli_w.dumps({"tool": {"semantic_release": project}})
        root.joinpath("pyproject.toml").write_text(config, encoding="utf-8")
        git(root, "add", "pyproject.toml", env=environment)
        identity = ("-c", "user.name=Setup role", "-c", "user.email=setup@example.invalid")
        git(root, *identity, "commit", "-qm", "chore: baseline", env=environment)
        git(root, "tag", "v0.23.1", env=environment)
        git(root, *identity, "commit", "--allow-empty", "-qm", title, env=environment)
        before = git(root, "rev-parse", "HEAD", env=environment)
        output = root / "output"
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "semantic_release",
                "version",
                "--no-commit",
                "--tag",
                "--no-push",
                "--no-changelog",
                "--no-vcs-release",
                "--skip-build",
            ],
            cwd=root,
            env=environment | {"GITHUB_OUTPUT": str(output), "GITHUB_ACTIONS": "true"},
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        values = output.read_text(encoding="utf-8")
        self.assertIn(f"released={released}", values)
        self.assertIn(f"tag={tag}", values)
        self.assertEqual(git(root, "rev-parse", "HEAD", env=environment), before)
        self.assertEqual(root.joinpath("pyproject.toml").read_text(), config)
        if released == "true":
            self.assertEqual(git(root, "rev-parse", f"{tag}^{{commit}}", env=environment), before)
