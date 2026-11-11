"""Physical HTTP framing must not turn partial or ambiguous bytes into success."""

import sqlite3
import traceback
from contextlib import closing
from http import HTTPStatus
from http.client import HTTPConnection, HTTPException, HTTPResponse
from pathlib import Path
from threading import Event, current_thread

import pytest
from http_receiver import WireReceiver

from nci_si_acceptance.request_authorization import Authorization, Limits, RequestGrant
from nci_si_acceptance.request_budget import AttemptLease
from nci_si_acceptance.request_supervision import SupervisedRun
from nci_si_acceptance.request_transport import ControlledHttpPolicy, TransportCleanupError
from nci_si_mcp import bounds
from nci_si_mcp.bounds import CallDeadlineError, deadline_scope
from nci_si_mcp.cadsr import CaDSRClient
from nci_si_mcp.config import Settings
from nci_si_mcp.errors import PlatformError, correlated
from nci_si_mcp.http_client import (
    HttpClient,
    RequestPolicyError,
    UpstreamError,
    UpstreamRejectedError,
    UpstreamTimeoutError,
)
from nci_si_mcp.invocation import call
from nci_si_mcp.request_policy import using_request_policy

# The independently scripted JSON document below has twelve wire bytes.
JSON_BYTES = 12
# The core's reviewed error-prefix contract is independent of the client success limit.
ERROR_PREFIX_BYTES = 4096


def owned_client(receiver, owner, *, limit=64, attempts=1):
    return HttpClient(
        receiver.origin,
        label="owned",
        timeout_seconds=1,
        max_attempts=attempts,
        retry_backoff_seconds=0,
        max_response_bytes=limit,
        size_bound="owned",
        request_policy=ControlledHttpPolicy(owner.run),
    )


def owned_run(tmp_path, receiver, *, response_bytes=64, total_bytes=8194):
    grant = RequestGrant("GET", receiver.origin + "/owned", (("accept", "application/json"),))
    return SupervisedRun.create(
        Authorization(
            (grant,), Limits(response_bytes=response_bytes, total_bytes=total_bytes), "a" * 64
        ),
        tmp_path,
    )


@pytest.mark.parametrize(
    "fields,body,known",
    [
        (b"Content-Length: 20\r\n", b'{"ok": true}', 12),
        (b"Content-Length: 12\r\nContent-Length: 13\r\n", b'{"ok": true}', 0),
        (
            b"Transfer-Encoding: chunked\r\nContent-Length: 12\r\n",
            b'c\r\n{"ok": true}\r\n0\r\n\r\n',
            0,
        ),
        (b"Content-Encoding: gzip\r\nContent-Length: 12\r\n", b'{"ok": true}', 0),
        (b"Content-Length: " + b"9" * 5000 + b"\r\n", b'{"ok": true}', 0),
    ],
    ids=[
        "truncated-valid-json",
        "conflicting-length",
        "conflicting-framing",
        "unsupported-encoding",
        "unrepresentable-declared-length",
    ],
)
def test_invalid_framing_cannot_return_success(tmp_path, fields, body, known):
    with WireReceiver([b"HTTP/1.1 200 OK\r\n" + fields + b"\r\n" + body]) as receiver:
        owner = owned_run(tmp_path, receiver)
        with pytest.raises(UpstreamError), owner:
            owned_client(receiver, owner).get_json("/owned")
        snapshot = owner.run.snapshot()
        assert receiver.contacts == 1
        assert snapshot.transport_entries == 1
        assert snapshot.consumed_body_bytes == known
        assert snapshot.pending_receipts == 0
        assert snapshot.active == 0
        assert snapshot.observation_complete is True
        assert owner.snapshot()["state"] == "failed"


@pytest.mark.parametrize(
    "fields,body",
    [
        (b"", b'{"ok": true}'),
        (b"Content-Length: 12\r\n", b'{"ok": true}FORBIDDEN-TRAILER'),
        (b"Transfer-Encoding: chunked\r\n", b'c\r\n{"ok": true}\r\n0\r\n\r\n'),
    ],
    ids=["bounded-eof", "declared-shorter", "chunked"],
)
def test_valid_framing_returns_only_framed_bytes(tmp_path, fields, body):
    with WireReceiver([b"HTTP/1.1 200 OK\r\n" + fields + b"\r\n" + body]) as receiver:
        owner = owned_run(tmp_path, receiver)
        with owner:
            assert owned_client(receiver, owner).get_json("/owned") == {"ok": True}
        snapshot = owner.run.snapshot()
        assert snapshot.consumed_body_bytes == JSON_BYTES
        assert snapshot.active == snapshot.pending_receipts == 0


def test_unknown_chunk_progress_closes_admission_without_retry(tmp_path):
    reply = b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\nNOT-A-CHUNK\r\n"
    with WireReceiver([reply]) as receiver:
        owner = owned_run(tmp_path, receiver)
        with pytest.raises(RequestPolicyError) as caught, owner:
            owned_client(receiver, owner, attempts=3).get_json("/owned")
        assert caught.value.reason == "transport"
        assert str(caught.value) == "Controlled upstream request refused."
        assert caught.value.details == {}
        snapshot = owner.run.snapshot()
        assert (receiver.contacts, snapshot.transport_entries) == (1, 1)
        assert snapshot.attempts == 1
        assert (snapshot.active, snapshot.pending_receipts) == (1, 1)
        assert snapshot.consumed_body_bytes == 0
        assert snapshot.observation_complete is False


@pytest.mark.parametrize(
    "length,body,empty", [(0, b"", True), (4, b"nope", False), (4, b"", False)]
)
def test_error_read_completeness_preserves_status_classification(tmp_path, length, body, empty):
    reply = f"HTTP/1.1 404 Private status\r\nContent-Length: {length}\r\n\r\n".encode() + body
    with WireReceiver([reply]) as receiver:
        owner = owned_run(tmp_path, receiver)
        with owner, pytest.raises(UpstreamError) as caught:
            owned_client(receiver, owner).get_json("/owned")
        assert isinstance(caught.value, UpstreamRejectedError)
        assert caught.value.empty_body is empty
        assert caught.value.details["status"] == HTTPStatus.NOT_FOUND
        assert (receiver.contacts, owner.run.snapshot().consumed_body_bytes) == (1, len(body))


def test_cleanup_retains_sanitized_initiating_and_close_failures(tmp_path, monkeypatch):
    original = HTTPResponse.close
    failed = set()

    def close_then_fail(response):
        original(response)
        if id(response) not in failed:
            failed.add(id(response))
            raise OSError("PRIVATE-CLOSE-DETAIL")

    monkeypatch.setattr(HTTPResponse, "close", close_then_fail)
    reply = b'HTTP/1.1 200 OK\r\nContent-Length: 20\r\n\r\n{"ok": true}'
    with WireReceiver([reply]) as receiver:
        owner = owned_run(tmp_path, receiver)
        with pytest.raises(UpstreamError) as caught, owner:
            owned_client(receiver, owner).get_json("/owned")
        error = caught.value
        assert isinstance(error, RequestPolicyError)
        assert type(error).__name__ == "TransportCleanupError"
        assert isinstance(error.initiating, HTTPException)
        assert str(error.initiating) == "Controlled upstream response framing failed."
        assert tuple(map(str, error.cleanup_errors)) == (
            "Controlled upstream transport cleanup failed.",
        )
        assert error.details == {}
        assert "PRIVATE" not in repr(error)
        assert owner.run.snapshot().consumed_body_bytes == JSON_BYTES


@pytest.mark.parametrize("kind", [RuntimeError, KeyboardInterrupt])
@pytest.mark.parametrize("publication_failure", [False, True])
@pytest.mark.parametrize("prior_reason", [None, "response_limit"])
def test_unexpected_cleanup_closes_admission_when_group_is_caught(
    tmp_path, monkeypatch, kind, publication_failure, prior_reason
):
    fault = kind("PRIVATE-CLEANUP-BUG")
    original_close, original_open = HTTPResponse.close, Path.open

    def fail_close(response):
        original_close(response)
        if prior_reason:
            with pytest.raises(RequestPolicyError):
                owner.run.refuse(prior_reason)
        raise fault

    def fail_publication(path, mode="r", *args, **kwargs):
        if publication_failure and path.name == "failed" and mode == "x":
            raise OSError("PRIVATE-PUBLICATION-FAILURE")
        return original_open(path, mode, *args, **kwargs)

    with WireReceiver() as receiver:
        owner = owned_run(tmp_path, receiver)
        outer = None
        try:
            with owner:
                with monkeypatch.context() as patch:
                    patch.setattr(HTTPResponse, "close", fail_close)
                    patch.setattr(Path, "open", fail_publication)
                    with pytest.raises(BaseExceptionGroup) as caught:
                        owned_client(receiver, owner).get_json("/owned")
                errors = caught.value.exceptions
                assert errors[0] is fault
                reasons = [
                    error.reason for error in errors if isinstance(error, RequestPolicyError)
                ]
                assert reasons == (["accounting"] if publication_failure else [])
                with pytest.raises(RequestPolicyError) as denied:
                    owner.run.check_wait(0)
                assert denied.value.reason == "closed"
        except RequestPolicyError as error:
            outer = error
        assert isinstance(outer, RequestPolicyError)
        snapshot = owner.run.snapshot()
        assert snapshot.failure == (prior_reason or "transport")
        assert owner.snapshot()["state"] == "failed"
        assert (snapshot.active, snapshot.pending_receipts, receiver.contacts) == (0, 0, 1)


def test_deadline_checks_after_known_fragment_before_waiting_for_more(tmp_path, monkeypatch):
    clock, finished = [0.0], Event()
    monkeypatch.setattr(bounds, "monotonic", lambda: clock[0])

    def partial(handler):
        clock[0] = 2.0
        handler.wfile.write(b'HTTP/1.1 200 OK\r\nContent-Length: 12\r\n\r\n{"ok": ')
        handler.wfile.flush()
        assert finished.wait(2), "The client did not stop its owned partial response"

    with WireReceiver([partial]) as receiver:
        owner = owned_run(tmp_path, receiver)
        with pytest.raises((UpstreamError, CallDeadlineError)) as caught, owner, deadline_scope(1):
            try:
                owned_client(receiver, owner).get_json("/owned")
            finally:
                finished.set()
        assert isinstance(caught.value, CallDeadlineError)
        snapshot = owner.run.snapshot()
        assert (snapshot.consumed_body_bytes, snapshot.active, snapshot.pending_receipts) == (
            7,
            0,
            0,
        )
        assert receiver.contacts == 1
        assert snapshot.observation_complete is True


@pytest.mark.parametrize(
    "client_limit,policy_limit,refused", [(12, 64, False), (11, 64, True), (64, 11, True)]
)
def test_success_sentinel_records_exact_authorized_bytes(
    tmp_path, client_limit, policy_limit, refused
):
    with WireReceiver() as receiver:
        owner = owned_run(tmp_path, receiver, response_bytes=policy_limit)
        outcome = None
        try:
            with owner:
                outcome = owned_client(receiver, owner, limit=client_limit).get_json("/owned")
        except RequestPolicyError as error:
            outcome = error.reason
        assert outcome == ("response_limit" if refused else {"ok": True})
        snapshot = owner.run.snapshot()
        assert snapshot.consumed_body_bytes == JSON_BYTES
        assert snapshot.active == snapshot.pending_receipts == 0
        assert receiver.contacts == 1


def test_error_prefix_reserves_more_than_small_success_limit(tmp_path):
    reply = b"HTTP/1.1 400 Bad request\r\nContent-Length: 4097\r\n\r\n" + b"x" * 4097
    with WireReceiver([reply]) as receiver:
        owner = owned_run(tmp_path, receiver, response_bytes=8192, total_bytes=16386)
        with owner, pytest.raises(UpstreamRejectedError) as caught:
            owned_client(receiver, owner, limit=12).get_json("/owned")
        assert caught.value.empty_body is False
        assert owner.run.snapshot().consumed_body_bytes == ERROR_PREFIX_BYTES
        assert receiver.contacts == 1


def test_aggregate_shortage_refuses_before_another_contact(tmp_path):
    with WireReceiver() as receiver:
        owner = owned_run(tmp_path, receiver, total_bytes=65)
        with pytest.raises(RequestPolicyError) as caught, owner:
            client = owned_client(receiver, owner)
            assert client.get_json("/owned") == {"ok": True}
            client.get_json("/owned")
        assert caught.value.reason == "byte_limit"
        snapshot = owner.run.snapshot()
        assert (receiver.contacts, snapshot.attempts, snapshot.transport_entries) == (1, 1, 1)
        assert snapshot.consumed_body_bytes == JSON_BYTES


@pytest.mark.parametrize(
    "first",
    [
        b'HTTP/1.1 200 OK\r\nContent-Length: 20\r\n\r\n{"ok": true}',
        b"HTTP/1.1 429 Slow down\r\nRetry-After: 0\r\nRetry-After: 99\r\nContent-Length: 0\r\n\r\n",
        b"HTTP/1.1 503 Unavailable\r\nContent-Length: 0\r\n\r\n",
    ],
    ids=["known-truncation", "first-retry-after", "server-error"],
)
def test_known_retryable_response_spends_a_separate_lease(tmp_path, first):
    with WireReceiver([first]) as receiver:
        owner = owned_run(tmp_path, receiver)
        waits = []
        with owner:
            client = owned_client(receiver, owner, attempts=3)
            client.sleep = waits.append
            assert client.get_json("/owned") == {"ok": True}
        snapshot = owner.run.snapshot()
        assert (
            receiver.contacts,
            snapshot.logical,
            snapshot.attempts,
            snapshot.transport_entries,
        ) == (2, 1, 2, 2)
        assert snapshot.active == snapshot.pending_receipts == 0
        assert all(wait == 0 for wait in waits)
        assert snapshot.consumed_body_bytes == (24 if first.startswith(b"HTTP/1.1 200") else 12)


@pytest.mark.parametrize("body", [b"not-json", b'"\xff"', b'{"apiResponse":{"type":"E"}}'])
def test_provider_preserves_existing_terminal_parser(tmp_path, body):
    reply = f"HTTP/1.1 200 OK\r\nContent-Length: {len(body)}\r\n\r\n".encode() + body
    with WireReceiver([reply]) as receiver:
        owner = owned_run(tmp_path, receiver)
        records = []
        with owner, pytest.raises(PlatformError) as caught:
            client = owned_client(receiver, owner, attempts=3)
            client.on_request = records.append
            client.get_json("/owned")
        assert caught.value.code == "upstream_unavailable"
        assert caught.value.details == {"surface": "owned", "attempts": 1}
        assert [record.failure for record in records] == ["unusable_response"]
        assert (receiver.contacts, owner.run.snapshot().consumed_body_bytes) == (1, len(body))


def test_form_absence_is_interpreted_by_existing_consumer(tmp_path):
    body = b'{"form":null,"apiResponse":{"type":"E"}}'
    reply = f"HTTP/1.1 200 OK\r\nContent-Length: {len(body)}\r\n\r\n".encode() + body
    path = "/NCIFormAPI.v2_0:NciFormApiRad/Form/5406471"
    with WireReceiver([reply]) as receiver:
        grant = RequestGrant("GET", receiver.origin + path, (("accept", "application/json"),))
        owner = SupervisedRun.create(Authorization((grant,), Limits(), "a" * 64), tmp_path)
        with owner, using_request_policy(ControlledHttpPolicy(owner.run)):
            client = CaDSRClient(Settings(cadsr_base_url=receiver.origin))
            with pytest.raises(PlatformError) as caught:
                client.get_form("5406471")
        assert caught.value.code == "not_found"
        assert caught.value.details == {"identifiers": ["5406471"]}
        assert [request[1] for request in receiver.requests] == [path]
        assert owner.run.snapshot().consumed_body_bytes == len(body)


@pytest.mark.parametrize(
    "stage,limit,reason,initiating,active",
    [
        ("close", 64, "transport", None, 0),
        ("close", 11, "response_limit", "RequestPolicyError", 0),
        ("release", 64, "accounting", None, 1),
    ],
)
def test_expected_cleanup_preserves_policy_reason_and_retirement(
    tmp_path, monkeypatch, stage, limit, reason, initiating, active
):
    original = HTTPConnection.close

    def fail_close(connection):
        original(connection)
        raise OSError("PRIVATE-CONNECTION-CLOSE")

    def fail_release(_lease):
        raise RequestPolicyError("accounting")

    if stage == "close":
        monkeypatch.setattr(HTTPConnection, "close", fail_close)
    else:
        monkeypatch.setattr(AttemptLease, "release", fail_release)
    with WireReceiver() as receiver:
        owner = owned_run(tmp_path, receiver)
        with (
            pytest.raises((RequestPolicyError, BaseExceptionGroup)),
            owner,
            pytest.raises(RequestPolicyError) as caught,
        ):
            owned_client(receiver, owner, limit=limit, attempts=3).get_json("/owned")
        error = caught.value
        assert type(error).__name__ == "TransportCleanupError"
        assert error.reason == reason
        assert error.details == {}
        assert (type(error.initiating).__name__ if error.initiating else None) == initiating
        assert [type(item).__name__ for item in error.cleanup_errors] == [
            "OSError" if stage == "close" else "RequestPolicyError"
        ]
        snapshot = owner.run.snapshot()
        assert (snapshot.consumed_body_bytes, snapshot.active, receiver.contacts) == (12, active, 1)
        assert snapshot.observation_complete is (not active)
        assert owner.snapshot()["state"] == "failed"


@pytest.mark.parametrize("limit", [False, 0, -1, 1.5])
def test_malformed_client_limit_refuses_before_reservation_or_io(tmp_path, limit):
    with WireReceiver() as receiver:
        owner = owned_run(tmp_path, receiver)
        with pytest.raises(RequestPolicyError) as caught, owner:
            owned_client(receiver, owner, limit=limit).get_json("/owned")
        assert caught.value.reason == "authorization"
        snapshot = owner.run.snapshot()
        assert (snapshot.attempts, snapshot.transport_entries, receiver.contacts) == (0, 0, 0)


def entry_receipt_fault(owner, monkeypatch, after_commit):
    if not after_commit:
        with (
            closing(sqlite3.connect(owner.run.directory / "ledger.sqlite")) as connection,
            connection,
        ):
            connection.execute(
                "CREATE TRIGGER reject_receipt BEFORE UPDATE OF entered ON leases "
                "WHEN NEW.entered=1 BEGIN SELECT RAISE(FAIL,'synthetic receipt failure'); END"
            )
        return
    original, armed = sqlite3.connect, [True]

    class ReceiptConnection(sqlite3.Connection):
        receipted = False

        def execute(self, sql, *args, **kwargs):
            result = super().execute(sql, *args, **kwargs)
            if sql == "UPDATE run SET entered=entered+1":
                self.receipted = True
            return result

        def close(self):
            super().close()
            if armed[0] and self.receipted:
                armed[0] = False
                raise OSError("PRIVATE-RECEIPT-CLOSE")

    def connect(database, *args, **kwargs):
        ledger = owner.run.directory / "ledger.sqlite"
        if str(database).partition("?")[0] in (str(ledger), ledger.as_uri()):
            kwargs["factory"] = ReceiptConnection
        return original(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", connect)


@pytest.mark.parametrize("after_commit,entries,active,pending", [(False, 0, 1, 1), (True, 1, 0, 0)])
def test_expected_transport_and_entry_receipt_failures_have_fixed_public_record(
    tmp_path, monkeypatch, after_commit, entries, active, pending
):
    with WireReceiver([b"PRIVATE-INVALID-STATUS\r\n\r\n"]) as receiver:
        grant = RequestGrant(
            "GET",
            receiver.origin + "/owned",
            (
                ("accept", "application/json"),
                ("x-correlation-id", "policy-test"),
            ),
        )
        owner = SupervisedRun.create(
            Authorization((grant,), Limits(response_bytes=64), "a" * 64), tmp_path
        )
        entry_receipt_fault(owner, monkeypatch, after_commit)
        result, records = None, []
        with (
            pytest.raises((RequestPolicyError, BaseExceptionGroup, HTTPException)) as outer,
            correlated("policy-test"),
            owner,
        ):
            client = owned_client(receiver, owner, attempts=3)
            client.on_request = records.append
            result = call("controlled-http", lambda: client.get_json("/owned"))
        snapshot = owner.run.snapshot()
        assert (len(records), receiver.contacts, snapshot.transport_entries) == (1, 1, entries)
        assert (snapshot.active, snapshot.pending_receipts, snapshot.consumed_body_bytes) == (
            active,
            pending,
            0,
        )
        assert snapshot.observation_complete is (not pending)
        assert owner.snapshot()["state"] == "failed"
        assert result == {
            "error": {
                "code": "upstream_unavailable",
                "message": "Controlled upstream request refused. Retry later.",
                "correlationId": "policy-test",
            }
        }
        assert "PRIVATE" not in "".join(traceback.format_exception(outer.value))


@pytest.mark.parametrize("kind", [RuntimeError, KeyboardInterrupt])
def test_unexpected_entry_failure_is_not_normalized_with_receipt_failure(
    tmp_path, monkeypatch, kind
):
    fault = kind("PRIVATE-ENTRY-BUG")
    original = HTTPConnection.getresponse

    def response_then_fail(connection):
        original(connection).close()
        raise fault

    with WireReceiver() as receiver:
        owner = owned_run(tmp_path, receiver)
        entry_receipt_fault(owner, monkeypatch, False)
        monkeypatch.setattr(HTTPConnection, "getresponse", response_then_fail)
        with pytest.raises(BaseExceptionGroup) as caught, owner:
            owned_client(receiver, owner).get_json("/owned")
        assert caught.value.exceptions[0] is fault
        assert isinstance(caught.value.exceptions[1], RequestPolicyError)
        snapshot = owner.run.snapshot()
        assert (snapshot.active, snapshot.pending_receipts, receiver.contacts) == (1, 1, 1)


@pytest.mark.parametrize("cleanup", [False, True])
def test_owned_timeout_preserves_caller_and_cleanup_contracts(
    tmp_path, monkeypatch, caplog, cleanup
):
    original_response, original_close = HTTPConnection.getresponse, HTTPConnection.close

    def response_then_timeout(connection):
        original_response(connection).close()
        raise TimeoutError("PRIVATE-TIMEOUT-DETAIL")

    def close_then_fail(connection):
        original_close(connection)
        raise OSError("PRIVATE-CLOSE-DETAIL")

    with WireReceiver() as receiver:
        owner, records = owned_run(tmp_path, receiver), []
        monkeypatch.setattr(HTTPConnection, "getresponse", response_then_timeout)
        if cleanup:
            monkeypatch.setattr(HTTPConnection, "close", close_then_fail)
        client = owned_client(receiver, owner)
        client.on_request = records.append
        with pytest.raises(Exception) as caught, owner:
            client.get_json("/owned")
        error = caught.value
        if cleanup:
            assert type(error) is TransportCleanupError
            assert error.reason == "transport"
            assert type(error.initiating) is TimeoutError
            assert str(error.initiating) == "Controlled upstream transport timed out."
            assert tuple(type(item) for item in error.cleanup_errors) == (OSError,)
            assert tuple(map(str, error.cleanup_errors)) == (
                "Controlled upstream transport cleanup failed.",
            )
            assert error.details == {}
        else:
            assert type(error) is UpstreamTimeoutError
            assert str(error) == "owned request failed for /owned: transport failure"
            assert error.details == {"surface": "owned", "seconds": 1, "attempts": 1}
        assert "PRIVATE" not in repr(error) + str(error.details) + caplog.text
        assert "PRIVATE" not in "".join(traceback.format_exception(error))
        snapshot = owner.run.snapshot()
        assert (len(records), receiver.contacts, snapshot.transport_entries) == (1, 1, 1)
        assert (snapshot.active, snapshot.pending_receipts, snapshot.consumed_body_bytes) == (
            0,
            0,
            0,
        )
        assert snapshot.observation_complete is True
        assert owner.snapshot()["state"] == "failed"


@pytest.mark.parametrize("kind", [OSError, RuntimeError])
def test_cleanup_observation_failure_remains_visible_and_closes_admission(
    tmp_path, monkeypatch, kind
):
    original_close, original_read = HTTPConnection.close, Path.read_text
    request_thread = current_thread()
    observation = kind("PRIVATE-OBSERVATION-DETAIL")
    close_error = RequestPolicyError("response_limit")

    def close_then_fail(connection):
        original_close(connection)
        raise close_error

    def fail_owned_observation(path, *args, **kwargs):
        if path == marker and current_thread() is request_thread and path.exists():
            raise observation
        return original_read(path, *args, **kwargs)

    with WireReceiver() as receiver:
        owner = owned_run(tmp_path, receiver)
        marker = owner.run.directory / "failed"
        with pytest.raises(RequestPolicyError) as terminal, owner:
            with monkeypatch.context() as patch:
                patch.setattr(HTTPConnection, "close", close_then_fail)
                patch.setattr(Path, "read_text", fail_owned_observation)
                with pytest.raises((RequestPolicyError, BaseExceptionGroup)) as caught:
                    owned_client(receiver, owner).get_json("/owned")
            error = caught.value
            if kind is OSError:
                assert type(error) is TransportCleanupError
                assert error.reason == "response_limit"
                assert error.initiating is None
                assert tuple(type(item) for item in error.cleanup_errors) == (
                    RequestPolicyError,
                    OSError,
                )
                assert error.cleanup_errors[0].reason == "response_limit"
                assert tuple(map(str, error.cleanup_errors)) == (
                    "Controlled upstream request refused.",
                    "Controlled upstream transport cleanup failed.",
                )
                assert error.details == {}
                assert "PRIVATE" not in "".join(traceback.format_exception(error))
            else:
                assert type(error) is ExceptionGroup
                assert error.exceptions == (close_error, observation)
                assert error.exceptions[1] is observation
            with pytest.raises(RequestPolicyError) as denied:
                owner.run.check_wait(0)
            assert denied.value.reason == "closed"
        assert terminal.value.reason == "response_limit"
        snapshot = owner.run.snapshot()
        assert snapshot.failure == "response_limit"
        assert (receiver.contacts, snapshot.transport_entries, snapshot.consumed_body_bytes) == (
            1,
            1,
            12,
        )
        assert (snapshot.active, snapshot.pending_receipts, snapshot.observation_complete) == (
            0,
            0,
            True,
        )
        assert owner.snapshot()["state"] == "failed"


@pytest.mark.parametrize("status", [204, 304])
def test_bodyless_status_rejects_mixed_physical_framing(tmp_path, status):
    reply = (
        f"HTTP/1.1 {status} Owned status\r\n".encode()
        + b"Content-Length: 0\r\nTransfer-Encoding: chunked\r\n\r\n0\r\n\r\n"
    )
    with WireReceiver([reply]) as receiver:
        owner = owned_run(tmp_path, receiver)
        with pytest.raises((UpstreamError, PlatformError)) as caught, owner:
            owned_client(receiver, owner, attempts=3).get_json("/owned")
        assert type(caught.value) is RequestPolicyError
        assert caught.value.reason == "transport"
        assert str(caught.value) == "Controlled upstream request refused."
        assert caught.value.details == {}
        snapshot = owner.run.snapshot()
        assert (receiver.contacts, snapshot.transport_entries, snapshot.attempts) == (1, 1, 1)
        assert (snapshot.consumed_body_bytes, snapshot.active, snapshot.pending_receipts) == (
            0,
            0,
            0,
        )
        assert snapshot.failure == "transport"
        assert snapshot.observation_complete is True
        assert owner.snapshot()["state"] == "failed"
