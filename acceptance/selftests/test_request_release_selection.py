"""Validated controller discovery and pinned content spend the same owned run."""

import json

from http_receiver import WireReceiver

from nci_si_acceptance.environments import SURFACES, Environment
from nci_si_acceptance.request_catalogue import compile_policy
from nci_si_acceptance.request_supervision import SupervisedRun
from nci_si_acceptance.request_transport import ControlledHttpPolicy
from nci_si_mcp.errors import correlated
from nci_si_mcp.http_client import HttpClient
from nci_si_mcp.invocation import call

# Exactly one discovery and one content request, with no hidden preliminary contact.
EXPECTED_CONTACTS = 2


def response(value):
    body = json.dumps(value).encode()
    return b"HTTP/1.1 200 OK\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body


def test_controller_selection_authorizes_only_validated_pinned_content(tmp_path):
    # A valid upstream path spelling can differ from its display version.
    release_row = {
        "terminology": "ncit",
        "latest": True,
        "tags": {"monthly": "true"},
        "version": "26.09d",
        "terminologyVersion": "ncit_26.09e",
    }
    with WireReceiver([response([release_row]), response({"ok": True})]) as receiver:
        environment = Environment(
            "evs-only",
            {surface: receiver.origin if surface == "evs" else None for surface in SURFACES},
            {},
            "e" * 64,
        )
        policy = compile_policy(environment)
        owner = SupervisedRun.create(policy.authorization, tmp_path)

        def select_and_read():
            with owner:
                release = policy.select_release(owner.run, channel="monthly")
                client = HttpClient(
                    receiver.origin,
                    label="owned",
                    timeout_seconds=1,
                    max_attempts=1,
                    retry_backoff_seconds=0,
                    max_response_bytes=1024,
                    size_bound="owned",
                    request_policy=ControlledHttpPolicy(owner.run),
                )
                return {
                    "selection": release.to_dict(),
                    "pinned": release.pinned_terminology,
                    "content": client.get_json(
                        "/api/v1/concept/ncit_26.09e", {"list": "C4817", "include": "summary"}
                    ),
                }

        with correlated("owned-selection-content"):
            result = call("owned-selection", select_and_read)
        snapshot = owner.run.snapshot()
        assert result == {
            "selection": {
                "terminology": "ncit",
                "channel": "monthly",
                "version": "26.09d",
                "date": None,
            },
            "pinned": "ncit_26.09e",
            "content": {"ok": True},
        }, {"contacts": receiver.contacts, "logical": snapshot.logical}
        assert receiver.contacts == EXPECTED_CONTACTS
        assert [(method, target, body) for method, target, _headers, body in receiver.requests] == [
            ("GET", "/api/v1/metadata/terminologies?terminology=ncit&latest=true&tag=monthly", b""),
            ("GET", "/api/v1/concept/ncit_26.09e?list=C4817&include=summary", b""),
        ]
        for _method, _target, headers, _body in receiver.requests:
            assert sorted((key.lower(), value) for key, value in headers) == sorted(
                [
                    ("accept", "application/json"),
                    ("x-correlation-id", "owned-selection-content"),
                    ("host", receiver.origin.removeprefix("http://")),
                    ("accept-encoding", "identity"),
                    ("connection", "close"),
                ]
            )
        assert (snapshot.logical, snapshot.attempts, snapshot.transport_entries) == (2, 2, 2)
        assert owner.snapshot()["state"] == "completed"
