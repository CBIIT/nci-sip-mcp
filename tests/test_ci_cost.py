"""Workflow runner identities stay pinned; the per-PR CI keeps its events and Linux jobs.

The macOS walk lives in its own workflow; the jobs' dependencies and conditions are owned by
the tests of the image selection and the badge isolation.
"""

import unittest
from pathlib import Path

import yaml

CI = yaml.safe_load(Path(".github/workflows/ci.yml").read_text(encoding="utf-8"))


class CiCostTest(unittest.TestCase):
    def test_ci_starts_on_the_same_events(self):
        self.assertEqual(
            CI[True],
            {
                "push": {"branches": ["main"]},
                "pull_request": None,
                "workflow_dispatch": {
                    "inputs": {
                        "base_sha": {
                            "description": (
                                "Full ancestor commit for acceptance regression comparison"
                            ),
                            "required": True,
                            "type": "string",
                        }
                    }
                },
            },
        )

    def test_only_pull_request_runs_can_be_cancelled_by_newer_runs(self):
        self.assertEqual(
            CI["concurrency"],
            {
                "group": (
                    "ci-${{ github.event_name == 'pull_request' && github.ref || github.sha }}"
                ),
                "cancel-in-progress": "${{ github.event_name == 'pull_request' }}",
            },
        )

    def test_every_workflow_job_has_its_exact_runner(self):
        runners = {
            path.name: {
                name: job.get("runs-on")
                for name, job in yaml.safe_load(path.read_text(encoding="utf-8"))["jobs"].items()
            }
            for path in Path(".github/workflows").glob("*.y*ml")
        }
        self.assertEqual(
            runners,
            {
                "acceptance-live.yml": {"acceptance-live": "ubuntu-24.04"},
                "audit.yml": {
                    "pip-audit": "ubuntu-24.04",
                    "dependency-review": "ubuntu-24.04",
                },
                "branch-cleanup.yml": {"cleanup": "ubuntu-24.04"},
                "ci.yml": {
                    "required": "ubuntu-24.04",
                    "changes": "ubuntu-24.04",
                    "companions": "ubuntu-24.04",
                    "documentation": "ubuntu-24.04",
                    "pages-package": "ubuntu-24.04",
                    "pages-deploy": "ubuntu-24.04",
                    "image": "ubuntu-24.04",
                    "quality": "ubuntu-24.04",
                    "test": "ubuntu-24.04",
                    "unit-coverage": "ubuntu-24.04",
                    "acceptance": "ubuntu-24.04",
                    "acceptance-http": "ubuntu-24.04",
                    "selftest": "ubuntu-24.04",
                    "coverage": "ubuntu-24.04",
                    "coverage-badges-render": "ubuntu-24.04",
                    "coverage-badges": "ubuntu-24.04",
                    "mutation": "ubuntu-24.04",
                },
                "local-run.yml": {
                    "prepare-containers": "ubuntu-24.04",
                    "local-run": "${{ matrix.os }}",
                },
                "local-installer.yml": {
                    "prepare": "ubuntu-24.04",
                    "local-installer": "${{ matrix.os }}",
                },
                "pr-title.yml": {"conventional": "ubuntu-24.04"},
                "scorecard.yml": {"analysis": "ubuntu-24.04"},
                "release-notes-preview.yml": {"preview": "ubuntu-24.04"},
                "release.yml": {
                    "release": "ubuntu-24.04",
                    "image": "ubuntu-24.04",
                    "local-installer": None,
                    "publish": "ubuntu-24.04",
                },
            },
        )
