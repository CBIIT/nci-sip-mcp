"""The cross-domain wire schemas and adapters, exercised through real MCP sessions."""

import asyncio
from copy import deepcopy
from unittest.mock import patch

from jsonschema import Draft202012Validator

from fakes import FakeSSIS, primary_element
from nci_sip_mcp.cli import build_parser
from nci_sip_mcp.evs import EVSClient
from nci_sip_mcp.traffic_identity import product_user_agent
from test_seam import NCIT, element, gdc_map
from test_server import ServerFixture


@patch("nci_sip_mcp.server.configure_logging")
class SeamProtocolTest(ServerFixture):
    def test_bound_value_without_a_crdc_field_name_is_an_upstream_error(self, _):
        # Given a valid value before an unnamed bound value, neither route returns partial content.
        self.context.ssis = FakeSSIS(self.settings)
        self.context.ssis.elements = [element(123)]
        self.context.ssis.values = [
            {"id": "123", "version": "1", "value": "Tumor", "concept": NCIT + "C3262"}
        ]
        valid = {
            "CDE Public ID": "123",
            "Version": "1",
            "CRDC Name": "diagnosis",
            "Used By": "PDC",
            "permissibleValues": [{"Permissible Value": "Tumor", "Concept Code": "C3262"}],
        }
        for tool in ("resolve_stored_value", "ground_value"):
            for name in ({"CRDC Name": None}, {}):
                unnamed = {key: value for key, value in valid.items() if key != "CRDC Name"}
                with (
                    self.subTest(tool=tool, name=name),
                    patch.object(
                        self.context.cadsr, "get_crdc_list", return_value=[valid, unnamed | name]
                    ),
                ):
                    result = self.session(
                        lambda client, tool=tool: client.call_tool(
                            tool,
                            {"conceptCode": "C3262", "commons": "PDC", "release": "26.06e"},
                        )
                    )
                    self.assertTrue(result.is_error, result.content)
                    data = result.structured_content
                    self.assertEqual(
                        data and data.get("error", {}).get("code"), "upstream_unavailable"
                    )
                    self.assertEqual(data["error"]["details"], {"surface": "cadsr"})
                    self.assertEqual(set(data), {"error"})

    def test_merged_graph_versions_make_only_public_id_associations(self, _):
        """Given a merged CDE node, finding its uses must not assign its values to a version."""
        self.context.ssis = FakeSSIS(self.settings)
        self.context.ssis.elements = [element(2008092, "1")]
        self.context.ssis.values = [
            {"id": "2008092", "version": "1", "value": "Tis", "concept": NCIT + "C3262"}
        ]

        async def call(client):
            return await client.call_tool(
                "find_data_elements_for_concept",
                {"conceptCode": "C3262", "includePermissibleValues": True},
            )

        result = self.session(call)
        self.assertFalse(result.is_error, result.content)
        data = result.structured_content
        self.assertEqual(
            data["dataElements"][0]["dataElement"],
            {"publicId": "2008092", "longName": "Element 2008092"},
        )
        self.assertEqual(data["permissibleValues"][0]["dataElement"], {"publicId": "2008092"})
        self.assertEqual(data["permissibleValues"][0]["value"], "Tis")
        self.assertNotIn("associationScope", data["dataElements"][0])

    def test_exact_find_preserves_graph_states_without_requiring_hierarchy_alignment(self, _):
        self.context.ssis = FakeSSIS(self.settings)
        self.context.ssis.elements = [element(123)]
        for version in ("26.05d", None):
            with self.subTest(version=version):
                self.context.ssis.graphs[0].pop("version", None)
                if version is not None:
                    self.context.ssis.graphs[0]["version"] = version

                async def call(client):
                    return await client.call_tool(
                        "find_data_elements_for_concept",
                        {"conceptCode": "C3262", "release": "26.06e"},
                    )

                result = self.session(call)
                self.assertFalse(result.is_error, result.content)
                use = result.structured_content["dataElements"][0]
                self.assertEqual(use["dataElement"]["publicId"], "123")
                provenance = use["provenance"]
                self.assertEqual(provenance["release"]["identifier"], "26.06e")
                expected = [
                    self.context.ssis.graphs[0] | {"date": "2026-06-29"},
                    self.context.ssis.graphs[1],
                ]
                self.assertEqual(provenance["graphs"], expected)
                self.assertEqual(provenance["upstream"]["graphs"], self.context.ssis.graphs)

    def test_all_four_tools_serialize_their_real_records_and_cache_policies(self, _):
        self.context.ssis = FakeSSIS(self.settings)
        self.context.ssis.elements = [element(123)]
        listing = '<pre><a href="releasedCDEsXML-OD.zip">export</a> 2026-06-05 12:30\n</pre>'

        async def calls(client):
            return await asyncio.gather(
                client.call_tool("find_data_elements_for_concept", {"conceptCode": "C3262"}),
                client.call_tool(
                    "get_concept_for_permissible_value", {"dataElementId": "123", "value": "Male"}
                ),
                client.call_tool(
                    "resolve_stored_value",
                    {"conceptCode": "C3262", "commons": "PDC", "release": "26.06e"},
                ),
                client.call_tool("get_release_alignment", {}),
            )

        with (
            patch.object(
                self.context.cadsr,
                "get_data_element",
                return_value=primary_element(
                    value="Male",
                    publicId="123",
                    codes=[
                        {
                            "conceptCode": "C3262",
                            "evsSource": "NCI_CONCEPT_CODE",
                            "primaryIndicator": "Yes",
                        }
                    ],
                ),
            ),
            patch.object(self.context.cadsr, "get_crdc_list", return_value=[]),
            patch.object(self.context.cadsr.export_http, "get_text", return_value=listing),
        ):
            uses, concept, stored, alignment = self.session(calls)
        for result in (uses, concept, stored, alignment):
            self.assertFalse(result.is_error, result.content)
        self.assertEqual(
            uses.structured_content["dataElements"][0]["dataElement"]["publicId"], "123"
        )
        self.assertEqual(concept.structured_content["code"], "C3262")
        self.assertEqual(stored.structured_content["confidence"], "none")
        self.assertEqual(alignment.structured_content["intervalDays"], 28)
        self.assertEqual((uses.meta["ttlMs"], concept.meta["cacheScope"]), (0, "private"))
        self.assertEqual((stored.meta["ttlMs"], stored.meta["cacheScope"]), (3_600_000, "public"))
        self.assertEqual((alignment.meta["ttlMs"], alignment.meta["cacheScope"]), (0, "public"))

    def test_cli_preserves_value_text_and_exposes_shared_paging_flags(self, _):
        parser = build_parser()
        value = parser.parse_args(
            [
                "get-concept-for-permissible-value",
                "--data-element-id",
                "123",
                "--value",
                " Male ",
                "--version",
                "4.0",
            ]
        )
        self.assertEqual((value.dataElementId, value.value), ("123", " Male "))
        self.assertEqual(value.version, "4.0")
        uses = parser.parse_args(
            [
                "find-data-elements-for-concept",
                "C3262",
                "--include-permissible-values",
                "--limit",
                "2",
            ]
        )
        self.assertEqual(
            (uses.conceptCode, uses.includePermissibleValues, uses.limit), ("C3262", True, 2)
        )


@patch("nci_sip_mcp.server.configure_logging")
class CrossDomainRecordSchemaTest(ServerFixture):
    def setUp(self):
        super().setUp()
        self.context.ssis = FakeSSIS(self.settings)

    def validated(self, name, arguments):
        async def interaction(client):
            return await client.list_tools(), await client.call_tool(name, arguments)

        listing, response = self.session(interaction)
        self.assertFalse(response.is_error, response.content)
        tool = next(tool for tool in listing.tools if tool.name == name)
        validator = Draft202012Validator(tool.output_schema)
        data = response.structured_content
        validator.validate(data)
        return validator, data

    def rejects_missing(self, validator, data, paths):
        for path in paths:
            with self.subTest(path=path):
                broken = deepcopy(data)
                parent = broken
                for key in path[:-1]:
                    parent = parent[key]
                del parent[path[-1]]
                self.assertFalse(validator.is_valid(broken))

    def rejects_types(self, validator, data, cases):
        for path, value in cases:
            with self.subTest(path=path, value=value):
                broken = deepcopy(data)
                parent = broken
                for key in path[:-1]:
                    parent = parent[key]
                parent[path[-1]] = value
                self.assertFalse(validator.is_valid(broken))

    def test_value_uses_and_required_provenance_survive_the_public_schema(self, _):
        self.context.ssis.elements = [element(123)]
        self.context.ssis.values = [
            {"id": "123", "version": "1", "value": " Male \t", "concept": NCIT + "C3262"}
        ]
        validator, data = self.validated(
            "find_data_elements_for_concept",
            {"conceptCode": "C3262", "includePermissibleValues": True},
        )
        self.assertEqual(data["permissibleValues"][0]["value"], " Male \t")
        self.assertEqual(data["permissibleValues"][0]["conceptCode"], "C3262")
        self.assertEqual(data["permissibleValues"][0]["conceptTerminology"], "ncit")
        self.rejects_missing(
            validator,
            data,
            [("truncation",), ("dataElements", 0, "provenance"), ("permissibleValues", 0, "value")],
        )
        self.rejects_types(
            validator, data, [(("permissibleValues", 0, "dataElement", "publicId"), 123)]
        )

    def test_graph_roles_and_codes_are_required_with_optional_integer_order(self, _):
        # Given graph role evidence, the advertised MCP schema rejects incomplete assertions.
        self.context.ssis.elements = [element(123)]
        validator, data = self.validated("find_data_elements_for_concept", {"conceptCode": "C1"})
        self.assertEqual(
            data["dataElements"][0]["roles"],
            [{"role": "objectClass", "conceptCode": "C1", "displayOrder": 0}],
        )
        self.rejects_missing(
            validator,
            data,
            [
                ("dataElements", 0, "roles"),
                ("dataElements", 0, "roles", 0, "conceptCode"),
                ("dataElements", 0, "roles", 0, "role"),
            ],
        )
        self.rejects_types(
            validator,
            data,
            [
                (("dataElements", 0, "roles", 0, "role"), "representationTerm"),
                (("dataElements", 0, "roles", 0, "displayOrder"), "0"),
            ],
        )
        without_order = deepcopy(data)
        del without_order["dataElements"][0]["roles"][0]["displayOrder"]
        self.assertTrue(validator.is_valid(without_order))

    def test_permissible_value_identity_is_required_in_the_concept_output(self, _):
        with patch.object(
            self.context.cadsr,
            "get_data_element",
            return_value=primary_element(
                value="Male",
                publicId="123",
                codes=[
                    {
                        "conceptCode": "C3262",
                        "evsSource": "NCI_CONCEPT_CODE",
                        "primaryIndicator": "Yes",
                    }
                ],
            ),
        ):
            validator, data = self.validated(
                "get_concept_for_permissible_value", {"dataElementId": "123", "value": "Male"}
            )
        self.assertEqual(data["permissibleValue"]["value"], "Male")
        self.assertEqual(data["provenance"]["servedBy"], "live")
        self.assertEqual(
            data["provenance"]["sourceUri"],
            self.evs.uri("/api/v1/concept/ncit_26.06e/C3262"),
        )
        self.rejects_missing(validator, data, [("permissibleValue",)])

    def test_gdc_hit_and_evidence_have_strict_mapset_source_records(self, _):
        self.context.evs = EVSClient("https://evs.test", user_agent=product_user_agent())
        with patch.object(
            self.context.evs,
            "_get_existing",
            side_effect=[
                self.evs.get_terminologies(),
                {"code": "NCIt_Maps_To_GDC", "version": "26.06e"},
                {"total": 1, "maps": [gdc_map("C3262")]},
            ],
        ):
            validator, data = self.validated(
                "resolve_stored_value",
                {"conceptCode": "C3262", "commons": "GDC", "release": "26.06e"},
            )
        self.assertEqual(data["confidence"], "asserted")
        self.assertEqual(data["storedValues"][0]["source"]["version"], "26.06e")
        self.assertEqual(data["evidence"]["sources"], [data["storedValues"][0]["source"]])
        self.rejects_missing(
            validator,
            data,
            [("storedValues", 0, "source"), ("storedValues", 0, "source", "version")],
        )
        self.rejects_types(
            validator,
            data,
            [
                (("confidence",), "probable"),
                (("storedValues", 0, "source", "version"), 26),
                (("evidence", "sources"), ["mapset"]),
                (("evidence", "valueLevelBinding"), "yes"),
                (("evidence", "coverage"), "1"),
            ],
        )

    def test_crdc_hit_and_evidence_have_strict_crosswalk_source_records(self, _):
        row = {
            "CDE Public ID": "123",
            "Version": "2",
            "CRDC Name": "diagnosis",
            "Used By": "PDC",
            "permissibleValues": [{"Permissible Value": "Tumor", "Concept Code": "C3262"}],
        }
        with patch.object(self.context.cadsr, "get_crdc_list", return_value=[row]):
            validator, data = self.validated(
                "resolve_stored_value", {"conceptCode": "C3262", "commons": "PDC"}
            )
        self.assertEqual(data["confidence"], "asserted")
        self.assertEqual(data["storedValues"][0]["source"]["crosswalk"], "CRDC")
        self.assertEqual(data["evidence"]["sources"], [data["storedValues"][0]["source"]])
        self.rejects_missing(validator, data, [("storedValues", 0, "source", "crosswalk")])
        self.rejects_types(validator, data, [(("storedValues", 0, "source", "crosswalk"), 7)])

    def test_cross_domain_terminology_input_is_advertised_as_ncit_only(self, _):
        listing = self.session(lambda client: client.list_tools())
        tool = next(tool for tool in listing.tools if tool.name == "find_data_elements_for_concept")
        declaration = tool.input_schema["properties"]["terminology"]
        self.assertEqual(declaration.get("enum", [declaration.get("const")]), ["ncit"])

    def test_stored_value_and_alignment_cli_commands_keep_their_public_flags(self, _):
        parser = build_parser()
        stored = parser.parse_args(["resolve-stored-value", "C3262", "GDC", "--release", "26.06e"])
        self.assertEqual(
            (stored.conceptCode, stored.commons, stored.release), ("C3262", "GDC", "26.06e")
        )
        alignment = parser.parse_args(["get-release-alignment", "--max-interval-days", "0"])
        self.assertEqual(alignment.maxIntervalDays, 0)
