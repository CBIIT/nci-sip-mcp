"""Coverage floors, warning aims and contributor statements agree exactly."""

# ruff: noqa: E501 - exact workflow snapshots preserve literal commands and messages.

import tomllib
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def floor(name):
    return tomllib.loads((ROOT / name).read_text())["tool"]["coverage"]["report"]["fail_under"]


class CoveragePolicyTest(unittest.TestCase):
    def test_both_suites_enforce_the_exact_floor(self):
        self.assertEqual([floor("pyproject.toml"), floor("acceptance/pyproject.toml")], [95, 95])
        project = tomllib.loads(ROOT.joinpath("pyproject.toml").read_text())["tool"]
        harness = tomllib.loads(ROOT.joinpath("acceptance/pyproject.toml").read_text())["tool"]
        self.assertEqual(
            project["coverage"],
            {
                "run": {
                    "data_file": "${PDM_PROJECT_ROOT?}/tmp/coverage/unit/.coverage",
                    "branch": True,
                    "source": ["nci_si_mcp", "ncit_index", "scripts"],
                    "omit": ["*/scripts/companion_entry.py"],
                    "patch": ["subprocess"],
                },
                "report": {
                    "fail_under": 95,
                    "precision": 2,
                    "show_missing": True,
                    "exclude_also": ["if __name__ == .__main__.:"],
                },
            },
        )
        self.assertEqual(
            harness["coverage"],
            {
                "run": {
                    "data_file": "${PDM_PROJECT_ROOT?}/tmp/coverage/harness/.coverage",
                    "branch": True,
                    "source": ["nci_si_acceptance"],
                    "patch": ["subprocess"],
                },
                "report": {"fail_under": 95, "precision": 2, "show_missing": True},
            },
        )
        self.assertEqual(
            {
                name: project["pdm"]["scripts"][name]
                for name in ("test", "acceptance-selftest", "github-setup")
            },
            {
                "test": "pytest --cov --cov-report=term-missing",
                "acceptance-selftest": {
                    "cmd": "pytest selftests -n 4 --dist worksteal --cov --cov-report=term-missing",
                    "working_dir": "acceptance",
                },
                "github-setup": "python -m scripts.github_setup",
            },
        )

    def test_ci_reports_the_exact_warning_aim_without_changing_measurement(self):
        jobs = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())["jobs"]
        server = next(
            step
            for step in jobs["unit-coverage"]["steps"]
            if step.get("name") == "Report coverage against the aim"
        )
        harness = next(
            step
            for step in jobs["coverage"]["steps"]
            if step.get("name") == "Combine the shards and enforce the minimum"
        )
        self.assertEqual(
            server,
            {
                "name": "Report coverage against the aim",
                "run": "total=\"$(pdm run coverage report --include='*/src/nci_si_mcp/*,*/src/ncit_index/*,*/scripts/*' --omit='*/scripts/companion_entry.py' --fail-under=0 --format=total)\"\n"
                'echo "Coverage ${total}% (the aim is above 98%)" >> "$GITHUB_STEP_SUMMARY"\n'
                "if awk -v total=\"$total\" 'BEGIN { exit !(total <= 98) }'; then\n"
                '  echo "::warning title=Coverage is not above 98%::${total}%. Cover the untested behaviour; do not pad."\nfi\n',
            },
        )
        self.assertEqual(
            harness,
            {
                "name": "Combine the shards and enforce the minimum",
                "run": "pdm run coverage combine --rcfile=acceptance/pyproject.toml --data-file=.coverage-selftest selftest-coverage/*/.coverage\n"
                "pdm run coverage report --rcfile=acceptance/pyproject.toml --data-file=.coverage-selftest --include='*/acceptance/src/*'\n"
                "total=\"$(pdm run coverage report --rcfile=acceptance/pyproject.toml --data-file=.coverage-selftest --include='*/acceptance/src/*' --format=total)\"\n"
                'echo "Self-test coverage ${total}% (the aim is above 98%)" >> "$GITHUB_STEP_SUMMARY"\n'
                "if awk -v total=\"$total\" 'BEGIN { exit !(total <= 98) }'; then\n"
                '  echo "::warning title=Self-test coverage is not above 98%::${total}%. Cover the untested behaviour; do not pad."\nfi\n',
            },
        )
        self.assertEqual(
            [step for step in jobs["selftest"]["steps"] if "SELFTEST_SHARD" in step.get("env", {})],
            [
                {
                    "run": "pdm run acceptance-selftest -p scripts.validation.mutation_probe --cov-config=../tmp/mutation-config/.coveragerc --cov-context=test --cov-fail-under=0 --cov-report= "
                    "--junitxml=tmp/test-results/selftest.xml",
                    "env": {
                        "PYTHONPATH": "${{ github.workspace }}",
                        "COVERAGE_PROCESS_START": "${{ github.workspace }}/tmp/mutation-config/.coveragerc",
                        "SELFTEST_SHARD": "${{ matrix.shard }}",
                    },
                },
                {
                    "name": "Export shared selftest coverage",
                    "env": {"SELFTEST_SHARD": "${{ matrix.shard }}"},
                    "run": 'pdm run python -m scripts.validation.mutation_coverage export selftest --data acceptance/.coverage --shard "$SELFTEST_SHARD" --output tmp/mutation-coverage/selftest.json',
                },
            ],
        )

    def test_documented_thresholds_match_the_gate(self):
        minimum = floor("pyproject.toml")
        expected = {
            "AGENTS.md": f"**Coverage: the checked minimum is {minimum}%, the aim is above 98%.**",
            "CONTRIBUTING.md": f"**Coverage: {minimum}% is the checked minimum, above 98% is the aim.**",
            "docs/evidence-contract.md": (
                f"CI's enforced {minimum}% floors cover both the combined core-and-scripts "
                "measurement and the two core packages separately"
            ),
        }
        for name, statement in expected.items():
            with self.subTest(name=name):
                document = " ".join((ROOT / name).read_text().split())
                self.assertIn(statement, document)

        for name in ("AGENTS.md", "CONTRIBUTING.md"):
            with self.subTest(name=name):
                document = " ".join((ROOT / name).read_text().split())
                self.assertIn(
                    "CI checks both the combined core-and-scripts and core-only minimums",
                    document,
                )
