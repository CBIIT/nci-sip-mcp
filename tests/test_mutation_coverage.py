"""Shared CI baselines retain local routes and reject unusable inputs at the CLI."""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from coverage import CoverageData
from scripts.validation.mutation_child_trace import trace_directory
from scripts.validation.mutation_coverage import SHARDS, export, identity, load
from scripts.validation.mutation_gate import manifest_data, schedule
from scripts.validation.mutation_run import baseline, environment, run_domain, selection
from scripts.validation.mutation_source import candidates

from fakes import INSTALLED_CHILD_TEST, project
from test_mutation_gate import (
    ROOT,
    cli,
    composed_wheel_child_test,
    wheel_project,
)

TEST_BATCH = f"local-{'a' * 32}"


def expected_producer_id(batch, domain, shard):
    value = f"nci-mutation-producer-v1\0{batch}\0{domain}\0{shard}".encode()
    return hashlib.sha256(value).hexdigest()[:32]


def wheel_candidate(root, name, line, operator):
    mutants, _ = candidates(name, (root / name).read_bytes())
    selected = [item for item in mutants if item.line == line and item.operator == operator]
    if len(selected) != 1:
        raise ValueError(f"Expected one wheel candidate at {name}:{line}: {operator}")
    target = selected[0]
    count = 10007
    index = int(hashlib.sha256(json.dumps(target.key(), sort_keys=True).encode()).hexdigest(), 16)
    index = index % count + 1
    return target, f"{index}/{count}"


def repository(root):
    project(root, "def answer():\n    return True\n")
    commit_repository(root)


def commit_repository(root):
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


def wheel_verdict_project(root, child_test):
    wheel_project(root)
    (root / "src/example.py").write_text("def touch():\n    return True\n")
    (root / "tests/test_example.py").write_text(
        "import example\ndef test_value():\n    assert example.touch()\n"
    )
    module = root / "src/wheel_sample/cli.py"
    with module.open("a") as source:
        source.write(
            "import socket\n"
            "try:\n    socket.socket().connect(('127.0.0.1', 0))\n"
            "except OSError:\n    pass\n"
            "def never_called():\n    return 17\n"
        )
    (root / "src/wheel_sample/unimported.py").write_text("def never_loaded():\n    return 17\n")
    (root / "tests/test_installed_wheel.py").write_text(child_test)
    commit_repository(root)


def stale_wheel_child_test():
    route = (
        '        route = ChildRoute("src/wheel_sample/cli.py", "wheel_sample.cli", environment)\n'
    )
    stale = (
        '        if "return False" in (root / "src/wheel_sample/cli.py").read_text():\n'
        '            probe = subprocess.run([str(interpreter), "-c", '
        '"import sysconfig; print(sysconfig.get_paths()[\\"purelib\\"])"],\n'
        "                                   env=clean_env, capture_output=True, "
        "text=True, check=True)\n"
        '            installed_source = Path(probe.stdout.strip()) / "wheel_sample/cli.py"\n'
        "            original = installed_source.read_bytes()\n"
        '            stale_source = original.replace(b"return False", b"return True ")\n'
        "            assert stale_source != original and len(stale_source) == len(original)\n"
        "            stat = installed_source.stat()\n"
        "            cache = Path(importlib.util.cache_from_source(str(installed_source)))\n"
        "            cache.parent.mkdir(parents=True, exist_ok=True)\n"
        '            code = compile(stale_source, str(installed_source), "exec", '
        "dont_inherit=True, optimize=0)\n"
        "            cache.write_bytes(importlib._bootstrap_external._code_to_timestamp_pyc(\n"
        "                code, int(stat.st_mtime), len(original)))\n"
        "            assert installed_source.read_bytes() == original\n"
        "            assert installed_source.stat().st_mtime_ns == stat.st_mtime_ns\n"
    )
    return (
        composed_wheel_child_test()
        .replace(
            "import json, os, subprocess, sys, tempfile, venv\n",
            "import importlib.util, importlib._bootstrap_external, "
            "json, os, subprocess, sys, tempfile, venv\n",
        )
        .replace(route, stale + route)
    )


def producer_checkout(root, directory, domain, index):
    checkout = root / "producer-checkouts" / directory.name / f"{domain}-{index}"
    checkout.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(  # noqa: S603 - local fixture repository and owned destination
        ["git", "clone", "--quiet", "--shared", str(root), str(checkout)],  # noqa: S607
        check=True,
        capture_output=True,
    )
    return checkout


def configure_coverage(root, domain, shard, output, batch):
    env = {**os.environ, "MUTATION_COVERAGE_BATCH": batch}
    env.pop("GITHUB_ENV", None)
    return coverage_cli(
        "configure", domain, "--root", str(root), "--shard", shard, "--output", str(output), env=env
    )


def fragments(root, directory, batch=TEST_BATCH, *, skip=()):
    directory.mkdir()
    for domain, count in SHARDS.items():
        node = {
            "unit": "tests/test_example.py",
            "selftest": "selftests/test_harness.py",
            "fixture": "tests/test_contract.py",
        }[domain]
        for index in range(1, count + 1):
            if (domain, index) in skip:
                continue
            checkout = producer_checkout(root, directory, domain, index)
            shard = f"{index}/{count}"
            configured = configure_coverage(checkout, domain, shard, checkout / "config", batch)
            if configured.returncode != 0:
                raise RuntimeError(configured.stdout + configured.stderr)
            data_file = checkout / (".coverage" if domain == "unit" else "acceptance/.coverage")
            data = CoverageData(basename=str(data_file))
            data.set_context(f"{node}::test_value|run")
            data.add_lines({str(checkout / "src/example.py"): {2}})
            data.write()
            with patch.dict(os.environ, {"MUTATION_COVERAGE_BATCH": batch}):
                export(checkout, data_file, domain, shard, directory / f"{domain}-{index}.json")


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


def coverage_cli(*arguments, env=None):
    return subprocess.run(  # noqa: S603 - fixed CLI, owned argument paths
        ["pdm", "run", "python", "-m", "scripts.validation.mutation_coverage", *arguments],  # noqa: S607
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def produce(root, config, *tests, trace_environment=None):
    """Run a real producer as CI does: subject probe, shared config, child coverage startup."""
    env = environment(root, root / "tmp")  # drops inherited COVERAGE_* settings
    env["COVERAGE_PROCESS_START"] = str(config)
    env.update(trace_environment or {})
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
    def setUp(self):
        reservations = tempfile.TemporaryDirectory()
        self.addCleanup(reservations.cleanup)
        environment_patch = patch.dict(
            os.environ,
            {
                "MUTATION_COVERAGE_BATCH": TEST_BATCH,
                "MUTATION_COVERAGE_RESERVATIONS": str(Path(reservations.name).resolve()),
            },
        )
        environment_patch.start()
        self.addCleanup(environment_patch.stop)
        os.environ.pop("GITHUB_ENV", None)

    def imported_wheel_coverage(self, root):
        output = root / "coverage"
        fragments(root, output, skip=(("unit", 1),))
        config = root / "tmp/mutation-config"
        env_file = root / "github-env"
        configured = coverage_cli(
            "configure",
            "unit",
            "--root",
            str(root),
            "--shard",
            "1/4",
            "--output",
            str(config),
            env={**os.environ, "GITHUB_ENV": str(env_file)},
        )
        self.assertEqual(configured.returncode, 0, configured.stdout + configured.stderr)
        trace_env = dict(line.split("=", 1) for line in env_file.read_text().splitlines())
        produced = produce(
            root,
            config / ".coveragerc",
            "tests/test_example.py",
            "tests/test_installed_wheel.py",
            trace_environment=trace_env,
        )
        self.assertEqual(produced.returncode, 0, produced.stdout + produced.stderr)
        export(root, root / ".coverage", "unit", "1/4", output / "unit-1.json")
        return output

    def test_real_wheel_mutant_outcomes_match_local_and_imported_coverage(self):
        hooks = '        hooks = work / "sentinel-hooks"\n'
        declaration = (
            "        with patch.dict(os.environ, child_env, clear=True):\n"
            '            route3 = ChildRoute("src/wheel_sample/unimported.py",\n'
            '                                "wheel_sample.unimported", environment)\n'
            "            child_env = child_environment(route3)\n"
        )
        child_test = composed_wheel_child_test().replace(hooks, declaration + hooks)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            wheel_verdict_project(root, child_test)
            module = root / "src/wheel_sample/cli.py"
            imported = self.imported_wheel_coverage(root)
            unused_line = module.read_text().splitlines().index("    return 17") + 1
            cases = (
                ("src/wheel_sample/cli.py", 3, "return:False", "KILLED", 0),
                ("src/wheel_sample/__init__.py", 1, "boolean:literal", "SURVIVED", 1),
                ("src/wheel_sample/cli.py", unused_line, "return:False", "NO_TESTS", 1),
                # Declared as a child route, but the child never imports it.
                ("src/wheel_sample/unimported.py", 2, "return:False", "NO_TESTS", 1),
            )
            for name, line, operator, expected, exit_code in cases:
                target, shard = wheel_candidate(root, name, line, operator)
                allowlist = root / "wheel-allowlist.json"
                allowlist.write_text(json.dumps([{**target.key(), "reason": "contract canary"}]))
                for mode in ("local", "imported"):
                    with self.subTest(outcome=expected, mode=mode):
                        arguments = ["--paths", name, "--shard", shard, "-n", "1"]
                        if mode == "imported":
                            arguments += ["--coverage-dir", str(imported)]
                        if expected == "NO_TESTS":
                            arguments += ["--allowlist", str(allowlist)]
                        result = cli(root, *arguments)
                        rows = cli_mutants(result)
                        self.assertEqual(
                            result.returncode, exit_code, result.stdout + result.stderr
                        )
                        self.assertEqual(len(rows), 1, result.stdout)
                        self.assertEqual(
                            (
                                rows[0]["file"],
                                rows[0]["line"],
                                rows[0]["operator"],
                                rows[0]["outcome"],
                            ),
                            (name, line, operator, expected),
                        )
                        if expected == "KILLED":
                            self.assertEqual(
                                rows[0]["node"], "tests/test_installed_wheel.py::test_wheel"
                            )
                            self.assertEqual(rows[0]["kind"], "assertion")

    def test_real_wheel_stale_bytecode_is_infrastructure_in_both_coverage_modes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            wheel_verdict_project(root, stale_wheel_child_test())
            imported = self.imported_wheel_coverage(root)
            name = "src/wheel_sample/cli.py"
            _, shard = wheel_candidate(root, name, 3, "return:False")
            for mode in ("local", "imported"):
                with self.subTest(mode=mode):
                    arguments = ["--paths", name, "--shard", shard, "-n", "1"]
                    if mode == "imported":
                        arguments += ["--coverage-dir", str(imported)]
                    result = cli(root, *arguments)
                    rows = cli_mutants(result)
                    self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                    self.assertEqual(len(rows), 1, result.stdout)
                    self.assertEqual(rows[0]["outcome"], "INFRA")
                    self.assertIn("Loaded bytecode does not match", rows[0]["message"])

    def test_configure_binds_producer_to_the_external_batch_and_slot(self):
        for batch in (TEST_BATCH, "gha-123-456-7"):
            with self.subTest(batch=batch), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                reservations = root / "reservations"
                output = root / "config"
                environment_file = root / "github-env"
                env = {
                    **os.environ,
                    "GITHUB_ENV": str(environment_file),
                    "MUTATION_COVERAGE_BATCH": batch,
                    "MUTATION_COVERAGE_RESERVATIONS": str(reservations),
                }
                result = coverage_cli(
                    "configure",
                    "unit",
                    "--root",
                    str(root),
                    "--shard",
                    "1/4",
                    "--output",
                    str(output),
                    env=env,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                run_id = expected_producer_id(batch, "unit", "1/4")
                trace_environment = dict(
                    line.split("=", 1) for line in environment_file.read_text().splitlines()
                )
                producer = json.loads(
                    (trace_directory(root / ".coverage") / "producer.json").read_text()
                )
                self.assertEqual(trace_environment["MUTATION_CHILD_TRACE_RUN"], run_id)
                self.assertEqual(producer["run_id"], run_id)
                self.assertEqual([path.name for path in reservations.iterdir()], [run_id])

    def test_repeated_configure_in_another_output_cannot_reuse_a_reserved_slot(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            reservations = root / "reservations"
            env = {
                **os.environ,
                "MUTATION_COVERAGE_BATCH": TEST_BATCH,
                "MUTATION_COVERAGE_RESERVATIONS": str(reservations),
            }
            arguments = ("configure", "unit", "--root", str(root), "--shard", "1/4")
            first = coverage_cli(*arguments, "--output", str(root / "first"), env=env)
            self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
            original = (trace_directory(root / ".coverage") / "producer.json").read_bytes()

            repeated = coverage_cli(*arguments, "--output", str(root / "second"), env=env)

            self.assertEqual(repeated.returncode, 2, repeated.stdout + repeated.stderr)
            self.assertEqual(json.loads(repeated.stdout)["outcome"], "INFRA")
            self.assertFalse((root / "second").exists())
            self.assertEqual(
                (trace_directory(root / ".coverage") / "producer.json").read_bytes(), original
            )

    def test_configure_rejects_noncanonical_shards_before_reserving_or_initializing(self):
        cases = (
            ("unit", "1/4", ("01/4", "1/04")),
            ("selftest", "8/8", ("08/8", "8/08")),
            ("fixture", "1/1", ("01/1", "1/01")),
        )
        for domain, shard, aliases in cases:
            with self.subTest(domain=domain), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                reservations = root / "reservations"
                canonical = root / "canonical"
                canonical.mkdir()
                env = {
                    **os.environ,
                    "MUTATION_COVERAGE_BATCH": TEST_BATCH,
                    "MUTATION_COVERAGE_RESERVATIONS": str(reservations),
                }
                accepted = coverage_cli(
                    "configure",
                    domain,
                    "--root",
                    str(canonical),
                    "--shard",
                    shard,
                    "--output",
                    str(canonical / "config"),
                    env=env,
                )
                self.assertEqual(accepted.returncode, 0, accepted.stdout + accepted.stderr)
                expected_slot = expected_producer_id(TEST_BATCH, domain, shard)
                self.assertEqual({path.name for path in reservations.iterdir()}, {expected_slot})
                self.assertTrue((canonical / "config/.coveragerc").is_file())
                for index, alias in enumerate(aliases):
                    with self.subTest(alias=alias):
                        snapshot = root / f"alias-{index}"
                        snapshot.mkdir()
                        output = snapshot / "config"
                        data = snapshot / (
                            ".coverage" if domain == "unit" else "acceptance/.coverage"
                        )
                        rejected = coverage_cli(
                            "configure",
                            domain,
                            "--root",
                            str(snapshot),
                            "--shard",
                            alias,
                            "--output",
                            str(output),
                            env=env,
                        )
                        self.assertEqual(rejected.returncode, 2, rejected.stdout + rejected.stderr)
                        self.assertEqual(
                            json.loads(rejected.stdout),
                            {"outcome": "INFRA", "message": "Invalid coverage shard membership"},
                        )
                        self.assertFalse(output.exists())
                        self.assertFalse(trace_directory(data).exists())
                        self.assertEqual(
                            {path.name for path in reservations.iterdir()}, {expected_slot}
                        )

    def test_configure_rejects_missing_or_malformed_campaign_inputs_before_output(self):
        cases = (
            ("missing batch", None, "owned"),
            ("malformed batch", "local-not-hex", "owned"),
            ("zero github field", "gha-123-0-7", "owned"),
            ("non-ascii github field", "gha-123-\uff14-7", "owned"),
            ("missing reservation root", TEST_BATCH, None),
            ("relative reservation root", TEST_BATCH, "relative-reservations"),
        )
        for name, batch, reservation in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                output = root / "config"
                env = {**os.environ, "GITHUB_ENV": str(root / "github-env")}
                env.pop("MUTATION_COVERAGE_BATCH", None)
                env.pop("MUTATION_COVERAGE_RESERVATIONS", None)
                if batch is not None:
                    env["MUTATION_COVERAGE_BATCH"] = batch
                if reservation is not None:
                    env["MUTATION_COVERAGE_RESERVATIONS"] = (
                        str(root / "reservations") if reservation == "owned" else reservation
                    )
                result = coverage_cli(
                    "configure",
                    "unit",
                    "--root",
                    str(root),
                    "--shard",
                    "1/4",
                    "--output",
                    str(output),
                    env=env,
                )
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertEqual(json.loads(result.stdout)["outcome"], "INFRA")
                self.assertFalse(output.exists())

    def test_configure_rejects_reservation_root_inside_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            output = root / "config"
            env = {
                **os.environ,
                "MUTATION_COVERAGE_BATCH": TEST_BATCH,
                "MUTATION_COVERAGE_RESERVATIONS": str(output / "reservations"),
            }
            result = coverage_cli(
                "configure",
                "unit",
                "--root",
                str(root),
                "--shard",
                "1/4",
                "--output",
                str(output),
                env=env,
            )
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertEqual(json.loads(result.stdout)["outcome"], "INFRA")
            self.assertFalse(output.exists())

    def test_complete_prior_campaign_fails_when_the_consumer_uses_a_new_batch(self):
        batches = (TEST_BATCH, f"local-{'b' * 32}")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repository(root)
            (root / "tests/test_installed_child.py").write_text(INSTALLED_CHILD_TEST)
            subprocess.run(["git", "add", "tests/test_installed_child.py"], cwd=root, check=True)  # noqa: S607
            subprocess.run(
                [  # noqa: S607 - fixed commit in an owned fixture repository
                    "git",
                    "-c",
                    "user.name=Test role",
                    "-c",
                    "user.email=test@example.invalid",
                    "commit",
                    "-qm",
                    "test: installed child campaign",
                ],
                cwd=root,
                check=True,
            )
            campaigns = {batch: root / f"coverage-{index}" for index, batch in enumerate(batches)}
            fragments(root, campaigns[batches[0]], batches[0], skip=(("unit", 1),))
            fragments(root, campaigns[batches[1]], batches[1])

            config = root / "tmp/mutation-config-a"
            environment_file = root / "github-env-a"
            env_a = {
                **os.environ,
                "GITHUB_ENV": str(environment_file),
                "MUTATION_COVERAGE_BATCH": TEST_BATCH,
            }
            configured = coverage_cli(
                "configure",
                "unit",
                "--root",
                str(root),
                "--shard",
                "1/4",
                "--output",
                str(config),
                env=env_a,
            )
            self.assertEqual(configured.returncode, 0, configured.stdout + configured.stderr)
            trace_environment = dict(
                line.split("=", 1) for line in environment_file.read_text().splitlines()
            )
            produced = produce(
                root,
                config / ".coveragerc",
                "tests/test_example.py",
                "tests/test_installed_child.py",
                trace_environment=trace_environment,
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
                str(campaigns[TEST_BATCH] / "unit-1.json"),
                env={**os.environ, "MUTATION_COVERAGE_BATCH": TEST_BATCH},
            )
            self.assertEqual(exported.returncode, 0, exported.stdout + exported.stderr)
            prior_fragment = json.loads((campaigns[TEST_BATCH] / "unit-1.json").read_text())
            self.assertTrue(prior_fragment["children"])
            self.assertEqual(
                {child["run_id"] for child in prior_fragment["children"]},
                {prior_fragment["producer"]["run_id"]},
            )

            prior_batch, current_batch = batches
            current_env = {**os.environ, "MUTATION_COVERAGE_BATCH": current_batch}
            with patch.dict(os.environ, {"MUTATION_COVERAGE_BATCH": prior_batch}):
                imported = cli(
                    root,
                    "--base",
                    "HEAD",
                    "--head",
                    "HEAD",
                    "--coverage-dir",
                    str(campaigns[prior_batch]),
                )
            with patch.dict(os.environ, current_env):
                replayed = cli(
                    root,
                    "--base",
                    "HEAD",
                    "--head",
                    "HEAD",
                    "--coverage-dir",
                    str(campaigns[prior_batch]),
                )
                current = cli(
                    root,
                    "--base",
                    "HEAD",
                    "--head",
                    "HEAD",
                    "--coverage-dir",
                    str(campaigns[current_batch]),
                )

            self.assertEqual(imported.returncode, 0, imported.stdout + imported.stderr)
            self.assertEqual(replayed.returncode, 2, replayed.stdout + replayed.stderr)
            self.assertEqual(json.loads(replayed.stdout)["outcome"], "INFRA")
            self.assertEqual(current.returncode, 0, current.stdout + current.stderr)
            self.assertEqual(json.loads(current.stdout)["outcome"], "EMPTY")

            check_a = coverage_cli(
                "check",
                "unit",
                "--root",
                str(root),
                "--output",
                str(campaigns[prior_batch]),
                env={**os.environ, "MUTATION_COVERAGE_BATCH": prior_batch},
            )
            replayed_check = coverage_cli(
                "check",
                "unit",
                "--root",
                str(root),
                "--output",
                str(campaigns[prior_batch]),
                env={**os.environ, "MUTATION_COVERAGE_BATCH": current_batch},
            )
            check_b = coverage_cli(
                "check",
                "unit",
                "--root",
                str(root),
                "--output",
                str(campaigns[current_batch]),
                env={**os.environ, "MUTATION_COVERAGE_BATCH": current_batch},
            )
            self.assertEqual(check_a.returncode, 0, check_a.stdout + check_a.stderr)
            self.assertEqual(
                replayed_check.returncode, 2, replayed_check.stdout + replayed_check.stderr
            )
            self.assertEqual(json.loads(replayed_check.stdout)["outcome"], "INFRA")
            self.assertEqual(check_b.returncode, 0, check_b.stdout + check_b.stderr)

            for batch in (None, "local-short"):
                env = os.environ.copy()
                if batch is None:
                    env.pop("MUTATION_COVERAGE_BATCH", None)
                else:
                    env["MUTATION_COVERAGE_BATCH"] = batch
                result = coverage_cli(
                    "check",
                    "unit",
                    "--root",
                    str(root),
                    "--output",
                    str(campaigns[current_batch]),
                    env=env,
                )
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertEqual(json.loads(result.stdout)["outcome"], "INFRA")

    def test_consumer_requires_its_own_attempt_without_invalidating_a_retained_campaign(self):
        prior_batch = "gha-123-456-1"
        current_batch = "gha-123-456-2"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repository(root)
            prior = root / "prior"
            current = root / "current"
            fragments(root, prior, prior_batch)
            fragments(root, current, current_batch)

            def consume(fragment_dir, batch):
                with patch.dict(os.environ, {"MUTATION_COVERAGE_BATCH": batch}):
                    return cli(
                        root,
                        "--base",
                        "HEAD",
                        "--head",
                        "HEAD",
                        "--coverage-dir",
                        str(fragment_dir),
                    )

            replayed = consume(prior, current_batch)
            self.assertEqual(replayed.returncode, 2, replayed.stdout + replayed.stderr)
            self.assertEqual(
                json.loads(replayed.stdout),
                {
                    "outcome": "INFRA",
                    "message": "Coverage fragment producer does not match the campaign batch",
                    "type": "ValueError",
                },
            )

            fixture = current / "fixture-1.json"
            retained = root / "retained-fixture.json"
            fixture.rename(retained)
            incomplete = consume(current, current_batch)
            self.assertEqual(incomplete.returncode, 2, incomplete.stdout + incomplete.stderr)
            self.assertEqual(
                json.loads(incomplete.stdout),
                {
                    "outcome": "INFRA",
                    "message": "Missing coverage shards: [('fixture', '1/1')]",
                    "type": "ValueError",
                },
            )
            retained.rename(fixture)
            for fragment_dir, batch in ((current, current_batch), (prior, prior_batch)):
                with self.subTest(batch=batch):
                    result = consume(fragment_dir, batch)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertEqual(json.loads(result.stdout), {"outcome": "EMPTY", "mutants": 0})

    def test_export_rejects_a_producer_not_matching_the_external_batch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repository(root)
            data_file = root / ".coverage"
            data = CoverageData(basename=str(data_file))
            data.set_context("tests/test_example.py::test_value|run")
            data.add_lines({str(root / "src/example.py"): {2}})
            data.write()
            configured = configure_coverage(root, "unit", "1/4", root / "hook", f"local-{'b' * 32}")
            self.assertEqual(configured.returncode, 0, configured.stdout + configured.stderr)
            output = root / "batch-a.json"
            result = coverage_cli(
                "export",
                "unit",
                "--root",
                str(root),
                "--data",
                str(data_file),
                "--shard",
                "1/4",
                "--output",
                str(output),
                env={**os.environ, "MUTATION_COVERAGE_BATCH": TEST_BATCH},
            )
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertEqual(json.loads(result.stdout)["outcome"], "INFRA")
            self.assertFalse(output.exists())

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
            configured = configure_coverage(root, "unit", "1/4", root / "hook", TEST_BATCH)
            self.assertEqual(configured.returncode, 0, configured.stdout + configured.stderr)
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
            environment_file = Path(directory) / "github-env"
            env = {
                **environment(Path(directory), output),
                "GITHUB_ENV": str(environment_file),
                "MUTATION_COVERAGE_BATCH": TEST_BATCH,
                "MUTATION_COVERAGE_RESERVATIONS": os.environ["MUTATION_COVERAGE_RESERVATIONS"],
            }
            result = subprocess.run(  # noqa: S603 - fixed CLI and owned output
                [  # noqa: S607
                    "pdm",
                    "run",
                    "python",
                    "-m",
                    "scripts.validation.mutation_coverage",
                    "configure",
                    "unit",
                    "--root",
                    str(Path(directory).resolve()),
                    "--shard",
                    "1/4",
                    "--output",
                    str(output),
                ],
                cwd=ROOT,
                env=env,
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
            self.assertEqual(
                {line.split("=", 1)[0] for line in environment_file.read_text().splitlines()},
                {
                    "MUTATION_CHILD_TRACE_DIR",
                    "MUTATION_CHILD_TRACE_HOOK",
                    "MUTATION_CHILD_TRACE_RUN",
                    "MUTATION_CHILD_TRACE_DOMAIN",
                    "MUTATION_CHILD_TRACE_SHARD",
                    "MUTATION_CHILD_TRACE_PHASE",
                },
            )
            repeated = subprocess.run(  # noqa: S603 - identical fixed configure invocation
                result.args, cwd=ROOT, capture_output=True, text=True, timeout=30, check=False
            )
            self.assertEqual(repeated.returncode, 2, repeated.stdout + repeated.stderr)

    def test_each_producer_configuration_names_its_own_absolute_data_file(self):
        # Unit writes beside the repository; self-test and fixture producers run in separate
        # jobs, each writing acceptance/.coverage in its own workspace.
        with tempfile.TemporaryDirectory(prefix="producer root ") as directory:
            root = Path(directory).resolve()
            for domain, folder, shard in (
                ("unit", "", "1/4"),
                ("selftest", "acceptance/", "1/8"),
                ("fixture", "acceptance/", "1/1"),
            ):
                with self.subTest(domain=domain):
                    producer_root = root / domain
                    producer_root.mkdir()
                    output = producer_root / f"config {domain}"
                    result = coverage_cli(
                        "configure",
                        domain,
                        "--root",
                        str(producer_root),
                        "--shard",
                        shard,
                        "--output",
                        str(output),
                    )
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertEqual(
                        (output / ".coveragerc").read_text(),
                        "[run]\nbranch = True\ncore = ctrace\npatch = subprocess\n"
                        "context = ${MUTATION_COVERAGE_CONTEXT-}\n"
                        f"source =\n    {producer_root}/src\n    {producer_root}/scripts\n"
                        f"    {producer_root}/acceptance/src\n"
                        f"data_file = {producer_root}/{folder}.coverage\n",
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
                "configure", "unit", "--root", str(root), "--shard", "1/4", "--output", str(config)
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
            output.mkdir()
            for domain, count in SHARDS.items():
                prefix = Path(FOLDERS[domain]).name
                for index in range(1, count + 1):
                    checkout = producer_checkout(root, output, domain, index)
                    outcome, data = run_domain(
                        checkout,
                        domain,
                        [f"{prefix}/test_value.py::test_{index}"],
                        30,
                        baseline=True,
                    )
                    self.assertEqual(outcome["outcome"], "SURVIVED", outcome)
                    shard = f"{index}/{count}"
                    configured = configure_coverage(
                        checkout, domain, shard, checkout / "config", TEST_BATCH
                    )
                    self.assertEqual(
                        configured.returncode, 0, configured.stdout + configured.stderr
                    )
                    export_data = checkout / (
                        ".coverage" if domain == "unit" else "acceptance/.coverage"
                    )
                    shutil.copyfile(data, export_data)
                    export(checkout, export_data, domain, shard, output / f"{domain}-{index}.json")
            imported = load(root, output)
            expected = expected_nodes()
            self.assertEqual(selection(local.lines, "src/example.py", 2, 2), expected)
            self.assertEqual(selection(imported.lines, "src/example.py", 2, 2), expected)
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

    def test_imported_coverage_preserves_installed_child_owner_and_kill_evidence(self):
        source = "def answer():\n    return 1\n"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            project(root, source, unit="assert coverage_sample.touch()", contract="assert True")
            (root / "src/coverage_sample.py").write_text("def touch():\n    return True\n")
            (root / "tests/test_example.py").write_text(
                "import coverage_sample\ndef test_value():\n    assert coverage_sample.touch()\n"
            )
            shutil.copyfile(
                ROOT / "scripts/validation/mutation_child_trace.py",
                root / "mutation_child_trace.py",
            )
            child_test = (
                INSTALLED_CHILD_TEST.replace(
                    "from scripts.validation.mutation_child_trace",
                    "from mutation_child_trace",
                )
                .replace(
                    "'import example; print(example.__file__)'",
                    "'import example; print(example.__file__); "
                    "print(type(example.answer()) is int and example.answer() == 1)'",
                )
                .replace(
                    "    imported = Path(child.stdout.strip()).resolve()\n"
                    "    assert imported.is_relative_to(environment.resolve())\n",
                    "    imported = Path(child.stdout.splitlines()[0]).resolve()\n"
                    "    assert imported.is_relative_to(environment.resolve())\n"
                    "    assert child.stdout.splitlines()[1] == 'True'\n",
                )
            )
            (root / "tests/test_installed_child.py").write_text(child_test)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)  # noqa: S607
            subprocess.run(["git", "add", "."], cwd=root, check=True)  # noqa: S607
            subprocess.run(
                [  # noqa: S607 - fixed command in a temporary fixture repository
                    "git",
                    "-c",
                    "user.name=Test role",
                    "-c",
                    "user.email=test@example.invalid",
                    "commit",
                    "-qm",
                    "test: installed child",
                ],
                cwd=root,
                check=True,
            )

            fragments(root, root / "coverage", skip=(("unit", 1),))
            config = root / "tmp/mutation-config"
            env_file = root / "github-env"
            configured = coverage_cli(
                "configure",
                "unit",
                "--root",
                str(root),
                "--shard",
                "1/4",
                "--output",
                str(config),
                env={**os.environ, "GITHUB_ENV": str(env_file)},
            )
            self.assertEqual(configured.returncode, 0, configured.stdout + configured.stderr)
            trace_environment = dict(
                line.split("=", 1) for line in env_file.read_text().splitlines()
            )
            produced = produce(
                root,
                config / ".coveragerc",
                "tests/test_example.py",
                "tests/test_installed_child.py",
                trace_environment=trace_environment,
            )
            self.assertEqual(produced.returncode, 0, produced.stdout + produced.stderr)
            export(root, root / ".coverage", "unit", "1/4", root / "coverage/unit-1.json")

            imported = load(root, root / "coverage")
            node = "tests/test_installed_child.py::test_value"
            mutants, _ = candidates("src/example.py", source.encode())
            local = baseline(root, 30)
            for mapping in (local, imported):
                manifest = manifest_data(root, mutants, mapping, 30)["mutants"]
                self.assertEqual(
                    [node in item["selected"].get("unit", []) for item in manifest],
                    [True] * len(mutants),
                )
                self.assertEqual(
                    [[route["node"] for route in item["child_routes"]] for item in manifest],
                    [[node]] * len(mutants),
                )
            local_result = cli(root, "--paths", "src/example.py", "-n", "1")
            imported_result = cli(
                root,
                "--paths",
                "src/example.py",
                "--coverage-dir",
                str(root / "coverage"),
                "-n",
                "1",
            )
            expected_verdicts = [("KILLED", node, "assertion")] * len(mutants)
            self.assertEqual(local_result.returncode, 0, local_result.stdout + local_result.stderr)
            self.assertEqual(
                imported_result.returncode, 0, imported_result.stdout + imported_result.stderr
            )
            self.assertEqual(verdict_rows(cli_mutants(local_result)), expected_verdicts)
            self.assertEqual(verdict_rows(cli_mutants(imported_result)), expected_verdicts)

            fragment_path = root / "coverage/unit-1.json"
            fragment = json.loads(fragment_path.read_text())
            source_digest = fragment["children"][0]["source_digest"]
            fragment["children"][0]["source_digest"] = "0" * 64
            fragment_path.write_text(json.dumps(fragment))
            stale_result = cli(
                root,
                "--paths",
                "src/example.py",
                "--coverage-dir",
                str(root / "coverage"),
                "-n",
                "1",
            )
            record = json.loads(stale_result.stdout)
            self.assertEqual(
                (
                    stale_result.returncode,
                    record["outcome"],
                    "source differs from producer" in record["message"],
                ),
                (2, "INFRA", True),
                stale_result.stdout + stale_result.stderr,
            )

            fragment["children"][0]["source_digest"] = source_digest
            fragment["producer"]["run_id"] = "0" * 32
            fragment_path.write_text(json.dumps(fragment))
            stale_result = cli(
                root,
                "--paths",
                "src/example.py",
                "--coverage-dir",
                str(root / "coverage"),
                "-n",
                "1",
            )
            record = json.loads(stale_result.stdout)
            self.assertEqual(
                (
                    stale_result.returncode,
                    record["outcome"],
                    "producer" in record["message"].lower(),
                ),
                (2, "INFRA", True),
                stale_result.stdout + stale_result.stderr,
            )

    def test_fragment_membership_and_corruption_fail_before_selection(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repository(root)
            fragments(root, root / "coverage")
            path = root / "coverage/unit-1.json"
            original = path.read_text()
            for content in ("{", "[]", "null", original.replace('"version": 2', '"version": true')):
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
            fragment = json.loads(original)
            for field, malformed in (("producer", []), ("children", None)):
                with self.subTest(field=field):
                    fragment[field] = malformed
                    path.write_text(json.dumps(fragment))
                    with self.assertRaisesRegex(ValueError, "Invalid coverage child trace records"):
                        load(root, root / "coverage")
                    fragment = json.loads(original)
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
            checkout = root / "producer-checkouts/coverage/unit-1"
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
                    str(checkout),
                    "--data",
                    str(checkout / ".coverage"),
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
                selection(mapping.lines, "src/example.py", 2, 2),
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
                {"version": 3},
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

    def test_a_non_ascii_digit_line_key_is_an_invalid_fragment(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repository(root)
            fragments(root, root / "coverage")
            path = root / "coverage/unit-1.json"
            fragment = json.loads(path.read_text())
            fragment["mapping"] = {"src/example.py": {"\u00b2": ["tests/t.py::test_x|run"]}}
            path.write_text(json.dumps(fragment))
            result = cli(
                root, "--base", "HEAD", "--head", "HEAD", "--coverage-dir", str(root / "coverage")
            )
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            row = json.loads(result.stdout)
            self.assertEqual(
                (row["outcome"], row["message"]),
                ("INFRA", "Invalid coverage line/context shape"),
            )

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
