"""Recorded acceptance evidence stays tied to its original selected inventory."""

import hashlib
import json
import unittest
from copy import deepcopy

from scripts.evidence_acceptance import project_acceptance
from scripts.evidence_envelope import EvidenceError
from scripts.portal_views import run_page

from test_evidence_envelope import envelope

ENVIRONMENT = {
    "name": "evs-only",
    "origins": {
        "evs": "https://example.test/api",
        "evs-fhir": "https://example.test/fhir/r4",
        "cadsr": None,
        "cadsr-ftp": None,
        "cadsr-contracts": None,
        "ssis": None,
        "ssis-sparql": None,
    },
    "policy_sha256": "a" * 64,
}


def encoded(value):
    return json.dumps(value).encode()


def bundle():
    cases = {
        "tests/test_example.py::test_lookup": {"tool": "lookup", "gate": False},
        "tests/test_example.py::test_protocol": {"tool": None, "gate": True},
    }
    return {
        "catalogue": encoded(
            {
                "schema": 1,
                "suite_digest": "a" * 64,
                "tools": {"lookup": "evs"},
                "cases": cases,
            }
        ),
        "stories": encoded(dict.fromkeys(cases, "find-a-concept")),
        "expectations": encoded(dict.fromkeys(cases, "passed")),
        "selection": encoded(list(cases)),
    }


def report():
    return {
        "mode": "fixture",
        "transport": "stdio",
        "suite": {"version": "1.0", "fixture_set": "recorded", "digest": "a" * 64},
        "failed_gates": [],
        "unrun_gates": [],
        "tools_list_bytes": 100,
        "tools": {
            "lookup": {
                "group": "evs",
                "outcome": "PASS",
                "gates_only": False,
                "counts": {"passed": 1},
            }
        },
        "tests": {
            key: row | {"outcome": "passed", "unmatched": []}
            for key, row in json.loads(bundle()["catalogue"])["cases"].items()
        },
    }


def project(native=None, context=None, **changes):
    context = bundle() if context is None else context
    native = report() if native is None else native
    raw = encoded(native)
    metadata = envelope(report_sha256=hashlib.sha256(raw).hexdigest(), **changes)
    metadata.update(
        {name + "_sha256": hashlib.sha256(data).hexdigest() for name, data in context.items()}
    )
    return project_acceptance(encoded(metadata), raw, **context)


def missing_report():
    context = bundle()
    metadata = envelope(state="unavailable", exit_code=None, report_sha256=None)
    metadata.update(
        {name + "_sha256": hashlib.sha256(data).hexdigest() for name, data in context.items()}
    )
    return project_acceptance(encoded(metadata), None, **context)


class AcceptanceProjectionTest(unittest.TestCase):
    def test_acceptance_projection_preserves_only_recorded_live_provenance(self):
        historical = report() | {"mode": "live"}
        self.assertNotIn("environment", project(historical))
        current = project(historical | {"environment": deepcopy(ENVIRONMENT)})
        self.assertEqual(current.get("environment"), ENVIRONMENT)

    def test_portal_displays_recorded_fields_escaped_and_omits_historical_environment(self):
        environment = deepcopy(ENVIRONMENT)
        environment["origins"]["evs"] = "https://example.test/<script>private-marker</script>|edge"
        record = {"sequence": 1, "checksum": "a" * 64, "origin": "local-import-unverified"}
        historical = project(report() | {"mode": "live"})
        self.assertNotIn(
            "<caption>Live environment</caption>", run_page(record | {"evidence": historical})
        )
        current = project(report() | {"mode": "live", "environment": environment})
        displayed = run_page(record | {"evidence": current})
        expected = (
            '<caption>Live environment</caption><thead><tr><th scope="col">Field</th>'
            '<th scope="col">Recorded value</th></tr></thead><tbody>'
            "<tr><td>name</td><td>evs-only</td></tr>"
            "<tr><td>evs</td><td>https://example.test/&lt;script&gt;private-marker&lt;/script&gt;|edge</td></tr>"
            "<tr><td>evs-fhir</td><td>https://example.test/fhir/r4</td></tr>"
            "<tr><td>cadsr</td><td>disabled</td></tr>"
            "<tr><td>cadsr-ftp</td><td>disabled</td></tr>"
            "<tr><td>cadsr-contracts</td><td>disabled</td></tr>"
            "<tr><td>ssis</td><td>disabled</td></tr>"
            "<tr><td>ssis-sparql</td><td>disabled</td></tr>"
            f"<tr><td>policy_sha256</td><td>{'a' * 64}</td></tr></tbody>"
        )
        self.assertIn(expected, displayed)
        self.assertNotIn("<script>", displayed)
        self.assertIn("Recorded configuration; not access approval.", displayed)

    def test_additive_live_skip_evidence_preserves_the_strict_companion_projection(self):
        native = report()
        key = "tests/test_example.py::test_lookup"
        native["tests"][key]["outcome"] = "not_live"
        native["tools"]["lookup"].update(outcome="NOT RUN", counts={"not_live": 1})
        historical = project(native)
        native["tests"][key]["not_live_reason"] = "scenario"
        current = project(native)
        self.assertEqual(
            {key: current[key] for key in ("cases", "tools", "counts", "inventory_complete")},
            {key: historical[key] for key in ("cases", "tools", "counts", "inventory_complete")},
        )

    def test_unknown_report_fields_are_rejected(self):
        with self.assertRaises(EvidenceError):
            project(report() | {"extra": True})

    def test_missing_report_fields_are_validation_errors(self):
        native = report()
        del native["mode"]
        with self.assertRaises(EvidenceError):
            project(native)

    def test_a_list_of_report_field_names_is_not_a_report(self):
        with self.assertRaises(EvidenceError):
            project(list(report()))

    def test_a_fresh_report_with_completion_evidence_is_projected(self):
        native = report() | {
            "run": {"exit_status": 0, "selected": 2, "finished": 2, "worker_crashes": 0}
        }
        try:
            result = project(native)
        except EvidenceError as error:
            self.fail(f"Fresh report rejected by the projection: {error}")
        self.assertTrue(result["inventory_complete"])
        self.assertEqual(result["counts"], {"passed": 2})
        self.assertEqual(result["tools"]["lookup"]["outcome"], "PASS")

    def test_all_recorded_cases_do_not_override_a_crash_or_interrupted_status(self):
        for status, crashes in ((0, 1), (2, 0)):
            native = report() | {
                "run": {
                    "exit_status": status,
                    "selected": 2,
                    "finished": 2,
                    "worker_crashes": crashes,
                }
            }
            with self.subTest(status=status, crashes=crashes):
                result = project(native)
                self.assertFalse(result["inventory_complete"])
                self.assertEqual(result["missing"], [])
                self.assertEqual(result["counts"], {"passed": 2})

    def test_run_completion_does_not_override_the_bound_selection(self):
        native = report() | {
            "run": {"exit_status": 0, "selected": 1, "finished": 1, "worker_crashes": 0}
        }
        del native["tests"]["tests/test_example.py::test_protocol"]
        result = project(native)
        self.assertFalse(result["inventory_complete"])
        self.assertEqual(len(result["missing"]), 1)
        self.assertEqual(result["counts"], {"passed": 1})

    def test_a_report_without_the_alias_field_keeps_its_verdict(self):
        native = report()
        self.assertNotIn("implemented_as", native["tools"]["lookup"])
        self.assertEqual(project(native)["tools"]["lookup"]["outcome"], "PASS")
        native["tools"]["lookup"]["outcome"] = "FAIL"
        with self.assertRaises(EvidenceError):
            project(native)

    def test_a_not_implemented_verdict_needs_no_alias_field(self):
        native = report()
        native["tools"]["lookup"]["outcome"] = "NOT IMPLEMENTED"
        self.assertEqual(project(native)["tools"]["lookup"]["outcome"], "NOT IMPLEMENTED")

    def test_a_recorded_report_with_the_alias_field_stays_valid_and_checked(self):
        native = report()
        native["tools"]["lookup"]["implemented_as"] = "lookup"
        self.assertEqual(project(native)["tools"]["lookup"]["outcome"], "PASS")
        native["tools"]["lookup"].update(implemented_as=None, outcome="NOT IMPLEMENTED")
        self.assertEqual(project(native)["tools"]["lookup"]["outcome"], "NOT IMPLEMENTED")
        native["tools"]["lookup"].update(implemented_as="lookup", outcome="NOT IMPLEMENTED")
        with self.assertRaises(EvidenceError):
            project(native)

    def test_unrun_gate_and_test_remain_unrun_and_incomplete(self):
        native = report()
        key = "tests/test_example.py::test_protocol"
        native["tests"][key]["outcome"] = "skipped"
        native["unrun_gates"] = [key]
        result = project(native)
        self.assertEqual(result["counts"], {"passed": 1, "skipped": 1})
        self.assertTrue(result["inventory_complete"])
        self.assertEqual(result["tools"]["lookup"]["outcome"], "PASS")

    def test_an_unrun_gate_leaves_the_tool_incomplete_in_a_current_report(self):
        # Since #265 the harness keeps a tool from passing while a gate did not run; the PASS
        # of an older report, above, stays that run's verdict.
        native = report()
        key = "tests/test_example.py::test_protocol"
        native["tests"][key]["outcome"] = "skipped"
        native["unrun_gates"] = [key]
        native["tools"]["lookup"]["outcome"] = "INCOMPLETE"
        result = project(native)
        self.assertEqual(result["tools"]["lookup"]["outcome"], "INCOMPLETE")
        native["tools"]["lookup"]["outcome"] = "NOT RUN"
        with self.assertRaises(EvidenceError):
            project(native)

    def test_the_projection_says_when_unrun_gates_alone_decide_a_verdict(self):
        again = "tests/test_example.py::test_lookup_again"
        context = bundle()
        catalogue = json.loads(context["catalogue"])
        catalogue["cases"][again] = {"tool": "lookup", "gate": False}
        context |= {
            "catalogue": encoded(catalogue),
            "stories": encoded(dict.fromkeys(catalogue["cases"], "find-a-concept")),
            "expectations": encoded(dict.fromkeys(catalogue["cases"], "passed")),
            "selection": encoded(list(catalogue["cases"])),
        }
        gate = "tests/test_example.py::test_protocol"
        for own, gate_outcome, verdict, decided in (
            ("passed", "skipped", "INCOMPLETE", True),
            ("skipped", "skipped", "INCOMPLETE", False),
            ("passed", "not_live", "PASS", False),
            ("passed", "not_implemented", "INCOMPLETE", True),
        ):
            native = report()
            native["tests"][again] = {"tool": "lookup", "gate": False, "unmatched": []}
            native["tests"][again]["outcome"] = own
            native["tests"][gate]["outcome"] = gate_outcome
            native["unrun_gates"] = [gate]
            counts = {"passed": 2} if own == "passed" else {"passed": 1, "skipped": 1}
            native["tools"]["lookup"].update(outcome=verdict, counts=counts)
            with self.subTest(own=own, gate=gate_outcome):
                tool = project(native, context)["tools"]["lookup"]
                self.assertEqual((tool["outcome"], tool["gates_unrun"]), (verdict, decided))

    def test_another_tool_s_own_skipped_test_is_no_unrun_gate(self):
        other = "tests/test_example.py::test_search"
        context = bundle()
        catalogue = json.loads(context["catalogue"])
        catalogue["tools"]["search"] = "evs"
        catalogue["cases"][other] = {"tool": "search", "gate": False}
        context |= {
            "catalogue": encoded(catalogue),
            "stories": encoded(dict.fromkeys(catalogue["cases"], "find-a-concept")),
            "expectations": encoded(dict.fromkeys(catalogue["cases"], "passed")),
            "selection": encoded(list(catalogue["cases"])),
        }
        native = report()
        native["tests"][other] = {"tool": "search", "gate": False, "unmatched": []}
        native["tests"][other]["outcome"] = "skipped"
        native["tools"]["search"] = {
            "group": "evs",
            "outcome": "NOT RUN",
            "gates_only": False,
            "counts": {"skipped": 1},
        }
        tools = project(native, context)["tools"]
        self.assertEqual(
            {name: (tool["outcome"], tool["gates_unrun"]) for name, tool in tools.items()},
            {"lookup": ("PASS", False), "search": ("NOT RUN", False)},
        )

    def test_the_gate_lists_are_checked_in_order(self):
        names = [f"tests/test_example.py::test_gate_{letter}" for letter in "abcd"]
        context = bundle()
        catalogue = json.loads(context["catalogue"])
        catalogue["cases"] |= {name: {"tool": None, "gate": True} for name in names}
        context |= {
            "catalogue": encoded(catalogue),
            "stories": encoded(dict.fromkeys(catalogue["cases"], "find-a-concept")),
            "expectations": encoded(dict.fromkeys(catalogue["cases"], "passed")),
            "selection": encoded(list(catalogue["cases"])),
        }
        native = report()
        for name, outcome in zip(names, ("failed", "failed", "skipped", "skipped"), strict=True):
            native["tests"][name] = {
                "tool": None,
                "gate": True,
                "outcome": outcome,
                "unmatched": [],
            }
        native["tools"]["lookup"].update(outcome="FAIL", gates_only=True)
        native |= {"failed_gates": names[:2], "unrun_gates": names[2:]}
        self.assertEqual(project(native, context)["tools"]["lookup"]["outcome"], "FAIL")
        for field, listed in (("failed_gates", names[1::-1]), ("unrun_gates", names[:1:-1])):
            with self.subTest(unsorted=field), self.assertRaises(EvidenceError):
                project(native | {field: listed}, context)

    def test_an_incomplete_verdict_no_harness_gives_is_refused(self):
        native = report()
        native["tools"]["lookup"]["outcome"] = "INCOMPLETE"
        with self.assertRaises(EvidenceError):
            project(native)

    def test_malformed_verdict_is_a_safe_validation_error(self):
        native = report()
        native["tools"]["lookup"]["outcome"] = ["PRIVATE-CANARY"]
        with self.assertRaises(EvidenceError) as failure:
            project(native)
        self.assertNotIn("PRIVATE-CANARY", str(failure.exception))

    def test_projection_preserves_verdicts_and_original_story_and_expected_outcome(self):
        result = project()
        self.assertTrue(result["inventory_complete"])
        self.assertEqual(result["tools"]["lookup"]["outcome"], "PASS")
        self.assertEqual(result["counts"], {"passed": 2})
        self.assertEqual(result["missing"], [])
        self.assertTrue(all(row["story"] == "find-a-concept" for row in result["cases"]))
        self.assertTrue(all(row["expected"] == "passed" for row in result["cases"]))
        self.assertIsNone(result["server_commit"])

    def test_missing_cases_are_not_inferred_passed_from_successful_process_exit(self):
        native = report()
        del native["tests"]["tests/test_example.py::test_protocol"]
        result = project(native)
        self.assertFalse(result["inventory_complete"])
        self.assertEqual(len(result["missing"]), 1)
        self.assertEqual(result["counts"], {"passed": 1})

    def test_interrupted_run_cannot_become_complete_even_with_all_results(self):
        result = project(state="interrupted", exit_code=None)
        self.assertFalse(result["inventory_complete"])
        self.assertEqual(result["state"], "interrupted")

    def test_selection_is_exact_and_can_be_a_subset_of_original_catalogue(self):
        context = bundle()
        context["selection"] = encoded(["tests/test_example.py::test_lookup"])
        native = report()
        del native["tests"]["tests/test_example.py::test_protocol"]
        self.assertTrue(project(native, context)["inventory_complete"])
        with self.assertRaises(EvidenceError):
            project(context=context)

    def test_raw_urls_labels_and_unmatched_credentials_never_reach_projection(self):
        native = report()
        native["suite"]["version"] = "<script>SECRET-CANARY</script>"
        native["tests"]["tests/test_example.py::test_lookup"]["unmatched"] = [
            "https://secret.invalid/?credential=SECRET-CANARY"
        ]
        result = project(native)
        self.assertNotIn("SECRET-CANARY", json.dumps(result))
        self.assertNotIn("test_example.py", json.dumps(result))

    def test_report_cannot_reassign_a_gate_or_tool(self):
        for field, value in (("gate", False), ("tool", "lookup")):
            native = report()
            native["tests"]["tests/test_example.py::test_protocol"][field] = value
            with self.subTest(field=field), self.assertRaises(EvidenceError):
                project(native)

    def test_counts_gates_and_verdicts_must_agree_with_individual_results(self):
        for field, value in (
            ("counts", {"passed": 2}),
            ("outcome", "FAIL"),
            ("gates_only", True),
            ("group", "cadsr"),
        ):
            native = report()
            native["tools"]["lookup"][field] = value
            with self.subTest(field=field), self.assertRaises(EvidenceError):
                project(native)

    def test_gate_failure_uses_harness_verdict_without_reinterpreting_no_fixture(self):
        for outcome in ("failed", "no_fixture"):
            native = report()
            key = "tests/test_example.py::test_protocol"
            native["tests"][key]["outcome"] = outcome
            native["failed_gates"] = [key]
            native["tools"]["lookup"].update(outcome="FAIL", gates_only=True)
            with self.subTest(outcome=outcome):
                result = project(native, state="failed", exit_code=1)
                self.assertEqual(result["tools"]["lookup"]["outcome"], "FAIL")
                self.assertEqual(result["counts"][outcome], 1)

    def test_original_suite_must_match_catalogue_not_current_checkout(self):
        native = report()
        native["suite"]["digest"] = "b" * 64
        with self.assertRaises(EvidenceError):
            project(native)

    def test_replacing_any_context_bytes_breaks_binding(self):
        context = bundle()
        raw = encoded(report())
        metadata = envelope(report_sha256=hashlib.sha256(raw).hexdigest())
        metadata.update(
            {name + "_sha256": hashlib.sha256(data).hexdigest() for name, data in context.items()}
        )
        for name in context:
            changed = context | {name: context[name] + b" "}
            with self.subTest(name=name), self.assertRaises(EvidenceError):
                project_acceptance(encoded(metadata), raw, **changed)

    def test_missing_report_preserves_execution_without_inventing_test_counts(self):
        result = missing_report()
        self.assertIsNone(result["counts"])
        self.assertFalse(result["inventory_complete"])
        self.assertEqual(len(result["missing"]), 2)

    def test_malformed_context_cannot_create_a_different_inventory(self):
        for name, data in (
            ("selection", ["unknown"]),
            ("selection", ["tests/test_example.py::test_lookup"] * 2),
            ("stories", {}),
            ("expectations", {}),
            ("catalogue", []),
        ):
            with self.subTest(name=name), self.assertRaises(EvidenceError):
                project(context=bundle() | {name: encoded(data)})

    def test_native_schema_rejects_malformed_types_and_unknown_statuses(self):
        for field, value in (
            ("mode", "other"),
            ("transport", "file"),
            ("tools_list_bytes", True),
            ("tests", []),
            ("failed_gates", ["invented"]),
        ):
            native = report() | {field: value}
            with self.subTest(field=field), self.assertRaises(EvidenceError):
                project(native)

    def test_unknown_case_fields_and_boolean_counts_fail_closed(self):
        originals = []
        native = report()
        native["tools"]["lookup"]["counts"] = {"passed": True}
        originals.append(native)
        native = report()
        native["tests"]["tests/test_example.py::test_lookup"]["unexpected"] = "canary"
        originals.append(native)
        for native in originals:
            with self.subTest(native=native), self.assertRaises(EvidenceError):
                project(deepcopy(native))
