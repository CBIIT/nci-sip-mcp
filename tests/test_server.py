import asyncio
import json
import logging
import re
import sys
import tempfile
import unittest
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from importlib import metadata
from pathlib import Path
from unittest.mock import patch

import yaml
from mcp.client import Client
from mcp.server.session import ServerSession
from mcp.shared.exceptions import MCPError

from fakes import FakeEVS, concept, release
from nci_sip_mcp.config import Settings
from nci_sip_mcp.context import Context
from nci_sip_mcp.errors import correlated
from nci_sip_mcp.http_client import UpstreamUnavailableError
from nci_sip_mcp.index_adapter import LocalIndexAdapter, index_event
from nci_sip_mcp.registry import invoke
from nci_sip_mcp.server import INSTRUCTIONS, create_mcp
from ncit_index.embeddings import HashingEmbeddingProvider
from ncit_index.index import LocalIndex
from test_docs import QUICKSTART, bullet_names, section
from test_traversal import complete_graph

NEOPLASM = concept(
    "C3262",
    "Neoplasm",
    active=True,
    parents=[{"code": "C2991", "name": "Disease or Disorder"}],
    children=[{"code": "C4741", "name": "Neoplasm by Morphology"}],
    roles=[{"type": "Disease_Has_Abnormal_Cell", "relatedCode": "C12922", "relatedName": "Cell"}],
    inverseRoles=[
        {"type": "Gene_Associated_With_Disease", "relatedCode": "C16612", "relatedName": "Gene"}
    ],
    associations=[
        {"type": "Concept_In_Subset", "relatedCode": "C165258", "relatedName": "A Subset"}
    ],
)


MORPHOLOGY = concept("C4741", "Neoplasm by Morphology", active=True)


TOOLS = yaml.safe_load((Path(__file__).parents[1] / "spec/tools.yaml").read_text())


def _specified(spec):
    """What the specification says one tool's input schema states, as (argument, keyword): value.

    Forms (pattern), refusal limits (maxItems), defaults (a bound's, else its own), closed
    sets (values) and the maximum of each bound, which the argument's description states. A
    form keyed by terminology (the NCIt code form) is deliberately not served, since a schema
    cannot say which terminology a value belongs to; a closed set states the form of its
    argument by its members."""

    closed = spec.get("values", {})
    return (
        {(a, "pattern"): form for a, form in _forms(spec).items() if a not in closed}
        | {(a, "maxItems"): limit for a, limit in spec.get("lists", {}).items()}
        | {(a, "default"): value for a, value in _defaults(spec).items()}
        | {(a, "values"): set(members) for a, members in closed.items()}
        | {(a, "maximum"): b["maximum"] for a, b in _maxima(spec).items()}
    )


def _maxima(spec):
    return {a: b for a, b in spec.get("bounds", {}).items() if "maximum" in b}


def _forms(spec):
    return {a: form for a, form in spec.get("patterns", {}).items() if isinstance(form, str)}


def _defaults(spec):
    bounded = {n: b["default"] for n, b in spec.get("bounds", {}).items() if "default" in b}
    return {**spec.get("defaults", {}), **bounded}


def _served_as(schema, key, expected):
    """What the input schema states for `key`, an (argument, keyword) of `_specified`."""

    argument, keyword = key
    if keyword == "values":
        return _served_values(schema, argument)
    parameter = schema["properties"][argument]
    if keyword == "maximum":
        return expected if str(expected) in parameter.get("description", "") else None
    found = [
        alternative[keyword] for alternative in _alternatives(parameter) if keyword in alternative
    ]
    return found[0] if found else None


def _names_all(line, names):
    """Whether `line` names every one of `names` as a whole word."""

    return all(re.search(rf"\b{re.escape(name)}\b", line) for name in names)


def _alternatives(schema):
    """The schema of a parameter and of the alternatives and items it is built from."""

    found = [schema]
    for inner in [*schema.get("anyOf", []), *([schema["items"]] if "items" in schema else [])]:
        found += _alternatives(inner)
    return found


def _served_values(schema, argument):
    """The values the schema closes `argument` to: enum or const, or the keys of its object."""

    values = set()
    for found in _alternatives(schema["properties"][argument]):
        values |= set(found.get("enum", ())) | ({found["const"]} if "const" in found else set())
        if "$ref" in found:
            values |= set(schema["$defs"][found["$ref"].rpartition("/")[2]]["properties"])
    return values


def pinned(**arguments):
    return {"terminology": "ncit", "release": "26.06e"} | arguments


@patch("nci_sip_mcp.server.configure_logging")
class ServerStartupTest(unittest.TestCase):
    def test_missing_or_incompatible_mcp_package_is_explained(self, _):
        with (
            patch.dict(sys.modules, {"mcp.server.mcpserver": None}),
            self.assertRaises(RuntimeError) as raised,
        ):
            create_mcp(Settings())

        self.assertIn("'server' extra", str(raised.exception))
        self.assertIn(f"Import failed: {raised.exception.__cause__}", str(raised.exception))

    def test_sdk_session_without_the_connection_seam_fails_at_startup(self, _):
        def renamed(self, *args, **kwargs):
            self._link = None

        with (
            patch.object(ServerSession, "__init__", renamed),
            self.assertRaises(RuntimeError) as raised,
        ):
            create_mcp(Settings())

        self.assertIn("_connection", str(raised.exception))
        self.assertIn(f"mcp {metadata.version('mcp')}", str(raised.exception))


class ServerFixture(unittest.TestCase):
    def setUp(self):
        # Creating an MCPServer installs a root log handler; keep it quiet and
        # take it out again so later tests are not affected.
        root = logging.getLogger()
        self.addCleanup(setattr, root, "handlers", root.handlers[:])
        self.addCleanup(root.setLevel, root.level)
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.settings = Settings(data_dir=Path(directory.name))
        self.evs = complete_graph(FakeEVS(deepcopy([NEOPLASM, MORPHOLOGY])))
        self.local_index = LocalIndex(self.settings.data_dir, on_event=index_event)
        self.context = Context(
            self.settings,
            evs=self.evs,
            index=LocalIndexAdapter(self.local_index),
            embedding_provider=HashingEmbeddingProvider(),
        )

    def session(self, interaction):
        """Run `interaction(client)` against the server over an in-process MCP session."""

        async def run():
            async with Client(create_mcp(self.settings, context=self.context)) as client:
                return await interaction(client)

        return asyncio.run(run())

    def call(self, tool, **arguments):
        if tool in {
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
        }:
            arguments = pinned(**arguments)
        result = self.session(lambda client: client.call_tool(tool, arguments))
        return result.is_error, json.loads(result.content[0].text)

    def read(self, uri):
        async def interaction(client):
            # Caught inside the session: leaving it would wrap the error in a group.
            try:
                return await client.read_resource(uri)
            except MCPError as error:
                return error

        result = self.session(interaction)
        if isinstance(result, MCPError):
            raise result
        self.assertEqual(result.contents[0].mime_type, "application/json")
        return json.loads(result.contents[0].text)


@patch("nci_sip_mcp.server.configure_logging")
class ServerTest(ServerFixture):
    def test_server_reports_its_version_and_its_instructions(self, _):
        async def server_info(client):
            return client.server_info, client.instructions

        info, instructions = self.session(server_info)

        self.assertEqual(
            (info.name, info.version), ("nci-sip-mcp", metadata.version("nci-sip-mcp"))
        )
        self.assertRegex(instructions, r"2026-07-28 HTTP.*per call")
        self.assertRegex(instructions, r"pin.*handshake.*stdio")
        self.assertRegex(instructions, r"pass.*release.*first result's provenance.*stable")
        self.assertEqual(instructions, INSTRUCTIONS)

    def test_release_parameter_descriptions_explain_sessionless_resolution(self, _):
        tools = {tool.name: tool for tool in self.session(lambda client: client.list_tools()).tools}
        for name in ("get_concept", "expand_cohort"):
            with self.subTest(tool=name):
                description = tools[name].input_schema["properties"]["release"]["description"]
                self.assertRegex(description, r"Sessionless.*HTTP.*per call")

    def test_tools_are_registered_with_descriptions_and_closed_value_sets(self, _):
        tools = {tool.name: tool for tool in self.session(lambda client: client.list_tools()).tools}

        self.assertEqual(len(tools), 29)
        # The closed value sets are advertised in the schemas, wherever the
        # schema generator puts them.
        traverse_schema = json.dumps(tools["get_concept_neighborhood"].input_schema)
        for value in ("parent", "inverseRole", "inverseAssociation"):
            self.assertIn(f'"{value}"', traverse_schema)
        self.assertIn('"hybrid"', json.dumps(tools["search_concepts"].input_schema))
        for term in ("release", "include", "semanticType"):
            self.assertIn(term, tools["get_concept"].description)
        for term in ("depth", "exact=false", "budgetPerKind"):
            self.assertIn(term, tools["get_concept_neighborhood"].description)

    def test_search_description_advertises_pagination_without_nonexistent_truncation(self, _):
        tools = {tool.name: tool for tool in self.session(lambda client: client.list_tools()).tools}
        description = tools["search_concepts"].description
        self.assertIn("totalKnown", description)
        self.assertIn("nextCursor", description)
        search_contract = description.split("Call elapsed-time budget:", 1)[0]
        self.assertNotIn("truncation", search_contract)

    def test_tool_titles_follow_the_specification_in_each_profile(self, _):
        for profile in ("evs", "cadsr", "unified"):
            self.settings = replace(self.settings, profile=profile)
            tools = self.session(lambda client: client.list_tools()).tools
            for tool in tools:
                with self.subTest(profile=profile, tool=tool.name):
                    self.assertIsInstance(tool.title, str)
                    self.assertTrue(tool.title.strip())
                    self.assertEqual(tool.title, TOOLS[tool.name]["title"])

    def test_schemas_carry_no_generated_titles_and_output_schemas_name_their_root(self, _):
        tools = {tool.name: tool for tool in self.session(lambda client: client.list_tools()).tools}

        def text_titles(schema):
            # A property named "title" holds a schema, not text; only text titles count.
            if isinstance(schema, list):
                return [found for item in schema for found in text_titles(item)]
            if not isinstance(schema, dict):
                return []
            own = [schema["title"]] if isinstance(schema.get("title"), str) else []
            return own + text_titles(list(schema.values()))

        for name, tool in tools.items():
            titles = text_titles(tool.output_schema)
            self.assertEqual(titles, [f"{name} result"], name)
            self.assertNotIn("RootModel", json.dumps(tool.output_schema))
            self.assertEqual(text_titles(tool.input_schema), [], name)

    def test_every_parameter_of_every_tool_is_described_and_extras_are_closed(self, _):
        tools = {tool.name: tool for tool in self.session(lambda client: client.list_tools()).tools}

        def undescribed(schema):
            records = [schema, *schema.get("$defs", {}).values()]
            return [
                name
                for record in records
                for name, spec in record.get("properties", {}).items()
                if not spec.get("description", "").strip()
            ]

        for name, tool in tools.items():
            self.assertEqual(undescribed(tool.input_schema), [], name)
            self.assertIs(tool.input_schema["additionalProperties"], False, name)
            for record, definition in tool.input_schema.get("$defs", {}).items():
                self.assertIs(definition.get("additionalProperties"), False, (name, record))

    def test_input_schemas_state_what_the_specification_says_of_each_argument(self, _):
        tools = {tool.name: tool for tool in self.session(lambda client: client.list_tools()).tools}

        for name, spec in TOOLS.items():
            schema = tools[name].input_schema
            specified = _specified(spec)
            served = {key: _served_as(schema, key, specified[key]) for key in specified}
            self.assertEqual(served, specified, name)

    def test_first_sentences_are_one_plain_sentence_free_of_cache_vocabulary(self, _):
        tools = {tool.name: tool for tool in self.session(lambda client: client.list_tools()).tools}
        banned = re.compile(r"TTL|cache|budget|replay|0/private", re.IGNORECASE)

        for name, tool in tools.items():
            first = re.match(r"(.*?\.)(\s|$)", tool.description, flags=re.DOTALL)
            self.assertIsNotNone(first, name)
            self.assertNotIn("\n\n", first.group(1), name)
            self.assertIsNone(banned.search(first.group(1)), (name, first.group(1)))

    def test_no_description_names_another_served_tool(self, _):
        tools = {tool.name: tool for tool in self.session(lambda client: client.list_tools()).tools}

        def texts(tool):
            """The tool's description and the description of each parameter and record field."""

            schema = tool.input_schema
            records = [schema, *schema.get("$defs", {}).values()]
            fields = [f for r in records for f in r.get("properties", {}).values()]
            return [tool.description, *(f.get("description", "") for f in fields)]

        for name, tool in tools.items():
            named = {
                other
                for other in tools
                if other != name and any(re.search(rf"\b{other}\b", t) for t in texts(tool))
            }
            expected = {
                "find_data_elements_for_concept": {"get_data_element"},
                "ground_value": {"get_data_element"},
            }.get(name, set())
            self.assertEqual(named, expected, name)

    def test_the_instructions_carry_the_tool_selection_map_and_the_release_rule_once(self, _):
        # Each decision: the tools one line of the map sets side by side, and the wording that
        # says when to pick which.
        decisions = [
            (("get_concept", "get_concepts", "search_concepts"), "several codes"),
            (
                ("get_concept_hierarchy", "get_concept_neighborhood", "expand_cohort"),
                "parents, children or paths to the root.*roles and associations too.*exclusion",
            ),
            (("get_concept_subsets", "expand_value_set"), "members of a subset"),
            (
                ("get_data_element", "search_data_elements", "match_data_elements"),
                "discovery route",
            ),
            (
                ("find_data_elements_for_concept", "get_data_element", "conceptAssociations"),
                "Data element to concepts",
            ),
            (("resolve_stored_value", "get_code_map"), "crdcName"),
            (("resolve_retired_code", "get_concept_mappings", "get_form"), "retired"),
            (("list_classification_schemes", "get_data_element", "OP-C13"), "not served"),
            (("get_release_alignment",), "before joining"),
        ]
        lines = INSTRUCTIONS.splitlines()

        for names, wording in decisions:
            named = [ln for ln in lines if _names_all(ln, names)]
            self.assertTrue(any(re.search(wording, ln) for ln in named), (names, wording))
        self.assertEqual(INSTRUCTIONS.count("first implicit pin"), 1)
        tools = self.session(lambda client: client.list_tools()).tools
        self.assertEqual([t.name for t in tools if "implicit pin" in t.description], [])

    def test_every_upstream_gap_requirement_id_stays_in_the_description_that_stated_it(self, _):
        tools = {tool.name: tool for tool in self.session(lambda client: client.list_tools()).tools}
        # Requirement signals to the EVS and caDSR teams, taken from the descriptions of the
        # milestone before the rewrite; removing one would silence the signal.
        stated = {
            "get_concept_for_permissible_value": {"OP-C10"},
            "get_permissible_value": {"OP-C10"},
            "match_data_elements": {"C-1", "C-6"},
            "match_value_meanings": {"C-1"},
            "get_data_element": {"OP-C02"},
            "search_data_elements": {"OP-C03"},
            "list_classification_schemes": {"OP-C13"},
        }

        for name, ids in stated.items():
            found = set(re.findall(r"\bOP-[A-Z]\d+\b|\bC-\d+\b", tools[name].description))
            self.assertLessEqual(ids, found, name)

    def test_the_matching_tools_share_a_filters_base_and_only_matching_adds_a_scheme(self, _):
        tools = {tool.name: tool for tool in self.session(lambda client: client.list_tools()).tools}

        def served(tool):
            schema = tools[tool].input_schema
            ref = next(s["$ref"] for s in schema["properties"]["filters"]["anyOf"] if "$ref" in s)
            return ref, schema["$defs"][ref.rpartition("/")[2]]["properties"]

        search_ref, search = served("search_data_elements")
        self.assertEqual(search_ref, "#/$defs/SearchFilters")
        self.assertNotIn("classificationScheme", search)
        for tool in ("match_data_elements", "harmonize_data_dictionary"):
            ref, match = served(tool)
            self.assertEqual(ref, "#/$defs/MatchFilters", tool)
            self.assertEqual(set(match), {*search, "classificationScheme"}, tool)

    def test_expand_value_set_refuses_another_terminology_as_an_invalid_request(self, _):
        failed, result = self.call("expand_value_set", terminology="other", valueSet="C85492")

        self.assertTrue(failed)
        self.assertEqual(result["error"]["code"], "invalid_request")
        self.assertEqual(result["error"]["details"]["parameter"], "terminology")

    def test_search_data_elements_still_refuses_a_classification_scheme_filter(self, _):
        failed, result = self.call(
            "search_data_elements",
            query="stage",
            filters={"classificationScheme": {"publicId": "1", "version": "1.0"}},
        )

        self.assertTrue(failed)
        self.assertEqual(result["error"]["code"], "invalid_request")
        self.assertEqual(result["error"]["details"]["parameter"], "filters")

    def test_parameters_the_platform_does_not_serve_yet_name_their_requirement(self, _):
        tools = {tool.name: tool for tool in self.session(lambda client: client.list_tools()).tools}
        requirements = {
            ("get_data_element", "registryRelease"): "C-1",
            ("get_data_element", "longName"): "OP-C02",
            ("search_data_elements", "filters"): "OP-C03",
            ("get_concept_for_permissible_value", "permissibleValueId"): "OP-C10",
            ("match_data_elements", "modelVariant"): "C-6",
            ("match_data_elements", "similarityThreshold"): "C-6",
            ("get_form", "keyword"): "Form/query",
        }
        for (tool, name), requirement in requirements.items():
            text = tools[tool].input_schema["properties"][name]["description"]
            self.assertIn(requirement, text, (tool, name))
            self.assertIn("leave unset", text.lower(), (tool, name))

    def test_a_value_off_a_stated_form_is_the_invalid_request_error_record(self, _):
        failed, result = self.call("get_form", publicId="0")

        self.assertTrue(failed)
        self.assertEqual(result["error"]["code"], "invalid_request")
        self.assertEqual(result["error"]["details"]["parameter"], "publicId")

    def test_quickstart_lists_exactly_the_public_tools(self, _):
        tools = {tool.name: tool for tool in self.session(lambda client: client.list_tools()).tools}

        # The names in backticks that start the rows of the generated table.
        names = set(re.findall(r"^\| `(\w+)` \|", section(QUICKSTART, "MCP Tools"), flags=re.M))

        self.assertEqual(names, set(tools))

    def test_optional_arguments_have_the_documented_defaults(self, _):
        tools = {tool.name: tool for tool in self.session(lambda client: client.list_tools()).tools}

        def defaults(tool):
            properties = tools[tool].input_schema["properties"]
            return {name: spec["default"] for name, spec in properties.items() if "default" in spec}

        self.assertEqual(
            defaults("search_concepts"),
            {"release": None, "limit": 10, "mode": "lexical", "cursor": None, "retired": "include"},
        )
        self.assertEqual(defaults("get_concept"), {"release": None, "include": None})
        self.assertEqual(
            defaults("get_concept_neighborhood"),
            {
                "release": None,
                "depth": 2,
                "maxNodes": 200,
                "maxEdges": 1000,
                "budgetPerKind": None,
                "kinds": None,
                "includeNegative": False,
            },
        )

    def test_resources_and_templates_are_registered_as_documented_json(self, _):
        templates = self.session(lambda client: client.list_resource_templates()).resource_templates
        resources = self.session(lambda client: client.list_resources()).resources

        self.assertEqual(len(templates), 5)
        self.assertEqual(
            {str(resource.uri) for resource in resources},
            {"cadsr://registry/release", "cadsr://crosswalk/crdc"},
        )
        self.assertEqual(
            {template.uri_template for template in templates}
            | {str(resource.uri) for resource in resources},
            set(bullet_names(section(QUICKSTART, "MCP Resources"))),
        )
        for template in [*templates, *resources]:
            self.assertEqual(template.mime_type, "application/json")
            self.assertTrue(template.description)

    def test_tools_return_the_service_results(self, _):
        invoke(self.context, "index_codes", ["C3262"])

        is_error, lookup = self.call("get_concept", code="C3262")
        self.assertFalse(is_error)
        self.assertEqual((lookup["code"], lookup["provenance"]["source"]), ("C3262", "evs_rest"))

        _, search = self.call("search_concepts", query="neoplasm", mode="hybrid", limit=1)
        self.assertEqual([hit["concept"]["code"] for hit in search["results"]], ["C3262"])

        _, traversal = self.call("get_concept_neighborhood", code="C3262", depth=1, kinds=["child"])
        self.assertEqual([edge["sourceCode"] for edge in traversal["edges"]], ["C4741"])

        _, info = self.call("resolve_release", terminology="ncit")
        self.assertEqual(
            {key: info[key] for key in ("terminology", "channel", "version", "date")},
            {
                "terminology": "ncit",
                "channel": "monthly",
                "version": "26.06e",
                "date": "2026-06-29",
            },
        )

    def test_release_resources_emit_the_resolver_record_for_the_requested_version(self, _):
        expected = invoke(self.context, "resolve_release", "ncit")
        result = self.read("ncit://release/26.06e")
        self.assertEqual(
            {key: value for key, value in result.items() if key != "provenance"},
            {key: value for key, value in expected.items() if key != "provenance"},
        )
        self.assertEqual(result["provenance"]["release"], expected["provenance"]["release"])

    def test_removed_monthly_aliases_are_refused(self, _):
        for alias in ("monthly", "monthly-latest", "current", "latest"):
            with self.subTest(alias), self.assertRaises(MCPError) as raised:
                self.read(f"ncit://release/{alias}")
            error = json.loads(str(raised.exception))["error"]
            self.assertEqual(error["code"], "release_not_available")
            self.assertEqual(error["details"], {"requested": alias, "source": "evs"})

    def test_pinned_resource_reports_its_weekly_channel(self, _):
        self.context.settings = replace(self.settings, release_channel="weekly")
        self.evs.release = release("26.07a", "2026-07-06", channel="weekly")

        report = self.read("ncit://release/26.07a")

        self.assertEqual(report["channel"], "weekly")
        self.assertEqual(report["version"], "26.07a")
        self.assertNotIn("selected_monthly_release", report)

    def test_cli_only_lookup_flags_are_rejected_by_the_public_tool(self, _):
        for flag in ("live_only", "include_raw"):
            with self.subTest(flag):
                failed, result = self.call("get_concept", code="C3262", **{flag: True})
                self.assertTrue(failed)
                self.assertEqual(result["error"]["code"], "invalid_request")
                self.assertEqual(result["error"]["details"]["parameter"], flag)
        self.assertEqual(self.evs.calls, [])

    def test_kind_budget_limits_nodes_through_the_mcp_adapter(self, _):
        self.evs.concepts["C3262"] = dict(self.evs.concepts["C3262"])
        self.evs.concepts["C3262"]["children"] = [
            {"code": "C2", "name": "Two"},
            {"code": "C3", "name": "Three"},
        ]
        complete_graph(self.evs)
        is_error, result = self.call(
            "get_concept_neighborhood",
            code="C3262",
            depth=1,
            kinds=["child"],
            budgetPerKind=1,
        )
        self.assertFalse(is_error)
        self.assertEqual([node["code"] for node in result["nodes"]], ["C3262", "C2"])
        self.assertEqual(result["truncation"]["bound"], "kind_budget")

    def test_every_tool_argument_shapes_the_result(self, _):
        invoke(self.context, "index_codes", ["C3262", "C4741"])
        arguments = {"query": "neoplasm", "mode": "semantic"}
        is_error, search = self.call("search_concepts", **arguments)
        self.assertEqual((is_error, len(search["results"])), (False, 2))
        _, search = self.call("search_concepts", limit=1, **arguments)
        self.assertEqual(len(search["results"]), 1)
        _, walk = self.call(
            "get_concept_neighborhood",
            code="C3262",
            depth=1,
            kinds=["role", "inverseRole"],
            maxEdges=1,
        )
        self.assertEqual(len(walk["edges"]), 1)
        self.assertEqual(walk["truncation"]["bound"], "edges")
        self.assertEqual(walk["truncation"]["limit"], 1)

    def test_get_concept_does_not_fall_back_to_an_index(self, _):
        invoke(self.context, "index_codes", ["C3262"])
        self.evs.errors = {"get_concept": UpstreamUnavailableError("down")}
        is_error, failed = self.call("get_concept", code="C3262")
        self.assertEqual((is_error, failed["error"]["code"]), (True, "upstream_unavailable"))

    def test_error_envelopes_are_flagged_as_protocol_errors(self, _):
        failures = (
            ("not_found", "get_concept", {"code": "C999"}),
            ("invalid_request", "get_concept", {"code": "../bad"}),
            ("capability_unavailable", "search_concepts", {"query": "tumor", "mode": "semantic"}),
            ("not_found", "get_concept_neighborhood", {"code": "C999"}),
        )
        for code, tool, arguments in failures:
            with self.subTest(tool=tool, code=code):
                is_error, envelope = self.call(tool, **arguments)
                self.assertTrue(is_error)
                self.assertEqual(envelope["error"]["code"], code)
                self.assertTrue(envelope["error"]["message"])

        invoke(self.context, "index_codes", ["C3262"])
        (self.settings.data_dir / "nci_si.sqlite3").write_bytes(b"not a database" * 100)
        is_error, envelope = self.call("search_concepts", query="tumor", mode="hybrid")
        self.assertTrue(is_error)
        self.assertEqual(envelope["error"]["code"], "internal_error")

    def test_an_error_is_the_error_record_as_structured_content_and_as_text(self, _):
        result = self.session(lambda client: client.call_tool("get_concept", pinned(code="C999")))

        self.assertTrue(result.is_error)
        self.assertEqual(set(result.structured_content), {"error"})
        self.assertEqual(result.structured_content, json.loads(result.content[0].text))

    def test_the_error_record_returns_the_correlation_identifier_of_the_call(self, _):
        def failing_lookup(meta):
            return self.session(
                lambda client: client.call_tool("get_concept", pinned(code="C999"), meta=meta)
            ).structured_content["error"]["correlationId"]

        self.assertEqual(failing_lookup({"correlationId": "caller-42"}), "caller-42")
        first, second = failing_lookup(None), failing_lookup({})
        self.assertTrue(first)
        self.assertNotEqual(first, second)

    def test_every_tool_that_can_fail_honours_the_correlation_identifier(self, _):
        calls = {
            "search_concepts": pinned(query="tumor", mode="semantic"),
            "get_concept_neighborhood": pinned(code="C999"),
        }
        for tool, arguments in calls.items():
            with self.subTest(tool):
                result = self.session(
                    lambda client, tool=tool, arguments=arguments: client.call_tool(
                        tool, arguments, meta={"correlationId": "c-1"}
                    )
                )

                self.assertTrue(result.is_error)
                self.assertEqual(result.structured_content["error"]["correlationId"], "c-1")

    def test_every_item_of_a_tool_result_carries_the_correlation_identifier_of_the_call(self, _):
        invoke(self.context, "index_codes", ["C3262"])
        calls = {
            "get_concept": pinned(code="C3262"),
            "search_concepts": pinned(query="neoplasm", mode="semantic"),
            "get_concept_neighborhood": pinned(code="C3262", depth=1),
        }

        def provenances(content):
            nodes = content.get("nodes", []) + content.get("edges", [])
            concepts = [hit["concept"] for hit in content.get("results", [])]
            return [item["provenance"] for item in (nodes or concepts or [content])]

        for tool, arguments in calls.items():
            with self.subTest(tool):
                result = self.session(
                    lambda client, tool=tool, arguments=arguments: client.call_tool(
                        tool, arguments, meta={"correlationId": "c-7"}
                    )
                )

                found = provenances(json.loads(result.content[0].text))
                self.assertTrue(found)
                self.assertEqual({each["correlationId"] for each in found}, {"c-7"})

    def test_no_tool_offers_the_raw_payload_and_no_result_carries_it(self, _):
        invoke(self.context, "index_codes", ["C3262"])
        tools = self.session(lambda client: client.list_tools()).tools

        for tool in tools:
            self.assertNotIn("include_raw", tool.input_schema.get("properties", {}), tool.name)
        _, lookup = self.call("get_concept", code="C3262")
        _, search = self.call("search_concepts", query="neoplasm", mode="semantic")
        self.assertNotIn("raw", lookup)
        self.assertNotIn("raw", search["results"][0]["concept"])

    def test_a_resource_error_carries_a_correlation_identifier_and_details(self, _):
        with self.assertRaises(MCPError) as raised:
            self.read("ncit://release/99.99z")

        error = json.loads(str(raised.exception))["error"]
        self.assertTrue(error["correlationId"])
        self.assertEqual(error["details"], {"requested": "99.99z", "source": "evs"})

    def test_an_unavailable_release_resource_names_the_next_discovery_step(self, _):
        self.context.settings = replace(self.settings, release_channel="weekly")
        self.evs.release = release("26.07a", "2026-07-06", channel="weekly")

        with self.assertRaises(MCPError) as raised:
            self.read("ncit://release/99.99z")

        error = json.loads(str(raised.exception))["error"]
        self.assertEqual(error["code"], "release_not_available")
        self.assertIn("resolve_release", error["message"])
        self.assertEqual(error["details"], {"requested": "99.99z", "source": "evs"})

    def test_the_index_manifest_of_another_release_is_not_available(self, _):
        invoke(self.context, "index_codes", ["C3262"])

        with self.assertRaises(MCPError) as raised:
            self.read("ncit://index/manifest/99.99z")

        error = json.loads(str(raised.exception))["error"]
        self.assertEqual(error["code"], "release_mismatch")
        self.assertEqual(
            error["details"], {"requested": "99.99z", "served": ["26.06e"], "source": "index"}
        )

    def failing_read_ids(self, uri):
        """The correlation identifier of a failing resource read, and those it opened."""

        opened = []

        @contextmanager
        def spy(*arguments):
            with correlated(*arguments) as value:
                opened.append(value)
                yield value

        with patch("nci_sip_mcp.audit.correlated", spy), self.assertRaises(MCPError) as raised:
            self.read(uri)
        return json.loads(str(raised.exception))["error"]["correlationId"], opened

    def test_each_resource_read_runs_under_one_correlation_identifier(self, _):
        invoke(self.context, "index_codes", ["C3262"])
        for uri in (
            "ncit://concept/26.06e/C999",
            "ncit://release/99.99z",
            "ncit://index/manifest/99.99z",
        ):
            with self.subTest(uri):
                identifier, opened = self.failing_read_ids(uri)

                self.assertEqual(opened, [identifier])

    def test_resolve_release_fails_closed_when_evs_is_down(self, _):
        self.evs.errors = {
            "get_api_version": UpstreamUnavailableError("down"),
            "get_terminologies": UpstreamUnavailableError("down"),
        }

        is_error, info = self.call("resolve_release", terminology="ncit")

        self.assertTrue(is_error)
        self.assertEqual(info["error"]["code"], "upstream_unavailable")

    def test_resources_route_by_version(self, _):
        with self.assertRaises(MCPError) as raised:
            self.read("ncit://index/manifest/26.06e")
        self.assertEqual(
            json.loads(str(raised.exception))["error"]["code"], "capability_unavailable"
        )
        invoke(self.context, "index_codes", ["C3262"])

        self.assertEqual(self.read("ncit://concept/26.06e/C3262")["provenance"]["servedBy"], "live")
        self.assertEqual(self.read("ncit://release/26.06e")["version"], "26.06e")
        manifest = self.read("ncit://index/manifest/26.06e")
        self.assertEqual(manifest["concepts"], 1)
        self.assertEqual(manifest["provenance"]["source"], "evs_index")
        self.assertEqual(manifest["provenance"]["servedBy"], "index")

    def test_concept_resource_does_not_fall_back_to_the_sample_index(self, _):
        invoke(self.context, "index_codes", ["C3262"])

        self.assertNotIn("raw", self.read("ncit://concept/26.06e/C3262"))
        self.evs.errors = {"get_concept": UpstreamUnavailableError("down")}
        with self.assertRaises(MCPError) as raised:
            self.read("ncit://concept/26.06e/C3262")
        self.assertEqual(json.loads(str(raised.exception))["error"]["code"], "upstream_unavailable")

    def test_resource_failures_are_protocol_errors_carrying_the_envelope(self, _):
        invoke(self.context, "index_codes", ["C3262"])
        failures = {
            "ncit://concept/26.06e/C999": "not_found",
            "ncit://release/99.99z": "release_not_available",
            "ncit://index/manifest/99.99z": "release_mismatch",
        }
        for uri, code in failures.items():
            with self.subTest(uri):
                with self.assertRaises(MCPError) as raised:
                    self.read(uri)
                self.assertEqual(json.loads(str(raised.exception))["error"]["code"], code)

        self.evs.errors = {"get_terminologies": UpstreamUnavailableError("down")}
        with self.assertRaises(MCPError) as raised:
            self.read("ncit://release/26.06e")
        envelope = json.loads(str(raised.exception))
        self.assertEqual(
            (envelope["error"]["code"], envelope["error"]["message"]),
            ("upstream_unavailable", "down. Retry later."),
        )

        (self.settings.data_dir / "nci_si.sqlite3").write_bytes(b"not a database" * 100)
        for uri in ("ncit://index/manifest/26.06e",):
            with self.subTest(uri):
                with self.assertRaises(MCPError) as raised:
                    self.read(uri)
                self.assertEqual(
                    json.loads(str(raised.exception))["error"]["code"], "internal_error"
                )


if __name__ == "__main__":
    unittest.main()
