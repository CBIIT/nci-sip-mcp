"""The mutation command measures assertion sensitivity without editing its caller."""

import ast
import contextlib
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from threading import Thread
from types import SimpleNamespace
from typing import ClassVar
from unittest.mock import patch

import yaml
from scripts.processes import stop, wait_until
from scripts.validation import mutation_gate
from scripts.validation import mutation_run as runner
from scripts.validation.mutation_gate import execute_job
from scripts.validation.mutation_run import coverage_map, program, run_domain, selection, snapshot
from scripts.validation.mutation_source import (
    allowlist,
    candidates,
    diff_lines,
    digest,
    fingerprint_dump,
    scope,
)

ROOT = Path(__file__).resolve().parents[1]
RECORDED_WAIT_SOURCE = """class StdioOwner:
    async def _wait(self, seconds: float, deadline: CleanupDeadline) -> bool:
        try:
            with anyio.fail_after(min(seconds, deadline.remaining())):
                code = await self._process().wait()
            if code != 0 and -code not in self._sent_signals:
                self._initiate(RequestPolicyError("transport"))
            return code == self._process().returncode
        except TimeoutError:
            return False
        except BaseException as error:
            self.errors.append(error)
            return False
"""
RECORDED_WAIT_KEY = {
    "file": "acceptance/src/nci_sip_acceptance/request_stdio.py",
    "qualname": "StdioOwner._wait",
    "operator": "return:None",
    "site": "/body/0/handlers/1/body/1",
    "context": "4d533410f44b8c205783b80e88589472e0c5775284193ff99180dbbca3c5a881",
    "original": "dd863ea504e8543d2fb185a820aa7e2cf2d0ceddec9c835185a3510fc2ab463d",
    "mutated": "e19ede9f5e840ec5e9c3ca6096c7341b0458e6c5c14c80709062e61900e94f51",
}
RECORDED_REMAINING_SOURCE = """class SupervisedRun:
    def _remaining(self, seconds: float) -> float:
        if not self._controlled_cleanup or self._deadline is None:
            return seconds
        return min(seconds, self._deadline.remaining())
"""
RECORDED_REMAINING_KEY = {
    "file": "acceptance/src/nci_sip_acceptance/request_supervision.py",
    "qualname": "SupervisedRun._remaining",
    "operator": "boolean:drop:1",
    "site": "/body/0/test/0",
    "context": "f36f6bf2841ccd71d34ad08175fe1d1069198f811ea06b7c05f0633bf4f5e518",
    "original": "ff105d5e1b393b7d932f3a338ac223fe44a1d825ca05e559507ad33386fa0a2d",
    "mutated": "ccddcadc719010718fc95f86d8289368500076b3b7547b038cc86a0b34743b7e",
}
FINGERPRINT_SOURCE = """def fingerprint(value: str = "ctx=Load()"):
    assigned = value
    return "ctx=Load()"
"""
FINGERPRINT_RETURN_KEY = {
    "file": "src/example.py",
    "qualname": "fingerprint",
    "operator": "return:None",
    "site": "/body/1",
    "context": "3163ffb7b54423f4c5c6140812861d6663e0f0a0d95931c4d0f6885902b63678",
    "original": "11c13472822fbfa58bea0087abb1f480200f45a41c94cedde76db259ca7c79e6",
    "mutated": "e19ede9f5e840ec5e9c3ca6096c7341b0458e6c5c14c80709062e61900e94f51",
}


def return_none_key(source: str) -> dict[str, str]:
    mutants, _ = candidates("src/example.py", source.encode())
    return next(
        mutant.key()
        for mutant in mutants
        if mutant.operator == "return:None" and mutant.site == "/body/1"
    )


def project(root, source, unit="assert example.answer() is True", contract="assert 1 == 1"):
    files = {
        "src/example.py": source,
        "tests/test_example.py": f"import example\ndef test_value():\n    {unit}\n",
        "acceptance/selftests/test_harness.py": "def test_harness():\n    assert 2 == 2\n",
        "acceptance/tests/test_contract.py": (
            f"import example\ndef test_contract():\n    {contract}\n"
        ),
        "acceptance/conftest.py": (
            "def pytest_addoption(parser):\n    parser.addoption('--report')\n"
        ),
        "pyproject.toml": '[tool.pytest.ini_options]\npythonpath = ["src"]\n',
    }
    for name, value in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value)


def command(root, *args):
    result = cli(root, *args)
    return result.returncode, [json.loads(line) for line in result.stdout.splitlines()]


def mutant_rows(rows):
    return [row for row in rows if "site" in row]


def isolated_shard(mutants, target):
    for count in range(len(mutants) + 1, len(mutants) + 100):
        index = int(digest(json.dumps(target.key(), sort_keys=True)), 16) % count + 1
        if mutation_gate.partition(mutants, (index, count)) == [target]:
            return f"{index}/{count}"
    raise AssertionError("Could not isolate the intended mutation shard")


def domain_routes(rows):
    return [[run["domain"] for run in row["domains"]] for row in rows]


def json_lines(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def cli(root, *args):
    return subprocess.run(  # noqa: S603 - fixed PDM command, owned paths
        ["pdm", "run", "mutation-gate", "--root", str(root), *args],  # noqa: S607
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )


class MutationOperatorsTest(unittest.TestCase):
    def test_recorded_314_allowlist_key_is_portable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            source = root / RECORDED_WAIT_KEY["file"]
            source.parent.mkdir(parents=True)
            source.write_text(RECORDED_WAIT_SOURCE, encoding="utf-8")
            path = root / "allowlist.yaml"
            path.write_text(yaml.safe_dump([{**RECORDED_WAIT_KEY, "reason": "Recorded 3.14 key."}]))

            self.assertEqual(
                allowlist(root, path),
                [{**RECORDED_WAIT_KEY, "reason": "Recorded 3.14 key."}],
            )

    def test_recorded_load_bearing_boolean_key_passes_allowlist_validation(self):
        entry = {
            **RECORDED_REMAINING_KEY,
            "reason": "Recorded 3.14 key with loads in the original and replacement.",
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            source = root / entry["file"]
            source.parent.mkdir(parents=True)
            source.write_text(RECORDED_REMAINING_SOURCE, encoding="utf-8")
            path = root / "scripts/validation/mutation-allowlist.yaml"
            path.parent.mkdir(parents=True)
            path.write_text(yaml.safe_dump([entry]), encoding="utf-8")

            self.assertEqual(allowlist(root, path), [entry])

    def test_fingerprint_preserves_314_defaults_contexts_and_literal_text(self):
        key = return_none_key(FINGERPRINT_SOURCE)
        self.assertEqual(key, FINGERPRINT_RETURN_KEY)
        changed_name = return_none_key(
            FINGERPRINT_SOURCE.replace("assigned = value", "assigned = other")
        )
        changed_literal = return_none_key(
            FINGERPRINT_SOURCE.replace('return "ctx=Load()"', 'return "ctx=Store()"')
        )
        self.assertNotEqual(changed_name["context"], key["context"])
        self.assertNotEqual(changed_literal["context"], key["context"])
        self.assertNotEqual(changed_literal["original"], key["original"])

    def test_fingerprint_drops_only_empty_lists_the_node_type_declares(self):
        call = ast.parse("f(x)", mode="eval").body
        no_arguments = ast.parse("f()", mode="eval").body
        self.assertIn("args=[Name(id='x', ctx=Load())]", fingerprint_dump(call))
        self.assertNotIn("args=", fingerprint_dump(no_arguments))

        class Untyped(ast.AST):
            _fields = ("a",)

        class Typed(ast.AST):
            _fields = ("a",)
            _field_types: ClassVar[dict[str, object]] = {"a": list[int]}

        class Malformed(ast.AST):
            _fields = ("a",)

        class Scalar(ast.AST):
            _fields = ("a",)
            _field_types: ClassVar[dict[str, object]] = {"a": int}

        untyped, typed, falsy, malformed = Untyped(), Typed(), Typed(), Malformed()
        scalar = Scalar(a=[])
        Malformed._field_types = None  # ast.AST.__init__ rejects a non-dict, so set it afterwards
        untyped.a = []
        typed.a = []
        falsy.a = ()
        malformed.a = []
        self.assertEqual(fingerprint_dump(untyped), "Untyped(a=[])")
        self.assertEqual(fingerprint_dump(typed), "Typed()")
        self.assertEqual(fingerprint_dump(falsy), "Typed(a=())")
        self.assertEqual(fingerprint_dump(malformed), "Malformed(a=[])")
        self.assertEqual(fingerprint_dump(scalar), "Scalar(a=[])")

    def test_recorded_allowlist_rejects_changed_source_site_and_operator(self):
        entry = {**RECORDED_WAIT_KEY, "reason": "Recorded 3.14 key."}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            source = root / entry["file"]
            source.parent.mkdir(parents=True)
            source.write_text(RECORDED_WAIT_SOURCE, encoding="utf-8")
            path = root / "allowlist.yaml"
            path.write_text(yaml.safe_dump([entry]))
            self.assertEqual(allowlist(root, path), [entry])

            source.write_text(RECORDED_WAIT_SOURCE.replace("code != 0", "code == 0"))
            with self.assertRaisesRegex(ValueError, "Stale, duplicate or ambiguous"):
                allowlist(root, path)
            source.write_text(RECORDED_WAIT_SOURCE, encoding="utf-8")

            for field, value in (
                ("site", "/body/0/handlers/1/body/0"),
                ("operator", "return:True"),
            ):
                with self.subTest(field=field):
                    path.write_text(yaml.safe_dump([{**entry, field: value}]))
                    with self.assertRaisesRegex(ValueError, "Stale, duplicate or ambiguous"):
                        allowlist(root, path)

    def test_fixed_function_context_hash_matches_the_ci_interpreter_contract(self):
        mutants, _ = candidates("src/example.py", b"def answer():\n    return True\n")
        self.assertEqual(
            {mutant.context for mutant in mutants},
            {"a0b3ebdc264a10cfed0053301d6d73c06f2e16170d397107ffbc7ba0a3a6be81"},
        )

    def test_multiline_expression_sites_compile_and_module_keys_survive_statement_line_shifts(self):
        source = b"flag = (True\n        and False)\n"
        mutants, _ = candidates("src/example.py", source)
        shifted, _ = candidates("src/example.py", b"# moved\n\n" + source)
        self.assertEqual([item.key() for item in mutants], [item.key() for item in shifted])
        self.assertEqual(
            {item.operator for item in mutants},
            {"boolean:literal", "boolean:swap", "boolean:drop:0", "boolean:drop:1"},
        )
        for item in mutants:
            changed = source[: item.start] + item.after.encode() + source[item.end :]
            self.assertIsNotNone(compile(changed, item.file, "exec"))

    def test_comparison_boolean_and_file_mode_variants_compile_without_touching_other_bytes(self):
        expressions = (
            "a == b",
            "a != b",
            "a < b",
            "a <= b",
            "a > b",
            "a >= b",
            "a is b",
            "a is not b",
            "a in b",
            "a not in b",
            "a < b < c",
            "a or b or c",
            "a and b and c",
            "not a",
            "False",
            "a < 2",
            "open('name', 'rb')",
            "settings.get('name')",
            "{'key': 1}",
        )
        for expression in expressions:
            with self.subTest(expression=expression):
                source = (
                    f"# é\ndef outer():\n    def inner(a, b, c):\n        return {expression}\n"
                )
                mutants, diagnostics = candidates("src/example.py", source.encode())
                self.assertTrue(mutants)
                self.assertLessEqual({row["outcome"] for row in diagnostics}, {"NO_OP"})
                for mutant in mutants:
                    changed = source.encode()[: mutant.start] + mutant.after.encode()
                    changed += source.encode()[mutant.end :]
                    compile(changed, "example.py", "exec")
                    self.assertEqual(mutant.qualname, "outer.inner")
                    self.assertTrue(changed.startswith(b"# \xc3\xa9\ndef outer():"))

    def test_ci_runs_pr_mutation_on_every_pull_request_to_main(self):
        workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
        self.assertEqual(
            workflow["jobs"]["mutation"],
            {
                "name": "mutation (diff)",
                "if": "github.event_name == 'pull_request' && github.base_ref == 'main'",
                "permissions": {"contents": "read"},
                "runs-on": "ubuntu-24.04",
                "timeout-minutes": 300,
                "strategy": {"fail-fast": False, "matrix": {"shard": [1, 2, 3]}},
                "needs": ["test", "unit-coverage", "selftest", "coverage", "acceptance"],
                "steps": [
                    {
                        "uses": "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1",
                        "with": {
                            "fetch-depth": 0,
                            "persist-credentials": False,
                        },
                    },
                    {"uses": "$/.github/actions/setup"},
                    {
                        "uses": (
                            "actions/download-artifact@9000827ccba6bdab643e8b6fd33ac0654aef8333"
                        ),
                        "with": {
                            "pattern": "mutation-coverage-${{ github.sha }}-*",
                            "path": "tmp/mutation-coverage",
                        },
                    },
                    {
                        "name": "Mutate every changed Python site",
                        "env": {
                            "BASE_SHA": "${{ github.event.pull_request.base.sha }}",
                            "HEAD_SHA": "${{ github.sha }}",
                            "SHARD": "${{ matrix.shard }}/3",
                        },
                        "run": (
                            'pdm run mutation-gate --base "$BASE_SHA" --head "$HEAD_SHA" '
                            '--shard "$SHARD" -n 2 --coverage-dir tmp/mutation-coverage'
                        ),
                    },
                    {
                        "name": "Retain raw mutation results",
                        "if": "always()",
                        "uses": (
                            "actions/upload-artifact@cf430e030ddbb5b0abf93d22962f4752f3646cd9"
                        ),
                        "with": {
                            "name": "mutation-results-${{ github.sha }}-${{ matrix.shard }}",
                            "path": "tmp/mutation-runs/",
                            "if-no-files-found": "warn",
                            "retention-days": 30,
                        },
                    },
                ],
            },
        )

    def test_fixed_operators_change_only_valid_utf8_source_and_are_killed_by_real_assertions(self):
        source = (
            "events = []\n"
            "def answer(a=2, b=3):\n"
            "    café = 'é'; flag = True\n"
            "    events.append(a)\n"
            "    if a < b and not a == 0:\n"
            "        return {'key': a if flag else 99}.get('key')\n"
            "    raise ValueError('bad')\n"
        )
        test = (
            "import pytest\n"
            "    try:\n"
            "        value = example.answer()\n"
            "        other = example.answer(1)\n"
            "    except (ValueError, KeyError) as error:\n"
            "        pytest.fail(str(error))\n"
            "    assert value == 2\n"
            "    assert other == 1\n"
            "    assert example.events == [2, 1]\n"
            "    with pytest.raises(ValueError):\n"
            "        example.answer(0)\n"
            "    with pytest.raises(ValueError):\n"
            "        example.answer(3, 2)\n"
            "    example.answer(2); example.answer(2)\n"
            "    assert example.answer(2) == 2"
        )
        mutants, rejected = candidates("src/example.py", source.encode())
        self.assertEqual({row["outcome"] for row in rejected}, set())
        self.assertEqual(
            {mutant.operator.split(":")[0] for mutant in mutants},
            {"comparison", "boolean", "not", "return", "raise", "integer", "call", "string"},
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            project(root, source, test)
            status, rows = command(root, "--paths", "src/example.py", "-n", "2", "--seed", "19")
            self.assertEqual(status, 0, rows)
            self.assertEqual(len(rows), len(mutants))
            self.assertEqual({row["outcome"] for row in rows}, {"KILLED"})
            self.assertEqual({row["domain"] for row in rows}, {"unit"})
            self.assertEqual({row["phase"] for row in rows}, {"call"})
            self.assertEqual((root / "src/example.py").read_bytes(), source.encode())

    def test_comment_shifts_keep_keys_but_function_changes_invalidate_full_source_exemptions(self):
        source = "def answer():\n    return True\n"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            project(root, source)
            mutants, _ = candidates("src/example.py", source.encode())
            entry = {**mutants[-1].key(), "reason": "Equivalent for the documented caller."}
            path = root / "allow.yaml"
            path.write_text(yaml.safe_dump([entry]))
            (root / "src/example.py").write_text("# moved\n\n" + source)
            self.assertEqual(allowlist(root, path), [entry])
            (root / "src/example.py").write_text(source.replace("return", "unused = 2\n    return"))
            with self.assertRaisesRegex(ValueError, "Stale"):
                allowlist(root, path)
            status, rows = command(
                root, "--paths", "scripts/missing.py", "--allowlist", "allow.yaml"
            )
            self.assertEqual(status, 2)
            self.assertIn("Stale", rows[-1]["message"])

    def test_allowlist_rejects_duplicate_ambiguous_malformed_entries(self):
        source = "def answer():\n    return True\n"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            project(root, source)
            mutants, _ = candidates("src/example.py", source.encode())
            entry = {**mutants[-1].key(), "reason": "Reviewed equivalent."}
            path = root / "allow.yaml"
            for entries in ([entry, entry], [{**entry, "extra": "bad"}], [{**entry, "reason": ""}]):
                with self.subTest(entries=entries):
                    path.write_text(yaml.safe_dump(entries))
                    with self.assertRaises(ValueError):
                        allowlist(root, path)
            (root / "src/example.py").write_text(source + source)
            path.write_text(yaml.safe_dump([entry]))
            with self.assertRaisesRegex(ValueError, "ambiguous"):
                allowlist(root, path)

    def test_diff_scopes_exact_head_lines_and_explicit_paths_cover_whole_modules(self):
        diff = "+++ b/src/example.py\n@@ -2 +2 @@\n-x\n+y\n+++ b/tests/test_x.py\n@@ -1 +1 @@\n"
        self.assertEqual(diff_lines(diff), {"src/example.py": {2}})
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            project(root, "def answer():\n    return True\n")
            self.assertEqual(
                scope(root, ["src/example.py"], "unused", "unused"), {"src/example.py": None}
            )
            with self.assertRaises(ValueError):
                scope(root, ["tests/test_example.py"], "unused", "unused")

    def test_contexts_are_domain_qualified_and_only_unattributed_lines_use_whole_domain(self):
        mapping = {
            "unit": {"src/a.py": {2: ["tests/test_a.py::test_a|run"]}},
            "selftest": {"src/a.py": {2: ["tests/test_a.py::test_a|setup", ""]}},
        }
        self.assertEqual(
            selection(mapping, "src/a.py", 2, 2),
            {"unit": ["tests/test_a.py::test_a"], "selftest": []},
        )
        self.assertEqual(selection(mapping, "src/a.py", 9, 9), {})
        for contexts in (
            ["", "tests/test_a.py::test_a|run"],
            ["tests/test_a.py::test_a|setup", "tests/test_a.py::test_a|run"],
            ["tests/test_a.py::test_a|teardown", "tests/test_a.py::test_a|run"],
        ):
            with self.subTest(contexts=contexts):
                self.assertEqual(
                    selection({"unit": {"src/a.py": {2: contexts}}}, "src/a.py", 2, 2),
                    {"unit": ["tests/test_a.py::test_a"]},
                )
        for context in (
            "unknown",
            "tests/x.py|run",
            "tests/x.py::test_x|unknown",
            "tests/x.py::test_x",
            "tests/x.py::test_x|",
        ):
            for contexts in ([context], ["tests/test_a.py::test_a|run", context]):
                with self.subTest(contexts=contexts), self.assertRaises(ValueError) as failed:
                    selection({"unit": {"src/a.py": {2: contexts}}}, "src/a.py", 2, 2)
                self.assertEqual(
                    str(failed.exception), f"Unrecognized coverage context: {context!r}"
                )


class MutationExecutionTest(unittest.TestCase):
    def test_aggregate_cost_overflow_keeps_complete_summary_and_candidate_bytes(self):
        # Given valid individual timings, an unreportable aggregate must fail
        # closed after all candidate identities and summary counts are emitted.
        for durations, repeated in (
            ((10**308, 10**308, 1.0), False),
            ((1e308, 1e308, 1.0), False),
            ((1e308, 0, 0), True),
        ):
            for cancelled in (False, True):
                with (
                    self.subTest(durations=durations, cancelled=cancelled),
                    tempfile.TemporaryDirectory() as temporary,
                ):
                    self.assert_aggregate_cost_report(
                        Path(temporary), durations, repeated, cancelled
                    )

    def assert_aggregate_cost_report(self, root, durations, repeated, cancelled):
        project(root, "def answer():\n    return True\n")
        retained = {}
        work = []
        later_durations = durations if repeated else (0, 0, 0)

        def scheduler(root, directory, data, workers):
            work.append(directory)
            for index, item in enumerate(data["mutants"]):
                row = {key: value for key, value in item.items() if key != "selected"}
                row["before"] = data["sources"][item["file"]][item["start"] : item["end"]]
                row.update(
                    outcome="KILLED",
                    restored=True,
                    kind="assertion",
                    fallback_domains=["unit"],
                    domains=[
                        {
                            "domain": domain,
                            "elapsed_seconds": duration,
                        }
                        for domain, duration in zip(
                            mutation_gate.DOMAINS,
                            later_durations if index else durations,
                            strict=True,
                        )
                    ],
                )
                retained[index] = (json.dumps(row) + "\n").encode()
                (directory / f"result-{index}.json").write_bytes(retained[index])
            if cancelled:
                raise KeyboardInterrupt("original scheduler cancellation")
            return 0

        output = io.StringIO()
        with (
            patch.object(mutation_gate, "imported_coverage", return_value={}),
            patch.object(mutation_gate, "run_scheduler", side_effect=scheduler),
            contextlib.redirect_stdout(output),
        ):
            status = mutation_gate.main(
                ["--root", str(root), "--paths", "src/example.py", "--shard", "1/1"]
            )
        self.assertEqual(status, 2, output.getvalue())
        rows = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual([row["outcome"] for row in mutant_rows(rows)], ["KILLED"] * 3)
        summary = next(row for row in rows if row["outcome"] == "SUMMARY")
        self.assertEqual(summary["counts"], {"KILLED": 3})
        self.assertEqual(summary["kill_kinds"], {"assertion": 3, "error": 0, "import": 0})
        self.assertEqual(summary["fallback_count"], 3)
        self.assertEqual(summary["fallback_domains"], {"unit": 3})
        self.assertEqual(summary["baseline_domains"], [])
        self.assertEqual(summary["shard_mutants"], 3)
        self.assertEqual(summary["full_mutants"], 3)
        self.assertEqual(summary["cost_error"], "Aggregate runner seconds exceeds reporting range")
        self.assertTrue(
            {"runner_seconds", "runner_hours", "runner_seconds_by_domain"}.isdisjoint(summary)
        )
        self.assertEqual(rows[-1]["outcome"], "INFRA")
        self.assertIn(
            "original scheduler cancellation" if cancelled else summary["cost_error"],
            rows[-1]["message"],
        )
        self.assert_retained_cost_evidence(work[0], retained)

    def assert_retained_cost_evidence(self, work, retained):
        for index, original in retained.items():
            self.assertEqual((work / f"result-{index}.json").read_bytes(), original)
        self.assertEqual(list(work.glob("result-*.infra.json")), [])

    def test_worker_publication_cancellation_keeps_previous_complete_result(self):
        # Given an existing result, cancellation at atomic publication must leave
        # its bytes intact; a subsequent successful publication is complete JSON.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            project(root, "def answer():\n    return True\n")
            mutants, _ = candidates("src/example.py", (root / "src/example.py").read_bytes())
            manifest = root / "mutations.json"
            manifest.write_text(json.dumps(mutation_gate.manifest_data(root, mutants[:1], {}, 20)))
            result = root / "result-0.json"
            original = b'{"previous": true}\n'
            result.write_bytes(original)

            def cancel_publication(source, destination):
                self.assertEqual(destination, result)
                self.assertEqual(result.read_bytes(), original)
                self.assertEqual(json.loads(source.read_text())["outcome"], "NO_TESTS")
                raise KeyboardInterrupt("cancelled before result publication")

            with (
                patch.object(Path, "replace", autospec=True, side_effect=cancel_publication),
                self.assertRaisesRegex(KeyboardInterrupt, "cancelled before result publication"),
            ):
                execute_job(manifest, 0)
            self.assertEqual(result.read_bytes(), original)
            execute_job(manifest, 0)
            self.assertEqual(json.loads(result.read_text())["outcome"], "NO_TESTS")

    def test_corrupt_result_retains_bytes_and_reports_every_candidate_on_cancel_or_success(self):
        # Given completed, invalid and missing results, neither cancellation nor
        # a zero scheduler exit may hide later candidates or overwrite evidence.
        for cancelled, defect in (
            (cancelled, defect)
            for cancelled in (True, False)
            for defect in self.invalid_candidate_records()
        ):
            with (
                self.subTest(cancelled=cancelled, defect=defect),
                tempfile.TemporaryDirectory() as temporary,
            ):
                self.assert_corrupt_scheduler_results(Path(temporary).resolve(), cancelled, defect)

    def test_supplied_invalid_metrics_fail_infra_without_false_pass_or_summary_abort(self):
        # Given matching completed identities with malformed optional metrics, the
        # CLI must retain corrupt bytes and report all candidates, including a shard summary.
        for sharded in (False, True):
            for defect, extra in self.invalid_candidate_metrics().items():
                with (
                    self.subTest(sharded=sharded, defect=defect),
                    tempfile.TemporaryDirectory() as t,
                ):
                    self.assert_corrupt_scheduler_results(
                        Path(t).resolve(), False, defect, sharded=sharded, extra=extra
                    )

    @staticmethod
    def invalid_candidate_metrics():
        timing = {"domain": "unit", "outcome": "KILLED", "elapsed_seconds": 0.25}
        records = {
            f"domains-{value!r}": {"domains": value}
            for value in (None, {}, "unit", [None], [[]], [{}])
        }
        records.update(
            (f"timing-{key}-{value!r}", {"domains": [{**timing, key: value}]})
            for key, values in (
                ("domain", (None, [], "unknown")),
                ("outcome", (None, [], "ALLOWLISTED", "unknown")),
                (
                    "elapsed_seconds",
                    (None, "1", True, False, -1, float("nan"), float("inf"), 10**400),
                ),
            )
            for value in values
        )
        records.update(
            (f"missing-timing-{key}", {"domains": [{k: v for k, v in timing.items() if k != key}]})
            for key in ("domain", "elapsed_seconds")
        )
        records.update(MutationExecutionTest.invalid_optional_metrics())
        return records

    @staticmethod
    def invalid_optional_metrics():
        return {
            f"{key}-{value!r}": {key: value}
            for key, values in (
                (
                    "fallback_domains",
                    (None, {}, "unit", [None], [[]], ["unknown"], ["unit", "unit"]),
                ),
                (
                    "elapsed_seconds",
                    (None, "1", True, False, -1, float("nan"), float("inf"), 10**400),
                ),
                ("kind", (None, [], {}, "unknown")),
                ("domain", (None, [], "unknown")),
            )
            for value in values
        }

    def test_minimal_and_supplied_valid_metrics_still_report_successful_shards(self):
        # Optional evidence may be omitted; supplied domain outcomes are optional
        # too, but valid durations and fallback names must reach the summary intact.
        for extra in (
            {},
            {"domains": [], "fallback_domains": [], "elapsed_seconds": 0},
            {
                "domains": [
                    {"domain": "unit", "elapsed_seconds": 0},
                    {"domain": "selftest", "outcome": "SURVIVED", "elapsed_seconds": 0.5},
                    {"domain": "fixture", "outcome": "KILLED", "elapsed_seconds": 1},
                ],
                "fallback_domains": ["unit", "selftest", "fixture"],
                "elapsed_seconds": 2.5,
                "kind": "assertion",
                "domain": "fixture",
            },
        ):
            with self.subTest(extra=extra), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary).resolve()
                project(root, "def answer():\n    return True\n")

                def scheduler(root, directory, data, workers, extra=extra):
                    for index, item in enumerate(data["mutants"]):
                        row = {key: value for key, value in item.items() if key != "selected"}
                        row["before"] = data["sources"][item["file"]][item["start"] : item["end"]]
                        row.update(outcome="KILLED", restored=True, **extra)
                        (directory / f"result-{index}.json").write_text(json.dumps(row))
                    return 0

                output = io.StringIO()
                with (
                    patch.object(mutation_gate, "imported_coverage", return_value={}),
                    patch.object(mutation_gate, "run_scheduler", side_effect=scheduler),
                    contextlib.redirect_stdout(output),
                ):
                    status = mutation_gate.main(
                        ["--root", str(root), "--paths", "src/example.py", "--shard", "1/1"]
                    )
                self.assertEqual(status, 0, output.getvalue())
                rows = [json.loads(line) for line in output.getvalue().splitlines()]
                self.assertEqual([row["outcome"] for row in mutant_rows(rows)], ["KILLED"] * 3)
                self.assertEqual(rows[-1]["outcome"], "SUMMARY")
                self.assertEqual(rows[-1]["runner_seconds"], 4.5 if "kind" in extra else 0)
                self.assertEqual(rows[-1]["fallback_count"], 3 if "kind" in extra else 0)

    @staticmethod
    def invalid_candidate_records():
        mutants, _ = candidates("src/example.py", b"def answer():\n    return True\n")
        mutants.sort(key=lambda mutant: (mutant.file, mutant.line, mutant.operator, mutant.site))
        valid = {**asdict(mutants[1]), "outcome": "KILLED", "restored": True}
        records = {
            "syntax": b'{"outcome": "KILLED",',
            "encoding": b'\xff{"outcome": "KILLED"}',
            "null": b"null",
            "list": b"[]",
            "empty": b"{}",
            "foreign": json.dumps({**valid, **asdict(mutants[0])}).encode(),
        }
        records.update(
            (f"missing-{key}", json.dumps({k: v for k, v in valid.items() if k != key}).encode())
            for key in valid
        )
        records.update(
            (f"invalid-{key}-{value!r}", json.dumps({**valid, key: value}).encode())
            for key, values in (
                ("outcome", (None, [], "ALLOWLISTED", "unknown")),
                ("restored", (None, "true", 1, [])),
            )
            for value in values
        )
        return records

    def assert_corrupt_scheduler_results(
        self, root, cancelled, defect, *, sharded=False, extra=None
    ):
        project(root, "def answer():\n    return True\n")
        mutants, _ = candidates("src/example.py", (root / "src/example.py").read_bytes())
        mutants.sort(key=lambda mutant: (mutant.file, mutant.line, mutant.operator, mutant.site))
        completed = (
            json.dumps(
                {
                    **asdict(mutants[0]),
                    "outcome": "KILLED",
                    "restored": True,
                    "domains": [],
                    "fallback_domains": [],
                }
            ).encode()
            + b"\n"
        )
        corrupt = self.corrupt_candidate_bytes(completed, mutants[1], defect, extra)
        work = []

        def scheduler(root, directory, data, workers):
            work.append(directory)
            (directory / "result-0.json").write_bytes(completed)
            (directory / "result-1.json").write_bytes(corrupt)
            if cancelled:
                raise KeyboardInterrupt("original scheduler cancellation")
            (directory / "result-2.json").write_text(
                json.dumps({**json.loads(completed), **asdict(mutants[2])})
            )
            return 0

        output = io.StringIO()
        with (
            patch.object(mutation_gate, "collect", return_value=(mutants, [])),
            patch.object(mutation_gate, "imported_coverage", return_value={}),
            patch.object(mutation_gate.random.Random, "shuffle"),
            patch.object(mutation_gate, "run_scheduler", side_effect=scheduler),
            contextlib.redirect_stdout(output),
        ):
            arguments = ["--root", str(root), "--paths", "src/example.py"]
            status = mutation_gate.main(arguments + ["--shard", "1/1"] * sharded)
        self.assertEqual(status, 2)
        rows = [json.loads(line) for line in output.getvalue().splitlines()]
        results = mutant_rows(rows)
        self.assertEqual(len(results), len(mutants))
        self.assertEqual(
            [{name: row[name] for name in mutants[0].key()} for row in results],
            [mutant.key() for mutant in mutants],
        )
        self.assertEqual(
            [row["outcome"] for row in results],
            ["KILLED", "INFRA", "INFRA" if cancelled else "KILLED"],
        )
        self.assertIn("candidate", results[1]["message"])
        self.assertIs(results[1]["restored"], False)
        self.assertEqual(results[1]["domains"], [])
        self.assertEqual(results[1]["fallback_domains"], [])
        self.assertEqual(rows[-1]["outcome"], "INFRA")
        self.assert_corrupt_summary(rows, sharded)
        self.assertEqual((work[0] / "result-0.json").read_bytes(), completed)
        self.assertEqual((work[0] / "result-1.json").read_bytes(), corrupt)
        self.assertEqual(json.loads((work[0] / "result-1.infra.json").read_text()), results[1])
        self.assertEqual((work[0] / "result-2.json").read_text(), json.dumps(results[2]))
        self.assert_corrupt_scheduler_diagnostic(work[0], results, cancelled)

    @staticmethod
    def corrupt_candidate_bytes(completed, mutant, defect, extra):
        if extra is None:
            return MutationExecutionTest.invalid_candidate_records()[defect]
        return json.dumps({**json.loads(completed), **asdict(mutant), **extra}).encode()

    def assert_corrupt_summary(self, rows, sharded):
        if sharded:
            self.assertEqual(rows[-2]["outcome"], "SUMMARY")
            self.assertEqual(rows[-2]["counts"], {"KILLED": 2, "INFRA": 1})

    def assert_corrupt_scheduler_diagnostic(self, work, results, cancelled):
        diagnostic = (work / "traceback.txt").read_text()
        if cancelled:
            self.assertIn("no result", results[2]["message"])
            self.assertIn("KeyboardInterrupt: original scheduler cancellation", diagnostic)
            self.assertIn('raise KeyboardInterrupt("original scheduler cancellation")', diagnostic)

    def test_declared_scheduler_call_allows_real_nested_worker_cleanup(self):
        # The production scheduler call, not its name, must enable the longer
        # grace needed for a worker's separate-session TERM-ignoring runner.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            ready, cleaned = root / "ready", root / "cleaned"
            nested = (
                "import os,signal; from pathlib import Path; "
                "signal.signal(signal.SIGTERM,signal.SIG_IGN); "
                f"Path({str(ready)!r}).write_text(str(os.getpid())); signal.pause()"
            )
            worker = (
                "import os,sys; from pathlib import Path\n"
                "from scripts.validation import mutation_run as r\n"
                "r.STOP_SECONDS=0.1; r.WORKER_CLEANUP_SECONDS=0.5; r.OWNED_CLEANUP_SECONDS=0.7\n"
                "try:\n"
                f"    r.program([sys.executable,'-c',{nested!r}],Path({str(root)!r}),"
                f"dict(os.environ),10,Path({str(root / 'nested.log')!r}))\n"
                "finally:\n"
                f"    Path({str(cleaned)!r}).touch()\n"
            )
            wait_exit = runner._wait_exit

            def cancel_ready(pid, deadline, cancel=None):
                if not ready.exists():
                    self.assertTrue(wait_until(ready.exists, 5, interval=0.01))
                    return None
                return wait_exit(pid, deadline, cancel)

            def launch_worker(command, cwd, env, timeout, log, **kwargs):
                return program([sys.executable, "-c", worker], cwd, env, timeout, log, **kwargs)

            try:
                with (
                    patch.object(mutation_gate, "program", side_effect=launch_worker),
                    patch.object(runner, "STOP_SECONDS", 0.1),
                    patch.object(runner, "WORKER_CLEANUP_SECONDS", 0.5),
                    patch.object(runner, "OWNED_CLEANUP_SECONDS", 0.7),
                    patch.object(runner, "_wait_exit", side_effect=cancel_ready),
                ):
                    status = mutation_gate.run_scheduler(
                        ROOT, root, {"timeout": 10, "mutants": []}, 1
                    )
                self.assertIsNone(status)
                self.assertTrue(cleaned.exists(), "declared scheduler call cut off worker cleanup")
                self.assertFalse(self.process_exists(int(ready.read_text())))
            finally:
                self.kill_marked_group(ready)

    def test_scheduler_owns_unique_retained_directories_and_announces_before_launch(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            project(root, "def answer():\n    return True\n")
            mutants, _ = candidates("src/example.py", (root / "src/example.py").read_bytes())
            args = SimpleNamespace(timeout=20, workers=1)
            output = io.StringIO()
            directories = []

            def scheduler(command, cwd, env, timeout, log, *, worker_cleanup):
                announcement = json.loads(output.getvalue().splitlines()[-1])
                self.assertEqual(announcement, {"outcome": "EVIDENCE", "directory": str(cwd)})
                self.assertTrue(cwd.is_dir())
                directories.append(cwd)
                execute_job(Path(command[-1]), 0)
                return 0

            with (
                contextlib.redirect_stderr(output),
                patch.object(mutation_gate, "program", side_effect=scheduler),
            ):
                first = mutation_gate.schedule(root, mutants[:1], {}, args)
                second = mutation_gate.schedule(root, mutants[:1], {}, args)
                third = mutation_gate.schedule(root, mutants[:1], {}, args)
            self.assertEqual(
                [directory.parent for directory in directories],
                [root / "tmp/mutation-runs"] * 3,
            )
            self.assertEqual(len(set(directories)), 3)
            self.assertTrue(all(directory.name.startswith("run-") for directory in directories))
            self.assertEqual([row["outcome"] for row in first + second + third], ["NO_TESTS"] * 3)
            for directory in directories:
                self.assertTrue((directory / "mutations.json").is_file())
                self.assertTrue((directory / "result-0.json").is_file())
                self.assertTrue((directory / "scheduler.log").is_file())

    def test_scheduler_retains_original_diagnostics_even_when_every_candidate_finished(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            project(root, "def answer():\n    return True\n")
            mutants, _ = candidates("src/example.py", (root / "src/example.py").read_bytes())
            for error in (
                KeyboardInterrupt("cancelled"),
                PermissionError("cleanup denied"),
                BaseExceptionGroup(
                    "Runner and owned cleanup failed",
                    [KeyboardInterrupt("cancelled"), PermissionError("cleanup denied")],
                ),
            ):
                with self.subTest(error=type(error).__name__):
                    work = []

                    def scheduler(
                        command, cwd, env, timeout, log, work=work, error=error, *, worker_cleanup
                    ):
                        work.append(cwd)
                        execute_job(Path(command[-1]), 0)
                        raise error

                    with (
                        patch.object(mutation_gate, "program", side_effect=scheduler),
                        self.assertRaises(mutation_gate.SchedulerError) as failed,
                    ):
                        mutation_gate.schedule(
                            root, mutants[:1], {}, SimpleNamespace(timeout=20, workers=1)
                        )
                    self.assertIs(failed.exception.__cause__, error)
                    self.assertEqual(failed.exception.rows[0]["outcome"], "NO_TESTS")
                    diagnostic = (work[0] / "traceback.txt").read_text()
                    self.assertIn(f"{type(error).__name__}: {error}", diagnostic)
                    self.assertIn("raise error", diagnostic)

    def test_result_permission_failure_is_not_reported_as_a_missing_candidate(self):
        mutants, _ = candidates("src/example.py", b"def answer():\n    return True\n")
        denied = PermissionError("result read denied")
        with (
            patch.object(Path, "read_text", side_effect=denied),
            self.assertRaises(PermissionError) as failed,
        ):
            mutation_gate.scheduler_rows(Path("unused"), mutants[:1], {}, "diagnostic")
        self.assertIs(failed.exception, denied)

    def test_real_scheduler_cancellation_retains_completed_bytes_and_all_candidate_identities(self):
        # Given one actual completed candidate and a ready separate-session runner,
        # cancellation retains that evidence and explicitly reports every unfinished site.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            driver, mutants = self.cancellation_project(root)
            self.assert_real_cancellation(root, driver, mutants, cleanup_denied=False)

    def test_real_scheduler_combined_cancellation_cleanup_failure_retains_evidence(self):
        # Given real completed and running candidates, cancellation plus an injected
        # cleanup denial must retain both failures and every candidate's evidence.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            driver, mutants = self.cancellation_project(root, cleanup_denied=True)
            self.assert_real_cancellation(root, driver, mutants, cleanup_denied=True)

    def assert_real_cancellation(self, root, driver, mutants, *, cleanup_denied):
        unrelated = subprocess.Popen(
            [sys.executable, "-c", "import signal; signal.pause()"],
            start_new_session=True,
        )
        with (root / "controller.log").open("w+") as stream:
            owner = subprocess.Popen(  # noqa: S603 - fixed owned controller, reaped below
                [sys.executable, "-c", driver],
                cwd=ROOT,
                stdout=stream,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            try:
                self.assertTrue(wait_until((root / "runner").exists, 30, interval=0.01))
                pid = int((root / "runner").read_text())
                self.assertEqual(os.getpgid(pid), pid)
                work = Path((root / "work").read_text())
                completed = (work / "result-0.json").read_bytes()
                self.assertEqual(json.loads(completed)["outcome"], "KILLED")
                owner.send_signal(signal.SIGTERM)
                status = owner.wait(timeout=45)
                self.assertNotEqual(status, 0)
                self.assertTrue(work.exists(), "Cancellation deleted completed scheduler evidence")
                self.assertEqual((work / "result-0.json").read_bytes(), completed)
                self.assertTrue((work / "scheduler.log").is_file())
                self.assertTrue(
                    (work / "traceback.txt").is_file(),
                    "Combined cancellation/cleanup failure lost scheduler traceback",
                )
                diagnostic = (work / "traceback.txt").read_text()
                self.assertEqual(status, 2)
                self.assertIn("KeyboardInterrupt", diagnostic)
                if cleanup_denied:
                    self.assertIn("BaseExceptionGroup: Runner and owned cleanup failed", diagnostic)
                    self.assertIn("PermissionError: injected scheduler cleanup denial", diagnostic)
                catalogue = json.loads((work / "mutations.json").read_text())["mutants"]
                stream.seek(0)
                output = [json.loads(line) for line in stream if line.startswith("{")]
                self.assertEqual(output[-1]["outcome"], "INFRA")
                self.assert_cancelled_rows(mutant_rows(output), catalogue, mutants)
                retained = [
                    json.loads(path.read_text()) for path in sorted(work.glob("result-*.json"))
                ]
                self.assert_cancelled_rows(retained, catalogue, mutants)
                self.assertTrue(wait_until(lambda: not self.process_exists(pid), 5, interval=0.01))
                self.assertIsNone(unrelated.poll())
            finally:
                stop(owner, group=True)
                stop(unrelated, group=True)
                self.kill_marked_group(root / "runner")

    def assert_cancelled_rows(self, rows, catalogue, mutants):
        expected = [mutant.key() for mutant in mutants]
        self.assertEqual(len(rows), len(mutants))
        for records in (rows, catalogue):
            self.assertEqual(
                [{name: row[name] for name in expected[0]} for row in records], expected
            )
        self.assertEqual([row["outcome"] for row in rows], ["KILLED", "INFRA", "INFRA"])
        self.assertTrue(all("no result" in row["message"] for row in rows[1:]))

    @staticmethod
    def cancellation_project(root, *, cleanup_denied=False):
        source = "".join(
            f"def {name}():\n    return True\n" for name in ("first", "second", "third")
        )
        project(root, source)
        (root / "tests/test_example.py").write_text(
            "import example, os, signal\nfrom pathlib import Path\n"
            "def test_first():\n    assert example.first() is True\n"
            "def test_second():\n"
            f"    ready = Path({str(root / 'runner')!r})\n"
            "    pending = ready.with_suffix('.pending')\n"
            "    pending.write_text(str(os.getpid()))\n    pending.rename(ready)\n"
            "    signal.pause()\n    assert example.second() is True\n"
            "def test_third():\n    assert example.third() is True\n"
        )
        found, _ = candidates("src/example.py", source.encode())
        mutants = [item for item in found if item.operator == "boolean:literal"]
        mapping = {
            "unit": {
                "src/example.py": {
                    item.line: [f"tests/test_example.py::test_{item.qualname}|run"]
                    for item in mutants
                }
            }
        }
        driver = (
            "from pathlib import Path\nfrom unittest.mock import patch\n"
            "from scripts.validation import mutation_gate as gate\n"
            "from scripts.validation import mutation_run as run_module\n"
            "from scripts.validation.mutation_source import candidates\n"
            f"root = Path({str(root)!r})\n"
            "found, _ = candidates('src/example.py', (root / 'src/example.py').read_bytes())\n"
            "mutants = [item for item in found if item.operator == 'boolean:literal']\n"
            "original = gate.program\n"
            "finish_owned = run_module._finish_owned\n"
            "def denied_cleanup(process, owner, **kwargs):\n"
            "    finish_owned(process, owner, **kwargs)\n"
            "    raise PermissionError('injected scheduler cleanup denial')\n"
            "def run(command, cwd, env, timeout, log, **kwargs):\n"
            "    (root / 'work').write_text(str(cwd))\n"
            "    return original(command, cwd, env, timeout, log, **kwargs)\n"
            "with patch.object(gate, 'collect', return_value=(mutants, [])), "
            f"patch.object(gate, 'imported_coverage', return_value={mapping!r}), "
            "patch.object(gate.random.Random, 'shuffle'), patch.object(gate, 'program', run), "
            "patch.object(run_module, '_finish_owned', "
            f"denied_cleanup if {cleanup_denied!r} else finish_owned):\n"
            "    raise SystemExit(gate.main(['--root', str(root), '--paths', 'src/example.py']))\n"
        )
        return driver, mutants

    def test_scheduler_exit_allows_worker_to_reap_its_separate_session_runner(self):
        # Given cleanup gated on leader exit, program must let the worker finish
        # before sweeping its group, without signaling an unrelated session.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            scheduler = self.cleanup_scheduler(root)
            released = []
            release = Thread(target=self.release_worker, args=(root, released))
            unrelated = subprocess.Popen(
                [sys.executable, "-c", "import signal; signal.pause()"],
                start_new_session=True,
            )
            release.start()
            try:
                status = program(
                    [sys.executable, "-c", scheduler],
                    ROOT,
                    dict(os.environ),
                    20,
                    root / "scheduler.log",
                )
                release.join(timeout=10)
                self.assertEqual(released, [True], (root / "scheduler.log").read_text())
                self.assertEqual(status, 0)
                self.assertTrue(
                    wait_until(
                        lambda: not self.process_exists(int((root / "runner").read_text())),
                        5,
                        interval=0.01,
                    ),
                    "Nested session child survived scheduler group stop",
                )
                self.assertTrue((root / "cleaned").exists(), "Worker cleanup was swept early")
                self.assertIsNone(unrelated.poll())
            finally:
                (root / "release").touch()
                release.join(timeout=10)
                stop(unrelated, group=True)
                self.kill_marked_group(root / "runner")
                self.kill_marked_group(root / "scheduler")

    @staticmethod
    def cleanup_scheduler(root):
        worker = (
            "import signal, subprocess, sys; from pathlib import Path; "
            "from scripts.processes import exit_on_sigterm, stop, wait_until; "
            f"root = Path({str(root)!r}); exit_on_sigterm(); "
            "runner = subprocess.Popen([sys.executable, '-c', "
            "\"import signal; print('ready', flush=True); signal.pause()\"], "
            "stdout=subprocess.PIPE, text=True, start_new_session=True); "
            "runner.stdout.readline(); (root / 'runner').write_text(str(runner.pid))\n"
            "try:\n    runner.wait(timeout=30)\n"
            "finally:\n"
            "    (root / 'cleaning').touch()\n"
            "    wait_until((root / 'release').exists, 30, interval=0.01)\n"
            "    stop(runner, group=True)\n"
            "    (root / 'cleaned').touch()\n"
        )
        return (
            "import os, signal, subprocess, sys; from pathlib import Path; "
            "from scripts.processes import wait_until; "
            f"root = Path({str(root)!r})\n"
            "def finish(signum, frame):\n"
            "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "    assert wait_until((root / 'cleaning').exists, 10, interval=0.01)\n"
            "    raise SystemExit(0)\n"
            "signal.signal(signal.SIGTERM, finish)\n"
            f"subprocess.Popen([sys.executable, '-c', {worker!r}])\n"
            "(root / 'scheduler').write_text(str(os.getpid()))\n"
            "signal.pause()\n"
        )

    @staticmethod
    def release_worker(root, released):
        if not wait_until(
            lambda: (root / "runner").exists() and (root / "scheduler").exists(),
            10,
            interval=0.01,
        ):
            return
        scheduler = int((root / "scheduler").read_text())
        os.killpg(scheduler, signal.SIGTERM)
        exited = wait_until(
            lambda: (
                os.waitid(os.P_PID, scheduler, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None
            ),
            10,
            interval=0.01,
        )
        released.append(exited)
        (root / "release").touch()

    @staticmethod
    def kill_marked_group(marker):
        if marker.exists():
            with contextlib.suppress(ProcessLookupError):
                os.killpg(int(marker.read_text()), signal.SIGKILL)

    def test_cancelling_parent_reaps_nested_session_runner_and_preserves_unrelated_process(self):
        # Given a ready runner in a separate session, cancelling its waiting parent
        # must unwind owned cleanup rather than orphan that runner.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            marker = root / "runner.pid"
            child = (
                "import os, signal; from pathlib import Path; "
                f"marker = Path({str(marker)!r}); "
                "ready = marker.with_suffix('.ready'); ready.write_text(str(os.getpid())); "
                "ready.rename(marker); signal.pause()"
            )
            parent = (
                "import os, sys; from pathlib import Path; "
                "from scripts.validation.mutation_run import program; "
                f"program([sys.executable, '-c', {child!r}], Path({str(root)!r}), "
                f"dict(os.environ), 60, Path({str(root / 'runner.log')!r}))"
            )
            unrelated = subprocess.Popen(
                [sys.executable, "-c", "import signal; signal.pause()"],
                start_new_session=True,
            )
            with (root / "parent.log").open("wb") as stream:
                owner = subprocess.Popen(  # noqa: S603 - owned test parent, reaped below
                    [sys.executable, "-c", parent],
                    cwd=ROOT,
                    stdout=stream,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
                try:
                    self.assertTrue(wait_until(marker.exists, 10, interval=0.01))
                    pid = int(marker.read_text())
                    self.assertEqual(os.getpgid(pid), pid)
                    owner.send_signal(signal.SIGTERM)
                    owner.wait(timeout=15)
                    self.assertTrue(
                        wait_until(lambda: not self.process_exists(pid), 5, interval=0.01),
                        "Nested session runner survived parent cancellation",
                    )
                    self.assertIsNone(unrelated.poll())
                finally:
                    stop(owner, group=True)
                    stop(unrelated, group=True)
                    if marker.exists() and self.process_exists(int(marker.read_text())):
                        os.killpg(int(marker.read_text()), signal.SIGKILL)

    @staticmethod
    def process_exists(pid):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        return True

    def test_program_restores_callers_sigterm_handler_after_completion_and_timeout(self):
        original = signal.signal(signal.SIGTERM, signal.SIG_IGN)
        try:
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                for script, timeout, expected in (
                    ("raise SystemExit(7)", 10, 7),
                    ("import signal; signal.pause()", 0.1, None),
                ):
                    with self.subTest(script=script):
                        status = program(
                            [sys.executable, "-c", script],
                            root,
                            dict(os.environ),
                            timeout,
                            root / "child.log",
                        )
                        self.assertEqual(status, expected)
                        self.assertEqual(signal.getsignal(signal.SIGTERM), signal.SIG_IGN)
        finally:
            signal.signal(signal.SIGTERM, original)

    def test_collection_constant_selects_killing_consumer_but_runtime_lines_stay_precise(self):
        # Given collection and an incidental child import share a declaration,
        # when the CLI mutates it, then the cached-module consumer kills it;
        # runtime-only lines still select just their own caller.
        source = "FLAG = True\nanswer = (lambda:\n    not not FLAG)\ndef harmless():\n    pass\n"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            project(root, source)
            launches = root / "unit-selections.jsonl"
            (root / "tests/conftest.py").write_text(
                "import example, json\nfrom pathlib import Path\n"
                "def pytest_collection_finish(session):\n"
                f"    with Path({str(launches)!r}).open('a') as stream:\n"
                "        stream.write(json.dumps([item.nodeid for item in session.items])"
                " + '\\n')\n"
            )
            (root / "tests/test_example.py").write_text(
                "import example, subprocess, sys\n"
                "def test_incidental():\n"
                "    child = subprocess.run([sys.executable, '-c', 'import example'])\n"
                "    assert child.returncode == 0\n"
                "    assert example.harmless() is None\n"
                "def test_value():\n    assert example.answer() is True\n"
                "def test_unrelated():\n    assert True\n"
            )
            status, rows = command(root, "--paths", "src/example.py")
            results = mutant_rows(rows)
            declaration = [row for row in results if row["line"] == 1]
            self.assertEqual(len(declaration), 1, rows)
            self.assertEqual(declaration[0]["outcome"], "KILLED", declaration)
            self.assertEqual(declaration[0]["node"], "tests/test_example.py::test_value")
            self.assertEqual(declaration[0]["kind"], "assertion")
            self.assertEqual(status, 0, rows)
            incidental = "tests/test_example.py::test_incidental"
            consumer = "tests/test_example.py::test_value"
            unrelated = "tests/test_example.py::test_unrelated"
            self.assertCountEqual(
                json_lines(launches),
                [[incidental, consumer, unrelated], [incidental, consumer], [consumer], [consumer]],
            )

    def test_prepared_fixture_stdio_rpc_reaches_source_with_honest_setup_fallback(self):
        # A session-scoped MCP child keeps its spawn-time setup context even
        # when this test's later RPC executes a handler for the first time.
        node = (
            "tests/test_evs.py::test_a_discovered_release_identifies_the_concept_and_its_provenance"
        )
        with snapshot(ROOT) as scratch:
            result, data = run_domain(scratch, "fixture", [node], 120, baseline=True)
            self.assertEqual(result["outcome"], "SURVIVED", result)
            mapping = coverage_map(data, scratch)
            source = "src/nci_sip_mcp/content.py"
            line = next(
                number
                for number, text in enumerate((scratch / source).read_text().splitlines(), 1)
                if text.strip() == "code = _code(code, terminology)"
            )
            self.assertEqual(set(mapping[source][line]), {f"{node}|setup"})
            self.assertEqual(selection({"fixture": mapping}, source, line, line), {"fixture": []})
            self.assertFalse(
                any(
                    mapping.get(f"scripts/validation/{name}.py", {})
                    for name in (
                        "mutation_gate",
                        "mutation_run",
                        "mutation_source",
                        "mutation_probe",
                    )
                )
            )

    def test_unreached_fixture_survivor_never_launches_a_confirmation_process(self):
        # Given a unit-only call, the full fixture baseline still runs, but no
        # mutant may launch a fixture process that cannot execute the changed line.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            project(
                root, "def answer():\n    return True\n", "assert example.answer() is not Ellipsis"
            )
            (root / "acceptance/tests/test_contract.py").write_text(
                "def test_contract():\n    assert 1 == 1\n"
            )
            log = root / "fixture-calls.jsonl"
            (root / "acceptance/conftest.py").write_text(
                "import json, os\nfrom pathlib import Path\n"
                "def pytest_addoption(parser):\n    parser.addoption('--report')\n"
                "def pytest_sessionstart(session):\n"
                "    if os.environ.get('NCI_SIP_ACCEPTANCE_MODE') != 'fixture':\n        return\n"
                f"    with Path({str(log)!r}).open('a') as stream:\n"
                "        stream.write(json.dumps(session.config.getoption('cov_context'))"
                " + '\\n')\n"
            )
            status, rows = command(root, "--paths", "src/example.py")
            survivors = mutant_rows(rows)
            self.assertEqual(status, 1, rows)
            self.assertEqual({row["outcome"] for row in survivors}, {"SURVIVED"})
            self.assertEqual({row.get("confirmation") for row in survivors}, {"unreached"})
            self.assertEqual(
                domain_routes(survivors),
                [["unit"]] * 3,
            )
            launches = json_lines(log)
            # One controller plus its four workers belong to the mandatory baseline.
            self.assertEqual(len(launches), 5, launches)
            self.assertEqual(launches, ["test"] * 5)

    def test_unreached_domains_do_not_hide_survival_or_claim_fixture_confirmation(self):
        orderings = (
            ("fixture", "unit"),
            ("unit", "fixture"),
        )
        for ordering in orderings:
            with self.subTest(ordering=ordering):
                selected = {domain: ["tests/test_owner.py::test_value"] for domain in ordering}
                results = [
                    (
                        {
                            "outcome": "NO_TESTS" if domain == "fixture" else "SURVIVED",
                            "domain": domain,
                            "elapsed_seconds": 0.01,
                        },
                        Path("coverage"),
                    )
                    for domain in ordering
                ]
                with patch.object(mutation_gate, "run_domain", side_effect=results):
                    result = mutation_gate.measure(Path("."), selected, 1)

                self.assertEqual(result["outcome"], "SURVIVED", result)
                self.assertEqual(result["domain"], "unit", result)
                self.assertEqual(result["confirmation"], "unreached", result)
                self.assertEqual([row["domain"] for row in result["domains"]], list(ordering))

    def test_fixture_timeout_is_not_reported_as_fixture_confirmation(self):
        selected = {"fixture": ["tests/test_owner.py::test_value"]}
        timed_out = {
            "outcome": "TIMEOUT",
            "domain": "fixture",
            "elapsed_seconds": 0.01,
        }
        with patch.object(mutation_gate, "run_domain", return_value=(timed_out, Path("coverage"))):
            result = mutation_gate.measure(Path("."), selected, 1)

        self.assertEqual(result["outcome"], "TIMEOUT", result)
        self.assertEqual(result["confirmation"], "unreached", result)
        self.assertNotIn("kind", result)
        self.assertNotIn("node", result)

    def test_cli_continues_from_unreached_unit_to_independent_fixture_kill(self):
        source = "def answer():\n    return True\n"
        unit = (
            "if example.answer():\n"
            "        assert example.answer() is True\n"
            "    else:\n"
            "        import pytest\n"
            "        pytest.skip('unit owner does not pass for this mutation')"
        )
        contract = "assert example.answer() is True"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            project(root, source, unit, contract)
            source_path = root / "src/example.py"
            original = source_path.read_bytes()
            mutants, _ = candidates("src/example.py", original)
            target = next(
                mutant
                for mutant in mutants
                if (mutant.operator, mutant.before, mutant.after)
                == ("boolean:literal", "True", "False")
            )
            shard = isolated_shard(mutants, target)

            status, rows = command(root, "--paths", "src/example.py", "--shard", shard)

            selected = mutant_rows(rows)
            self.assertEqual(status, 0, rows)
            self.assertEqual(len(selected), 1, rows)
            result = selected[0]
            self.assertEqual(
                {key: result[key] for key in target.key()},
                target.key(),
            )
            self.assertEqual(result["outcome"], "KILLED", result)
            self.assertEqual(result["domain"], "fixture", result)
            self.assertEqual(result["node"], "tests/test_contract.py::test_contract", result)
            self.assertEqual(result["confirmation"], "fixture", result)
            self.assertIs(result["restored"], True)
            self.assertEqual(
                [(row["domain"], row["outcome"]) for row in result["domains"]],
                [("unit", "NO_TESTS"), ("fixture", "KILLED")],
            )
            self.assertEqual(source_path.read_bytes(), original)

    def test_fixture_only_child_line_selects_its_one_owner_and_kills_without_unit_coverage(self):
        # Given a line reached only in a fixture test's real child program, run
        # that owner alone after the clean three-domain baseline, never NO_TESTS.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            project(root, "def answer():\n    return True\n", "assert True")
            log = root / "fixture-calls.jsonl"
            (root / "acceptance/conftest.py").write_text(
                "import json\nfrom pathlib import Path\n"
                "def pytest_addoption(parser):\n    parser.addoption('--report')\n"
                "def pytest_runtest_logreport(report):\n"
                "    if report.when == 'call':\n"
                f"        with Path({str(log)!r}).open('a') as stream:\n"
                "            stream.write(json.dumps(report.nodeid) + '\\n')\n"
            )
            (root / "acceptance/tests/test_contract.py").write_text(
                "import subprocess, sys\n"
                "def test_contract():\n"
                "    child = subprocess.run([sys.executable, '-c', "
                "'import example; print(example.answer())'], capture_output=True, text=True)\n"
                "    assert child.returncode == 0, child.stderr\n"
                "    assert child.stdout.strip() == 'True'\n"
                "def test_unrelated():\n    assert True\n"
            )
            status, rows = command(root, "--paths", "src/example.py")
            results = mutant_rows(rows)
            self.assertEqual(status, 0, rows)
            self.assertEqual({row["outcome"] for row in results}, {"KILLED"})
            self.assertEqual(
                {row["node"] for row in results}, {"tests/test_contract.py::test_contract"}
            )
            self.assertEqual({row["confirmation"] for row in results}, {"fixture"})
            self.assertEqual([row["fallback_domains"] for row in results], [[], [], []])
            self.assertEqual(
                domain_routes(results),
                [["fixture"]] * 3,
            )
            calls = json_lines(log)
            # xdist's worker and controller both report baseline calls; serial
            # confirmations report once. Unrelated calls belong only to baseline.
            self.assertEqual(calls.count("tests/test_contract.py::test_unrelated"), 2)
            self.assertEqual(calls.count("tests/test_contract.py::test_contract"), 5)

    def test_subject_probe_plugin_labels_real_child_phases_and_restores_parent_context(self):
        # Given the registered repository plugin, each phase's competing wrapper
        # must pass its label to a real child and restore the caller's context.
        parent_context = os.environ.get("MUTATION_COVERAGE_CONTEXT")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            project(root, "def answer():\n    return True\n")
            (root / "tests/test_nested.py").write_text(
                "import os, pytest\nfrom pathlib import Path\n"
                "from scripts.validation import mutation_probe\n"
                "@pytest.fixture\ndef phase():\n"
                "    assert os.environ['MUTATION_COVERAGE_CONTEXT'] == "
                "'tests/test_nested.py::test_phases|setup'\n"
                "    yield\n"
                "    assert os.environ['MUTATION_COVERAGE_CONTEXT'] == "
                "'tests/test_nested.py::test_phases|teardown'\n"
                "def test_phases(phase):\n"
                f"    assert Path(mutation_probe.__file__).resolve() == "
                f"Path({str(ROOT / 'scripts/validation/mutation_probe.py')!r})\n"
                "    assert os.environ['MUTATION_COVERAGE_CONTEXT'] == "
                "'tests/test_nested.py::test_phases|run'\n"
            )
            (root / "tests/conftest.py").write_text(
                "import os, pytest, subprocess, sys\n"
                "def check_child(item, phase):\n"
                "    if not item.nodeid.endswith('test_phases'): return\n"
                "    child = subprocess.run([sys.executable, '-c', "
                "\"import os; print(os.environ.get('MUTATION_COVERAGE_CONTEXT'))\"], "
                "capture_output=True, text=True, check=True)\n"
                "    assert child.stdout.strip() == item.nodeid + '|' + phase\n"
                "@pytest.hookimpl(wrapper=True)\n"
                "def pytest_runtest_setup(item):\n"
                "    check_child(item, 'setup')\n    return (yield)\n"
                "@pytest.hookimpl(wrapper=True)\n"
                "def pytest_runtest_call(item):\n"
                "    check_child(item, 'run')\n    return (yield)\n"
                "@pytest.hookimpl(wrapper=True)\n"
                "def pytest_runtest_teardown(item):\n"
                "    check_child(item, 'teardown')\n    return (yield)\n"
                "def pytest_runtest_logfinish(nodeid):\n"
                "    if nodeid.endswith('test_phases'):\n"
                "        assert os.environ.get('MUTATION_COVERAGE_CONTEXT') == "
                f"{parent_context!r}\n"
            )
            result = subprocess.run(  # noqa: S603 - real registered plugin, owned offline fixture
                [
                    sys.executable,
                    "-c",
                    f"import sys; sys.path.insert(0, {str(ROOT)!r}); "
                    "import pytest; raise SystemExit(pytest.main(sys.argv[1:]))",
                    "-p",
                    "scripts.validation.mutation_probe",
                    "--no-cov",
                    "-n",
                    "0",
                    "tests/test_nested.py",
                ],
                cwd=root,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_python_module_children_keep_each_xdist_owners_phase_and_inprocess_selection(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            project(root, "def answer():\n    return True\n")
            (root / "src/child.py").write_text(
                "import sys\ndef first():\n    return True\n"
                "def second():\n    return True\n"
                "if __name__ == '__main__':\n    print(globals()[sys.argv[1]]())\n"
            )
            (root / "acceptance/selftests/test_harness.py").write_text(
                "import subprocess, sys, example\nimport pytest\n"
                "def launch(name):\n"
                "    result = subprocess.run([sys.executable, '-m', 'child', name], "
                "capture_output=True, text=True, check=True)\n"
                "    assert result.stdout.strip() == 'True'\n"
                "@pytest.fixture\ndef child_fixture():\n"
                "    launch('first')\n    yield\n    launch('second')\n"
                "def test_first():\n    launch('first')\n    assert example.answer()\n"
                "def test_second():\n    launch('second')\n"
                "def test_phases(child_fixture):\n    assert True\n"
            )
            result, data = run_domain(root, "selftest", [], 30, baseline=True)
            self.assertEqual(result["outcome"], "SURVIVED", result)
            mapping = coverage_map(data, root)
            self.assertEqual(
                selection({"selftest": mapping}, "src/child.py", 3, 3),
                {"selftest": ["selftests/test_harness.py::test_first"]},
            )
            self.assertEqual(
                selection({"selftest": mapping}, "src/child.py", 5, 5),
                {"selftest": ["selftests/test_harness.py::test_second"]},
            )
            self.assertEqual(
                selection({"selftest": mapping}, "src/example.py", 2, 2),
                {"selftest": ["selftests/test_harness.py::test_first"]},
            )
            self.assertEqual(
                set(mapping["src/child.py"][3]),
                {
                    "selftests/test_harness.py::test_first|run",
                    "selftests/test_harness.py::test_phases|setup",
                },
            )
            self.assertEqual(
                set(mapping["src/child.py"][5]),
                {
                    "selftests/test_harness.py::test_second|run",
                    "selftests/test_harness.py::test_phases|teardown",
                },
            )

    def test_call_typeerror_is_an_error_kill_but_setup_error_is_infrastructure(self):
        source = "def answer():\n    return True\n"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            project(root, source, "example.answer() + 1")
            baseline, _ = run_domain(root, "unit", [], 20, baseline=True)
            self.assertEqual(baseline["outcome"], "SURVIVED", baseline)
            (root / "src/example.py").write_text(source.replace("True", "None"))
            result, _ = run_domain(root, "unit", [], 20)
            self.assertEqual(
                {key: result.get(key) for key in ("outcome", "kind", "node", "phase")},
                {
                    "outcome": "KILLED",
                    "kind": "error",
                    "node": "tests/test_example.py::test_value",
                    "phase": "call",
                },
            )
            (root / "tests/conftest.py").write_text(
                "import pytest, example\n@pytest.fixture(autouse=True)\n"
                "def setup():\n    example.answer() + 1\n"
            )
            result, _ = run_domain(root, "unit", [], 20)
            self.assertEqual(result["outcome"], "INFRA", result)
            self.assertEqual(result["failures"][0]["phase"], "setup")

    def test_mixed_subtests_keep_one_owning_error_kill(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            project(root, "def answer():\n    return True\n")
            (root / "tests/test_example.py").write_text(
                "import unittest, example\nclass Mixed(unittest.TestCase):\n"
                "    def test_mixed(self):\n"
                "        with self.subTest(kind='assertion'):\n"
                "            self.assertIs(example.answer(), True)\n"
                "        with self.subTest(kind='error'):\n"
                "            example.answer() + 1\n"
            )
            baseline, _ = run_domain(root, "unit", [], 20, baseline=True)
            self.assertEqual(baseline["outcome"], "SURVIVED", baseline)
            (root / "src/example.py").write_text("def answer():\n    return None\n")
            result, data = run_domain(root, "unit", [], 20)
            self.assertEqual((result["outcome"], result.get("kind")), ("KILLED", "error"))
            rows = [
                json.loads(line) for line in data.with_name("events.jsonl").read_text().splitlines()
            ]
            self.assertEqual(
                [row for row in rows if row["outcome"] == "failed"],
                [
                    {
                        "node": "tests/test_example.py::Mixed::test_mixed",
                        "phase": "call",
                        "outcome": "failed",
                        "assertion": False,
                    }
                ],
            )

    def test_mutated_probe_is_subject_but_cannot_corrupt_controller_collection_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            project(root, "def answer():\n    return True\n")
            package = root / "scripts/validation"
            package.mkdir(parents=True)
            (package.parent / "__init__.py").write_text("")
            (package / "__init__.py").write_text("")
            original = (ROOT / "scripts/validation/mutation_probe.py").read_text()
            subject = package / "mutation_probe.py"
            subject.write_text(original)
            (root / "tests/test_example.py").write_text(
                "from pathlib import Path\nfrom types import SimpleNamespace\n"
                "from scripts.validation import mutation_probe as subject\n"
                "def test_collection_contract(tmp_path):\n"
                "    destination = tmp_path / 'subject.jsonl'\n"
                "    original = subject.DESTINATION\n"
                "    try:\n"
                "        subject.DESTINATION = str(destination)\n"
                "        subject.pytest_collectreport(\n"
                "            SimpleNamespace(failed=False, nodeid='good'))\n"
                "    finally:\n"
                "        subject.DESTINATION = original\n"
                "    assert not destination.exists()\n"
            )
            baseline, _ = run_domain(root, "unit", [], 20, baseline=True)
            self.assertEqual(baseline["outcome"], "SURVIVED", baseline)
            subject.write_text(original.replace("destination and report.failed", "destination"))
            result, data = run_domain(root, "unit", [], 20)
            self.assertEqual(
                (result["outcome"], result.get("kind")), ("KILLED", "assertion"), result
            )
            events = [
                json.loads(line) for line in data.with_name("events.jsonl").read_text().splitlines()
            ]
            self.assertEqual(
                [(row["node"], row["phase"]) for row in events],
                [
                    ("tests/test_example.py::test_collection_contract", phase)
                    for phase in ("setup", "call", "teardown")
                ],
            )

    def test_baseline_maps_line_executed_only_in_pdm_script_child(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            project(root, "def answer():\n    return True\n")
            (root / "pyproject.toml").write_text(
                '[project]\nname = "mapping-contract"\nversion = "0.0.0"\n'
                'requires-python = ">=3.14"\n'
                '[tool.pytest.ini_options]\npythonpath = ["src"]\n'
                '[tool.pdm.scripts]\nmutation-gate = "python -m scripts.validation.mutation_gate"\n'
            )
            (root / ".pdm-python").write_text(sys.executable)
            (root / "src/child.py").write_text("# no mutation sites\n")
            # Use the actual command helper in a snapshot of the invoking project;
            # its child runs mutation_gate.main while the inner gate remains uninstrumented.
            (root / "tests/test_example.py").write_text(
                "from test_mutation_gate import command\nfrom pathlib import Path\n"
                "def test_child():\n"
                "    status, rows = command(Path.cwd(), '--paths', 'src/child.py', "
                "'--allowlist', 'allow.yaml')\n"
                "    assert (status, rows) == (0, [{'outcome': 'EMPTY', 'mutants': 0}])\n"
                "def test_unrelated():\n    assert True\n"
            )
            # The helper's ROOT must be this snapshot, as it is in a real baseline.
            (root / "tests/test_mutation_gate.py").write_text(
                (ROOT / "tests/test_mutation_gate.py")
                .read_text()
                .split("class MutationOperatorsTest")[0]
            )
            shutil.copytree(ROOT / "scripts", root / "scripts")
            # This tiny independent project owns no reviewed production equivalents.
            (root / "allow.yaml").write_text("[]\n")
            result, data = run_domain(root, "unit", [], 30, baseline=True)
            self.assertEqual(result["outcome"], "SURVIVED", result)
            mapping = coverage_map(data, root)
            line = next(
                i
                for i, text in enumerate(
                    (root / "scripts/validation/mutation_gate.py").read_text().splitlines(), 1
                )
                if text.strip() == "args = parser.parse_args(argv)"
            )
            self.assertEqual(
                selection({"unit": mapping}, "scripts/validation/mutation_gate.py", line, line),
                {"unit": ["tests/test_example.py::test_child"]},
            )
            mutants, _ = candidates(
                "scripts/validation/mutation_gate.py",
                (root / "scripts/validation/mutation_gate.py").read_bytes(),
            )
            child_only = [m for m in mutants if m.qualname == "main" and m.before == "return 0"]
            self.assertEqual(len(child_only), 3)
            routes = mutation_gate.manifest_data(root, child_only, {"unit": mapping}, 30)
            self.assertEqual(
                [row["selected"] for row in routes["mutants"]],
                [{"unit": ["tests/test_example.py::test_child"]}] * 3,
            )

    def test_inherited_probe_child_cannot_append_to_parent_evidence(self):
        # A caller may launch pytest directly, without the gate's environment helper.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            project(root, "def answer():\n    return True\n")
            (root / "tests/test_child.py").write_text("def test_child():\n    assert True\n")
            (root / "tests/test_example.py").write_text(
                "import subprocess, sys\ndef test_outer():\n"
                "    child = subprocess.run([sys.executable, '-m', 'pytest', '-p', "
                "'scripts.validation.mutation_probe', '--no-cov', '-n', '0', "
                "'tests/test_child.py'], capture_output=True)\n"
                "    assert child.returncode == 0\n"
                "    assert False, 'outer mutation detected'\n"
            )
            result, data = run_domain(root, "unit", ["tests/test_example.py::test_outer"], 30)
            self.assertEqual(result["outcome"], "KILLED", result)
            events = [
                json.loads(line) for line in data.with_name("events.jsonl").read_text().splitlines()
            ]
            self.assertEqual({row["node"] for row in events}, {"tests/test_example.py::test_outer"})

    def test_scheduler_manifest_shares_source_and_keeps_only_selected_test_routes(self):
        source = "def answer():\n    return True\n"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            project(root, source)
            mutants, _ = candidates("src/example.py", source.encode())
            mutant = mutants[-1]
            mapping = {"unit": {"src/example.py": {2: ["tests/test_example.py::test_value|run"]}}}
            captured = []

            def scheduler(command, cwd, env, timeout, log, *, worker_cleanup):
                manifest = Path(command[-1])
                captured.append(json.loads(manifest.read_text()))
                log.write_text("")
                (manifest.parent / "result-0.json").write_text(
                    json.dumps({**asdict(mutant), "outcome": "KILLED", "restored": True})
                )
                return 0

            with patch.object(mutation_gate, "program", side_effect=scheduler):
                rows = mutation_gate.schedule(
                    root, [mutant], mapping, SimpleNamespace(timeout=20, workers=1)
                )
            self.assertEqual(rows, [{**asdict(mutant), "outcome": "KILLED", "restored": True}])
            self.assertEqual(
                captured,
                [
                    {
                        "root": str(root),
                        "sources": {"src/example.py": source},
                        "mutants": [
                            {
                                **mutant.key(),
                                "line": 2,
                                "end_line": 2,
                                "start": mutant.start,
                                "end": mutant.end,
                                "after": "False",
                                "selected": {"unit": ["tests/test_example.py::test_value"]},
                            }
                        ],
                        "timeout": 20,
                    }
                ],
            )

    def test_mutant_report_records_measured_total_and_domain_elapsed_seconds(self):
        source = "def answer():\n    return True\n"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            project(root, source)
            mutants, _ = candidates("src/example.py", source.encode())
            manifest = root / "mutations.json"
            manifest.write_text(
                json.dumps(
                    {
                        "root": str(root),
                        "sources": {"src/example.py": source},
                        "mutants": [
                            {
                                **{
                                    key: value
                                    for key, value in asdict(mutants[-1]).items()
                                    if key != "before"
                                },
                                "selected": {"unit": ["tests/test_example.py::test_value"]},
                            }
                        ],
                        "timeout": 20,
                    }
                )
            )
            execute_job(manifest, 0)
            row = json.loads((root / "result-0.json").read_text())
            self.assertEqual(
                set(row) & {"elapsed_seconds", "domains"}, {"elapsed_seconds", "domains"}
            )
            self.assertEqual(
                [
                    {key: value for key, value in domain.items() if key != "elapsed_seconds"}
                    for domain in row["domains"]
                ],
                [{"domain": "unit", "outcome": "KILLED"}],
            )
            self.assertIsInstance(row["elapsed_seconds"], float)
            self.assertIsInstance(row["domains"][0]["elapsed_seconds"], float)
            self.assertEqual(row["fallback_domains"], [])
            self.assertEqual(
                set(row),
                set(asdict(mutants[-1]))
                | {
                    "outcome",
                    "node",
                    "phase",
                    "kind",
                    "domain",
                    "log",
                    "elapsed_seconds",
                    "domains",
                    "fallback_domains",
                    "restored",
                },
            )
            for selected, expected in (({"unit": []}, ["unit"]), ({}, [])):
                jobs = json.loads(manifest.read_text())
                jobs["mutants"][0]["selected"] = selected
                manifest.write_text(json.dumps(jobs))
                execute_job(manifest, 0)
                result = json.loads((root / "result-0.json").read_text())
                self.assertEqual(result["fallback_domains"], expected)
                self.assertIsInstance(result["elapsed_seconds"], float)
                self.assertGreaterEqual(result["elapsed_seconds"], 0)
                self.assertTrue(result["restored"])
            self.assertEqual(
                set(result),
                set(asdict(mutants[-1]))
                | {"outcome", "domains", "fallback_domains", "restored", "elapsed_seconds"},
            )
            self.assertEqual((result["outcome"], result["domains"]), ("NO_TESTS", []))

    def test_nested_probe_and_multiple_subtest_failures_credit_exactly_one_outer_kill(self):
        # Given a selected unittest that runs a child probe, when two subtests detect
        # the mutant, then only its outer node owns the kill and child evidence is separate.
        source = "def answer():\n    return True\n"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            project(root, source)
            project(root / "nested", source)
            (root / "tests/test_example.py").write_text(
                "import unittest, example\nfrom pathlib import Path\n"
                "from scripts.validation.mutation_run import run_domain\n"
                "class Outer(unittest.TestCase):\n"
                "    def test_nested(self):\n"
                "        result, data = run_domain(Path('nested').resolve(), 'unit', [], 20)\n"
                "        self.assertEqual(result['outcome'], 'SURVIVED')\n"
                "        for value in (1, 2):\n"
                "            with self.subTest(value=value):\n"
                "                self.assertIs(example.answer(), True)\n"
            )
            (root / "src/example.py").write_text(source.replace("True", "False"))
            result, data = run_domain(
                root, "unit", ["tests/test_example.py::Outer::test_nested"], 30
            )
            self.assertEqual(result["outcome"], "KILLED", result)
            self.assertEqual(result["node"], "tests/test_example.py::Outer::test_nested")
            events = [
                json.loads(line) for line in data.with_name("events.jsonl").read_text().splitlines()
            ]
            self.assertEqual(
                [row for row in events if row["outcome"] == "failed"],
                [
                    {
                        "node": "tests/test_example.py::Outer::test_nested",
                        "phase": "call",
                        "outcome": "failed",
                        "assertion": True,
                    }
                ],
            )
            self.assertEqual(
                {row["node"] for row in events}, {"tests/test_example.py::Outer::test_nested"}
            )

    def test_uncovered_sites_fail_no_tests_and_reviewed_survivors_record_unreached_fixture(
        self,
    ):
        source = "def answer():\n    return True\n"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            project(root, source, "assert True")
            status, rows = command(root, "--paths", "src/example.py")
            self.assertEqual(status, 1)
            self.assertEqual({row["outcome"] for row in rows}, {"NO_TESTS", "NO_OP"})
            project(root, source, "assert example.answer() is not Ellipsis")
            mutants, _ = candidates("src/example.py", source.encode())
            (root / "allow.yaml").write_text(
                yaml.safe_dump(
                    [
                        {**mutant.key(), "reason": "Fixture deliberately ignores this value."}
                        for mutant in mutants
                    ]
                )
            )
            status, rows = command(root, "--paths", "src/example.py", "--allowlist", "allow.yaml")
            self.assertEqual(status, 0, rows)
            allowed = [row for row in rows if row["outcome"] == "ALLOWLISTED"]
            self.assertEqual(len(allowed), 3)
            self.assertEqual({row["confirmation"] for row in allowed}, {"unreached"})

    def test_fixture_owns_a_kill_when_units_only_execute_the_value(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            project(
                root,
                "def answer():\n    return True\n",
                "assert example.answer() is not Ellipsis",
                "assert example.answer() is True",
            )
            status, rows = command(root, "--paths", "src/example.py", "-n", "2")
            actual = [row for row in rows if row["outcome"] != "NO_OP"]
            self.assertEqual(status, 0, rows)
            self.assertEqual({row["outcome"] for row in actual}, {"KILLED"})
            self.assertEqual({row["domain"] for row in actual}, {"fixture"})
            self.assertEqual(
                {row["node"] for row in actual}, {"tests/test_contract.py::test_contract"}
            )
            self.assertEqual({row["confirmation"] for row in actual}, {"fixture"})

    def test_child_imports_and_console_programs_use_scratch_and_seed_does_not_change_results(self):
        source = "def answer():\n    return True\n"
        unit = (
            "import subprocess, sys\n"
            "    child = subprocess.run([sys.executable, '-c', "
            "'import example; print(example.answer())'], capture_output=True, text=True)\n"
            "    assert child.stdout.strip() == 'True'\n"
            "    console = subprocess.run(['nci-sip-mcp'], capture_output=True, text=True)\n"
            "    assert console.stdout.strip() == 'True'"
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            project(root, source, unit)
            package = root / "src/nci_sip_mcp"
            package.mkdir()
            (package / "__init__.py").write_text("")
            (package / "cli.py").write_text(
                "import example\ndef main():\n    print(example.answer())\n"
            )
            status, first = command(root, "--paths", "src/example.py", "-n", "2", "--seed", "7")
            other, second = command(root, "--paths", "src/example.py", "--seed", "41")
            self.assertEqual((status, other), (0, 0), (first, second))
            self.assertEqual(
                [(row.get("operator"), row["outcome"]) for row in first],
                [(row.get("operator"), row["outcome"]) for row in second],
            )
            self.assertEqual((root / "src/example.py").read_text(), source)

    def test_setup_collection_crash_and_skips_cannot_be_assertion_kills(self):
        programs = {
            "setup": (
                "import pytest\n@pytest.fixture(autouse=True)\ndef broken():\n"
                "    assert False\ndef test_x():\n    assert True\n"
            ),
            "collection": "raise AssertionError('collection')\n",
            "crash": "import os\ndef test_crash():\n    os._exit(9)\n",
            "skip": "import pytest\ndef test_skip():\n    pytest.skip('absent')\n",
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            project(root, "def answer():\n    return True\n")
            for name, program_text in programs.items():
                with self.subTest(name=name), snapshot(root) as scratch:
                    (scratch / "tests/test_example.py").write_text(program_text)
                    result, _ = run_domain(scratch, "unit", [], 20)
                    self.assertEqual(result["outcome"], "NO_TESTS" if name == "skip" else "INFRA")

    def test_mutant_crash_and_timeout_restore_scratch_and_never_edit_caller(self):
        cases = {
            "crash": ("import os\n    if not example.answer(): os._exit(8)", "INFRA", 20),
            "timeout": ("import time\n    if not example.answer(): time.sleep(60)", "TIMEOUT", 1),
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            source = "def answer():\n    return True\n"
            for name, (body, expected, timeout) in cases.items():
                with self.subTest(name=name):
                    project(root, source, body + "\n    assert True")
                    mutants, _ = candidates("src/example.py", source.encode())
                    mutant = next(item for item in mutants if item.operator == "boolean:literal")
                    manifest = root / "mutations.json"
                    manifest.write_text(
                        json.dumps(
                            {
                                "root": str(root),
                                "sources": {"src/example.py": source},
                                "mutants": [
                                    {
                                        **{
                                            key: value
                                            for key, value in asdict(mutant).items()
                                            if key != "before"
                                        },
                                        "selected": {"unit": ["tests/test_example.py::test_value"]},
                                    }
                                ],
                                "timeout": timeout,
                            }
                        )
                    )
                    execute_job(manifest, 0)
                    row = json.loads((root / "result-0.json").read_text())
                    self.assertEqual(row["outcome"], expected, row)
                    self.assertTrue(row["restored"])
                    self.assertEqual((root / "src/example.py").read_text(), source)


class MutationCommandTest(unittest.TestCase):
    def test_an_unkilled_mutant_fails_with_source_and_unreached_fixture(self):
        # Given an executed return whose value neither domain checks, when the gate runs,
        # then it records the unreached fixture domain and fails closed.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            files = {
                "src/example.py": "def answer():\n    return True\n",
                "tests/test_example.py": (
                    "from example import answer\ndef test_call():\n"
                    "    assert answer() is not Ellipsis\n"
                ),
                "acceptance/selftests/test_harness.py": (
                    "def test_harness():\n    assert 2 + 2 == 4\n"
                ),
                "acceptance/tests/test_contract.py": (
                    "def test_contract():\n    assert 3 + 3 == 6\n"
                ),
                "acceptance/conftest.py": (
                    "def pytest_addoption(parser):\n    parser.addoption('--report')\n"
                ),
                "pyproject.toml": '[tool.pytest.ini_options]\npythonpath = ["src"]\n',
            }
            for name, text in files.items():
                target = root / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(text)
            before = (root / "src/example.py").read_bytes()
            result = subprocess.run(  # noqa: S603 - fixed project command, owned fixture path
                ["pdm", "run", "mutation-gate", "--root", str(root), "--paths", "src/example.py"],  # noqa: S607
                cwd=ROOT,
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            report = [json.loads(line) for line in result.stdout.splitlines()]
            survivors = [row for row in report if row.get("outcome") == "SURVIVED"]
            self.assertTrue(survivors, result.stdout)
            self.assertEqual(survivors[0]["file"], "src/example.py")
            self.assertEqual(survivors[0]["line"], 2)
            self.assertEqual(survivors[0]["before"], "True")
            self.assertEqual(survivors[0]["after"], "False")
            self.assertEqual(survivors[0]["confirmation"], "unreached")
            self.assertEqual((root / "src/example.py").read_bytes(), before)


RENAME_BASE = (
    "import nci_si_mcp\nimport json\n\n\n"
    "def keep_a():\n    return 1\n\n\n"
    "def keep_b():\n    return 2\n"
)


def scope_between(base_files, head_files):
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()

        def run(*args):
            return subprocess.run(  # noqa: S603 - fixed git subcommands, owned temporary repository
                ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],  # noqa: S607
                cwd=root,
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()

        def write(files):
            for name, content in files.items():
                (root / name).parent.mkdir(parents=True, exist_ok=True)
                (root / name).write_bytes(content.encode())

        run("init", "-q")
        write(base_files)
        run("add", ".")
        run("commit", "-qm", "base")
        base = run("rev-parse", "HEAD")
        for name in base_files:
            (root / name).unlink()
        write(head_files)
        run("add", "-A")
        run("commit", "-qm", "head")
        return scope(root, [], base, "HEAD")


OLD_TABLE_PATH = "src/nci_si_mcp/x.py"
NEW_TABLE_PATH = "src/nci_sip_mcp/x.py"


def scope_after_git_rename(head_text, base_text=RENAME_BASE):
    return scope_between({OLD_TABLE_PATH: base_text}, {NEW_TABLE_PATH: head_text})


class RenamedFileScopeTest(unittest.TestCase):
    def test_renamed_file_is_selected_whole_whatever_changed_in_it(self):
        head = RENAME_BASE.replace("nci_si_mcp", "nci_sip_mcp").replace("return 2", "return 3")
        self.assertEqual(scope_after_git_rename(head), {NEW_TABLE_PATH: set(range(1, 11))})


class SpacedFileNameScopeTest(unittest.TestCase):
    def test_added_line_in_a_file_whose_name_contains_a_space_is_selected(self):
        base = "A = 1\nB = 2\n"
        self.assertEqual(
            scope_between({"src/x y.py": base}, {"src/x y.py": base + "C = 3\n"}),
            {"src/x y.py": {3}},
        )

    def test_renamed_file_with_a_space_in_its_name_is_selected_whole(self):
        old, new = "src/nci_si_mcp/a b.py", "src/nci_sip_mcp/a b.py"
        body = "".join(f"V{n} = {n}\n" for n in range(1, 21))
        base = "import nci_si_mcp\n" + body
        head = base.replace("nci_si_mcp", "nci_sip_mcp")
        self.assertEqual(scope_between({old: base}, {new: head}), {new: set(range(1, 22))})
        self.assertEqual(
            scope_between({old: base}, {new: head + "F = 6\n"}), {new: set(range(1, 23))}
        )


MODULE = "".join(f"def f{n}():\n    return {n}\n\n\n" for n in range(10))


class MovedCodeScopeTest(unittest.TestCase):
    def test_module_moved_to_an_unrelated_path_is_selected_whole(self):
        self.assertEqual(
            scope_between({"src/a.py": MODULE}, {"src/elsewhere/b.py": MODULE}),
            {"src/elsewhere/b.py": set(range(1, 41))},
        )

    def test_module_split_leaves_every_moved_line_selected(self):
        cut = len(MODULE) * 2 // 5
        result = scope_between(
            {"src/a.py": MODULE}, {"src/a.py": MODULE[:cut], "src/b.py": MODULE[cut:]}
        )
        self.assertEqual(result, {"src/a.py": set(), "src/b.py": set(range(1, 25))})

    def test_split_that_git_pairs_with_the_larger_part_selects_both_files_whole(self):
        cut = len(MODULE) * 3 // 5
        self.assertEqual(
            scope_between(
                {"src/a.py": MODULE}, {"src/b.py": MODULE[:cut], "src/c.py": MODULE[cut:]}
            ),
            {"src/b.py": set(range(1, 25)), "src/c.py": set(range(1, 17))},
        )


class RenameRobustnessScopeTest(unittest.TestCase):
    def test_move_from_an_eligible_to_an_ineligible_path_is_not_selected(self):
        self.assertEqual(
            scope_between({"src/a.py": MODULE}, {"src/tests/a.py": MODULE}),
            {},
        )


class HunkParsingTest(unittest.TestCase):
    def test_hunk_that_is_equal_before_and_after_is_selected(self):
        diff = "+++ b/src/example.py\n@@ -2 +2 @@\n-same\n+same\n"
        self.assertEqual(diff_lines(diff), {"src/example.py": {2}})

    def test_crlf_to_lf_change_in_an_eligible_module_is_selected(self):
        self.assertEqual(
            scope_between({"src/m.py": "a = 1\r\n"}, {"src/m.py": "a = 1\n"}),
            {"src/m.py": {1}},
        )

    def test_form_feed_in_a_changed_line_keeps_the_hunk_counts_and_the_next_hunk(self):
        diff = (
            "+++ b/src/example.py\n@@ -1 +1 @@\n-import nci_si_mcp\x0cx\n+import nci_sip_mcp\x0cx\n"
            "@@ -5 +5 @@\n-x\n+y\n"
        )
        self.assertEqual(diff_lines(diff), {"src/example.py": {1, 5}})

    def test_non_ascii_path_is_selected(self):
        self.assertEqual(
            scope_between({"src/café.py": "a = 1\n"}, {"src/café.py": "a = 2\n"}),
            {"src/café.py": {1}},
        )

    def test_renamed_file_whose_name_git_quotes_raises(self):
        with self.assertRaisesRegex(ValueError, "Hunk without a file name"):
            scope_between({"src/a\tb.py": MODULE}, {"src/c\td.py": MODULE})

    def test_hunk_without_a_file_name_raises(self):
        with self.assertRaisesRegex(ValueError, r"@@ -1 \+1 @@"):
            diff_lines("diff --git a/x b/x\n@@ -1 +1 @@\n-a\n+b\n")

    def test_malformed_hunk_header_raises(self):
        with self.assertRaisesRegex(ValueError, "@@ -x @@"):
            diff_lines("+++ b/src/example.py\n@@ -x @@\n")
