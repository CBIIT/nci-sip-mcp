"""Immutable diagnostic authorization values, private records and binding contracts."""

import math
import sqlite3
import time
from contextlib import closing
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


@pytest.mark.parametrize(
    "field,value",
    [
        ("method", "GET"),
        ("url", "http://127.0.0.1:9/owned?x=2"),
        ("headers", (("accept", "text/plain"), ("authorization", "Bearer synthetic"))),
        ("body", b"changed"),
        ("credential_origin", "http://127.0.0.1:9"),
    ],
)
def test_each_request_field_changes_private_binding(field, value):
    original = binding_authorization()
    changed = replace(original, grants=(replace(original.grants[0], **{field: value}),))
    assert changed.binding(b"k" * 32) != original.binding(b"k" * 32)
    assert changed.policy_sha256 == original.policy_sha256 == "a" * 64


@pytest.mark.parametrize(
    "field,value",
    [
        ("logical", 3),
        ("attempts", 5),
        ("per_logical", 2),
        ("concurrent", 1),
        ("seconds", 31),
        ("response_bytes", 6),
        ("total_bytes", 65),
    ],
)
def test_each_limit_changes_private_binding(field, value):
    original = binding_authorization()
    changed = replace(original, limits=replace(original.limits, **{field: value}))
    assert changed.binding(b"k" * 32) != original.binding(b"k" * 32)


def test_public_identity_and_header_canonicalization_are_separate():
    original = binding_authorization()
    reordered = replace(
        original.grants[0],
        headers=[
            ["accept", "application/json"],
            ["authorization", "Bearer synthetic"],
        ],
    )
    assert replace(original, grants=[reordered]).binding(b"k" * 32) == original.binding(b"k" * 32)
    secret_changed = replace(
        original,
        grants=[
            replace(
                reordered,
                headers=[
                    ["accept", "application/json"],
                    ["authorization", "Bearer changed"],
                ],
            )
        ],
    )
    assert secret_changed.binding(b"k" * 32) != original.binding(b"k" * 32)
    assert secret_changed.policy_sha256 == "a" * 64
    assert replace(original, policy_sha256="b" * 64).binding(b"k" * 32) != original.binding(
        b"k" * 32
    )


@pytest.mark.parametrize(
    "changes",
    [
        pytest.param({"logical": True}, id="boolean-counter"),
        pytest.param({"logical": 1.5}, id="noninteger-counter"),
        pytest.param({"attempts": 0}, id="zero-counter"),
        pytest.param({"logical": 0}, id="zero-logical-counter"),
        pytest.param({"logical": -1}, id="negative-counter"),
        pytest.param({"per_logical": 769}, id="per-logical-exceeds-global"),
        pytest.param({"concurrent": 769}, id="concurrency-exceeds-global"),
        pytest.param({"seconds": True}, id="boolean-duration"),
        pytest.param({"seconds": "30"}, id="text-duration"),
        pytest.param({"seconds": None}, id="missing-duration"),
        pytest.param({"seconds": float("nan")}, id="nan-duration"),
        pytest.param({"seconds": float("inf")}, id="infinite-duration"),
        pytest.param({"seconds": 0}, id="zero-duration"),
        pytest.param({"seconds": 10**1000}, id="duration-overflow"),
        pytest.param({"response_bytes": 1, "total_bytes": 1}, id="sentinel-does-not-fit"),
        pytest.param({"total_bytes": 1 << 63}, id="sqlite-counter-overflow"),
        pytest.param(
            {"response_bytes": (1 << 63) - 1, "total_bytes": (1 << 63) - 1}, id="sentinel-overflow"
        ),
    ],
)
def test_limits_refuse_invalid_and_unrepresentable_values(changes):
    assert refusal(lambda: Limits(**changes)) == (
        RequestPolicyError,
        "Controlled upstream request refused.",
        None,
    )


def test_exact_defaults_and_native_storage_clock_boundaries():
    limits = Limits()
    assert (
        limits.logical,
        limits.attempts,
        limits.per_logical,
        limits.concurrent,
        limits.seconds,
        limits.response_bytes,
        limits.total_bytes,
    ) == (256, 768, 3, 4, 1800.0, 1048576, 67108864)
    maximum = (1 << 63) - 1
    largest = Limits(
        logical=maximum,
        attempts=maximum,
        per_logical=maximum,
        concurrent=maximum,
        seconds=float.fromhex("0x1.fffffffffffffp+1023"),
        response_bytes=maximum - 1,
        total_bytes=maximum,
    )
    with closing(sqlite3.connect(":memory:")) as connection:
        assert connection.execute(
            "SELECT ?,?,?", (largest.logical, largest.total_bytes, largest.seconds)
        ).fetchone() == (maximum, maximum, largest.seconds)
    assert math.isfinite(time.monotonic() + largest.seconds)
    assert largest.response_bytes + 1 == largest.total_bytes == maximum


@pytest.mark.parametrize(
    "url",
    [
        "ftp://127.0.0.1/owned",
        "http:///owned",
        "http://user:synthetic@127.0.0.1/owned",
        "http://user@127.0.0.1/owned",
        "http://127.0.0.1/owned#fragment",
        "http://127.0.0.1/own ed",
        "http://127.0.0.1/owned\x7f",
        "http://127.0.0.1:70000/owned",
        "http://127.0.0.1/%2e%2e/owned",
        "http://127.0.0.1/./owned",
        "http://127.0.0.1/own\\ed",
        "http://127.0.0.1/own%2fed",
        "http://127.0.0.1/own%5ced",
        "http://127.0.0.1/own%252fed",
    ],
)
def test_ambiguous_destinations_are_terminal(url):
    assert refusal(lambda: RequestGrant("GET", url, ())) == (
        RequestPolicyError,
        "Controlled upstream request refused.",
        None,
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"method": "DELETE"},
        {"body": bytearray(b"mutable")},
        {"headers": (("accept", "json"), ("Accept", "text"))},
        {"credential_origin": "https://127.0.0.1:9/owned"},
        {"credential_origin": "http://127.0.0.2:9/owned"},
        {"credential_origin": "http://127.0.0.1:10/owned"},
        {"credential_origin": "http://127.0.0.1:9/own"},
        {"credential_origin": "http://127.0.0.1:9/owned?q=1"},
    ],
)
def test_exact_grant_fields_and_credential_destination(changes):
    grant = binding_authorization().grants[0]
    assert refusal(lambda: replace(grant, **changes)) == (
        RequestPolicyError,
        "Controlled upstream request refused.",
        None,
    )


@pytest.mark.parametrize("missing", ["run_id", "policy_sha256", "directory"])
def test_private_descriptor_refuses_missing_keys(missing):
    record = {"run_id": "b" * 32, "policy_sha256": "a" * 64, "directory": "/owned"}
    del record[missing]
    assert refusal(lambda: RunDescriptor.from_record(record)) == (
        RequestPolicyError,
        "Controlled upstream request refused.",
        None,
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"extra": "synthetic"},
        {"run_id": "b" * 31},
        {"policy_sha256": "A" * 64},
        {"directory": "relative"},
        {"directory": 7},
    ],
)
def test_private_descriptor_refuses_extra_or_malformed_fields(changes):
    record = {"run_id": "b" * 32, "policy_sha256": "a" * 64, "directory": "/owned"} | changes
    assert refusal(lambda: RunDescriptor.from_record(record)) == (
        RequestPolicyError,
        "Controlled upstream request refused.",
        None,
    )


@pytest.mark.parametrize(
    "make,field,value",
    [
        pytest.param(Limits, "logical", 1, id="limits"),
        pytest.param(binding_authorization, "policy_sha256", "b" * 64, id="authorization"),
        pytest.param(
            lambda: RunDescriptor("b" * 32, "a" * 64, Path("/owned")),
            "run_id",
            "c" * 32,
            id="descriptor",
        ),
    ],
)
def test_frozen_authority_records_refuse_reassignment(make, field, value):
    record = make()
    previous = getattr(record, field)
    with pytest.raises(FrozenInstanceError):
        setattr(record, field, value)
    assert getattr(record, field) == previous


@pytest.mark.parametrize(
    "private_value",
    [
        pytest.param("GRANT-URL.invalid", id="url"),
        pytest.param("private-synthetic-header", id="headers"),
        pytest.param("private-synthetic-body", id="body"),
        pytest.param("http://grant-url.invalid/private-destination", id="credential-origin"),
    ],
)
def test_grant_repr_omits_private_fields(private_value):
    # Host case and explicit default port distinguish the URL and destination markers.
    grant = RequestGrant(
        "POST",
        "http://GRANT-URL.invalid:80/private-destination/private-url?private-query=1",
        (("Authorization", "Bearer private-synthetic-header"),),
        b"private-synthetic-body",
        "http://grant-url.invalid/private-destination",
    )
    assert private_value not in repr(grant)


def test_private_binding_depends_on_supplied_key():
    authorization = binding_authorization()
    assert authorization.binding(b"k" * 32) != authorization.binding(b"z" * 32)


@pytest.mark.parametrize("name", ["\u212aey", "bad:name", "\u03b1lpha"])
def test_original_header_name_grammar_is_terminal(name):
    assert refusal(
        lambda: RequestGrant("GET", "http://127.0.0.1:9/owned", ((name, "synthetic"),))
    ) == (RequestPolicyError, "Controlled upstream request refused.", None)


@pytest.mark.parametrize("name", ["Authorization", "X-EVSRestAPI-License-Key"])
def test_sensitive_header_requires_credential_destination(name):
    assert refusal(
        lambda: RequestGrant("GET", "http://127.0.0.1:9/owned", ((name, "synthetic"),))
    ) == (RequestPolicyError, "Controlled upstream request refused.", None)


@pytest.mark.parametrize("field,value", [("grants", ()), ("grants", (None,)), ("limits", None)])
def test_authorization_refuses_empty_or_wrong_value_records(field, value):
    authorization = binding_authorization()
    assert refusal(lambda: replace(authorization, **{field: value})) == (
        RequestPolicyError,
        "Controlled upstream request refused.",
        None,
    )


@pytest.mark.parametrize("value", [None, []])
def test_private_descriptor_refuses_nonmapping_records(value):
    assert refusal(lambda: RunDescriptor.from_record(value)) == (
        RequestPolicyError,
        "Controlled upstream request refused.",
        None,
    )
