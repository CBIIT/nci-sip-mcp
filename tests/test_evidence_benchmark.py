"""Benchmarks cannot hide failed samples or compare unknown operating conditions."""

import hashlib
import json
import unittest
from pathlib import Path

from scripts.benchmark_records import summarize
from scripts.evidence_benchmark import comparable, project_benchmark
from scripts.evidence_envelope import EvidenceError

from fakes import selected_environment
from test_evidence_envelope import envelope


def benchmark():
    samples = [
        {
            "elapsedMs": 10.0,
            "resultBytes": 50,
            "outboundRequests": 1,
            "responseCode": "ok",
            "error": False,
            "release": {},
        },
        {
            "elapsedMs": 30.0,
            "resultBytes": 70,
            "outboundRequests": 2,
            "responseCode": "upstream_unavailable",
            "error": True,
            "release": {},
        },
    ]
    phase = {"summary": summarize(samples), "samples": samples}
    return {
        "mode": "fixture",
        "transport": "stdio",
        "createdAt": "2026-10-08T10:00:00Z",
        "serverVersion": "0.16.0",
        "sourceSha256": "a" * 64,
        "python": "3.14",
        "platform": "test",
        "suite": {
            "version": "1",
            "fixture_set": "test",
            "digest": "b" * 64,
        },
        "repetitions": 2,
        "expectedCases": ["get_concept"],
        "complete": True,
        "conditions": {"clientTimeoutSeconds": 300},
        "cases": [
            {
                "tool": "get_concept",
                "arguments": {"code": "C1"},
                "scenario": None,
                "cold": phase,
                "warm": phase,
            }
        ],
    }


def project(native=None, **changes):
    raw = json.dumps(benchmark() if native is None else native).encode()
    record = envelope(kind="benchmark", report_sha256=hashlib.sha256(raw).hexdigest(), **changes)
    return project_benchmark(json.dumps(record).encode(), raw)


def selection_manifest(native):
    fingerprint = project(native)["fingerprint"]
    return {
        "schema": 1,
        "cases": [
            {key: row[key] for key in ("tool", "arguments", "scenario")} for row in native["cases"]
        ],
        "fingerprint": {key: value or "a" * 64 for key, value in fingerprint.items()},
    }


def selected_projection(native=None, change_selection=None, selected_from=None, **changes):
    native = benchmark() if native is None else native
    manifest = selection_manifest(native if selected_from is None else selected_from)
    if change_selection:
        change_selection(manifest)
    selection = json.dumps(manifest).encode()
    raw = json.dumps(native).encode()
    record = envelope(
        kind="benchmark",
        report_sha256=hashlib.sha256(raw).hexdigest(),
        selection_sha256=hashlib.sha256(selection).hexdigest(),
        **changes,
    )
    return project_benchmark(json.dumps(record).encode(), raw, selection=selection)


class BenchmarkProjectionTest(unittest.TestCase):
    def test_recorded_environment_projects_without_inventing_historical_metadata(self):
        historical = benchmark() | {"mode": "live"}
        self.assertNotIn("environment", project(historical))
        environment = selected_environment()
        current = historical | {"environment": environment}
        failure = None
        projected = None
        try:
            projected = project(current)
        except EvidenceError as error:
            failure = str(error)
        self.assertIsNone(failure, "recorded live metadata must reach the evidence interface")
        self.assertEqual(projected["environment"], environment)
        self.assertFalse(projected["comparison_ready"])

    def test_different_recorded_policy_rejects_comparison_and_selection_substitution(self):
        original = benchmark() | {"mode": "live", "environment": selected_environment()}
        changed = original | {"environment": selected_environment() | {"policy_sha256": "b" * 64}}
        left, right = selected_projection(original), selected_projection(changed)
        for first, second in ((left, right), (right, left)):
            with self.subTest(first=first):
                allowed, reasons = comparable(first, second)
                self.assertFalse(allowed)
                self.assertIn("environment", reasons)
        self.assertNotEqual(left["fingerprint"]["environment"], right["fingerprint"]["environment"])
        with self.assertRaises(EvidenceError) as caught:
            selected_projection(changed, selected_from=original)
        self.assertEqual(
            str(caught.exception), "Invalid benchmark field: selection.fingerprint.environment"
        )

    def test_complete_and_interrupted_runs_cannot_be_compared_in_either_order(self):
        complete = selected_projection()
        interrupted = selected_projection(state="interrupted", exit_code=None)
        self.assertEqual(complete["fingerprint"], interrupted["fingerprint"])
        for left, right in ((complete, interrupted), (interrupted, complete)):
            with self.subTest(left=left["state"]):
                self.assertEqual(comparable(left, right), (False, ["incomplete"]))

    def test_nonfinite_exponent_in_unprojected_conditions_is_rejected(self):
        raw = (
            json.dumps(benchmark())
            .encode()
            .replace(b'"clientTimeoutSeconds": 300', b'"clientTimeoutSeconds": 1e400')
        )
        record = envelope(kind="benchmark", report_sha256=hashlib.sha256(raw).hexdigest())
        with self.assertRaises(EvidenceError):
            project_benchmark(json.dumps(record).encode(), raw)

    def test_partial_report_keeps_the_full_independent_selection_without_faking_completion(self):
        native = benchmark()
        native.update(cases=[], complete=False)
        result = selected_projection(native, selected_from=benchmark())
        self.assertTrue(result["selection_verified"])
        self.assertFalse(result["inventory_complete"])
        self.assertEqual(result["missing"], ["get_concept"])
        self.assertFalse(result["comparison_ready"])

    def test_committed_native_benchmarks_preserve_all_cases_without_promoting_provenance(self):
        directory = Path(__file__).resolve().parents[1] / "docs/evidence/phase-5"
        for mode in ("fixture", "live"):
            native = json.loads((directory / f"benchmark-{mode}.json").read_bytes())
            # Synthetic execution metadata is a test fixture, never a historical attestation.
            with self.subTest(mode=mode):
                result = project(native)
                self.assertEqual(len(result["cases"]), 10)
                self.assertFalse(result["selection_verified"])
                self.assertFalse(result["comparison_ready"])
                self.assertEqual(result["cases"][0]["cold"]["summary"]["calls"], 20)

    def test_older_report_without_inventory_requires_an_unverified_import_view(self):
        path = (
            Path(__file__).resolve().parents[1]
            / "docs/evidence/phase-5/benchmark-live-interrupted.json"
        )
        with self.assertRaises(EvidenceError):
            project(json.loads(path.read_bytes()), state="interrupted", exit_code=None)

    def test_extreme_integers_are_rejected_before_float_aggregation(self):
        for field in ("elapsedMs", "resultBytes", "outboundRequests"):
            native = benchmark()
            native["cases"][0]["cold"]["samples"][0][field] = 10**400
            with self.subTest(field=field), self.assertRaises(EvidenceError):
                project(native)

    def test_missing_report_is_not_displayed_as_a_zero_error_benchmark(self):
        record = envelope(kind="benchmark", state="failed", exit_code=1, report_sha256=None)
        result = project_benchmark(json.dumps(record).encode(), None)
        self.assertIsNone(result["cases"])
        self.assertIsNone(result["missing"])
        self.assertFalse(result["comparison_ready"])

    def test_safe_summary_counts_errors_and_preserves_sample_distribution(self):
        result = project()
        phase = result["cases"][0]["warm"]
        self.assertEqual(phase["summary"]["errorRate"], 0.5)
        self.assertEqual(phase["summary"]["p95Ms"], 30.0)
        self.assertEqual(phase["errors"], 1)
        self.assertEqual(phase["samples"], [10.0, 30.0])
        self.assertFalse(result["comparison_ready"])

    def test_same_report_is_not_comparable_when_deployment_identity_is_unknown(self):
        ready, reasons = comparable(project(), project())
        self.assertFalse(ready)
        self.assertIn("environment", reasons)
        self.assertIn("index", reasons)

    def test_changed_arguments_or_mode_block_comparison(self):
        native = benchmark()
        native["cases"][0]["arguments"]["code"] = "C2"
        native["mode"] = "live"
        ready, reasons = comparable(project(), project(native))
        self.assertFalse(ready)
        self.assertIn("workload", reasons)
        self.assertIn("mode", reasons)

    def test_raw_arguments_release_text_and_platform_labels_are_not_displayed(self):
        native = benchmark()
        native["platform"] = "<script>PRIVATE-CANARY</script>"
        native["cases"][0]["arguments"] = {"input": "PRIVATE-CANARY"}
        native["cases"][0]["cold"]["samples"][0]["release"] = {"label": "PRIVATE-CANARY"}
        self.assertNotIn("PRIVATE-CANARY", json.dumps(project(native)))

    def test_report_summaries_are_recomputed_and_contradictions_rejected(self):
        native = benchmark()
        native["cases"][0]["warm"]["summary"]["errorRate"] = 0
        with self.assertRaises(EvidenceError):
            project(native)

    def test_complete_flag_cannot_hide_missing_duplicate_or_unexpected_cases(self):
        for mutate in (
            lambda row: row.update(cases=[]),
            lambda row: row["cases"].append(row["cases"][0]),
            lambda row: row.update(expectedCases=["other"]),
            lambda row: row.update(expectedCases=["get_concept", "get_concept"]),
        ):
            native = benchmark()
            mutate(native)
            with self.subTest(native=native), self.assertRaises(EvidenceError):
                project(native)

    def test_partial_native_and_interrupted_process_remain_incomplete(self):
        native = benchmark()
        native.update(cases=[], complete=False)
        result = project(native, state="interrupted", exit_code=None)
        self.assertEqual(result["missing"], ["get_concept"])
        self.assertFalse(result["inventory_complete"])
        self.assertFalse(project(state="interrupted", exit_code=None)["inventory_complete"])

    def test_missing_telemetry_is_not_zero_inferred_from_a_remote_report(self):
        native = benchmark()
        native["transport"] = "streamable-http"
        with self.assertRaises(EvidenceError):
            project(native)

    def test_invalid_sample_values_never_produce_plausible_distributions(self):
        for field, value in (
            ("elapsedMs", -1),
            ("elapsedMs", float("inf")),
            ("elapsedMs", True),
            ("resultBytes", -1),
            ("outboundRequests", None),
            ("error", 1),
            ("responseCode", "unknown"),
            ("responseCode", "ok"),
        ):
            native = benchmark()
            native["cases"][0]["warm"]["samples"][1][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(EvidenceError):
                project(native)

    def test_repetitions_are_enforced_for_each_phase_not_only_summary_counts(self):
        native = benchmark()
        native["repetitions"] = 3
        with self.assertRaises(EvidenceError):
            project(native)

    def test_fully_known_identical_fingerprints_allow_comparison(self):
        left = selected_projection()
        self.assertTrue(left["comparison_ready"])
        self.assertEqual(comparable(left, left), (True, []))

    def test_incomplete_execution_blocks_even_matching_known_fingerprints(self):
        left = project(state="interrupted", exit_code=None)
        left["fingerprint"] = dict.fromkeys(left["fingerprint"], "a" * 64)
        ready, reasons = comparable(left, left)
        self.assertFalse(ready)
        self.assertIn("incomplete", reasons)

    def test_bound_selection_cannot_disagree_with_measured_arguments_or_conditions(self):
        for mutate in (
            lambda row: row["cases"][0]["arguments"].update(code="C2"),
            lambda row: row["fingerprint"].update(mode="0" * 64),
            lambda row: row["fingerprint"].update(environment="raw-secret"),
            lambda row: row.update(cases=[]),
        ):
            with self.subTest(mutate=mutate), self.assertRaises(EvidenceError):
                selected_projection(change_selection=mutate)

    def test_unknown_wrapper_dimension_still_blocks_comparison(self):
        result = selected_projection(
            change_selection=lambda row: row["fingerprint"].update(index=None)
        )
        self.assertEqual(comparable(result, result), (False, ["index"]))
