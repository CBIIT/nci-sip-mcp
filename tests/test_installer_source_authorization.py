"""The workflow selects an authorized commit and keeps it across later tag moves."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).parent.parent
WORKFLOW = ROOT / ".github/workflows/local-installer.yml"
PROJECT_METADATA = '[project]\nname = "nci-sip-mcp"\nversion = "0.0.0"\n'


class InstallerSourceAuthorizationTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.origin = self.root / "origin"
        self.checkout = self.root / "checkout"
        self.output = self.root / "outputs"
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.commits = 0
        self.env = {
            **os.environ,
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_ALLOW_PROTOCOL": "file",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_AUTHOR_NAME": "Test role",
            "GIT_AUTHOR_EMAIL": "test@example.invalid",
            "GIT_COMMITTER_NAME": "Test role",
            "GIT_COMMITTER_EMAIL": "test@example.invalid",
            "PATH": str(self.bin) + os.pathsep + os.environ["PATH"],
        }
        self.git(self.root, "init", "-q", "-b", "main", str(self.origin))
        (self.origin / "pyproject.toml").write_text(PROJECT_METADATA, encoding="utf-8")
        self.git(self.origin, "add", "pyproject.toml")
        self.approved = self.commit(self.origin)
        self.git(self.origin, "tag", "v1.2.3")
        self.git(self.root, "clone", "-q", str(self.origin), str(self.checkout))
        gh = self.bin / "gh"
        gh.write_text(
            "#!/bin/sh\n"
            'test "$*" = "release view --json tagName --jq .tagName" || exit 2\n'
            'printf "%s\\n" "$TEST_RELEASE"\n',
            encoding="utf-8",
        )
        gh.chmod(0o700)
        self.workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
        self.selector = self.workflow["jobs"]["prepare"]["steps"][-1]["run"]

    def git(self, directory, *args, stdin=None):
        return subprocess.run(  # noqa: S603 - Git operates only on owned local fixtures.
            ["git", "-C", str(directory), *args],  # noqa: S607 - Use the developer's Git executable.
            input=stdin,
            env=self.env,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

    def commit(self, directory):
        self.commits += 1
        self.git(directory, "commit", "-q", "--allow-empty", "-m", f"fixture {self.commits}")
        return self.git(directory, "rev-parse", "HEAD")

    def select(self, tag="v1.2.3", **overrides):
        self.output.write_text("", encoding="utf-8")
        env = {
            **self.env,
            "EVENT_NAME": "schedule",
            "INPUT_REF": "",
            "INPUT_TAG": "",
            "PR_REF": "",
            "DISPATCH_REF": "",
            "TEST_RELEASE": tag,
            "GITHUB_OUTPUT": str(self.output),
            **overrides,
        }
        result = subprocess.run(  # noqa: S603 - Execute the tracked selector with local doubles.
            ["bash", "-c", self.selector],  # noqa: S607 - The workflow explicitly runs Bash.
            cwd=self.checkout,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        outputs = dict(line.split("=", 1) for line in self.output.read_text().splitlines())
        return result.returncode, outputs

    def unmerged(self, *, orphan=False):
        option = "--orphan" if orphan else "-c"
        branch = "unrelated" if orphan else "unmerged"
        self.git(self.origin, "switch", option, branch)
        if orphan:
            (self.origin / "pyproject.toml").write_text(PROJECT_METADATA, encoding="utf-8")
            self.git(self.origin, "add", "pyproject.toml")
        commit = self.commit(self.origin)
        tag = "v1.2.5" if orphan else "v1.2.4"
        self.git(self.origin, "tag", tag)
        self.git(self.origin, "switch", "main")
        self.git(self.checkout, "fetch", "origin", "--tags")
        return commit, tag

    def test_scheduled_release_selects_the_exact_main_history_commit(self):
        self.assertEqual(self.select(), (0, {"ref": self.approved, "tag": "v1.2.3"}))

    def test_scheduled_release_rejects_an_unmerged_commit(self):
        _, tag = self.unmerged()
        self.assertEqual(self.select(tag), (1, {}))

    def test_scheduled_release_rejects_an_unrelated_orphan_with_valid_project_metadata(self):
        commit, tag = self.unmerged(orphan=True)
        project = self.git(self.origin, "show", f"{commit}:pyproject.toml")
        self.assertEqual(project, PROJECT_METADATA.rstrip())
        self.assertEqual(self.select(tag), (1, {}))

    def test_scheduled_release_rejects_invalid_missing_and_noncommit_tags(self):
        blob = self.git(self.origin, "hash-object", "-w", "--stdin", stdin="not a commit")
        self.git(self.origin, "update-ref", "refs/tags/v1.2.9", blob)
        # A real non-version tag must be rejected, not merely an absent tag lookup.
        self.git(self.origin, "tag", "main", self.approved)
        self.git(self.checkout, "fetch", "origin", "--tags")
        for tag, exit_code in (
            ("main", 1),
            ("v1.2.3^{commit}", 1),
            ("v9.9.9", 128),
            ("v1.2.9", 128),
        ):
            with self.subTest(tag=tag):
                self.assertEqual(self.select(tag), (exit_code, {}))

    def test_failed_main_refresh_does_not_use_a_previously_valid_tracking_ref(self):
        self.git(self.checkout, "remote", "set-url", "origin", str(self.root / "absent"))
        self.assertEqual(self.select(), (128, {}))

    def test_main_refresh_accepts_an_already_fetched_release_after_main_advances(self):
        selected, tag = self.unmerged()
        self.git(self.origin, "merge", "--ff-only", "unmerged")
        self.assertEqual(
            self.git(self.checkout, "rev-parse", "refs/remotes/origin/main"), self.approved
        )
        self.assertEqual(self.select(tag), (0, {"ref": selected, "tag": tag}))

    def test_local_branches_and_tags_cannot_impersonate_the_protected_main_ref(self):
        selected, tag = self.unmerged()
        self.git(self.checkout, "checkout", "--detach", self.approved)
        self.git(self.checkout, "branch", "-f", "main", selected)
        self.git(self.checkout, "tag", "main", selected)
        self.git(self.checkout, "tag", "origin/main", selected)
        self.assertEqual(self.select(tag), (1, {}))

    def test_pr_and_manual_selection_do_not_require_a_released_main_commit(self):
        selected, _ = self.unmerged()
        self.git(self.checkout, "remote", "set-url", "origin", str(self.root / "absent"))
        for event, pr_ref in (("pull_request", selected), ("workflow_dispatch", "")):
            with self.subTest(event=event):
                result = self.select(EVENT_NAME=event, PR_REF=pr_ref, DISPATCH_REF=selected)
                self.assertEqual(result, (0, {"ref": selected, "tag": "v0.0.0"}))

    def test_reusable_call_requires_its_exact_tag_and_commit_pair(self):
        matching = self.select(
            EVENT_NAME="workflow_call", INPUT_REF=self.approved, INPUT_TAG="v1.2.3"
        )
        self.assertEqual(matching, (0, {"ref": self.approved, "tag": "v1.2.3"}))
        other = self.commit(self.checkout)
        mismatched = self.select(EVENT_NAME="workflow_call", INPUT_REF=other, INPUT_TAG="v1.2.3")
        self.assertEqual(mismatched, (1, {}))

    def bootstrap_python(self):
        venv = self.root / "bootstrap"
        (venv / "bin").mkdir(parents=True)
        python = venv / "bin/python"
        python.write_text(
            f"#!{sys.executable}\n"
            "import json, os, subprocess, sys\n"
            "ref = sys.argv[-1].rsplit('@', 1)[1]\n"
            "resolved = subprocess.check_output(\n"
            "    ['git', '-C', os.environ['TEST_ORIGIN'], 'rev-parse', ref + '^{commit}'],\n"
            "    text=True).strip()\n"
            "print(json.dumps({'arguments': sys.argv[1:], 'resolved': resolved}))\n",
            encoding="utf-8",
        )
        python.chmod(0o700)
        return venv

    def bootstrap(self, step, selected, venv):
        values = {
            "${{ needs.prepare.outputs.ref }}": selected["ref"],
            "${{ needs.prepare.outputs.tag }}": selected["tag"],
        }
        bindings = {**self.workflow["jobs"]["local-installer"]["env"], **step.get("env", {})}
        env = {
            **self.env,
            "INSTALLER_VENV": str(venv),
            "NEGATIVE_INSTALLER_VENV": str(venv),
            "TEST_ORIGIN": str(self.origin),
        }
        for key in ("INSTALL_REF", "RELEASE_TAG"):
            if key in bindings:
                env[key] = values.get(bindings[key], bindings[key])
        command = next(line for line in step["run"].splitlines() if '" -m pip install ' in line)
        return subprocess.run(  # noqa: S603 - Tracked pip command, owned fake executable/local Git.
            ["bash", "-c", "set -euo pipefail\n" + command],  # noqa: S607 - Workflow Bash boundary.
            cwd=self.checkout,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_both_bootstrap_fetches_keep_the_selected_commit_when_the_release_tag_moves(self):
        exit_code, selected = self.select()
        self.assertEqual(exit_code, 0)
        other, _ = self.unmerged()
        self.git(self.origin, "tag", "-f", "v1.2.3", other)
        self.assertNotEqual(self.git(self.origin, "rev-parse", "refs/tags/v1.2.3"), selected["ref"])
        venv = self.bootstrap_python()
        steps = {
            step.get("name"): step for step in self.workflow["jobs"]["local-installer"]["steps"]
        }
        for name in (
            "Install the selected installer source",
            "Verify stdio and HTTP installer lifecycles",
        ):
            with self.subTest(step=name):
                result = self.bootstrap(steps[name], selected, venv)
                self.assertEqual(result.returncode, 0, result.stderr)
                record = json.loads(result.stdout)
                self.assertEqual(record["resolved"], selected["ref"])
                self.assertEqual(
                    record["arguments"],
                    [
                        "-m",
                        "pip",
                        "install",
                        f"nci-sip-mcp @ git+https://github.com/CBIIT/nci-sip-mcp@{self.approved}",
                    ],
                )
