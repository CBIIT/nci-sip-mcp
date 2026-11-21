import json
from unittest.mock import patch

from jsonschema import Draft202012Validator

from fakes import concept
from nci_sip_mcp.evs import EVSClient
from nci_sip_mcp.registry import invoke
from test_audit import captured, records
from test_evs_client import FakeResponse, http_error
from test_server import ServerFixture


class SubsetsMappingsTest(ServerFixture):
    def setUp(self):
        super().setUp()
        self.mapping = {
            "targetCode": " X/1 ",
            "targetTerminology": "Other Target",
            "targetName": " Raw name ",
            "type": "Related To",
        }
        self.subset = {"type": "Concept_In_Subset", "relatedCode": "C2", "relatedName": "Second"}
        self.raw = concept("C1", active=True, associations=[self.subset], maps=[self.mapping])
        self.context.evs = EVSClient("https://example.invalid", max_attempts=1)

    def invoke_tool(self, kind, **arguments):
        return invoke(
            self.context,
            f"get_concept_{kind}",
            **({"terminology": "ncit", "release": "26.06e", "code": "C1"} | arguments),
        )

    def reply(self, kind, **arguments):
        with patch(
            "nci_sip_mcp.http_client._open",
            return_value=FakeResponse(json.dumps(self.raw).encode()),
        ) as opened:
            result = self.invoke_tool(kind, **arguments)
        return result, opened

    def test_subsets_select_exact_type_in_order_including_computed_associations(self):
        self.raw["associations"] = [
            self.subset | {"type": "Other"},
            self.subset | {"code": "A8", "relatedCode": "C3", "relatedName": " Third "},
            self.subset,
            self.subset | {"type": "concept_in_subset"},
        ]
        result, opened = self.reply("subsets")
        self.assertEqual(
            [
                {key: value for key, value in row.items() if key != "provenance"}
                for row in result["subsets"]
            ],
            [
                {"code": "C3", "terminology": "ncit", "name": " Third "},
                {"code": "C2", "terminology": "ncit", "name": "Second"},
            ],
        )
        self.assertEqual(opened.call_count, 1)
        self.assertEqual(
            opened.call_args.args[0].full_url,
            "https://example.invalid/api/v1/concept/ncit_26.06e/C1?include=minimal%2Cassociations",
        )
        self.assertEqual(result["subsets"][0]["provenance"]["release"]["identifier"], "26.06e")
        self.assertNotIn("provenance", result)

    def test_mapping_values_order_and_optional_target_version_survive_closed_projection(self):
        second = self.mapping | {
            "targetCode": "two",
            "targetTermType": " PT ",
            "targetTerminologyVersion": "v2",
        }
        self.raw["maps"] = [second | {"extra": "drop"}, self.mapping]
        result, opened = self.reply("mappings")
        self.assertEqual(
            [
                {key: value for key, value in row.items() if key != "provenance"}
                for row in result["mappings"]
            ],
            [second, self.mapping],
        )
        self.assertEqual(opened.call_count, 1)
        self.assertEqual(
            opened.call_args.args[0].full_url,
            "https://example.invalid/api/v1/concept/ncit_26.06e/C1?include=minimal%2Cmaps",
        )
        self.assertEqual(result["mappings"][0]["provenance"]["release"]["identifier"], "26.06e")
        self.assertNotIn("provenance", result)

    def test_null_empty_and_absent_optional_mapping_fields_are_omitted(self):
        for value in (None, ""):
            with self.subTest(value=value):
                self.raw["maps"] = [
                    self.mapping | {"targetTermType": value, "targetTerminologyVersion": value}
                ]
                result, _ = self.reply("mappings")
                self.assertNotIn("targetTermType", result["mappings"][0])
                self.assertNotIn("targetTerminologyVersion", result["mappings"][0])

    def test_target_filter_is_exact_and_empty_result_has_source_provenance(self):
        self.raw["maps"].append(self.mapping | {"targetTerminology": "other target"})
        for label, count in (
            ("Other Target", 1),
            ("other target", 1),
            ("OTHER TARGET", 0),
            ("", 0),
        ):
            with self.subTest(label=label):
                result, _ = self.reply("mappings", targetTerminology=label)
                self.assertEqual(len(result["mappings"]), count)
                self.assertTrue(
                    all(row["targetTerminology"] == label for row in result["mappings"])
                )
                source = result["mappings"][0] if count else result
                self.assertEqual(source["provenance"]["release"]["identifier"], "26.06e")

    def test_missing_required_fields_fail_instead_of_skipping_even_a_filtered_map(self):
        for field in self.mapping:
            with self.subTest(field=field):
                self.raw["maps"] = [
                    {key: value for key, value in self.mapping.items() if key != field}
                ]
                result, _ = self.reply("mappings", targetTerminology="does not match")
                self.assertEqual(result["error"]["code"], "upstream_unavailable")
                self.assertNotIn("mappings", result)
        for field in ("relatedCode", "relatedName"):
            with self.subTest(field=field):
                self.raw["associations"] = [
                    {key: value for key, value in self.subset.items() if key != field}
                ]
                result, _ = self.reply("subsets")
                self.assertEqual(result["error"]["code"], "upstream_unavailable")

    def test_bad_list_or_member_is_an_error_not_empty_or_partial(self):
        for kind, section in (("subsets", "associations"), ("mappings", "maps")):
            for value in (None, {}, [None], ["bad"]):
                with self.subTest(kind=kind, value=value):
                    self.raw[section] = value
                    result, _ = self.reply(kind)
                    self.assertEqual(result["error"]["code"], "upstream_unavailable")
                    self.assertNotIn(kind, result)

    def test_malformed_required_and_optional_values_are_errors(self):
        for fields in (
            {"targetCode": ""},
            {"targetName": 1},
            {"targetTermType": []},
            {"targetTerminologyVersion": 1},
        ):
            with self.subTest(fields=fields):
                self.raw["maps"] = [self.mapping | fields]
                result, _ = self.reply("mappings")
                self.assertEqual(result["error"]["code"], "upstream_unavailable")

    def test_absent_or_empty_lists_are_attributed_empty_results(self):
        self.raw["licenseText"] = "Upstream licence"
        for kind, section in (("subsets", "associations"), ("mappings", "maps")):
            for present in (True, False):
                with self.subTest(kind=kind, present=present):
                    self.raw[section] = []
                    if not present:
                        del self.raw[section]
                    result, _ = self.reply(kind)
                    self.assertEqual(result[kind], [])
                    self.assertEqual(result["provenance"]["attribution"], "Upstream licence")

    def test_attribution_comes_only_from_upstream_and_credentials_use_existing_client(self):
        self.context.evs = EVSClient(
            "https://example.invalid", license_key="test-only", max_attempts=1
        )
        self.raw["licenseText"] = "Source licence"
        self.raw["maps"][0]["licenseText"] = "Map licence"
        result, opened = self.reply("mappings")
        self.assertEqual(result["mappings"][0]["provenance"]["attribution"], "Map licence")
        self.assertEqual(
            opened.call_args.args[0].get_header("X-evsrestapi-license-key"), "test-only"
        )
        result, _ = self.reply("subsets")
        self.assertEqual(result["subsets"][0]["provenance"]["attribution"], "Source licence")
        del self.raw["licenseText"]
        result, _ = self.reply("subsets")
        self.assertNotIn("attribution", result["subsets"][0]["provenance"])

    def test_licence_text_of_another_type_is_rejected_not_passed_on(self):
        for kind in ("subsets", "mappings"):
            for licence in (7, {"unexpected": "object"}):
                with self.subTest(kind=kind, licence=licence):
                    self.raw["licenseText"] = licence
                    result, _ = self.reply(kind)
                    self.assertEqual(result["error"]["code"], "upstream_unavailable")
                    self.assertEqual(result["error"]["details"]["surface"], "evs")

    def test_wrong_identity_or_release_fails_closed(self):
        original = self.raw
        for fields, expected in (
            ({"code": "C9"}, "upstream_unavailable"),
            ({"terminology": "other"}, "upstream_unavailable"),
            ({"version": "other"}, "release_mismatch"),
        ):
            for kind in ("subsets", "mappings"):
                with self.subTest(fields=fields, kind=kind):
                    self.raw = original | fields
                    result, _ = self.reply(kind)
                    self.assertEqual(result["error"]["code"], expected)

    def test_invalid_code_is_rejected_before_network(self):
        for kind in ("subsets", "mappings"):
            with self.subTest(kind=kind), patch("nci_sip_mcp.http_client._open") as opened:
                result = self.invoke_tool(kind, code="C0")
                self.assertEqual(result["error"]["code"], "invalid_request")
                self.assertEqual(opened.call_count, 0)

    def test_target_label_is_plain_in_the_completion_audit(self):
        with captured() as stream:
            result, _ = self.reply("mappings", targetTerminology="Other Target")
        self.assertEqual(len(result["mappings"]), 1)
        self.assertEqual(records(stream)[0]["parameters"]["targetTerminology"], "Other Target")

    def test_association_without_type_cannot_disappear_as_a_non_subset(self):
        self.raw["associations"] = [{"relatedCode": "C2", "relatedName": "Second"}]
        result, _ = self.reply("subsets")
        self.assertEqual(result["error"]["code"], "upstream_unavailable")
        self.assertNotIn("subsets", result)

    def test_upstream_errors_are_never_empty_results(self):
        for kind in ("subsets", "mappings"):
            with (
                self.subTest(kind=kind),
                patch(
                    "nci_sip_mcp.http_client._open",
                    side_effect=http_error(503, b'{"message":"Unavailable"}'),
                ),
            ):
                result = self.invoke_tool(kind)
                self.assertEqual(result["error"]["code"], "upstream_unavailable")
                self.assertNotIn(kind, result)

    def test_protocol_schema_cache_and_optional_fields(self):
        self.context.evs = self.evs
        self.evs.concepts["C1"] = self.raw
        tools = {tool.name: tool for tool in self.session(lambda client: client.list_tools()).tools}
        for kind in ("subsets", "mappings"):
            with self.subTest(kind=kind):
                name = f"get_concept_{kind}"
                result = self.session(
                    lambda client, name=name: client.call_tool(
                        name, {"terminology": "ncit", "release": "26.06e", "code": "C1"}
                    )
                )
                self.assertFalse(result.is_error)
                Draft202012Validator(tools[name].output_schema).validate(result.structured_content)
                self.assertEqual(len(result.structured_content[kind]), 1)
                self.assertEqual(
                    (result.meta["ttlMs"], result.meta["cacheScope"]), (86_400_000, "public")
                )
