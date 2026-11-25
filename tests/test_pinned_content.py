import json
import unittest
from pathlib import Path
from unittest.mock import patch

from fakes import concept, release
from nci_sip_mcp.evs import EVSClient, EVSReleaseMismatchError, EVSResponseError
from nci_sip_mcp.registry import invoke
from nci_sip_mcp.traffic_identity import product_user_agent
from test_evs_client import FakeResponse
from test_server import ServerFixture


class PinnedClientTest(unittest.TestCase):
    def test_every_recorded_release_uses_the_constructed_pinned_segment(self):
        path = Path(__file__).parents[1] / "acceptance/fixtures/recorded/evs/terminologies.json"
        rows = json.loads(path.read_text())["response"]["body"]
        self.assertTrue(rows)
        for row in rows:
            with self.subTest(terminology=row["terminology"], version=row["version"]):
                self.assertEqual(
                    row["terminologyVersion"], f"{row['terminology']}_{row['version']}"
                )

    def test_arbitrary_codes_are_one_encoded_segment_in_requests_and_provenance(self):
        code = "8001/3?#% name"
        selected = release("29_0", terminology="mdr")
        payload = concept(code, terminology="mdr", version="29_0")
        client = EVSClient("https://example.invalid", user_agent=product_user_agent())
        with patch(
            "nci_sip_mcp.http_client._open",
            return_value=FakeResponse(json.dumps(payload).encode()),
        ) as opened:
            actual = client.get_concept(code, selected, include="minimal")
        self.assertEqual(actual, payload)
        self.assertEqual(
            opened.call_args.args[0].full_url,
            "https://example.invalid/api/v1/concept/mdr_29_0/8001%2F3%3F%23%25%20name"
            "?include=minimal",
        )

    def test_single_and_batch_payloads_fail_closed_on_missing_or_different_version(self):
        client = EVSClient("https://example.invalid", user_agent=product_user_agent())
        for version in (None, "different"):
            raw = concept("C1", version=version)
            for method, payload in (("get_concept", raw), ("get_concepts_by_codes", [raw])):
                with (
                    self.subTest(version=version, method=method),
                    patch.object(client.http, "get_json", return_value=payload),
                    self.assertRaises(EVSReleaseMismatchError) as raised,
                ):
                    getattr(client, method)("C1" if method == "get_concept" else ["C1"], release())
                self.assertEqual(raised.exception.details["requested"], "26.06e")
                self.assertEqual(raised.exception.details["served"], [version or "unknown"])

    def test_matching_version_does_not_make_another_terminology_acceptable(self):
        client = EVSClient("https://example.invalid", user_agent=product_user_agent())
        for terminology in (None, "other"):
            for method in ("get_concept", "get_concepts_by_codes"):
                raw = concept("C1", terminology=terminology)
                payload = raw if method == "get_concept" else [raw]
                with (
                    self.subTest(terminology=terminology, method=method),
                    patch.object(client.http, "get_json", return_value=payload),
                    self.assertRaisesRegex(EVSResponseError, "terminology"),
                ):
                    getattr(client, method)("C1" if method == "get_concept" else ["C1"], release())

    def test_compact_descendants_are_pinned_without_inventing_payload_identity(self):
        client = EVSClient("https://example.invalid", user_agent=product_user_agent())
        compact = [{"code": "8001/3", "name": "Example", "level": 1, "leaf": True}]
        with patch.object(client.http, "get_json", return_value=compact) as request:
            actual = client.get_descendants("A/B", 2, release("old", terminology="other"))
        self.assertEqual(actual, compact)
        self.assertNotIn("active", actual[0])
        self.assertNotIn("conceptStatus", actual[0])
        self.assertNotIn("status", actual[0])
        self.assertEqual(
            request.call_args.args,
            ("/api/v1/concept/other_old/A%2FB/descendants", {"maxLevel": 2}),
        )

    def test_batch_preserves_codes_of_a_terminology_without_a_stated_form(self):
        client = EVSClient("https://example.invalid", user_agent=product_user_agent())
        raw = concept(" A/B ", terminology="other", version="old")
        with patch(
            "nci_sip_mcp.http_client._open",
            return_value=FakeResponse(json.dumps([raw]).encode()),
        ) as request:
            actual = client.get_concepts_by_codes(
                [" A/B "], release("old", terminology="other"), include="minimal"
            )
        self.assertEqual(actual, [raw])
        self.assertEqual(
            request.call_args.args[0].full_url,
            "https://example.invalid/api/v1/concept/other_old?list=+A%2FB+&include=minimal",
        )


class TerminologyContentTest(ServerFixture):
    def setUp(self):
        super().setUp()
        self.evs.concepts = {
            "8001/3": concept(
                "8001/3",
                terminology="other",
                version="old",
                active=True,
                children=[{"code": "target", "name": "Target"}],
                licenseText="Upstream text",
            ),
            "target": concept("target", terminology="other", version="old", active=False),
        }

    def content(self, operation, **arguments):
        result = invoke(
            self.context,
            operation,
            terminology="other",
            release="old",
            code="8001/3",
            **arguments,
        )
        self.assertNotIn("error", result)
        return result

    def test_lookup_preserves_the_requested_terminology_release_and_upstream_attribution(self):
        result = self.content("get_concept")
        self.assertEqual((result["code"], result["terminology"]), ("8001/3", "other"))
        provenance = result["provenance"]
        self.assertEqual(provenance["release"], {"terminology": "other", "identifier": "old"})
        self.assertEqual(provenance["attribution"], "Upstream text")
        self.assertTrue(provenance["sourceUri"].endswith("/other_old/8001%2F3"))
        self.assertEqual(self.evs.calls, [("get_concept", "other_old", "8001/3")])

    def test_hierarchy_and_neighborhood_keep_other_terminology_nodes_and_edges(self):
        hierarchy = self.content("get_concept_hierarchy", direction="child", depth=1)
        self.assertEqual(
            [(n["code"], n["terminology"]) for n in hierarchy["nodes"]], [("target", "other")]
        )
        self.assertNotIn("attribution", hierarchy["nodes"][0]["provenance"])
        graph = self.content("get_concept_neighborhood", kinds=["child"], depth=1)
        self.assertEqual({n["terminology"] for n in graph["nodes"]}, {"other"})
        (edge,) = graph["edges"]
        self.assertEqual((edge["sourceTerminology"], edge["targetTerminology"]), ("other", "other"))
        self.assertEqual((edge["sourceCode"], edge["targetCode"]), ("target", "8001/3"))
        self.assertEqual({call[1] for call in self.evs.calls}, {"other_old"})

    def test_non_ncit_index_modes_are_invalid_before_any_upstream_read(self):
        for mode in ("semantic", "hybrid"):
            with self.subTest(mode=mode):
                result = invoke(
                    self.context,
                    "search_concepts",
                    terminology="other",
                    release="old",
                    query="name",
                    mode=mode,
                )
                self.assertEqual(result["error"]["code"], "invalid_request")
                self.assertEqual(result["error"]["details"]["parameter"], "terminology")
        self.assertEqual(self.evs.calls, [])
