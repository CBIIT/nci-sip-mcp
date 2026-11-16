"""Shared HTTP deadlines with real wire requests and a controlled progress clock."""

import os
import unittest
from http.client import HTTPException
from unittest.mock import patch
from urllib.error import HTTPError

from nci_si_mcp import http_client
from nci_si_mcp.bounds import Budget, CallDeadlineError, budgeted, deadline_scope
from nci_si_mcp.config import Settings
from nci_si_mcp.errors import correlated
from nci_si_mcp.http_client import RequestRecord, UpstreamRejectedError
from nci_si_mcp.invocation import call
from test_http_client import BufferedPolicy, Reply, ServerTestCase


class Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.waits: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds

    def sleep(self, seconds: float) -> None:
        self.waits.append(seconds)
        self.advance(seconds)


class TimedPolicy(BufferedPolicy):
    def __init__(self, url):
        super().__init__(url)
        self.timeouts = []

    def exchange(self, *, timeout_seconds, response_limit, on_start):
        self.timeouts.append(timeout_seconds)
        return super().exchange(
            timeout_seconds=timeout_seconds,
            response_limit=response_limit,
            on_start=on_start,
        )


class ProgressResponse:
    """Keep the real response; make each progress read cost deterministic time."""

    def __init__(self, response, clock: Clock, step: float, timed_out: bool) -> None:
        self.response = response
        self.clock = clock
        self.step = step
        self.timed_out = timed_out
        self.status = response.status
        self.headers = response.headers
        self.chunks: list[bytes] = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self) -> None:
        self.response.close()

    def read(self, size: int) -> bytes:
        raise AssertionError("A deadline body must use progress reads, not read()")

    def read1(self, size: int) -> bytes:
        chunk = self.response.read1(min(size, 1))
        self.chunks.append(chunk)
        self.clock.advance(self.step)
        if self.timed_out:
            raise TimeoutError("controlled socket timeout")
        return chunk


class DeadlineHttpTest(ServerTestCase):
    def setUp(self) -> None:
        self.clock = Clock()
        self.records: list[RequestRecord] = []
        self.responses: list[ProgressResponse] = []
        clock_patch = patch("nci_si_mcp.bounds.monotonic", self.clock.monotonic)
        clock_patch.start()
        self.addCleanup(clock_patch.stop)

    def observed_client(self, server, *, advance: float = 0, **options):
        client = self.client(server, **options)
        client.sleep = self.clock.sleep

        def record(row: RequestRecord) -> None:
            self.records.append(row)
            self.clock.advance(advance)

        client.on_request = record
        return client

    def progress_reads(self, step: float, *, timed_out: bool = False):
        open_real = http_client._open

        def open_progress(request, timeout):
            try:
                response = open_real(request, timeout)
            except HTTPError as error:
                wrapper = ProgressResponse(error.fp, self.clock, step, timed_out)
                self.responses.append(wrapper)
                read_patch = patch.object(error, "read1", wrapper.read1, create=True)
                read_patch.start()
                self.addCleanup(read_patch.stop)
                raise
            wrapper = ProgressResponse(response, self.clock, step, timed_out)
            self.responses.append(wrapper)
            return wrapper

        return patch("nci_si_mcp.http_client._open", open_progress)

    def test_expired_budget_starts_no_request_and_reports_zero_attempts(self):
        """Given an expired call, when HTTP is requested, then no attempt starts."""
        server = self.serve()
        client = self.observed_client(server)
        with deadline_scope(1):
            self.clock.advance(1)
            with self.assertRaises(CallDeadlineError) as raised:
                client.get_json("/x")
        self.assertEqual(raised.exception.code, "upstream_unavailable")
        self.assertEqual(raised.exception.details, {"attempts": 0})
        self.assertEqual(server.seen, [])
        self.assertEqual(self.records, [])

    def test_deadline_precedes_an_empty_shared_request_budget(self):
        """Given expiry and no budget, when requested, then deadline refusal wins."""
        server = self.serve()
        client = self.observed_client(server)
        budget = Budget(requests=0)
        with budgeted(budget), deadline_scope(0):
            result = call("read", lambda: client.get_json("/x"))
        self.assertEqual(result["error"]["code"], "upstream_unavailable")
        self.assertEqual(result["error"]["details"], {"attempts": 0})
        self.assertEqual(budget.attempts, 0)
        self.assertEqual(server.seen, [])
        self.assertEqual(self.records, [])

    def test_deadline_refusal_does_not_spend_shared_budget_needed_by_followup(self):
        """Given one shared request, when admission expires, then follow-up can enter."""
        server = self.serve()
        client = self.observed_client(server)
        budget = Budget(requests=1)
        readings = iter((0.0, 0.0, 1.0, 1.0))

        def monotonic():
            return next(readings, 0.0)

        with patch("nci_si_mcp.bounds.monotonic", monotonic), budgeted(budget):
            with deadline_scope(1):
                refused = call("read", lambda: client.get_json("/refused"))
            self.assertEqual(
                refused["error"]["details"],
                {"surface": "evs", "attempts": 0},
            )
            self.assertEqual(server.seen, [])
            self.assertEqual(self.records, [])
            self.assertEqual(budget.attempts, 0)
            with deadline_scope(10):
                self.assertEqual(client.get_json("/eligible"), {"ok": True})
        self.assertEqual(budget.attempts, 1)
        self.assertEqual([path for path, _ in server.seen], ["/eligible"])
        self.assertEqual(
            [(record.attempt, record.failure) for record in self.records],
            [(1, None)],
        )

    def test_backoff_that_cannot_fit_never_sleeps_or_retries(self):
        """Given insufficient time for backoff, retry fails without doing more work."""
        for seconds in (0.125, 0.25):
            with self.subTest(seconds=seconds):
                server = self.serve(Reply(503))
                client = self.observed_client(server)
                with deadline_scope(seconds), self.assertRaises(CallDeadlineError) as raised:
                    client.get_json("/x")
                self.assertEqual(raised.exception.details, {"surface": "evs", "attempts": 1})
                self.assertEqual(len(server.seen), 1)
                self.assertEqual(self.clock.waits, [])

    def test_backoffs_that_fit_are_waited_and_return_the_retried_content(self):
        """Given enough time, two backoffs advance the same clock before success."""
        server = self.serve(Reply(503), Reply(503))
        client = self.observed_client(server)
        with deadline_scope(1):
            result = client.get_json("/x")
        self.assertEqual(result, {"ok": True})
        self.assertEqual(self.clock.waits, [0.25, 0.5])
        self.assertEqual(len(server.seen), 3)
        self.assertEqual(
            [(r.attempt, r.status, r.failure) for r in self.records],
            [(1, 503, "http_status"), (2, 503, "http_status"), (3, 200, None)],
        )

    def test_retry_after_that_cannot_fit_is_never_shortened(self):
        """Given a named wait at or beyond remaining time, no early retry is sent."""
        for seconds in (6, 7):
            with self.subTest(seconds=seconds):
                server = self.serve(Reply(429, headers={"Retry-After": "7"}))
                client = self.observed_client(server)
                with deadline_scope(seconds), self.assertRaises(CallDeadlineError) as raised:
                    client.get_json("/x")
                self.assertEqual(raised.exception.details, {"surface": "evs", "attempts": 1})
                self.assertEqual(len(server.seen), 1)
                self.assertEqual(self.clock.waits, [])

    def test_retry_after_that_fits_is_waited_in_full_before_success(self):
        """Given a fitting Retry-After, the full named wait precedes usable content."""
        server = self.serve(Reply(429, headers={"Retry-After": "7"}))
        client = self.observed_client(server)
        with deadline_scope(8):
            result = client.get_json("/x")
        self.assertEqual(result, {"ok": True})
        self.assertEqual(self.clock.waits, [7.0])
        self.assertEqual(len(server.seen), 2)

    def test_expiry_reported_after_an_attempt_prevents_even_a_zero_delay_retry(self):
        """Given an attempt consumes the budget, retry starts no second request."""
        server = self.serve(Reply(503))
        client = self.observed_client(server, advance=1, retry_backoff_seconds=0)
        with deadline_scope(1), self.assertRaises(CallDeadlineError) as raised:
            client.get_json("/x")
        self.assertEqual(raised.exception.details, {"surface": "evs", "attempts": 1})
        self.assertEqual(len(server.seen), 1)
        self.assertEqual([(r.attempt, r.status) for r in self.records], [(1, 503)])
        self.assertEqual(self.clock.waits, [])

    def test_sleep_overrun_is_checked_before_the_next_attempt(self):
        """Given a fitting sleep returns late, no request starts after the deadline."""
        server = self.serve(Reply(503))
        client = self.observed_client(server)

        def oversleep(seconds: float) -> None:
            self.clock.sleep(seconds)
            self.clock.advance(1)

        client.sleep = oversleep
        with deadline_scope(1), self.assertRaises(CallDeadlineError) as raised:
            client.get_json("/x")
        self.assertEqual(raised.exception.details, {"surface": "evs", "attempts": 1})
        self.assertEqual(self.clock.waits, [0.25])
        self.assertEqual(len(server.seen), 1)
        self.assertEqual(len(self.records), 1)

    def test_final_transient_attempt_expiry_reports_deadline_instead_of_status(self):
        """Given no retries remain, expiry after the actual failure still owns the error."""
        server = self.serve(Reply(503))
        client = self.observed_client(server, advance=1, max_attempts=1)
        with deadline_scope(1), correlated("final-attempt-expiry"):
            result = call("lookup", lambda: client.get_json("/x"))
        self.assertEqual(
            result,
            {
                "error": {
                    "code": "upstream_unavailable",
                    "message": (
                        "The call exhausted its elapsed-time budget. "
                        "Retry later or ask for less work."
                    ),
                    "details": {"surface": "evs", "attempts": 1},
                    "correlationId": "final-attempt-expiry",
                }
            },
        )
        self.assertEqual([path for path, _ in server.seen], ["/x"])
        self.assertEqual(
            [(row.attempt, row.status, row.failure) for row in self.records],
            [(1, 503, "http_status")],
        )
        self.assertEqual(self.clock.waits, [])

    def test_shared_attempt_total_includes_retries_and_names_the_last_surface(self):
        """Given two clients in one call, expiry reports all actual attempts."""
        server = self.serve(Reply(503))
        evs = self.observed_client(server, advance=2, retry_backoff_seconds=0)
        cadsr = self.observed_client(server, advance=2, label="CADSR")
        with deadline_scope(5), correlated("shared-http-deadline"):
            self.assertEqual(evs.get_json("/evs"), {"ok": True})
            self.assertEqual(cadsr.get_json("/cadsr"), {"ok": True})
            result = call("get_data_element", lambda: cadsr.get_json("/blocked"))
        self.assertEqual(result["error"]["code"], "upstream_unavailable")
        self.assertEqual(result["error"]["details"], {"surface": "cadsr", "attempts": 3})
        self.assertEqual(result["error"]["correlationId"], "shared-http-deadline")
        self.assertEqual([path for path, _ in server.seen], ["/evs", "/evs", "/cadsr"])
        self.assertEqual(
            [(r.surface, r.attempt, r.status) for r in self.records],
            [("evs", 1, 503), ("evs", 2, 200), ("cadsr", 1, 200)],
        )

    def test_wire_timeout_uses_the_smaller_of_request_timeout_and_remaining_budget(self):
        """Given time already spent, a real successful exchange gets the tighter timeout."""
        server = self.serve()
        client = self.observed_client(server)
        open_real = http_client._open
        timeouts: list[float] = []

        def open_timed(request, timeout):
            timeouts.append(timeout)
            return open_real(request, timeout)

        with patch("nci_si_mcp.http_client._open", open_timed), deadline_scope(10):
            self.assertEqual(client.get_json("/first"), {"ok": True})
            self.clock.advance(8)
            self.assertEqual(client.get_json("/second"), {"ok": True})
        self.assertEqual(timeouts, [5, 2])
        self.assertTrue(all(timeout > 0 for timeout in timeouts))
        self.assertEqual([path for path, _ in server.seen], ["/first", "/second"])

    def test_controlled_wire_timeout_stays_positive_as_deadline_runs_down(self):
        """Given a running deadline, when controlled, then timeouts stay positive."""
        server = self.serve()
        policy = TimedPolicy(server.url + "/x")
        client = self.observed_client(server, request_policy=policy)
        with deadline_scope(10):
            self.assertEqual(client.get_json("/x"), {"ok": True})
            self.clock.advance(8)
            self.assertEqual(client.get_json("/x"), {"ok": True})
        self.assertEqual(policy.timeouts, [5, 2])
        self.assertTrue(all(timeout > 0 for timeout in policy.timeouts))
        self.assertEqual([path for path, _ in server.seen], ["/x", "/x"])
        self.assertEqual(policy.witnesses, [False, False, False, False])

    def test_expiry_at_controlled_timeout_construction_prevents_exchange(self):
        """Given expiry during timeout construction, when controlled, then no exchange starts."""
        server = self.serve()
        policy = TimedPolicy(server.url + "/x")
        client = self.observed_client(server, request_policy=policy)
        readings = iter((0.0, 1.0))

        def monotonic():
            return next(readings, 1.0)

        with (
            patch("nci_si_mcp.bounds.monotonic", monotonic),
            deadline_scope(1),
            self.assertRaises(CallDeadlineError) as raised,
        ):
            client.get_json("/x")
        self.assertEqual(raised.exception.details, {"attempts": 0})
        self.assertEqual(policy.timeouts, [])
        self.assertEqual(server.seen, [])
        self.assertEqual(self.records, [])

    def test_expiry_before_uncontrolled_transport_is_a_deadline_refusal(self):
        """Given expiry before an uncontrolled send, when requested, then no transport opens."""
        server = self.serve()
        client = self.observed_client(server)
        readings = iter((0.0, 0.0, 0.0, 1.0))
        opened = []

        def monotonic():
            return next(readings, 1.0)

        def forbidden_open(request, timeout):
            opened.append((request, timeout))
            raise AssertionError("expired uncontrolled transport was invoked")

        with (
            patch("nci_si_mcp.bounds.monotonic", monotonic),
            patch.object(http_client, "_open", forbidden_open),
            deadline_scope(1),
            self.assertRaises(CallDeadlineError) as raised,
        ):
            client.get_json("/x")
        self.assertEqual(raised.exception.details, {"attempts": 0})
        self.assertEqual(opened, [])
        self.assertEqual(server.seen, [])
        self.assertEqual(self.records, [])

    def test_request_timeout_before_deadline_retains_the_timeout_error_contract(self):
        """Given socket timeouts before call expiry, the caller still receives timeout."""
        server = self.serve()
        client = self.observed_client(server, max_attempts=2, timeout_seconds=1)
        with self.progress_reads(1, timed_out=True), deadline_scope(10):
            result = call("lookup", lambda: client.get_json("/x"))
        self.assertEqual(result["error"]["code"], "timeout")
        self.assertEqual(
            result["error"]["details"], {"surface": "evs", "seconds": 1, "attempts": 2}
        )
        self.assertEqual(len(server.seen), 2)
        self.assertEqual(
            [(r.attempt, r.status, r.failure) for r in self.records],
            [(1, 200, "timeout"), (2, 200, "timeout")],
        )
        self.assertTrue(all(r.response.closed for r in self.responses))

    def test_progress_body_that_fits_returns_all_content_and_closes_response(self):
        """Given a body making progress within budget, all its JSON is returned."""
        server = self.serve(Reply(body=b'{"n":1}'))
        client = self.observed_client(server)
        with self.progress_reads(0.25), deadline_scope(3):
            result = client.get_json("/x")
        self.assertEqual(result, {"n": 1})
        self.assertEqual(b"".join(self.responses[0].chunks), b'{"n":1}')
        self.assertTrue(self.responses[0].response.closed)
        self.assertEqual(len(server.seen), 1)

    def test_slow_success_body_expires_mid_progress_and_closes_without_retry(self):
        """Given a dripping success body, expiry closes it instead of returning partial JSON."""
        server = self.serve()
        client = self.observed_client(server)
        with (
            self.progress_reads(1),
            deadline_scope(2),
            self.assertRaises(CallDeadlineError) as raised,
        ):
            client.get_json("/x")
        self.assertEqual(raised.exception.details, {"surface": "evs", "attempts": 1})
        self.assertEqual(self.responses[0].chunks, [b"{", b'"'])
        self.assertTrue(self.responses[0].response.closed)
        self.assertEqual(len(server.seen), 1)
        self.assertEqual([(r.attempt, r.status) for r in self.records], [(1, 200)])
        self.assertEqual(self.clock.waits, [])

    def test_deadline_is_checked_after_the_last_body_chunk_before_returning_content(self):
        """Given the final chunk consumes the budget, complete bytes do not become success."""
        server = self.serve(Reply(body=b"1"))
        client = self.observed_client(server)
        with (
            self.progress_reads(1),
            deadline_scope(1),
            self.assertRaises(CallDeadlineError) as raised,
        ):
            client.get_json("/x")
        self.assertEqual(raised.exception.details, {"surface": "evs", "attempts": 1})
        self.assertEqual(self.responses[0].chunks, [b"1"])
        self.assertTrue(self.responses[0].response.closed)
        self.assertEqual(len(server.seen), 1)

    def test_slow_http_error_body_expires_and_closes_without_retry_or_rejection(self):
        """Given an error body drips past expiry, deadline wins over status handling."""
        for status in (403, 503):
            with self.subTest(status=status):
                server = self.serve(Reply(status, body=b'{"message":"busy"}'))
                client = self.observed_client(server)
                with (
                    self.progress_reads(1),
                    deadline_scope(2),
                    self.assertRaises(CallDeadlineError) as raised,
                ):
                    client.get_json("/x")
                self.assertEqual(raised.exception.details, {"surface": "evs", "attempts": 1})
                self.assertEqual(self.responses[-1].chunks, [b"{", b'"'])
                self.assertTrue(self.responses[-1].response.closed)
                self.assertEqual(len(server.seen), 1)
                self.assertEqual(self.records[-1].status, status)
                self.assertEqual(self.clock.waits, [])

    def test_http_error_body_within_budget_retains_rejection_detail_and_closes(self):
        """Given a fitting error body, rejection still retains upstream classification text."""
        server = self.serve(Reply(403, body=b'{"message":"denied"}'))
        client = self.observed_client(server)
        with (
            self.progress_reads(0.125),
            deadline_scope(5),
            self.assertRaises(UpstreamRejectedError) as raised,
        ):
            client.get_json("/x")
        self.assertEqual(raised.exception.body_message, "denied")
        self.assertFalse(raised.exception.empty_body)
        self.assertEqual(raised.exception.details, {"surface": "evs", "status": 403, "attempts": 1})
        self.assertTrue(self.responses[0].response.closed)
        self.assertEqual(len(server.seen), 1)

    def test_terminal_error_read_timeout_after_expiry_reports_deadline_not_rejection(self):
        # Given a real terminal error response, a stalled body read cannot erase expiry.
        server = self.serve(Reply(404, body=b'{"message":"missing"}'))
        client = self.observed_client(server)
        with (
            self.progress_reads(2, timed_out=True),
            deadline_scope(1),
            self.assertRaises(CallDeadlineError) as raised,
        ):
            client.get_json("/x")
        self.assertEqual(raised.exception.details, {"surface": "evs", "attempts": 1})
        self.assertTrue(self.responses[0].response.closed)
        self.assertEqual(len(server.seen), 1)
        self.assertEqual(self.records[0].status, 404)
        self.assertEqual(self.clock.waits, [])

    def test_terminal_error_read_timeout_before_expiry_keeps_status_rejection(self):
        server = self.serve(Reply(404, body=b'{"message":"missing"}'))
        client = self.observed_client(server)
        with (
            self.progress_reads(0.125, timed_out=True),
            deadline_scope(5),
            self.assertRaises(UpstreamRejectedError) as raised,
        ):
            client.get_json("/x")
        self.assertEqual(raised.exception.details, {"surface": "evs", "status": 404, "attempts": 1})
        self.assertTrue(self.responses[0].response.closed)
        self.assertEqual(len(server.seen), 1)

    def test_terminal_error_incomplete_read_preserves_deadline_or_status_by_elapsed_time(self):
        for expired in (False, True):
            with self.subTest(expired=expired):
                server = self.serve(Reply(404, body=b'{"message":"missing"}'))
                client = self.observed_client(server)

                def incomplete(response, size, expired=expired):
                    response.clock.advance(2 if expired else 0.125)
                    raise HTTPException("controlled incomplete error body")

                expected = CallDeadlineError if expired else UpstreamRejectedError
                with (
                    self.progress_reads(0),
                    patch.object(ProgressResponse, "read1", incomplete),
                    deadline_scope(1),
                    self.assertRaises(expected) as raised,
                ):
                    client.get_json("/x")
                details = {"surface": "evs", "attempts": 1}
                self.assertEqual(
                    raised.exception.details, details if expired else details | {"status": 404}
                )
                self.assertTrue(self.responses[-1].response.closed)
                self.assertEqual(len(server.seen), 1)
                self.assertEqual(self.clock.waits, [])


class DeadlineSettingsTest(unittest.TestCase):
    def test_deadline_defaults_to_fifty_seconds_and_accepts_a_fractional_override(self):
        """Given default or explicit configuration, callers get the selected budget."""
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(Settings.from_env().call_deadline_seconds, 50.0)
        with patch.dict(os.environ, {"NCI_SI_CALL_DEADLINE_SECONDS": "2.5"}, clear=True):
            self.assertEqual(Settings.from_env().call_deadline_seconds, 2.5)
        self.assertEqual(Settings(call_deadline_seconds=0.25).call_deadline_seconds, 0.25)

    def test_invalid_deadline_environment_names_the_setting_at_startup(self):
        """Given nonpositive, nonfinite or nonnumeric configuration, startup rejects it."""
        for value in ("0", "-1", "nan", "inf", "-inf", "disabled", ""):
            with (
                self.subTest(value=value),
                patch.dict(os.environ, {"NCI_SI_CALL_DEADLINE_SECONDS": value}, clear=True),
                self.assertRaisesRegex(ValueError, "NCI_SI_CALL_DEADLINE_SECONDS"),
            ):
                Settings.from_env()

    def test_invalid_direct_deadline_settings_cannot_disable_the_budget(self):
        """Given a direct invalid setting, construction also fails closed."""
        for value in (0, -1, float("nan"), float("inf"), float("-inf")):
            with (
                self.subTest(value=value),
                self.assertRaisesRegex(ValueError, "NCI_SI_CALL_DEADLINE_SECONDS"),
            ):
                Settings(call_deadline_seconds=value)
