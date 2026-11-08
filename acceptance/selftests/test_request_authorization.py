"""The explicit diagnostic caller's private authority and ledger record contracts."""

import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from nci_si_acceptance.request_authorization import (
    Authorization,
    Limits,
    RequestGrant,
    RunDescriptor,
)
from nci_si_acceptance.request_budget import ControlledRun
from nci_si_mcp.http_client import RequestPolicyError


def test_explicit_attachment_and_receipt_snapshot(tmp_path):
    grant = RequestGrant("GET", "http://127.0.0.1:9/owned", ())
    authorization = Authorization((grant,), Limits(), "a" * 64)
    run = ControlledRun.create(authorization, tmp_path)
    try:
        assert run.snapshot().state == "created"
        run.open()
        run.authorize_participant("local-worker")
        descriptor = RunDescriptor.from_record(run.descriptor.to_record())
        attached = ControlledRun.attach(descriptor, authorization, participant="local-worker")
        with attached.logical(grant) as logical, logical.attempt(response_limit=5) as lease:
            lease.entered()
            assert lease.read(lambda maximum: b"known"[:maximum], 6) == b"known"
        snapshot = run.snapshot()
        assert (snapshot.run_id, snapshot.policy_sha256) == (descriptor.run_id, "a" * 64)
        assert (
            snapshot.state,
            snapshot.logical,
            snapshot.attempts,
            snapshot.reserved_body_bytes,
            snapshot.transport_entries,
            snapshot.consumed_body_bytes,
            snapshot.active,
            snapshot.peak_active,
            snapshot.pending_receipts,
            snapshot.observation_complete,
            snapshot.failure,
        ) == ("open", 1, 1, 5, 1, 5, 0, 1, 0, True, None)
    finally:
        run.close()
    assert run.snapshot().state == "closed"


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


@pytest.mark.parametrize(
    "query,value",
    [
        ("UPDATE run SET consumed=?", 2),
        ("UPDATE run SET peak=?", 5),
        ("UPDATE run SET state=?", "unexpected"),
    ],
)
def test_snapshot_rejects_incoherent_ledger_values(tmp_path, query, value):
    grant = RequestGrant("GET", "http://127.0.0.1:9/owned", ())
    run = ControlledRun.create(Authorization((grant,), Limits(), "a" * 64), tmp_path)
    try:
        run.open()
        with sqlite3.connect(run.directory / "ledger.sqlite") as connection:
            connection.execute(query, (value,))
        assert refusal(run.snapshot) == (
            RequestPolicyError,
            "Controlled upstream request refused.",
            None,
        )
    finally:
        run.close()
