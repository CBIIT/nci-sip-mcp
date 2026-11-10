"""Physical HTTP framing must not turn partial or ambiguous bytes into success."""

from http import HTTPStatus
from http.client import HTTPException, HTTPResponse
from pathlib import Path
from threading import Event

import pytest
from http_receiver import WireReceiver

from nci_si_acceptance.request_authorization import Authorization, Limits, RequestGrant
from nci_si_acceptance.request_supervision import SupervisedRun
from nci_si_acceptance.request_transport import ControlledHttpPolicy
from nci_si_mcp import bounds
from nci_si_mcp.bounds import CallDeadlineError, deadline_scope
from nci_si_mcp.http_client import (
    HttpClient,
    RequestPolicyError,
    UpstreamError,
    UpstreamRejectedError,
)

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
