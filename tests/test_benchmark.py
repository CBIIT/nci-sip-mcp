import hashlib
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager, redirect_stdout
from http.client import HTTPConnection
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlsplit

import yaml
from scripts import benchmark
from scripts.benchmark_records import percentile, summarize

from nci_si_acceptance import client, denied_surfaces, environments
from nci_si_acceptance.fixture_server import FixtureServer, load_fixtures


def sample(elapsed=1.0, *, error=False, requests=2, size=9):
    return {
        "elapsedMs": elapsed,
        "error": error,
        "outboundRequests": requests,
        "resultBytes": size,
        "responseCode": "upstream_unavailable" if error else "ok",
        "release": {},
    }


def local_platform_query(command):
    """Allow only stdlib OS queries, normalizing symlinks to this interpreter."""
    match command:
        case ["uname", "-p"]:
            return True
        case ["file", "-b", target]:
            return Path(target).resolve() == Path(sys.executable).resolve()
    return False


def local_policy_inputs(selected, forbidden, directory):
    """Write independent seven-surface test inputs; never substitute selection or output."""
    names = (
        "evs",
        "evs-fhir",
        "cadsr",
        "cadsr-ftp",
        "cadsr-contracts",
        "ssis",
        "ssis-sparql",
    )
    origins = dict.fromkeys(names)
    origins.update({"evs": selected.base_url("evs"), "evs-fhir": selected.base_url("evs-fhir")})
    policy = {
        "allowed": {name: origins[name] or forbidden.base_url(name) for name in names},
        "environments": {"local-measurement": origins},
    }
    raw_policy = yaml.safe_dump(policy).encode()
    path = Path(directory) / "policy.yaml"
    path.write_bytes(raw_policy)
    expected = {
        "name": "local-measurement",
        "origins": origins,
        "policy_sha256": hashlib.sha256(raw_policy).hexdigest(),
    }
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("NCI_SI_") and not environments.external_runtime_setting(key)
    }
    env |= {
        "NCI_SI_ACCEPTANCE_MODE": "fixture",
        "NCI_SI_ACCEPTANCE_ENVIRONMENT": "local-measurement",
    }
    return path, expected, env


class BenchmarkTest(unittest.TestCase):
    def test_live_selection_and_prepare_fail_before_process_or_report(self):
        variable = "NCI_SI_ACCEPTANCE_ENVIRONMENT"
        cases = [
            ({}, f"{variable} is required for live mode"),
            ({variable: ""}, f"{variable} is required for live mode"),
            ({variable: "unreviewed"}, f"{variable} does not name a reviewed environment"),
            (
                {variable: "evs-only", "NCI_SI_ACCEPTANCE_PREPARE": "nci-si-mcp index-sample"},
                "NCI_SI_ACCEPTANCE_PREPARE is not supported by the index-free benchmark",
            ),
            (
                {variable: "evs-only", "NCI_SI_ACCEPTANCE_URL": "http://private-marker.test/mcp"},
                "NCI_SI_ACCEPTANCE_URL is incompatible with the selected live environment",
            ),
        ]
        for settings, expected in cases:
            for existing in (False, True):
                with (
                    self.subTest(settings=settings, existing=existing),
                    tempfile.TemporaryDirectory() as d,
                ):
                    output = Path(d) / "report.json"
                    original = b"preserved prior report bytes"
                    if existing:
                        output.write_bytes(original)
                    reached = []

                    def forbidden_child(*args, reached=reached, **kwargs):
                        reached.append("child")
                        raise AssertionError("preflight launched a child")

                    with (
                        patch.dict(os.environ, settings, clear=True),
                        patch("subprocess.Popen", forbidden_child),
                        self.assertRaises(ValueError) as caught,
                    ):
                        benchmark.run("live", ["get_concept"], 1, output)
                    self.assertEqual(str(caught.exception), expected)
                    self.assertNotIn("private-marker", str(caught.exception))
                    self.assertEqual(reached, [])
                    self.assertEqual(
                        output.read_bytes() if output.exists() else None,
                        original if existing else None,
                    )

    def test_explicit_target_mode_does_not_mutate_ambient_or_change_default_callers(self):
        settings = {"NCI_SI_ACCEPTANCE_MODE": "live"}
        with patch.dict(os.environ, settings, clear=True):
            failure = None
            selected = None
            try:
                selected = client.Target.from_env(mode_override="fixture")
            except TypeError as error:
                failure = str(error)
            self.assertIsNone(failure, "explicit mode must be supported without global mutation")
            self.assertEqual(selected.mode, "fixture")
            self.assertEqual(os.environ["NCI_SI_ACCEPTANCE_MODE"], "live")
            with self.assertRaisesRegex(ValueError, "is required for live mode"):
                client.Target.from_env()
        with patch.dict(os.environ, {"NCI_SI_ACCEPTANCE_MODE": "fixture"}, clear=True):
            self.assertEqual(client.Target.from_env().mode, "fixture")

    def test_fixture_producer_ignores_acceptance_only_target_selectors(self):
        settings = {
            "NCI_SI_ACCEPTANCE_MODE": "live",
            "NCI_SI_ACCEPTANCE_ENVIRONMENT": "unreviewed",
            "NCI_SI_ACCEPTANCE_SERVER": "must-not-execute",
            "NCI_SI_ACCEPTANCE_PROFILE": "evs",
            "NCI_SI_ACCEPTANCE_PREPARE": "must-not-prepare",
            "NCI_SI_ACCEPTANCE_URL": "http://must-not-contact.test/mcp",
        }
        with patch.dict(os.environ, settings), tempfile.TemporaryDirectory() as d:
            output = Path(d) / "report.json"
            report = benchmark.run("fixture", ["match_data_elements"], 1, output)
            self.assertEqual(json.loads(output.read_text()), report)
            self.assertEqual({key: os.environ[key] for key in settings}, settings)
        self.assertEqual(
            (report["mode"], report["transport"], report["complete"]), ("fixture", "stdio", True)
        )
        self.assertNotIn("environment", report)
        self.assertEqual(
            report["conditions"]["index"],
            "none; lexical search; no semantic-quality or production latency claim",
        )
        for phase in ("cold", "warm"):
            self.assertEqual(report["cases"][0][phase]["summary"]["errorRate"], 0)

    def test_real_local_policy_producer_persists_selected_evs_and_fhir_measurements(self):
        real_process = subprocess.Popen
        launches = []
        listeners = []
        refusals = []
        enter = denied_surfaces.DeniedSurfaces.__enter__
        handle = denied_surfaces._Refusal.handle

        def observed_listener(listener):
            url = enter(listener)
            listeners.append((listener, url))
            return url

        def observed_refusal(request):
            handle(request)
            refusals.append("refused")

        def local_child(command, *args, **kwargs):
            # stdlib platform metadata queries are local; no other child bypasses the guard.
            if local_platform_query(command):
                return real_process(command, *args, **kwargs)
            env = kwargs.get("env") or {}
            self.assertEqual(len(listeners), 1)
            self.assertEqual(
                {
                    key: value
                    for key, value in env.items()
                    if key.startswith("NCI_SI_")
                    and key.endswith(("_BASE_URL", "_FTP_URL", "_SPARQL_URL"))
                },
                {
                    "NCI_SI_EVS_BASE_URL": expected["origins"]["evs"],
                    "NCI_SI_EVS_FHIR_BASE_URL": expected["origins"]["evs-fhir"],
                    "NCI_SI_CADSR_BASE_URL": listeners[0][1],
                    "NCI_SI_CADSR_FTP_URL": listeners[0][1],
                    "NCI_SI_SSIS_SPARQL_URL": listeners[0][1],
                },
            )
            self.assertEqual(
                list(command[:5]),
                [sys.executable, "-I", "-m", "nci_si_acceptance.live_launcher", "serve"],
            )
            launches.append(dict(env))
            return real_process(command, *args, **kwargs)

        with (
            tempfile.TemporaryDirectory() as d,
            FixtureServer(load_fixtures(benchmark.FIXTURES)) as selected,
            FixtureServer(load_fixtures(benchmark.FIXTURES)) as forbidden,
        ):
            path, expected, env = local_policy_inputs(selected, forbidden, d)
            output = Path(d) / "report.json"
            failure = None
            report = None
            with (
                patch.dict(os.environ, env, clear=True),
                patch.object(environments, "POLICY", path),
                patch("subprocess.Popen", local_child),
                patch.object(denied_surfaces.DeniedSurfaces, "__enter__", observed_listener),
                patch.object(denied_surfaces._Refusal, "handle", observed_refusal),
            ):
                try:
                    report = benchmark.run(
                        "live", ["get_concept", "expand_value_set", "get_data_element"], 1, output
                    )
                except ValueError as error:
                    failure = str(error)
            self.assertIsNone(failure, "the real selected-policy benchmark must complete")
            self.assertEqual(json.loads(output.read_text()), report)
            self.assertEqual(report["environment"], expected)
            self.assertEqual(
                (report["mode"], report["transport"], report["complete"]), ("live", "stdio", True)
            )
            self.assertEqual(
                report["expectedCases"], ["get_concept", "expand_value_set", "get_data_element"]
            )
            self.assertEqual(
                [row["tool"] for row in report["cases"]],
                ["get_concept", "expand_value_set", "get_data_element"],
            )
            self.assert_producer_phases(report["cases"])
            self.assertEqual(len(launches), 6)
            surfaces = {row["surface"] for row in selected.log()}
            self.assertEqual(surfaces, {"evs", "evs-fhir"})
            self.assertEqual(forbidden.log(), [])
            self.assertEqual(
                len(selected.log()), 6
            )  # cold, excluded prime, warm for each allowed case
            self.assertEqual(refusals, ["refused"] * 3)
            self.assertEqual(len(listeners), 1)
            listener, url = listeners[0]
            self.assertFalse(listener.thread.is_alive())
            with socket.socket() as probe:
                probe.settimeout(2)
                self.assertNotEqual(probe.connect_ex(("127.0.0.1", urlsplit(url).port)), 0)
            self.assertTrue(all(row["NCI_SI_UPSTREAM_MODE"] == "live" for row in launches))

    def assert_producer_phases(self, cases):
        rates = {"get_concept": 0, "expand_value_set": 0, "get_data_element": 1}
        codes = {
            "get_concept": "ok",
            "expand_value_set": "ok",
            "get_data_element": "upstream_unavailable",
        }
        for row in cases:
            for phase in ("cold", "warm"):
                self.assertEqual(len(row[phase]["samples"]), 1)
                self.assertEqual(row[phase]["summary"]["errorRate"], rates[row["tool"]])
                self.assertGreater(row[phase]["summary"]["meanResultBytes"], 0)
                self.assertEqual(row[phase]["samples"][0]["outboundRequests"], 1)
                self.assertEqual(row[phase]["summary"]["meanOutboundRequests"], 1)
                self.assertEqual(row[phase]["samples"][0]["responseCode"], codes[row["tool"]])

    def test_an_interrupted_first_case_cannot_leave_a_stale_successful_report(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "benchmark.json"
            output.write_text('{"complete": true}')
            with (
                patch.object(benchmark, "run_case", side_effect=ValueError("missing audit")),
                self.assertRaisesRegex(ValueError, "missing audit"),
            ):
                benchmark.run("fixture", ["get_form"], 1, output)
            report = json.loads(output.read_text())
        self.assertFalse(report["complete"])
        self.assertEqual(report["cases"], [])
        self.assertEqual(report["expectedCases"], ["get_form"])

    def test_a_representative_call_produces_successful_correlated_fixture_evidence(self):
        # match_data_elements needs its fixture scenario (cadsr/credentialed) activated.
        names = ["get_concept", "match_data_elements"]
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "benchmark.json"
            report = benchmark.run("fixture", names, 1, output)
            self.assertEqual(json.loads(output.read_text()), report)
        self.assertTrue(report["complete"])
        self.assertEqual(report["expectedCases"], names)
        self.assertEqual([case["tool"] for case in report["cases"]], names)
        for case in report["cases"]:
            with self.subTest(tool=case["tool"]):
                self.assertEqual(case["cold"]["summary"]["errorRate"], 0)
                self.assertEqual(case["warm"]["summary"]["errorRate"], 0)
                self.assertEqual(len(case["cold"]["samples"]), 1)
                self.assertEqual(len(case["warm"]["samples"]), 1)
                self.assertGreater(case["warm"]["summary"]["meanResultBytes"], 0)
                self.assertGreater(case["cold"]["summary"]["meanOutboundRequests"], 0)

    def test_nearest_rank_and_error_calls_remain_in_the_distribution(self):
        maximum = 20
        rows = [
            sample(float(value), error=value == maximum, requests=value) for value in range(1, 21)
        ]
        result = summarize(rows)
        self.assertEqual((result["p50Ms"], result["p95Ms"]), (10, 19))
        self.assertEqual(result["errorRate"], 0.05)
        self.assertEqual(result["meanOutboundRequests"], 10.5)
        self.assertEqual(result["meanResultBytes"], 9)
        self.assertEqual(result["calls"], 20)

    def test_invalid_timing_evidence_is_refused(self):
        for values, fraction in (([], 0.5), ([1], 0), ([1], 1.1), ([float("nan")], 0.5), ([-1], 1)):
            with self.subTest(values=values), self.assertRaises(ValueError):
                percentile(values, fraction)

    def test_only_one_correlated_completion_with_actual_attempts_is_evidence(self):
        record = {
            "event": "call_completed",
            "correlationId": "call",
            "tool": "get_concept",
            "outboundRequests": 3,
        }
        line = json.dumps(record)
        other = json.dumps(record | {"correlationId": "another", "outboundRequests": 99})
        self.assertEqual(
            benchmark.completion(other + "\n" + line, "call", "get_concept")["outboundRequests"], 3
        )
        bad = ["", line + "\n" + line, json.dumps(record | {"tool": "another"})]
        bad += [json.dumps(record | {"outboundRequests": value}) for value in (None, True, -1)]
        for log in bad:
            with self.subTest(log=log), self.assertRaises(ValueError):
                benchmark.completion(log, "call", "get_concept")

    def test_measure_uses_utf8_result_and_correlated_attempts_including_error(self):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "audit.jsonl"
            content = {"error": {"code": "upstream_unavailable", "message": "é"}}

            def call_tool(name, _arguments, *, meta):
                record = {
                    "event": "call_completed",
                    "correlationId": meta["correlationId"],
                    "tool": name,
                    "outboundRequests": 3,
                    "status": "error",
                    "responseCode": "upstream_unavailable",
                    "release": {"requested": "26.09d", "resolved": []},
                }
                log.write_text(json.dumps(record) + "\n")
                return SimpleNamespace(structured_content=content, is_error=True)

            measured = benchmark.measure(
                SimpleNamespace(call_tool=call_tool), log, {"tool": "get_concept", "arguments": {}}
            )
        self.assertTrue(measured["error"])
        self.assertEqual(measured["outboundRequests"], 3)
        self.assertEqual(measured["release"]["requested"], "26.09d")
        expected = b'{"error":{"code":"upstream_unavailable","message":"\xc3\xa9"}}'
        self.assertEqual(measured["resultBytes"], len(expected))

    def test_cold_processes_are_distinct_and_warm_priming_is_excluded(self):
        processes = []

        @contextmanager
        def opened(_environment):
            process = {"id": len(processes), "calls": 0}
            processes.append(process)
            yield process, None

        def measure(process, _log, _case):
            process["calls"] += 1
            return sample(100 if process["calls"] == 1 else 5, requests=process["calls"])

        with patch.object(benchmark, "opened", opened), patch.object(benchmark, "measure", measure):
            result = benchmark.run_case(
                {"tool": "get_concept", "arguments": {}, "scenario": None}, {}, 2
            )
        self.assertEqual([process["calls"] for process in processes], [1, 1, 3])
        self.assertEqual(result["cold"]["summary"]["p50Ms"], 100)
        self.assertEqual(result["warm"]["summary"]["p95Ms"], 5)
        self.assertEqual(result["warm"]["summary"]["meanOutboundRequests"], 2.5)

    def test_missing_content_or_disagreeing_mcp_error_does_not_become_a_measurement(self):
        replies = [
            SimpleNamespace(structured_content=None, is_error=True),
            SimpleNamespace(structured_content={"answer": []}, is_error=False),
        ]
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "audit.jsonl"
            log.write_text("")
            for reply in replies:
                with (
                    self.subTest(reply=reply),
                    patch.object(benchmark, "completion", return_value={"status": "error"}),
                    self.assertRaisesRegex(ValueError, "disagree"),
                ):
                    session = SimpleNamespace(
                        call_tool=lambda *_args, reply=reply, **_kwargs: reply
                    )
                    benchmark.measure(session, log, {"tool": "get_concept", "arguments": {}})


class BenchmarkCommandTest(unittest.TestCase):
    def test_nonpositive_repetitions_are_rejected_without_creating_measurements(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "report.json"
            for repetitions in ("0", "-1"):
                with (
                    self.subTest(repetitions=repetitions),
                    patch(
                        "sys.argv",
                        ["benchmark", "--output", str(output), "--repetitions", repetitions],
                    ),
                    redirect_stdout(io.StringIO()),
                    self.assertRaisesRegex(ValueError, "At least one measured repetition"),
                ):
                    benchmark.main()
                self.assertFalse(output.exists())


class CaseIsolationTest(unittest.TestCase):
    def test_a_case_starts_with_the_log_of_no_earlier_request(self):
        case = benchmark.cases(["get_concept"])[0]
        with FixtureServer(load_fixtures(benchmark.FIXTURES)) as upstream:
            connection = HTTPConnection(urlsplit(upstream.url).netloc, timeout=3)
            self.addCleanup(connection.close)
            connection.request("GET", "/left-by-the-previous-case")
            connection.getresponse().read()
            self.assertEqual(len(upstream.log()), 1)
            benchmark.environment_for("fixture", upstream, case)
            self.assertEqual(upstream.log(), [])


class BenchmarkBoundaryTest(unittest.TestCase):
    def test_unrecorded_fixture_response_cannot_be_published_as_complete_evidence(self):
        case = benchmark.cases(["get_concept"])[0]
        case["arguments"]["code"] = "C999999999"
        with TemporaryDirectory() as directory:
            output = Path(directory) / "benchmark.json"
            with (
                patch.object(benchmark, "cases", return_value=[case]),
                self.assertRaisesRegex(ValueError, "unrecorded fixture response"),
            ):
                benchmark.run("fixture", ["get_concept"], 1, output)
            self.assertFalse(json.loads(output.read_text())["complete"])

    def test_benchmark_session_uses_its_own_timeout_instead_of_the_harness_default(self):
        with (
            TemporaryDirectory() as directory,
            patch.object(client, "READ_TIMEOUT_SECONDS", 0.000001),
        ):
            report = benchmark.run("fixture", ["get_concept"], 1, Path(directory) / "report.json")
        self.assertTrue(report["complete"])
        result = report["cases"][0]
        self.assertEqual(result["cold"]["summary"]["errorRate"], 0)
        self.assertEqual(result["warm"]["summary"]["errorRate"], 0)
