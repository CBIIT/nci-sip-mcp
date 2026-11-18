"""Both fixture runs of the acceptance suite run its gates, neither is given a secret, and every
parallel run steals work."""

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import yaml
from scripts import github_setup as module

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github/workflows"
FIXTURE_STEP = "Run the suite against the fixtures"
# The permission gates (X-25 to X-28) run only with a security server; without one they are
# unrun, and an unrun gate leaves every tool INCOMPLETE (#265).
FIXTURE_ENV = {
    "NCI_SI_ACCEPTANCE_REGISTRY_RELEASE": "required",
    "NCI_SI_ACCEPTANCE_SECURITY_SERVER": "python ../scripts/permissions_fixture.py",
    "NCI_SI_ACCEPTANCE_PREPARE": 'nci-si-mcp index-sample $(cat "$NCI_SI_ACCEPTANCE_INDEX_CODES")',
}


LIVE_STEP = "Run the suite against the live services"


def jobs(workflow):
    return yaml.safe_load((WORKFLOWS / workflow).read_text())["jobs"]


def step(workflow, job, name):
    steps = jobs(workflow)[job]["steps"]
    (found,) = [step for step in steps if step.get("name") == name]
    return found


def fixture_env(workflow, job):
    return step(workflow, job, FIXTURE_STEP)["env"]


def suite_line(workflow, job, name):
    """The line of a step that runs the suite."""

    (line,) = [
        line.strip()
        for line in step(workflow, job, name)["run"].splitlines()
        if line.strip().startswith("pdm run acceptance ")
    ]
    return line


class AcceptanceGateWorkflowTest(unittest.TestCase):
    def test_fixture_ratchet_uses_the_event_or_ancestry_checked_dispatch_base(self):
        compare = step("ci.yml", "acceptance", "Compare the outcomes with the expected ones")
        self.assertEqual(
            compare,
            {
                "name": "Compare the outcomes with the expected ones",
                "if": "${{ always() && hashFiles('acceptance/fixture.json') != '' }}",
                "env": {
                    "EVENT_NAME": "${{ github.event_name }}",
                    "PR_BASE_SHA": "${{ github.event.pull_request.base.sha }}",
                    "DISPATCH_BASE_SHA": "${{ inputs.base_sha }}",
                },
                "run": "status=0\n"
                "arguments=()\n"
                'if [ "$EVENT_NAME" = "pull_request" ] || '
                '[ "$EVENT_NAME" = "workflow_dispatch" ]; then\n'
                '  if [ "$EVENT_NAME" = "pull_request" ]; then '
                'BASE_SHA="$PR_BASE_SHA"; else BASE_SHA="$DISPATCH_BASE_SHA"; fi\n'
                '  if ! [[ "$BASE_SHA" =~ ^[0-9a-f]{40}$ ]]; then\n'
                '    echo "::error title=Invalid base SHA::Supply a full lowercase '
                '40-character commit SHA."\n'
                "    exit 1\n"
                "  fi\n"
                '  if ! git cat-file -e "$BASE_SHA^{commit}" 2>/dev/null; then\n'
                '    echo "::error title=Unknown base commit::$BASE_SHA is not available '
                'in this checkout."\n'
                "    exit 1\n"
                "  fi\n"
                '  if [ "$(git cat-file -t "$BASE_SHA")" != "commit" ]; then\n'
                '    echo "::error title=Base is not a commit object::$BASE_SHA must identify '
                'a commit object directly."\n'
                "    exit 1\n"
                "  fi\n"
                '  if ! git merge-base --is-ancestor "$BASE_SHA" HEAD; then\n'
                '    echo "::error title=Base is not an ancestor::$BASE_SHA must be an '
                'ancestor of HEAD."\n'
                "    exit 1\n"
                "  fi\n"
                '  git show "$BASE_SHA:acceptance/expected/fixture.json" > tmp/base-fixture.json\n'
                "  arguments=(--base tmp/base-fixture.json "
                '--base-sha "$BASE_SHA" '
                "--approvals acceptance/expected/regressions.yaml)\n"
                "fi\n"
                "pdm run acceptance-expected check acceptance/fixture.json "
                '"${arguments[@]}" > acceptance/difference.md || status=$?\n'
                '{ echo "### Outcomes against acceptance/expected/fixture.json"; echo; '
                'cat acceptance/difference.md; } >> "$GITHUB_STEP_SUMMARY"\n'
                "cat acceptance/difference.md\n"
                'exit "$status"\n',
            },
        )

    def test_only_a_clean_fixture_execution_exports_the_mutation_baseline(self):
        job = jobs("ci.yml")["acceptance"]
        self.assertEqual(step("ci.yml", "acceptance", FIXTURE_STEP)["id"], "fixture")
        self.assertEqual(
            [
                entry
                for entry in job["steps"]
                if entry.get("name") == "Configure shared fixture coverage"
            ],
            [
                {
                    "name": "Configure shared fixture coverage",
                    "run": (
                        "pdm run python -m scripts.validation.mutation_coverage configure "
                        "fixture --shard 1/1 --output tmp/mutation-config"
                    ),
                }
            ],
        )
        self.assertEqual(
            [
                entry
                for entry in job["steps"]
                if entry.get("name") == "Export shared fixture coverage"
            ],
            [
                {
                    "name": "Export shared fixture coverage",
                    "env": {"FIXTURE_STATUS": "${{ steps.fixture.outputs.status }}"},
                    "run": 'test "$FIXTURE_STATUS" -eq 0\n'
                    "pdm run python -m scripts.validation.mutation_coverage export fixture "
                    "--data acceptance/.coverage --shard 1/1 "
                    "--output tmp/mutation-coverage/fixture.json\n",
                }
            ],
        )

    def test_ci_selftest_job_runs_all_shards_without_overrides(self):
        job = jobs("ci.yml")["selftest"]
        self.assertEqual(
            job["strategy"],
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
        self.assertEqual(job.get("env", {}), {})
        self.assertEqual(job.get("if", ""), "")
        self.assertEqual(job.get("needs", []), [])
        (run,) = [
            entry
            for entry in job["steps"]
            if entry.get("run", "").startswith("pdm run acceptance-selftest ")
        ]
        self.assertEqual(
            run,
            {
                "run": "pdm run acceptance-selftest -p scripts.validation.mutation_probe "
                "--cov-config=../tmp/mutation-config/.coveragerc --cov-context=test "
                "--cov-fail-under=0 --cov-report= "
                "--junitxml=tmp/test-results/selftest.xml",
                "env": {
                    "PYTHONPATH": "${{ github.workspace }}",
                    "COVERAGE_PROCESS_START": (
                        "${{ github.workspace }}/tmp/mutation-config/.coveragerc"
                    ),
                    "SELFTEST_SHARD": "${{ matrix.shard }}",
                },
            },
        )

    def test_anonymous_ssis_contract_step_always_attempts_and_preserves_failure(self):
        uploads = [
            step
            for step in jobs("acceptance-live.yml")["acceptance-live"]["steps"]
            if step.get("uses", "").startswith("actions/upload-artifact@")
        ]
        self.assertEqual(
            uploads,
            [
                {
                    "uses": "actions/upload-artifact@cf430e030ddbb5b0abf93d22962f4752f3646cd9",
                    "if": "always()",
                    "with": {
                        "name": "acceptance-live",
                        "path": "acceptance/fixture.json\nacceptance/live.json\n"
                        "acceptance/report.md\nacceptance/network.md\n"
                        "acceptance/drift.md\nacceptance/ssis-contracts.json\n",
                        "if-no-files-found": "warn",
                        "retention-days": 30,
                    },
                }
            ],
        )
        workflow = yaml.safe_load((WORKFLOWS / "acceptance-live.yml").read_text())
        job = workflow["jobs"]["acceptance-live"]
        self.assertEqual(workflow["permissions"], {"contents": "read"})
        self.assertEqual(job.get("permissions"), None)
        self.assertEqual(job.get("env", {}), {})
        self.assertEqual(job.get("if"), None)
        self.assertEqual(job.get("needs"), None)
        self.assertEqual(
            step(
                "acceptance-live.yml",
                "acceptance-live",
                "Check every anonymous Shared SI query contract",
            ),
            {
                "name": "Check every anonymous Shared SI query contract",
                "if": "${{ !cancelled() }}",
                "run": "status=0\n"
                "pdm run python -m scripts.ssis_contracts > "
                "acceptance/ssis-contracts.json || status=$?\n"
                "{ echo '### Shared SI query contracts'; echo; "
                "cat acceptance/ssis-contracts.json; } "
                '>> "$GITHUB_STEP_SUMMARY"\nexit "$status"\n',
            },
        )

    def test_live_placeholders_are_absent_and_real_values_never_enter_logs(self):
        live = step("acceptance-live.yml", "acceptance-live", LIVE_STEP)
        self.assertEqual(
            live["env"],
            {
                "NCI_SI_ACCEPTANCE_MODE": "live",
                "NCI_SI_ACCEPTANCE_PREPARE": FIXTURE_ENV["NCI_SI_ACCEPTANCE_PREPARE"],
                "NCI_SI_EVS_LICENSE_KEY": "${{ secrets.NCI_SI_EVS_LICENSE_KEY }}",
                "NCI_SI_CADSR_CREDENTIAL": "${{ secrets.NCI_SI_CADSR_CREDENTIAL }}",
                "CREDENTIAL_PLACEHOLDER": module.PLACEHOLDER,
            },
        )
        self.assertEqual(
            live,
            {
                "name": LIVE_STEP,
                "env": {
                    "NCI_SI_ACCEPTANCE_MODE": "live",
                    "NCI_SI_ACCEPTANCE_PREPARE": FIXTURE_ENV["NCI_SI_ACCEPTANCE_PREPARE"],
                    "NCI_SI_EVS_LICENSE_KEY": "${{ secrets.NCI_SI_EVS_LICENSE_KEY }}",
                    "NCI_SI_CADSR_CREDENTIAL": "${{ secrets.NCI_SI_CADSR_CREDENTIAL }}",
                    "CREDENTIAL_PLACEHOLDER": "NCI_SI_CONFIGURE_ME",
                },
                "run": 'absent=""\n'
                "for name in NCI_SI_EVS_LICENSE_KEY NCI_SI_CADSR_CREDENTIAL; do\n"
                '  if [ -z "${!name}" ] || [ "${!name}" = "$CREDENTIAL_PLACEHOLDER" ]; then\n'
                '    absent="$absent $name"\n    unset "$name"\n  fi\ndone\n'
                'echo "- Credentials absent from the live run:${absent:- none}" '
                '| tee -a acceptance/network.md >> "$GITHUB_STEP_SUMMARY"\n'
                "status=0\npdm run acceptance -n 4 --dist worksteal "
                "--report=live.json || status=$?\n"
                'if [ "$status" -gt 1 ] || [ ! -s acceptance/live.json ]; then\n'
                '  echo "::error title=The live run did not finish::'
                'exit status ${status}, or no report."\n'
                "  exit 1\nfi\n"
                "pdm run python -m nci_si_acceptance.report acceptance/live.json "
                "--check-complete live\n",
            },
        )
        for supplied in (
            ("", ""),
            (module.PLACEHOLDER, module.PLACEHOLDER),
            ("PRIVATE-LIVE-EVS-CANARY", "PRIVATE-LIVE-CADSR-CANARY"),
            (module.PLACEHOLDER, "PRIVATE-LIVE-CADSR-CANARY"),
            ("PRIVATE-LIVE-EVS-CANARY", module.PLACEHOLDER),
        ):
            with self.subTest(supplied=supplied), TemporaryDirectory() as directory:
                self.assert_live_environment(Path(directory), live, supplied)

    def assert_live_environment(self, root, live, supplied):
        root.joinpath("acceptance").mkdir()
        executable = root / "pdm"
        executable.write_text(
            f"#!{sys.executable}\n"
            + """
import json, os, sys
from pathlib import Path
if sys.argv[1:3] == ["run", "acceptance"]:
    Path("seen.json").write_text(json.dumps({k:v for k,v in os.environ.items() if k in
        ("NCI_SI_EVS_LICENSE_KEY", "NCI_SI_CADSR_CREDENTIAL")}))
    Path("acceptance/live.json").write_text("{}")
"""
        )
        executable.chmod(0o700)
        environment = os.environ | {
            "PATH": str(root) + os.pathsep + os.environ["PATH"],
            "GITHUB_STEP_SUMMARY": str(root / "summary"),
            "CREDENTIAL_PLACEHOLDER": live["env"]["CREDENTIAL_PLACEHOLDER"],
            "NCI_SI_EVS_LICENSE_KEY": supplied[0],
            "NCI_SI_CADSR_CREDENTIAL": supplied[1],
        }
        result = subprocess.run(  # noqa: S603 - repository step with an owned fake acceptance CLI.
            ["bash", "-c", live["run"]],  # noqa: S607
            cwd=root,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        names = ("NCI_SI_EVS_LICENSE_KEY", "NCI_SI_CADSR_CREDENTIAL")
        expected = {
            name: value
            for name, value in zip(names, supplied, strict=True)
            if value not in ("", module.PLACEHOLDER)
        }
        self.assertEqual(json.loads(root.joinpath("seen.json").read_text()), expected)
        summary = root.joinpath("summary").read_text()
        absent = "".join(f" {name}" for name in names if name not in expected) or " none"
        self.assertEqual(summary, f"- Credentials absent from the live run:{absent}\n")
        self.assertNotIn("PRIVATE-LIVE-", result.stdout + result.stderr + summary)
        self.assertEqual(root.joinpath("acceptance/network.md").read_text(), summary)

    def test_the_weekly_fixture_run_runs_the_same_gates_as_ci(self):
        self.assertEqual(
            fixture_env("ci.yml", "acceptance"),
            FIXTURE_ENV
            | {
                "PYTHONPATH": "${{ github.workspace }}",
                "COVERAGE_PROCESS_START": "${{ github.workspace }}/tmp/mutation-config/.coveragerc",
            },
        )
        self.assertEqual(fixture_env("acceptance-live.yml", "acceptance-live"), FIXTURE_ENV)

    def test_no_secret_reaches_either_fixture_run(self):
        # The job's environment and the fixture step's environment, inputs, condition and
        # script: everything the fixture run can see.
        for workflow, job in (("ci.yml", "acceptance"), ("acceptance-live.yml", "acceptance-live")):
            with self.subTest(workflow=workflow):
                fixture = step(workflow, job, FIXTURE_STEP)
                visible = {
                    "job env": jobs(workflow)[job].get("env", {}),
                    **{key: fixture.get(key, "") for key in ("env", "with", "if", "run")},
                }
                leaking = [key for key, part in visible.items() if "secrets." in json.dumps(part)]
                self.assertEqual(leaking, [])

    def test_every_parallel_suite_run_steals_work(self):
        # The default load distribution left one worker with the slow tail (#222).
        fixture = "pdm run acceptance -n 4 --dist worksteal --report=fixture.json || status=$?"
        live = "pdm run acceptance -n 4 --dist worksteal --report=live.json || status=$?"
        runs = {
            ("ci.yml", "acceptance", FIXTURE_STEP): (
                "pdm run acceptance -n 4 --dist worksteal -p scripts.validation.mutation_probe "
                "--cov --cov-config=../tmp/mutation-config/.coveragerc --cov-context=test "
                "--cov-fail-under=0 --cov-report= --report=fixture.json || status=$?"
            ),
            ("acceptance-live.yml", "acceptance-live", FIXTURE_STEP): fixture,
            ("acceptance-live.yml", "acceptance-live", LIVE_STEP): live,
        }
        for (workflow, job, name), expected in runs.items():
            with self.subTest(workflow=workflow, step=name):
                self.assertEqual(suite_line(workflow, job, name), expected)
