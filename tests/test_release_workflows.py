"""The release and preview workflows keep their exact release-notes contract."""

import unittest
from pathlib import Path

import yaml

WORKFLOWS = Path(__file__).parent.parent / ".github" / "workflows"
CHECKOUT = "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1"
PREVIEW_SCRIPT = """\
set -euo pipefail
base="${BASE_SHA:-$(git rev-parse refs/remotes/origin/main)}"
head="$(git rev-parse HEAD)"
pdm run python -m scripts.release_notes preview --base "$base" --head "$head" \\
  --repository-url "https://github.com/$REPOSITORY.git" | tee -a "$GITHUB_STEP_SUMMARY"
"""
FAILURE_LINE = (
    'echo "The release step did not succeed; read the log. If the tag was not pushed, start"'
)
GROUP = "release-notes-preview-${{ github.event.pull_request.number || github.ref }}"
HEAD_REF = "${{ github.event.pull_request.head.sha || github.sha }}"
RECOVERY_LINES = [
    'echo "check whether its draft exists (\\`gh release view <tag>\\`); a failed request may"',
    'echo "still have created it. If none exists, check the tag out in a clean clone and run"',
    'echo "\\`pdm run python -m scripts.release_notes recover <tag> --repository <owner/repo>\\`,"',
    'echo "then dispatch Release with retry_image_tag=<tag> while the tag is main\'s tip."',
]
TAGGED_LINE = (
    'echo "Tagged **$TAG**; its GitHub release stays a draft until the publish release job'
    ' succeeds."'
)


def load(name):
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


class ReleaseWorkflowTest(unittest.TestCase):
    def test_notes_are_prepared_before_the_tag_with_no_token_and_recovery_names_the_command(self):
        steps = load("release.yml")["jobs"]["release"]["steps"]
        names = [step["name"] for step in steps]
        prepare = [step for step in steps if step["name"] == "Prepare release notes"]
        [summary] = [step for step in steps if step["name"] == "Summary"]

        self.assertEqual(
            prepare,
            [
                {
                    "name": "Prepare release notes",
                    "if": "steps.guard.outputs.skip == 'false'",
                    "run": "pdm run python -m scripts.release_notes prepare",
                }
            ],
        )
        self.assertEqual(names.index("Prepare release notes") + 1, names.index("Semantic release"))
        self.assertNotIn("--generate-notes", summary["run"])
        lines = [line.strip() for line in summary["run"].splitlines()]
        start = lines.index(FAILURE_LINE)
        self.assertEqual(
            lines[start : start + 6],
            [
                FAILURE_LINE,
                'echo "the workflow by hand. If it was pushed,"',
                *RECOVERY_LINES,
            ],
        )
        self.assertIn(TAGGED_LINE, lines)
        self.assertNotIn("Released", summary["run"])
        self.assertEqual(summary["env"]["DRAFT"], "${{ steps.draft.outcome }}")

    def test_the_release_job_keeps_its_permissions_and_trigger(self):
        workflow = load("release.yml")
        job = workflow["jobs"]["release"]

        self.assertEqual(workflow["permissions"], {})
        self.assertEqual(job["permissions"], {"contents": "write", "actions": "read"})
        self.assertEqual(
            workflow[True]["workflow_run"],
            {
                "workflows": ["CI"],
                "types": ["completed"],
                "branches": ["main"],
            },
        )


class PreviewWorkflowTest(unittest.TestCase):
    def test_the_preview_is_read_only_and_reaches_no_publication(self):
        self.maxDiff = None
        self.assertTrue((WORKFLOWS / "release-notes-preview.yml").is_file())
        self.assertEqual(
            load("release-notes-preview.yml"),
            {
                "name": "Release notes preview",
                True: {"pull_request": {"branches": ["main"]}, "workflow_dispatch": None},
                "concurrency": {
                    "group": GROUP,
                    "cancel-in-progress": True,
                },
                "permissions": {},
                "jobs": {
                    "preview": {
                        "name": "release notes preview",
                        "runs-on": "ubuntu-24.04",
                        "timeout-minutes": 15,
                        "permissions": {"contents": "read"},
                        "steps": [
                            {
                                "name": "Check out the candidate",
                                "uses": CHECKOUT,
                                "with": {
                                    "ref": HEAD_REF,
                                    "fetch-depth": 0,
                                    "persist-credentials": False,
                                },
                            },
                            {
                                "name": "Set up release tooling",
                                "uses": "$/.github/actions/setup",
                                "with": {"release-only": "true"},
                            },
                            {
                                "name": "Preview the release notes",
                                "env": {
                                    "BASE_SHA": "${{ github.event.pull_request.base.sha }}",
                                    "REPOSITORY": "${{ github.repository }}",
                                },
                                "run": PREVIEW_SCRIPT,
                            },
                        ],
                    }
                },
            },
        )


if __name__ == "__main__":
    unittest.main()
