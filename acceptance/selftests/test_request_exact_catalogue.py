"""Reviewed exact catalogue forms reach only their independently selected wire targets."""

import pytest
from http_receiver import WireReceiver
from test_request_release_boundaries import ROW
from test_request_release_selection import response

from nci_si_acceptance.environments import SURFACES, Environment
from nci_si_acceptance.request_catalogue import compile_policy
from nci_si_acceptance.request_supervision import SupervisedRun
from nci_si_acceptance.request_transport import ControlledHttpPolicy
from nci_si_mcp.errors import correlated
from nci_si_mcp.evs import LICENSE_KEY_HEADER
from nci_si_mcp.http_client import HttpClient
from nci_si_mcp.invocation import call

EXPECTED_CONTACTS = 2


@pytest.mark.parametrize(
    "surface,path,params,wire",
    [
        ("evs", "/api/v1/metadata/ncit_26.09e/roles", None, "/api/v1/metadata/ncit_26.09e/roles"),
        (
            "evs",
            "/api/v1/concept/ncit_26.09e/search",
            {
                "term": "ewing sarcoma",
                "type": "contains",
                "include": "minimal,highlights",
                "fromRecord": 0,
                "pageSize": 10,
            },
            "/api/v1/concept/ncit_26.09e/search?term=ewing+sarcoma&type=contains&include=minimal%2Chighlights&fromRecord=0&pageSize=10",
        ),
        ("evs", "/api/v1/mapset", {"include": "minimal"}, "/api/v1/mapset?include=minimal"),
        (
            "evs-fhir",
            "/ValueSet/$expand",
            {"url": "http://ncicb.nci.nih.gov/xml/owl/EVS/Thesaurus.owl?fhir_vs=C85492"},
            "/fhir/r4/ValueSet/$expand?url=http%3A%2F%2Fncicb.nci.nih.gov%2Fxml%2Fowl%2FEVS%2FThesaurus.owl%3Ffhir_vs%3DC85492",
        ),
    ],
)
def test_exact_catalogue_uses_selected_origin_and_bound_release(
    tmp_path, surface, path, params, wire
):
    with (
        WireReceiver([response([ROW]), response({"ok": True})]) as evs,
        WireReceiver([response({"ok": True})]) as fhir,
    ):
        origins = dict.fromkeys(SURFACES)
        origins.update({"evs": evs.origin, "evs-fhir": fhir.origin + "/fhir/r4"})
        policy = compile_policy(
            Environment(
                "evs-only", origins, {"NCI_SI_EVS_LICENSE_KEY": "synthetic-owned-key"}, "e" * 64
            )
        )
        owner = SupervisedRun.create(policy.authorization, tmp_path)
        target = evs if surface == "evs" else fhir

        def select_and_read():
            with owner:
                policy.select_release(owner.run, channel="monthly")
                client = HttpClient(
                    origins[surface],
                    label="owned",
                    timeout_seconds=1,
                    max_attempts=1,
                    retry_backoff_seconds=0,
                    max_response_bytes=1024,
                    size_bound="owned",
                    request_policy=ControlledHttpPolicy(owner.run),
                    credentials={LICENSE_KEY_HEADER: "synthetic-owned-key"}
                    if surface == "evs"
                    else None,
                )
                return client.get_json(path, params)

        with correlated("owned-exact-catalogue"):
            result = call("owned", select_and_read)
        snapshot = owner.run.snapshot()
        assert result == {"ok": True}, {
            "evs_contacts": evs.contacts,
            "fhir_contacts": fhir.contacts,
            "logical": snapshot.logical,
            "snapshot": snapshot,
            "evs_requests": len(evs.requests),
            "fhir_requests": len(fhir.requests),
        }
        assert evs.contacts + fhir.contacts == EXPECTED_CONTACTS
        assert evs.requests[0][0:2] == (
            "GET",
            "/api/v1/metadata/terminologies?terminology=ncit&latest=true&tag=monthly",
        )
        method, actual, headers, body = target.requests[-1]
        assert (method, actual, body) == ("GET", wire, b"")
        expected_license = (
            [("x-evsrestapi-license-key", "synthetic-owned-key")] if surface == "evs" else []
        )
        assert sorted((name.lower(), value) for name, value in headers) == sorted(
            [
                ("accept", "application/json"),
                ("x-correlation-id", "owned-exact-catalogue"),
                ("host", target.origin.removeprefix("http://")),
                ("accept-encoding", "identity"),
                ("connection", "close"),
                *expected_license,
            ]
        )
        assert (snapshot.logical, snapshot.attempts, snapshot.transport_entries) == (2, 2, 2)
        assert owner.snapshot()["state"] == "completed"
