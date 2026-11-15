"""Scorecard publishes repository analysis without any pull-request entry point."""

import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
EXPECTED = yaml.safe_load("""
name: Scorecard
on:
  push:
    branches: [main]
  schedule:
    - cron: '37 6 * * 1'
  branch_protection_rule:
    types: [created, edited, deleted]
  workflow_dispatch:
permissions: read-all
concurrency:
  group: scorecard-${{ github.ref }}
  cancel-in-progress: false
jobs:
  analysis:
    name: Scorecard analysis
    runs-on: ubuntu-24.04
    timeout-minutes: 15
    permissions:
      contents: read
      actions: read
      checks: read
      issues: read
      pull-requests: read
      security-events: write
      id-token: write
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1
        with:
          persist-credentials: false
      - uses: ossf/scorecard-action@fa7117db83c52d787ae5a89f350999701505d78e
        with:
          results_file: results.sarif
          results_format: sarif
          publish_results: true
      - uses: actions/upload-artifact@cf430e030ddbb5b0abf93d22962f4752f3646cd9
        if: always()
        with:
          name: scorecard-sarif
          path: results.sarif
          retention-days: 5
          if-no-files-found: error
      - uses: github/codeql-action/upload-sarif@33d7b85da1a04171e211befe2c739303b9a4c066
        if: always()
        with:
          sarif_file: results.sarif
""")
BADGE = (
    "[![OpenSSF Scorecard](https://api.scorecard.dev/projects/github.com/CBIIT/nci-sip-mcp/badge)]"
    "(https://scorecard.dev/viewer/?uri=github.com/CBIIT/nci-sip-mcp)"
)


class ScorecardWorkflowTest(unittest.TestCase):
    def test_only_declared_jobs_can_write_attestations_or_security_results(self):
        actual = {}
        for path in (ROOT / ".github/workflows").glob("*.yml"):
            value = yaml.safe_load(path.read_text())
            for name, job in value["jobs"].items():
                permissions = job.get("permissions", value.get("permissions", {}))
                writes = privileged_writes(permissions)
                if writes:
                    actual[f"{path.stem}/{name}"] = writes
        self.assertEqual(
            actual,
            {
                "scorecard/analysis": {"security-events": "write", "id-token": "write"},
                "release/image": {"id-token": "write", "attestations": "write"},
            },
        )

    def test_exact_workflow_has_one_restricted_job_and_no_pr_trigger(self):
        path = ROOT / ".github/workflows/scorecard.yml"
        self.assertTrue(path.is_file(), "The Scorecard publication workflow must exist")
        self.assertEqual(yaml.safe_load(path.read_text()), EXPECTED)

    def test_badge_identifies_repository_security_not_acceptance(self):
        document = (ROOT / "README.md").read_text()
        self.assertIn(BADGE, document)
        self.assertIn(
            "Scorecard measures repository security practices, not acceptance outcomes.", document
        )


def privileged_writes(permissions):
    keys = ("security-events", "id-token", "attestations")
    if permissions == "write-all":
        return dict.fromkeys(keys, "write")
    if not isinstance(permissions, dict):
        return {}
    return {key: "write" for key in keys if permissions.get(key) == "write"}
