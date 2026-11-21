"""A traced installed child yields evidence only for the process the owning test started."""

import tempfile
import unittest
from pathlib import Path

from scripts.validation import mutation_child_trace as child_trace
from scripts.validation import mutation_gate
from scripts.validation.mutation_run import run_domain
from scripts.validation.mutation_source import candidates

from test_mutation_gate import INSTALLED_CHILD_TEST, project

TEST_PATH = "tests/test_installed_child.py"
NODE = f"{TEST_PATH}::test_value"
CHILD_COMMAND = "'import example; print(example.__file__)'"
FIRST_CALL_LINE = 3
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
                "Installed child trace is invalid: Tracer callback failed: "
                "lstat: embedded null character in path",
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
