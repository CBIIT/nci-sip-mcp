"""Benchmark imports identify invalid fields without disclosing their values."""

import hashlib
import json
import unittest
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from scripts.benchmark import source_digest
from scripts.benchmark_report import Report
from scripts.evidence_benchmark import project_benchmark
from scripts.evidence_envelope import EvidenceError

from fakes import selected_environment
from test_evidence_benchmark import benchmark, project, selected_projection
from test_evidence_envelope import envelope
from test_evidence_http import http_report, project_http


class BenchmarkRecordTest(unittest.TestCase):
    def test_recorded_environment_roundtrips_and_binds_the_canonical_input(self):
        historical = benchmark() | {"mode": "live"}
        self.assertEqual(Report.from_dict(historical, "completed").to_dict(), historical)
        environment = selected_environment()
        current = historical | {"environment": environment}
        failure = None
        parsed = None
        try:
            parsed = Report.from_dict(current, "completed")
        except EvidenceError as error:
            failure = str(error)
        self.assertIsNone(failure, "a valid recorded live environment must be accepted")
        self.assertEqual(parsed.to_dict(), current)
        canonical = json.dumps(environment, sort_keys=True, separators=(",", ":")).encode()
        self.assertEqual(parsed.fingerprint().environment, hashlib.sha256(canonical).hexdigest())
        self.assertIsNone(Report.from_dict(historical, "completed").fingerprint().environment)

    def test_environment_presence_is_strict_and_does_not_relax_other_fields(self):
        environment = selected_environment()
        bad = [None, {}, environment | {"private-marker": "private-marker"}]
        bad += [
            environment | {key: value}
            for key, value in (
                ("name", None),
                ("policy_sha256", "private-marker"),
                ("origins", None),
                ("origins", environment["origins"] | {"evs": True}),
                ("origins", {"evs": "https://private-marker@example.test"}),
                (
                    "origins",
                    environment["origins"] | {"evs": "https://private-marker@example.test"},
                ),
            )
        ]
        inputs = [benchmark() | {"mode": "live", "environment": item} for item in bad]
        inputs += [benchmark() | {"environment": item} for item in (None, environment)]
        inputs += [http_report() | {"mode": "live", "environment": environment}]
        for native in inputs:
            with self.subTest(native=native):
                with self.assertRaises(EvidenceError) as caught:
                    Report.from_dict(native, "completed")
                self.assertEqual(str(caught.exception), "Invalid benchmark field: environment")
                self.assertNotIn("private-marker", str(caught.exception))
        for field in ("sourceSha256",):
            missing = benchmark() | {"mode": "live", "environment": environment}
            missing.pop(field)
            with self.assertRaises(EvidenceError) as caught:
                Report.from_dict(missing, "completed")
            self.assertEqual(str(caught.exception), f"Invalid benchmark field: {field}")
        with self.assertRaises(EvidenceError) as caught:
            Report.from_dict(benchmark() | {"private-marker": "secret"}, "completed")
        self.assertEqual(str(caught.exception), "Invalid benchmark field: report")

    def test_source_identity_covers_extracted_measurement_records(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "scripts").mkdir()
            runner = root / "scripts/benchmark.py"
            runner.write_text("runner")
            for name in ("benchmark_records.py", "benchmark_report.py"):
                (root / "scripts" / name).write_text("original")
            with (
                patch("scripts.benchmark.ROOT", root),
                patch("scripts.benchmark.__file__", str(runner)),
            ):
                original = source_digest()
                for name in ("benchmark_records.py", "benchmark_report.py"):
                    with self.subTest(name=name):
                        path = root / "scripts" / name
                        path.write_text("changed")
                        self.assertNotEqual(source_digest(), original)
                        path.write_text("original")

    def test_existing_wire_records_roundtrip_and_original_bytes_import(self):
        directory = Path(__file__).resolve().parents[1] / "docs/evidence/phase-5"
        for name in ("benchmark-fixture.json", "benchmark-live.json"):
            raw = (directory / name).read_bytes()
            metadata = envelope(kind="benchmark", report_sha256=hashlib.sha256(raw).hexdigest())
            with self.subTest(name=name):
                result = project_benchmark(json.dumps(metadata).encode(), raw)
                self.assertEqual(len(result["cases"]), 10)
                self.assertEqual(
                    Report.from_dict(json.loads(raw), "completed").to_dict(), json.loads(raw)
                )
        native = http_report()
        self.assertEqual(Report.from_dict(native, "completed").to_dict(), native)

    def test_each_known_fingerprint_dimension_rejects_a_bound_mismatch(self):
        native = http_report()
        expected = {
            "mode": '"fixture"',
            "transport": '"streamable-http"',
            "samples": "1",
            "warmup": "1",
            "concurrency": "1",
            "timeouts": '{"max_requests":20,"seconds":120,"timeout":15}',
        }
        measured = project_http(native)["fingerprint"]
        for name, serialized in expected.items():
            with self.subTest(name=name):
                self.assertEqual(measured[name], hashlib.sha256(serialized.encode()).hexdigest())
        for name in (
            "mode",
            "transport",
            "definitions",
            "workload",
            "samples",
            "warmup",
            "concurrency",
            "timeouts",
        ):
            manifest = {
                "schema": 1,
                "cases": [
                    {
                        "tool": "get_concept",
                        "arguments": {"code": "PRIVATE-CANARY"},
                        "scenario": None,
                    }
                ],
                "fingerprint": measured | {name: "0" * 64},
            }
            with self.subTest(name=name), self.assertRaises(EvidenceError) as caught:
                project_http(native, selection=json.dumps(manifest).encode())
            self.assertEqual(
                str(caught.exception), f"Invalid benchmark field: selection.fingerprint.{name}"
            )
            self.assertNotIn("PRIVATE-CANARY", str(caught.exception))

    def test_unknown_keys_and_missing_fields_name_only_the_owning_record(self):
        for field, remove, expected in (
            ("PRIVATE-CANARY", False, "cases[0].cold.samples[0]"),
            ("resultBytes", True, "cases[0].cold.samples[0].resultBytes"),
            ("release", False, "cases[0].cold.samples[0]"),
        ):
            report = http_report()
            sample = report["cases"][0]["cold"]["samples"][0]
            if remove:
                del sample[field]
            else:
                sample[field] = "PRIVATE-CANARY"
            with self.subTest(field=field), self.assertRaises(EvidenceError) as caught:
                project_http(report)
            self.assertEqual(str(caught.exception), f"Invalid benchmark field: {expected}")
            self.assertNotIn("PRIVATE-CANARY", str(caught.exception))

    def test_invalid_selection_names_the_fingerprint_dimension(self):
        with self.assertRaises(EvidenceError) as caught:
            selected_projection(
                change_selection=lambda manifest: manifest["fingerprint"].update(
                    mode="PRIVATE-CANARY"
                )
            )
        self.assertEqual(
            str(caught.exception), "Invalid benchmark field: selection.fingerprint.mode"
        )
        self.assertNotIn("PRIVATE-CANARY", str(caught.exception))

    def test_invalid_report_names_its_field_without_exposing_private_input(self):
        for build, import_report, path, value in (
            (benchmark, project, ("repetitions",), "PRIVATE-CANARY"),
            (benchmark, project, ("suite", "digest"), "PRIVATE-CANARY"),
            (http_report, project_http, ("conditions", "concurrency"), "PRIVATE-CANARY"),
            (http_report, project_http, ("limits", "timeout"), "PRIVATE-CANARY"),
            (http_report, project_http, ("state",), "PRIVATE-CANARY"),
            (http_report, project_http, ("targetSha256",), "PRIVATE-CANARY"),
        ):
            with self.subTest(transport=build()["transport"], path=path):
                report = deepcopy(build())
                parent = report
                for key in path[:-1]:
                    parent = parent[key]
                parent[path[-1]] = value
                with self.assertRaises(ValueError) as caught:
                    import_report(report)
                self.assertEqual(
                    str(caught.exception), f"Invalid benchmark field: {'.'.join(path)}"
                )
                self.assertNotIn("PRIVATE-CANARY", str(caught.exception))

    def test_invalid_summary_names_the_contradictory_field(self):
        for build, import_report in ((benchmark, project), (http_report, project_http)):
            with self.subTest(transport=build()["transport"]):
                report = deepcopy(build())
                report["cases"][0]["cold"]["summary"]["calls"] = "PRIVATE-CANARY"
                with self.assertRaises(EvidenceError) as caught:
                    import_report(report)
                self.assertEqual(
                    str(caught.exception), "Invalid benchmark field: cases[0].cold.summary.calls"
                )
                self.assertNotIn("PRIVATE-CANARY", str(caught.exception))

    def test_invalid_sample_names_its_field_without_exposing_the_value(self):
        for build, import_report, field in (
            (benchmark, project, "release"),
            (benchmark, project, "outboundRequests"),
            (http_report, project_http, "correlationId"),
            (http_report, project_http, "cacheState"),
            (http_report, project_http, "responseCode"),
        ):
            with self.subTest(transport=build()["transport"], field=field):
                report = deepcopy(build())
                report["cases"][0]["cold"]["samples"][0][field] = "PRIVATE-CANARY"
                with self.assertRaises(EvidenceError) as caught:
                    import_report(report)
                self.assertEqual(
                    str(caught.exception),
                    f"Invalid benchmark field: cases[0].cold.samples[0].{field}",
                )
                self.assertNotIn("PRIVATE-CANARY", str(caught.exception))
