"""CI retains failed-run JUnit evidence without changing producer failure or cancellation."""

# ruff: noqa: E501 - exact workflow contracts preserve literal commands and messages.

import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
UPLOAD = "actions/upload-artifact@cf430e030ddbb5b0abf93d22962f4752f3646cd9"


def report_positions(steps):
    setup = next(
        index for index, step in enumerate(steps) if step.get("uses") == "$/.github/actions/setup"
    )
    producer = next(
        index for index, step in enumerate(steps) if "--junitxml=" in step.get("run", "")
    )
    names = [step.get("name") for step in steps]
    return (
        setup,
        producer,
        names.index("Summarize test results"),
        names.index("Upload test results"),
    )


class TestReportWorkflowTest(unittest.TestCase):
    def test_report_steps_have_exact_paths_conditions_and_artifact_names(self):
        jobs = yaml.safe_load(ROOT.joinpath(".github/workflows/ci.yml").read_text())["jobs"]
        self.assertEqual(
            jobs["selftest"]["strategy"],
            {
                "fail-fast": False,
                "matrix": {
                    "shard": [
                        "1/8",
                        "2/8",
                        "3/8",
                        "4/8",
                        "5/8",
                        "6/8",
                        "7/8",
                        "8/8",
                    ]
                },
            },
        )
        for job, path, artifact, title in (
            (
                "test",
                "tmp/test-results/unit.xml",
                "unit-test-results-${{ strategy.job-index }}",
                "Unit tests",
            ),
            (
                "selftest",
                "acceptance/tmp/test-results/selftest.xml",
                "selftest-results-${{ strategy.job-index }}",
                "Harness self-tests",
            ),
        ):
            with self.subTest(job=job):
                steps = jobs[job]["steps"]
                setup, producer, summary, upload = report_positions(steps)
                self.assertLess(setup, producer)
                self.assertEqual((summary, upload), (producer + 1, producer + 2))
                reports = [
                    step
                    for step in jobs[job]["steps"]
                    if step.get("name") in {"Summarize test results", "Upload test results"}
                ]
                self.assertEqual(
                    reports,
                    [
                        {
                            "name": "Summarize test results",
                            "if": "${{ !cancelled() }}",
                            "run": f'pdm run python scripts/junit_summary.py {path} "{title}" '
                            '>> "$GITHUB_STEP_SUMMARY"',
                        },
                        {
                            "name": "Upload test results",
                            "if": "${{ !cancelled() }}",
                            "uses": UPLOAD,
                            "with": {
                                "name": artifact,
                                "path": path,
                                "if-no-files-found": "error",
                                "retention-days": 30,
                            },
                        },
                    ],
                )

    def test_unit_producer_still_fails_normally_and_writes_junit(self):
        workflow = yaml.safe_load(ROOT.joinpath(".github/workflows/ci.yml").read_text())
        self.assertEqual(workflow["permissions"], {"contents": "read"})
        self.assertEqual(
            workflow["jobs"]["test"],
            {
                "name": "test (3.14, ${{ matrix.shard }})",
                "runs-on": "ubuntu-24.04",
                "timeout-minutes": 10,
                "strategy": {
                    "fail-fast": False,
                    "matrix": {"shard": ["1/4", "2/4", "3/4", "4/4"]},
                },
                "steps": [
                    {
                        "uses": "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1",
                        "with": {"fetch-depth": 0, "persist-credentials": False},
                    },
                    {"uses": "$/.github/actions/setup"},
                    {
                        "name": "Configure shared unit coverage",
                        "run": "pdm run python -m scripts.validation.mutation_coverage configure unit --output tmp/mutation-config",
                    },
                    {
                        "name": "Test, including recorded retrieval evaluation",
                        "env": {
                            "PYTHONPATH": "${{ github.workspace }}",
                            "COVERAGE_PROCESS_START": "${{ github.workspace }}/tmp/mutation-config/.coveragerc",
                            "UNIT_SHARD": "${{ matrix.shard }}",
                        },
                        "run": 'pdm run test --unit-shard "$UNIT_SHARD" -n 0 -p scripts.validation.mutation_probe --cov-config=tmp/mutation-config/.coveragerc --cov-context=test --cov-fail-under=0 --cov-report= --junitxml=tmp/test-results/unit.xml -vv --durations=20 -o faulthandler_timeout=60',
                    },
                    {
                        "name": "Summarize test results",
                        "if": "${{ !cancelled() }}",
                        "run": 'pdm run python scripts/junit_summary.py tmp/test-results/unit.xml "Unit tests" >> "$GITHUB_STEP_SUMMARY"',
                    },
                    {
                        "name": "Upload test results",
                        "if": "${{ !cancelled() }}",
                        "uses": UPLOAD,
                        "with": {
                            "name": "unit-test-results-${{ strategy.job-index }}",
                            "path": "tmp/test-results/unit.xml",
                            "if-no-files-found": "error",
                            "retention-days": 30,
                        },
                    },
                    {
                        "uses": UPLOAD,
                        "with": {
                            "name": "unit-coverage-${{ strategy.job-index }}",
                            "path": ".coverage",
                            "include-hidden-files": True,
                            "if-no-files-found": "error",
                            "retention-days": 1,
                        },
                    },
                    {
                        "name": "Export shared unit coverage",
                        "env": {"UNIT_SHARD": "${{ matrix.shard }}"},
                        "run": 'pdm run python -m scripts.validation.mutation_coverage export unit --data .coverage --shard "$UNIT_SHARD" --output tmp/mutation-coverage/unit.json',
                    },
                    {
                        "uses": UPLOAD,
                        "with": {
                            "name": "mutation-coverage-${{ github.sha }}-unit-${{ strategy.job-index }}",
                            "path": "tmp/mutation-coverage/unit.json",
                            "if-no-files-found": "error",
                            "retention-days": 1,
                        },
                    },
                ],
            },
        )

    def test_unit_coverage_requires_every_partition_before_enforcing_the_floor(self):
        workflow = yaml.safe_load(ROOT.joinpath(".github/workflows/ci.yml").read_text())
        self.assertEqual(workflow["permissions"], {"contents": "read"})
        self.assertEqual(
            workflow["jobs"].get("unit-coverage"),
            {
                "name": "unit coverage",
                "needs": "test",
                "runs-on": "ubuntu-24.04",
                "timeout-minutes": 10,
                "steps": [
                    {
                        "uses": "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1",
                        "with": {"fetch-depth": 0, "persist-credentials": False},
                    },
                    {"uses": "$/.github/actions/setup"},
                    {
                        "uses": "actions/download-artifact@9000827ccba6bdab643e8b6fd33ac0654aef8333",
                        "with": {"pattern": "unit-coverage-*", "path": "unit-coverage"},
                    },
                    {
                        "name": "Combine every unit shard and enforce the minimum",
                        "run": "shards=(unit-coverage/unit-coverage-*)\n"
                        'test "${#shards[@]}" -eq 4\n'
                        "pdm run coverage combine unit-coverage/unit-coverage-{0,1,2,3}/.coverage\n"
                        "pdm run coverage report --include='*/src/nci_si_mcp/*,*/src/ncit_index/*,*/scripts/*' --omit='*/scripts/companion_entry.py' --fail-under=95\n"
                        "pdm run coverage report --include='*/src/nci_si_mcp/*,*/src/ncit_index/*' --fail-under=95\n",
                    },
                    {
                        "name": "Report coverage against the aim",
                        "run": "total=\"$(pdm run coverage report --include='*/src/nci_si_mcp/*,*/src/ncit_index/*,*/scripts/*' --omit='*/scripts/companion_entry.py' --fail-under=0 --format=total)\"\n"
                        'echo "Coverage ${total}% (the aim is above 98%)" >> "$GITHUB_STEP_SUMMARY"\n'
                        "if awk -v total=\"$total\" 'BEGIN { exit !(total <= 98) }'; then\n"
                        '  echo "::warning title=Coverage is not above 98%::${total}%. Cover the untested behaviour; do not pad."\nfi\n',
                    },
                    {
                        "name": "Export measured server coverage",
                        "run": "pdm run coverage json --include='*/src/nci_si_mcp/*,*/src/ncit_index/*' -o tmp/coverage/server.json",
                    },
                    {
                        "uses": UPLOAD,
                        "with": {
                            "name": "badge-coverage-server",
                            "path": "tmp/coverage/server.json",
                            "if-no-files-found": "error",
                            "retention-days": 30,
                        },
                    },
                ],
            },
        )
