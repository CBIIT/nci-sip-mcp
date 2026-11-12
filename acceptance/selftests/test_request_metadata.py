"""Finite metadata permission cannot broaden request fields or private run identity."""

from dataclasses import replace

import pytest
import yaml
from http_receiver import WireReceiver

from nci_si_acceptance import request_catalogue
from nci_si_acceptance.environments import SURFACES, Environment
from nci_si_acceptance.request_authorization import Authorization, ExactRequestFamily, RequestGrant
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
        pytest.param(
            VALID_POLICY.replace("[summary]", "[!!int summary]"), id="non-string-include-tag"
        ),
        pytest.param(VALID_POLICY.replace("[parents]", "[unreviewed]"), id="unknown-relation"),
        pytest.param(VALID_POLICY.replace("[monthly]", "[]"), id="empty-channels"),
        pytest.param(VALID_POLICY.replace("[monthly]", "monthly"), id="scalar-channels"),
        pytest.param(VALID_POLICY.replace("[recorded/evs/mapsets.json]", "[]"), id="empty-exact"),
        pytest.param(
            VALID_POLICY.replace("mapsets.json]", "mapsets.json, recorded/evs/mapsets.json]"),
            id="duplicate-exact",
        ),
        pytest.param(
            VALID_POLICY.replace("graph-identities,", "graph-identities, graph-identities,"),
            id="duplicate-paused",
        ),
        pytest.param(
            VALID_POLICY.replace("[summary]", "[!!str {bad: value}]"), id="text-tag-mapping"
        ),
        pytest.param(
            VALID_POLICY.replace("[summary]", "[!!str [summary]]"), id="text-tag-sequence"
        ),
        pytest.param(
            VALID_POLICY.replace("[monthly]", "[monthly, monthly]"), id="duplicate-channels"
        ),
        pytest.param(
            VALID_POLICY.replace(
                "recorded/evs/mapsets.json", "recorded/evs/search-go-obsolete.json"
            ),
            id="ungranted-go-reference",
        ),
    ],
)
def test_invalid_reviewed_policy_refuses_before_any_contact(tmp_path, monkeypatch, text):
    document = tmp_path / "policy.yaml"
    document.write_text(VALID_POLICY)
    monkeypatch.setattr(request_catalogue, "POLICY", document)
    with WireReceiver() as receiver:
        request_catalogue.compile_policy(selected(receiver.origin))
        document.write_text(text)
        with pytest.raises(Exception) as refused:
            request_catalogue.compile_policy(selected(receiver.origin))
        assert (type(refused.value), str(refused.value), refused.value.__cause__) == (
            RequestPolicyError,
            "Controlled upstream request refused.",
            None,
        )
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


@pytest.mark.parametrize(
    "path,value",
    [
        (("manifest", "record", "requests"), 7),
        (("manifest", "record", "requests"), [7]),
        (("manifest", "record", "requests"), [["not-a-row"]]),
        (("manifest", "record", "requests"), None),
        (("manifest", "evs", "release"), "not-a-release"),
        (("row", "path"), {}),
        (("row", "path"), "http://other.invalid/api/v1/mapset"),
        (("row", "path"), "/api/v1/mapset?include=minimal&include=full"),
        (("row", "path"), "/api/v1/mapset?bare-member"),
        (("row", "surface"), "cadsr"),
    ],
)
def test_malformed_manifest_refuses_before_any_contact(tmp_path, monkeypatch, path, value):
    manifest = yaml.safe_load(request_catalogue.MANIFEST.read_text())
    row = next(
        row
        for row in manifest["record"]["requests"]
        if row["fixture"] == "recorded/evs/mapsets.json"
    )
    parent = {"manifest": manifest, "row": row}
    for key in path[:-1]:
        parent = parent[key]
    document = tmp_path / "manifest.yaml"
    document.write_text(yaml.safe_dump(manifest))
    declaration = tmp_path / "policy.yaml"
    declaration.write_text(VALID_POLICY)
    monkeypatch.setattr(request_catalogue, "MANIFEST", document)
    monkeypatch.setattr(request_catalogue, "POLICY", declaration)
    with WireReceiver() as receiver:
        request_catalogue.compile_policy(selected(receiver.origin))
        # None requests omission; the successful control differs only at this boundary.
        if value is None:
            del parent[path[-1]]
        else:
            parent[path[-1]] = value
        document.write_text(yaml.safe_dump(manifest))
        with pytest.raises(Exception) as refused:
            request_catalogue.compile_policy(selected(receiver.origin))
        assert (type(refused.value), str(refused.value), refused.value.__cause__) == (
            RequestPolicyError,
            "Controlled upstream request refused.",
            None,
        )
        assert refused.value.reason == "authorization" and receiver.contacts == 0


@pytest.mark.parametrize("change", ["duplicate", "blank-query"])
def test_selected_fixture_rows_preserve_exact_membership_and_query(tmp_path, monkeypatch, change):
    manifest = yaml.safe_load(request_catalogue.MANIFEST.read_text())
    row = next(
        row
        for row in manifest["record"]["requests"]
        if row["fixture"] == "recorded/evs/mapsets.json"
    )
    document, declaration = tmp_path / "manifest.yaml", tmp_path / "policy.yaml"
    document.write_text(yaml.safe_dump(manifest))
    declaration.write_text(VALID_POLICY)
    monkeypatch.setattr(request_catalogue, "MANIFEST", document)
    monkeypatch.setattr(request_catalogue, "POLICY", declaration)
    origin = "http://127.0.0.1:9"
    request_catalogue.compile_policy(selected(origin))
    if change == "duplicate":
        manifest["record"]["requests"].append(dict(row))
        document.write_text(yaml.safe_dump(manifest))
        with pytest.raises(Exception) as refused:
            request_catalogue.compile_policy(selected(origin))
    else:
        row["path"] = "/api/v1/mapset?include="
        document.write_text(yaml.safe_dump(manifest))
        policy = request_catalogue.compile_policy(selected(origin))
        headers = (("accept", "application/json"),)
        try:
            admitted = policy.authorization.admit("GET", origin + row["path"], headers, None)
        except RequestPolicyError as error:
            admitted = error
        assert admitted == RequestGrant(
            "GET", origin + "/api/v1/mapset?include=", headers, credential_origin=origin
        )
        with pytest.raises(Exception) as refused:
            policy.authorization.admit("GET", origin + "/api/v1/mapset", headers, None)
    assert (type(refused.value), str(refused.value), refused.value.__cause__) == (
        RequestPolicyError,
        "Controlled upstream request refused.",
        None,
    )
    assert refused.value.reason == "authorization"


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


def test_duplicate_registered_query_id_refuses_before_any_contact(tmp_path, monkeypatch):
    register = yaml.safe_load(request_catalogue.REGISTER.read_text())
    document = tmp_path / "register.yaml"
    document.write_text(yaml.safe_dump(register))
    declaration = tmp_path / "policy.yaml"
    declaration.write_text(VALID_POLICY)
    monkeypatch.setattr(request_catalogue, "REGISTER", document)
    monkeypatch.setattr(request_catalogue, "POLICY", declaration)
    with WireReceiver() as receiver:
        # Prove the independently valid declaration and copied register before the one defect.
        request_catalogue.compile_policy(selected(receiver.origin))
        register["queries"].append(dict(register["queries"][0]))
        expected_ids = {
            "graph-identities",
            "elements-exact",
            "elements-descendants",
            "values-exact",
            "values-descendants",
        }
        assert {row["id"] for row in register["queries"]} == expected_ids
        assert len(register["queries"]) == len(expected_ids) + 1
        document.write_text(yaml.safe_dump(register))
        with pytest.raises(RequestPolicyError) as refused:
            request_catalogue.compile_policy(selected(receiver.origin))
        assert refused.value.reason == "authorization"
        assert receiver.contacts == 0 and receiver.requests == []


@pytest.mark.parametrize("invalid", ["type", "name", "hash", "origin", "absent-evs"])
def test_invalid_environment_has_sanitized_compiler_refusal(invalid):
    with WireReceiver() as receiver:
        valid = selected(receiver.origin)
        request_catalogue.compile_policy(valid)
        changes = {
            "name": {"name": "INVALID"},
            "hash": {"policy_sha256": "invalid"},
            "origin": {"origins": {**valid.origins, "evs": "invalid"}},
            "absent-evs": {"origins": {**valid.origins, "evs": None}},
        }
        value = object() if invalid == "type" else replace(valid, **changes[invalid])
        with pytest.raises(Exception) as refused:
            request_catalogue.compile_policy(value)
        assert (type(refused.value), str(refused.value), refused.value.__cause__) == (
            RequestPolicyError,
            "Controlled upstream request refused.",
            None,
        )
        assert refused.value.reason == "authorization"
        assert receiver.contacts == 0 and receiver.requests == []


def test_duplicate_fixture_key_in_unselected_row_refuses(tmp_path, monkeypatch):
    raw = request_catalogue.MANIFEST.read_text()
    marker = "    - fixture: recorded/evs/version.json\n"
    assert raw.count(marker) == 1
    declaration = tmp_path / "policy.yaml"
    declaration.write_text(VALID_POLICY)
    document = tmp_path / "manifest.yaml"
    document.write_text(raw)
    monkeypatch.setattr(request_catalogue, "POLICY", declaration)
    monkeypatch.setattr(request_catalogue, "MANIFEST", document)
    with WireReceiver() as receiver:
        request_catalogue.compile_policy(selected(receiver.origin))
        duplicate = marker + "      fixture: recorded/evs/version.json\n"
        document.write_text(raw.replace(marker, duplicate, 1))
        with pytest.raises(Exception) as refused:
            request_catalogue.compile_policy(selected(receiver.origin))
        assert (type(refused.value), str(refused.value), refused.value.__cause__) == (
            RequestPolicyError,
            "Controlled upstream request refused.",
            None,
        )
        assert refused.value.reason == "authorization"
        assert receiver.contacts == 0 and receiver.requests == []
