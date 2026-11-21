"""A traced installed child yields evidence only for the process the owning test started."""

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from scripts.validation import mutation_child_trace as child_trace
from scripts.validation import mutation_gate
from scripts.validation.mutation_run import (
    CoverageEvidence,
    child_selection,
    native_candidate_owners,
    run_domain,
    validate_baseline_sources,
)
from scripts.validation.mutation_source import candidates

from test_mutation_gate import INSTALLED_CHILD_TEST, project

TEST_PATH = "tests/test_installed_child.py"
NODE = f"{TEST_PATH}::test_value"
CHILD_COMMAND = "'import example; print(example.__file__)'"
FIRST_CALL_LINE = 3
LIST_ELEMENT_LINE = 3
RUN_CHILD = "    child = subprocess.run(\n"
GRANDCHILD_SOURCE = (
    "import os, sys, time\n"
    "open(sys.argv[2], 'w').write(str(os.getpid()))\n"
    "while not os.path.exists(sys.argv[1]):\n"
    "    time.sleep(0.05)\n"
)
SPAWN_GRANDCHILD = (
    "import example, os, subprocess, sys\n"
    "subprocess.Popen(\n"
    "    [sys.executable, '-c', open(os.environ['GRANDCHILD']).read(),\n"
    "     os.environ['RELEASE'], os.environ['PIDFILE']],\n"
    "    start_new_session=True, stdin=subprocess.DEVNULL,\n"
    "    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,\n"
    ")\n"
    "print(example.__file__)\n"
)
RELEASE_GRANDCHILD_AFTER_CHILD_EXIT = (
    "    import os, time\n"
    "    deadline = time.monotonic() + 30\n"
    "    while time.monotonic() < deadline and not (\n"
    "        pidfile.exists() and pidfile.read_text().isdigit()\n"
    "    ):\n"
    "        time.sleep(0.05)\n"
    "    pid = int(pidfile.read_text())\n"
    "    release.write_text('go')\n"
    "    while time.monotonic() < deadline:\n"
    "        try:\n"
    "            os.kill(pid, 0)\n"
    "        except ProcessLookupError:\n"
    "            break\n"
    "        time.sleep(0.05)\n"
    "    else:\n"
    "        raise AssertionError('grandchild did not finish')\n"
)
RELOAD_WITH_OTHER_BYTES = (
    "import example, importlib, pathlib\n"
    "pathlib.Path(example.__file__).write_text('value = 22\\n')\n"
    "importlib.reload(example)\n"
    "print(example.__file__)\n"
)
TRACER_FAILURE = (
    "import example, types\n"
    "bad = (lambda: 1).__code__.replace(co_filename='a\\0b')\n"
    "try:\n"
    "    types.FunctionType(bad, {'__name__': 'example'})()\n"
    "except ValueError:\n"
    "    pass\n"
    "print(example.__file__)\n"
)
TRACER_FAILS_TWICE = (
    "import example, types\n"
    "bad = (lambda: 1).__code__.replace(co_filename='a\\0b')\n"
    "try:\n"
    "    types.FunctionType(bad, {'__name__': 'example'})()\n"
    "except ValueError:\n"
    "    pass\n"
    "class Globals(dict):\n"
    "    def get(self, *arguments):\n"
    "        raise RuntimeError('second failure')\n"
    "exec(compile('1', 'x', 'exec'), Globals())\n"
    "print(example.__file__)\n"
)
COLLAPSED_STATEMENT_CHILD = (
    "import example, pathlib\n"
    "text = pathlib.Path(example.__file__).read_text()\n"
    "example.second() if 'pass' in text else example.first()\n"
    "print(example.__file__)\n"
)
COLLAPSIBLE_SOURCE = (
    "VALUES = []\n"
    "def first():\n"
    "    VALUES.append(\n"
    "        1,\n"
    "    )\n"
    "def second():\n"
    "    VALUES.append(2)\n"
)


def installed_child_test(code, *, before_child="", environment="child_env", after_child=""):
    return (
        INSTALLED_CHILD_TEST.replace(CHILD_COMMAND, repr(code))
        .replace("[str(interpreter), '-c'", "[str(interpreter), '-B', '-c'")
        .replace(RUN_CHILD, before_child + RUN_CHILD)
        .replace("env=child_env", f"env={environment}")
        + after_child
    )


def grandchild_test():
    before = (
        "    release, pidfile = root / 'release', root / 'pid'\n"
        "    (root / 'grandchild.py').write_text(" + repr(GRANDCHILD_SOURCE) + ")\n"
    )
    environment = (
        "{**child_env, 'GRANDCHILD': str(root / 'grandchild.py'), "
        "'RELEASE': str(release), 'PIDFILE': str(pidfile)}"
    )
    return installed_child_test(
        SPAWN_GRANDCHILD,
        before_child=before,
        environment=environment,
        after_child=RELEASE_GRANDCHILD_AFTER_CHILD_EXIT,
    )


def baseline_run(root, test, source="value = 1\n"):
    project(root, source)
    (root / TEST_PATH).write_text(test)
    return run_domain(root, "unit", [TEST_PATH], 60, baseline=True)


class ChildTraceFidelityTest(unittest.TestCase):
    def test_a_grandchild_python_process_does_not_overwrite_the_traced_child_record(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            outcome, data = baseline_run(root, grandchild_test())
            self.assertEqual(outcome["outcome"], "SURVIVED", outcome["log"])
            _, traces = child_trace.read_traces(data)
            self.assertEqual(
                [(trace.node, trace.outcome, trace.lines) for trace in traces],
                [(NODE, "reached", (1,))],
            )

    def test_reloading_a_module_with_other_bytes_makes_the_record_invalid(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            outcome, _ = baseline_run(root, installed_child_test(RELOAD_WITH_OTHER_BYTES))
            self.assertEqual(outcome["outcome"], "INFRA", outcome)
            self.assertEqual(
                outcome["message"],
                "Installed child trace is invalid: Module executed more than one source",
            )

    def test_an_error_inside_the_tracer_makes_the_record_invalid(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            outcome, _ = baseline_run(root, installed_child_test(TRACER_FAILURE))
            self.assertEqual(outcome["outcome"], "INFRA", outcome)
            self.assertEqual(
                outcome["message"],
                "Installed child trace is invalid: "
                "ValueError: lstat: embedded null character in path",
            )

    def test_the_first_tracer_failure_is_the_one_recorded(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            outcome, _ = baseline_run(root, installed_child_test(TRACER_FAILS_TWICE))
            self.assertEqual(outcome["outcome"], "INFRA", outcome)
            self.assertEqual(
                outcome["message"],
                "Installed child trace is invalid: "
                "ValueError: lstat: embedded null character in path",
            )


class CandidateEvidenceTest(unittest.TestCase):
    def test_a_collapsed_statement_does_not_credit_the_line_that_shifted_into_its_span(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            outcome, data = baseline_run(
                root, installed_child_test(COLLAPSED_STATEMENT_CHILD), COLLAPSIBLE_SOURCE
            )
            self.assertEqual(outcome["outcome"], "SURVIVED", outcome["log"])
            _, routes = child_trace.read_traces(data)
            mutants, _ = candidates("src/example.py", COLLAPSIBLE_SOURCE.encode())
            mutant = next(
                m for m in mutants if m.operator == "call:remove" and m.line == FIRST_CALL_LINE
            )
            self.assertEqual((mutant.end_line, mutant.after), (5, "pass"))
            result, _ = mutation_gate.mutate_and_measure(
                {"root": str(root), "timeout": 60},
                {"selected": {"unit": [NODE]}},
                mutant,
                tuple(routes),
            )
            self.assertEqual(result["outcome"], "NO_TESTS", result)

    def test_a_candidate_run_that_leaves_no_coverage_data_is_an_infra_record(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            outcome, data = baseline_run(root, INSTALLED_CHILD_TEST)
            self.assertEqual(outcome["outcome"], "SURVIVED", outcome["log"])
            _, routes = child_trace.read_traces(data)
            (root / "tests/conftest.py").write_text(
                "import os, signal\nos.kill(os.getpid(), signal.SIGKILL)\n"
            )
            result, _ = run_domain(
                root,
                "unit",
                [TEST_PATH],
                60,
                mutated_file=root / "src/example.py",
                mutated_span=(1, 1),
                expected_children=tuple(routes),
            )
            self.assertEqual(result["outcome"], "INFRA", result)
            self.assertEqual(
                result["message"], "Candidate run left no coverage data: No data to combine"
            )


def child_routes(root, test, source="value = 1\n"):
    outcome, data = baseline_run(root, test, source)
    if outcome["outcome"] != "SURVIVED":
        raise AssertionError(outcome["log"])
    return child_trace.read_traces(data)[1]


def candidate_run(root, routes, source="value = 2\n", span=(1, 1)):
    target = root / "src/example.py"
    target.write_text(source)
    return run_domain(
        root,
        "unit",
        [TEST_PATH],
        60,
        mutated_file=target,
        mutated_span=span,
        expected_children=tuple(routes),
    )[0]


class ImportKillAndSourceChecksTest(unittest.TestCase):
    def test_a_child_that_installs_other_bytes_than_the_mutation_is_infra(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            routes = child_routes(root, INSTALLED_CHILD_TEST)
            stale = INSTALLED_CHILD_TEST.replace(
                "package.write_bytes((root / 'src/example.py').read_bytes())",
                "package.write_bytes(b'value = 1\\n')",
            )
            (root / TEST_PATH).write_text(stale)
            result = candidate_run(root, routes)
        self.assertEqual(result["outcome"], "INFRA", result)
        self.assertEqual(result["message"], "Installed child source differs from the mutation")

    def test_a_route_naming_another_source_is_infra(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            routes = child_routes(root, INSTALLED_CHILD_TEST)
            other = [replace(route, source="src/other.py") for route in routes]
            result = candidate_run(root, other)
        self.assertEqual(result["outcome"], "INFRA", result)
        self.assertEqual(result["message"], "Expected installed child route names another source")

    def test_a_child_mutation_without_its_source_span_is_infra(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            routes = child_routes(root, INSTALLED_CHILD_TEST)
            (root / "src/example.py").write_text("value = 2\n")
            result, _ = run_domain(
                root,
                "unit",
                [TEST_PATH],
                60,
                mutated_file=root / "src/example.py",
                expected_children=tuple(routes),
            )
            with self.assertRaisesRegex(ValueError, "lacks its source span"):
                native_candidate_owners(root / ".coverage", root, root / "src/example.py", None)
            with self.assertRaisesRegex(ValueError, "lacks its source span"):
                native_candidate_owners(root / ".coverage", root, None, (1, 1))
        self.assertEqual(result["outcome"], "INFRA", result)
        self.assertEqual(result["message"], "Installed child mutation lacks its source span")

    def test_a_baseline_child_source_that_changed_since_the_run_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            routes = child_routes(root, INSTALLED_CHILD_TEST)
            (root / "src/example.py").write_text("value = 3\n")
            with self.assertRaisesRegex(
                ValueError, "^Installed child source differs from baseline: src/example.py$"
            ):
                validate_baseline_sources(root, routes)
            evidence = CoverageEvidence(root, {}, tuple(routes))
            with self.assertRaisesRegex(
                ValueError, "^Installed child source differs from baseline: src/example.py$"
            ):
                child_selection(evidence, "src/example.py", 1, 1)


EXPANDING_SOURCE = "def f(a):\n    return [\n        a == 1,\n    ]\nx = 1\n"
CALL_F = "import example; example.f(1); print(example.__file__)"
IMPORT_ONLY = "import example; print(example.__file__)"


def in_fixture(test):
    # The child then starts during pytest's setup phase, not in the test call.
    return (
        test.replace("def test_value():\n", "import pytest\n@pytest.fixture\ndef started():\n")
        + "def test_value(started):\n    assert True\n"
    )


class SpanAndPhaseAttributionTest(unittest.TestCase):
    def measure_expanded_mutant(self, root, routes, code):
        mutants, _ = candidates("src/example.py", EXPANDING_SOURCE.encode())
        mutant = next(
            m for m in mutants if m.operator == "comparison:0" and m.line == LIST_ELEMENT_LINE
        )
        # The mutant's own first line holds no instruction; its second line does the work.
        expanded = replace(mutant, after="(\n            a != 1)")
        (root / TEST_PATH).write_text(installed_child_test(code))
        result, _ = mutation_gate.mutate_and_measure(
            {"root": str(root), "timeout": 60},
            {"selected": {"unit": [NODE]}},
            expanded,
            tuple(routes),
        )
        return result["outcome"]

    def test_a_line_inside_an_expanded_mutant_is_credited_and_one_past_it_is_not(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            routes = child_routes(root, installed_child_test(IMPORT_ONLY), EXPANDING_SOURCE)
            reaching = self.measure_expanded_mutant(root, routes, CALL_F)
            past = self.measure_expanded_mutant(root, routes, IMPORT_ONLY)
        self.assertEqual((reaching, past), ("SURVIVED", "NO_TESTS"))

    def test_a_child_started_in_the_setup_phase_is_not_a_run_attempt(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            routes = child_routes(root, installed_child_test(IMPORT_ONLY))
            (root / TEST_PATH).write_text(in_fixture(installed_child_test(IMPORT_ONLY)))
            result = candidate_run(root, routes)
        self.assertEqual(result["outcome"], "NO_TESTS", result)


UNREACHED_CHILD = INSTALLED_CHILD_TEST.replace(
    "'import example; print(example.__file__)'",
    "('pass' if 'value = 2' in package.read_text() else 'import example; print(example.__file__)')",
).replace(
    "    imported = Path(child.stdout.strip()).resolve()\n"
    "    assert imported.is_relative_to(environment.resolve())\n",
    "    assert child.returncode == 0\n",
)
SKIPPING_AFTER_CHILD = (
    "import pytest\n"
    + INSTALLED_CHILD_TEST
    + "    pytest.skip('the call neither passed nor failed')\n"
)


class RecordAndPhaseEvidenceTest(unittest.TestCase):
    def test_an_unreached_record_that_carries_evidence_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            routes = child_routes(root, UNREACHED_CHILD)
            target = root / "src/example.py"
            target.write_text("value = 2\n")
            result, data = run_domain(
                root,
                "unit",
                [TEST_PATH],
                60,
                mutated_file=target,
                mutated_span=(1, 1),
                expected_children=tuple(routes),
            )
            self.assertEqual(result["outcome"], "NO_TESTS", result)
            _, traces = child_trace.read_traces(data)
            self.assertEqual([trace.outcome for trace in traces], ["unreached"])
            record = next(child_trace.trace_directory(data).glob("*.result.json"))
            original = json.loads(record.read_text())
            for key, value in (("source_digest", "a" * 64), ("lines", [1])):
                with self.subTest(key=key):
                    record.write_text(json.dumps({**original, key: value}))
                    with self.assertRaisesRegex(
                        ValueError,
                        "^Installed child trace belongs to another producer or route$",
                    ):
                        child_trace.read_traces(data)

    def test_a_setup_phase_baseline_child_is_not_a_candidate_for_selection(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            routes = child_routes(root, in_fixture(installed_child_test(IMPORT_ONLY)))
            self.assertEqual([trace.test_phase for trace in routes], ["setup"])
            evidence = CoverageEvidence(root, {}, tuple(routes))
            self.assertEqual(child_selection(evidence, "src/example.py", 1, 1), [])

    def test_a_setup_pass_is_not_a_call_pass(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            project(root, "value = 1\n")
            (root / TEST_PATH).write_text(
                "import pytest\ndef test_skipped():\n    pytest.skip('no call pass')\n"
            )
            only_skipped, _ = run_domain(root, "unit", [TEST_PATH], 60)
            routes = child_routes(root, INSTALLED_CHILD_TEST)
            (root / TEST_PATH).write_text(SKIPPING_AFTER_CHILD)
            (root / "tests/test_example.py").write_text("def test_other():\n    assert True\n")
            target = root / "src/example.py"
            target.write_text("value = 2\n")
            owner_skipped, _ = run_domain(
                root,
                "unit",
                [TEST_PATH, "tests/test_example.py"],
                60,
                mutated_file=target,
                mutated_span=(1, 1),
                expected_children=tuple(routes),
            )
        self.assertEqual(only_skipped["outcome"], "NO_TESTS", only_skipped)
        # test_other passed its call, but the owner reached the mutation and passed only setup.
        self.assertEqual(owner_skipped["outcome"], "NO_TESTS", owner_skipped)
