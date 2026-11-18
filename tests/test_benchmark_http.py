"""The HTTP benchmark measures the actual MCP transport and retains incomplete attempts."""

import asyncio
import hashlib
import json
import socket
import sys
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import uvicorn
from scripts.benchmark_http import run_http
from scripts.benchmark_limits import Limits, RunStoppedError
from scripts.evidence_benchmark import project_benchmark
from scripts.http_measurement import MeasurementError

from nci_sip_mcp.transport import create_http_app
from test_evidence_envelope import envelope
from test_server import ServerFixture, pinned


@asynccontextmanager
async def serving(settings, context):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        app = create_http_app(
            replace(settings, http_allowed_hosts=(f"127.0.0.1:{port}",)), context=context
        )
        server = uvicorn.Server(uvicorn.Config(app, log_config=None, access_log=False))
        task = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            async with asyncio.timeout(5):
                while not server.started:  # noqa: ASYNC110 - Uvicorn exposes a flag, not an event
                    await asyncio.sleep(0.01)
            yield f"http://127.0.0.1:{port}/mcp"
        finally:
            server.should_exit = True
            await asyncio.wait_for(task, timeout=5)


class HTTPBenchmarkTest(ServerFixture):
    def setUp(self):
        super().setUp()
        self.output_directory = TemporaryDirectory()
        self.addCleanup(self.output_directory.cleanup)
        self.output = Path(self.output_directory.name) / "report.json"
        self.case = {"tool": "get_concept", "arguments": pinned(code="C3262"), "scenario": None}

    def test_symlink_output_cannot_replace_another_file(self):
        original = self.output.with_name("original.json")
        original.write_text("keep")
        self.output.symlink_to(original)
        url = "http://127.0.0.1:8000/mcp"
        with self.assertRaisesRegex(ValueError, "symlink"):
            asyncio.run(run_http(url, (url,), [self.case], Limits(), self.output, fixture=True))
        self.assertEqual(original.read_text(), "keep")

    def test_invalid_protocol_measurement_is_retained_as_failure_without_response_content(self):
        async def scenario():
            async with serving(self.settings, self.context) as url:
                return await run_http(url, (url,), [self.case], Limits(), self.output, fixture=True)

        with patch("scripts.benchmark_http.measure_reply", side_effect=MeasurementError("private")):
            report = asyncio.run(scenario())
        self.assertEqual(report["stopReason"], "invalid_response")
        self.assertEqual(report["cases"][0]["cold"]["summary"]["errorRate"], 1)
        self.assertNotIn("private", self.output.read_text())

    def test_connection_refusal_is_failed_evidence_not_an_empty_success(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
            # Bound but not listening: no other service can claim this port during the probe.
            # Linux refuses the connection at once; macOS leaves the SYN unanswered until the
            # kernel gives up after about 7.8 s. The one-second timeout bounds both; the report
            # names the refusal "transport_error" and the unanswered connect "timeout".
            url = f"http://127.0.0.1:{port}/mcp"
            report = asyncio.run(
                run_http(url, (url,), [self.case], Limits(timeout=1), self.output, fixture=True)
            )
        expected = "timeout" if sys.platform == "darwin" else "transport_error"
        self.assertEqual(report["stopReason"], expected)
        self.assertFalse(report["complete"])
        self.assertEqual(report["cases"][0]["cold"]["samples"], [])

    def test_invalid_case_selection_fails_before_creating_evidence(self):
        url = "http://127.0.0.1:8000/mcp"
        for selected in (
            [],
            [self.case, self.case],
            [self.case | {"arguments": []}],
            [{}],
            [self.case | {"scenario": 7}],
        ):
            with self.subTest(selected=selected), self.assertRaises(ValueError):
                asyncio.run(run_http(url, (url,), selected, Limits(), self.output, fixture=True))
        self.assertFalse(self.output.exists())

    def test_invalid_authorization_is_rejected_before_any_work_without_echoing_it(self):
        url = "http://127.0.0.1:8000/mcp"
        for value in ("PRIVATE-CANARY\r\nHeader: value", "Bearer café"):
            with self.subTest(value=value), self.assertRaises(ValueError) as caught:
                asyncio.run(
                    run_http(
                        url,
                        (url,),
                        [self.case],
                        Limits(),
                        self.output,
                        fixture=True,
                        authorization=value,
                    )
                )
            self.assertNotIn(value, str(caught.exception))
        self.assertFalse(self.output.exists())

    def test_caller_cancellation_is_propagated_after_saving_partial_evidence(self):
        async def cancelled():
            raise asyncio.CancelledError

        url = "http://127.0.0.1:8000/mcp"
        with (
            patch("scripts.benchmark_http._Campaign.execute", side_effect=cancelled),
            self.assertRaises(asyncio.CancelledError),
        ):
            asyncio.run(run_http(url, (url,), [self.case], Limits(), self.output, fixture=True))
        report = json.loads(self.output.read_text())
        self.assertEqual(report["state"], "cancelled")
        self.assertFalse(report["complete"])

    def test_real_http_first_and_warmed_calls_keep_server_telemetry_unknown(self):
        async def scenario():
            async with serving(self.settings, self.context) as url:
                return await run_http(
                    url, (url,), [self.case], Limits(repetitions=2), self.output, fixture=True
                )

        report = asyncio.run(scenario())
        self.assertEqual(json.loads(self.output.read_text()), report)
        self.assertTrue(report["complete"])
        self.assertEqual(report["transport"], "streamable-http")
        self.assertIn("not a cold server", report["conditions"]["cold"])
        self.assertGreater(report["httpRequests"], 4)
        case = report["cases"][0]
        self.assertEqual(case["warm"]["summary"]["errorRate"], 0)
        self.assertEqual(len(case["cold"]["samples"]), 2)
        self.assertEqual(len(case["warmups"]["samples"]), 1)
        self.assertIsNone(case["cold"]["samples"][0]["outboundRequests"])
        self.assertNotIn("preferred_name", json.dumps(report))
        raw = json.dumps(report).encode()
        metadata = envelope(kind="benchmark", report_sha256=hashlib.sha256(raw).hexdigest())
        projected = project_benchmark(json.dumps(metadata).encode(), raw)
        self.assertEqual(projected["phase_labels"]["cold"], "First call in new client session")
        self.assertEqual(projected["cases"][0]["warm"]["summary"]["calls"], 2)
        self.assertFalse(projected["comparison_ready"])
        self.assertNotIn("arguments", projected["cases"][0])

    def test_request_budget_stops_campaign_and_replaces_stale_success(self):
        self.output.write_text('{"complete":true}')

        async def scenario():
            async with serving(self.settings, self.context) as url:
                return await run_http(
                    url, (url,), [self.case], Limits(max_requests=1), self.output, fixture=True
                )

        report = asyncio.run(scenario())
        self.assertFalse(report["complete"])
        self.assertEqual(report["stopReason"], "request_budget")
        self.assertEqual(report["httpRequests"], 1)
        self.assertEqual(json.loads(self.output.read_text())["state"], "failed")

    def test_remote_probe_requires_explicit_opt_in_before_any_work(self):
        with self.assertRaises(ValueError):
            asyncio.run(
                run_http(
                    "https://example.org/mcp",
                    ("https://example.org/mcp",),
                    [self.case],
                    Limits(),
                    self.output,
                )
            )
        self.assertFalse(self.output.exists())

    def test_cancel_during_a_slow_call_retains_partial_state_and_starts_no_more_calls(self):
        cancelled = threading.Event()
        original = self.evs.get_concept
        observed = []

        def slow(*args, **kwargs):
            observed.append(args)
            cancelled.set()
            time.sleep(0.15)
            return original(*args, **kwargs)

        async def scenario():
            async with serving(self.settings, self.context) as url:
                return await run_http(
                    url,
                    (url,),
                    [self.case],
                    Limits(repetitions=3),
                    self.output,
                    fixture=True,
                    cancelled=cancelled,
                )

        with patch.object(self.evs, "get_concept", side_effect=slow):
            report = asyncio.run(scenario())
        self.assertEqual(report["state"], "cancelled")
        self.assertFalse(report["complete"])
        self.assertEqual(len(observed), 1)

    def test_timed_out_tool_call_is_retained_as_an_error_and_stops_further_work(self):
        original = self.evs.get_concept

        def slow(*args, **kwargs):
            time.sleep(0.2)
            return original(*args, **kwargs)

        async def scenario():
            async with serving(self.settings, self.context) as url:
                return await run_http(
                    url, (url,), [self.case], Limits(timeout=0.05), self.output, fixture=True
                )

        with patch.object(self.evs, "get_concept", side_effect=slow):
            report = asyncio.run(scenario())
        self.assertEqual(report["state"], "failed")
        self.assertEqual(report["stopReason"], "timeout")
        phase = report["cases"][0]["cold"]
        self.assertEqual(phase["summary"]["errorRate"], 1)
        self.assertIsNone(phase["samples"][0]["resultBytes"])
        self.assertEqual(len(phase["samples"]), 1)

    def test_unexpected_grouped_bug_is_not_hidden_by_an_expected_budget_failure(self):
        failure = ExceptionGroup("both", [RunStoppedError("request_budget"), AssertionError("bug")])
        url = "http://127.0.0.1:8000/mcp"
        with (
            patch("scripts.benchmark_http._Campaign.execute", side_effect=failure),
            self.assertRaises(ExceptionGroup),
        ):
            asyncio.run(run_http(url, (url,), [self.case], Limits(), self.output, fixture=True))
        report = json.loads(self.output.read_text())
        self.assertFalse(report["complete"])
        self.assertEqual(report["stopReason"], "runner_error")
