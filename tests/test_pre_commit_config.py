"""Local hooks and CI run the exact gates assigned to their boundaries."""

import os
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import yaml

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / ".pre-commit-config.yaml"
CI = ROOT / ".github" / "workflows" / "ci.yml"


def configuration():
    return yaml.safe_load(CONFIG.read_text())


def hooks():
    return [hook for repository in configuration()["repos"] for hook in repository["hooks"]]


def install_hooks(repository, environment=None):
    environment = environment or os.environ
    clean = {name: value for name, value in environment.items() if not name.startswith("GIT_")}
    subprocess.run(
        ["git", "init", "--quiet"],  # noqa: S607 - Git is the developer boundary.
        cwd=repository,
        env=clean,
        check=True,
    )
    subprocess.run(  # noqa: S603 - all arguments are fixed or repository-owned paths.
        [sys.executable, "-m", "pre_commit", "install", "--config", str(CONFIG)],
        cwd=repository,
        env=clean,
        check=True,
        capture_output=True,
        text=True,
    )


class RuffHooksTest(unittest.TestCase):
    def test_both_ruff_hooks_run_without_the_cache(self):
        entries = {
            hook["id"]: hook["entry"]
            for hook in hooks()
            if hook["id"] in {"ruff-check", "ruff-format"}
        }
        self.assertEqual(
            entries,
            {
                "ruff-format": "pdm run ruff format --no-cache --force-exclude",
                "ruff-check": (
                    "pdm run ruff check --no-cache --fix --exit-non-zero-on-fix --force-exclude"
                ),
            },
        )


class PrePushTestHookTest(unittest.TestCase):
    def test_installation_includes_exactly_commit_and_push_hooks(self):
        self.assertEqual(configuration()["default_install_hook_types"], ["pre-commit", "pre-push"])

    def test_push_runs_the_whole_suite_with_exact_configuration(self):
        (hook,) = [hook for hook in hooks() if hook["id"] == "full-test-suite"]

        self.assertEqual(
            hook,
            {
                "id": "full-test-suite",
                "name": "whole test suite before push",
                "entry": "pdm run test",
                "language": "system",
                "stages": ["pre-push"],
                "pass_filenames": False,
                "always_run": True,
            },
        )

    def test_only_the_full_suite_hook_runs_before_push(self):
        effective = {hook["id"]: hook["stages"] for hook in hooks()}

        self.assertEqual(effective.pop("full-test-suite"), ["pre-push"])
        self.assertEqual(set(map(tuple, effective.values())), {("pre-commit",)})

    def test_commit_stage_configuration_preserves_every_local_gate(self):
        self.assertEqual(configuration()["default_stages"], ["pre-commit"])
        self.assertEqual(configuration()["exclude"], "^tmp/")
        self.assertEqual(
            next(hook for hook in hooks() if hook["id"] == "zizmor"),
            {
                "id": "zizmor",
                "args": ["--offline"],
                "stages": ["pre-commit"],
            },
        )
        local = [
            hook
            for repo in configuration()["repos"]
            if repo["repo"] == "local"
            for hook in repo["hooks"]
            if hook["id"] != "full-test-suite"
        ]
        common = {"language": "system", "stages": ["pre-commit"]}
        self.assertEqual(
            local,
            [
                common
                | {
                    "id": "actionlint",
                    "name": "actionlint",
                    "entry": "pdm run actionlint",
                    "files": r"^\.github/(workflows/.*\.ya?ml|actionlint\.yaml)$",
                    "pass_filenames": False,
                },
                common
                | {
                    "id": "ruff-format",
                    "name": "ruff format",
                    "entry": "pdm run ruff format --no-cache --force-exclude",
                    "types_or": ["python", "pyi"],
                },
                common
                | {
                    "id": "ruff-check",
                    "name": "ruff check",
                    "entry": "pdm run ruff check --no-cache --fix "
                    "--exit-non-zero-on-fix --force-exclude",
                    "types_or": ["python", "pyi"],
                },
                common
                | {
                    "id": "basedpyright",
                    "name": "basedpyright",
                    "entry": "pdm run basedpyright",
                    "pass_filenames": False,
                    "always_run": True,
                },
                common
                | {
                    "id": "upstream-requirements",
                    "name": "upstream requirements packages are current",
                    "entry": "pdm run python scripts/upstream_requirements.py --check",
                    "pass_filenames": False,
                    "always_run": True,
                },
                common
                | {
                    "id": "installer-requirements",
                    "name": "installer requirements are current",
                    "entry": "pdm export -G server -G build --no-default "
                    "-o src/nci_sip_mcp/server-requirements.txt",
                    "pass_filenames": False,
                    "files": r"^(pyproject\.toml|pdm\.lock|"
                    r"src/nci_sip_mcp/server-requirements\.txt)$",
                },
                common
                | {
                    "id": "complexity",
                    "name": "cyclomatic complexity below 8",
                    "entry": "pdm run python scripts/validation/check_complexity.py",
                    "types": ["python"],
                    "pass_filenames": False,
                },
                common
                | {
                    "id": "test-quality",
                    "name": "tests that can catch a regression",
                    "entry": "pdm run python scripts/validation/check_test_quality.py",
                    "files": "^(tests|acceptance/(tests|selftests))/test_.*\\.py$",
                },
                common
                | {
                    "id": "dead-code",
                    "name": "dead code",
                    "entry": "pdm run vulture",
                    "types": ["python"],
                    "pass_filenames": False,
                },
            ],
        )

    def test_real_install_creates_exactly_commit_and_push_hooks(self):
        with TemporaryDirectory() as directory:
            repository = Path(directory)
            install_hooks(repository)
            installed = sorted(
                path.name
                for path in (repository / ".git" / "hooks").iterdir()
                if not path.name.endswith(".sample")
            )

        self.assertEqual(installed, ["pre-commit", "pre-push"])

    def test_real_install_cannot_modify_an_inherited_git_directory(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            decoy = root / "decoy"
            target = root / "target"
            decoy.mkdir()
            target.mkdir()
            install_hooks(decoy)
            config = decoy / ".git" / "config"
            hooks = decoy / ".git" / "hooks"
            before = config.read_bytes(), sorted(path.name for path in hooks.iterdir())
            poisoned = os.environ | {"GIT_DIR": str(decoy / ".git")}

            install_hooks(target, poisoned)

            self.assertEqual(
                (config.read_bytes(), sorted(path.name for path in hooks.iterdir())), before
            )

    def test_ci_runs_only_the_default_commit_stage(self):
        workflow = yaml.safe_load(CI.read_text())
        commands = [step["run"] for step in workflow["jobs"]["quality"]["steps"] if "run" in step]

        self.assertEqual(
            commands,
            ["pdm run pre-commit run --all-files --show-diff-on-failure"],
        )
