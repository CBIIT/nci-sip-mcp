"""The pull request title check accepts what the release configuration can read."""

import os
import re
import subprocess
import tempfile
import tomllib
import unittest
from copy import deepcopy
from pathlib import Path

import yaml

ROOT = Path(__file__).parent.parent
WORKFLOW = yaml.safe_load((ROOT / ".github" / "workflows" / "pr-title.yml").read_text("utf-8"))
# The shell script of the workflow's only step.
[SCRIPT] = [step["run"] for step in WORKFLOW["jobs"]["conventional"]["steps"]]


class PullRequestTitleTest(unittest.TestCase):
    def check(self, title):
        """Run the title check as the workflow does; return its exit code and its summary."""

        with tempfile.NamedTemporaryFile() as summary:
            # The script comes from this repository, and bash is found on the PATH as on the runner.
            process = subprocess.run(  # noqa: S603
                ["bash", "-c", SCRIPT],  # noqa: S607
                env={
                    "PATH": os.environ["PATH"],
                    "TITLE": title,
                    "GITHUB_STEP_SUMMARY": summary.name,
                },
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            return process.returncode, Path(summary.name).read_text(encoding="utf-8")

    def test_title_check_accepts_exactly_the_types_the_release_parser_reads(self):
        with (ROOT / "pyproject.toml").open("rb") as file:
            options = tomllib.load(file)["tool"]["semantic_release"]["commit_parser_options"]

        accepted = re.search(r"pattern='\^\(([a-z|]+)\)", SCRIPT).group(1).split("|")

        self.assertCountEqual(accepted, options["allowed_tags"])
        self.assertLessEqual(
            set(options["minor_tags"] + options["patch_tags"]), set(options["allowed_tags"])
        )

    def test_conventional_titles_pass(self):
        for title in (
            "feat: add a tool",
            "fix(index): reject a vector of another dimension",
            "feat(server)!: rename the lookup tool",
            "build!: require Python 3.13",
            "security!: require a token",
        ):
            with self.subTest(title):
                self.assertEqual(self.check(title), (0, ""))

    def test_title_workflow_preserves_validation_structure_and_exact_regex(self):
        """Given a PR event, validation keeps its exact authorization and execution contract."""
        workflow = deepcopy(WORKFLOW)
        workflow["jobs"]["conventional"]["steps"][0]["run"] = "<Bash checked at its entry point>"
        self.assertEqual(
            workflow,
            {
                "name": "PR title",
                "run-name": "Validate PR title: ${{ github.event.pull_request.title }}",
                True: {
                    "pull_request": {
                        "types": ["opened", "edited", "reopened", "synchronize"],
                        "branches": ["main"],
                    }
                },
                "permissions": {},
                "concurrency": {
                    "group": "pr-title-${{ github.event.pull_request.number }}",
                    "cancel-in-progress": True,
                },
                "jobs": {
                    "conventional": {
                        "name": "conventional commit subject",
                        "runs-on": "ubuntu-24.04",
                        "timeout-minutes": 5,
                        "steps": [
                            {
                                "name": "Validate",
                                "env": {"TITLE": "${{ github.event.pull_request.title }}"},
                                "run": "<Bash checked at its entry point>",
                            }
                        ],
                    }
                },
            },
        )
        [pattern] = re.findall(r"^pattern='([^']+)'$", SCRIPT, re.MULTILINE)
        self.assertEqual(
            pattern,
            r"^(feat|fix|perf|docs|style|refactor|test|chore|build|ci|revert|security|deprecate)"
            r"(\([a-z0-9._/-]+\))?!?: .+",
        )

    def test_other_titles_fail_with_an_explanation(self):
        """Given an invalid title, Bash rejects it and explains both release landing methods."""
        self.maxDiff = None
        for title in (
            "Fix something",
            "feature: add a tool",
            "fix:missing space",
            "fix: ",
            "Fix: wrong case",
            "fix(Index): upper-case scope",
            "wip fix: not at the start",
        ):
            with self.subTest(title):
                code, summary = self.check(title)

                self.assertEqual(code, 1)
                self.assertEqual(
                    summary,
                    "## The PR title is not a Conventional Commit\n"
                    "\n"
                    "```\n"
                    f"{title}\n"
                    "```\n"
                    "\n"
                    "Expected `type(scope)?!?: subject`, for example:\n"
                    "\n"
                    "- `feat(traversal): follow role edges in both directions`\n"
                    "- `fix(index): reject a vector of another dimension`\n"
                    "- `docs: describe the release pinning`\n"
                    "- `feat(server)!: rename the lookup tool`\n"
                    "\n"
                    "Merge releases follow included commit messages, including bodies; squash\n"
                    'releases follow the title. CONTRIBUTING.md, "Pull requests and releases",\n'
                    "lists the types and what each releases.\n",
                )


class PackageDependenciesTest(unittest.TestCase):
    def test_the_core_package_has_no_dependencies_and_exposes_both_commands(self):
        with (ROOT / "pyproject.toml").open("rb") as file:
            project = tomllib.load(file)["project"]

        self.assertEqual(project["dependencies"], [])
        self.assertEqual(
            project["scripts"],
            {
                "nci-sip-install": "nci_sip_mcp.installer:main",
                "nci-sip-mcp": "nci_sip_mcp.cli:main",
            },
        )


if __name__ == "__main__":
    unittest.main()
