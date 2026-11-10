"""Physical HTTP framing must not turn partial or ambiguous bytes into success."""

from http import HTTPStatus
from http.client import HTTPException, HTTPResponse

import pytest
from http_receiver import WireReceiver

from nci_si_acceptance.request_authorization import Authorization, Limits, RequestGrant
from nci_si_acceptance.request_supervision import SupervisedRun
from nci_si_acceptance.request_transport import ControlledHttpPolicy
from nci_si_mcp.http_client import (
    HttpClient,
    RequestPolicyError,
    UpstreamError,
    UpstreamRejectedError,
)

# The independently scripted JSON document below has twelve wire bytes.
JSON_BYTES = 12


def owned_client(receiver, owner, *, limit=64):
    return HttpClient(
        receiver.origin,
        label="owned",
        timeout_seconds=1,
        max_attempts=1,
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
        with pytest.raises(UpstreamError), owner:
            owned_client(receiver, owner).get_json("/owned")
        snapshot = owner.run.snapshot()
        assert (receiver.contacts, snapshot.transport_entries) == (1, 1)
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
