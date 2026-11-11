"""Reviewed finite forms reach only their selected destination with immutable metadata."""

from http_receiver import WireReceiver

from nci_si_acceptance.environments import SURFACES, Environment
from nci_si_acceptance.request_catalogue import compile_policy
from nci_si_acceptance.request_supervision import SupervisedRun
from nci_si_acceptance.request_transport import ControlledHttpPolicy
from nci_si_mcp.errors import correlated
from nci_si_mcp.http_client import HttpClient
from nci_si_mcp.invocation import call

# Two separately correlated calls each receive the independently twelve-byte response.
EXPECTED_CONTACTS = 2
EXPECTED_BODY_BYTES = 24


def correlated_responses(owner, origin, identifiers):
    with owner:
        client = HttpClient(
            origin,
            label="owned",
            timeout_seconds=1,
            max_attempts=1,
            retry_backoff_seconds=0,
            max_response_bytes=64,
            size_bound="owned",
            request_policy=ControlledHttpPolicy(owner.run),
        )
        results = []
        for identifier in identifiers:
            with correlated(identifier):
                results.append(client.get_json("/api/v1/version"))
        return results


def test_correlation_is_preserved_without_broadening_headers(tmp_path):
    with WireReceiver() as receiver:
        environment = Environment(
            "evs-only",
            {surface: receiver.origin if surface == "evs" else None for surface in SURFACES},
            {},
            "e" * 64,
        )
        policy = compile_policy(environment)
        owner = SupervisedRun.create(policy.authorization, tmp_path)
        identifiers = ("owned-call-1", "owned-call-2")
        with correlated("owned-invocation"):
            result = call(
                "finite-metadata", lambda: correlated_responses(owner, receiver.origin, identifiers)
            )
        snapshot = owner.run.snapshot()
        assert result == [{"ok": True}, {"ok": True}], {
            "contacts": receiver.contacts,
            "logical": snapshot.logical,
            "attempts": snapshot.attempts,
            "entries": snapshot.transport_entries,
        }
        assert receiver.contacts == EXPECTED_CONTACTS
        assert [(method, target, body) for method, target, _headers, body in receiver.requests] == [
            ("GET", "/api/v1/version", b""),
            ("GET", "/api/v1/version", b""),
        ]
        for (_method, _target, headers, _body), identifier in zip(
            receiver.requests, identifiers, strict=True
        ):
            assert sorted((key.lower(), value) for key, value in headers) == sorted(
                [
                    ("accept", "application/json"),
                    ("x-correlation-id", identifier),
                    ("host", receiver.origin.removeprefix("http://")),
                    ("accept-encoding", "identity"),
                    ("connection", "close"),
                ]
            )
        assert (snapshot.logical, snapshot.attempts, snapshot.transport_entries) == (2, 2, 2)
        assert (
            snapshot.consumed_body_bytes == EXPECTED_BODY_BYTES
            and owner.snapshot()["state"] == "completed"
        )
