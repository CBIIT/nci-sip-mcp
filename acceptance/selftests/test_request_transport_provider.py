"""Production provider contracts observed through HttpClient and real owned sockets."""

import ssl
from contextlib import contextmanager
from importlib import import_module, util
from urllib.request import Request

import pytest
from http_receiver import WireReceiver, tls_certificate

from nci_si_acceptance.request_authorization import Authorization, Limits, RequestGrant
from nci_si_acceptance.request_supervision import SupervisedRun
from nci_si_mcp.errors import correlated
from nci_si_mcp.http_client import HttpClient, RequestPolicyError, UpstreamError
from nci_si_mcp.invocation import call


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


class MutatingPolicy:
    def __init__(self, policy, other):
        self.policy, self.other = policy, other

    @contextmanager
    def request(self, request):
        with self.policy.request(request) as controlled:
            request.full_url = self.other + "/changed"
            request.method, request.data = "POST", b"changed"
            request.headers.clear()
            request.add_unredirected_header("Authorization", "PRIVATE-MUTATION")
            yield controlled


def test_admitted_snapshot_keeps_wire_and_audit_truthful(tmp_path):
    with WireReceiver() as receiver, WireReceiver() as other:
        grant = RequestGrant("GET", receiver.origin + "/owned", (("accept", "application/json"),))
        owner = SupervisedRun.create(Authorization((grant,), Limits(), "a" * 64), tmp_path)
        records = []
        with owner:
            client = HttpClient(
                receiver.origin,
                label="owned",
                timeout_seconds=1,
                max_attempts=1,
                retry_backoff_seconds=0,
                max_response_bytes=64,
                size_bound="owned",
                request_policy=MutatingPolicy(provider(owner.run), other.origin),
            )
            client.on_request = records.append
            assert client.get_json("/owned") == {"ok": True}
        assert (receiver.contacts, other.contacts) == (1, 0)
        method, target, fields, body = receiver.requests[0]
        assert (method, target, body) == ("GET", "/owned", b"")
        assert "authorization" not in {name.lower() for name, _ in fields}
        assert [(record.method, record.url) for record in records] == [
            ("GET", receiver.origin + "/owned")
        ]


@pytest.mark.parametrize("field", ["method", "url", "headers", "data", "duplicate"])
def test_unmatched_request_refuses_before_logical_admission(tmp_path, field):
    with WireReceiver() as receiver:
        grant = RequestGrant(
            "GET", receiver.origin + "/owned?x=1", (("accept", "application/json"),)
        )
        owner = SupervisedRun.create(Authorization((grant,), Limits(), "a" * 64), tmp_path)
        request = Request(grant.url, headers={"Accept": "application/json"})  # noqa: S310 - owned HTTP
        changes = {"method": "POST", "url": receiver.origin + "/owned?x=2", "data": b"changed"}
        if field in changes:
            setattr(request, "full_url" if field == "url" else field, changes[field])
        elif field == "headers":
            request.add_header("X-Extra", "ungranted")
        else:
            request.add_unredirected_header("Accept", "application/json")
        with pytest.raises(RequestPolicyError), owner, provider(owner.run).request(request):
            pytest.fail("Unmatched requests must never be admitted")
        snapshot = owner.run.snapshot()
        assert (receiver.contacts, snapshot.logical, snapshot.transport_entries) == (0, 0, 0)


@pytest.mark.parametrize(
    "name",
    [
        "Host",
        "Content-Length",
        "Transfer-Encoding",
        "Connection",
        "Proxy-Authorization",
        "Proxy-Connection",
        "Expect",
        "Upgrade",
        "TE",
        "Trailer",
        "Accept-Encoding",
        "Cookie",
    ],
)
def test_transport_headers_are_refused_even_when_granted(tmp_path, name):
    with WireReceiver() as receiver:
        grant = RequestGrant("GET", receiver.origin + "/owned", ((name, "override"),))
        owner = SupervisedRun.create(Authorization((grant,), Limits(), "a" * 64), tmp_path)
        request = Request(grant.url, headers={name: "override"})  # noqa: S310 - owned HTTP
        with pytest.raises(RequestPolicyError), owner, provider(owner.run).request(request):
            pytest.fail("Transport framing overrides must never be admitted")
        assert receiver.contacts == owner.run.snapshot().logical == 0


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
@pytest.mark.parametrize("same_origin", [False, True])
def test_redirect_never_moves_credentials_or_exposes_private_fields(tmp_path, status, same_origin):
    secret = "synthetic-private-credential"  # noqa: S105 - synthetic loopback fixture only
    with WireReceiver() as receiver, WireReceiver() as other:
        destination = (receiver.origin if same_origin else other.origin) + "/outside?PRIVATE-QUERY"
        receiver.replies.append(
            (
                f"HTTP/1.1 {status} PRIVATE-STATUS\r\nLocation: {destination}\r\n"
                "Content-Length: 0\r\n\r\n"
            ).encode()
        )
        grant = RequestGrant(
            "GET",
            receiver.origin + "/allowed",
            (
                ("accept", "application/json"),
                ("authorization", secret),
                ("x-correlation-id", "policy-test"),
            ),
            credential_origin=receiver.origin + "/allowed",
        )
        owner = SupervisedRun.create(Authorization((grant,), Limits(), "a" * 64), tmp_path)
        with correlated("policy-test"), owner:
            client = HttpClient(
                receiver.origin,
                label="owned",
                timeout_seconds=1,
                max_attempts=3,
                retry_backoff_seconds=0,
                max_response_bytes=64,
                size_bound="owned",
                credentials={"Authorization": secret},
                request_policy=provider(owner.run),
            )
            result = call("controlled-http", lambda: client.get_json("/allowed"))
        assert result == {
            "error": {
                "code": "upstream_unavailable",
                "message": "Controlled upstream request refused. Retry later.",
                "correlationId": "policy-test",
            }
        }
        assert (receiver.contacts, other.contacts, owner.run.snapshot().attempts) == (1, 0, 1)
        fields = {name.lower(): value for name, value in receiver.requests[0][2]}
        assert fields["authorization"] == secret
        assert receiver.requests[0][1] == "/allowed"
        assert secret not in repr(grant)


@pytest.mark.parametrize("trusted", [False, True])
def test_tls_verification_precedes_credential_http(tmp_path, monkeypatch, trusted):
    server_context, certificate = tls_certificate(tmp_path)
    if trusted:
        client_context = ssl.create_default_context(cafile=certificate)
        assert client_context.check_hostname is True
        assert client_context.verify_mode == ssl.CERT_REQUIRED
        monkeypatch.setattr(ssl, "_create_default_https_context", lambda: client_context)
    with WireReceiver(tls=server_context) as receiver:
        credential = "synthetic-loopback-only"
        grant = RequestGrant(
            "GET",
            receiver.origin + "/owned",
            (
                ("accept", "application/json"),
                ("authorization", credential),
            ),
            credential_origin=receiver.origin,
        )
        owner = SupervisedRun.create(Authorization((grant,), Limits(), "a" * 64), tmp_path)
        result = None
        with owner:
            client = HttpClient(
                receiver.origin,
                label="owned",
                timeout_seconds=1,
                max_attempts=1,
                retry_backoff_seconds=0,
                max_response_bytes=64,
                size_bound="owned",
                credentials={"Authorization": credential},
                request_policy=provider(owner.run),
            )
            try:
                result = client.get_json("/owned")
            except UpstreamError as error:
                result = type(error).__name__
        assert result == ({"ok": True} if trusted else "UpstreamUnavailableError")
        assert receiver.contacts == 1
        assert len(receiver.requests) == int(trusted)
        if trusted:
            assert dict(receiver.requests[0][2])["authorization"] == credential
        assert owner.run.snapshot().transport_entries == 1
