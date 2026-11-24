import json
from dataclasses import replace
from unittest.mock import patch
from urllib.error import URLError

from jsonschema import Draft202012Validator

from fakes import catalogue_rows, concept
from nci_sip_acceptance.spec import RECORDS
from nci_sip_mcp.bounds import Budget
from nci_sip_mcp.config import Settings
from nci_sip_mcp.evs import EVSClient
from nci_sip_mcp.registry import invoke
from nci_sip_mcp.traffic_identity import product_user_agent
from test_evs_client import FakeResponse, http_error
from test_server import ServerFixture


class CatalogueTest(ServerFixture):
    def test_empty_catalogue_has_release_provenance(self):
        self.evs.catalogues = {"role": [], "association": []}
        result = self.listing(terminology="other", release="v1")
        self.assertEqual(result["relationships"], [])
        self.assertEqual(
            result["provenance"]["release"], {"terminology": "other", "identifier": "v1"}
        )
        self.assertEqual(result["provenance"]["source"], "evs_rest")

    def setUp(self):
        super().setUp()
        self.evs.catalogues = {kind: catalogue_rows(kind) for kind in ("role", "association")}

    def listing(self, **arguments):
        return invoke(
            self.context,
            "list_relationships",
            **({"terminology": "ncit", "release": "26.06e"} | arguments),
        )

    def test_default_exclusions_equal_the_specification(self):
        self.assertEqual(
            set(Settings().exclusion_role_codes),
            set(RECORDS["traversal"]["fields"]["polarity"]["exclusions"]["ncit"]),
        )

    def test_all_rows_preserve_names_kinds_and_origin_with_polarity_by_code(self):
        self.evs.catalogues["role"][0].update(name="Positive sounding", licenseText="Upstream")
        self.evs.catalogues["association"][0]["name"] = "Excludes finding"
        listed = self.listing()["relationships"]
        self.assertEqual(len(listed), 9)
        self.assertEqual(
            {row["code"] for row in listed}, set(Settings().exclusion_role_codes) | {"A1"}
        )
        first, last = listed[0], listed[-1]
        self.assertEqual(
            (first["name"], first["kind"], first["polarity"]),
            ("Positive sounding", "role", "negative"),
        )
        self.assertEqual(
            (last["name"], last["kind"], last["polarity"]),
            ("Excludes finding", "association", "positive"),
        )
        self.assertEqual(
            first["provenance"]["upstream"], {"terminology": "ncit", "version": "26.06e"}
        )
        self.assertEqual(
            first["provenance"]["release"], {"terminology": "ncit", "identifier": "26.06e"}
        )
        self.assertEqual(first["provenance"]["attribution"], "Upstream")
        self.assertNotIn("attribution", last["provenance"])
        self.assertTrue(
            last["provenance"]["sourceUri"].endswith("/metadata/ncit_26.06e/associations")
        )
        self.assertEqual(
            self.evs.calls,
            [
                ("get_relationship_catalogue", "ncit_26.06e", "role"),
                ("get_relationship_catalogue", "ncit_26.06e", "association"),
            ],
        )

    def test_both_affected_calls_name_only_absent_codes_and_never_start_a_graph(self):
        self.evs.catalogues["role"] = self.evs.catalogues["role"][2:]
        for operation, arguments in (
            ("list_relationships", {}),
            ("get_concept_neighborhood", {"code": "C3262", "kinds": ["child"]}),
        ):
            with self.subTest(operation=operation):
                result = invoke(
                    self.context, operation, terminology="ncit", release="26.06e", **arguments
                )
                self.assertEqual(result["error"]["code"], "internal_error")
                self.assertEqual(result["error"]["details"], {"missingCodes": ["R135", "R136"]})
                self.assertNotIn("nodes", result)
        self.assertEqual({call[0] for call in self.evs.calls}, {"get_relationship_catalogue"})

    def test_each_call_reloads_the_catalogue_without_poisoning_unrelated_tools(self):
        self.assertEqual(len(self.listing()["relationships"]), 9)
        self.evs.catalogues["role"] = []
        self.assertEqual(
            self.listing()["error"]["details"]["missingCodes"],
            list(Settings().exclusion_role_codes),
        )
        self.assertEqual(len(self.evs.calls), 4)
        failed, concept_result = self.call("get_concept", code="C3262")
        self.assertFalse(failed)
        self.assertEqual(concept_result["code"], "C3262")

    def test_configured_set_drives_catalogue_and_traversal_together(self):
        self.context.settings = replace(self.settings, exclusion_role_codes=("R1",))
        self.evs.catalogues["role"].append(self.evs.catalogues["role"][0] | {"code": "R1"})
        self.evs.concepts["C3262"] = self.evs.concepts["C3262"] | {
            "roles": [{"code": "R1", "type": "Positive", "relatedCode": "C4741"}]
        }
        listed = self.listing()["relationships"]
        self.assertEqual({row["code"] for row in listed if row["polarity"] == "negative"}, {"R1"})
        failed, graph = self.call("get_concept_neighborhood", code="C3262", kinds=["role"], depth=1)
        self.assertFalse(failed)
        self.assertEqual(graph["edges"][0]["provenance"]["polarity"], "negative")

    def test_other_terminology_never_inherits_ncit_exclusions(self):
        self.evs.catalogues = {
            "role": catalogue_rows("role", terminology="other"),
            "association": [],
        }
        result = self.listing(terminology="other")
        self.assertEqual({row["polarity"] for row in result["relationships"]}, {"positive"})
        self.evs.catalogues["role"] = []
        self.assertEqual(self.listing(terminology="other")["relationships"], [])

    def test_invalid_identifiers_are_rejected_before_metadata_reads(self):
        for arguments in ({"terminology": "../ncit"}, {"release": "old\n"}):
            with self.subTest(arguments=arguments):
                self.assertEqual(self.listing(**arguments)["error"]["code"], "invalid_request")
        self.assertEqual(self.evs.calls, [])

    def test_other_terminology_traversal_is_positive_even_for_an_ncit_exclusion_code(self):
        self.evs.catalogues = {
            kind: catalogue_rows(kind, terminology="other") for kind in ("role", "association")
        }
        self.evs.concepts = {
            "X": concept(
                "X",
                terminology="other",
                active=True,
                roles=[{"code": "R135", "type": "Label", "relatedCode": "Y"}],
            ),
            "Y": concept("Y", terminology="other", active=True),
        }
        result = invoke(
            self.context,
            "get_concept_neighborhood",
            terminology="other",
            release="26.06e",
            code="X",
            kinds=["role"],
            depth=1,
        )
        self.assertEqual(result["edges"][0]["provenance"]["polarity"], "positive")
        self.assertEqual(result["edges"][0]["sourceTerminology"], "other")

    def test_malformed_and_duplicate_catalogue_identities_fail_closed(self):
        original = catalogue_rows("association")
        for rows in (
            original * 2,
            catalogue_rows("role")[:1],
            [original[0] | {"code": None}],
            [original[0] | {"code": []}],
            [original[0] | {"code": ""}],
            [original[0] | {"code": 42}],
            [original[0] | {"name": None}],
            [original[0] | {"name": 42}],
            [original[0] | {"name": ""}],
        ):
            with self.subTest(rows=rows):
                self.evs.catalogues["association"] = rows
                result = self.listing()
                self.assertEqual(result["error"]["code"], "upstream_unavailable")
                self.assertNotIn("relationships", result)

    def test_startup_and_listing_tools_are_offline_and_schema_covers_success_and_failure(self):
        tools = self.session(lambda client: client.list_tools()).tools
        self.assertEqual(self.evs.calls, [])
        tool = next(tool for tool in tools if tool.name == "list_relationships")
        validator = Draft202012Validator(tool.output_schema)
        failed, good = self.call("list_relationships")
        self.assertFalse(failed)
        self.assertEqual(list(validator.iter_errors(good)), [])
        self.evs.catalogues["role"] = []
        failed, error = self.call("list_relationships")
        self.assertTrue(failed)
        self.assertEqual(list(validator.iter_errors(error)), [])


class CatalogueHttpTest(ServerFixture):
    def setUp(self):
        super().setUp()
        self.client = EVSClient(
            "https://example.invalid", retry_backoff_seconds=0, user_agent=product_user_agent()
        )
        self.context.evs = self.client

    def listing(self, **arguments):
        return invoke(
            self.context,
            "list_relationships",
            **({"terminology": "ncit", "release": "26.06e"} | arguments),
        )

    def responses(self):
        return [
            FakeResponse(json.dumps(catalogue_rows(kind)).encode())
            for kind in ("role", "association")
        ]

    def test_both_metadata_paths_use_the_caller_release(self):
        with patch("nci_sip_mcp.http_client._open", side_effect=self.responses()) as opened:
            result = self.listing()
        self.assertEqual(len(result["relationships"]), 9)
        self.assertEqual(
            [call.args[0].full_url for call in opened.call_args_list],
            [
                "https://example.invalid/api/v1/metadata/ncit_26.06e/roles",
                "https://example.invalid/api/v1/metadata/ncit_26.06e/associations",
            ],
        )

    def test_mismatched_or_missing_row_identity_never_becomes_a_success(self):
        for field, value, error in (
            ("version", "old", "release_mismatch"),
            ("version", None, "release_mismatch"),
            ("terminology", "other", "upstream_unavailable"),
            ("terminology", None, "upstream_unavailable"),
        ):
            rows = [catalogue_rows("role")[0] | {field: value}]
            with (
                self.subTest(field=field, value=value),
                patch.object(self.client.http, "get_json", return_value=rows),
            ):
                self.assertEqual(self.listing()["error"]["code"], error)

    def test_unknown_release_retains_its_specific_error(self):
        failure = http_error(404, b'{"message":"Terminology not found = ncit_old"}')
        with patch("nci_sip_mcp.http_client._open", side_effect=failure):
            result = self.listing(release="old")
        self.assertEqual(result["error"]["code"], "release_not_available")
        self.assertEqual(result["error"]["details"]["requested"], "ncit_old")

    def test_retries_spend_the_shared_budget_without_partial_catalogue_success(self):
        budget = Budget(requests=2)
        with (
            patch("nci_sip_mcp.content.Budget", return_value=budget),
            patch(
                "nci_sip_mcp.http_client._open", side_effect=[URLError("down"), *self.responses()]
            ) as opened,
        ):
            result = self.listing()
        self.assertEqual(result["error"]["code"], "bound_exceeded")
        self.assertEqual(result["error"]["details"]["bound"], "requests")
        self.assertEqual(budget.attempts, 2)
        self.assertEqual(opened.call_count, 2)
        self.assertNotIn("relationships", result)

    def test_neighborhood_catalogue_and_graph_share_one_budget(self):
        budget = Budget(requests=2)
        with (
            patch("nci_sip_mcp.content.Budget", return_value=budget),
            patch("nci_sip_mcp.http_client._open", side_effect=self.responses()) as opened,
        ):
            result = invoke(
                self.context,
                "get_concept_neighborhood",
                terminology="ncit",
                release="26.06e",
                code="C1",
            )
        self.assertEqual(result["error"]["code"], "bound_exceeded")
        self.assertEqual(result["error"]["details"]["bound"], "requests")
        self.assertEqual(opened.call_count, 2)
        self.assertNotIn("nodes", result)
