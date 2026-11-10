"""Entry accounting through HttpClient and an explicitly local diagnostic transport.

This component consumer tests the entry handshake, not the production provider's grants.
"""

import socket
from contextlib import contextmanager, nullcontext
from http.client import HTTPConnection
from urllib.parse import urlsplit

import pytest

from nci_si_acceptance.fixture_server import (
    Fixture,
    FixtureServer,
    FixtureSet,
    Response,
    request_key,
)
from nci_si_acceptance.request_authorization import Authorization, Limits, RequestGrant
from nci_si_acceptance.request_summary import read_summary
from nci_si_acceptance.request_supervision import SupervisedRun
from nci_si_mcp.audit import audited
from nci_si_mcp.bounds import Budget, budgeted
from nci_si_mcp.errors import correlated
from nci_si_mcp.http_client import HttpClient, RequestPolicyError
from nci_si_mcp.invocation import call
from nci_si_mcp.request_policy import Exchange


class ClosingStart:
    """Close the actual ledger at final admission, after the core's preflight."""

    def __init__(self, start, run):
        self.start, self.run = start, run

    def invoke(self, *, admit, operation):
        def close_then_admit():
            self.run.close()
            admit()

        return self.start.invoke(admit=close_then_admit, operation=operation)


class EntryConsumer:
    """An explicit component caller; no production transport claim or activation."""

    def __init__(self, run, grant, case, monkeypatch):
        self.run, self.grant = run, grant
        self.case, self.monkeypatch = case, monkeypatch
        self.response = None

    @contextmanager
    def request(self, request):
        assert request.full_url == self.grant.url
        with self.run.logical(self.grant) as logical:
            self.logical = logical
            yield self

    def check_wait(self, delay_seconds):
        self.run.check_wait(delay_seconds)

    def exchange(self, *, timeout_seconds, response_limit, on_start):
        parts = urlsplit(self.grant.url)
        connection = HTTPConnection(parts.hostname, parts.port, timeout=timeout_seconds)
        try:
            with self.logical.attempt(response_limit=response_limit) as lease:
                assert hasattr(lease, "transport"), (
                    "The lease must guard and receipt transport entry."
                )
                if self.case == "receipt-failure":
                    self.monkeypatch.setattr(lease, "_receipt_entry", self.fail_receipt)
                start = (
                    ClosingStart(on_start, self.run) if self.case == "final-refusal" else on_start
                )
                response = lease.transport(start, lambda: self.open(connection, parts.path))
                body = lease.read(response.read1, response_limit + 1)
                return Exchange(response.status, dict(response.headers), body)
        finally:
            if self.response is not None:
                self.response.close()
            connection.close()

    def open(self, connection, path):
        connection.request("GET", path)
        self.response = connection.getresponse()
        return self.response

    def fail_receipt(self):
        self.run.refuse("accounting")


CASES = (
    # case, entries, bytes, active, reserved, pending, core, contacts, summary, outer
    ("success", 1, 12, 0, 12, 0, 1, 1, "completed", None),
    ("final-refusal", 0, 0, 0, 0, 0, 0, 0, "failed", "closed"),
    ("callback-denial", 0, 0, 0, 0, 0, 0, 0, "completed", None),
    ("connection-failure", 1, 0, 0, 0, 0, 1, 0, "completed", None),
    ("receipt-failure", 0, 0, 1, 33, 1, 1, 1, "failed", "accounting"),
)


@pytest.mark.parametrize("expected", CASES, ids=[row[0] for row in CASES])
def test_handshake_entry_receipts(tmp_path, monkeypatch, expected):
    case, entries, known, active, reserved, pending, core, contacts, state, reason = expected
    fixtures = FixtureSet(
        {request_key("evs", "GET", "/owned", {}): Fixture("owned", (Response(body={"ok": True}),))},
        {},
    )
    # Keeping a non-listening bound socket owns the refused destination without a port-reuse gap.
    with FixtureServer(fixtures) as receiver, socket.socket() as refused_socket:
        refused_socket.bind(("127.0.0.1", 0))
        base = (
            f"http://127.0.0.1:{refused_socket.getsockname()[1]}"
            if case == "connection-failure"
            else receiver.base_url("evs")
        )
        grant = RequestGrant("GET", base + "/owned", (("accept", "application/json"),))
        authorization = Authorization((grant,), Limits(response_bytes=32, total_bytes=66), "a" * 64)
        owner = SupervisedRun.create(authorization, tmp_path)
        scope = budgeted(Budget(requests=0)) if case == "callback-denial" else nullcontext()
        hooks, outer, result = [], None, None
        with scope, correlated("policy-test"), audited("entry", {}, {}, ()) as audit:
            try:
                with owner:
                    consumer = EntryConsumer(owner.run, grant, case, monkeypatch)
                    client = HttpClient(
                        base,
                        label="EVS",
                        timeout_seconds=1,
                        max_attempts=1,
                        retry_backoff_seconds=0,
                        max_response_bytes=32,
                        size_bound="entry-test",
                        request_policy=consumer,
                    )
                    client.on_request = hooks.append
                    result = call("entry", lambda: client.get_json("/owned"))
                    audit.result = result
            except RequestPolicyError as error:
                outer = error.reason
        snapshot = owner.run.snapshot()
        assert (snapshot.logical, snapshot.attempts, snapshot.peak_active) == (1, 1, 1)
        assert (
            snapshot.transport_entries,
            snapshot.consumed_body_bytes,
            snapshot.active,
            snapshot.reserved_body_bytes,
            snapshot.pending_receipts,
        ) == (entries, known, active, reserved, pending)
        assert (audit.outbound_requests, len(hooks), len(receiver.log())) == (core, core, contacts)
        assert outer == reason
        summary = read_summary(
            owner.run.directory / "summary.json",
            run_id=owner.run.descriptor.run_id,
            policy_sha256="a" * 64,
        )
        assert (summary["state"], summary["observationComplete"]) == (state, pending == 0)
        assert summary["observed"] == {
            "transportEntries": entries,
            "bodyBytes": known,
            "peakActive": 1,
        }
        assert summary["reservations"] == {"logical": 1, "attempts": 1, "bodyBytes": reserved}
        if case == "success":
            assert result == {"ok": True}
        else:
            expected_code = (
                "bound_exceeded" if case == "callback-denial" else "upstream_unavailable"
            )
            assert result["error"]["code"] == expected_code
        if reason is not None:
            assert result == {
                "error": {
                    "code": "upstream_unavailable",
                    "message": "Controlled upstream request refused. Retry later.",
                    "correlationId": "policy-test",
                }
            }
