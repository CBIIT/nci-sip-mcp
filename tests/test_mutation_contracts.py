"""Caller-visible edge contracts left uncovered by the gate's subprocess tests."""

import ast
import hashlib
import io
import itertools
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, suppress
from dataclasses import asdict
from pathlib import Path
from time import monotonic, sleep
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import yaml
from coverage import CoverageData
from scripts.validation import mutation_gate as gate
from scripts.validation import mutation_probe as probe
from scripts.validation import mutation_run as runner
from scripts.validation import mutation_source as source

from test_mutation_gate import cli, command, project


def git(root, *args):
    return subprocess.check_output(  # noqa: S603 - fixed git commands, owned fixture
        [  # noqa: S607 - fixed git executable
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.org",
            *args,
        ],
        text=True,
        stderr=subprocess.PIPE,
    ).strip()


def result_key(row):
    return json.dumps(
        {
            name: row[name]
            for name in ("file", "qualname", "operator", "site", "context", "original", "mutated")
        },
        sort_keys=True,
    )


def runner_seconds(rows):
    return sum(domain["elapsed_seconds"] for row in rows for domain in row["domains"])


def seconds_by_domain(rows):
    return {
        name: sum(
            domain["elapsed_seconds"]
            for row in rows
            for domain in row["domains"]
            if domain["domain"] == name
        )
        for name in ("unit", "fixture")
    }


def subject_pytest(root, sink=True):
    env = dict(os.environ)
    env.pop("MUTATION_EVIDENCE", None)
    if sink:
        env["MUTATION_EVIDENCE"] = str(root / "subject.jsonl")
    return subprocess.run(  # noqa: S603 - real subject plugin, owned offline fixture
        [
            sys.executable,
            "-c",
            f"import sys; sys.path.insert(0, {str(Path(gate.__file__).parents[2])!r}); "
            "import pytest; raise SystemExit(pytest.main(sys.argv[1:]))",
            "-p",
            "no:xdist",
            "-p",
            "scripts.validation.mutation_probe",
            "--no-cov",
            "-q",
            "tests",
        ],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def events(root):
    return [json.loads(line) for line in (root / "subject.jsonl").read_text().splitlines()]


def wait_for(predicate, seconds=15):
    deadline = monotonic() + seconds
    while not predicate():
        if monotonic() >= deadline:
            return False
        sleep(0.02)
    return True


def running(pid):
    result = subprocess.run(  # noqa: S603 - owned PID, read-only process status
        ["ps", "-o", "stat=", "-p", str(pid)],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0 and not result.stdout.strip().startswith("Z")


def terminate_group(pid):
    with suppress(ProcessLookupError):
        os.killpg(pid, signal.SIGKILL)


def malformed_entries(entry):
    shapes = [None, 42, "entry", list(entry), {**entry, "extra": "bad"}]
    shapes.extend(
        {key: value for key, value in entry.items() if key != missing} for missing in entry
    )
    return [(json.dumps([shape]), "Malformed allowlist entry fields") for shape in shapes]


class MutationCliContractsTest(unittest.TestCase):
    def test_seed_controls_actual_job_launch_order_without_changing_results(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            project(root, "def answer():\n    return True\n")
            marker = root / "launches.jsonl"
            (root / "tests/test_example.py").write_text(
                "import json, example\nfrom pathlib import Path\ndef test_value():\n"
                "    value = example.answer()\n"
                f"    with Path({str(marker)!r}).open('a') as stream:\n"
                "        stream.write(json.dumps(value) + '\\n')\n"
                "    assert value is True\n"
            )
            results = []
            for seed, expected in (
                (0, [True, None, False, False]),
                (1, [True, False, False, None]),
            ):
                with self.subTest(seed=seed):
                    marker.unlink(missing_ok=True)
                    status, rows = command(
                        root, "--paths", "src/example.py", "--workers", "1", "--seed", str(seed)
                    )
                    self.assertEqual(status, 0, rows)
                    self.assertEqual(
                        [json.loads(line) for line in marker.read_text().splitlines()], expected
                    )
                    results.append(
                        [(result_key(row), row["outcome"]) for row in rows if "site" in row]
                    )
            self.assertEqual(results[0], results[1])

    def test_sigterm_unwinds_real_gate_and_cleans_owned_children(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            project(root, "def answer():\n    return True\n")
            marker = root / "started.json"
            (root / "tests/test_example.py").write_text(
                "import json, os, signal\nfrom pathlib import Path\ndef test_value():\n"
                f"    Path({str(marker)!r}).write_text(json.dumps({{'pid': os.getpid(), "
                "'group': os.getpgrp(), 'scratch': str(Path.cwd().parent)}))\n"
                "    signal.pause()\n"
            )
            env = dict(os.environ)
            with (root / "controller.log").open("w") as log:
                controller = subprocess.Popen(  # noqa: S603 - owned controller and session
                    [
                        sys.executable,
                        "-m",
                        "scripts.validation.mutation_gate",
                        "--root",
                        str(root),
                        "--paths",
                        "src/example.py",
                    ],
                    cwd=Path(gate.__file__).parents[2],
                    env=env,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
                owned = None
                try:
                    self.assertTrue(wait_for(marker.exists), (root / "controller.log").read_text())
                    owned = json.loads(marker.read_text())
                    self.assertNotEqual(owned["group"], os.getpgrp())
                    self.assertEqual(owned["pid"], owned["group"])
                    controller.send_signal(signal.SIGTERM)
                    controller.wait(timeout=30)
                    self.assertTrue(wait_for(lambda: not running(owned["pid"])))
                    self.assertFalse(Path(owned["scratch"]).exists())
                    self.assertNotEqual(controller.returncode, 0)
                finally:
                    if controller.poll() is None:
                        terminate_group(controller.pid)
                    if owned:
                        terminate_group(owned["group"])
                    controller.wait(timeout=15)

    def test_scheduler_failure_retains_completed_keys_and_marks_each_missing_job_infra(self):
        # Given one broken controller job, the remaining completed evidence must
        # survive the scheduler's nonzero exit, with an explicit missing-job record.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            project(root, "def answer():\n    return True\n")
            (root / "broken_job.py").write_text(
                "import pytest\n@pytest.hookimpl(tryfirst=True)\n"
                "def pytest_runtest_call(item):\n"
                "    if item.name == '0':\n        raise RuntimeError('controller defect')\n"
            )
            mutants, _ = source.candidates("src/example.py", (root / "src/example.py").read_bytes())
            mapping = {
                "unit": {"src/example.py": {2: ["tests/test_example.py::test_value|run"]}},
                "selftest": {"src/example.py": {2: [""]}},
                "fixture": {"src/example.py": {2: [""]}},
            }

            def launch(command, cwd, env, timeout, log, **options):
                return runner.program(
                    [*command, "-p", "broken_job"], cwd, env, timeout, log, **options
                )

            with (
                patch.object(gate, "program", side_effect=launch),
                self.assertRaises(ValueError) as failed,
            ):
                gate.schedule(root, mutants, mapping, SimpleNamespace(timeout=20, workers=1))
            rows = getattr(failed.exception, "rows", [])
            self.assertEqual(len(rows), len(mutants))
            self.assertEqual(
                [result_key(row) for row in rows],
                [json.dumps(m.key(), sort_keys=True) for m in mutants],
            )
            self.assertEqual([row["outcome"] for row in rows], ["INFRA", "KILLED", "KILLED"])
            self.assertFalse(rows[0]["restored"])
            self.assertEqual(rows[0]["message"], "Controller produced no result for this candidate")
            self.assertEqual(rows[0]["domains"], [])
            self.assertEqual(rows[0]["fallback_domains"], ["fixture", "selftest"])
            self.assertIn("controller defect", rows[0]["log"])
            self.assertTrue(all(row["restored"] for row in rows[1:]))

    def test_summary_counts_fallback_mutants_by_domain_and_measured_runner_hours(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            project(
                root,
                "flag = True\ndef answer():\n    return True\ndef unused():\n    return True\n",
            )
            status, rows = command(root, "--paths", "src/example.py", "--shard", "1/1")
            results = [row for row in rows if "site" in row]
            self.assertEqual(status, 1, rows)
            for row in results:
                self.assertEqual(
                    row.get("fallback_domains"), ["fixture"] if row["line"] == 1 else []
                )
            fallback_count = sum(row["line"] == 1 for row in results)
            self.assertGreater(fallback_count, 0)
            self.assertEqual(rows[-1].get("fallback_count"), fallback_count)
            self.assertEqual(
                rows[-1].get("fallback_domains"),
                {"fixture": fallback_count},
            )
            self.assertGreater(rows[-1]["counts"]["NO_TESTS"], 0)
            seconds = runner_seconds(results)
            self.assertEqual(rows[-1].get("runner_seconds"), seconds)
            self.assertEqual(rows[-1].get("runner_hours"), seconds / 3600)
            self.assertEqual(
                rows[-1].get("runner_seconds_by_domain"),
                seconds_by_domain(results),
            )
            self.assertEqual(
                set(rows[-1]),
                {
                    "outcome",
                    "shard",
                    "full_mutants",
                    "shard_mutants",
                    "counts",
                    "kill_kinds",
                    "baseline_domains",
                    "fallback_count",
                    "fallback_domains",
                    "runner_seconds",
                    "runner_seconds_by_domain",
                    "runner_hours",
                    "elapsed_seconds",
                },
            )
            self.assertIsInstance(rows[-1]["elapsed_seconds"], float)
            self.assertGreaterEqual(rows[-1]["elapsed_seconds"], 0)

    def test_preexisting_fixture_failure_stops_the_gate_before_any_mutant_is_credited(self):
        # Given clean unit/self-test suites and an already failing fixture test,
        # the CLI must reject the baseline rather than credit that failure as a kill.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            project(
                root,
                "def answer():\n    return True\n",
                "assert example.answer() is not Ellipsis",
                "assert 1 == 2",
            )
            status, rows = command(root, "--paths", "src/example.py")
            self.assertEqual(status, 2, rows)
            self.assertEqual([row["outcome"] for row in rows], ["NO_OP", "INFRA"])
            self.assertIn("Baseline fixture failed:", rows[-1]["message"])
            self.assertIn("'outcome': 'INFRA'", rows[-1]["message"])
            self.assertIn("tests/test_contract.py::test_contract", rows[-1]["message"])
            self.assertEqual(
                (root / "src/example.py").read_text(), "def answer():\n    return True\n"
            )

    def test_seed_and_comment_line_shifts_preserve_shard_membership_across_processes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            text = "def answer():\n    return True\n"
            project(root, text)
            first_status, first = command(root, "--paths", "src/example.py", "--shard", "1/3")
            (root / "src/example.py").write_text("# moved\n\n" + text)
            second_status, second = command(
                root, "--paths", "src/example.py", "--shard", "1/3", "--seed", "79"
            )
            self.assertEqual((first_status, second_status), (0, 0), (first, second))
            self.assertEqual(
                [result_key(row) for row in first if "site" in row],
                [result_key(row) for row in second if "site" in row],
            )
            self.assertEqual(first[1], second[1])

    def test_shard_union_covers_every_stable_key_once_and_preserves_survivor_verdict(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            text = "def answer():\n    return True\n"
            project(root, text, "assert example.answer() is not Ellipsis")
            mutants, _ = source.candidates("src/example.py", text.encode())
            expected = {json.dumps(m.key(), sort_keys=True) for m in mutants}
            observed = []
            for index in (1, 2, 3):
                status, rows = command(root, "--paths", "src/example.py", "--shard", f"{index}/3")
                self.assertEqual(status, 1, rows)
                catalogue = rows[1]
                results = [row for row in rows if "site" in row]
                self.assertEqual(
                    catalogue,
                    {
                        "outcome": "CATALOGUE",
                        "shard": f"{index}/3",
                        "full_mutants": len(mutants),
                        "shard_mutants": len(results),
                    },
                )
                for row in results:
                    key = result_key(row)
                    observed.append(key)
                    self.assertEqual(
                        int(hashlib.sha256(key.encode()).hexdigest(), 16) % 3 + 1, index
                    )
                summary = rows[-1]
                self.assertEqual(summary["outcome"], "SUMMARY")
                self.assertEqual(summary["counts"], {"SURVIVED": len(results)})
                self.assertEqual(summary["kill_kinds"], {"assertion": 0, "error": 0, "import": 0})
                self.assert_clean_baseline_timings(summary)
            self.assertEqual(set(observed), expected)
            self.assertEqual(len(observed), len(expected))

    def assert_clean_baseline_timings(self, summary):
        self.assertIn("baseline_domains", summary)
        self.assertEqual(
            [(row["domain"], row["outcome"]) for row in summary["baseline_domains"]],
            [(domain, "SURVIVED") for domain in ("unit", "selftest", "fixture")],
        )
        for row in summary["baseline_domains"]:
            self.assertIsInstance(row["elapsed_seconds"], float)

    def test_invalid_shards_are_usage_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            project(root, "# no mutation sites\n")
            for value in ("0/3", "4/3", "1/0", "-1/3", "1", "a/3", "1/2/3"):
                with self.subTest(value=value):
                    result = cli(root, "--paths", "src/example.py", f"--shard={value}")
                    self.assertEqual(result.returncode, 2)
                    self.assertEqual(
                        result.stderr.splitlines()[-1],
                        "python -m scripts.validation.mutation_gate: error: argument --shard: "
                        "expected K/N with 1 <= K <= N",
                    )

    def test_empty_shard_reports_full_count_and_stale_entries_still_fail(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            text = "def answer():\n    return True\n"
            project(root, text)
            mutants, _ = source.candidates("src/example.py", text.encode())
            occupied = {
                int(source.digest(json.dumps(m.key(), sort_keys=True)), 16) % 100 + 1
                for m in mutants
            }
            empty = next(index for index in range(1, 101) if index not in occupied)
            status, rows = command(root, "--paths", "src/example.py", "--shard", f"{empty}/100")
            self.assertEqual(status, 0, rows)
            self.assertEqual(
                rows[-1],
                {
                    "outcome": "EMPTY",
                    "mutants": 0,
                    "shard": f"{empty}/100",
                    "full_mutants": 3,
                    "shard_mutants": 0,
                },
            )
            entry = {**mutants[0].key(), "context": "stale", "reason": "Reviewed equivalent."}
            (root / "allow.yaml").write_text(yaml.safe_dump([entry]))
            status, rows = command(
                root,
                "--paths",
                "src/example.py",
                "--shard",
                f"{empty}/100",
                "--allowlist",
                "allow.yaml",
            )
            self.assertEqual(status, 2)
            self.assertIn("Stale", rows[-1]["message"])

    def test_real_main_reports_every_kill_and_returns_the_scheduler_verdict(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            project(root, "def answer():\n    return True\n")
            status, rows = command(
                root,
                "--paths",
                "src/example.py",
                "--seed",
                "17",
                "--allowlist",
                "missing.yaml",
                "--workers",
                "1",
            )
            self.assertEqual(status, 0, rows)
            self.assertEqual(
                [(row["operator"], row["outcome"]) for row in rows],
                [
                    ("return:True", "NO_OP"),
                    ("boolean:literal", "KILLED"),
                    ("return:False", "KILLED"),
                    ("return:None", "KILLED"),
                ],
            )
            self.assertEqual(
                {row["node"] for row in rows if row["outcome"] == "KILLED"},
                {"tests/test_example.py::test_value"},
            )
            project(
                root, "def answer():\n    return True\n", "assert example.answer() is not Ellipsis"
            )
            status, rows = command(root, "--paths", "src/example.py")
            self.assertEqual(status, 1, rows)
            self.assertEqual(
                [row["outcome"] for row in rows], ["NO_OP", "SURVIVED", "SURVIVED", "SURVIVED"]
            )
            project(root, "def answer():\n    return True\n")
            status, rows = command(root, "--paths", "src/example.py", "--shard", "1/1")
            self.assertEqual(status, 0, rows)
            self.assertEqual(rows[-1]["kill_kinds"], {"assertion": 3, "error": 0, "import": 0})
            project(root, "def answer():\n    return True\n", "example.answer() + 1")
            status, rows = command(root, "--paths", "src/example.py", "--shard", "1/1")
            self.assertEqual(status, 1, rows)
            self.assertEqual(rows[-1]["kill_kinds"], {"assertion": 0, "error": 1, "import": 0})
            project(
                root,
                "flag = True\nif not flag:\n    raise TypeError('invalid table')\n"
                "def answer():\n    return flag\n",
            )
            status, rows = command(root, "--paths", "src/example.py", "--shard", "1/1")
            self.assertEqual(status, 1, rows)
            self.assertEqual(rows[-1]["kill_kinds"], {"assertion": 2, "error": 0, "import": 2})

    def test_nonpositive_execution_limits_are_usage_errors(self):
        # Given invalid CLI limits, no baseline may start; argparse owns exit 2.
        for option in ("--timeout", "--baseline-timeout", "--workers"):
            for value in ("0", "-1"):
                with self.subTest(option=option, value=value):
                    result = cli(Path.cwd(), option, value)
                    self.assertEqual(result.returncode, 2)
                    self.assertEqual(
                        result.stderr.splitlines()[-1],
                        "python -m scripts.validation.mutation_gate: error: "
                        "timeout and workers must be positive",
                    )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project(root, "# no sites\n")
            for option in ("--timeout", "--baseline-timeout"):
                self.assertEqual(
                    command(root, "--paths", "src/example.py", option, "1"),
                    (0, [{"outcome": "EMPTY", "mutants": 0}]),
                )

    def test_legitimate_empty_module_succeeds_without_starting_baselines(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project(root, "# no executable mutation sites\n")
            self.assertEqual(
                command(root, "--paths", "src/example.py"),
                (0, [{"outcome": "EMPTY", "mutants": 0}]),
            )

    def test_invalid_module_and_syntax_are_machine_readable_infrastructure_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project(root, "def broken(:\n")
            (root / "src/folder.py").mkdir()
            for path, message in (
                ("tests/test_example.py", "Not an eligible Python module: tests/test_example.py"),
                ("src/example.py", "invalid syntax (<unknown>, line 1)"),
                ("src/missing.py", "Not an eligible Python module: src/missing.py"),
                ("src/folder.py", "Not an eligible Python module: src/folder.py"),
            ):
                with self.subTest(path=path):
                    self.assertEqual(
                        command(root, "--paths", path),
                        (2, [{"outcome": "INFRA", "message": message}]),
                    )

    def test_scheduler_crash_raises_instead_of_returning_partial_results(self):
        def crash(command, cwd, env, timeout, log, **options):
            log.write_text("worker exited 9")
            return 9

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project(root, "def answer():\n    return True\n")
            mutants, _ = source.candidates("src/example.py", (root / "src/example.py").read_bytes())
            with (
                patch.object(gate, "program", side_effect=crash),
                self.assertRaises(gate.SchedulerError) as failed,
            ):
                gate.schedule(root, mutants, {}, SimpleNamespace(timeout=20, workers=1))
            self.assertEqual(str(failed.exception), "Mutant scheduler failed: worker exited 9")
            self.assertEqual(len(failed.exception.rows), len(mutants))

    def test_scheduler_os_failure_preserves_completed_and_missing_rows_and_cause(self):
        # Given a completed job before an OS wait failure, the caller still gets
        # that evidence and one explicit INFRA row for every unfinished job.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            project(root, "def answer():\n    return True\n")
            mutants, _ = source.candidates("src/example.py", (root / "src/example.py").read_bytes())
            completed = {**asdict(mutants[0]), "outcome": "KILLED", "restored": True}
            error = OSError("scheduler wait denied")

            def interrupted(command, cwd, env, timeout, log, **options):
                (cwd / "result-0.json").write_text(json.dumps(completed))
                raise error

            with (
                patch.object(gate, "program", side_effect=interrupted),
                self.assertRaises(gate.SchedulerError) as failed,
            ):
                gate.schedule(root, mutants, {}, SimpleNamespace(timeout=20, workers=1))
            self.assertEqual(
                str(failed.exception), "Mutant scheduler failed: scheduler wait denied"
            )
            self.assertIs(failed.exception.__cause__, error)
            self.assertEqual(failed.exception.rows[0], completed)
            self.assertEqual(
                failed.exception.rows[1:],
                [
                    {
                        **asdict(mutant),
                        "outcome": "INFRA",
                        "message": "Controller produced no result for this candidate",
                        "log": "scheduler wait denied",
                        "restored": False,
                        "domains": [],
                        "fallback_domains": [],
                    }
                    for mutant in mutants[1:]
                ],
            )

    def test_scheduler_rejects_nonzero_exit_and_unrestored_rows_independently(self):
        # Either controller failure or unverified restoration invalidates an
        # otherwise complete result set; a zero exit with restored rows is valid.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            project(root, "flag = True\n")
            mutants, _ = source.candidates("src/example.py", b"flag = True\n")
            for status, restored in ((9, True), (0, False), (0, True)):
                with self.subTest(status=status, restored=restored):
                    row = {**asdict(mutants[0]), "outcome": "KILLED", "restored": restored}

                    def completed(
                        command, cwd, env, timeout, log, row=row, status=status, **options
                    ):
                        (cwd / "result-0.json").write_text(json.dumps(row))
                        log.write_text("controller verdict")
                        return status

                    with patch.object(gate, "program", side_effect=completed):
                        self.assert_scheduler_verdict(root, mutants, status, restored, row)

    def assert_scheduler_verdict(self, root, mutants, status, restored, row):
        if status == 0 and restored:
            self.assertEqual(
                gate.schedule(root, mutants, {}, SimpleNamespace(timeout=20, workers=1)), [row]
            )
            return
        with self.assertRaises(gate.SchedulerError) as failed:
            gate.schedule(root, mutants, {}, SimpleNamespace(timeout=20, workers=1))
        self.assertEqual(str(failed.exception), "Mutant scheduler failed: controller verdict")
        self.assertEqual(failed.exception.rows, [row])

    def test_cli_scheduler_failure_reports_all_rows_summary_and_terminal_infra(self):
        # Given a controller that loses one job, the real CLI must retain all
        # candidate rows and SUMMARY before its exact terminal diagnostic/exit 2.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            project(root, "def answer():\n    return True\n")
            package = Path(gate.__file__).parents[2]
            driver = root / "driver.py"
            driver.write_text(
                "import sys\nfrom pathlib import Path\nfrom unittest.mock import patch\n"
                f"sys.path.insert(0, {str(package)!r})\n"
                "from scripts.validation import mutation_gate as gate\n"
                "def controller(command, cwd, env, timeout, log, **options):\n"
                "    gate.execute_job(Path(command[-1]), 1)\n"
                "    gate.execute_job(Path(command[-1]), 2)\n"
                "    log.write_text('controller lost job')\n    return 9\n"
                "with patch.object(gate, 'program', side_effect=controller):\n"
                "    raise SystemExit(gate.main(sys.argv[1:]))\n"
            )
            child = subprocess.run(  # noqa: S603 - real CLI with OS-boundary fault injection
                [
                    sys.executable,
                    str(driver),
                    "--root",
                    str(root),
                    "--paths",
                    "src/example.py",
                    "--shard",
                    "1/1",
                ],
                cwd=root,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            rows = [json.loads(line) for line in child.stdout.splitlines()]
            results = [row for row in rows if "site" in row]
            mutants, _ = source.candidates("src/example.py", (root / "src/example.py").read_bytes())
            self.assertEqual(child.returncode, 2, child.stdout + child.stderr)
            self.assertCountEqual(
                [result_key(row) for row in results],
                [json.dumps(m.key(), sort_keys=True) for m in mutants],
            )
            self.assertEqual(
                sorted(row["outcome"] for row in results), ["INFRA", "KILLED", "KILLED"]
            )
            self.assertEqual(rows[-2]["outcome"], "SUMMARY")
            self.assertEqual(rows[-2]["counts"], {"INFRA": 1, "KILLED": 2})
            self.assertEqual(
                rows[-1],
                {"outcome": "INFRA", "message": "Mutant scheduler failed: controller lost job"},
            )


class MutationSourceContractsTest(unittest.TestCase):
    def test_scope_excludes_each_nonproduction_path_family(self):
        # Given parseable files, explicit scope rejects each excluded family alone.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            project(root, "# allowed empty module\n")
            excluded = (
                "docs/helper.py",
                "src/helper.txt",
                "src/test_helper.py",
                "src/tests/helper.py",
                "src/selftests/helper.py",
                "src/generated/helper.py",
                "src/__pycache__/helper.py",
            )
            for name in excluded:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("flag = True\n")
                with self.subTest(path=name):
                    self.assertEqual(
                        command(root, "--paths", name),
                        (
                            2,
                            [
                                {
                                    "outcome": "INFRA",
                                    "message": f"Not an eligible Python module: {name}",
                                }
                            ],
                        ),
                    )
            self.assertEqual(
                command(root, "--paths", "src/example.py"),
                (0, [{"outcome": "EMPTY", "mutants": 0}]),
            )

    def test_statement_operators_preserve_docstrings_and_noncall_expressions(self):
        text = (
            b'"module doc"\ndef f():\n    "function doc"\n    42\n    return\n'
            b"def g():\n    ping()\n    return True\n"
        )
        mutants, diagnostics = source.candidates("src/example.py", text)
        self.assertEqual(
            [(m.line, m.operator, m.after) for m in mutants],
            [
                (7, "call:remove", "pass"),
                (8, "return:None", "return None"),
                (8, "return:False", "return False"),
                (8, "boolean:literal", "False"),
            ],
        )
        self.assertEqual(
            diagnostics,
            [{"file": "src/example.py", "line": 8, "operator": "return:True", "outcome": "NO_OP"}],
        )

    def test_unary_removal_is_only_for_logical_not(self):
        for expression, expected in (
            ("+x", []),
            ("-x", []),
            ("~x", []),
            ("not x", [("not:remove", "(x)")]),
        ):
            with self.subTest(expression=expression):
                mutants, diagnostics = source.candidates(
                    "src/example.py", f"value = {expression}\n".encode()
                )
                self.assertEqual([(m.operator, m.after) for m in mutants], expected)
                self.assertEqual(diagnostics, [])

    def test_comparison_and_boolean_catalogues_have_exact_declared_replacements(self):
        comparisons = {
            "a == b": {"comparison:0": "(a) != (b)", "comparison:negate": "not (a == b)"},
            "a != b": {"comparison:0": "(a) == (b)", "comparison:negate": "not (a != b)"},
            "a < b": {"comparison:0": "(a) >= (b)", "comparison:negate": "not (a < b)"},
            "a <= b": {"comparison:0": "(a) > (b)", "comparison:negate": "not (a <= b)"},
            "a > b": {"comparison:0": "(a) <= (b)", "comparison:negate": "not (a > b)"},
            "a >= b": {"comparison:0": "(a) < (b)", "comparison:negate": "not (a >= b)"},
            "a is b": {"comparison:0": "(a) is not (b)", "comparison:negate": "not (a is b)"},
            "a is not b": {"comparison:0": "(a) is (b)", "comparison:negate": "not (a is not b)"},
            "a in b": {"comparison:0": "(a) not in (b)", "comparison:negate": "not (a in b)"},
            "a not in b": {"comparison:0": "(a) in (b)", "comparison:negate": "not (a not in b)"},
            "a < b <= c": {
                "comparison:0": "(a) >= (b) <= (c)",
                "comparison:1": "(a) < (b) > (c)",
                "comparison:negate": "not (a < b <= c)",
            },
            "a and b and c": {
                "boolean:swap": "(a) or (b) or (c)",
                "boolean:drop:0": "(b) and (c)",
                "boolean:drop:1": "(a) and (c)",
                "boolean:drop:2": "(a) and (b)",
            },
            "a or b or c": {
                "boolean:swap": "(a) and (b) and (c)",
                "boolean:drop:0": "(b) or (c)",
                "boolean:drop:1": "(a) or (c)",
                "boolean:drop:2": "(a) or (b)",
            },
        }
        for expression, expected in comparisons.items():
            with self.subTest(expression=expression):
                mutants, diagnostics = source.candidates(
                    "src/example.py", f"value = {expression}\n".encode()
                )
                self.assertEqual({m.operator: m.after for m in mutants}, expected)
                self.assertEqual(diagnostics, [])

    def test_boolean_operand_drops_have_the_declared_truth_tables(self):
        truth = {
            "a and b and c": {
                "boolean:swap": [False, True, True, True, True, True, True, True],
                "boolean:drop:0": [False, False, False, True, False, False, False, True],
                "boolean:drop:1": [False, False, False, False, False, True, False, True],
                "boolean:drop:2": [False, False, False, False, False, False, True, True],
            },
            "a or b or c": {
                "boolean:swap": [False, False, False, False, False, False, False, True],
                "boolean:drop:0": [False, True, True, True, False, True, True, True],
                "boolean:drop:1": [False, True, False, True, True, True, True, True],
                "boolean:drop:2": [False, False, True, True, True, True, True, True],
            },
        }
        for expression, operators in truth.items():
            mutants, _ = source.candidates("src/example.py", f"value = {expression}".encode())
            for mutant in mutants:
                namespace = {}
                exec(f"def variant(a, b, c):\n    return {mutant.after}", namespace)  # noqa: S102
                self.assertEqual(
                    [
                        namespace["variant"](*args)
                        for args in itertools.product((False, True), repeat=3)
                    ],
                    operators[mutant.operator],
                )

    def test_comparison_replacements_preserve_untargeted_chain_links(self):
        same, equal, different = [], [], [1]
        numbers = ((1, 2, 3), (2, 2, 3), (3, 2, 3), (1, 3, 2))
        identities = (
            (same, same, same),
            (same, same, equal),
            (same, equal, equal),
            (same, equal, different),
        )
        members = ((1, [1], None), (2, [1], None), (1, [], None), (2, [2], None))
        comparison_truth = {
            "a < b": (
                numbers,
                {
                    "comparison:0": [False, True, True, False],
                    "comparison:negate": [False, True, True, False],
                },
            ),
            "a < b <= c": (
                numbers,
                {
                    "comparison:0": [False, True, True, False],
                    "comparison:1": [False, False, False, True],
                    "comparison:negate": [False, True, True, True],
                },
            ),
            "a is b": (
                identities,
                {
                    "comparison:0": [False, False, True, True],
                    "comparison:negate": [False, False, True, True],
                },
            ),
            "a is not b": (
                identities,
                {
                    "comparison:0": [True, True, False, False],
                    "comparison:negate": [True, True, False, False],
                },
            ),
            "a is b is not c": (
                identities,
                {
                    "comparison:0": [False, False, False, True],
                    "comparison:1": [True, False, False, False],
                    "comparison:negate": [True, False, True, True],
                },
            ),
            "a in b": (
                members,
                {
                    "comparison:0": [False, True, True, False],
                    "comparison:negate": [False, True, True, False],
                },
            ),
            "a not in b": (
                members,
                {
                    "comparison:0": [True, False, False, True],
                    "comparison:negate": [True, False, False, True],
                },
            ),
            "a in b not in c": (
                ((1, [1], [[1]]), (1, [1], []), (2, [1], [[1]]), (2, [1], [])),
                {
                    "comparison:0": [False, False, False, True],
                    "comparison:1": [True, False, False, False],
                    "comparison:negate": [True, False, True, True],
                },
            ),
            "0 < a <= 3": (
                ((-1, 0, 0), (0, 0, 0), (2, 0, 0), (4, 0, 0)),
                {
                    "comparison:0": [True, True, False, False],
                    "comparison:1": [False, False, False, True],
                    "comparison:negate": [True, True, False, True],
                },
            ),
        }
        for expression, (arguments, operators) in comparison_truth.items():
            mutants, _ = source.candidates("src/example.py", f"value = {expression}".encode())
            for mutant in mutants:
                if not mutant.operator.startswith("comparison:"):
                    continue
                namespace = {}
                exec(f"def variant(a, b, c):\n    return {mutant.after}", namespace)  # noqa: S102
                self.assertEqual(
                    [namespace["variant"](*args) for args in arguments],
                    operators[mutant.operator],
                )

    def test_only_exact_module_entry_point_comparisons_are_rejected(self):
        for comparison in ("__name__ == '__main__'", "'__main__' == __name__"):
            with self.subTest(comparison=comparison):
                snippet = (
                    f"if {comparison}:\n    launch()\nelse:\n    fallback()\n"
                    f"def inner(value=1):\n    if {comparison}:\n        return value == 1\n"
                )
                mutants, diagnostics = source.candidates("scripts/example.py", snippet.encode())
                self.assertEqual([m.operator for m in mutants if m.line == 1], [])
                self.assertEqual(
                    [row for row in diagnostics if row.get("reason") == "entry-point guard"],
                    [
                        {
                            "file": "scripts/example.py",
                            "line": 1,
                            "operator": operator,
                            "outcome": "REJECTED",
                            "reason": "entry-point guard",
                        }
                        for operator in ("comparison:0", "comparison:negate")
                    ],
                )

    def test_guard_body_else_and_nested_name_checks_remain_candidates(self):
        mutants, _ = source.candidates(
            "scripts/example.py",
            b"if __name__ == '__main__':\n    launch()\nelse:\n    fallback()\n"
            b"def inner():\n    if __name__ == '__main__':\n        return True\n",
        )
        self.assertEqual(
            {(m.line, m.operator) for m in mutants if m.operator == "call:remove"},
            {(2, "call:remove"), (4, "call:remove")},
        )
        self.assertEqual(
            {m.qualname for m in mutants if m.before == "__name__ == '__main__'"}, {"inner"}
        )

    def test_other_module_name_comparisons_remain_candidates(self):
        for comparison in (
            "__name__ != '__main__'",
            "name == '__main__'",
            "__name__ == 'other'",
            "__name__ == '__main__' == value",
        ):
            with self.subTest(comparison=comparison):
                mutants, diagnostics = source.candidates(
                    "scripts/example.py", f"if {comparison}:\n    launch()\n".encode()
                )
                self.assertTrue([m for m in mutants if m.line == 1])
                self.assertEqual(diagnostics, [])

    def test_git_snapshot_keeps_history_and_dirty_bytes_in_an_independent_object_database(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            project(root, "flag = True\n")
            git(root, "init", "-q")
            git(root, "add", ".")
            git(root, "commit", "-qm", "initial")
            head = git(root, "rev-parse", "HEAD")
            (root / "src/example.py").write_text("flag = False\n")
            (root / "untracked.txt").write_text("untracked")
            with runner.snapshot(root) as scratch:
                self.assertEqual(git(scratch, "rev-parse", "HEAD"), head)
                self.assertEqual((scratch / "src/example.py").read_text(), "flag = False\n")
                self.assertEqual((scratch / "untracked.txt").read_text(), "untracked")
                original_object = root / ".git/objects" / head[:2] / head[2:]
                copied_object = scratch / ".git/objects" / head[:2] / head[2:]
                self.assertNotEqual(original_object.stat().st_ino, copied_object.stat().st_ino)
                git(scratch, "add", ".")
                git(scratch, "commit", "-qm", "scratch only")
            self.assertFalse(scratch.exists())
            self.assertEqual(git(root, "rev-parse", "HEAD"), head)
            self.assertEqual(
                git(root, "status", "--porcelain"), "M src/example.py\n?? untracked.txt"
            )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            project(root, "flag = True\n")
            with runner.snapshot(root) as scratch:
                self.assertEqual(git(scratch, "rev-parse", "--show-toplevel"), str(scratch))
                self.assertFalse((root / ".git").exists())
            self.assertFalse(scratch.exists())

    def test_snapshot_git_failures_keep_cli_output_structured(self):
        real_git = shutil.which("git")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            project(root, "flag = True\n")
            binary = root / "bin"
            binary.mkdir()
            wrapper = binary / "git"
            for operation in ("init", "clone"):
                with self.subTest(operation=operation):
                    if operation == "clone":
                        git(root, "init", "-q")
                    wrapper.write_text(
                        f"#!{sys.executable}\nimport os, sys\n"
                        f"if sys.argv[1] == {operation!r}:\n"
                        "    sys.stderr.write('PRIVATE_GIT_DIAGNOSTIC\\n')\n"
                        "    raise SystemExit(43)\n"
                        f"os.execv({real_git!r}, [{real_git!r}, *sys.argv[1:]])\n"
                    )
                    wrapper.chmod(0o755)
                    with patch.dict(
                        os.environ, {"PATH": str(binary) + os.pathsep + os.environ["PATH"]}
                    ):
                        result = cli(root, "--paths", "src/example.py")
                    rows = [json.loads(line) for line in result.stdout.splitlines()]
                    self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                    self.assertEqual([row["outcome"] for row in rows], ["INFRA"])
                    self.assertIn("returned non-zero exit status 43", rows[0]["message"])
                    self.assertNotIn("PRIVATE_GIT_DIAGNOSTIC", result.stderr)

    def test_broken_git_snapshot_fails_without_yielding_a_historyless_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project(root, "flag = True\n")
            (root / ".git").write_text("gitdir: absent\n")
            with self.assertRaises(subprocess.CalledProcessError), runner.snapshot(root):
                self.fail("An invalid repository must not yield a snapshot")
            self.assertEqual((root / "src/example.py").read_text(), "flag = True\n")

    def test_invalid_allowlist_shapes_and_values_fail_closed_at_the_cli(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            project(root, "def answer():\n    return True\n")
            entry = {
                **source.candidates("src/example.py", b"flag = True\n")[0][0].key(),
                "reason": "Reviewed equivalent.",
            }
            cases = [
                ("[", f"Malformed allowlist YAML: {root / 'allow.yaml'}"),
                ("{}", "Allowlist must be a YAML list"),
            ]
            cases.extend(malformed_entries(entry))
            cases.extend(
                (
                    json.dumps([{**entry, "reason": value}]),
                    "Allowlist values must be nonempty single-line strings",
                )
                for value in (None, 42, "", "  ", "two\nlines")
            )
            cases.extend(
                (json.dumps([{**entry, "file": name}]), f"Invalid allowlist path: {name}")
                for name in ("tests/test_example.py", "src/../../outside.py")
            )
            for content, message in cases:
                with self.subTest(content=content):
                    (root / "allow.yaml").write_text(content)
                    self.assertEqual(
                        command(root, "--paths", "src/example.py", "--allowlist", "allow.yaml"),
                        (2, [{"outcome": "INFRA", "message": message}]),
                    )

    def test_changed_sites_use_merge_base_and_include_multiline_intersections_only(self):
        # Given diverged refs, changes unique to base must not enter the head's scope.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project(root, "def answer():\n    return (True\n            and False)\n")
            git(root, "init", "-q")
            git(root, "add", ".")
            git(root, "commit", "-qm", "initial")
            initial = git(root, "rev-parse", "HEAD")
            (root / "src/base.py").write_text("flag = True\n")
            git(root, "add", ".")
            git(root, "commit", "-qm", "base only")
            base = git(root, "rev-parse", "HEAD")
            git(root, "checkout", "-q", "--detach", initial)
            (root / "src/example.py").write_text(
                "def answer():\n    return (True\n            and True)\n"
            )
            git(root, "add", ".")
            git(root, "commit", "-qm", "head only")
            selected = source.scope(root, [], base, "HEAD")
            self.assertEqual(selected, {"src/example.py": {3}})
            mutants, diagnostics = gate.collect(root, selected)
            self.assertEqual(diagnostics, [])
            self.assertEqual(
                {(item.operator, item.line, item.end_line) for item in mutants},
                {
                    ("return:None", 2, 3),
                    ("return:True", 2, 3),
                    ("return:False", 2, 3),
                    ("boolean:swap", 2, 3),
                    ("boolean:drop:0", 2, 3),
                    ("boolean:drop:1", 2, 3),
                    ("boolean:literal", 3, 3),
                },
            )
            (root / "src/example.py").write_text(
                "def answer():\n    return (False\n            or True\n            or False)\n"
            )
            (root / "src/deletion.py").write_text("flag = True\nflag = False\n")
            git(root, "add", ".")
            git(root, "commit", "-qm", "before multiline and deletion")
            previous = git(root, "rev-parse", "HEAD")
            (root / "src/example.py").write_text(
                "def answer():\n    return (True\n            and False\n            and True)\n"
            )
            (root / "src/deletion.py").write_text("flag = True\n")
            git(root, "add", ".")
            git(root, "commit", "-qm", "multiline and deletion")
            selected = source.scope(root, [], previous, "HEAD")
            self.assertEqual(selected, {"src/example.py": {2, 3, 4}, "src/deletion.py": set()})
            mutants, _ = gate.collect(root, selected)
            self.assertEqual(
                {(m.file, m.line, m.end_line) for m in mutants},
                {
                    ("src/example.py", 2, 4),
                    ("src/example.py", 2, 2),
                    ("src/example.py", 3, 3),
                    ("src/example.py", 4, 4),
                },
            )

    def test_only_key_mode_and_setting_literal_roles_are_eligible(self):
        snippets = {
            "mapping = {'key': 'value'}": {"'key'"},
            "value = mapping['lookup']": {"'lookup'"},
            "value = 'abc'[0]": set(),
            "value = 'abc'['index']": {"'index'"},
            "value = 'plain'": set(),
            "value = other('argument')": set(),
            "value = settings.get(key='keyword')": set(),
            "value = settings.get('setting')": {"'setting'"},
            "value = open(path, 'rb')": {"'rb'"},
        }
        for snippet, expected in snippets.items():
            with self.subTest(snippet=snippet):
                mutants, _ = source.candidates("src/example.py", snippet.encode())
                self.assertEqual(
                    {m.before for m in mutants if m.operator == "string:key-mode-setting"}, expected
                )

    def test_equivalent_and_uncompilable_replacements_are_not_executable_candidates(self):
        node = ast.parse("True", mode="eval").body
        self.assertEqual(source.rejection(node, "True", b"True", "x.py"), {"outcome": "NO_OP"})
        self.assertEqual(
            source.rejection(node, "False", b"return False", "x.py"),
            {"outcome": "INVALID", "message": "'return' outside function"},
        )


class MutationVerdictContractsTest(unittest.TestCase):
    def test_controller_cleanup_error_records_infra_restores_source_and_allows_next_job(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            project(root, "def answer():\n    return True\n")
            mutants, _ = source.candidates("src/example.py", (root / "src/example.py").read_bytes())
            mapping = {"unit": {"src/example.py": {2: ["tests/test_example.py::test_value|run"]}}}
            path = root / "mutations.json"
            path.write_text(json.dumps(gate.manifest_data(root, mutants, mapping, 20)))
            with (
                patch.object(gate, "run_domain", side_effect=PermissionError("cleanup denied")),
                suppress(PermissionError),  # Require durable evidence even if it propagates.
            ):
                gate.execute_job(path, 0)
            self.assertTrue((root / "result-0.json").exists(), "cleanup failure lost its record")
            first = json.loads((root / "result-0.json").read_text())
            self.assertEqual(first["outcome"], "INFRA")
            self.assertTrue(first["restored"])
            self.assertIn("cleanup denied", first["message"])
            self.assertEqual(first["domains"], [])
            gate.execute_job(path, 1)
            self.assertEqual(json.loads((root / "result-1.json").read_text())["outcome"], "KILLED")
            self.assertEqual(
                (root / "src/example.py").read_text(), "def answer():\n    return True\n"
            )

    def test_worker_snapshot_failure_never_claims_verified_restoration(self):
        # Given an OS failure before a scratch snapshot exists, the durable row
        # reports INFRA with canonical empty domains and restored=false.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            project(root, "flag = True\n")
            mutants, _ = source.candidates("src/example.py", b"flag = True\n")
            path = root / "mutations.json"
            path.write_text(json.dumps(gate.manifest_data(root, mutants, {}, 20)))
            with patch.object(gate, "snapshot", side_effect=OSError("snapshot denied")):
                gate.execute_job(path, 0)
            row = json.loads((root / "result-0.json").read_text())
            self.assertEqual(
                {key: row[key] for key in ("outcome", "message", "domains", "restored")},
                {
                    "outcome": "INFRA",
                    "message": "snapshot denied",
                    "domains": [],
                    "restored": False,
                },
            )
            self.assertEqual((root / "src/example.py").read_bytes(), b"flag = True\n")

    def test_restoration_readback_mismatch_raises_without_publishing_a_success_record(self):
        # A filesystem can return bytes other than those just written. The worker
        # must fail closed even when its domain result would otherwise be publishable.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            project(root, "def answer():\n    return True\n")
            mutants = source.candidates("src/example.py", (root / "src/example.py").read_bytes())[0]
            path = root / "mutations.json"
            path.write_text(json.dumps(gate.manifest_data(root, mutants[:1], {}, 20)))
            read_bytes = Path.read_bytes
            reads = []

            def readback(target):
                value = read_bytes(target)
                if target.name == "example.py" and target != root / "src/example.py":
                    reads.append(value)
                    return value if len(reads) == 1 else b"corrupt readback"
                return value

            with (
                patch.object(Path, "read_bytes", readback),
                self.assertRaisesRegex(ValueError, "^Scratch restoration failed: src/example.py$"),
            ):
                gate.execute_job(path, 0)
            self.assertFalse((root / "result-0.json").exists())
            self.assertEqual(
                (root / "src/example.py").read_text(), "def answer():\n    return True\n"
            )

    def test_only_exact_reviewed_survivors_are_exempt_and_timeout_is_reported_separately(self):
        mutant = source.candidates("src/example.py", b"def answer():\n    return True\n")[0][-1]
        entry = {**mutant.key(), "reason": "Reviewed equivalent."}
        cases = (
            ("SURVIVED", 0, "ALLOWLISTED"),
            ("KILLED", 0, "KILLED"),
            ("TIMEOUT", 0, "TIMEOUT"),
            ("NO_TESTS", 1, "NO_TESTS"),
            ("INFRA", 1, "INFRA"),
        )
        for outcome, expected_status, expected_outcome in cases:
            with self.subTest(outcome=outcome):
                row = {**asdict(mutant), "outcome": outcome}
                output = io.StringIO()
                with redirect_stdout(output):
                    status = gate.verdict([row], [entry])
                expected = {**asdict(mutant), "outcome": expected_outcome}
                if expected_outcome == "ALLOWLISTED":
                    expected["reason"] = "Reviewed equivalent."
                self.assertEqual(
                    (status, json.loads(output.getvalue())), (expected_status, expected)
                )
        for field in mutant.key():
            with self.subTest(mismatched_field=field):
                output = io.StringIO()
                row = {**asdict(mutant), "outcome": "SURVIVED"}
                with redirect_stdout(output):
                    status = gate.verdict([row], [{**entry, field: "different"}])
                self.assertEqual((status, json.loads(output.getvalue())), (1, row))


class MutationEvidenceContractsTest(unittest.TestCase):
    def test_program_cleans_owned_descendants_after_leader_exit_or_timeout(self):
        # A detached controller stays outside the subject's group and owns emergency cleanup.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            other = subprocess.Popen(
                [sys.executable, "-c", "import signal; signal.pause()"], start_new_session=True
            )
            wait_exit = runner._wait_exit
            try:
                for mode in ("exit", "timeout"):
                    marker = root / f"{mode}.json"
                    ready = root / f"{mode}.ready"
                    observed = []

                    def timeout_after_ready(
                        pid, deadline, cancel=None, *, mode=mode, ready=ready, observed=observed
                    ):
                        if mode == "timeout" and not observed:
                            self.assertTrue(wait_for(ready.exists))
                            observed.append(pid)
                            return None
                        return wait_exit(pid, deadline, cancel)

                    child = (
                        "import json, os, signal, subprocess, sys, time\nfrom pathlib import Path\n"
                        "descendant = subprocess.Popen([sys.executable, '-c', "
                        '"import signal, sys; from pathlib import Path; '
                        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                        'Path(sys.argv[1]).touch(); signal.pause()", '
                        f"{str(ready)!r}])\n"
                        f"Path({str(marker)!r}).write_text(json.dumps({{'leader': os.getpid(), "
                        "'group': os.getpgrp(), 'descendant': descendant.pid}))\n"
                        "deadline = time.monotonic() + 10\n"
                        f"while not Path({str(ready)!r}).exists():\n"
                        "    if time.monotonic() >= deadline: raise RuntimeError('not ready')\n"
                        "    time.sleep(0.01)\n" + ("signal.pause()\n" if mode == "timeout" else "")
                    )
                    owned = None
                    try:
                        # Exercise timeout cleanup only after the descendant ignores TERM.
                        with patch.object(runner, "_wait_exit", side_effect=timeout_after_ready):
                            status = runner.program(
                                [sys.executable, "-c", child],
                                root,
                                dict(os.environ),
                                1 if mode == "timeout" else 15,
                                root / f"{mode}.log",
                            )
                        self.assertTrue(marker.exists())
                        self.assertTrue(ready.exists())
                        owned = json.loads(marker.read_text())
                        self.assertEqual(observed, [owned["leader"]] if mode == "timeout" else [])
                        self.assertEqual(owned["group"], owned["leader"])
                        self.assertNotEqual(owned["group"], os.getpgrp())
                        self.assertEqual(status, None if mode == "timeout" else 0)
                        self.assertTrue(wait_for(lambda pid=owned["descendant"]: not running(pid)))
                        self.assertFalse(running(owned["leader"]))
                        self.assertIsNone(other.poll())
                    finally:
                        if marker.exists():
                            terminate_group(json.loads(marker.read_text())["group"])
            finally:
                terminate_group(other.pid)
                other.wait(timeout=15)

    def test_subject_probe_runs_serially_when_xdist_plugin_is_disabled(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            project(root, "def answer():\n    return True\n")
            (root / "tests/conftest.py").write_text(
                "def pytest_sessionstart(session):\n"
                "    assert not session.config.pluginmanager.hasplugin('xdist')\n"
                "    assert not session.config.hook.pytest_configure_node.has_spec()\n"
            )
            result = subject_pytest(root)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(
                events(root),
                [
                    {
                        "node": "tests/test_example.py::test_value",
                        "phase": phase,
                        "outcome": "passed",
                        "assertion": False,
                    }
                    for phase in ("setup", "call", "teardown")
                ],
            )

    def test_subject_probe_preserves_setup_failure_without_a_call_stash(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            project(root, "def answer():\n    return True\n")
            (root / "tests/conftest.py").write_text(
                "import pytest\n@pytest.fixture(autouse=True)\n"
                "def broken():\n    assert False, 'original setup failure'\n"
            )
            result = subject_pytest(root)
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertIn("original setup failure", result.stdout)
            self.assertNotIn("INTERNALERROR", result.stdout + result.stderr)
            rows = events(root)
            self.assertEqual(
                rows,
                [
                    {
                        "node": "tests/test_example.py::test_value",
                        "phase": "setup",
                        "outcome": "failed",
                        "assertion": False,
                    },
                    {
                        "node": "tests/test_example.py::test_value",
                        "phase": "teardown",
                        "outcome": "passed",
                        "assertion": False,
                    },
                ],
            )
            self.assertEqual(
                runner.evidence_outcome(1, rows),
                {"outcome": "INFRA", "status": 1, "failures": [rows[0]]},
            )

    def test_subject_probe_teardown_assertion_is_not_a_call_assertion(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            project(root, "def answer():\n    return True\n")
            (root / "tests/conftest.py").write_text(
                "import pytest\n@pytest.fixture(autouse=True)\n"
                "def broken():\n    yield\n    assert False, 'original teardown failure'\n"
            )
            result = subject_pytest(root)
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            rows = events(root)
            self.assertEqual(
                rows,
                [
                    {
                        "node": "tests/test_example.py::test_value",
                        "phase": phase,
                        "outcome": outcome,
                        "assertion": False,
                    }
                    for phase, outcome in (
                        ("setup", "passed"),
                        ("call", "passed"),
                        ("teardown", "failed"),
                    )
                ],
            )
            self.assertEqual(
                runner.evidence_outcome(1, rows),
                {"outcome": "INFRA", "status": 1, "failures": [rows[-1]]},
            )

    def test_disabled_collection_sink_preserves_original_collection_error(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            project(root, "def answer():\n    return True\n")
            (root / "tests/test_example.py").write_text(
                "raise TypeError('original collection error')\n"
            )
            result = subject_pytest(root, sink=False)
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertIn("TypeError: original collection error", result.stdout)
            self.assertNotIn("INTERNALERROR", result.stdout + result.stderr)
            self.assertFalse((root / "subject.jsonl").exists())

    def test_skipped_subtest_then_pass_has_one_passed_owning_call(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            project(root, "def answer():\n    return True\n")
            (root / "tests/test_example.py").write_text(
                "import unittest\nclass Owner(unittest.TestCase):\n"
                "    def test_subtests(self):\n"
                "        with self.subTest(kind='skip'):\n            self.skipTest('one part')\n"
                "        with self.subTest(kind='pass'):\n            self.assertEqual(2 + 2, 4)\n"
            )
            result = subject_pytest(root)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            rows = events(root)
            self.assertEqual(
                rows,
                [
                    {
                        "node": "tests/test_example.py::Owner::test_subtests",
                        "phase": phase,
                        "outcome": "passed",
                        "assertion": False,
                    }
                    for phase in ("setup", "call", "teardown")
                ],
            )
            self.assertEqual(runner.evidence_outcome(0, rows), {"outcome": "SURVIVED"})

    def test_child_environment_isolates_data_temp_bytecode_and_executable_search(self):
        # Given hostile inherited settings, real imports and writes use only owned paths.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            project(root, "def answer():\n    return True\n")
            work = root / "work"
            work.mkdir()
            caller = root / "caller-data"
            caller.mkdir()
            parent = {
                "TMPDIR": str(root / ("long" * 30)),
                "TMP": "parent-tmp",
                "TEMP": "parent-temp",
                "NCI_SI_DATA_DIR": str(caller),
                "PYTHONDONTWRITEBYTECODE": "0",
                "PATH": "/parent-search",
            }
            code = (
                "import json, os, shutil, sys, example\nfrom pathlib import Path\n"
                "data = Path(os.environ['NCI_SI_DATA_DIR'])\n"
                "data.mkdir(parents=True, exist_ok=True)\n(data / 'written').write_text('owned')\n"
                "print(json.dumps({'settings': {key: value for key, value in os.environ.items() "
                "if key in ('TMPDIR', 'TMP', 'TEMP', 'PATH', 'PYTHONDONTWRITEBYTECODE') "
                "or key.startswith('NCI_SI_') or key.endswith('__mutated')}, "
                "'python': shutil.which('python')}))\n"
            )
            for has_path in (True, False):
                with self.subTest(has_path=has_path), patch.dict(os.environ, parent):
                    if not has_path:
                        os.environ.pop("PATH")
                    before = dict(os.environ)
                    env = runner.environment(root, work)
                    child = subprocess.run(  # noqa: S603 - fixed owned child program
                        [sys.executable, "-c", code],
                        cwd=root,
                        env=env,
                        capture_output=True,
                        text=True,
                        check=True,
                    )
                    observed = json.loads(child.stdout)
                    self.assertEqual(
                        observed["settings"],
                        {
                            "TMPDIR": tempfile.gettempdir(),
                            "TMP": tempfile.gettempdir(),
                            "TEMP": tempfile.gettempdir(),
                            "NCI_SI_DATA_DIR": str(work / "data"),
                            "PYTHONDONTWRITEBYTECODE": "1",
                            "PATH": str(Path(sys.executable).parent)
                            + os.pathsep
                            + ("/parent-search" if has_path else ""),
                        },
                    )
                    self.assertEqual(
                        Path(observed["python"]).resolve(), Path(sys.executable).resolve()
                    )
                    self.assertEqual(dict(os.environ), before)
            self.assertEqual((work / "data/written").read_text(), "owned")
            self.assertEqual(list(caller.iterdir()), [])
            self.assertEqual(list((root / "src").rglob("__pycache__")), [])

    def test_fixture_child_receives_exact_prepared_contract_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            project(root, "def answer():\n    return True\n")
            marker = root / "settings.json"
            (root / "acceptance/tests/test_contract.py").write_text(
                "import json, os\nfrom pathlib import Path\ndef test_settings():\n"
                f"    Path({str(marker)!r}).write_text(json.dumps({{key: value for key, value "
                "in os.environ.items() if key.startswith('NCI_SI_ACCEPTANCE_')}))\n"
                "    assert True\n"
            )
            result, _ = runner.run_domain(root, "fixture", [], 30)
            self.assertEqual(result["outcome"], "SURVIVED", result)
            self.assertEqual(
                json.loads(marker.read_text()),
                {
                    "NCI_SI_ACCEPTANCE_MODE": "fixture",
                    "NCI_SI_ACCEPTANCE_REGISTRY_RELEASE": "required",
                    "NCI_SI_ACCEPTANCE_SECURITY_SERVER": "python ../scripts/permissions_fixture.py",
                    "NCI_SI_ACCEPTANCE_PREPARE": (
                        'nci-si-mcp index-sample $(cat "$NCI_SI_ACCEPTANCE_INDEX_CODES")'
                    ),
                },
            )

    def test_unit_baseline_is_serial_and_other_baselines_use_explicit_workers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            project(root, "def answer():\n    return True\n")
            plugin = (
                "import json\nfrom pathlib import Path\n"
                "def pytest_sessionstart(session):\n"
                "    config = session.config\n"
                "    worker = getattr(config, 'workerinput', None)\n"
                "    name = worker['workerid'] if worker else 'controller'\n"
                "    Path('worker-' + name + '.json').write_text(json.dumps("
                "{'worker': worker is not None, 'count': config.getoption('numprocesses')}))\n"
            )
            (root / "conftest.py").write_text(plugin)
            (root / "acceptance/conftest.py").write_text(
                plugin + "def pytest_addoption(parser):\n    parser.addoption('--report')\n"
            )
            for domain, cwd, expected in (
                ("unit", root, {"controller": {"worker": False, "count": 0}}),
                (
                    "selftest",
                    root / "acceptance",
                    {
                        "controller": {"worker": False, "count": 4},
                        **{f"gw{i}": {"worker": True, "count": None} for i in range(4)},
                    },
                ),
                (
                    "fixture",
                    root / "acceptance",
                    {
                        "controller": {"worker": False, "count": 4},
                        **{f"gw{i}": {"worker": True, "count": None} for i in range(4)},
                    },
                ),
            ):
                with self.subTest(domain=domain):
                    for path in cwd.glob("worker-*.json"):
                        path.unlink()
                    result, _ = runner.run_domain(root, domain, [], 30, baseline=True)
                    self.assertEqual(result["outcome"], "SURVIVED", result)
                    self.assertEqual(
                        {
                            path.stem.removeprefix("worker-"): json.loads(path.read_text())
                            for path in cwd.glob("worker-*.json")
                        },
                        expected,
                    )

    def test_session_disabling_mutant_is_detected_without_losing_controller_evidence(self):
        # Given the real session-disabling operator, the immutable controller must
        # retain the owning call failure and restore its subject, not lose the job.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            project(root, "def answer():\n    return True\n")
            shutil.copytree(Path(gate.__file__).parents[1], root / "scripts")
            target = root / "scripts/validation/mutation_run.py"
            original = target.read_bytes()
            (root / "tests/test_example.py").write_text(
                "import os, sys\nfrom pathlib import Path\n"
                "from scripts.validation.mutation_run import program\n"
                "def test_owned_session(tmp_path):\n"
                "    marker = tmp_path / 'group'\n"
                "    status = program([sys.executable, '-c', "
                '"import os,sys; from pathlib import Path; '
                'Path(sys.argv[1]).write_text(str(os.getpid() == os.getpgrp()))", '
                "str(marker)], Path.cwd(), dict(os.environ), 5, tmp_path / 'child.log')\n"
                "    assert status == 0\n"
                "    assert marker.read_text() == 'True'\n"
            )
            baseline, data = runner.run_domain(root, "unit", [], 20, baseline=True)
            self.assertEqual(baseline["outcome"], "SURVIVED", baseline)
            mutants, _ = source.candidates("scripts/validation/mutation_run.py", original)
            mutant = next(
                m
                for m in mutants
                if m.qualname == "program"
                and m.before == "True"
                and m.operator == "boolean:literal"
            )
            mapping = {"unit": runner.coverage_map(data, root)}
            rows = gate.schedule(root, [mutant], mapping, SimpleNamespace(timeout=20, workers=1))
            self.assertEqual(
                [(row["outcome"], row.get("phase"), row.get("node")) for row in rows],
                [("KILLED", "call", "tests/test_example.py::test_owned_session")],
            )
            self.assertTrue(rows[0]["restored"])
            self.assertEqual(target.read_bytes(), original)

    def test_program_rejects_nonowned_group_before_waiting_and_reaps_only_its_child(self):
        # A session-less child shares our protected group. No group signal is
        # authorized, even when the caller's requested launch contract is broken.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            popen = subprocess.Popen
            children = []

            def without_session(*args, **kwargs):
                kwargs["start_new_session"] = False
                child = popen(*args, **kwargs)
                children.append(child)
                return child

            try:
                with (
                    patch.object(runner.subprocess, "Popen", side_effect=without_session),
                    patch("scripts.processes.os.killpg") as group_signal,
                    self.assertRaisesRegex(ValueError, "does not own its process group"),
                ):
                    runner.program(
                        [sys.executable, "-c", "pass"],
                        root,
                        dict(os.environ),
                        0.1,
                        root / "child.log",
                    )
                self.assertEqual(group_signal.call_args_list, [])
                self.assertIsNotNone(children[0].poll())
            finally:
                for child in children:
                    if child.poll() is None:
                        child.kill()
                    child.wait(timeout=5)

    def test_ownership_query_error_reaps_leader_without_signalling_unverified_descendants(self):
        # Given a ready descendant in the child's new group, when the OS cannot
        # verify ownership, only the direct leader is reaped; the group stays alive.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            marker = root / "descendant.pid"
            ready = root / "descendant.ready"
            child_code = (
                "import signal, subprocess, sys\nfrom pathlib import Path\n"
                "child = subprocess.Popen([sys.executable, '-c', "
                "'import signal, sys; from pathlib import Path; "
                "Path(sys.argv[1]).touch(); signal.pause()', "
                f"{str(ready)!r}])\n"
                f"Path({str(marker)!r}).write_text(str(child.pid))\n"
                "signal.pause()\n"
            )
            children = []
            popen = subprocess.Popen

            def capture(*args, **kwargs):
                child = popen(*args, **kwargs)
                children.append(child)
                return child

            def denied(pid):
                if not wait_for(ready.exists):
                    raise RuntimeError("descendant did not become ready")
                raise OSError("ownership query denied")

            try:
                with (
                    patch.object(runner.subprocess, "Popen", side_effect=capture),
                    patch.object(runner.os, "getpgid", side_effect=denied),
                    self.assertRaises(OSError) as failed,
                ):
                    runner.program(
                        [sys.executable, "-c", child_code],
                        root,
                        dict(os.environ),
                        15,
                        root / "child.log",
                    )
                self.assertEqual(str(failed.exception), "ownership query denied")
                self.assertTrue(ready.exists())
                self.assertIsNotNone(children[0].poll())
                self.assertFalse(running(children[0].pid))
                self.assertTrue(running(int(marker.read_text())))
            finally:
                for child in children:
                    terminate_group(child.pid)
                    child.wait(timeout=15)
                if marker.exists():
                    with suppress(ProcessLookupError):
                        os.kill(int(marker.read_text()), signal.SIGKILL)

    def test_phase_hooks_preserve_hook_result_and_restore_environment(self):
        phases = (
            (probe.pytest_runtest_setup, "setup"),
            (probe.pytest_runtest_call, "run"),
            (probe.pytest_runtest_teardown, "teardown"),
        )
        with patch.dict(os.environ, {"MUTATION_COVERAGE_CONTEXT": "parent"}):
            for hook, phase in phases:
                with self.subTest(phase=phase):
                    invocation = hook(SimpleNamespace(nodeid="tests/x.py::test_x"))
                    next(invocation)
                    self.assertEqual(
                        os.environ["MUTATION_COVERAGE_CONTEXT"], f"tests/x.py::test_x|{phase}"
                    )
                    result = object()
                    with self.assertRaises(StopIteration) as finished:
                        invocation.send(result)
                    self.assertIs(finished.exception.value, result)
                    self.assertEqual(os.environ["MUTATION_COVERAGE_CONTEXT"], "parent")

    def test_child_context_restores_environment_on_failed_phases_and_inner_runs_strip_it(self):
        keys = ("MUTATION_COVERAGE_CONTEXT", "COVERAGE_PROCESS_CONFIG", "COVERAGE_PROCESS_START")
        for initial in ({}, dict(zip(keys, ("previous", "serialized", "config"), strict=True))):
            with self.subTest(initial=initial), patch.dict(os.environ, initial, clear=True):
                with (
                    self.assertRaisesRegex(RuntimeError, "phase failed"),
                    probe.child_context(SimpleNamespace(nodeid="tests/x.py::test_x"), "run"),
                ):
                    self.assertEqual(
                        os.environ["MUTATION_COVERAGE_CONTEXT"], "tests/x.py::test_x|run"
                    )
                    self.assertNotIn("COVERAGE_PROCESS_CONFIG", os.environ)
                    inner = runner.environment(Path("source"), Path("work"))
                    self.assertEqual({key: inner[key] for key in keys if key in inner}, {})
                    raise RuntimeError("phase failed")
                self.assertEqual(dict(os.environ), initial)

    def test_collection_hook_serializes_canonical_traceback_paths_and_exception_type(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory).resolve() / "events.jsonl"
            try:
                raise TypeError("invalid table")
            except TypeError:
                representation = pytest.ExceptionInfo.from_current().getrepr()
            report = SimpleNamespace(
                failed=True, nodeid="tests/test_example.py", longrepr=representation
            )
            with patch.object(probe, "DESTINATION", str(path)):
                probe.pytest_collectreport(report)
            self.assertEqual(
                json.loads(path.read_text()),
                {
                    "node": "tests/test_example.py",
                    "phase": "collection",
                    "outcome": "failed",
                    "assertion": False,
                    "traceback_paths": [str(Path(__file__).resolve())],
                    "exception": "TypeError: invalid table",
                },
            )

    def test_collection_row_records_the_stop_marker_only_when_the_report_carries_it(self):
        # The real-run tests reach this hook only in a child pytest, which loads a copy of the
        # probe; the recording contract is therefore also checked in process.
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            with patch.object(probe, "DESTINATION", str(path)):
                for marker in (True, False):
                    report = SimpleNamespace(failed=True, nodeid="", mutation_stop_notice=marker)
                    probe.pytest_collectreport(report)
            rows = [json.loads(line) for line in path.read_text().splitlines()]
        row = {"node": "", "phase": "collection", "outcome": "failed", "assertion": False}
        self.assertEqual(rows, [row | {"stop_notice": True}, row])

    def test_system_exit_and_internal_errors_never_become_import_kills(self):
        target = Path("src/example.py").resolve()
        failure = {
            "node": "tests/test_example.py",
            "phase": "collection",
            "outcome": "failed",
            "assertion": False,
            "traceback_paths": [str(target)],
            "exception": "SystemExit: 1",
        }
        for status in (1, 2, 3, 4):
            with self.subTest(status=status):
                self.assertEqual(
                    runner.evidence_outcome(status, [failure], target),
                    {"outcome": "INFRA", "status": status, "failures": [failure]},
                )
        failure["exception"] = "TypeError: invalid table"
        self.assertEqual(runner.evidence_outcome(3, [failure], target)["outcome"], "INFRA")
        for phase in ("setup", "teardown", "call"):
            row = {**failure, "phase": phase}
            self.assertEqual(
                runner.evidence_outcome(2, [row], target),
                {"outcome": "INFRA", "status": 2, "failures": [row]},
            )

    def test_usage_error_without_collection_evidence_is_infra_not_an_import_kill(self):
        # Given a bad node id, exit 4 alone supplies no evidence that the subject
        # failed to import, even when the caller identifies a mutated file.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            project(root, "flag = True\n")
            target = root / "src/example.py"
            result, _ = runner.run_domain(
                root, "unit", ["tests/test_example.py::missing"], 20, mutated_file=target
            )
            self.assertEqual(
                {key: result[key] for key in ("outcome", "status", "failures")},
                {"outcome": "INFRA", "status": 4, "failures": []},
            )

    def test_import_validation_kill_requires_traceback_through_the_mutated_file(self):
        cases = (
            (
                "flag = True\nif not flag:\n    raise TypeError('invalid table')\n"
                "def answer():\n    return flag\n",
                None,
                "KILLED",
            ),
            (
                "flag = True\ndef answer():\n    return flag\n",
                "import example\nif not example.flag:\n    raise TypeError('unrelated collector')\n"
                "def test_value():\n    assert example.answer()\n",
                "INFRA",
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            for text, test, outcome in cases:
                with self.subTest(outcome=outcome):
                    project(root, text)
                    if test:
                        (root / "tests/test_example.py").write_text(test)
                    baseline, _ = runner.run_domain(root, "unit", [], 20, baseline=True)
                    self.assertEqual(baseline["outcome"], "SURVIVED", baseline)
                    mutants, _ = source.candidates("src/example.py", text.encode())
                    mutant = next(m for m in mutants if m.line == 1)
                    path = root / "mutations.json"
                    mapping = {"unit": {"src/example.py": {1: [""]}}}
                    path.write_text(json.dumps(gate.manifest_data(root, [mutant], mapping, 20)))
                    gate.execute_job(path, 0)
                    row = json.loads((root / "result-0.json").read_text())
                    self.assertEqual(row["outcome"], outcome, row)
                    self.assertEqual(row["fallback_domains"], ["unit"])
                    if outcome == "KILLED":
                        self.assertEqual(
                            {name: row[name] for name in ("kind", "phase", "node")},
                            {
                                "kind": "import",
                                "phase": "collection",
                                "node": "tests/test_example.py",
                            },
                        )

    def two_file_import_run(self, root, text, first_test):
        """A clean baseline, then the flag-flipping mutant with owners selected in two files."""
        project(root, text)
        (root / "tests/test_example.py").write_text(first_test)
        (root / "tests/test_other.py").write_text(
            "import example\ndef test_other():\n    assert example.answer()\n"
        )
        baseline, _ = runner.run_domain(root, "unit", [], 20, baseline=True)
        self.assertEqual(baseline["outcome"], "SURVIVED", baseline)
        mutant = next(
            m for m in source.candidates("src/example.py", text.encode())[0] if m.line == 1
        )
        owners = ["tests/test_example.py::test_value|run", "tests/test_other.py::test_other|run"]
        mapping = {"unit": {"src/example.py": {1: owners}}}
        path = root / "mutations.json"
        path.write_text(json.dumps(gate.manifest_data(root, [mutant], mapping, 20)))
        gate.execute_job(path, 0)
        return json.loads((root / "result-0.json").read_text())

    def test_import_failure_across_selected_files_is_an_import_kill_keeping_the_stop_notice(self):
        # Given owners in two files, pytest -x stops collecting after the first file's import
        # failure and reports that stop as a second collection failure of the session.
        with tempfile.TemporaryDirectory() as directory:
            row = self.two_file_import_run(
                Path(directory).resolve(),
                "flag = True\nif not flag:\n    raise TypeError('invalid table')\n"
                "def answer():\n    return flag\n",
                "import example\ndef test_value():\n    assert example.answer() is True\n",
            )
        self.assertEqual(
            {name: row.get(name) for name in ("outcome", "kind", "phase", "node")},
            {
                "outcome": "KILLED",
                "kind": "import",
                "phase": "collection",
                "node": "tests/test_example.py",
            },
            row,
        )
        (notice,) = row["stop_notices"]
        self.assertEqual(
            {name: notice[name] for name in ("node", "phase", "outcome", "stop_notice")},
            {"node": "", "phase": "collection", "outcome": "failed", "stop_notice": True},
        )
        self.assertEqual(notice["exception"], "_pytest.main.Failed: stopping after 1 failures")

    def test_an_unrelated_or_lookalike_first_collection_failure_stays_infra(self):
        # Given a mutant that leaves its module importable, the first selected file's own
        # collector fails (an unrelated error, a lookalike message, or pytest's stop type raised
        # by the test itself). No row is a stop notice and the run stays infrastructure failure.
        failures = {
            "unrelated": "raise TypeError('unrelated collector')\n",
            "lookalike text": "raise RuntimeError('stopping after 1 failures')\n",
            "raised stop type": (
                "raise __import__('pytest').Session.Failed('stopping after 1 failures')\n"
            ),
            "raised stop type carrying the inactive state": (
                "raise __import__('pytest').Session.Failed(False)\n"
            ),
        }
        for case, failure in failures.items():
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                row = self.two_file_import_run(
                    Path(directory).resolve(),
                    "flag = True\ndef answer():\n    return flag\n",
                    "import example\nif not example.flag:\n    "
                    + failure
                    + "def test_value():\n    assert example.answer()\n",
                )
                self.assertEqual(row["outcome"], "INFRA", row)
                self.assertEqual(row["failures"][0]["node"], "tests/test_example.py")
                self.assertNotIn("stop_notice", row["failures"][0])
                self.assertNotIn("stop_notices", row)

    def test_only_a_marked_notice_after_one_qualifying_import_failure_is_set_aside(self):
        target = Path("src/example.py").resolve()
        failure = {
            "node": "tests/test_example.py",
            "phase": "collection",
            "outcome": "failed",
            "assertion": False,
            "traceback_paths": [str(target)],
            "exception": "TypeError: invalid table",
        }
        notice = {
            "node": "",
            "phase": "collection",
            "outcome": "failed",
            "assertion": False,
            "traceback_paths": [],
            "exception": "_pytest.main.Failed: stopping after 1 failures",
            "stop_notice": True,
        }
        self.assertEqual(
            runner.evidence_outcome(4, [failure, notice], target),
            {
                "outcome": "KILLED",
                "kind": "import",
                "phase": "collection",
                "node": "tests/test_example.py",
                "stop_notices": [notice],
            },
        )
        unrelated = {**failure, "traceback_paths": [str(target.with_name("other.py"))]}
        rejected = {
            "notice only": [notice],
            "marked primary reaching the file": [{**notice, "traceback_paths": [str(target)]}],
            "marked primary with an invalid marker": [{**failure, "stop_notice": "true"}, notice],
            "notice before the failure": [{**notice, "traceback_paths": [str(target)]}, failure],
            "marker not exactly true": [failure, {**notice, "stop_notice": "true"}],
            "marker on a call row": [failure, {**notice, "phase": "call"}],
            "two independent failures": [failure, {**failure, "node": "tests/b.py"}, notice],
            "SystemExit": [{**failure, "exception": "SystemExit: 1"}, notice],
            "unrelated path": [unrelated, notice],
        }
        for case, rows in rejected.items():
            with self.subTest(case=case):
                self.assertEqual(
                    runner.evidence_outcome(4, rows, target),
                    {"outcome": "INFRA", "status": 4, "failures": rows},
                )
        self.assertEqual(runner.evidence_outcome(3, [failure, notice], target)["outcome"], "INFRA")

    def test_only_pytests_own_stop_after_a_failure_is_a_stop_notice(self):
        # pytest raises session.Failed(session.shouldfail) once -x has seen a failure; nothing
        # else, including the same text, another type or a subclass, is that notice.
        reason = "stopping after 1 failures"
        stopped = SimpleNamespace(Failed=pytest.Session.Failed, shouldfail=reason)

        def raised(error):
            return SimpleNamespace(excinfo=SimpleNamespace(value=error))

        self.assertTrue(probe.session_stop(stopped, raised(pytest.Session.Failed(reason))))
        subclass = type("Subclass", (pytest.Session.Failed,), {})
        cases = {
            "no call": (stopped, None),
            "no exception": (stopped, SimpleNamespace(excinfo=None)),
            "same text, other type": (stopped, raised(RuntimeError(reason))),
            "subclass": (stopped, raised(subclass(reason))),
            "other reason": (stopped, raised(pytest.Session.Failed("stopping after 2 failures"))),
            "before any failure": (
                SimpleNamespace(Failed=pytest.Session.Failed, shouldfail=False),
                raised(pytest.Session.Failed(reason)),
            ),
            "stop type carrying the inactive state": (
                SimpleNamespace(Failed=pytest.Session.Failed, shouldfail=False),
                raised(pytest.Session.Failed(False)),
            ),
        }
        for case, (session, call) in cases.items():
            with self.subTest(case=case):
                self.assertFalse(probe.session_stop(session, call))

    def test_probe_configuration_consumes_launch_sink_and_passes_only_explicit_worker_sink(self):
        with (
            patch.object(probe, "DESTINATION", None),
            patch.dict(os.environ, {"MUTATION_EVIDENCE": "launch.jsonl"}),
        ):
            probe.pytest_configure(SimpleNamespace())
            self.assertEqual(probe.DESTINATION, "launch.jsonl")
            self.assertNotIn("MUTATION_EVIDENCE", os.environ)
            node = SimpleNamespace(workerinput={"other": "retained"})
            probe.pytest_configure_node(node)
            self.assertEqual(
                node.workerinput, {"other": "retained", "mutation_evidence": "launch.jsonl"}
            )
            probe.pytest_configure(
                SimpleNamespace(workerinput={"mutation_evidence": "worker.jsonl"})
            )
            self.assertEqual(probe.DESTINATION, "worker.jsonl")
            probe.pytest_configure(SimpleNamespace())
            self.assertIsNone(probe.DESTINATION)

    def test_report_hook_writes_one_aggregated_call_between_setup_and_teardown(self):
        # Given real pytest hook inputs, reports retain their identity and the JSON
        # consumer receives one owning call, including mixed unittest subtest failures.
        cases = (
            ([("passed", None)], "passed", False),
            ([("failed", AssertionError()), ("passed", None)], "failed", True),
            ([("failed", pytest.fail.Exception()), ("failed", TypeError())], "failed", False),
            ([("skipped", None)], "skipped", False),
            ([("skipped", None), ("passed", None)], "passed", False),
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            for calls, outcome, assertion in cases:
                with self.subTest(calls=calls), patch.object(probe, "DESTINATION", str(path)):
                    path.unlink(missing_ok=True)
                    item = SimpleNamespace(stash=pytest.Stash())
                    reports = [("setup", "passed", None)]
                    reports += [("call", value, error) for value, error in calls]
                    reports += [("teardown", "passed", None)]
                    for phase, value, error in reports:
                        report = SimpleNamespace(nodeid="owner", when=phase, outcome=value)
                        call = SimpleNamespace(
                            excinfo=None if error is None else SimpleNamespace(value=error)
                        )
                        hook = probe.pytest_runtest_makereport(item, call)
                        next(hook)
                        with self.assertRaises(StopIteration) as finished:
                            hook.send(report)
                        self.assertIs(finished.exception.value, report)
                    self.assertEqual(
                        [json.loads(line) for line in path.read_text().splitlines()],
                        [
                            {"node": "owner", "phase": phase, "outcome": value, "assertion": flag}
                            for phase, value, flag in (
                                ("setup", "passed", False),
                                ("call", outcome, assertion),
                                ("teardown", "passed", False),
                            )
                        ],
                    )

    def test_disabled_report_sink_preserves_report_without_stashing_or_writing(self):
        item = SimpleNamespace(stash=pytest.Stash())
        report = SimpleNamespace(when="call", outcome="failed", nodeid="owner")
        with patch.object(probe, "DESTINATION", None):
            hook = probe.pytest_runtest_makereport(item, SimpleNamespace(excinfo=None))
            next(hook)
            with self.assertRaises(StopIteration) as finished:
                hook.send(report)
            self.assertIs(finished.exception.value, report)
            self.assertNotIn(probe.CALLS, item.stash)

    def test_pytest_collector_accepts_only_manifest_name_and_enumerates_exact_job_indices(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mutations.json"
            config = pytest.Config.fromdictargs({}, [])
            self.addCleanup(config._ensure_unconfigure)
            session = pytest.Session.from_config(config)
            self.assertIsNone(probe.pytest_collect_file(path.with_name("other.json"), session))
            collector = probe.pytest_collect_file(path, session)
            self.assertIsInstance(collector, probe.MutationFile)
            for jobs, expected in (([], []), ([{}, {}], ["0", "1"])):
                with self.subTest(jobs=jobs):
                    path.write_text(json.dumps({"mutants": jobs}))
                    self.assertEqual([item.name for item in collector.collect()], expected)

    def test_collected_job_executes_its_manifest_index_and_publishes_restored_result(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            project(root, "def answer():\n    return True\n")
            mutants = source.candidates("src/example.py", (root / "src/example.py").read_bytes())[0]
            path = root / "mutations.json"
            path.write_text(json.dumps(gate.manifest_data(root, mutants, {}, 20)))
            config = pytest.Config.fromdictargs({}, [])
            self.addCleanup(config._ensure_unconfigure)
            collector = probe.pytest_collect_file(path, pytest.Session.from_config(config))
            items = list(collector.collect())
            items[1].runtest()
            result = json.loads((root / "result-1.json").read_text())
            self.assertEqual(
                {key: result[key] for key in ("operator", "outcome", "restored", "domains")},
                {
                    "operator": mutants[1].operator,
                    "outcome": "NO_TESTS",
                    "restored": True,
                    "domains": [],
                },
            )
            self.assertFalse((root / "result-0.json").exists())

    def test_collection_evidence_records_only_failures_and_never_credits_an_assertion(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            with patch.object(probe, "DESTINATION", str(path)):
                probe.pytest_collectreport(SimpleNamespace(failed=False, nodeid="tests/good.py"))
                self.assertFalse(path.exists())
                probe.pytest_collectreport(SimpleNamespace(failed=True, nodeid="tests/bad.py"))
            self.assertEqual(
                json.loads(path.read_text()),
                {
                    "node": "tests/bad.py",
                    "phase": "collection",
                    "outcome": "failed",
                    "assertion": False,
                },
            )

    def test_call_kill_requires_one_owning_call_and_failure_exit_status(self):
        failure = {"node": "tests/test_x.py::test_x", "phase": "call", "assertion": True}
        for status, failures in (
            (0, [failure]),
            (2, [failure]),
            (1, []),
            (1, [failure, failure]),
            (1, [{**failure, "phase": "setup"}]),
            (1, [{**failure, "phase": "teardown"}]),
            (1, [{**failure, "phase": "collection"}]),
        ):
            with self.subTest(status=status, failures=failures):
                self.assertEqual(runner.call_kill(status, failures), {})
        self.assertEqual(
            runner.call_kill(1, [failure]),
            {
                "outcome": "KILLED",
                "node": "tests/test_x.py::test_x",
                "phase": "call",
                "kind": "assertion",
            },
        )
        failed = {**failure, "outcome": "failed"}
        passed = {**failure, "outcome": "passed"}
        for status in (0, 5):
            for rows in ([failed], [failed, passed]):
                self.assertEqual(
                    runner.evidence_outcome(status, rows),
                    {"outcome": "INFRA", "status": status, "failures": [failed]},
                )

    def test_coverage_mapping_keeps_only_canonical_scratch_paths_and_test_contexts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            scratch = root / "source"
            scratch.mkdir()
            data_file = root / ".coverage"
            with self.assertRaisesRegex(ValueError, "Missing baseline coverage:"):
                runner.coverage_map(data_file, scratch)
            data = CoverageData(basename=str(data_file))
            data.set_context("tests/test_x.py::test_x|run")
            data.add_lines({str(scratch / "src/x.py"): {2}, str(root / "outside.py"): {9}})
            data.set_context("")
            data.add_lines({str(scratch / "src/x.py"): {3}})
            data.write()
            mapping = runner.coverage_map(data_file, scratch)
            self.assertEqual(mapping, {"src/x.py": {2: ["tests/test_x.py::test_x|run"], 3: [""]}})
            self.assertEqual(
                runner.selection({"unit": mapping}, "src/x.py", 2, 3),
                {"unit": ["tests/test_x.py::test_x"]},
            )

    def test_baseline_failure_names_the_domain_and_cannot_become_a_successful_map(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project(root, "def answer():\n    return True\n", "assert False")
            failed, _ = runner.run_domain(root, "unit", [], 30, baseline=True)
            self.assertEqual(failed["outcome"], "INFRA", failed)
            self.assertEqual(
                set(failed),
                {"outcome", "node", "phase", "kind", "domain", "log", "elapsed_seconds"},
            )
            with self.assertRaisesRegex(ValueError, "Baseline unit failed:.*INFRA"):
                runner.baseline(root, 30)
            project(root, "def answer():\n    return True\n")
            (root / "acceptance/selftests/test_harness.py").write_text(
                "def test_harness():\n    assert False\n"
            )
            with self.assertRaisesRegex(ValueError, "Baseline selftest failed:.*INFRA"):
                runner.baseline(root, 30)
            project(root, "def answer():\n    return True\n")
            mapping = runner.baseline(root, 30)
            self.assertEqual(set(mapping), {"unit", "selftest", "fixture"})
            self.assertEqual(
                runner.selection(mapping, "src/example.py", 2, 2),
                {"unit": ["tests/test_example.py::test_value"]},
            )

    def test_selected_domain_kill_stops_routing_and_survivors_require_fixture_confirmation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            project(
                root,
                "def answer():\n    return True\n",
                "assert example.answer() is not Ellipsis",
                contract=(
                    "import os\n"
                    "    assert {key: os.environ.get(key) for key in "
                    "('NCI_SI_ACCEPTANCE_MODE', 'NCI_SI_ACCEPTANCE_REGISTRY_RELEASE', "
                    "'NCI_SI_ACCEPTANCE_SECURITY_SERVER', 'NCI_SI_ACCEPTANCE_PREPARE')} == "
                    "{'NCI_SI_ACCEPTANCE_MODE': 'fixture', "
                    "'NCI_SI_ACCEPTANCE_REGISTRY_RELEASE': 'required', "
                    "'NCI_SI_ACCEPTANCE_SECURITY_SERVER': "
                    "'python ../scripts/permissions_fixture.py', "
                    "'NCI_SI_ACCEPTANCE_PREPARE': "
                    "'nci-si-mcp index-sample $(cat \"$NCI_SI_ACCEPTANCE_INDEX_CODES\")'}\n"
                    "    assert example.answer() is True"
                ),
            )
            selected = {
                "unit": ["tests/test_example.py::test_value"],
                "selftest": [],
                "fixture": ["tests/test_contract.py::test_contract"],
            }
            confirmation = gate.measure(root, selected, 30)
            self.assertEqual(
                (confirmation["outcome"], confirmation["confirmation"]), ("SURVIVED", "fixture")
            )
            self.assertEqual(
                [(row["domain"], row["outcome"]) for row in confirmation["domains"]],
                [(domain, "SURVIVED") for domain in ("unit", "selftest", "fixture")],
            )
            unit_only = gate.measure(root, {"unit": selected["unit"]}, 30)
            self.assertEqual(
                set(unit_only),
                {"outcome", "domain", "log", "elapsed_seconds", "confirmation", "domains"},
            )
            self.assertEqual(unit_only["confirmation"], "unreached")
            self.assertEqual(
                [(row["domain"], row["outcome"]) for row in unit_only["domains"]],
                [("unit", "SURVIVED")],
            )
            (root / "src/example.py").write_text("def answer():\n    return False\n")
            result = gate.measure(root, selected, 30)
            self.assertEqual(
                {key: result[key] for key in ("outcome", "domain", "node", "confirmation")},
                {
                    "outcome": "KILLED",
                    "domain": "fixture",
                    "node": "tests/test_contract.py::test_contract",
                    "confirmation": "fixture",
                },
            )
            self.assertEqual(
                [(row["domain"], row["outcome"]) for row in result["domains"]],
                [("unit", "SURVIVED"), ("selftest", "SURVIVED"), ("fixture", "KILLED")],
            )
            (root / "tests/test_example.py").write_text(
                "import example\ndef test_value():\n    assert example.answer() is True\n"
            )
            result = gate.measure(root, selected, 30)
            self.assertEqual((result["outcome"], result["domain"]), ("KILLED", "unit"))
            self.assertEqual([row["domain"] for row in result["domains"]], ["unit"])
            self.assertNotIn("confirmation", result)
            self.assertEqual(gate.measure(root, {}, 30), {"outcome": "NO_TESTS", "domains": []})
