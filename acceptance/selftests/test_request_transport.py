"""Entry accounting through HttpClient and an explicitly local diagnostic transport.

This component consumer tests the entry handshake, not the production provider's grants.
"""

import socket
import struct
from contextlib import contextmanager, nullcontext
from http.client import HTTPConnection
from pathlib import Path
from threading import Event, Thread
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
from nci_si_acceptance.request_budget import AttemptLease
from nci_si_acceptance.request_summary import read_summary
from nci_si_acceptance.request_supervision import SupervisedRun
from nci_si_mcp.audit import audited
from nci_si_mcp.bounds import Budget, RequestBudgetError, budgeted, deadline_scope
from nci_si_mcp.errors import correlated
from nci_si_mcp.http_client import HttpClient, RequestPolicyError
from nci_si_mcp.invocation import call
from nci_si_mcp.request_policy import Exchange


@contextmanager
def entry_origin(case, receiver):
    if case != "connection-failure":
        yield receiver.base_url("evs"), []
        return
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(0.05)
        stop, accepted, failures = Event(), [], []

        def reset_connection():
            while not stop.is_set():
                try:
                    connection, _address = listener.accept()
                    with connection:
                        accepted.append(1)
                        # Abort the owned connection without accepting an HTTP request.
                        connection.setsockopt(
                            socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0)
                        )
                    return
                except TimeoutError:
                    continue
                except OSError:
                    if not stop.is_set():
                        failures.append("owned connection receiver failed")
                    return

        thread = Thread(target=reset_connection)
        thread.start()
        try:
            yield f"http://127.0.0.1:{listener.getsockname()[1]}", accepted
        finally:
            stop.set()
            thread.join(2)
            assert not thread.is_alive()
            assert failures == []


class ClosingStart:
    """Close the actual ledger at final admission, after the core's preflight."""

    def __init__(self, start, run):
        self.start, self.run = start, run

    @property
    def refused_before_entry(self):
        return self.start.refused_before_entry

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
            if self.case == "reused-start":
                with (
                    self.logical.attempt(response_limit=response_limit) as denied,
                    budgeted(Budget(requests=0)),
                    pytest.raises(RequestBudgetError),
                ):
                    denied.transport(on_start, lambda: self.open(connection, parts.path))
                self.first_snapshot = self.run.snapshot()
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
        if self.case == "closed-after-entry":
            self.run.close()
        return self.response

    def fail_receipt(self):
        self.run.refuse("accounting")


def known_denial_scope(case, monkeypatch):
    if case == "deadline-denial":
        original_enter = HttpClient._enter

        def expired_gate(client, attempt):
            with deadline_scope(0):
                original_enter(client, attempt)

        monkeypatch.setattr(HttpClient, "_enter", expired_gate)
    return budgeted(Budget(requests=0)) if case == "callback-denial" else nullcontext()


CASES = (
    # case, entries, bytes, active, reserved, pending, core, fixture requests, summary, outer
    ("success", 1, 12, 0, 12, 0, 1, 1, "completed", None),
    ("final-refusal", 0, 0, 0, 0, 0, 0, 0, "failed", "closed"),
    ("callback-denial", 0, 0, 0, 0, 0, 0, 0, "completed", None),
    ("deadline-denial", 0, 0, 0, 0, 0, 0, 0, "completed", None),
    ("connection-failure", 1, 0, 0, 0, 0, 1, 0, "completed", None),
    ("receipt-failure", 0, 0, 1, 33, 1, 1, 1, "failed", "accounting"),
    ("closed-after-entry", 1, 0, 0, 0, 0, 1, 1, "failed", "closed"),
)


@pytest.mark.parametrize("expected", CASES, ids=[row[0] for row in CASES])
def test_handshake_entry_receipts(tmp_path, monkeypatch, expected):
    case, entries, known, active, reserved, pending, core, fixture_requests, state, reason = (
        expected
    )
    fixtures = FixtureSet(
        {request_key("evs", "GET", "/owned", {}): Fixture("owned", (Response(body={"ok": True}),))},
        {},
    )
    with FixtureServer(fixtures) as receiver, entry_origin(case, receiver) as (base, tcp_contacts):
        grant = RequestGrant("GET", base + "/owned", (("accept", "application/json"),))
        authorization = Authorization((grant,), Limits(response_bytes=32, total_bytes=66), "a" * 64)
        owner = SupervisedRun.create(authorization, tmp_path)
        scope = known_denial_scope(case, monkeypatch)
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
        assert (audit.outbound_requests, len(hooks), len(receiver.log())) == (
            core,
            core,
            fixture_requests,
        )
        if case == "connection-failure":
            assert tcp_contacts == [1]
            assert consumer.response is None
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


def install_entry_faults(monkeypatch, run, phase, failure, cleanup):
    original_enter, original_open = HttpClient._enter, Path.open
    injected = []

    def core_gate(client, attempt):
        if phase != "before-core":
            original_enter(client, attempt)
        if phase != "operation":
            raise failure

    def marker_open(path, mode="r", *args, **kwargs):
        if path == run.directory / "failed" and mode == "x" and not injected:
            injected.append(1)
            raise OSError("owned marker write failure")
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(HttpClient, "_enter", core_gate)
    if phase == "after-prepare":
        original_prepare = AttemptLease._prepare_entry

        def interrupted_prepare(lease):
            original_prepare(lease)
            raise failure

        monkeypatch.setattr(AttemptLease, "_prepare_entry", interrupted_prepare)
    if cleanup:
        monkeypatch.setattr(Path, "open", marker_open)


ENTRY_FAILURES = (
    # phase, factory, core, operations, entries, active, reserved, pending, complete, cleanup
    ("before-core", KeyboardInterrupt, 0, 0, 0, 1, 33, 1, False, False),
    ("after-core", KeyboardInterrupt, 1, 0, 0, 1, 33, 1, False, False),
    ("operation", KeyboardInterrupt, 1, 1, 1, 0, 0, 0, True, False),
    ("before-core", RuntimeError, 0, 0, 0, 1, 33, 1, False, False),
    ("after-core", RuntimeError, 1, 0, 0, 1, 33, 1, False, False),
    ("operation", RuntimeError, 1, 1, 1, 0, 0, 0, True, False),
    ("after-core", lambda _message: RequestBudgetError(0, 0), 1, 0, 0, 1, 33, 1, False, False),
    ("after-core", KeyboardInterrupt, 1, 0, 0, 1, 33, 1, False, True),
    ("after-prepare", KeyboardInterrupt, 0, 0, 0, 1, 33, 1, False, False),
    (
        "after-core",
        lambda _message: RequestPolicyError("authorization"),
        1,
        0,
        0,
        1,
        33,
        1,
        False,
        False,
    ),
)


def exception_leaves(error):
    if isinstance(error, BaseExceptionGroup):
        return tuple(leaf for child in error.exceptions for leaf in exception_leaves(child))
    return (error,)


@pytest.mark.parametrize(
    "expected",
    ENTRY_FAILURES,
    ids=[
        "interrupt-before-core",
        "interrupt-after-core",
        "interrupt-operation",
        "bug-before-core",
        "bug-after-core",
        "bug-operation",
        "limit-shaped-after-core",
        "interrupt-and-marker-failure",
        "interrupt-after-persisted-prepare",
        "precise-policy-after-core",
    ],
)
def test_entry_failure_stage_preserves_truthful_receipts(tmp_path, monkeypatch, expected):
    phase, factory, core, invoked, entries, active, reserved, pending, complete, cleanup = expected
    failure = factory("owned entry failure")
    fixtures = FixtureSet(
        {request_key("evs", "GET", "/owned", {}): Fixture("owned", (Response(body={"ok": True}),))},
        {},
    )
    with FixtureServer(fixtures) as receiver:
        base = receiver.base_url("evs")
        grant = RequestGrant("GET", base + "/owned", (("accept", "application/json"),))
        owner = SupervisedRun.create(
            Authorization((grant,), Limits(response_bytes=32, total_bytes=66), "a" * 64), tmp_path
        )
        operations, closed = [], []
        install_entry_faults(monkeypatch, owner.run, phase, failure, cleanup)

        def operation(_connection, _path):
            operations.append(1)
            raise failure

        with (
            audited("entry", {}, {}, ()) as audit,
            pytest.raises((type(failure), BaseExceptionGroup)) as raised,
            owner,
        ):
            consumer = EntryConsumer(owner.run, grant, "success", monkeypatch)
            monkeypatch.setattr(consumer, "open", operation)
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
            try:
                client.get_json("/owned")
            except BaseException:
                try:
                    owner.run.check_wait(0)
                except RequestPolicyError as refusal:
                    closed.append(refusal.reason)
                raise
        errors = exception_leaves(raised.value)
        assert errors[0] is failure
        if cleanup:
            assert tuple(type(error) for error in errors) == (type(failure), RequestPolicyError)
            assert errors[1].reason == "accounting"
        else:
            assert errors == (failure,)
        assert (audit.outbound_requests, len(operations), receiver.log()) == (core, invoked, [])
        snapshot = owner.run.snapshot()
        assert (snapshot.logical, snapshot.attempts, snapshot.consumed_body_bytes) == (1, 1, 0)
        assert (
            snapshot.transport_entries,
            snapshot.active,
            snapshot.reserved_body_bytes,
            snapshot.pending_receipts,
            snapshot.observation_complete,
            closed,
        ) == (entries, active, reserved, pending, complete, [] if complete else ["closed"])
        if isinstance(failure, RequestPolicyError):
            assert snapshot.failure == "authorization"
        summary = read_summary(
            owner.run.directory / "summary.json",
            run_id=owner.run.descriptor.run_id,
            policy_sha256="a" * 64,
        )
        assert (summary["state"], summary["observationComplete"]) == ("failed", complete)


def test_refusal_proof_cannot_survive_start_reuse(tmp_path, monkeypatch):
    failure = RuntimeError("owned failure after a prior known refusal")
    fixtures = FixtureSet(
        {request_key("evs", "GET", "/owned", {}): Fixture("owned", (Response(body={"ok": True}),))},
        {},
    )
    with FixtureServer(fixtures) as receiver:
        base = receiver.base_url("evs")
        grant = RequestGrant("GET", base + "/owned", (("accept", "application/json"),))
        owner = SupervisedRun.create(
            Authorization((grant,), Limits(response_bytes=32, total_bytes=66), "a" * 64), tmp_path
        )
        operations = []
        install_entry_faults(monkeypatch, owner.run, "after-core", failure, False)

        def operation(_connection, _path):
            operations.append(1)
            pytest.fail("Neither the known denial nor the interrupted gate may invoke transport")

        with audited("entry", {}, {}, ()) as audit, pytest.raises(RuntimeError) as raised, owner:
            consumer = EntryConsumer(owner.run, grant, "reused-start", monkeypatch)
            monkeypatch.setattr(consumer, "open", operation)
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
            client.get_json("/owned")
        assert raised.value is failure
        first = consumer.first_snapshot
        assert (
            first.attempts,
            first.transport_entries,
            first.active,
            first.reserved_body_bytes,
        ) == (
            1,
            0,
            0,
            0,
        )
        assert (audit.outbound_requests, operations, receiver.log()) == (1, [], [])
        snapshot = owner.run.snapshot()
        assert (snapshot.logical, snapshot.attempts, snapshot.transport_entries) == (1, 2, 0)
        assert (snapshot.active, snapshot.reserved_body_bytes, snapshot.pending_receipts) == (
            1,
            33,
            1,
        )
        summary = read_summary(
            owner.run.directory / "summary.json",
            run_id=owner.run.descriptor.run_id,
            policy_sha256="a" * 64,
        )
        assert (summary["state"], summary["observationComplete"]) == ("failed", False)
