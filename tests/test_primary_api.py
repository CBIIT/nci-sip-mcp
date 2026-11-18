"""PV primary correction through real MCP; crafted from issue #367's live observation."""

import json
from pathlib import Path
from unittest.mock import patch

from jsonschema import validate

from fakes import FakeEVS, FakeSSIS, concept, primary_element
from nci_sip_mcp.evs import EVSClient
from nci_sip_mcp.http_client import UpstreamUnavailableError
from nci_sip_mcp.registry import invoke
from test_evs_client import FakeResponse
from test_server import ServerFixture


def sourced_primary(code, **fields):
    return {"conceptCode": code, "primaryIndicator": "Yes"} | fields


@patch("nci_sip_mcp.server.configure_logging")
class PrimaryAPIProtocolTest(ServerFixture):
    def setUp(self):
        super().setUp()
        self.context.ssis = FakeSSIS(self.settings)
        self.context.ssis.element_values = [
            {
                "version": "2",
                "value": "To a large extent",
                "concept": "http://ncicb.nci.nih.gov/xml/owl/EVS/Thesaurus.owl#C49508",
            }
        ]
        qualifier = self.context.ssis.element_values[0]["concept"].rpartition("#")[2]
        self.context.evs = FakeEVS(
            [concept("C25332", active=True), concept(qualifier, active=True)]
        )

    def lookup(self, **arguments):
        return self.session(
            lambda client: client.call_tool(
                "get_concept_for_permissible_value",
                {"dataElementId": "2178439", "value": "To a large extent", "release": "26.06e"}
                | arguments,
            )
        )

    def test_invalid_version_refuses_before_any_upstream_request(self, _):
        terminologies = self.context.evs.get_terminologies()
        self.context.evs = EVSClient(self.settings.evs_base_url)
        with patch(
            "nci_sip_mcp.http_client._open",
            return_value=FakeResponse(json.dumps(terminologies).encode()),
        ) as requests:
            # The registry exercises the tool's validation without MCP's schema guard.
            result = invoke(
                self.context,
                "get_concept_for_permissible_value",
                dataElementId="2178439",
                value="To a large extent",
                version="4.x",
            )
        self.assertEqual(set(result), {"error"})
        self.assertEqual(result["error"]["code"], "invalid_request")
        self.assertEqual(
            result["error"]["details"],
            {"parameter": "version", "reason": "version must match ^[0-9]+([.][0-9]+)?$"},
        )
        self.assertEqual(requests.call_args_list, [])
        self.assertEqual(self.context.ssis.calls, [])

    def test_primary_flag_wins_over_the_graphs_qualifier(self, _):
        # Keep the observed inversion, and separately challenge display-order selection.
        for codes in (
            None,
            [
                {
                    "conceptCode": "C49508",
                    "evsSource": "NCI_CONCEPT_CODE",
                    "primaryIndicator": "No",
                    "displayOrder": "0",
                },
                {
                    "conceptCode": "C25332",
                    "evsSource": "NCI_CONCEPT_CODE",
                    "primaryIndicator": "Yes",
                    "displayOrder": "2",
                },
            ],
        ):
            with (
                self.subTest(codes=codes),
                patch.object(
                    self.context.cadsr,
                    "get_data_element",
                    return_value=primary_element(codes=codes),
                ),
            ):
                response = self.lookup()
            self.assertFalse(response.is_error, response.content)
            self.assertEqual(response.structured_content["code"], "C25332")

    def test_foreign_and_compound_primaries_return_exact_unresolved_references(self, _):
        # Given #368's observed codes with explicitly synthetic surrounding metadata,
        # when called through MCP, then return references without an NCIt concept read.
        cases = [
            ("LA20347-3", {"evsSource": "LOINC_CODE"}, "non_ncit_source"),
            ("C0009219", {"evsSource": "UMLS_CUI"}, "non_ncit_source"),
            ("C0009219", {"evsSource": "NCI_META_CUI"}, "non_ncit_source"),
            ("C45255::10000", {"evsSource": "NCI_CONCEPT_CODE"}, "compound_code"),
            ("C45255::10000", {}, "compound_code"),
            ("C25332", {}, "missing_source"),
            ("C25332", {"evsSource": None}, "missing_source"),
            ("C25332", {"evsSource": "未知 Ω "}, "non_ncit_source"),
            ("C25332", {"evsSource": "nci_concept_code"}, "non_ncit_source"),
        ]
        for code, source, reason in cases:
            self.context.evs.calls.clear()
            raw = primary_element(codes=[sourced_primary(code, longName="API name", **source)])
            with (
                self.subTest(code=code, source=source),
                patch.object(self.context.cadsr, "get_data_element", return_value=raw),
            ):
                response = self.lookup()
            self.assertFalse(response.is_error, response.content)
            result = response.structured_content
            reference = {"conceptCode": code, "longName": "API name"}
            if source:
                reference["terminology"] = source["evsSource"]
            self.assertEqual(
                {key: value for key, value in result.items() if key != "provenance"},
                {
                    "conceptReference": reference,
                    "unresolvedReason": reason,
                    "permissibleValue": {
                        "dataElement": {"publicId": "2178439", "version": "2"},
                        "value": "To a large extent",
                    },
                },
            )
            provenance = result["provenance"]
            self.assertEqual(provenance["source"], "cadsr_rest")
            self.assertEqual(provenance["release"], {"registry": "cadsr"})
            self.assertEqual(
                provenance["sourceUri"],
                self.settings.cadsr_base_url + "/NCIAPI/1.0/api/DataElement/2178439",
            )
            self.assertEqual(
                provenance["upstream"],
                {"publicId": "2178439", "version": "2", "dateModified": "2026-08-25"},
            )
            self.assertNotIn("graphs", provenance)
            self.assertEqual(self.context.evs.calls, [("get_terminologies", None, (False, None))])
            self.assertEqual(self.context.ssis.calls, [])
            tools = self.session(lambda client: client.list_tools())
            schema = next(
                t.output_schema
                for t in tools.tools
                if t.name == "get_concept_for_permissible_value"
            )
            self.assertIsNone(validate(result, schema))

    def test_primary_agreement_includes_source_presence_and_value(self, _):
        for first, second in (
            ({"evsSource": "NCI_CONCEPT_CODE"}, {"evsSource": "UMLS_CUI"}),
            ({}, {"evsSource": None}),
        ):
            for separate_rows in (False, True):
                raw = primary_element(codes=[sourced_primary("C25332", **first)])
                other = sourced_primary("C25332", **second)
                values = raw["ValueDomain"]["PermissibleValues"]
                if separate_rows:
                    values.extend(
                        primary_element(codes=[other])["ValueDomain"]["PermissibleValues"]
                    )
                else:
                    values[0]["ValueMeaning"]["Concepts"].append(other)
                with (
                    self.subTest(first=first, second=second, separate_rows=separate_rows),
                    patch.object(self.context.cadsr, "get_data_element", return_value=raw),
                ):
                    response = self.lookup()
                self.assertTrue(response.is_error)
                self.assertEqual(
                    response.structured_content["error"]["code"], "upstream_unavailable"
                )

    def test_data_element_preserves_source_evidence_in_both_concept_lists(self, _):
        for source in ({}, {"evsSource": None}, {"evsSource": "未知 Ω "}):
            raw = primary_element(codes=[sourced_primary("C0332307", **source)])
            raw["DataElementConcept"] = {
                "ObjectClass": {"Concepts": [sourced_primary("C0332307", **source)]},
                "Property": {"Concepts": []},
            }
            with (
                self.subTest(source=source),
                patch.object(self.context.cadsr, "get_data_element", return_value=raw),
            ):
                response = self.session(
                    lambda client: client.call_tool(
                        "get_data_element",
                        {
                            "publicId": "2178439",
                            "include": ["conceptAssociations", "permissibleValues"],
                        },
                    )
                )
            self.assertFalse(response.is_error, response.content)
            data = response.structured_content
            reference = {"conceptCode": "C0332307", "primary": True}
            if source:
                reference["terminology"] = source["evsSource"]
            self.assertEqual(data["conceptAssociations"], [reference | {"role": "objectClass"}])
            self.assertEqual(
                data["permissibleValues"][0]["valueMeaning"]["concepts"],
                [reference | {"primary": True}],
            )

    def test_malformed_sources_are_upstream_errors_in_both_concept_lists(self, _):
        for source in ("", " \t", 12, True, [], {}):
            for section in ("conceptAssociations", "permissibleValues"):
                code = sourced_primary("C25332", evsSource=source)
                raw = primary_element(codes=[code])
                raw["DataElementConcept"] = {
                    "ObjectClass": {"Concepts": []},
                    "Property": {"Concepts": [code]},
                }
                with (
                    self.subTest(source=source, section=section),
                    patch.object(self.context.cadsr, "get_data_element", return_value=raw),
                ):
                    response = self.session(
                        lambda client, section=section: client.call_tool(
                            "get_data_element", {"publicId": "2178439", "include": [section]}
                        )
                    )
                self.assertTrue(response.is_error)
                self.assertEqual(
                    response.structured_content["error"]["code"], "upstream_unavailable"
                )

    def test_ncit_source_with_a_malformed_standalone_code_is_not_resolved(self, _):
        raw = primary_element(
            codes=[sourced_primary("not-an-NCIt-code", evsSource="NCI_CONCEPT_CODE")]
        )
        with patch.object(self.context.cadsr, "get_data_element", return_value=raw):
            response = self.lookup()
        self.assertTrue(response.is_error)
        self.assertEqual(response.structured_content["error"]["code"], "upstream_unavailable")
        self.assertEqual(response.structured_content["error"]["details"], {"surface": "cadsr"})
        self.assertEqual(self.context.evs.calls, [("get_terminologies", None, (False, None))])

    def test_provenance_names_only_the_api_and_pinned_evs_reads(self, _):
        with patch.object(
            self.context.cadsr, "get_data_element", return_value=primary_element(version="4")
        ):
            response = self.lookup(version="4.00")
        self.assertFalse(response.is_error, response.content)
        provenance = response.structured_content["provenance"]
        self.assertNotIn("graphs", provenance)
        self.assertEqual(provenance["registry"], {"registry": "cadsr"})
        self.assertEqual(
            provenance["release"],
            {"terminology": "ncit", "identifier": "26.06e", "date": "2026-06-29"},
        )
        self.assertEqual(provenance["source"], "evs_rest")
        self.assertEqual(
            provenance["sourceUri"], self.context.evs.uri("/api/v1/concept/ncit_26.06e/C25332")
        )
        self.assertEqual(provenance["upstream"]["version"], "26.06e")
        origin = provenance["upstream"]["cadsr"]
        self.assertEqual(origin["release"], {"registry": "cadsr"})
        self.assertEqual(origin["source"], "cadsr_rest")
        self.assertEqual(
            origin["sourceUri"],
            self.settings.cadsr_base_url + "/NCIAPI/1.0/api/DataElement/2178439?version=4.00",
        )
        self.assertEqual(
            provenance["upstream"]["cadsr"]["upstream"],
            {"publicId": "2178439", "version": "4", "dateModified": "2026-08-25"},
        )
        self.assertEqual(self.context.ssis.calls, [])

    def test_requested_numeric_version_preserves_api_spelling(self, _):
        with patch.object(
            self.context.cadsr, "get_data_element", return_value=primary_element(version="4.0")
        ) as read:
            response = self.lookup(version="4")
        self.assertFalse(response.is_error, response.content)
        self.assertEqual(
            response.structured_content["permissibleValue"]["dataElement"],
            {"publicId": "2178439", "version": "4.0"},
        )
        self.assertEqual(read.call_args.args, ("2178439", "4"))

        # Given a recorded value exclusive to v1, requesting it succeeds only at v1.
        # EVS is an explicit unit double, not an invented upstream recording.
        self.context.evs = FakeEVS([concept("C38046", active=True)])
        fixtures = Path(__file__).parents[1] / "acceptance/fixtures/recorded/cadsr"
        for version, filename in (
            ("1", "data-element-2200604-version-1.json"),
            (None, "data-element-2200604.json"),
        ):
            body = json.loads((fixtures / filename).read_text())["response"]["body"]
            with (
                self.subTest(version=version),
                patch(
                    "nci_sip_mcp.http_client._open",
                    return_value=FakeResponse(json.dumps(body).encode()),
                ),
            ):
                response = self.lookup(
                    dataElementId="2200604", value="Not Specified", version=version
                )
            if version is not None:
                self.assertFalse(response.is_error, response.content)
                self.assertEqual(response.structured_content["code"], "C38046")
                self.assertEqual(
                    response.structured_content["permissibleValue"],
                    {
                        "dataElement": {"publicId": "2200604", "version": "1"},
                        "value": "Not Specified",
                    },
                )
            else:
                self.assertTrue(response.is_error)
                self.assertEqual(response.structured_content["error"]["code"], "not_found")

    def test_primary_evidence_never_comes_from_order_or_an_invalid_flag(self, _):
        cases = [
            [],
            [{"conceptCode": "C25332", "primaryIndicator": "No", "displayOrder": "0"}],
            [{"conceptCode": "C25332"}],
            [{"conceptCode": "C25332", "primaryIndicator": "yes"}],
            [{"conceptCode": "C25332", "primaryIndicator": True}],
            [
                {"conceptCode": "C25332", "primaryIndicator": "Yes"},
                {"conceptCode": "C49508", "primaryIndicator": "Yes"},
            ],
        ]
        for codes in cases:
            with (
                self.subTest(codes=codes),
                patch.object(
                    self.context.cadsr,
                    "get_data_element",
                    return_value=primary_element(codes=codes),
                ),
            ):
                response = self.lookup()
                self.assertTrue(response.is_error)
                error = response.structured_content["error"]
                self.assertEqual(error["code"], "upstream_unavailable")
                self.assertEqual(error["details"]["surface"], "cadsr")
                self.assertNotIn("code", response.structured_content)

    def test_identical_primary_assertions_collapse_without_using_array_position(self, _):
        codes = [
            {"conceptCode": "C25332", "evsSource": "NCI_CONCEPT_CODE", "primaryIndicator": "Yes"}
        ] * 2
        with patch.object(
            self.context.cadsr, "get_data_element", return_value=primary_element(codes=codes)
        ):
            response = self.lookup()
        self.assertFalse(response.is_error, response.content)
        self.assertEqual(response.structured_content["code"], "C25332")

    def test_each_exact_value_row_must_agree_on_one_primary(self, _):
        raw = primary_element()
        values = raw["ValueDomain"]["PermissibleValues"]
        for codes in (
            [],
            [{"conceptCode": "C49508", "primaryIndicator": "Yes"}],
        ):
            other = primary_element(codes=codes)["ValueDomain"]["PermissibleValues"][0]
            with (
                self.subTest(codes=codes),
                patch.object(
                    self.context.cadsr,
                    "get_data_element",
                    return_value=raw | {"ValueDomain": {"PermissibleValues": [*values, other]}},
                ),
            ):
                response = self.lookup()
            self.assertTrue(response.is_error)
            self.assertEqual(response.structured_content["error"]["code"], "upstream_unavailable")

    def test_api_failures_never_fall_back_to_the_graph(self, _):
        with patch.object(
            self.context.cadsr,
            "get_data_element",
            side_effect=UpstreamUnavailableError("caDSR unavailable"),
        ):
            response = self.lookup()
        self.assertTrue(response.is_error)
        self.assertEqual(response.structured_content["error"]["code"], "upstream_unavailable")
        self.assertEqual(self.context.ssis.calls, [])

    def test_missing_exact_values_do_not_use_graph_or_an_older_version(self, _):
        raw = primary_element()
        for value in (
            "to a large extent",
            " To a large extent",
            "To a large extent ",
            "older value",
        ):
            with (
                self.subTest(value=value),
                patch.object(self.context.cadsr, "get_data_element", return_value=raw),
            ):
                response = self.lookup(value=value)
            self.assertTrue(response.is_error)
            self.assertEqual(response.structured_content["error"]["code"], "not_found")
        self.assertEqual(self.context.ssis.calls, [])
