"""The explicit diagnostic caller's private authority and ledger record contracts."""

from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest

from nci_si_acceptance.request_authorization import (
    Authorization,
    Limits,
    RequestGrant,
    RunDescriptor,
)
from nci_si_mcp.http_client import RequestPolicyError


def refusal(construct):
    try:
        construct()
    except (
        RequestPolicyError,
        TypeError,
        ValueError,
        AttributeError,
        OverflowError,
        KeyError,
    ) as error:
        return type(error), str(error), error.__cause__
    return None


@pytest.mark.parametrize("value", [None, False, 3, b"a" * 64])
def test_malformed_public_policy_identity_is_terminal(value):
    grant = RequestGrant("GET", "http://127.0.0.1:9/owned", ())
    assert refusal(lambda: Authorization((grant,), Limits(), value)) == (
        RequestPolicyError,
        "Controlled upstream request refused.",
        None,
    )


@pytest.mark.parametrize("value", [None, 3, b"http://127.0.0.1:9/owned"])
def test_malformed_request_destination_is_terminal(value):
    assert refusal(lambda: RequestGrant("GET", value, ())) == (
        RequestPolicyError,
        "Controlled upstream request refused.",
        None,
    )


@pytest.mark.parametrize(
    "field,value", [("run_id", None), ("policy_sha256", 3), ("directory", "/owned")]
)
def test_malformed_private_descriptor_is_terminal(field, value):
    descriptor = RunDescriptor("b" * 32, "a" * 64, Path("/owned"))
    assert refusal(lambda: replace(descriptor, **{field: value})) == (
        RequestPolicyError,
        "Controlled upstream request refused.",
        None,
    )


def test_private_descriptor_record_contract(tmp_path):
    directory = tmp_path / "private-descriptor"
    descriptor = RunDescriptor("b" * 32, "a" * 64, directory)
    record = {"run_id": "b" * 32, "policy_sha256": "a" * 64, "directory": str(directory)}
    assert descriptor.to_record() == record
    assert RunDescriptor.from_record(record) == descriptor
    assert str(directory) not in repr(descriptor)


def binding_authorization():
    grant = RequestGrant(
        "POST",
        "http://127.0.0.1:9/owned?x=1",
        (("Authorization", "Bearer synthetic"), ("accept", "application/json")),
        b"owned",
        "http://127.0.0.1:9/owned",
    )
    return Authorization(
        (grant,),
        Limits(
            logical=2,
            attempts=6,
            per_logical=3,
            concurrent=2,
            seconds=30,
            response_bytes=5,
            total_bytes=64,
        ),
        "a" * 64,
    )


def test_binding_matches_independent_vector():
    # Oracle: independently authored canonical bytes, checked with OpenSSL and Python;
    # literal SHA256 fda8d4aee60307a163d9623b86d720dedc2b90937703dbae62cbf9cb0c7b4ccf.
    assert binding_authorization().binding(b"k" * 32).hex() == (
        "91e318fb3e8245adcda7299d669e5946792d95fba55258f92f776ee4e0642745"
    )


@pytest.mark.parametrize(
    "field,value",
    [
        pytest.param("credential_origin", 7, id="origin-number"),
        pytest.param("credential_origin", b"http://127.0.0.1:9", id="origin-bytes"),
        pytest.param("headers", ("ab",), id="row-string"),
        pytest.param("headers", {"ab": "cd"}, id="outer-mapping"),
        pytest.param("headers", ({"ab": "x", "cd": "y"},), id="row-mapping"),
        pytest.param("headers", (iter(("accept", "json")),), id="row-iterator"),
        pytest.param("headers", iter((("accept", "json"),)), id="outer-iterator"),
        pytest.param("headers", (("accept",),), id="short-row"),
        pytest.param("headers", (("accept", 7),), id="nontext-value"),
        pytest.param("headers", (("accept", "j\x00son"),), id="control-value"),
    ],
)
def test_malformed_grant_shapes_are_terminal(field, value):
    grant = RequestGrant("GET", "http://127.0.0.1:9/owned", ())
    assert refusal(lambda: replace(grant, **{field: value})) == (
        RequestPolicyError,
        "Controlled upstream request refused.",
        None,
    )


@pytest.mark.parametrize(
    "wrap",
    [
        pytest.param(lambda _: 7, id="number"),
        pytest.param(iter, id="iterator"),
        pytest.param(set, id="set"),
    ],
)
def test_malformed_grants_container_is_terminal(wrap):
    grants = binding_authorization().grants
    assert refusal(lambda: Authorization(wrap(grants), Limits(), "a" * 64)) == (
        RequestPolicyError,
        "Controlled upstream request refused.",
        None,
    )


@pytest.mark.parametrize("key", ["not-bytes", None])
def test_malformed_binding_key_is_terminal(key):
    assert refusal(lambda: binding_authorization().binding(key)) == (
        RequestPolicyError,
        "Controlled upstream request refused.",
        None,
    )


def test_valid_authority_is_an_immutable_snapshot():
    headers = [["X-Allowed", "synthetic-value"]]
    grant = RequestGrant("POST", "http://127.0.0.1:9/owned", headers, b"owned-body")
    grants = [grant]
    authorization = Authorization(grants, Limits(), "a" * 64)
    headers[0][1] = "changed"
    grants.clear()
    assert grant.headers == (("x-allowed", "synthetic-value"),)
    assert grant.body == b"owned-body"
    assert authorization.grants == (grant,)
    with pytest.raises(FrozenInstanceError):
        grant.body = b"changed"
    assert "synthetic-value" not in repr(grant)
    assert "owned-body" not in repr(authorization)
