"""Real MCP and CLI integration of the caDSR registry tools."""

import asyncio
import io
import json
import logging
import re
import sys
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

import yaml
from pydantic import TypeAdapter

from fakes import cde_response, cde_row, code_map, data_element, form, vm_response, vm_row
from nci_sip_mcp import results
from nci_sip_mcp.cli import build_parser, main
from nci_sip_mcp.context import Context
from nci_sip_mcp.registry import invoke
from nci_sip_mcp.release import RegistryState, registry_state
from test_server import ServerFixture


@patch("nci_sip_mcp.server.configure_logging")
class CaDSRProtocolTest(ServerFixture):
    def test_code_map_omitted_name_stays_absent_and_explicit_null_stays_null(self, _):
        # Given two upstream rows, MCP must distinguish omission from explicit null.
        omitted = code_map("123")
        del omitted["CRDC Name"]
        with patch.object(
            self.context.cadsr,
            "get_crdc_list",
            return_value=[omitted, code_map("456", **{"CRDC Name": None})],
        ):
            result = self.session(lambda client: client.call_tool("get_code_map", {}))
        self.assertFalse(result.is_error, result.content)
        first, second = result.structured_content["codeMaps"]
        self.assertNotIn("crdcName", first)
        self.assertIn("crdcName", second)
        self.assertIsNone(second["crdcName"])
        self.assertEqual([first["usedBy"], second["usedBy"]], [["CRDC", "CIP"]] * 2)

    def test_public_citation_schema_has_exact_optional_include_and_api_roles(self, _):
        listing = self.session(lambda client: client.list_tools())
        tool = next(tool for tool in listing.tools if tool.name == "get_data_element")
        self.assertEqual(
            tool.input_schema["properties"]["include"]["anyOf"][0]["items"]["enum"],
            [
                "permissibleValues",
                "valueDomain",
                "conceptAssociations",
                "alternateNames",
                "classificationSchemes",
                "referenceDocuments",
                "dataElementConcept",
            ],
        )
        definitions = tool.output_schema["$defs"]
        self.assertEqual(
            definitions["ConceptAssociation"]["properties"]["role"]["enum"],
            ["objectClass", "property", "representationTerm"],
        )
        self.assertNotIn("dateModified", definitions["DataElement"]["required"])
        form_tool = next(tool for tool in listing.tools if tool.name == "get_form")
        self.assertNotIn("dateModified", form_tool.output_schema["$defs"]["Form"]["required"])
        self.assertNotIn("dataElementConcept", definitions["DataElement"]["required"])
        self.assertEqual(
            set(definitions["DataElementConcept"]["properties"]),
            {
                "publicId",
                "version",
                "longName",
                "dateModified",
                "beginDate",
                "endDate",
                "objectClass",
                "property",
            },
        )
        self.assertEqual(
            definitions["ConceptAssociation"]["properties"]["primary"],
            {"type": "boolean"},
        )
        self.assertEqual(
            definitions["ValueMeaningConcept"]["properties"]["displayOrder"],
            {"type": "integer"},
        )

    def test_numeric_item_versions_preserve_upstream_spelling(self, _):
        # Given caDSR formats a numeric version differently, an explicit MCP read still
        # returns that item and its original version in both content and provenance.
        for tool, factory, requested, returned in (
            ("get_data_element", data_element, "4.0", "4"),
            ("get_form", form, "1", "1.0"),
            ("get_form", form, "1.0", "1.0"),
        ):
            with (
                self.subTest(tool=tool, requested=requested),
                patch.object(self.context.cadsr, tool, return_value=factory(version=returned)),
            ):
                result = self.session(
                    lambda client, tool=tool, requested=requested: client.call_tool(
                        tool, {"publicId": "123", "version": requested}
                    )
                )
                self.assertFalse(result.is_error)
                self.assertEqual(result.structured_content["version"], returned)
                self.assertEqual(
                    result.structured_content["provenance"]["upstream"]["version"], returned
                )

    def test_distinct_item_versions_fail_closed_without_float_approximation(self, _):
        for requested, returned in (
            ("4", "3"),
            ("1.00000000000000000000000000001", "1.00000000000000000000000000002"),
        ):
            with (
                self.subTest(requested=requested),
                patch.object(
                    self.context.cadsr,
                    "get_data_element",
                    return_value=data_element(version=returned),
                ),
            ):
                result = self.session(
                    lambda client, requested=requested: client.call_tool(
                        "get_data_element", {"publicId": "123", "version": requested}
                    )
                )
                self.assertTrue(result.is_error)
                self.assertEqual(result.structured_content["error"]["code"], "upstream_unavailable")
                self.assertNotIn("publicId", result.structured_content)

    def test_cadsr_licence_text_is_preserved_or_rejected_before_mcp_serialization(self, _):
        for licence in ("", "Upstream attribution", 7, {"unexpected": "object"}):
            with (
                self.subTest(licence=licence),
                patch.object(
                    self.context.cadsr,
                    "get_data_element",
                    return_value=data_element(licenseText=licence),
                ),
            ):
                result = self.session(
                    lambda client: client.call_tool("get_data_element", {"publicId": "123"})
                )
                if isinstance(licence, str):
                    self.assertFalse(result.is_error)
                    # Empty text is no text: nothing is attributed that upstream did not say.
                    self.assertEqual(
                        result.structured_content["provenance"].get("attribution"), licence or None
                    )
                else:
                    self.assertTrue(result.is_error)
                    self.assertIsNotNone(result.structured_content)
                    self.assertEqual(
                        result.structured_content["error"]["code"], "upstream_unavailable"
                    )

    def test_real_mcp_preserves_large_integer_matching_scores(self, _):
        score = 10**400

        async def calls(client):
            return await asyncio.gather(
                client.call_tool("match_data_elements", {"entities": [{"name": "Q"}]}),
                client.call_tool("match_value_meanings", {"values": ["Q"]}),
            )

        with (
            patch.object(
                self.context.cadsr,
                "match_data_element",
                return_value=cde_response(matches=[cde_row(score=score)])["matchResults"],
            ),
            patch.object(
                self.context.cadsr,
                "match_value_meanings",
                return_value=vm_response(matches=[vm_row(score=score)])["matchResults"],
            ),
        ):
            results = self.session(calls)
        for result in results:
            with self.subTest(result=result):
                self.assertFalse(result.is_error)
                self.assertEqual(result.structured_content["matches"][0]["score"], score)

    def test_form_and_crosswalk_resource_keep_governed_policy_alongside_matching(self, _):
        async def calls(client):
            return await asyncio.gather(
                client.call_tool("get_form", {"publicId": "123"}),
                client.read_resource("cadsr://crosswalk/crdc"),
                client.call_tool("get_code_map", {}),
                client.call_tool("match_data_elements", {"entities": [{"name": "Q"}]}),
            )

        with (
            patch.object(self.context.cadsr, "get_form", return_value=form()),
            patch.object(self.context.cadsr, "get_crdc_list", return_value=[code_map()]),
            patch.object(
                self.context.cadsr,
                "match_data_element",
                return_value=cde_response()["matchResults"],
            ),
        ):
            item, resource, tool, computed = self.session(calls)
        self.assertFalse(item.is_error)
        self.assertEqual(item.structured_content["modules"], form()["modules"])
        self.assertEqual((item.meta["ttlMs"], item.meta["cacheScope"]), (3_600_000, "public"))
        content = json.loads(resource.contents[0].text)
        self.assertEqual(
            content["codeMaps"][0]["dataElement"],
            tool.structured_content["codeMaps"][0]["dataElement"],
        )
        self.assertEqual((resource.ttl_ms, resource.cache_scope), (3_600_000, "public"))
        self.assertEqual((computed.meta["ttlMs"], computed.meta["cacheScope"]), (0, "private"))

    def test_permissible_value_error_is_private_and_cli_can_exclude_form_modules(self, _):
        result = self.session(
            lambda client: client.call_tool("get_permissible_value", {"permissibleValueId": "456"})
        )
        self.assertTrue(result.is_error)
        self.assertEqual(result.structured_content["error"]["code"], "capability_unavailable")
        self.assertEqual((result.meta["ttlMs"], result.meta["cacheScope"]), (0, "private"))
        args = build_parser().parse_args(["get-form", "--public-id", "123", "--no-modules"])
        self.assertFalse(args.includeModules)

    def test_matching_is_private_even_alongside_governed_content(self, _):
        async def calls(client):
            return await asyncio.gather(
                client.call_tool("match_data_elements", {"entities": [{"name": "Q"}]}),
                client.call_tool("match_value_meanings", {"values": ["Q"]}),
                client.call_tool("get_data_element", {"publicId": "123"}),
            )

        with (
            patch.object(
                self.context.cadsr,
                "match_data_element",
                return_value=cde_response(matches=[cde_row()])["matchResults"],
            ),
            patch.object(
                self.context.cadsr,
                "match_value_meanings",
                return_value=vm_response(matches=[vm_row()])["matchResults"],
            ),
            patch.object(self.context.cadsr, "get_data_element", return_value=data_element()),
        ):
            cde, vm, content = self.session(calls)
        for result in (cde, vm):
            self.assertFalse(result.is_error)
            self.assertEqual((result.meta["ttlMs"], result.meta["cacheScope"]), (0, "private"))
            self.assertNotIn("_meta", result.structured_content)
            self.assertEqual(len(result.structured_content["matches"]), 1)
        self.assertEqual((content.meta["ttlMs"], content.meta["cacheScope"]), (3_600_000, "public"))

    def test_empty_matching_is_private_and_invalid_nested_input_is_an_error(self, _):
        with patch.object(
            self.context.cadsr, "match_data_element", return_value=cde_response()["matchResults"]
        ):
            empty = self.session(
                lambda client: client.call_tool(
                    "match_data_elements", {"entities": [{"name": "Q"}]}
                )
            )
        self.assertFalse(empty.is_error)
        self.assertEqual(empty.structured_content["matches"], [])
        self.assertEqual((empty.meta["ttlMs"], empty.meta["cacheScope"]), (0, "private"))
        invalid = self.session(
            lambda client: client.call_tool(
                "match_data_elements",
                {
                    "entities": [{"name": "Q"}],
                    "filters": {"classificationScheme": {"publicId": "123"}},
                },
            )
        )
        self.assertTrue(invalid.is_error)
        self.assertEqual(invalid.structured_content["error"]["code"], "invalid_request")
        self.assertEqual(
            invalid.structured_content["error"]["details"]["parameter"],
            "filters.classificationScheme",
        )

    def test_cli_parses_matching_entity_and_scheme_objects(self, _):
        args = build_parser().parse_args(
            [
                "match-data-elements",
                '{"name":"Q"}',
                "--filters",
                '{"classificationScheme":{"publicId":"123","version":"2"}}',
            ]
        )
        self.assertEqual(args.entities, [{"name": "Q"}])
        self.assertEqual(
            args.filters, {"classificationScheme": {"publicId": "123", "version": "2"}}
        )

    def test_concurrent_calls_keep_the_actual_cache_class_in_protocol_metadata(self, _):
        state = RegistryState(None, "2026-07-01T22:19", "releasedCDEsXML-OD.zip")

        async def calls(client):
            return await asyncio.gather(
                client.call_tool("get_data_element", {"publicId": "123"}),
                client.call_tool("resolve_registry_release", {}),
                client.read_resource("cadsr://registry/release"),
                client.read_resource("cadsr://data-element/123/2"),
                client.read_resource("cadsr://data-element/123"),
            )

        with (
            patch.object(self.context.cadsr, "get_data_element", return_value=data_element()),
            patch.object(self.context.cadsr, "resolve_registry_release", return_value=state),
        ):
            item, resolution, resource, versioned, latest = self.session(calls)
        self.assertEqual([item.meta["ttlMs"], resolution.meta["ttlMs"]], [3_600_000, 0])
        self.assertEqual([resource.ttl_ms, versioned.ttl_ms], [3_600_000, 3_600_000])
        self.assertEqual(
            [item.meta["cacheScope"], resolution.meta["cacheScope"], resource.cache_scope],
            ["public"] * 3,
        )
        self.assertNotIn("_meta", item.structured_content)
        self.assertNotIn("ttlMs", item.model_dump(by_alias=True))
        self.assertEqual(
            json.loads(resource.contents[0].text)["provenance"]["source"], "cadsr_export"
        )
        self.assertEqual(json.loads(versioned.contents[0].text)["version"], "2")
        self.assertEqual(json.loads(latest.contents[0].text)["publicId"], "123")
        self.assertEqual(latest.ttl_ms, 3_600_000)

    def test_verified_pinned_tool_content_is_long_lived_and_public(self, _):
        row = {"identifier": "known", "generatedAt": "2026-07-01T22:19"}
        with (
            patch.object(self.context.cadsr, "get_registry_releases", return_value=[row]),
            patch.object(self.context.cadsr, "get_data_element", return_value=data_element()),
        ):
            result = self.session(
                lambda client: client.call_tool(
                    "get_data_element", {"publicId": "123", "registryRelease": "known"}
                )
            )
        self.assertFalse(result.is_error)
        self.assertEqual((result.meta["ttlMs"], result.meta["cacheScope"]), (86_400_000, "public"))
        self.assertEqual(result.structured_content["provenance"]["release"]["identifier"], "known")

    def test_registry_resource_names_only_a_published_release_in_its_provenance(self, _):
        for identifier in (None, "known"):
            state = RegistryState(identifier, "2026-07-01T22:19", "distribution")
            with (
                self.subTest(identifier=identifier),
                patch.object(self.context.cadsr, "resolve_registry_release", return_value=state),
            ):
                resource = self.session(
                    lambda client: client.read_resource("cadsr://registry/release")
                )
                result = json.loads(resource.contents[0].text)
                expected = {"registry": "cadsr"}
                if identifier is not None:
                    expected |= {"identifier": identifier, "date": state.generated_at}
                self.assertEqual(result["provenance"]["release"], expected)
                self.assertEqual(result["published"], identifier is not None)
                self.assertEqual(resource.ttl_ms, 3_600_000)

    def test_invalid_and_unavailable_calls_are_uncached_private_protocol_errors(self, _):
        for tool, arguments, code in (
            ("get_data_element", {"publicId": 123}, "invalid_request"),
            ("list_classification_schemes", {"context": "TEST"}, "capability_unavailable"),
            ("search_data_elements", {"query": "Q", "filters": {"other": "x"}}, "invalid_request"),
        ):
            with self.subTest(tool=tool):
                result = self.session(
                    lambda client, tool=tool, arguments=arguments: client.call_tool(tool, arguments)
                )
                self.assertTrue(result.is_error)
                self.assertEqual(result.structured_content["error"]["code"], code)
                self.assertEqual((result.meta["ttlMs"], result.meta["cacheScope"]), (0, "private"))

    def test_cli_uses_kebab_flags_and_parses_filter_objects(self, _):
        parser = build_parser()
        args = parser.parse_args(
            ["get-data-element", "--public-id", "123", "--registry-release", "known"]
        )
        self.assertEqual((args.publicId, args.registryRelease), ("123", "known"))
        search = parser.parse_args(["search-data-elements", "Q", "--filters", '{"context":"TEST"}'])
        self.assertEqual(search.filters, {"context": "TEST"})


ROOT = Path(__file__).resolve().parents[1]
RECORDS = yaml.safe_load((ROOT / "spec/records.yaml").read_text())
TOOLS = yaml.safe_load((ROOT / "spec/tools.yaml").read_text())
RECORD_TYPES = {
    "cited_item": results.CitedItem,
    "data_element_concept": results.DataElementConcept,
    "registry_release": results.RegistryReleaseResult,
    "data_element": results.DataElement,
    "permissible_value": results.PermissibleValue,
    "classification_scheme": results.ClassificationScheme,
    "context": results.RegistryContext,
    "data_element_match": results.DataElementMatch,
    "value_meaning_match": results.ValueMeaningMatch,
    "form": results.Form,
    "code_map": results.CodeMap,
}


def schema(record):
    return TypeAdapter(record).json_schema()


def fields_in(text, occurrence=0):
    declaration = re.findall(r"\{([^{}]*)\}", text)[occurrence]
    return {
        part.strip().removesuffix("[]").removesuffix("?"): "?" not in part
        for part in declaration.split(",")
    }


class CaDSRAdapterBoundaryTest(ServerFixture):
    def setUp(self):
        super().setUp()
        self.enterContext(
            patch(
                "nci_sip_mcp.http_client._open",
                side_effect=AssertionError("Unexpected HTTP request"),
            )
        )

    def injected_context(self):
        self.state = registry_state(
            "1901-02-03T04:05", None, source_distribution="releasedCDEsXML-OD.zip"
        )
        client = SimpleNamespace(resolve_registry_release=lambda: self.state)
        return Context(
            self.settings,
            cadsr=client,
            evs=self.evs,
            index=self.context.index,
            embedding_provider=self.context.embedding_provider,
        )

    def test_injected_cadsr_produces_the_result_without_a_release_selection_audit(self):
        context = self.injected_context()
        logging.disable(logging.NOTSET)
        with self.assertLogs("nci_sip_mcp.audit", level="INFO") as captured:
            result = invoke(context, "resolve_registry_release")
        self.assertEqual(result, self.state.to_dict())
        self.assertNotIn("selection", captured.records[-1].structured.get("release", {}))

    def test_registry_resolution_cli_command_returns_the_injected_content_state(self):
        self.assertIn("resolve-registry-release", build_parser().format_help())
        context = self.injected_context()
        output = io.StringIO()
        with (
            patch.object(sys, "argv", ["nci-sip-mcp", "resolve-registry-release"]),
            patch("nci_sip_mcp.cli.Context", return_value=context),
            patch("nci_sip_mcp.cli.configure_logging"),
            redirect_stdout(output),
        ):
            status = main()
        self.assertEqual(status, 0)
        self.assertEqual(json.loads(output.getvalue()), self.state.to_dict())

    def test_real_mcp_names_missing_top_level_and_malformed_nested_arguments(self):
        async def interact(client):
            results = []
            for operation, arguments in (
                ("get_concept", {"terminology": "ncit"}),
                ("match_data_elements", {"entities": [{}]}),
                ("match_data_elements", {"entities": [{"name": 5}]}),
            ):
                result = await client.call_tool(operation, arguments)
                results.append(result.structured_content["error"])
            return results

        errors = self.session(interact)
        self.assertEqual([error["code"] for error in errors], ["invalid_request"] * 3)
        self.assertEqual(
            [error.get("details", {}).get("parameter") for error in errors],
            ["code", "entities.0", "entities"],
        )
        self.assertEqual(self.evs.calls, [])


class CaDSRRecordSchemaTest(TestCase):
    def assert_fields(self, record, fields):
        actual = schema(record)
        self.assertEqual(set(actual["properties"]), set(fields))
        self.assertEqual(
            set(actual.get("required", [])), {key for key, required in fields.items() if required}
        )

    def test_all_cadsr_record_keys_and_optionality_match_specification(self):
        for name, record in RECORD_TYPES.items():
            with self.subTest(record=name):
                fields = {
                    key: not field.get("optional", False)
                    for key, field in RECORDS[name]["fields"].items()
                }
                if name == "registry_release":
                    # The same record is a tool result and a resource; M3.2 adds
                    # top-level provenance only for its resource representation.
                    fields["provenance"] = False
                self.assert_fields(record, fields)

    def test_nested_record_fields_match_the_specification_declarations(self):
        pv = RECORDS["permissible_value"]["fields"]["valueMeaning"]["content"]
        cases = (
            (results.ValueMeaning, pv, 0),
            (results.ValueMeaningConcept, pv, 1),
            (
                results.CitedItem,
                RECORDS["classification_scheme"]["fields"]["items"]["content"],
                0,
            ),
            (
                results.ConceptAssociation,
                RECORDS["data_element"]["fields"]["conceptAssociations"]["content"],
                0,
            ),
            (
                results.MatchedRegistryItem,
                RECORDS["value_meaning_match"]["fields"]["item"]["content"],
                0,
            ),
            (
                results.MatchCrosswalk,
                RECORDS["value_meaning_match"]["fields"]["crosswalk"]["content"],
                0,
            ),
            (results.CodeMapIdentity, RECORDS["code_map"]["fields"]["dataElement"]["content"], 0),
            (results.CodeMapValue, RECORDS["code_map"]["fields"]["values"]["content"], 0),
            (
                results.RegistryReference,
                RECORDS["provenance"]["fields"]["release"]["forms"]["registry"],
                0,
            ),
            (results.DataElementHit, TOOLS["search_data_elements"]["returns"], 0),
        )
        for record, text, occurrence in cases:
            with self.subTest(record=record.__name__):
                self.assert_fields(record, fields_in(text, occurrence))

    def test_search_envelope_matches_the_tool_contract_and_empty_page_provenance(self):
        declared = TOOLS["search_data_elements"]["returns"]
        outer = re.sub(r"\[\{[^{}]*\}\]", "[]", declared)
        self.assert_fields(results.DataElementSearch, fields_in(outer) | {"provenance": False})

    def test_nested_closed_value_sets_remain_closed(self):
        declarations = (
            (results.RegistryReference, "registry", ["cadsr"], "provenance", "release"),
            (
                results.MatchedRegistryItem,
                "itemType",
                ["Concept", "ValueMeaning"],
                "value_meaning_match",
                "item",
            ),
            (
                results.ConceptAssociation,
                "role",
                ["objectClass", "property", "representationTerm"],
                "data_element",
                "conceptAssociations",
            ),
        )
        for record, field, values, name, specification_field in declarations:
            with self.subTest(record=record.__name__, field=field):
                text = str(RECORDS[name]["fields"][specification_field])
                self.assertTrue(all(value in text for value in values))
                declaration = schema(record)["properties"][field]
                actual = declaration.get("enum", [declaration.get("const")])
                self.assertEqual(set(actual), set(values))

    def test_every_cadsr_field_preserves_its_nullability_contract(self):
        # caDSR's own nullable text metadata passes through unchanged (A9.1); a registry item's
        # name and statuses never do (#295). vmMatch's optional metadata is instead absent on
        # null, per its record.
        item_text = {"context", "dateCreated", "dateModified"}
        nullable = {
            results.DataElement: item_text
            | {
                "definition",
                "shortName",
                "latestVersionIndicator",
                "beginDate",
                "endDate",
                "origin",
            },
            results.Form: item_text | {"definition", "type", "beginDate", "endDate"},
            results.RegistryItemIdentity: set(),
            results.ValueMeaning: {"dateModified", "beginDate", "endDate"},
            results.CitedItem: {"dateModified", "beginDate", "endDate"},
            results.DataElementConcept: {"dateModified", "beginDate", "endDate"},
            results.PermissibleValue: {"dateModified", "beginDate", "endDate"},
            results.ValueMeaningConcept: {"longName", "terminology"},
            results.ConceptAssociation: {"longName", "terminology"},
            results.ClassificationScheme: {"dateModified", "beginDate", "endDate"},
            results.CodeMap: {"crdcName"},
        }
        nested = {
            results.MatchedRegistryItem,
            results.MatchCrosswalk,
            results.CodeMapIdentity,
            results.CodeMapValue,
            results.RegistryReference,
            results.DataElementHit,
            results.DataElementSearch,
        }
        for record in set(RECORD_TYPES.values()) | set(nullable) | nested:
            for field, declaration in schema(record)["properties"].items():
                with self.subTest(record=record.__name__, field=field):
                    permits_null = {"type": "null"} in declaration.get("anyOf", [])
                    self.assertEqual(permits_null, field in nullable.get(record, set()))
