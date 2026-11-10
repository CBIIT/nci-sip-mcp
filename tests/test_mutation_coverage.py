"""Shared CI baselines retain local routes and reject unusable inputs at the CLI."""

import json
import subprocess
import sys
import tempfile
import tomllib
import unittest
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

from coverage import CoverageData
from scripts.validation.mutation_coverage import SHARDS, export, identity, load
from scripts.validation.mutation_gate import schedule
from scripts.validation.mutation_run import baseline, environment, run_domain, selection
from scripts.validation.mutation_source import candidates

from test_mutation_gate import ROOT, cli, project


def repository(root):
    project(root, "def answer():\n    return True\n")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)  # noqa: S607
    subprocess.run(["git", "add", "."], cwd=root, check=True)  # noqa: S607
    subprocess.run(
        [  # noqa: S607 - fixed fixture command; no hooks exist in this owned repository
            "git",
            "-c",
            "user.name=Test role",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "test: coverage tree",
        ],
        cwd=root,
        check=True,
    )


def fragments(root, directory):
    directory.mkdir()
    for domain, count in SHARDS.items():
        node = {
            "unit": "tests/test_example.py",
            "selftest": "selftests/test_harness.py",
            "fixture": "tests/test_contract.py",
        }[domain]
        for index in range(1, count + 1):
            data_file = root / f".coverage-{domain}-{index}"
            data = CoverageData(basename=str(data_file))
            data.set_context(f"{node}::test_value|run")
            data.add_lines({str(root / "src/example.py"): {2}})
            data.write()
            export(
                root, data_file, domain, f"{index}/{count}", directory / f"{domain}-{index}.json"
            )


FOLDERS = {"unit": "tests", "selftest": "acceptance/selftests", "fixture": "acceptance/tests"}


def partition_project(root):
    repository(root)
    for domain, count in SHARDS.items():
        folder = root / FOLDERS[domain]
        for path in folder.glob("test_*.py"):
            path.unlink()
        (folder / "test_value.py").write_text(
            "import example\n"
            + "".join(
                f"def test_{index}():\n    assert example.answer() is True\n"
                for index in range(1, count + 1)
            )
        )
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)  # noqa: S607
    subprocess.run(
        [  # noqa: S607 - fixed fixture commit
            "git",
            "-c",
            "user.name=Test role",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "test: partition fixtures",
        ],
        cwd=root,
        check=True,
    )


def expected_nodes():
    return {
        domain: sorted(
            [
                f"{Path(FOLDERS[domain]).name}/test_value.py::test_{index}"
                for index in range(1, count + 1)
            ]
        )
        for domain, count in SHARDS.items()
    }


CHILD_SOURCE = "def answer():\n    return True\n\n\ndef child():\n    return 'child-only'\n"
CHILD_TEST = (
    "import subprocess, sys, tempfile\n"
    "def test_child():\n"
    "    with tempfile.TemporaryDirectory() as elsewhere:\n"
    "        done = subprocess.run(\n"
    "            [sys.executable, '-c', 'import example; example.child()'], cwd=elsewhere\n"
    "        )\n"
    "    assert done.returncode == 0\n"
)


def coverage_cli(*arguments):
    return subprocess.run(  # noqa: S603 - fixed CLI, owned argument paths
        ["pdm", "run", "python", "-m", "scripts.validation.mutation_coverage", *arguments],  # noqa: S607
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def produce(root, config, *tests):
    """Run a real producer as CI does: subject probe, shared config, child coverage startup."""
    env = environment(root, root / "tmp")  # drops inherited COVERAGE_* settings
    env["COVERAGE_PROCESS_START"] = str(config)
    return subprocess.run(  # noqa: S603 - the CI producer command, owned paths
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "scripts.validation.mutation_probe",
            "-p",
            "no:cacheprovider",
            "-n",
            "0",
            "--cov",
            f"--cov-config={config}",
            "--cov-context=test",
            "--cov-fail-under=0",
            "--cov-report=",
            *tests,
        ],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def ordinary_coverage_project(root):
    project(root, CHILD_SOURCE)
    (root / "pyproject.toml").write_bytes((ROOT / "pyproject.toml").read_bytes())
    (root / "acceptance/pyproject.toml").write_bytes(
        (ROOT / "acceptance/pyproject.toml").read_bytes()
    )
    (root / ".pdm-python").write_text(sys.executable)
    elsewhere = root / "elsewhere"
    elsewhere.mkdir()
    for name in ("tests/test_example.py", "acceptance/selftests/test_harness.py"):
        (root / name).write_text(
            "import subprocess, sys\n"
            "def test_child():\n"
            "    done = subprocess.run([sys.executable, '-c', "
            f"'import example; example.child()'], cwd={str(elsewhere)!r}, timeout=10)\n"
            "    assert done.returncode == 0\n"
        )


def ordinary_coverage_run(root, command, destination=None):
    env = environment(root, root / "tmp")
    if destination is not None:
        env["COVERAGE_FILE"] = str(destination)
    return subprocess.run(  # noqa: S603 - ordinary PDM alias, owned synthetic project
        [  # noqa: S607 - fixed PDM alias and synthetic coverage source
            "pdm",
            "run",
            command,
            "--cov-reset",
            "--cov=example",
            "--cov-fail-under=0",
            "--cov-report=",
        ],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


class OrdinaryCoverageTest(unittest.TestCase):
    def test_ordinary_commands_keep_combined_child_data_in_owned_scratch(self):
        for command, domain in (("test", "unit"), ("acceptance-selftest", "harness")):
            with self.subTest(command=command), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                ordinary_coverage_project(root)
                destination = root / "tmp/coverage" / domain / ".coverage"
                for _repeat in range(2):
                    result = ordinary_coverage_run(root, command)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertTrue(destination.is_file(), result.stdout + result.stderr)
                    data = CoverageData(basename=str(destination))
                    data.read()
                    self.assertIn(6, data.lines(str(root / "src/example.py")) or [])
                    self.assertEqual(list(root.rglob(".coverage*")), [destination])

    def test_explicit_data_destination_remains_owned_by_the_consumer(self):
        for command in ("test", "acceptance-selftest"):
            with self.subTest(command=command), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                ordinary_coverage_project(root)
                destination = root / "consumer/.coverage"
                result = ordinary_coverage_run(root, command, destination)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                data = CoverageData(basename=str(destination))
                data.read()
                self.assertIn(6, data.lines(str(root / "src/example.py")) or [])
                self.assertEqual(list(root.rglob(".coverage*")), [destination])

    def test_missing_project_root_refuses_configuration_before_output(self):
        for name in ("pyproject.toml", "acceptance/pyproject.toml"):
            with self.subTest(config=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                result = subprocess.run(  # noqa: S603 - installed coverage, exact tracked config
                    [
                        sys.executable,
                        "-c",
                        "import coverage,sys; coverage.Coverage(config_file=sys.argv[1])",
                        str(ROOT / name),
                    ],
                    cwd=root,
                    env=environment(root, root / "tmp"),
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                )
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("PDM_PROJECT_ROOT", result.stderr)
                self.assertEqual(list(root.rglob(".coverage*")), [])
                settings = tomllib.loads((ROOT / name).read_text())["tool"]["coverage"]["run"]
                domain = "unit" if name == "pyproject.toml" else "harness"
                self.assertEqual(
                    settings["data_file"], f"${{PDM_PROJECT_ROOT?}}/tmp/coverage/{domain}/.coverage"
                )


def verdict_rows(rows):
    return [(row["outcome"], row.get("node"), row.get("kind")) for row in rows]


def cli_mutants(result):
    rows = [json.loads(line) for line in result.stdout.splitlines()]
    return [row for row in rows if "site" in row]


class SharedCoverageTest(unittest.TestCase):
    def test_fragment_counts_match_the_real_ci_matrix_independently(self):
        self.assertEqual(SHARDS, {"unit": 4, "selftest": 8, "fixture": 1})

    def test_export_rejects_bad_shards_and_unattributed_data_without_creating_fragments(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repository(root)
            data_file = root / ".coverage"
            data = CoverageData(basename=str(data_file))
            data.set_context("tests/test_example.py::test_value|run")
            data.add_lines({str(root / "src/example.py"): {2}})
            data.write()
            for shard in ("1/3", "0/4", "5/4"):
                with self.subTest(shard=shard):
                    with self.assertRaisesRegex(ValueError, "shard membership"):
                        export(root, data_file, "unit", shard, root / "bad.json")
                    self.assertFalse((root / "bad.json").exists())
            output = root / "nested/deeper/piece.json"
            export(root, data_file, "unit", "1/4", output)
            export(root, data_file, "unit", "1/4", output)
            self.assertTrue(output.is_file())
            data.erase()
            data.set_context("")
            data.add_lines({str(root / "src/example.py"): {2}})
            data.write()
            with self.assertRaisesRegex(ValueError, "no per-test call contexts"):
                export(root, data_file, "unit", "1/4", root / "unattributed.json")
            self.assertFalse((root / "unattributed.json").exists())

    def test_export_requires_each_argument_and_output_path_at_the_cli(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for missing in ("--data", "--shard", "--output"):
                arguments = {
                    "--data": str(root / "data"),
                    "--shard": "1/4",
                    "--output": str(root / "out"),
                }
                arguments.pop(missing)
                command = [
                    "pdm",
                    "run",
                    "python",
                    "-m",
                    "scripts.validation.mutation_coverage",
                    "export",
                    "unit",
                ]
                command += [part for pair in arguments.items() for part in pair]
                result = subprocess.run(  # noqa: S603 - fixed CLI, owned argument paths
                    command, cwd=ROOT, capture_output=True, text=True, timeout=30, check=False
                )
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                if missing == "--output":
                    self.assertIn("--output", result.stderr)
                else:
                    self.assertEqual(
                        json.loads(result.stdout),
                        {
                            "outcome": "INFRA",
                            "message": "Coverage export requires --data and --shard",
                        },
                    )

    def test_configure_writes_the_shared_child_context_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "nested/config"
            result = subprocess.run(  # noqa: S603 - fixed CLI and owned output
                [  # noqa: S607
                    "pdm",
                    "run",
                    "python",
                    "-m",
                    "scripts.validation.mutation_coverage",
                    "configure",
                    "unit",
                    "--output",
                    str(output),
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            self.assertTrue((output / ".coveragerc").is_file(), result.stdout + result.stderr)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn(
                "context = ${MUTATION_COVERAGE_CONTEXT-}", (output / ".coveragerc").read_text()
            )
            repeated = subprocess.run(  # noqa: S603 - identical fixed configure invocation
                result.args, cwd=ROOT, capture_output=True, text=True, timeout=30, check=False
            )
            self.assertEqual(repeated.returncode, 0, repeated.stdout + repeated.stderr)

    def test_each_producer_configuration_names_its_own_absolute_data_file(self):
        # Unit writes beside the repository; self-test and fixture producers run in separate
        # jobs, each writing acceptance/.coverage in its own workspace.
        with tempfile.TemporaryDirectory(prefix="producer root ") as directory:
            root = Path(directory).resolve()
            for domain, folder in (
                ("unit", ""),
                ("selftest", "acceptance/"),
                ("fixture", "acceptance/"),
            ):
                with self.subTest(domain=domain):
                    output = root / f"config {domain}"
                    result = coverage_cli(
                        "configure", domain, "--root", str(root), "--output", str(output)
                    )
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertEqual(
                        (output / ".coveragerc").read_text(),
                        "[run]\nbranch = True\ncore = ctrace\npatch = subprocess\n"
                        "context = ${MUTATION_COVERAGE_CONTEXT-}\n"
                        f"source =\n    {root}/src\n    {root}/scripts\n    {root}/acceptance/src\n"
                        f"data_file = {root}/{folder}.coverage\n",
                    )

    def test_a_producer_keeps_coverage_of_children_started_in_another_directory(self):
        # Given a test whose child process runs from another temporary directory, the
        # producer's exported mapping must still credit that test with the child's line.
        with tempfile.TemporaryDirectory(prefix="producer root ") as directory:
            root = Path(directory).resolve()
            repository(root)
            (root / "src/example.py").write_text(CHILD_SOURCE)
            (root / "tests/test_child.py").write_text(CHILD_TEST)
            config = root / "tmp/mutation-config"
            configured = coverage_cli(
                "configure", "unit", "--root", str(root), "--output", str(config)
            )
            self.assertEqual(configured.returncode, 0, configured.stdout + configured.stderr)
            produced = produce(
                root, config / ".coveragerc", "tests/test_example.py", "tests/test_child.py"
            )
            self.assertEqual(produced.returncode, 0, produced.stdout + produced.stderr)
            exported = coverage_cli(
                "export",
                "unit",
                "--root",
                str(root),
                "--data",
                str(root / ".coverage"),
                "--shard",
                "1/4",
                "--output",
                str(root / "unit.json"),
            )
            self.assertEqual(exported.returncode, 0, exported.stdout + exported.stderr)
            lines = json.loads((root / "unit.json").read_text())["mapping"]["src/example.py"]
            self.assertEqual(lines.get("6"), ["tests/test_child.py::test_child|run"])
            self.assertFalse((root / "acceptance/.coverage").exists())

    def test_check_reports_missing_producer_data_as_structured_infrastructure(self):
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(  # noqa: S603 - fixed CLI and owned path
                [  # noqa: S607
                    "pdm",
                    "run",
                    "python",
                    "-m",
                    "scripts.validation.mutation_coverage",
                    "check",
                    "unit",
                    "--output",
                    str(Path(directory) / "absent"),
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertTrue(result.stdout.strip(), "Missing structured INFRA result")
            record = json.loads(result.stdout)
            self.assertEqual(set(record), {"outcome", "message"})
            self.assertEqual(record["outcome"], "INFRA")
            self.assertIn("Missing coverage directory", record["message"])

    def test_real_sharded_coverage_matches_local_routes_and_mutant_verdict(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            partition_project(root)
            local = baseline(root, 30)
            output = root / "coverage"
            for domain, count in SHARDS.items():
                prefix = Path(FOLDERS[domain]).name
                for index in range(1, count + 1):
                    outcome, data = run_domain(
                        root, domain, [f"{prefix}/test_value.py::test_{index}"], 30, baseline=True
                    )
                    self.assertEqual(outcome["outcome"], "SURVIVED", outcome)
                    export(
                        root, data, domain, f"{index}/{count}", output / f"{domain}-{index}.json"
                    )
            imported = load(root, output)
            expected = expected_nodes()
            self.assertEqual(selection(local, "src/example.py", 2, 2), expected)
            self.assertEqual(selection(imported, "src/example.py", 2, 2), expected)
            mutants, _ = candidates("src/example.py", (root / "src/example.py").read_bytes())
            args = SimpleNamespace(timeout=30, workers=1)
            results = [schedule(root, mutants, mapping, args) for mapping in (local, imported)]
            outcomes = [verdict_rows(rows) for rows in results]
            self.assertEqual(outcomes[0], outcomes[1])
            self.assertEqual(
                outcomes[0], [("KILLED", "tests/test_value.py::test_1", "assertion")] * 3
            )
            invoked = cli(root, "--paths", "src/example.py", "--coverage-dir", str(output))
            self.assertEqual(invoked.returncode, 0, invoked.stdout + invoked.stderr)
            self.assertEqual(verdict_rows(cli_mutants(invoked)), outcomes[0])

    def test_fragment_membership_and_corruption_fail_before_selection(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repository(root)
            fragments(root, root / "coverage")
            path = root / "coverage/unit-1.json"
            original = path.read_text()
            for content in ("{", "[]", "null", original.replace('"version": 1', '"version": true')):
                with self.subTest(content=content[:20]):
                    path.write_text(content)
                    result = cli(
                        root,
                        "--base",
                        "HEAD",
                        "--head",
                        "HEAD",
                        "--coverage-dir",
                        str(root / "coverage"),
                    )
                    self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                    self.assertEqual(json.loads(result.stdout)["outcome"], "INFRA")
            path.write_text(original)
            path.rename(root / "missing.json")
            with self.assertRaisesRegex(ValueError, "Missing coverage shards"):
                load(root, root / "coverage")
            path.write_text(original)
            (root / "coverage/duplicate.json").write_text(original)
            with self.assertRaisesRegex(ValueError, "Duplicate"):
                load(root, root / "coverage")

    def test_complete_domain_fragments_merge_routes_without_recomputing_a_baseline(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repository(root)
            fragments(root, root / "coverage")
            mapping = load(root, root / "coverage")
            cli_output = root / "exported.json"
            exported = subprocess.run(  # noqa: S603 - fixed export interface and owned paths
                [  # noqa: S607
                    "pdm",
                    "run",
                    "python",
                    "-m",
                    "scripts.validation.mutation_coverage",
                    "export",
                    "unit",
                    "--root",
                    str(root),
                    "--data",
                    str(root / ".coverage-unit-1"),
                    "--shard",
                    "1/4",
                    "--output",
                    str(cli_output),
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            self.assertTrue(cli_output.is_file(), exported.stdout + exported.stderr)
            self.assertEqual(exported.returncode, 0, exported.stdout + exported.stderr)
            self.assertEqual(
                json.loads(cli_output.read_text()),
                json.loads((root / "coverage/unit-1.json").read_text()),
            )
            self.assertEqual(
                selection(mapping, "src/example.py", 2, 2),
                {
                    "unit": ["tests/test_example.py::test_value"],
                    "selftest": ["selftests/test_harness.py::test_value"],
                    "fixture": ["tests/test_contract.py::test_value"],
                },
            )
            # An empty catalogue still validates the explicit import, then exits without baselines.
            result = cli(
                root, "--base", "HEAD", "--head", "HEAD", "--coverage-dir", str(root / "coverage")
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(json.loads(result.stdout)["outcome"], "EMPTY")

    def test_invalid_fragment_contracts_are_infrastructure_not_silent_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repository(root)
            fragments(root, root / "coverage")
            path = root / "coverage/unit-1.json"
            original = json.loads(path.read_text())
            invalid = [
                {"version": 2},
                {"sha": "other"},
                {"inputs": "other"},
                {"domain": []},
                {"domain": "unknown"},
                {"shard": "8/4"},
                {"shard": []},
                {"mapping": {}},
                {"mapping": ["not a mapping"]},
                {"extra": "unexpected"},
                {"mapping": {"../outside.py": {"2": ["tests/t.py::test_x|run"]}}},
                {"mapping": {"/absolute.py": {"2": ["tests/t.py::test_x|run"]}}},
                {"mapping": {"src//example.py": {"2": ["tests/t.py::test_x|run"]}}},
                {"mapping": {"src/example.py": ["not lines"]}},
                {"mapping": {"src/example.py": {"0": ["tests/t.py::test_x|run"]}}},
                {"mapping": {"src/example.py": {"not-a-line": ["tests/t.py::test_x|run"]}}},
                {"mapping": {"src/example.py": {"+2": ["tests/t.py::test_x|run"]}}},
                {"mapping": {"src/example.py": {"2": [3]}}},
                {"mapping": {"src/example.py": {"2": {"tests/t.py::test_x|run": True}}}},
                {"mapping": {"src/example.py": {"2": ["tests/t.py::test_x|bad"]}}},
                {"mapping": {"src/example.py": {"2": ["tests/t.py::test_x|invalid"]}}},
                {
                    "mapping": {
                        "src/example.py": {
                            "2": ["tests/t.py::test_x|run", "tests/t.py::test_x|invalid"]
                        }
                    }
                },
                {"mapping": {"src/example.py": {"2": ["tests/../t.py::test_x|run"]}}},
                {"mapping": {"src/example.py": {"2": ["selftests/t.py::test_x|run"]}}},
                {"mapping": {"src/example.py": {"2": [""]}}},
            ]
            for change in invalid:
                with self.subTest(change=change):
                    path.write_text(json.dumps(original | deepcopy(change)))
                    result = cli(
                        root,
                        "--base",
                        "HEAD",
                        "--head",
                        "HEAD",
                        "--coverage-dir",
                        str(root / "coverage"),
                    )
                    self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                    self.assertEqual(json.loads(result.stdout)["outcome"], "INFRA")
            path.write_text(json.dumps(original))
            before = identity(root)
            (root / "src/example.py").write_text("def answer():\n    return False\n")
            self.assertNotEqual(identity(root), before)
            result = cli(
                root, "--base", "HEAD", "--head", "HEAD", "--coverage-dir", str(root / "coverage")
            )
            self.assertEqual(json.loads(result.stdout)["outcome"], "INFRA")

    def test_explicit_missing_coverage_is_infrastructure_even_with_no_changed_sites(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project(root, "def answer():\n    return True\n")
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)  # noqa: S607
            result = cli(root, "--paths", "README.md", "--coverage-dir", str(root / "absent"))

            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            rows = [json.loads(line) for line in result.stdout.splitlines()]
            self.assertEqual([row["outcome"] for row in rows], ["INFRA"])
            self.assertIn("coverage", rows[0]["message"].lower())
            self.assertNotIn("Baseline unit", rows[0]["message"])

    def test_coverage_command_missing_database_cannot_export_success(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = subprocess.run(  # noqa: S603 - fixed CLI and owned test paths
                [  # noqa: S607 - project PDM environment
                    "pdm",
                    "run",
                    "python",
                    "-m",
                    "scripts.validation.mutation_coverage",
                    "export",
                    "unit",
                    "--root",
                    str(root),
                    "--data",
                    str(root / "absent"),
                    "--shard",
                    "1/4",
                    "--output",
                    str(root / "piece.json"),
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertEqual(json.loads(result.stdout)["outcome"], "INFRA")
            self.assertFalse((root / "piece.json").exists())
