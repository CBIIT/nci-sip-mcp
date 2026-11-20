"""Postmerge-only work has an exact PR verifier or a bounded documented exemption."""

# ruff: noqa: E501 - exact workflow conditions and documentation rows are literal contracts.

import copy
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
CONDITIONS = {
    None: True,
    "always()": True,
    "github.event_name == 'pull_request'": True,
    "github.event_name == 'pull_request' && github.base_ref == 'main'": True,
    "${{ !cancelled() }}": True,
    "github.event_name != 'pull_request' || needs.changes.outputs.image == 'true'": True,
    "${{ !cancelled() && (github.event_name != 'pull_request' || needs.changes.outputs.companions == 'true') }}": True,
    "github.event_name == 'push' && github.ref == 'refs/heads/main'": False,
    "needs.release.outputs.image_tag != ''": True,
    "github.event_name == 'workflow_dispatch' || (github.event.workflow_run.conclusion == 'success' &&\n github.event.workflow_run.event == 'push' &&\n github.event.workflow_run.head_branch == 'main')": False,
    "github.event.pull_request.merged == true && startsWith(github.event.pull_request.head.ref, 'milestone/') && github.event.pull_request.head.repo.full_name == github.repository": False,
}
ROWS = {
    "scorecard/analysis": "ci/test exact offline workflow contract; first run after the milestone merge, reported in step 9.",
    "ci/coverage-badges": "ci/coverage-badges-render; privileged mutation: publish rendered badge data only on main.",
    "branch-cleanup/cleanup": "ci/test; privileged mutation: delete merged branches only after merge.",
    "release/release": "ci/test and pr-title/conventional; privileged mutation: create real tags and releases only after validated main.",
    "release/image": "ci/image uses the same image action; privileged mutation: registry and release publication; post-publication digest availability requires the published digest.",
    "release/local-installer": "local-installer/prepare and local-installer/local-installer use the same reusable verification; actual release tag identity is checked after tagging.",
    "acceptance-live/acceptance-live": "Live drift exemption: measures real services, not fixture behavior; operationally disabled, no PR live run.",
}


def workflows():
    return {
        path.stem: yaml.safe_load(path.read_text())
        for path in (ROOT / ".github/workflows").glob("*.y*ml")
    }


def reachable(workflow, name, definitions, visiting=()):
    if name in visiting:
        raise ValueError("Cyclic workflow dependencies")
    job = workflow["jobs"][name]
    condition = job.get("if")
    allowed = CONDITIONS[condition]
    needs = job.get("needs", [])
    needs = [needs] if isinstance(needs, str) else needs
    dependencies = all(
        reachable(workflow, parent, definitions, (*visiting, name)) for parent in needs
    )
    return (
        allowed and (condition == "always()" or dependencies) and check_reusable(job, definitions)
    )


def check_reusable(job, definitions):
    if "uses" in job:
        target = Path(job["uses"]).stem
        called = definitions[target]
        if "workflow_call" not in called[True]:
            raise ValueError("Reusable workflow lacks workflow_call")
        return all(reachable(called, name, definitions) for name in called["jobs"])
    return True


def postmerge(definitions):
    found = set()
    for workflow, value in definitions.items():
        events = value[True]
        pr = events.get("pull_request", False)
        has_pr = "pull_request" in events and not (
            isinstance(pr, dict) and pr.get("types") == ["closed"]
        )
        for name in value["jobs"]:
            job_pr = reachable(value, name, definitions)
            if not (has_pr and job_pr):
                found.add(f"{workflow}/{name}")
    return found


class WorkflowTwinsTest(unittest.TestCase):
    def test_derived_postmerge_jobs_equal_the_documented_twin_or_exemption_table(self):
        self.assertEqual(postmerge(workflows()), set(ROWS))
        document = (ROOT / "CONTRIBUTING.md").read_text()
        table = document.partition("### Verification before merge\n")[2].split("\n## ", 1)[0]
        actual = {
            cells[0].strip().strip("`"): cells[1].strip()
            for line in table.splitlines()
            if line.startswith("| `")
            for cells in [line.split("|")[1:3]]
        }
        self.assertEqual(actual, ROWS)
        self.assertIn(
            "No verification runs only after merge: provide a pull-request twin or an explicit "
            "tested exemption; publishing remains post-merge.",
            (ROOT / "AGENTS.md").read_text(),
        )

    def test_unknown_conditions_and_unclassified_postmerge_jobs_cannot_pass(self):
        definitions = workflows()
        unknown = copy.deepcopy(definitions)
        unknown["ci"]["jobs"]["test"]["if"] = "new condition"
        with self.assertRaises(KeyError):
            postmerge(unknown)
        added = copy.deepcopy(definitions)
        added["release"]["jobs"]["unclassified"] = {"runs-on": "ubuntu-24.04"}
        self.assertEqual(postmerge(added) - set(ROWS), {"release/unclassified"})

    def test_release_verifiers_share_the_exact_read_only_pr_interfaces(self):
        definitions = workflows()
        release = definitions["release"]["jobs"]
        self.assertEqual(
            [
                step
                for step in release["image"]["steps"]
                if step.get("uses") == "$/.github/actions/image"
            ],
            [{"uses": "$/.github/actions/image"}],
        )
        self.assertEqual(
            [
                step
                for step in definitions["ci"]["jobs"]["image"]["steps"]
                if step.get("uses") == "$/.github/actions/image"
            ],
            [{"uses": "$/.github/actions/image"}],
        )
        self.assertEqual(
            release["local-installer"]["uses"], "$/.github/workflows/local-installer.yml"
        )
        for workflow in ("ci", "local-installer", "local-run"):
            self.assertEqual(definitions[workflow]["permissions"], {"contents": "read"})
        self.assertEqual(
            definitions["local-installer"][True].get("workflow_dispatch", "absent"), None
        )
