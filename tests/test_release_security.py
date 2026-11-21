"""Release publication requires exact CI evidence and attests the pushed identity."""

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import yaml

ROOT = Path(__file__).resolve().parents[1]
SHA = "a" * 40
PROVENANCE = "actions/attest-build-provenance@4d101475d8b20a2381f78447822ac1eab6504dd8"
SBOM = "actions/attest@1e69f48acb82d1966a394da916b4c1698aa569d6"


def release_workflow():
    return yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text())


def ci_run(**changes):
    return {
        "id": 2,
        "head_sha": SHA,
        "head_branch": "main",
        "event": "push",
        "path": ".github/workflows/ci.yml",
        "status": "completed",
        "conclusion": "success",
    } | changes


class ReleaseGuardTest(unittest.TestCase):
    def guard(self, runs, api_status=0, raw=False):
        self.assertTrue(
            (ROOT / "scripts/release_ci.py").is_file(),
            "Manual releases must have a fail-closed CI guard",
        )
        with TemporaryDirectory() as directory:
            executable = Path(directory) / "gh"
            executable.write_text(
                f"#!{sys.executable}\nimport os,sys\n"
                "from pathlib import Path\n"
                "Path(os.environ['ARGV_FILE']).write_text('\\n'.join(sys.argv[1:]))\n"
                "print(os.environ['FAKE_RUNS'])\n"
                "sys.exit(int(os.environ['API_STATUS']))\n"
            )
            executable.chmod(0o755)
            result = subprocess.run(  # noqa: S603 - fixed module and test SHA, no shell
                [sys.executable, "-m", "scripts.release_ci", SHA],
                cwd=ROOT,
                env=os.environ
                | {
                    "PATH": directory + os.pathsep + os.environ["PATH"],
                    "GITHUB_REPOSITORY": "CBIIT/nci-sip-mcp",
                    "FAKE_RUNS": json.dumps(runs if raw else {"workflow_runs": runs}),
                    "API_STATUS": str(api_status),
                    "ARGV_FILE": str(Path(directory) / "argv"),
                },
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            self.assertEqual(
                (Path(directory) / "argv").read_text(),
                "api\nrepos/CBIIT/nci-sip-mcp/actions/workflows/ci.yml/runs"
                f"?head_sha={SHA}&branch=main&event=push&per_page=100",
            )
            return result

    def test_exact_successful_main_push_ci_allows_release(self):
        result = self.guard([ci_run(id=1)])
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_unavailable_or_untrusted_ci_evidence_refuses_release(self):
        cases = [
            [],
            [ci_run(conclusion="failure")],
            [ci_run(conclusion="cancelled")],
            [ci_run(status="in_progress", conclusion=None)],
            [ci_run(head_sha="b" * 40)],
            [ci_run(head_branch="issue/390")],
            [ci_run(event="pull_request")],
            [ci_run(path=".github/workflows/audit.yml")],
            [ci_run(conclusion=None)],
            [ci_run(conclusion=1)],
            [ci_run(id=0)],
            [ci_run(id=-1)],
            [ci_run(id=True)],
            [ci_run(id="2")],
            [None],
        ]
        for runs in cases:
            with self.subTest(runs=runs):
                result = self.guard(runs)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn("CI", result.stderr)

    def test_empty_runs_have_a_specific_actionable_diagnostic(self):
        result = self.guard([])
        self.assertEqual(result.returncode, 1)
        self.assertIn("No CI run for the main commit", result.stderr)

    def test_malformed_api_body_is_a_controlled_refusal(self):
        for body in ([], None, {"workflow_runs": "bad"}):
            with self.subTest(body=body):
                result = self.guard(body, raw=True)
                self.assertEqual(result.returncode, 1)
                self.assertIn("Release refused", result.stderr)
                self.assertNotIn("Traceback", result.stderr)
                if isinstance(body, dict):
                    self.assertIn("No CI run for the main commit", result.stderr)

    def test_newest_unsuccessful_run_is_not_hidden_by_older_success(self):
        for runs in (
            [ci_run(id=1), ci_run(id=3, conclusion="failure")],
            [ci_run(id=3, conclusion="failure"), ci_run(id=1)],
        ):
            with self.subTest(runs=runs):
                result = self.guard(runs)
                self.assertEqual(result.returncode, 1, result.stderr)

    def test_api_failure_cannot_authorize_release(self):
        result = self.guard([ci_run()], api_status=1)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("CI", result.stderr)


class ReleaseAttestationContractTest(unittest.TestCase):
    def test_attestation_identity_and_sbom_follow_successful_publication(self):
        steps = release_workflow()["jobs"]["image"]["steps"]
        attestations = [step for step in steps if step.get("uses") in (PROVENANCE, SBOM)]
        identity = {
            "subject-name": "${{ steps.publish.outputs.subject-name }}",
            "subject-digest": "${{ steps.publish.outputs.subject-digest }}",
            "push-to-registry": True,
        }
        self.assertEqual(
            attestations,
            [
                {"uses": PROVENANCE, "with": identity},
                {"uses": SBOM, "with": identity | {"sbom-path": "tmp/image/sbom.cdx.json"}},
            ],
        )
        self.assertEqual(steps[-2:], attestations)
        publication = steps[-3]
        self.assertEqual(publication["id"], "publish")
        self.assertEqual(
            publication["run"], 'pdm run python -m scripts.image_publish "$RELEASE_TAG"'
        )

    def test_release_has_no_pr_entry_and_permissions_are_job_scoped(self):
        value = release_workflow()
        self.assertEqual(set(value[True]), {"workflow_dispatch", "workflow_run"})
        self.assertEqual(set(value[True]["workflow_dispatch"]["inputs"]), {"retry_image_tag"})
        self.assertEqual(value["permissions"], {})
        self.assertEqual(
            {name: job["permissions"] for name, job in value["jobs"].items()},
            {
                "release": {"contents": "write", "actions": "read"},
                "image": {
                    "contents": "write",
                    "packages": "write",
                    "id-token": "write",
                    "attestations": "write",
                },
                "local-installer": {"contents": "read"},
            },
        )

    def test_manual_guard_runs_before_release_tooling_and_has_no_shell_interpolation(self):
        steps = release_workflow()["jobs"]["release"]["steps"]
        guard = next(step for step in steps if step.get("id") == "guard")
        self.assertIn("python3 -m scripts.release_ci", guard["run"])
        self.assertIn('"$head"', guard["run"])
        self.assertEqual(
            guard["env"],
            {
                "VALIDATED_SHA": "${{ github.event.workflow_run.head_sha }}",
                "GH_TOKEN": "${{ secrets.GITHUB_TOKEN }}",
            },
        )
        self.assertLess(
            steps.index(guard), next(i for i, s in enumerate(steps) if s.get("id") == "release")
        )
