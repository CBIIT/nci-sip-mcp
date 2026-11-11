"""Finite content and authenticated selections fail closed at actual caller boundaries."""

import json
import sqlite3
from contextlib import closing, contextmanager
from dataclasses import replace

import pytest
from http_receiver import WireReceiver
from test_request_metadata import selected
from test_request_release_selection import response

from nci_si_acceptance import request_catalogue
from nci_si_acceptance.request_budget import ControlledRun
from nci_si_acceptance.request_catalogue import compile_policy
from nci_si_acceptance.request_supervision import SupervisedRun
from nci_si_acceptance.request_transport import ControlledHttpPolicy
from nci_si_mcp.http_client import HttpClient, RequestPolicyError
from nci_si_mcp.invocation import call

ROW = {
    "terminology": "ncit",
    "latest": True,
    "tags": {"monthly": "true"},
    "version": "26.09d",
    "terminologyVersion": "ncit_26.09e",
}
ROOT = "/api/v1/concept/ncit_26.09e"


@contextmanager
def scenario(directory, replies):
    with WireReceiver([response(value) for value in replies]) as receiver:
        policy = compile_policy(selected(receiver.origin))
        yield receiver, policy, SupervisedRun.create(policy.authorization, directory)


def invoke(owner, operation):
    def execute():
        with owner:
            return operation()

    return call("owned", execute)


def client(receiver, owner):
    return HttpClient(
        receiver.origin,
        label="owned",
        timeout_seconds=1,
        max_attempts=1,
        retry_backoff_seconds=0,
        max_response_bytes=1024,
        size_bound="owned",
        request_policy=ControlledHttpPolicy(owner.run),
    )


def bindings(owner):
    with closing(sqlite3.connect(owner.run.directory / "ledger.sqlite")) as connection:
        return connection.execute(
            "SELECT terminology,channel,version,pinned FROM release_bindings "
            "WHERE version IS NOT NULL ORDER BY terminology,channel"
        ).fetchall()


@pytest.mark.parametrize(
    "suffix,params,target",
    [
        ("/C4817", {"include": "full"}, ROOT + "/C4817?include=full"),
        ("/C4817/parents", None, ROOT + "/C4817/parents"),
        (
            "",
            {"list": ",".join(["C4817"] * 1000), "include": "summary"},
            ROOT + "?list=" + "%2C".join(["C4817"] * 1000) + "&include=summary",
        ),
    ],
)
def test_finite_single_relation_and_full_batch_reach_exact_wire(tmp_path, suffix, params, target):
    with scenario(tmp_path, [[ROW], {"ok": True}]) as (receiver, policy, owner):

        def read():
            policy.select_release(owner.run, channel="monthly")
            return client(receiver, owner).get_json(ROOT + suffix, params)

        assert invoke(owner, read) == {"ok": True}
        assert [(method, path, body) for method, path, _headers, body in receiver.requests] == [
            ("GET", "/api/v1/metadata/terminologies?terminology=ncit&latest=true&tag=monthly", b""),
            ("GET", target, b""),
        ]
        snapshot = owner.run.snapshot()
        assert (snapshot.logical, snapshot.attempts, snapshot.transport_entries) == (2, 2, 2)
        assert receiver.contacts == len(receiver.requests)
        assert owner.snapshot()["state"] == "completed"


def test_globally_valid_undeclared_channel_refuses_before_discovery(tmp_path, monkeypatch):
    declaration = tmp_path / "policy.yaml"
    declaration.write_text(
        request_catalogue.POLICY.read_text().replace("[monthly, weekly]", "[monthly]")
    )
    monkeypatch.setattr(request_catalogue, "POLICY", declaration)
    with scenario(tmp_path, []) as (receiver, policy, owner):
        result = invoke(owner, lambda: policy.select_release(owner.run, channel="weekly"))
        assert result["error"]["code"] == "upstream_unavailable"
        assert receiver.contacts == 0 and bindings(owner) == []
        snapshot = owner.run.snapshot()
        assert (snapshot.logical, snapshot.attempts, snapshot.transport_entries) == (0, 0, 0)


@pytest.mark.parametrize(
    "path,params",
    [
        (ROOT, {"list": "C4817", "include": "summary", "extra": "1"}),
        (ROOT, {"list": "C4817", "include": "summary,history"}),
        (ROOT, {"list": "C999999998", "include": "summary"}),
        (ROOT, {"list": "", "include": "summary"}),
        (ROOT, {"list": ",".join(["C4817"] * 1001), "include": "summary"}),
        (ROOT + "?list=C4817&list=C4817&include=summary", None),
        (ROOT + "?list=%434817&include=summary", None),
        (ROOT + "/C4817/unreviewed", None),
        (ROOT + "/C4817/parents/child", None),
        (ROOT + "/C4817/parents?", None),
        (ROOT.replace("26.09e", "26.09d"), {"list": "C4817", "include": "summary"}),
    ],
)
def test_selected_content_refuses_undeclared_forms(tmp_path, path, params):
    with scenario(tmp_path, [[ROW]]) as (receiver, policy, owner):

        def read():
            policy.select_release(owner.run, channel="monthly")
            return client(receiver, owner).get_json(path, params)

        result = invoke(owner, read)
        assert result["error"]["code"] == "upstream_unavailable"
        assert receiver.contacts == 1 and len(receiver.requests) == 1
        snapshot = owner.run.snapshot()
        assert (snapshot.logical, snapshot.attempts, snapshot.transport_entries) == (1, 1, 1)
        assert bindings(owner) == [("ncit", "monthly", "26.09d", "ncit_26.09e")]
        assert owner.snapshot()["state"] == "failed"


def test_valid_segment_without_controller_selection_never_opens_content(tmp_path):
    with scenario(tmp_path, []) as (receiver, _policy, owner):
        result = invoke(
            owner,
            lambda: client(receiver, owner).get_json(ROOT, {"list": "C4817", "include": "summary"}),
        )
        assert result["error"]["code"] == "upstream_unavailable"
        assert receiver.contacts == 0 and bindings(owner) == []
        snapshot = owner.run.snapshot()
        assert (snapshot.logical, snapshot.attempts, snapshot.transport_entries) == (0, 0, 0)


@pytest.mark.parametrize(
    "rows",
    [
        [],
        [ROW, ROW],
        [{**ROW, "terminology": "go"}],
        [{**ROW, "tags": {"weekly": "true"}}],
        [{**ROW, "terminologyVersion": "ncit_26.09e/child"}],
    ],
)
def test_invalid_discovery_spends_cost_without_binding(tmp_path, rows):
    with scenario(tmp_path, [rows]) as (receiver, policy, owner):
        result = invoke(owner, lambda: policy.select_release(owner.run, channel="monthly"))
        assert result["error"]["code"] == "release_not_available"
        assert receiver.contacts == 1 and bindings(owner) == []
        snapshot = owner.run.snapshot()
        assert (snapshot.logical, snapshot.attempts, snapshot.transport_entries) == (1, 1, 1)
        assert owner.snapshot()["state"] == "failed"


@pytest.mark.parametrize("conflict", [False, True])
def test_repeated_selection_retains_first_binding_and_all_costs(tmp_path, conflict):
    second = {**ROW, "version": "26.09f", "terminologyVersion": "ncit_26.09f"} if conflict else ROW
    with scenario(tmp_path, [[ROW], [second]]) as (receiver, policy, owner):
        original = policy.authorization.binding(b"synthetic-comparison-key")
        descriptor = owner.run.descriptor.to_record()

        def repeat():
            first = policy.select_release(owner.run, channel="monthly")
            return policy.select_release(owner.run, channel="monthly") == first

        result = invoke(owner, repeat)
        if conflict:
            assert result["error"]["code"] == "upstream_unavailable"
            assert owner.snapshot()["state"] == "failed"
        else:
            assert result is True and owner.snapshot()["state"] == "completed"
        assert bindings(owner) == [("ncit", "monthly", "26.09d", "ncit_26.09e")]
        assert policy.authorization.binding(b"synthetic-comparison-key") == original
        assert owner.run.descriptor.to_record() == descriptor
        snapshot = owner.run.snapshot()
        assert (snapshot.logical, snapshot.attempts, snapshot.transport_entries) == (2, 2, 2)
        assert receiver.contacts == len(receiver.requests) == len([ROW, second])


@pytest.mark.parametrize("caller", ["attached", "different-authorization"])
def test_noncontroller_or_other_compilation_refuses_before_discovery(tmp_path, caller):
    with scenario(tmp_path, []) as (receiver, policy, owner):
        with pytest.raises(RequestPolicyError) as denied, owner:
            if caller == "attached":
                owner.run.authorize_participant("owned-worker")
                run = ControlledRun.attach(
                    owner.run.descriptor, policy.authorization, participant="owned-worker"
                )
                policy.select_release(run, channel="monthly")
            else:
                foreign = replace(
                    policy, authorization=compile_policy(selected(receiver.origin)).authorization
                )
                foreign.select_release(owner.run, channel="monthly")
        assert denied.value.reason == "authorization"
        assert receiver.contacts == 0 and bindings(owner) == []
        snapshot = owner.run.snapshot()
        assert (snapshot.logical, snapshot.attempts, snapshot.transport_entries) == (0, 0, 0)


@pytest.mark.parametrize(
    "statement",
    [
        "DELETE FROM release_bindings WHERE channel='monthly'",
        "UPDATE release_bindings SET pinned='ncit_26.09f' WHERE channel='monthly'",
        "DROP TABLE release_bindings",
    ],
)
def test_corrupt_or_missing_selection_state_never_resets(tmp_path, statement):
    with scenario(tmp_path, [[ROW]]) as (receiver, policy, owner):

        def tamper_and_read():
            policy.select_release(owner.run, channel="monthly")
            with (
                closing(sqlite3.connect(owner.run.directory / "ledger.sqlite")) as connection,
                connection,
            ):
                connection.execute(statement)
            return client(receiver, owner).get_json(ROOT, {"list": "C4817", "include": "summary"})

        with pytest.raises(ExceptionGroup) as failed:
            invoke(owner, tamper_and_read)
        # Corrupt identity also prevents close and final projection; retain all three failures.
        assert [type(error) for error in failed.value.exceptions] == [RequestPolicyError] * 3
        reason = "accounting" if statement == "DROP TABLE release_bindings" else "authorization"
        assert [error.reason for error in failed.value.exceptions] == [reason] * 3
        assert receiver.contacts == 1 and len(receiver.requests) == 1
        assert (
            json.loads((owner.run.directory / "summary.json").read_text())["state"] == "incomplete"
        )
        with closing(sqlite3.connect(owner.run.directory / "ledger.sqlite")) as connection:
            assert connection.execute("SELECT logical,attempts,entered FROM run").fetchone() == (
                1,
                1,
                1,
            )
            exists = connection.execute(
                "SELECT name FROM sqlite_master WHERE name='release_bindings'"
            ).fetchall()
            if statement == "DROP TABLE release_bindings":
                assert exists == []
            else:
                assert exists == [("release_bindings",)]
                rows = connection.execute(
                    "SELECT version,pinned FROM release_bindings WHERE channel='monthly'"
                ).fetchall()
                assert rows == (
                    [] if statement.startswith("DELETE") else [("26.09d", "ncit_26.09f")]
                )
