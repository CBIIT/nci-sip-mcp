import json
import re
from copy import deepcopy
from dataclasses import replace
from itertools import product
from typing import get_args
from unittest.mock import patch

from jsonschema import Draft202012Validator

from fakes import release
from nci_sip_mcp.config import BASE_URL_VARIABLES
from nci_sip_mcp.errors import ErrorCode, PlatformError, serialise
from nci_sip_mcp.http_client import UpstreamUnavailableError
from nci_sip_mcp.registry import ToolSpec, invoke
from nci_sip_mcp.validation import PROFILES, RELEASE_CHANNELS, UPSTREAM_MODES
from test_server import ServerFixture, pinned


class SchemaTest(ServerFixture):
    def test_truncation_schema_rejects_unknown_bounds_and_fields_on_complete_results(self):
        validator = self.validators()["get_concept_hierarchy"]
        complete = {"nodes": [], "truncation": {"occurred": False}}
        validator.validate(complete)
        invalid = dict(complete, truncation={"occurred": False, "bound": "nodes"})
        self.assertFalse(validator.is_valid(invalid))
        invalid["truncation"] = {
            "occurred": True,
            "bound": "typo",
            "limit": 1,
            "reached": 1,
            "omitted": 1,
            "exact": False,
        }
        self.assertFalse(validator.is_valid(invalid))

    def validators(self):
        listing = self.session(lambda client: client.list_tools())
        return {tool.name: Draft202012Validator(tool.output_schema) for tool in listing.tools}

    def result(self, name, **arguments):
        if name in {
            "get_concept",
            "search_concepts",
            "get_concept_hierarchy",
            "get_concept_neighborhood",
        }:
            arguments = pinned(**arguments)
        result = self.session(lambda client: client.call_tool(name, arguments))
        self.assertEqual(result.structured_content, json.loads(result.content[0].text))
        return result.structured_content

    def test_every_schema_is_valid_and_rejects_malformed_errors(self):
        validators = self.validators()
        self.assertEqual(len(validators), 29)
        for name, validator in validators.items():
            with self.subTest(tool=name):
                validator.check_schema(validator.schema)
                self.assertFalse(validator.is_valid({}))
                self.assertFalse(validator.is_valid({"error": {"message": "missing code"}}))
                self.assertFalse(
                    validator.is_valid(
                        {"error": {"code": "unknown", "message": "retry", "correlationId": "call"}}
                    )
                )
                for code in get_args(ErrorCode):
                    validator.validate(serialise(PlatformError(code, "Retry", reason="test")))

    def test_current_success_shapes_validate_without_rewriting_fields(self):
        invoke(self.context, "index_codes", ["C3262", "C4741"])
        calls = {
            "get_concept": {"code": "C3262"},
            "search_concepts": {"query": "Neoplasm", "limit": 1, "mode": "semantic"},
            "get_concept_neighborhood": {"code": "C3262", "maxNodes": 1, "depth": 1},
            "get_concept_hierarchy": {"code": "C3262", "direction": "child"},
            "resolve_release": {"terminology": "ncit"},
            "list_terminologies": {},
        }
        validators = self.validators()
        for name, arguments in calls.items():
            with self.subTest(tool=name):
                data = self.result(name, **arguments)
                validators[name].validate(data)
                self.assertNotIn("result", data)
                broken = dict(data)
                broken.pop(next(iter(data)))
                self.assertFalse(validators[name].is_valid(broken))

    def test_lookup_schema_checks_nested_types_and_preserves_upstream_fields(self):
        self.evs.concepts["C3262"]["definitions"] = [{"definition": "A definition"}]
        data = self.result("get_concept", code="C3262")
        validator = self.validators()["get_concept"]
        validator.validate(data)
        self.assertEqual(data["provenance"]["upstream"]["version"], "26.06e")
        self.assertIn("sourceUri", data["provenance"])
        broken = deepcopy(data)
        broken["provenance"]["release"]["identifier"] = 42
        self.assertFalse(validator.is_valid(broken))
        broken = deepcopy(data)
        broken["code"] = 42
        self.assertFalse(validator.is_valid(broken))
        for field in ("source", "servedBy"):
            with self.subTest(field=field):
                broken = deepcopy(data)
                broken["provenance"][field] = "typo"
                self.assertFalse(validator.is_valid(broken))

    def test_release_schema_rejects_an_unknown_channel(self):
        data = self.result("resolve_release", terminology="ncit")
        validator = self.validators()["resolve_release"]
        validator.validate(data)
        data["channel"] = "typo"
        self.assertFalse(validator.is_valid(data))

    def test_empty_search_and_concept_optional_fields_validate(self):
        invoke(self.context, "index_codes", ["C3262"])
        validators = self.validators()
        with patch("ncit_index.index.rank_page", return_value=([], 0)):
            empty = self.result("search_concepts", query="zzzzzz", mode="semantic")
        validators["search_concepts"].validate(empty)
        self.assertEqual(empty["results"], [])
        self.assertIn("provenance", empty)
        self.evs.concepts["C3262"].pop("conceptStatus", None)
        concept = self.result("get_concept", code="C3262", include=["synonyms"])
        validators["get_concept"].validate(concept)
        self.assertNotIn("status", concept)
        self.assertEqual(concept["synonyms"], [])

    def test_discovery_errors_are_top_level_and_have_no_success_fields(self):
        self.evs.errors["get_terminologies"] = UpstreamUnavailableError("offline")
        self.evs.errors["get_api_version"] = UpstreamUnavailableError("offline")
        data = self.result("resolve_release", terminology="ncit")
        validator = self.validators()["resolve_release"]
        validator.validate(data)
        self.assertNotIn("provenance", data)
        self.assertEqual(set(data), {"error"})
        self.assertEqual(data["error"]["code"], "upstream_unavailable")
        broken = deepcopy(data)
        broken["error"]["code"] = "unknown"
        self.assertFalse(validator.is_valid(broken))

    def test_recursive_kind_truncation_and_node_types_are_checked(self):
        data = self.result("get_concept_neighborhood", code="C3262", maxNodes=1)
        validator = self.validators()["get_concept_neighborhood"]
        validator.validate(data)
        self.assertTrue(data["truncation"]["occurred"])
        self.assertIn("perKind", data["truncation"])
        broken = deepcopy(data)
        first = next(iter(broken["truncation"]["perKind"].values()))
        first["omitted"] = "not a count"
        self.assertFalse(validator.is_valid(broken))
        broken = deepcopy(data)
        broken["nodes"][0]["provenance"]["depth"] = "zero"
        self.assertFalse(validator.is_valid(broken))

    def test_real_error_results_validate(self):
        calls = {
            "get_concept": {"code": "bad"},
            "search_concepts": {"query": "", "mode": "semantic"},
            "get_concept_neighborhood": {"code": "C3262", "kinds": []},
        }
        validators = self.validators()
        for name, arguments in calls.items():
            with self.subTest(tool=name):
                data = self.result(name, **arguments)
                validators[name].validate(data)
                self.assertEqual(data["error"]["code"], "invalid_request")

    def test_error_record_requires_all_shared_fields(self):
        valid = serialise(PlatformError("not_found", "Use an existing code"))
        for name, validator in self.validators().items():
            validator.validate(valid)
            for field in ("code", "message", "correlationId"):
                with self.subTest(tool=name, field=field):
                    broken = deepcopy(valid)
                    del broken["error"][field]
                    self.assertFalse(validator.is_valid(broken))

    def test_occurred_truncation_requires_its_bound_and_counts(self):
        data = self.result("get_concept_neighborhood", code="C3262", maxNodes=1)
        validator = self.validators()["get_concept_neighborhood"]
        self.assertTrue(data["truncation"]["occurred"])
        for field in ("bound", "limit", "reached", "omitted", "exact"):
            with self.subTest(field=field):
                broken = deepcopy(data)
                del broken["truncation"][field]
                self.assertFalse(validator.is_valid(broken))

    def listing_bytes(self):
        listing = self.session(lambda client: client.list_tools())
        return json.dumps(
            [tool.model_dump(by_alias=True) for tool in listing.tools], sort_keys=True
        )

    def test_schema_surface_is_static_in_every_configuration(self):
        before = {}
        for profile in PROFILES:
            self.settings = replace(self.settings, profile=profile)
            before[profile] = self.listing_bytes()
        urls = dict.fromkeys(BASE_URL_VARIABLES, "https://fixture.test")
        for profile, channel, mode in product(PROFILES, RELEASE_CHANNELS, UPSTREAM_MODES):
            with self.subTest(profile=profile, channel=channel, mode=mode):
                self.settings = replace(
                    self.settings,
                    profile=profile,
                    release_channel=channel,
                    upstream_mode=mode,
                    **urls,
                )
                self.evs.release = release("99.01a", channel=channel)
                self.assertEqual(self.listing_bytes(), before[profile])

    def test_schema_surface_is_static_after_calls_and_upstream_failure(self):
        before = self.listing_bytes()
        self.result("get_concept", code="C3262")
        self.assertEqual(self.listing_bytes(), before)
        self.evs.errors["get_concept"] = UpstreamUnavailableError("offline")
        self.result("get_concept", code="C3262")
        self.assertEqual(self.listing_bytes(), before)

    def test_descriptions_have_no_unfinished_text_or_unsupported_search_values(self):
        listing = self.session(lambda client: client.list_tools())
        text = json.dumps([tool.model_dump(by_alias=True) for tool in listing.tools])
        self.assertIsNone(re.search(r"\b(TODO|FIXME|XXX|TBD|HACK|placeholder|debug)\b", text, re.I))
        search = next(tool for tool in listing.tools if tool.name == "search_concepts")
        modes = search.input_schema["properties"]["mode"]["enum"]
        self.assertEqual(set(modes), {"lexical", "typeahead", "semantic", "hybrid"})
        for unsupported in ("lucene", "regex", "contains"):
            with self.subTest(mode=unsupported):
                self.assertNotIn(f"`{unsupported}`", search.description)
                response = self.session(
                    lambda client, mode=unsupported: client.call_tool(
                        "search_concepts", pinned(query="term", mode=mode)
                    )
                )
                self.assertTrue(response.is_error)

    def test_missing_output_declaration_fails_at_registration(self):
        def undeclared(context):
            return {"anything": True}

        with self.assertRaisesRegex(TypeError, "output"):
            ToolSpec(handler=undeclared, group="evs", resolution=False)
