"""Physical HTTP framing must not turn partial or ambiguous bytes into success."""

import pytest
from http_receiver import WireReceiver

from nci_si_acceptance.request_authorization import Authorization, Limits, RequestGrant
from nci_si_acceptance.request_supervision import SupervisedRun
from nci_si_acceptance.request_transport import ControlledHttpPolicy
from nci_si_mcp.http_client import HttpClient, UpstreamError

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
    ],
    ids=[
        "truncated-valid-json",
        "conflicting-length",
        "conflicting-framing",
        "unsupported-encoding",
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
