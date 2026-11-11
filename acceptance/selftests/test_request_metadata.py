"""Finite metadata permission cannot broaden request fields or private run identity."""

import pytest
import yaml
from http_receiver import WireReceiver

from nci_si_acceptance import request_catalogue
from nci_si_acceptance.environments import SURFACES, Environment
from nci_si_acceptance.request_authorization import Authorization, ExactRequestFamily
from nci_si_acceptance.request_budget import ControlledRun
from nci_si_acceptance.request_supervision import SupervisedRun
from nci_si_acceptance.request_transport import ControlledHttpPolicy
from nci_si_mcp.http_client import HttpClient, RequestPolicyError
from nci_si_mcp.invocation import call

# Independent complete declaration: each invalid scalar case changes only its named field.
VALID_POLICY = (
    "version: 1\ncorrelation: true\nchannels: [monthly]\n"
    "includes: [summary]\nrelations: [parents]\nexact: [recorded/evs/mapsets.json]\n"
    "paused_queries: [graph-identities, elements-exact, elements-descendants, "
    "values-exact, values-descendants]\n"
)


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
        pytest.param("", id="empty"),
        pytest.param(VALID_POLICY.replace("channels: [monthly]\n", ""), id="missing-channels"),
        pytest.param(VALID_POLICY + "correlation: false\n", id="duplicate"),
        pytest.param(VALID_POLICY + "extra: 1\n", id="unknown"),
        pytest.param(VALID_POLICY.replace("version: 1", "version: true"), id="boolean-version"),
        pytest.param(VALID_POLICY.replace("version: 1", 'version: "1"'), id="string-version"),
        pytest.param(VALID_POLICY.replace("version: 1", "version: 2"), id="version-two"),
        pytest.param(
            VALID_POLICY.replace("correlation: true", "correlation: yes"), id="noncanonical-bool"
        ),
        pytest.param(
            VALID_POLICY.replace("correlation: true", 'correlation: "true"'), id="string-bool"
        ),
        pytest.param(
            VALID_POLICY.replace("correlation: true", "correlation: [true]"), id="sequence-bool"
        ),
        pytest.param("[version, correlation]\n", id="root-sequence"),
        pytest.param(
            VALID_POLICY.replace("mapsets.json", "unknown.json"), id="unknown-exact-reference"
        ),
        pytest.param(
            VALID_POLICY.replace("graph-identities", "unknown-family"), id="unknown-paused-family"
        ),
        pytest.param(VALID_POLICY.replace("[monthly]", "[annual]"), id="unknown-channel"),
        pytest.param(VALID_POLICY.replace("[summary]", "[unreviewed]"), id="unknown-include"),
        pytest.param(VALID_POLICY.replace("[parents]", "[unreviewed]"), id="unknown-relation"),
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


def test_independent_complete_policy_admits_actual_metadata(tmp_path, monkeypatch):
    document = tmp_path / "policy.yaml"
    document.write_text(VALID_POLICY)
    monkeypatch.setattr(request_catalogue, "POLICY", document)
    with WireReceiver() as receiver:
        policy = request_catalogue.compile_policy(selected(receiver.origin))
        owner = SupervisedRun.create(policy.authorization, tmp_path)
        result = call(
            "owned",
            lambda: metadata_request(owner, receiver.origin, "/api/v1/version", None, {}, None),
        )
        assert result == {"ok": True}
        assert receiver.contacts == 1 and len(receiver.requests) == 1
        snapshot = owner.run.snapshot()
        assert (snapshot.logical, snapshot.attempts, snapshot.transport_entries) == (1, 1, 1)
        assert owner.snapshot()["state"] == "completed"


@pytest.mark.parametrize(
    "field,value",
    [("method", "POST"), ("headers", {"Accept": "*/*"}), ("form", {"query": "undeclared"})],
)
def test_selected_record_rejects_unreviewed_executable_fields(tmp_path, monkeypatch, field, value):
    manifest = yaml.safe_load(request_catalogue.MANIFEST.read_text())
    row = next(
        row
        for row in manifest["record"]["requests"]
        if row["fixture"] == "recorded/evs/mapsets.json"
    )
    row[field] = value
    document = tmp_path / "manifest.yaml"
    document.write_text(yaml.safe_dump(manifest))
    monkeypatch.setattr(request_catalogue, "MANIFEST", document)
    with WireReceiver() as receiver:
        with pytest.raises(RequestPolicyError) as refused:
            request_catalogue.compile_policy(selected(receiver.origin))
        assert refused.value.reason == "authorization" and receiver.contacts == 0


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
