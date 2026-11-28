"""GitHub Pages publishes the same run's validated documentation, and only from fresh main."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

WORKFLOW = yaml.safe_load(Path(".github/workflows/ci.yml").read_text())
JOBS = WORKFLOW["jobs"]
SHA = "a" * 40
OTHER = "b" * 40

PRODUCER_REFUSAL = {
    "name": "Require the documentation producer",
    "if": "needs.documentation.result != 'success'",
    "run": 'echo "::error::The documentation artifact producer did not succeed."\nexit 1\n',
}
PACKAGE_CHECK = """\
set -euo pipefail
jq -e --arg sha "$GITHUB_SHA" '.source_commit == $sha' tmp/pages-site/build.json > /dev/null
links="$(find tmp/pages-site -type l -print)"
if [ -n "$links" ]; then
  echo "::error::The documentation artifact contains symbolic links: $links"
  exit 1
fi
"""
FRESH = """\
set -euo pipefail
current="$(gh api "repos/$GITHUB_REPOSITORY/commits/main" --jq .sha)"
if ! [[ "$current" =~ ^[0-9a-f]{40}$ ]]; then
  echo "::error::The main branch lookup returned no commit SHA."
  exit 1
fi
if [ "$current" = "$GITHUB_SHA" ]; then
  echo "deploy=true" >> "$GITHUB_OUTPUT"
else
  reason="main has moved past $GITHUB_SHA to $current; this run does not deploy."
  echo "$reason"
  echo "$reason" >> "$GITHUB_STEP_SUMMARY"
  echo "deploy=false" >> "$GITHUB_OUTPUT"
fi
"""
PACKAGE = {
    "name": "pages package",
    "needs": ["documentation"],
    "if": "${{ !cancelled() }}",
    "runs-on": "ubuntu-24.04",
    "timeout-minutes": 10,
    "permissions": {"contents": "read"},
    "steps": [
        PRODUCER_REFUSAL,
        {
            "uses": "actions/download-artifact@9000827ccba6bdab643e8b6fd33ac0654aef8333",
            "with": {"name": "documentation-preview-${{ github.sha }}", "path": "tmp/pages-site"},
        },
        {
            "name": "Check the artifact's source and symbolic links",
            "shell": "bash",
            "run": PACKAGE_CHECK,
        },
        {
            "uses": "actions/upload-pages-artifact@fc324d3547104276b827a68afc52ff2a11cc49c9",
            "with": {"path": "tmp/pages-site"},
        },
    ],
}
DEPLOY_CONDITION = (
    "github.event_name == 'push' && github.ref == 'refs/heads/main' && "
    "needs.required.result == 'success' && needs.pages-package.result == 'success'"
)
DEPLOY = {
    "name": "pages deploy",
    "needs": ["pages-package", "required"],
    "if": DEPLOY_CONDITION,
    "runs-on": "ubuntu-24.04",
    "timeout-minutes": 15,
    "permissions": {"contents": "read", "pages": "write", "id-token": "write"},
    "environment": {"name": "github-pages", "url": "${{ steps.deployment.outputs.page_url }}"},
    "concurrency": {"group": "pages-main", "cancel-in-progress": False},
    "steps": [
        {
            "name": "Deploy only the current main",
            "id": "fresh",
            "env": {"GH_TOKEN": "${{ github.token }}"},
            "shell": "bash",
            "run": FRESH,
        },
        {
            "name": "Deploy the same-run site",
            "id": "deployment",
            "if": "steps.fresh.outputs.deploy == 'true'",
            "uses": "actions/deploy-pages@368f82528645a54fb793d4d04e342629a3f51346",
        },
        {
            "name": "Record the deployment",
            "if": "steps.fresh.outputs.deploy == 'true'",
            "env": {"PAGE_URL": "${{ steps.deployment.outputs.page_url }}"},
            "run": 'echo "Deployed $GITHUB_SHA to $PAGE_URL" >> "$GITHUB_STEP_SUMMARY"\n',
        },
    ],
}


def run(script, cwd, env):
    return subprocess.run(  # noqa: S603 - exact repository-owned shell contract
        ["/bin/bash", "-c", script],
        cwd=cwd,
        env={"PATH": os.environ["PATH"], **env},
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


class PagesWorkflowTest(unittest.TestCase):
    def test_packaging_consumes_only_the_same_runs_validated_documentation(self):
        self.assertEqual(JOBS.get("pages-package"), PACKAGE)

    def test_deployment_runs_only_for_fresh_main_after_every_ci_result(self):
        self.assertEqual(JOBS.get("pages-deploy"), DEPLOY)

    def test_only_deployment_holds_pages_and_token_authority(self):
        holders = {
            name
            for name, job in JOBS.items()
            if {"pages", "id-token"} & set(job.get("permissions", {}))
        }
        self.assertEqual(holders, {"pages-deploy"})

    def test_required_waits_for_packaging_but_not_for_deployment(self):
        self.assertEqual(set(JOBS["required"]["needs"]), set(JOBS) - {"required", "pages-deploy"})


class PackageCheckTest(unittest.TestCase):
    def site(self, root, build):
        site = Path(root) / "tmp/pages-site"
        site.mkdir(parents=True)
        (site / "index.html").write_text("<p>docs</p>\n")
        if build is not None:
            (site / "build.json").write_text(build)
        return site

    def test_same_commit_without_links_passes(self):
        with tempfile.TemporaryDirectory() as root:
            self.site(root, f'{{"source_commit": "{SHA}"}}')
            result = run(PACKAGE_CHECK, root, {"GITHUB_SHA": SHA})
        self.assertEqual((result.returncode, result.stdout), (0, ""), result.stderr)

    def test_other_missing_or_invalid_identity_fails(self):
        for build in (f'{{"source_commit": "{OTHER}"}}', None, "not json", "{}"):
            with self.subTest(build=build), tempfile.TemporaryDirectory() as root:
                self.site(root, build)
                result = run(PACKAGE_CHECK, root, {"GITHUB_SHA": SHA})
                self.assertNotEqual(result.returncode, 0, result.stdout)

    def test_a_symbolic_link_fails(self):
        with tempfile.TemporaryDirectory() as root:
            site = self.site(root, f'{{"source_commit": "{SHA}"}}')
            (site / "linked.html").symlink_to(site / "index.html")
            result = run(PACKAGE_CHECK, root, {"GITHUB_SHA": SHA})
        self.assertEqual(result.returncode, 1)
        self.assertIn("contains symbolic links", result.stdout)

    def test_a_failed_link_scan_fails(self):
        # A find that fails must fail the check, whatever the runner's file permissions.
        with tempfile.TemporaryDirectory() as root:
            self.site(root, f'{{"source_commit": "{SHA}"}}')
            stub = Path(root) / "bin"
            stub.mkdir()
            (stub / "find").write_text("#!/bin/sh\necho 'find: scan failed' >&2\nexit 1\n")
            (stub / "find").chmod(0o755)
            result = run(
                PACKAGE_CHECK,
                root,
                {"GITHUB_SHA": SHA, "PATH": f"{stub}:{os.environ['PATH']}"},
            )
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertNotIn("contains symbolic links", result.stdout)


def workflow_step(job, step_id):
    """The step's run text as the workflow holds it, not this module's expected constant."""
    return next(step["run"] for step in JOBS[job]["steps"] if step.get("id") == step_id)


class FreshMainTest(unittest.TestCase):
    def decide(self, gh_body):
        """Run the workflow's own decision step; return exit code, output and summary."""
        with tempfile.TemporaryDirectory() as root:
            stub = Path(root) / "bin"
            stub.mkdir()
            (stub / "gh").write_text("#!/bin/bash\n" + gh_body)
            (stub / "gh").chmod(0o755)
            output = Path(root) / "output"
            output.touch()
            summary = Path(root) / "summary"
            summary.touch()
            result = subprocess.run(  # noqa: S603 - exact repository-owned shell contract
                ["/bin/bash", "-c", workflow_step("pages-deploy", "fresh")],
                env={
                    "PATH": f"{stub}:{os.environ['PATH']}",
                    "GITHUB_SHA": SHA,
                    "GITHUB_REPOSITORY": "CBIIT/nci-sip-mcp",
                    "GITHUB_OUTPUT": str(output),
                    "GITHUB_STEP_SUMMARY": str(summary),
                },
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            return result.returncode, output.read_text(), summary.read_text()

    def test_current_main_deploys(self):
        self.assertEqual(self.decide(f"echo {SHA}\n"), (0, "deploy=true\n", ""))

    def test_moved_main_does_not_deploy_and_says_why_in_the_summary(self):
        reason = f"main has moved past {SHA} to {OTHER}; this run does not deploy.\n"
        self.assertEqual(self.decide(f"echo {OTHER}\n"), (0, "deploy=false\n", reason))

    def test_a_failed_or_malformed_lookup_fails_without_a_decision(self):
        for body in ("exit 2\n", "echo\n", "echo null\n", "echo abc\n", f"echo {SHA.upper()}\n"):
            with self.subTest(body=body):
                code, written, summary = self.decide(body)
                self.assertNotEqual(code, 0)
                self.assertEqual((written, summary), ("", ""))
