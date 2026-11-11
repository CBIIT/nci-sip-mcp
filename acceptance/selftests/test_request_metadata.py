"""Finite metadata permission cannot broaden request fields or private run identity."""

import pytest
from http_receiver import WireReceiver

from nci_si_acceptance import request_catalogue
from nci_si_acceptance.environments import SURFACES, Environment
from nci_si_acceptance.request_authorization import Authorization, ExactRequestFamily
from nci_si_acceptance.request_budget import ControlledRun
from nci_si_acceptance.request_supervision import SupervisedRun
from nci_si_acceptance.request_transport import ControlledHttpPolicy
from nci_si_mcp.http_client import HttpClient, RequestPolicyError
from nci_si_mcp.invocation import call


def selected(origin):
    return Environment(
        "evs-only",
        {surface: origin if surface == "evs" else None for surface in SURFACES},
        {},
        "e" * 64,
    )


def metadata_request(owner, origin, path, params, headers, body):
    with owner:
        client = HttpClient(
            origin,
            label="owned",
            timeout_seconds=1,
            max_attempts=1,
            retry_backoff_seconds=0,
            max_response_bytes=64,
            size_bound="owned",
            credentials=headers,
            request_policy=ControlledHttpPolicy(owner.run),
        )
        return client.get_json(path, params) if body is None else client.post_json(path, body)


@pytest.mark.parametrize(
    "path,params,headers,body",
    [
        ("/api/v1/version", {"extra": "1"}, {}, None),
        ("/api/v1/version?", None, {}, None),
        ("/api/v1/version/child", None, {}, None),
        ("/api/v1/version", None, {"Accept": "text/plain"}, None),
        ("/api/v1/version", None, {"X-Not-Reviewed": "private-metadata-canary"}, None),
        ("/api/v1/version", None, {"X-Correlation-ID": ""}, None),
        ("/api/v1/version", None, {"X-Correlation-ID": "private\nmetadata"}, None),
        ("/api/v1/version", None, {"Host": "other.invalid"}, None),
        ("/api/v1/version", None, {}, {"unreviewed": True}),
    ],
)
def test_metadata_family_refuses_changed_request_fields(tmp_path, path, params, headers, body):
    with WireReceiver() as receiver:
        policy = request_catalogue.compile_policy(selected(receiver.origin))
        owner = SupervisedRun.create(policy.authorization, tmp_path)
        result = call(
            "owned", lambda: metadata_request(owner, receiver.origin, path, params, headers, body)
        )
        assert result.get("error", {}).get("code") == "upstream_unavailable"
        assert set(result["error"]) == {"code", "message", "correlationId"}
        assert "private-metadata-canary" not in str(result)
        assert receiver.contacts == 0 and receiver.requests == []
        snapshot = owner.run.snapshot()
        assert (snapshot.logical, snapshot.attempts, snapshot.transport_entries) == (0, 0, 0)
        assert owner.snapshot()["state"] == "failed"


def test_metadata_family_refuses_another_origin(tmp_path):
    with WireReceiver() as selected_receiver, WireReceiver() as other:
        policy = request_catalogue.compile_policy(selected(selected_receiver.origin))
        owner = SupervisedRun.create(policy.authorization, tmp_path)
        result = call(
            "owned",
            lambda: metadata_request(owner, other.origin, "/api/v1/version", None, {}, None),
        )
        assert result.get("error", {}).get("code") == "upstream_unavailable"
        assert selected_receiver.contacts == other.contacts == 0


@pytest.mark.parametrize(
    "text",
    [
        "",
        "version: 1\ncorrelation: false\ncorrelation: true\n",
        "version: 1\ncorrelation: true\nextra: 1\n",
        "version: true\ncorrelation: true\n",
        "version: 2\ncorrelation: true\n",
        "version: 1\ncorrelation: yes\n",
        "version: 1\ncorrelation: [true]\n",
        "[version, correlation]\n",
    ],
)
def test_invalid_reviewed_policy_refuses_before_any_contact(tmp_path, monkeypatch, text):
    document = tmp_path / "policy.yaml"
    document.write_text(text)
    monkeypatch.setattr(request_catalogue, "POLICY", document)
    with WireReceiver() as receiver:
        with pytest.raises(RequestPolicyError) as refused:
            request_catalogue.compile_policy(selected(receiver.origin))
        assert refused.value.reason == "authorization"
        assert receiver.contacts == 0


def test_family_default_and_binding_keep_strict_authority(tmp_path):
    with WireReceiver() as receiver:
        policy = request_catalogue.compile_policy(selected(receiver.origin))
        original = policy.authorization
        strict = Authorization(
            original.grants,
            original.limits,
            original.policy_sha256,
            families=(ExactRequestFamily(original.grants[0]),),
        )
        owner = SupervisedRun.create(strict, tmp_path / "strict")
        result = call(
            "owned",
            lambda: metadata_request(
                owner, receiver.origin, "/api/v1/version", None, {"X-Correlation-ID": "owned"}, None
            ),
        )
        assert result.get("error", {}).get("code") == "upstream_unavailable"
        assert receiver.contacts == 0
        bound = SupervisedRun.create(original, tmp_path / "bound")
        with pytest.raises(RequestPolicyError) as refused, bound:
            bound.run.authorize_participant("different-policy")
            ControlledRun.attach(bound.run.descriptor, strict, participant="different-policy")
        assert refused.value.reason == "authorization"
        assert receiver.contacts == 0 and bound.snapshot()["state"] == "failed"
