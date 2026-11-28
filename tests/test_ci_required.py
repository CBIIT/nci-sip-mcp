"""All CI results reach one fail-closed merge context; shell inputs stay data."""

import json
import os
import re
import subprocess
import sys
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github/workflows"
ALLOWED_SKIPS = ("mutation", "image", "companions", "coverage-badges")
CONTEXTS = {
    "required": "CI",
    "conventional commit subject": "PR title",
    "dependency review": "Audit",
    "CodeQL": "GitHub default setup (integration 57789)",
    "pip-audit": "Audit",
}


def workflow(name: str):
    return yaml.safe_load((WORKFLOWS / f"{name}.yml").read_text())


def context_producers() -> dict[str, str]:
    return {
        job.get("name", name): value["name"]
        for path in WORKFLOWS.glob("*.yml")
        for value in [yaml.safe_load(path.read_text())]
        for name, job in value["jobs"].items()
    }


class RequiredCommandTest(unittest.TestCase):
    def run_gate(self, needs: object) -> subprocess.CompletedProcess[str]:
        self.assertTrue(
            (ROOT / "scripts/ci_required.py").is_file(),
            "The required-check command must exist before CI can report the merge gate",
        )
        return subprocess.run(
            [sys.executable, "-m", "scripts.ci_required"],
            cwd=ROOT,
            env=os.environ | {"NEEDS_JSON": json.dumps(needs)},
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

    def test_all_success_and_only_the_closed_skip_list_pass(self):
        for skipped in (None, *ALLOWED_SKIPS):
            with self.subTest(skipped=skipped):
                needs = {name: {"result": "success"} for name in (*ALLOWED_SKIPS, "quality")}
                if skipped is not None:
                    needs[skipped] = {"result": "skipped"}
                result = self.run_gate(needs)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_failure_cancellation_missing_unknown_and_unapproved_skips_fail(self):
        for name, result in (
            ("quality", "skipped"),
            ("quality", "failure"),
            ("quality", "cancelled"),
            ("quality", "pending"),
            ("quality", None),
            *((name, "failure") for name in ALLOWED_SKIPS),
            *((name, "cancelled") for name in ALLOWED_SKIPS),
        ):
            with self.subTest(name=name, result=result):
                row = {} if result is None else {"result": result}
                process = self.run_gate({name: row, "test": {"result": "success"}})
                self.assertEqual(process.returncode, 1, process.stderr)
                self.assertIn(name, process.stderr)

    def test_dependency_failure_is_not_hidden_by_an_allowed_skip(self):
        result = self.run_gate({"changes": {"result": "failure"}, "image": {"result": "skipped"}})
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("changes", result.stderr)

    def test_only_declared_conditional_jobs_can_skip_across_the_whole_ci_graph(self):
        for name in set(workflow("ci")["jobs"]) - {"required"}:
            with self.subTest(job=name):
                result = self.run_gate({name: {"result": "skipped"}})
                self.assertEqual(result.returncode, 0 if name in ALLOWED_SKIPS else 1)

    def test_empty_or_malformed_needs_cannot_report_success(self):
        for needs in (
            {},
            [],
            [1],
            None,
            {"quality": None},
            {"quality": "success"},
            {"quality": {"result": []}},
        ):
            with self.subTest(needs=needs):
                result = self.run_gate(needs)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn("Invalid CI needs", result.stderr)
                if needs == {"quality": None}:
                    self.assertEqual(
                        result.stderr, "Invalid CI needs: quality must be a job object\n"
                    )


class RequiredWorkflowTest(unittest.TestCase):
    def test_required_runs_after_every_other_ci_job_even_on_failure(self):
        jobs = workflow("ci")["jobs"]
        self.assertIn("required", set(jobs), "CI must always report one aggregate required context")
        gate = jobs["required"]
        # Pages deployment needs the aggregate itself, so it cannot be one of its inputs.
        self.assertEqual(set(gate["needs"]), set(jobs) - {"required", "pages-deploy"})
        self.assertEqual(len(gate["needs"]), len(jobs) - 2)
        self.assertEqual(
            {key: value for key, value in gate.items() if key != "needs"},
            {
                "name": "required",
                "if": "always()",
                "runs-on": "ubuntu-24.04",
                "timeout-minutes": 5,
                "permissions": {"contents": "read"},
                "steps": [
                    {
                        "uses": "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1",
                        "with": {"persist-credentials": False},
                    },
                    {
                        "uses": "actions/setup-python@6d4fd22b20465ccc0f7354476588e8df7c9ca0e0",
                        "with": {"python-version": "3.14"},
                    },
                    {
                        "name": "Require every CI result",
                        "env": {"NEEDS_JSON": "${{ toJSON(needs) }}"},
                        "run": "python3 -m scripts.ci_required",
                    },
                ],
            },
        )
        self.assertEqual(workflow("ci")[True]["pull_request"], None)

    def test_required_check_documentation_names_exact_contexts_and_producers(self):
        document = (ROOT / "CONTRIBUTING.md").read_text()
        section = document.partition("### Required checks\n")[2].split("\n### ", 1)[0]
        rows = {
            cells[0].strip().strip("`"): cells[1].strip()
            for line in section.splitlines()
            if line.startswith("| `")
            for cells in [line.split("|")[1:3]]
        }
        self.assertEqual(rows, CONTEXTS)
        producers = context_producers()
        for context, producer in CONTEXTS.items():
            if context != "CodeQL":
                with self.subTest(context=context):
                    self.assertEqual(producers.get(context), producer)


def run_steps():
    """Enumerate workflow and composite-action run steps, not generated shell files."""
    for path in (ROOT / ".github").rglob("*.yml"):
        value = yaml.safe_load(path.read_text())
        groups = [job.get("steps", []) for job in value.get("jobs", {}).values()]
        groups.append(value.get("runs", {}).get("steps", []))
        for steps in groups:
            for step in steps:
                if "run" in step:
                    yield path, step


class WorkflowHygieneTest(unittest.TestCase):
    def test_local_run_expression_inputs_reach_exact_step_environment(self):
        expected = {
            "Select release or candidate index source (CI only)": {
                "EVENT_NAME": "${{ github.event_name }}"
            },
            "Install released server and optional index environments": {
                "WITH_INDEX": "${{ matrix.index }}"
            },
            "Build selected local container images": {
                "COMPOSE_COMPANION": "${{ matrix.compose_companion }}"
            },
            "Verify the selected Compose profiles (CI only)": {
                "COMPOSE_DOCS": "${{ matrix.compose_docs }}",
                "COMPOSE_COMPANION": "${{ matrix.compose_companion }}",
            },
            "Verify every selected component together (CI only)": {
                "HOST_DOCS": "${{ matrix.host_docs }}"
            },
        }
        steps = workflow("local-run")["jobs"]["local-run"]["steps"]
        actual = {step["name"]: step.get("env") for step in steps if step.get("name") in expected}
        self.assertEqual(actual, expected)

    def test_workflow_and_composite_shell_text_never_interpolates_expressions(self):
        violations = [
            f"{path.relative_to(ROOT)}: {step.get('name', step['run'].splitlines()[0])}"
            for path, step in run_steps()
            if "${{" in step["run"]
        ]
        self.assertEqual(violations, [], "Pass expressions through env, not shell text")

    def test_each_write_permission_has_an_adjacent_reason_without_new_privileges(self):
        missing = [
            f"{path.name}:{number}"
            for path in WORKFLOWS.glob("*.yml")
            for number, line in enumerate(path.read_text().splitlines(), 1)
            if re.fullmatch(r"\s+[\w-]+: write\s*", line)
        ]
        self.assertEqual(missing, [], "Explain each write permission on its own line")
        self.assertEqual(workflow("release")["permissions"], {})
        self.assertEqual(
            {name: job["permissions"] for name, job in workflow("release")["jobs"].items()},
            {
                "release": {"contents": "write", "actions": "read"},
                "image": {
                    "contents": "write",
                    "packages": "write",
                    "id-token": "write",
                    "attestations": "write",
                },
                "local-installer": {"contents": "read"},
                "publish": {"contents": "write"},
            },
        )
        self.assertEqual(workflow("branch-cleanup")["permissions"], {})
        self.assertEqual(
            workflow("branch-cleanup")["jobs"]["cleanup"]["permissions"], {"contents": "write"}
        )

    def test_live_workflow_concurrency_cancels_only_replaced_manual_runs(self):
        live = workflow("acceptance-live")
        self.assertEqual(
            live.get("concurrency"),
            {
                "group": "acceptance-live-${{ github.ref }}",
                "cancel-in-progress": True,
            },
        )
        self.assertEqual(
            live[True], {"workflow_dispatch": None, "schedule": [{"cron": "41 6 * * 1"}]}
        )
        self.assertEqual(live["permissions"], {"contents": "read"})
