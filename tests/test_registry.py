import json
from dataclasses import replace
from pathlib import Path
from typing import Annotated, Literal, TypedDict
from unittest import TestCase
from unittest.mock import patch

import yaml
from jsonschema import Draft202012Validator
from pydantic import TypeAdapter

from nci_sip_mcp import cli, results
from nci_sip_mcp.caching import cache_call, select_cache_hint
from nci_sip_mcp.context import Context
from nci_sip_mcp.http_client import (
    UpstreamRejectedError,
    UpstreamTimeoutError,
    UpstreamTooLargeError,
    UpstreamUnavailableError,
)
from nci_sip_mcp.registry import OPERATIONS, ToolSpec, cli_arguments, invoke
from test_server import ServerFixture


class Echo(TypedDict):
    code: str
    limit: int
    mode: str


class RegistryTest(ServerFixture):
    def test_profiles_select_only_the_current_inventory_and_keep_group_metadata(self):
        evs = {
            "get_concept",
            "get_concepts",
            "list_relationships",
            "resolve_retired_code",
            "get_concept_subsets",
            "expand_value_set",
            "get_concept_mappings",
            "search_concepts",
            "get_concept_hierarchy",
            "get_concept_neighborhood",
            "resolve_release",
            "list_terminologies",
        }
        cadsr = {
            "get_data_element",
            "search_data_elements",
            "list_contexts",
            "list_classification_schemes",
            "resolve_registry_release",
            "get_form",
            "get_permissible_value",
            "get_code_map",
            "match_data_elements",
            "match_value_meanings",
        }
        seam = {
            "find_data_elements_for_concept",
            "get_concept_for_permissible_value",
            "resolve_stored_value",
            "get_release_alignment",
        }
        workflows = {"ground_value", "expand_cohort", "harmonize_data_dictionary"}
        expected = {"evs": evs, "cadsr": cadsr, "unified": evs | cadsr | seam | workflows}
        for profile, names in expected.items():
            with self.subTest(profile=profile):
                self.settings = replace(self.settings, profile=profile)
                tools = self.session(lambda client: client.list_tools()).tools
                self.assertEqual({tool.name for tool in tools}, names)
                for tool in tools:
                    groups = {
                        **dict.fromkeys(evs, "evs"),
                        **dict.fromkeys(cadsr, "cadsr"),
                        **dict.fromkeys(seam, "cross-domain"),
                        **dict.fromkeys(workflows, "workflow"),
                    }
                    self.assertEqual(tool.meta["group"], groups[tool.name])

    def test_every_tool_advertises_all_four_read_only_annotations(self):
        tools = self.session(lambda client: client.list_tools()).tools
        expected = {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        }
        for tool in tools:
            with self.subTest(tool=tool.name):
                actual = tool.annotations.model_dump(by_alias=True)
                self.assertEqual({key: actual[key] for key in expected}, expected)

    def test_known_writing_operations_are_neither_tools_nor_resources(self):
        # Identified from their handlers: they build, activate or add to the local index, or
        # record an evaluation (evaluation.evaluate_build). The guard covers these declared
        # operations; a side effect added to another handler later is not detected here.
        writers = {"index_codes", "index_build", "index_rebuild", "index_activate", "evaluate"}
        self.assertEqual(
            {
                operation: (OPERATIONS[operation].name, OPERATIONS[operation].uri)
                for operation in writers
            },
            dict.fromkeys(writers, (None, None)),
        )

    def test_a_single_new_declaration_drives_both_adapters(self):
        def echo(context: Context, code: str, limit: int = 3, mode: Literal["a", "b"] = "a"):
            """Echo the shared arguments."""
            return {"code": code, "limit": limit, "mode": mode}

        spec = ToolSpec(echo, "evs", Echo, False, name="echo", command="echo")
        with (
            patch("nci_sip_mcp.server.SPECS", (spec,)),
            patch("nci_sip_mcp.cli.SPECS", (spec,)),
            patch.dict(OPERATIONS, {spec.operation: spec}),
        ):
            parser = cli.build_parser()
            default = cli._run(self.context, parser.parse_args(["echo", "C1"]))
            changed = cli._run(
                self.context, parser.parse_args(["echo", "C1", "--limit", "7", "--mode", "b"])
            )
            result = self.session(
                lambda client: client.call_tool("echo", {"code": "C1", "limit": 7, "mode": "b"})
            )
        self.assertEqual(default, {"code": "C1", "limit": 3, "mode": "a"})
        self.assertEqual(changed, {"code": "C1", "limit": 7, "mode": "b"})
        self.assertEqual(result.structured_content, changed)
        self.assertEqual(json.loads(result.content[0].text), changed)

    def test_cli_maintenance_commands_are_available_in_every_profile(self):
        for profile in ("evs", "cadsr", "unified"):
            with self.subTest(profile=profile):
                self.context.settings = replace(self.settings, profile=profile)
                args = cli.build_parser().parse_args(["index-sample", "C3262"])
                result = cli._run(self.context, args)
                self.assertEqual(result["concepts"], 1)
                self.assertEqual(cli.build_parser().parse_args(["evaluate"]).operation, "evaluate")

    def test_handler_owned_cache_policy_has_no_registry_default(self):
        def content(context: Context):
            before = dict(hint)
            select_cache_hint(resolution=False, unpinned=True)
            return {"before": before}

        spec = ToolSpec(content, "cross-domain", dict)
        with cache_call() as hint, patch.dict(OPERATIONS, {spec.operation: spec}):
            result = invoke(self.context, spec.operation)
        self.assertEqual(result, {"before": {}})
        self.assertEqual(hint, {"ttlMs": 3_600_000, "cacheScope": "public"})

    def test_upstream_details_survive_the_boundary_without_an_evs_bridge(self):
        failures = {
            UpstreamUnavailableError: "upstream_unavailable",
            UpstreamRejectedError: "upstream_unavailable",
            UpstreamTimeoutError: "timeout",
            UpstreamTooLargeError: "bound_exceeded",
        }
        details = {"surface": "EVS", "status": 503, "attempts": 3}
        for kind, code in failures.items():
            with self.subTest(kind=kind.__name__):
                self.evs.errors["get_concept"] = kind("failed", **details)
                result = invoke(
                    self.context, "lookup", "C3262", live_only=True, _correlation_id="registry-call"
                )
                self.assertEqual(result["error"]["code"], code)
                self.assertEqual(result["error"]["details"], details)
                self.assertEqual(result["error"]["correlationId"], "registry-call")


ROOT = Path(__file__).resolve().parents[1]


class DeclarationTest(TestCase):
    """A declaration that would silently lose an input or invent a CLI flag is refused."""

    def test_a_handler_that_cannot_take_the_context_first_is_refused_by_name(self):
        def unannotated(context, code: str):
            return {}

        def public_first(code: str, context: Context):
            return {}

        def no_parameters():
            return {}

        def keyword_context(*, context: Context):
            return {}

        for handler in (unannotated, public_first, no_parameters, keyword_context):
            with (
                self.subTest(handler=handler.__name__),
                self.assertRaisesRegex(ValueError, f"^{handler.__name__} must take the Context"),
            ):
                ToolSpec(handler, "evs", dict, False)

    def test_the_context_type_not_its_spelling_marks_the_injected_parameter(self):
        # list_classification_schemes names its public caDSR context filter `context`.
        def schemes(ctx: Context, context: str | None = None):
            return {}

        self.assertEqual(
            [p.name for p in ToolSpec(schemes, "cadsr", dict, False).parameters], ["context"]
        )

    def test_a_true_default_flag_without_a_declared_cli_spelling_is_refused(self):
        def plain(context: Context, enabled: bool = True):
            return {"enabled": enabled}

        def described(context: Context, enabled: Annotated[bool, "Described"] = True):
            return {"enabled": enabled}

        def optional(context: Context, enabled: bool | None = True):
            return {"enabled": enabled}

        for handler in (plain, described, optional):
            with (
                self.subTest(handler=handler.__name__),
                self.assertRaisesRegex(
                    ValueError, f"^{handler.__name__}: enabled defaults to True"
                ),
            ):
                ToolSpec(handler, "evs", dict, False)

    def test_declared_negative_flags_and_false_defaults_remain_accepted(self):
        def legacy(context: Context, include_hierarchy: bool = True, verbose: bool = False):
            return {"include_hierarchy": include_hierarchy, "verbose": verbose}

        spec = ToolSpec(legacy, "evs", dict, False, command="legacy")
        flags = [flag for (flag,), _ in cli_arguments(spec)]
        self.assertEqual(flags, ["--no-hierarchy", "--verbose"])


ITEM_TYPES = {
    "concept": "Concept",
    "node": "Node",
    "edge": "Edge",
    "subset": "Subset",
    "mapping": "Mapping",
    "replacement": "Replacement",
    "relationship": "CatalogueRelationship",
    "search_result": "RankedConcept",
    "member": "ValueSetMember",
    "terminology": "TerminologyResult",
    "index_manifest": "IndexManifestResult",
    "error": "ErrorRecord",
}


class ContentRecordSchemaTest(TestCase):
    def test_content_item_keys_and_optionality_match_specification(self):
        records = yaml.safe_load((ROOT / "spec/records.yaml").read_text())
        for name, type_name in ITEM_TYPES.items():
            with self.subTest(record=name):
                fields = records[name]["fields"]
                schema = TypeAdapter(getattr(results, type_name)).json_schema()
                required = {key for key, field in fields.items() if not field.get("optional")}
                self.assertEqual(set(schema["properties"]), set(fields))
                self.assertEqual(set(schema["required"]), required)

    def test_catalogue_closed_values_match_specification(self):
        fields = yaml.safe_load((ROOT / "spec/records.yaml").read_text())["relationship"]["fields"]
        schema = TypeAdapter(results.CatalogueRelationship).json_schema()
        for key in ("kind", "polarity"):
            with self.subTest(key=key):
                self.assertEqual(set(schema["properties"][key]["enum"]), set(fields[key]["values"]))

    def test_provenance_traversal_and_truncation_closed_values_match_specification(self):
        records = yaml.safe_load((ROOT / "spec/records.yaml").read_text())
        for record, type_name, field in (
            ("provenance", "Provenance", "source"),
            ("provenance", "Provenance", "servedBy"),
            ("traversal", "TraversalProvenance", "polarity"),
            ("truncation", "Truncated", "bound"),
        ):
            with self.subTest(record=record, field=field):
                schema = TypeAdapter(getattr(results, type_name)).json_schema()
                if "$ref" in schema:
                    schema = schema["$defs"][schema["$ref"].rsplit("/", 1)[1]]
                self.assertEqual(
                    set(schema["properties"][field].get("enum", [])),
                    set(records[record]["fields"][field]["values"]),
                )

    def test_list_envelopes_preserve_required_counts_and_missing_identifiers(self):
        tools = yaml.safe_load((ROOT / "spec/tools.yaml").read_text())
        for tool, record, field in (
            ("get_concepts", results.ConceptBatch, "missing"),
            ("expand_value_set", results.ValueSetExpansion, "total"),
        ):
            with self.subTest(tool=tool):
                self.assertIn(field, tools[tool]["returns"])
                self.assertNotIn(field + "?", tools[tool]["returns"])
                self.assertIn(field, TypeAdapter(record).json_schema()["required"])
        # The specification permits omission; this implementation guarantees the count.
        self.assertIn("totalKnown", TypeAdapter(results.ConceptSearch).json_schema()["required"])


def validator(operation):
    return Draft202012Validator(TypeAdapter(OPERATIONS[operation].output).json_schema())


class OutputSchemaValidationTest(ServerFixture):
    def test_provenance_correlation_is_required_in_output_schema(self):
        result = invoke(self.context, "lookup", "C3262")
        schema = validator("lookup")
        self.assertTrue(schema.is_valid(result))
        del result["provenance"]["correlationId"]
        self.assertFalse(schema.is_valid(result))

    def test_occurred_truncation_cannot_claim_false_with_counts(self):
        result = invoke(self.context, "traverse", ["C3262"], max_nodes=1)
        record = result["truncation"]
        schema = Draft202012Validator(TypeAdapter(results.Truncated).json_schema())
        self.assertTrue(schema.is_valid(record))
        record["occurred"] = False
        self.assertFalse(schema.is_valid(record))

    def test_fallback_reason_is_a_closed_value_in_lookup_schema(self):
        invoke(self.context, "index_codes", ["C3262"])
        self.evs.errors["get_concept"] = UpstreamUnavailableError("offline")
        result = invoke(self.context, "lookup", "C3262")
        schema = validator("lookup")
        self.assertTrue(schema.is_valid(result))
        self.assertEqual(result["fallback"]["reason"], "upstream_unavailable")
        result["fallback"]["reason"] = "invented"
        self.assertFalse(schema.is_valid(result))

    def test_release_report_requires_active_index_even_when_null(self):
        result = invoke(self.context, "release_info")
        schema = validator("release_info")
        self.assertTrue(schema.is_valid(result))
        self.assertIsNone(result.pop("active_index"))
        self.assertFalse(schema.is_valid(result))

    def test_search_schema_rejects_unknown_legacy_mode(self):
        invoke(self.context, "index_codes", ["C3262"])
        result = invoke(self.context, "search", "Neoplasm")
        schema = validator("search")
        self.assertTrue(schema.is_valid(result))
        result["mode"] = "invented"
        self.assertFalse(schema.is_valid(result))

    def test_traversal_schema_rejects_unknown_edge_type(self):
        result = invoke(self.context, "traverse", ["C3262"], max_depth=1)
        schema = validator("traverse")
        self.assertTrue(schema.is_valid(result))
        self.assertTrue(result["edges"])
        result["edges"][0]["edge_type"] = "invented"
        self.assertFalse(schema.is_valid(result))

    def test_release_report_accepts_selected_release_error_arm(self):
        self.evs.errors["get_terminologies"] = UpstreamUnavailableError("offline")
        result = invoke(self.context, "release_info")
        self.assertEqual(result["selected_release"]["error"]["code"], "upstream_unavailable")
        self.assertNotIn("error", result)
        self.assertTrue(validator("release_info").is_valid(result))

    def test_traversal_schema_rejects_unknown_assertion_direction(self):
        result = invoke(self.context, "traverse", ["C3262"], max_depth=1)
        schema = validator("traverse")
        self.assertTrue(schema.is_valid(result))
        result["edges"][0]["provenance"]["direction"] = "sideways"
        self.assertFalse(schema.is_valid(result))
