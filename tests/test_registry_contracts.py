"""Every served tool through the real SDK client: its declared cache decision or declared error.

`server._cache_results` refuses a successful call that made no cache decision, and supplies an
error's hint itself. Only a successful call therefore shows that a tool declares its policy;
the declared errors are listed separately with their exact contract (#401).
"""

import json
from contextlib import ExitStack
from operator import attrgetter
from pathlib import Path
from unittest.mock import patch

from fakes import (
    FakeSSIS,
    cde_response,
    code_map,
    data_element,
    form,
    primary_element,
    vm_response,
)
from nci_sip_mcp.registry import invoke
from test_server import ServerFixture

ROOT = Path(__file__).resolve().parents[1]
VALUE_SET = ROOT / "acceptance/fixtures/recorded/evs-fhir/expand-c85492.json"
LISTING = '<pre><a href="releasedCDEsXML-OD.zip">export</a> 2026-06-05 12:30\n</pre>'

# Expected hints come from spec/conventions.yaml M2.2 and M2.3, not from the implementation,
# except list_terminologies: M2.2 gives it no class, so its row repeats test_caching's
# status-tool test.
LONG = (86_400_000, "public")  # release-pinned NCIt content
# caDSR content no registry release pins, or joined with it: M2.2 bounds the TTL at 3,600,000,
# and the server uses that bound (caching.SHORT_TTL_MS).
SHORT = (3_600_000, "public")
STATUS = (0, "public")  # resolve tools and get_release_alignment (M2.2); list_terminologies too
COMPUTED = (0, "private")  # the tools marked computed in spec/tools.yaml
ERROR = (0, "private")  # the error hint, which server._cache_results supplies itself

PINNED = {"terminology": "ncit", "release": "26.06e"}
NO_REGISTRY_RELEASE = (("cadsr", "get_registry_releases", []),)
EXPORT = (*NO_REGISTRY_RELEASE, ("cadsr.export_http", "get_text", LISTING))

# Tool: (arguments, upstream answers as (client path, method, value), expected hint).
SUCCEEDS = {
    "get_concept": (PINNED | {"code": "C3262"}, (), LONG),
    "get_concepts": (PINNED | {"codes": ["C3262"]}, (), LONG),
    "list_relationships": (PINNED, (), LONG),
    "resolve_retired_code": (PINNED | {"code": "C3262"}, (), LONG),
    "get_concept_subsets": (PINNED | {"code": "C3262"}, (), LONG),
    "get_concept_mappings": (PINNED | {"code": "C3262"}, (), LONG),
    "search_concepts": (PINNED | {"query": "Neoplasm", "mode": "semantic"}, (), LONG),
    "get_concept_hierarchy": (PINNED | {"code": "C3262", "direction": "child"}, (), LONG),
    "get_concept_neighborhood": (PINNED | {"code": "C3262"}, (), LONG),
    "expand_value_set": (
        {"terminology": "ncit", "release": "26.09d", "valueSet": "C85492"},
        (("fhir", "get_json", json.loads(VALUE_SET.read_text())["response"]["body"]),),
        LONG,
    ),
    "resolve_release": ({"terminology": "ncit"}, (), STATUS),
    "list_terminologies": ({}, (), STATUS),
    "get_data_element": (
        {"publicId": "123"},
        (("cadsr", "get_data_element", data_element()),),
        SHORT,
    ),
    "search_data_elements": (
        {"query": "Contract"},
        (("cadsr", "search_data_elements", {"DataElements": [data_element()]}),),
        SHORT,
    ),
    "list_contexts": ({}, (("cadsr", "list_contexts", ["TEST"]),), SHORT),
    "get_form": ({"publicId": "123"}, (("cadsr", "get_form", form()),), SHORT),
    "get_code_map": ({}, (("cadsr", "get_crdc_list", [code_map()]),), SHORT),
    "resolve_registry_release": ({}, EXPORT, STATUS),
    "match_data_elements": (
        {"entities": [{"name": "Q"}]},
        (("cadsr", "match_data_element", cde_response()["matchResults"]),),
        COMPUTED,
    ),
    "match_value_meanings": (
        {"values": ["Q"]},
        (("cadsr", "match_value_meanings", vm_response()["matchResults"]),),
        COMPUTED,
    ),
    "find_data_elements_for_concept": ({"conceptCode": "C3262", "release": "26.06e"}, (), SHORT),
    "get_concept_for_permissible_value": (
        {"dataElementId": "123", "value": "Male", "release": "26.06e"},
        (
            (
                "cadsr",
                "get_data_element",
                primary_element(
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
        ),
        SHORT,
    ),
    "resolve_stored_value": (
        {"conceptCode": "C3262", "commons": "PDC", "release": "26.06e"},
        (("cadsr", "get_crdc_list", []),),
        SHORT,
    ),
    "get_release_alignment": ({}, EXPORT, STATUS),
    "ground_value": ({"conceptCode": "C3262", "release": "26.06e"}, (), SHORT),
    "expand_cohort": ({"conceptCode": "C3262", "release": "26.06e"}, (), LONG),
    "harmonize_data_dictionary": (
        {"columns": [{"name": "Q"}]},
        (("cadsr", "match_data_element", cde_response()["matchResults"]),),
        COMPUTED,
    ),
}

# Tools caDSR does not serve yet answer with their declared error on every valid call.
UNAVAILABLE = {
    "get_permissible_value": (
        {"permissibleValueId": "456"},
        "permissible-value retrieval by identifier (OP-C10)",
    ),
    "list_classification_schemes": ({}, "standalone classification scheme listing (OP-C13)"),
}


class ToolContractTest(ServerFixture):
    def setUp(self):
        super().setUp()
        self.context.ssis = FakeSSIS(self.settings)
        invoke(self.context, "index_codes", ["C3262", "C4741"])
        # Every upstream answer is scripted per tool; nothing may reach the network.
        refuse = patch(
            "nci_sip_mcp.http_client._open", side_effect=AssertionError("Unexpected HTTP")
        )
        refuse.start()
        self.addCleanup(refuse.stop)

    def answer(self, name, arguments, answers=()):
        """Call `name` over the SDK while each listed upstream client method returns its value."""
        with ExitStack() as stack:
            for path, method, value in answers:
                target = attrgetter(path)(self.context)
                stack.enter_context(patch.object(target, method, return_value=value))
            return self.session(lambda client: client.call_tool(name, arguments))

    def test_every_served_tool_has_exactly_one_scenario(self):
        served = {tool.name for tool in self.session(lambda client: client.list_tools()).tools}
        self.assertEqual(set(SUCCEEDS) & set(UNAVAILABLE), set())
        self.assertEqual(set(SUCCEEDS) | set(UNAVAILABLE), served)

    def test_every_supported_tool_succeeds_with_its_specified_cache_hint(self):
        for name, (arguments, answers, hint) in SUCCEEDS.items():
            with self.subTest(tool=name):
                result = self.answer(name, arguments, answers)
                self.assertFalse(result.is_error, result.content)
                self.assertEqual((result.meta["ttlMs"], result.meta["cacheScope"]), hint)

    def test_every_unserved_capability_is_its_declared_private_error(self):
        for name, (arguments, capability) in UNAVAILABLE.items():
            with self.subTest(tool=name):
                result = self.answer(name, arguments)
                self.assertTrue(result.is_error)
                error = result.structured_content["error"]
                self.assertEqual(error["code"], "capability_unavailable")
                self.assertEqual(error["details"], {"capability": capability})
                self.assertEqual((result.meta["ttlMs"], result.meta["cacheScope"]), ERROR)
