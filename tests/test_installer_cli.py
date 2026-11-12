import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

from nci_si_mcp import installer, installer_state

ROOT = Path(__file__).parent.parent
INSTALLER = Path(sys.executable).with_name("nci-si-install")
ROOT_HELP = "usage: nci-si-install [-h]\n                      {plan,check,run,status,start,stop,uninstall,help} ...\n\nInstall and operate the local NCI Semantic Infrastructure MCP server.\n\npositional arguments:\n  {plan,check,run,status,start,stop,uninstall,help}\n    plan                Print the ordered installation plan\n    check               Check every prerequisite before installation\n    run                 Run and record verified installation steps\n    status              Reverify and show installation status\n    start               Start HTTP or print the installed stdio client\n                        configuration\n    stop                Stop the installed local HTTP server\n    uninstall           Remove the recorded local installation\n    help                Explain an executable installation step\n\noptions:\n  -h, --help            show this help message and exit\n"  # noqa: E501

SELECTION_OPTIONS = """options:
  -h, --help        show this help message and exit
  --target {local}  installation target
"""

PLAN_HELP = "usage: nci-si-install plan [-h] --target {local}\n\n" + SELECTION_OPTIONS

CHECK_HELP = "usage: nci-si-install check [-h] --target {local}\n\n" + SELECTION_OPTIONS

RUN_HELP = (
    """usage: nci-si-install run [-h] --target {local} [--transport {stdio,http}]
                          [--profile {cadsr,evs,unified}] --data-dir DATA_DIR
                          [--restart] [--non-interactive]
"""
    + """
options:
  -h, --help            show this help message and exit
  --target {local}      installation target
  --transport {stdio,http}
                        local server transport (default: stdio)
  --profile {cadsr,evs,unified}
                        EVS/caDSR tool profile (default: unified)
  --data-dir DATA_DIR   installation state directory
  --restart             reset progress for the same selection
  --non-interactive     report unresolved actions without prompting
"""
)

STEP_HELP = "Step: check_prerequisites\nPurpose: Check every selected requirement before installation changes anything.\nWhy: A partial installation must not be created when required inputs are missing.\nAction: Apply every fix reported by `check` or a BLOCKED `run`.\nVerification: Rerun every selected prerequisite; confirmation alone is never accepted.\nCommon failures: missing tools, occupied ports, or unreachable EVS and caDSR endpoints.\nGuide: https://github.com/CBIIT/nci-sip-mcp/blob/main/docs/installer-targets.md\n"  # noqa: E501

STATUS_HELP = (
    """usage: nci-si-install status [-h] --target {local} [--transport {stdio,http}]
                             [--profile {cadsr,evs,unified}]
                             --data-dir DATA_DIR
"""
    + """
options:
  -h, --help            show this help message and exit
  --target {local}      installation target
  --transport {stdio,http}
                        local server transport (default: stdio)
  --profile {cadsr,evs,unified}
                        EVS/caDSR tool profile (default: unified)
  --data-dir DATA_DIR   installation state directory
"""
)


class InstallerCliTest(unittest.TestCase):
    def run_installer(self, *arguments, force_color=None, environment=None):
        environment = os.environ.copy() if environment is None else environment.copy()
        environment["COLUMNS"] = "80"
        if force_color is not None:
            environment["FORCE_COLOR"] = force_color
        process = subprocess.run(  # noqa: S603 - fixed project entry point with test arguments
            [INSTALLER, *arguments],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        return process.returncode, process.stdout, process.stderr

    def state_command(self, command, result, *, restart=False):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        with patch.object(installer, "reconcile", return_value=result) as reconcile:
            output = installer._state_command(  # pyright: ignore[reportPrivateUsage]
                installer.argparse.Namespace(
                    command=command,
                    data_dir=Path(directory.name),
                    restart=restart,
                    transport="stdio",
                    profile="unified",
                ),
                installer.InstallationPlan("local", ("server",)),
            )
        return output, reconcile

    def test_local_check_reports_every_failure_without_leaking_observations(self):
        environment = os.environ.copy()
        environment.update(
            {
                "COLUMNS": "80",
                "PATH": "",
                "NCI_SI_EVS_BASE_URL": "http://127.0.0.1:1/PRIVATE-CANARY",
                "NCI_SI_CADSR_BASE_URL": "http://127.0.0.1:2/PRIVATE-CANARY",
                "NCI_SI_DATA_DIR": str(ROOT / "missing-installer-state"),
            }
        )
        code, stdout, stderr = self.run_installer(
            "check",
            "--target",
            "local",
            environment=environment,
        )

        self.assertEqual((code, stderr), (1, ""))
        report = json.loads(stdout)
        self.assertEqual(report["status"], "failed")
        self.assertEqual(
            [result["name"] for result in report["results"]],
            [
                "supported_os",
                "git",
                "python",
                "evs_reachable",
                "cadsr_reachable",
            ],
        )
        failed = {result["name"] for result in report["results"] if result["status"] == "failed"}
        self.assertLessEqual(
            {
                "git",
                "evs_reachable",
                "cadsr_reachable",
            },
            failed,
        )
        self.assertNotIn("PRIVATE-CANARY", stdout + stderr)
        self.assertFalse((ROOT / "missing-installer-state").exists())

    def test_unexpected_value_and_runtime_bugs_propagate_from_main(self):
        for error in (ValueError("PRIVATE-CANARY"), RuntimeError("PRIVATE-CANARY")):
            with (
                tempfile.TemporaryDirectory() as directory,
                self.subTest(error=type(error).__name__),
                patch.object(installer, "_state_command", side_effect=error),
                self.assertRaisesRegex(type(error), "PRIVATE-CANARY"),
            ):
                installer.main(["status", "--target", "local", "--data-dir", directory])
        for value in ("not-a-url", ""):
            for command in ("check", "run", "status"):
                with (
                    self.subTest(value=value, command=command),
                    tempfile.TemporaryDirectory() as directory,
                ):
                    arguments = [command, "--target", "local"]
                    if command in ("run", "status"):
                        arguments.extend(("--data-dir", directory))
                    if command == "status":
                        installer_state.write_state(
                            Path(directory),
                            installer_state.InstallationState(
                                2,
                                "local",
                                ("server",),
                                (installer_state.StepOutcome("check_prerequisites", "completed"),),
                            ),
                        )
                    environment = os.environ.copy()
                    environment["NCI_SI_EVS_BASE_URL"] = value
                    code, stdout, stderr = self.run_installer(*arguments, environment=environment)
                self.assertEqual((code, stdout, "NCI_SI_EVS_BASE_URL" in stderr), (2, "", True))

    def test_run_records_and_status_reverifies_the_real_prerequisite_stage(self):
        with tempfile.TemporaryDirectory() as directory:
            environment = {
                "PATH": "",
                "NCI_SI_EVS_BASE_URL": "http://127.0.0.1:1",
                "NCI_SI_CADSR_BASE_URL": "http://127.0.0.1:2",
            }
            with patch.dict(os.environ, environment, clear=False):
                run = self.run_installer("run", "--target", "local", "--data-dir", directory)
                status = self.run_installer("status", "--target", "local", "--data-dir", directory)

            self.assertEqual((run[0], run[2]), (1, ""))
            self.assertEqual((status[0], status[2]), (1, ""))
            expected = {
                "components": ["server"],
                "completed": [],
                "failed": [],
                "blocked": [
                    {
                        "step": "check_prerequisites",
                        "missing": [
                            "Install Git so pip can install the recorded source commit.",
                            "Restore network access to NCI_SI_EVS_BASE_URL.",
                            "Restore network access to NCI_SI_CADSR_BASE_URL.",
                        ],
                    }
                ],
                "pending": ["install", "configure", "verify"],
                "nextAction": (
                    "Resolve the failed prerequisites: Install Git so pip can install the recorded "
                    "source commit.; Restore "
                    "network access to NCI_SI_EVS_BASE_URL.; Restore network access to "
                    "NCI_SI_CADSR_BASE_URL."
                ),
            }
            self.assertEqual(json.loads(run[1]), expected)
            self.assertEqual(json.loads(status[1]), expected)

    def test_restart_rejects_removed_component_flags_without_changing_state(self):
        with tempfile.TemporaryDirectory() as directory:
            environment = os.environ | {
                "NCI_SI_EVS_BASE_URL": "http://127.0.0.1:1",
                "NCI_SI_CADSR_BASE_URL": "http://127.0.0.1:2",
            }
            self.run_installer(
                "run", "--target", "local", "--data-dir", directory, environment=environment
            )
            state_path = Path(directory) / "installer-state.json"
            before = state_path.read_bytes()

            code, stdout, stderr = self.run_installer(
                "run",
                "--target",
                "local",
                "--with-index",
                "--data-dir",
                directory,
                "--restart",
                environment=environment,
            )

            self.assertEqual((code, stdout), (2, ""))
            self.assertIn("unrecognized arguments", stderr)
            self.assertEqual(state_path.read_bytes(), before)

    def test_run_forwards_restart_to_reconciliation(self):
        state = installer_state.InstallationState(2, "local", ("server",), ())
        (code, _), reconcile = self.state_command("run", state, restart=True)
        self.assertEqual(code, 1)
        self.assertTrue(reconcile.call_args.kwargs["restart"])

    def test_status_persists_and_projects_its_reverified_state(self):
        with tempfile.TemporaryDirectory() as directory:
            state = installer_state.InstallationState(
                2,
                "local",
                ("server",),
                (installer_state.StepOutcome("check_prerequisites", "completed"),),
            )
            installer_state.write_state(Path(directory), state)
            failed = installer.Prerequisite("required", "Repair it.", lambda: False)
            with patch.object(installer, "local_prerequisites", return_value=(failed,)):
                code, record = installer._state_command(  # pyright: ignore[reportPrivateUsage]
                    installer.argparse.Namespace(
                        command="status",
                        data_dir=Path(directory),
                        transport="stdio",
                        profile="unified",
                    ),
                    installer.InstallationPlan("local", ("server",)),
                )
            persisted = installer_state.read_state(Path(directory), "local", ("server",))
        blocked = cast("list[dict[str, Any]]", record["blocked"])[0]
        self.assertEqual((code, blocked["missing"]), (1, ["Repair it."]))
        self.assertEqual(persisted.outcomes[0].status, "blocked")

    def test_status_persists_progress_after_prerequisite_recovery(self):
        cases = (
            (
                None,
                0,
                {
                    "components": ["server"],
                    "completed": ["check_prerequisites", "install", "configure", "verify"],
                    "failed": [],
                    "blocked": [],
                    "pending": [],
                    "nextAction": None,
                },
                (
                    ("check_prerequisites", "completed"),
                    ("install", "completed"),
                    ("configure", "completed"),
                    ("verify", "completed"),
                ),
            ),
            (
                "install",
                1,
                {
                    "components": ["server"],
                    "completed": ["check_prerequisites"],
                    "failed": ["install"],
                    "blocked": [],
                    "pending": ["configure", "verify"],
                    "nextAction": "Retry failed step install.",
                },
                (("check_prerequisites", "completed"), ("install", "failed")),
            ),
        )
        for invalid, expected_code, expected_record, expected_outcomes in cases:
            with self.subTest(invalid=invalid), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                sentinel = root / "installation-applied"
                stages = ("check_prerequisites", "install", "configure", "verify")
                state = installer_state.InstallationState(
                    2,
                    "local",
                    ("server",),
                    tuple(installer_state.StepOutcome(step, "completed") for step in stages),
                )
                installer_state.write_state(root, state)
                prerequisites = iter((False, True))

                def verify(step, prerequisites=prerequisites, invalid=invalid):
                    if step == "check_prerequisites":
                        return next(prerequisites)
                    return step != invalid

                output, error = io.StringIO(), io.StringIO()
                with (
                    patch.object(installer.LocalActions, "verify", side_effect=verify),
                    patch.object(installer.LocalActions, "missing_inputs", return_value=()),
                    patch.object(installer.LocalActions, "apply", side_effect=sentinel.write_text),
                    redirect_stdout(output),
                    redirect_stderr(error),
                ):
                    code = installer.main(["status", "--target", "local", "--data-dir", directory])
                self.assertEqual(code, expected_code)
                self.assertEqual(json.loads(output.getvalue()), expected_record)
                self.assertEqual(error.getvalue(), "")
                self.assertEqual(
                    installer_state.read_state(root, "local", ("server",)),
                    installer_state.InstallationState(
                        2,
                        "local",
                        ("server",),
                        tuple(
                            installer_state.StepOutcome(step, outcome)
                            for step, outcome in expected_outcomes
                        ),
                    ),
                )
                self.assertFalse(sentinel.exists())

    def test_failed_and_pending_state_commands_exit_nonzero(self):
        states = (
            installer_state.InstallationState(
                2,
                "local",
                ("server",),
                (installer_state.StepOutcome("check_prerequisites", "failed"),),
            ),
            installer_state.InstallationState(2, "local", ("server",), ()),
        )
        for state in states:
            with self.subTest(state=state):
                (code, _), _ = self.state_command("run", state)
            self.assertEqual(code, 1)

    def test_run_and_status_refuse_semantically_invalid_state_without_rewriting(self):
        content = (
            b'{"version":2,"target":"local","components":["server"],'
            b'"transport":"stdio","profile":"unified",'
            b'"outcomes":[{"step":"unknown","status":"completed"}]}'
        )
        for command in ("run", "status"):
            with self.subTest(command=command), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "installer-state.json"
                path.write_bytes(content)
                arguments = [command, "--target", "local", "--data-dir", directory]
                if command == "run":
                    arguments.append("--non-interactive")
                code, stdout, stderr = self.run_installer(*arguments)
                self.assertEqual((code, stdout), (2, ""))
                self.assertIn("malformed", stderr)
                self.assertEqual(path.read_bytes(), content)

    def test_run_refuses_a_dangling_state_symlink_without_replacing_it(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "installer-state.json"
            target = Path(directory) / "missing-target"
            path.symlink_to(target)

            code, stdout, stderr = self.run_installer(
                "run", "--target", "local", "--data-dir", directory, "--non-interactive"
            )

            self.assertEqual((code, stdout), (2, ""))
            self.assertIn(f"Cannot read {path}", stderr)
            self.assertTrue(path.is_symlink())
            self.assertFalse(target.exists())

    def test_status_refuses_a_state_path_that_is_a_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "installer-state.json"
            path.mkdir()

            code, stdout, stderr = self.run_installer(
                "status", "--target", "local", "--data-dir", directory
            )

            self.assertEqual((code, stdout), (2, ""))
            self.assertIn(f"Cannot read {path}", stderr)

    def test_status_refuses_invalid_utf8_state_without_a_traceback_or_rewrite(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "installer-state.json"
            path.write_bytes(b"\xff\xfe")

            code, stdout, stderr = self.run_installer(
                "status", "--target", "local", "--data-dir", directory
            )

            self.assertEqual((code, stdout), (2, ""))
            self.assertIn("malformed", stderr)
            self.assertNotIn("Traceback", stderr)
            self.assertEqual(path.read_bytes(), b"\xff\xfe")

    def test_status_without_recorded_state_does_not_create_the_directory(self):
        with tempfile.TemporaryDirectory() as parent:
            data_dir = Path(parent) / "absent"

            code, stdout, stderr = self.run_installer(
                "status", "--target", "local", "--data-dir", str(data_dir)
            )

            self.assertEqual((code, stdout), (2, ""))
            self.assertIn(f"no installation recorded in {data_dir}", stderr)
            self.assertIn("run `run` first", stderr)
            self.assertFalse(data_dir.exists())

    def test_server_only_has_one_ordered_plan(self):
        code, stdout, stderr = self.run_installer("plan", "--target", "local")
        self.assertEqual((code, stderr), (0, ""))
        self.assertEqual(
            json.loads(stdout),
            {
                "target": "local",
                "components": ["server"],
                "stages": ["check_prerequisites", "install", "configure", "verify"],
            },
        )

    def test_removed_targets_and_component_flags_are_not_cli_values(self):
        cases = [
            ("plan", "--target", target) for target in ("aws", "cloud-one", "azure", "gcp", "other")
        ] + [
            ("plan", "--target", "local", flag)
            for flag in ("--with-index", "--with-documentation", "--with-companion")
        ]
        for arguments in cases:
            with self.subTest(arguments=arguments):
                code, stdout, stderr = self.run_installer(*arguments)
                self.assertEqual((code, stdout), (2, ""))
                self.assertTrue("invalid choice" in stderr or "unrecognized arguments" in stderr)

    def test_step_help_is_side_effect_free_and_every_local_stage_is_executable(self):
        with tempfile.TemporaryDirectory():
            environment = os.environ | {
                "PATH": "",
                "NCI_SI_EVS_BASE_URL": "http://127.0.0.1:1",
                "NCI_SI_CADSR_BASE_URL": "http://127.0.0.1:2",
            }
            code, stdout, stderr = self.run_installer(
                "help", "check_prerequisites", "--target", "local", environment=environment
            )

            self.assertEqual((code, stdout, stderr), (0, STEP_HELP, ""))
        for step in ("install", "configure", "verify"):
            with self.subTest(step=step):
                code, stdout, stderr = self.run_installer("help", step, "--target", "local")
                self.assertEqual((code, stderr), (0, ""))
                self.assertTrue(stdout.startswith(f"Step: {step}\n"))
                self.assertIn("Purpose:", stdout)
                self.assertIn("Verification:", stdout)
        code, stdout, stderr = self.run_installer("help", "unknown", "--target", "local")
        self.assertEqual((code, stdout), (2, ""))
        self.assertIn("not executable", stderr)

    def test_help_is_exact_and_ignores_forced_colour(self):
        for arguments, expected in (
            (("--help",), ROOT_HELP),
            (("plan", "--help"), PLAN_HELP),
            (("check", "--help"), CHECK_HELP),
            (("run", "--help"), RUN_HELP),
            (("status", "--help"), STATUS_HELP),
        ):
            with self.subTest(arguments=arguments):
                code, stdout, stderr = self.run_installer(*arguments, force_color="1")

                self.assertEqual((code, stdout, stderr), (0, expected, ""))

    def test_the_installed_distribution_exposes_the_installer_command(self):
        self.assertTrue(INSTALLER.is_file())
