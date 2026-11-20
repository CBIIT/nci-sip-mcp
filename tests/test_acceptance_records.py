"""Native acceptance records preserve old evidence and reject fields without disclosing values."""

import hashlib
import json
import subprocess
import sys
import unittest
from pathlib import Path

from scripts.evidence_acceptance import project_acceptance
from scripts.evidence_envelope import EvidenceError

from nci_sip_acceptance import report as report_module
from test_evidence_acceptance import bundle, encoded, project, report
from test_evidence_envelope import envelope


class AcceptanceRecordTest(unittest.TestCase):
    def test_tool_set_kind_and_missing_case_identity_are_checked(self):
        native = report()
        native["tools"] = {}
        self.assert_invalid(native, "tools")
        native = report()
        native["tools"]["PRIVATE-CANARY"] = native["tools"]["lookup"].copy()
        self.assert_invalid(native, "tools")
        with self.assertRaises(EvidenceError) as failure:
            project(kind="benchmark")
        self.assertEqual(str(failure.exception), "Invalid acceptance field: kind")
        native = report()
        name = next(iter(native["tests"]))
        del native["tests"][name]
        native["tools"]["lookup"].update(counts={}, outcome="NO TESTS")
        result = project(native)
        self.assertEqual(result["missing"], [hashlib.sha256(name.encode()).hexdigest()])
        self.assertNotIn(name, json.dumps(result))

    def test_alias_availability_and_native_numeric_guards(self):
        self.assertEqual(project()["tools"]["lookup"]["outcome"], "PASS")
        native = report()
        native["tools"]["lookup"]["implemented_as"] = None
        self.assertEqual(project(native)["tools"]["lookup"]["outcome"], "PASS")
        native["tools"]["lookup"]["outcome"] = "NOT IMPLEMENTED"
        self.assertEqual(project(native)["tools"]["lookup"]["outcome"], "NOT IMPLEMENTED")
        del native["tools"]["lookup"]["implemented_as"]
        self.assertEqual(project(native)["tools"]["lookup"]["outcome"], "NOT IMPLEMENTED")
        native["tools"]["lookup"].update(implemented_as="lookup", outcome="NOT IMPLEMENTED")
        self.assert_invalid(native, "tools[0].outcome")
        for gate in (1, "yes"):
            native = report()
            next(iter(native["tests"].values()))["gate"] = gate
            self.assert_invalid(native, "tests[0].gate")
            with self.assertRaises(ValueError) as failure:
                report_module.NativeReport.from_dict(native)
            self.assertEqual(str(failure.exception), "Invalid acceptance field: tests[0].gate")
            self.assertNotIn("PRIVATE-CANARY", str(failure.exception))
        self.assertEqual(
            report_module.NativeReport.from_dict(report()).tools["lookup"].counts, {"passed": 1}
        )
        native = report()
        native["tools"]["lookup"]["counts"] = {"passed": 0}
        with self.assertRaises(ValueError) as failure:
            report_module.NativeReport.from_dict(native)
        self.assertEqual(str(failure.exception), "Invalid acceptance field: tools[0].counts")
        self.assertNotIn("PRIVATE-CANARY", str(failure.exception))

    def test_malformed_report_names_report_without_echoing_json(self):
        raw = b'{"PRIVATE-CANARY":'
        context = bundle()
        metadata = envelope(report_sha256=hashlib.sha256(raw).hexdigest())
        metadata.update(
            {name + "_sha256": hashlib.sha256(value).hexdigest() for name, value in context.items()}
        )
        with self.assertRaises(EvidenceError) as failure:
            project_acceptance(encoded(metadata), raw, **context)
        self.assertEqual(str(failure.exception), "Invalid acceptance field: report")
        self.assertNotIn("PRIVATE-CANARY", str(failure.exception))

    def assert_invalid(self, native, path):
        with self.assertRaises(EvidenceError) as failure:
            project(native)
        self.assertEqual(str(failure.exception), f"Invalid acceptance field: {path}")
        self.assertNotIn("PRIVATE-CANARY", str(failure.exception))
        self.assertNotIn("test_example", str(failure.exception))

    def test_report_header_fields_are_checked_at_import(self):
        for name, value in (
            ("mode", "PRIVATE-CANARY"),
            ("transport", "PRIVATE-CANARY"),
            ("tools_list_bytes", True),
            ("tools_list_bytes", -1),
            ("tests", []),
            ("tools", []),
            ("failed_gates", ["PRIVATE-CANARY"]),
            ("unrun_gates", ["PRIVATE-CANARY"]),
        ):
            with self.subTest(name=name):
                self.assert_invalid(report() | {name: value}, name)
        self.assert_invalid(report() | {"PRIVATE-CANARY": True}, "report")
        self.assert_invalid([], "report")
        for name in report():
            native = report()
            del native[name]
            self.assert_invalid(native, name)

    def test_suite_case_and_tool_tables_name_safe_field_paths(self):
        cases = (
            ("suite", "version", None, "suite.version"),
            ("suite", "fixture_set", None, "suite.fixture_set"),
            ("suite", "digest", "PRIVATE-CANARY", "suite.digest"),
            ("suite", "PRIVATE-CANARY", True, "suite"),
            ("tests", "gate", 1, "tests[0].gate"),
            ("tests", "gate", True, "tests[0].gate"),
            ("tests", "tool", "PRIVATE-CANARY", "tests[0].tool"),
            ("tests", "outcome", "PRIVATE-CANARY", "tests[0].outcome"),
            ("tests", "outcome", [], "tests[0].outcome"),
            ("tests", "unmatched", None, "tests[0].unmatched"),
            ("tests", "unmatched", [1], "tests[0].unmatched"),
            ("tests", "PRIVATE-CANARY", True, "tests[0]"),
            ("tools", "group", "PRIVATE-CANARY", "tools[0].group"),
            ("tools", "outcome", "FAIL", "tools[0].outcome"),
            ("tools", "outcome", [], "tools[0].outcome"),
            ("tools", "counts", {"passed": True}, "tools[0].counts"),
            ("tools", "counts", {"passed": 0}, "tools[0].counts"),
            ("tools", "counts", {"passed": 2}, "tools[0].counts"),
            ("tools", "counts", [], "tools[0].counts"),
            ("tools", "gates_only", 0, "tools[0].gates_only"),
            ("tools", "gates_only", True, "tools[0].gates_only"),
            ("tools", "implemented_as", [], "tools[0].implemented_as"),
            ("tools", "PRIVATE-CANARY", True, "tools[0]"),
        )
        for group, name, value, path in cases:
            native = report()
            row = native[group] if group == "suite" else next(iter(native[group].values()))
            row[name] = value
            with self.subTest(path=path, value=value):
                self.assert_invalid(native, path)

    def test_existing_archives_roundtrip_without_changing_absent_fields(self):
        root = Path(__file__).resolve().parents[1]
        for path in sorted((root / "docs/evidence").rglob("acceptance-*.json")):
            original = path.read_bytes()
            native = json.loads(original)
            with self.subTest(file=path.name):
                self.assertEqual(report_module.NativeReport.from_dict(native).to_dict(), native)
                self.assertEqual(path.read_bytes(), original)

    def test_nested_records_require_each_declared_field(self):
        for group in ("suite", "tests", "tools"):
            base = report()
            row = base[group] if group == "suite" else next(iter(base[group].values()))
            owner = "suite" if group == "suite" else f"{group}[0]"
            for name in row:
                native = report()
                changed = native[group] if group == "suite" else next(iter(native[group].values()))
                del changed[name]
                with self.subTest(owner=owner, missing=name):
                    self.assert_invalid(native, f"{owner}.{name}")

    def test_nested_records_reject_non_objects(self):
        for group in ("suite", "tests", "tools"):
            for value in (None, []):
                native = report()
                if group == "suite":
                    native[group] = value
                else:
                    native[group][next(iter(native[group]))] = value
                self.assert_invalid(native, "suite" if group == "suite" else f"{group}[0]")

    def test_partial_completion_and_legacy_availability_are_preserved(self):
        self.assertTrue(project(report() | {"tools_list_bytes": 0})["inventory_complete"])
        for run in (None, {}, {"selected": "PRIVATE-CANARY"}):
            result = project(report() | {"run": run})
            self.assertFalse(result["inventory_complete"])
            self.assertEqual(result["completion_problem"], "invalid run completion metadata")
        run = {
            "exit_status": 0,
            "selected": 2,
            "finished": 2,
            "worker_crashes": 0,
            "extra": "PRIVATE-CANARY",
        }
        self.assertTrue(project(report() | {"run": run})["inventory_complete"])

    def test_acceptance_report_runs_with_scripts_imports_unavailable(self):
        code = """
import importlib.abc, sys
class BlockScripts(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'scripts' or fullname.startswith('scripts.'):
            raise ImportError('acceptance must run independently of scripts')
sys.meta_path.insert(0, BlockScripts())
from nci_sip_acceptance.report import Collector
assert Collector().report('fixture')['tests'] == {}
"""
        result = subprocess.run(  # noqa: S603 -- fixed interpreter and inline independence check
            [sys.executable, "-I", "-c", code], capture_output=True, text=True, check=False
        )
        self.assertEqual(result.returncode, 0, result.stderr)
