"""The compiler's explicit legacy grant view keeps strict delivered admission."""

import pytest
from http_receiver import WireReceiver

from nci_si_acceptance.environments import SURFACES, Environment
from nci_si_acceptance.request_authorization import Authorization
from nci_si_acceptance.request_catalogue import compile_policy
from nci_si_acceptance.request_supervision import SupervisedRun
from nci_si_acceptance.request_transport import ControlledHttpPolicy
from nci_si_mcp.errors import correlated
from nci_si_mcp.http_client import HttpClient, RequestPolicyError


def test_catalogue_preparation_preserves_exact_grant_admission(tmp_path):
    with WireReceiver() as receiver:
        environment = Environment(
            "evs-only",
            {surface: receiver.origin if surface == "evs" else None for surface in SURFACES},
            {},
            "e" * 64,
        )
        policy = compile_policy(environment)
        # The explicit legacy view retains exact semantics when families are added later.
        exact = Authorization(
            policy.authorization.grants,
            policy.authorization.limits,
            policy.authorization.policy_sha256,
        )
        plain = SupervisedRun.create(exact, tmp_path / "plain")
        with plain:
            client = HttpClient(
                receiver.origin,
                label="owned",
                timeout_seconds=1,
                max_attempts=1,
                retry_backoff_seconds=0,
                max_response_bytes=64,
                size_bound="owned",
                request_policy=ControlledHttpPolicy(plain.run),
            )
            assert client.get_json("/api/v1/version") == {"ok": True}
        strict = SupervisedRun.create(exact, tmp_path / "strict")
        with pytest.raises(RequestPolicyError) as refused, strict:
            client = HttpClient(
                receiver.origin,
                label="owned",
                timeout_seconds=1,
                max_attempts=1,
                retry_backoff_seconds=0,
                max_response_bytes=64,
                size_bound="owned",
                request_policy=ControlledHttpPolicy(strict.run),
            )
            with correlated("not-in-the-exact-grant"):
                client.get_json("/api/v1/version")
        assert refused.value.reason == "authorization"
        assert receiver.contacts == 1
        assert [(method, path, body) for method, path, _headers, body in receiver.requests] == [
            ("GET", "/api/v1/version", b"")
        ]
        assert sorted((key.lower(), value) for key, value in receiver.requests[0][2]) == sorted(
            [
                ("accept", "application/json"),
                ("host", receiver.origin.removeprefix("http://")),
                ("accept-encoding", "identity"),
                ("connection", "close"),
            ]
        )
        denied = strict.run.snapshot()
        assert (denied.logical, denied.attempts, denied.transport_entries) == (0, 0, 0)
        assert plain.snapshot()["state"] == "completed" and strict.snapshot()["state"] == "failed"
