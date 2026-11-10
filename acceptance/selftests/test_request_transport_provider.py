"""Production provider contracts observed through HttpClient and real owned sockets."""

from importlib import import_module, util

import pytest
from http_receiver import WireReceiver

from nci_si_acceptance.request_authorization import Authorization, Limits, RequestGrant
from nci_si_acceptance.request_supervision import SupervisedRun
from nci_si_mcp.http_client import HttpClient


def provider(run):
    name = "nci_si_acceptance.request_transport"
    assert util.find_spec(name) is not None, "The authorized production HTTP provider must exist"
    return import_module(name).ControlledHttpPolicy(run)


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_exact_authorized_request_reaches_owned_receiver(tmp_path, monkeypatch, method):
    with WireReceiver() as receiver, WireReceiver() as proxy:
        for name in (
            "http_proxy",
            "HTTP_PROXY",
            "https_proxy",
            "HTTPS_PROXY",
            "all_proxy",
            "ALL_PROXY",
        ):
            monkeypatch.setenv(name, proxy.origin)
        for name in ("no_proxy", "NO_PROXY"):
            monkeypatch.setenv(name, "")
        headers = (("accept", "application/json"),)
        body = None
        if method == "POST":
            headers += (("content-type", "application/json"),)
            body = b'{"query": "alpha"}'
        grant = RequestGrant(method, receiver.origin + "/owned?raw=a%20b&x=1", headers, body)
        owner = SupervisedRun.create(
            Authorization((grant,), Limits(response_bytes=64, total_bytes=8194), "a" * 64), tmp_path
        )
        with owner:
            client = HttpClient(
                receiver.origin,
                label="owned",
                max_attempts=1,
                timeout_seconds=1,
                retry_backoff_seconds=0,
                max_response_bytes=64,
                size_bound="owned",
                request_policy=provider(owner.run),
            )
            result = (
                client.get_json("/owned?raw=a%20b&x=1")
                if method == "GET"
                else client.post_json("/owned?raw=a%20b&x=1", {"query": "alpha"})
            )
        assert result == {"ok": True}
        assert receiver.contacts == 1
        assert proxy.contacts == 0
        assert len(receiver.requests) == 1
        actual_method, target, physical, actual_body = receiver.requests[0]
        expected_headers = [
            *headers,
            ("host", receiver.origin.removeprefix("http://")),
            ("accept-encoding", "identity"),
            ("connection", "close"),
        ]
        if body is not None:
            expected_headers.append(("content-length", "18"))
        assert (actual_method, target, actual_body) == (method, "/owned?raw=a%20b&x=1", body or b"")
        assert sorted((key.lower(), value) for key, value in physical) == sorted(expected_headers)
        snapshot = owner.run.snapshot()
        assert (
            snapshot.logical,
            snapshot.attempts,
            snapshot.transport_entries,
            snapshot.consumed_body_bytes,
        ) == (
            1,
            1,
            1,
            12,
        )
