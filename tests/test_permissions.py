import asyncio
import json
import logging
import unittest
from dataclasses import FrozenInstanceError, replace
from time import time
from unittest.mock import patch

from mcp.client import Client
from mcp.shared.exceptions import MCPError

from fakes import cde_response, cde_row, concept, primary_element
from nci_sip_mcp import cadsr_content, cadsr_matching, seam, workflows
from nci_sip_mcp.caching import cache_call
from nci_sip_mcp.errors import PlatformError, correlated
from nci_sip_mcp.permissions import (
    Authority,
    PolicyUnavailableError,
    Principal,
    authority_scope,
    permits,
    require,
    secured,
)
from nci_sip_mcp.registry import invoke
from nci_sip_mcp.server import create_mcp
from test_evs_client import FakeResponse
from test_index import normalized
from test_release_selection import initialize_http
from test_seam import gdc_map
from test_server import ServerFixture, pinned
from test_transport import concept_response, http_app
from test_transport import result as http_result
from test_workflows import WorkflowFixture


def authority(*capabilities, **changes):
    value = Authority(
        Principal("https://issuer.example", "subject", tenant="tenant", client="client"),
        frozenset(capabilities),
        "policy-1",
        time() + 60,
    )
    return replace(value, **changes)


class AuthorityTest(unittest.TestCase):
    def test_scope_marks_the_request_secured_only_while_it_lasts(self):
        self.assertTrue(permits("get_concept"))
        for scoped in (None, authority("get_concept")):
            with self.subTest(authority=scoped), authority_scope(scoped):
                self.assertTrue(secured())
            self.assertFalse(secured())

    def test_a_secured_request_permits_only_granted_tools_the_server_has(self):
        with authority_scope(authority("get_concept", "no_such_tool")):
            self.assertTrue(permits("get_concept"))
            self.assertFalse(permits("lookup"))
            self.assertFalse(permits("no_such_tool"))
        with authority_scope(None):
            self.assertFalse(permits("get_concept"))

    def test_snapshot_cannot_gain_permissions_from_a_mutated_input_set(self):
        capabilities = {"get_concept"}
        snapshot = authority(capabilities=capabilities)
        capabilities.add("get_data_element")
        with authority_scope(snapshot), self.assertRaises(PlatformError) as raised:
            require("get_data_element")
        self.assertEqual(raised.exception.code, "permission_denied")

    def test_unbounded_expiry_does_not_grant_access(self):
        with (
            authority_scope(authority("get_concept", expires_at=float("inf"))),
            self.assertRaises(PlatformError) as raised,
        ):
            require("get_concept")
        self.assertEqual(raised.exception.code, "permission_denied")

    def test_unknown_capability_cannot_grant_an_operation(self):
        with authority_scope(authority("invented")), self.assertRaises(PlatformError) as raised:
            require("invented")
        self.assertEqual(raised.exception.code, "permission_denied")

    def test_a_held_snapshot_identity_cannot_be_reassigned(self):
        with self.assertRaises(FrozenInstanceError):
            authority().principal.subject = "other"

    def test_missing_expired_and_unversioned_policy_denies(self):
        policies = [
            None,
            authority("get_concept", expires_at=0),
            authority("get_concept", policy_version=""),
        ]
        for policy in policies:
            with self.subTest(policy=policy), authority_scope(policy):
                with self.assertRaises(PlatformError) as raised:
                    require("get_concept")
                self.assertEqual(raised.exception.code, "permission_denied")
                self.assertEqual(raised.exception.details, {})

    def test_scope_restores_outer_authority_even_after_failure(self):
        with authority_scope(authority("get_concept")):
            with self.assertRaises(PlatformError), authority_scope(None):
                require("get_concept")
            require("get_concept")
            with self.assertRaises(PlatformError) as raised:
                require("get_data_element")
        self.assertEqual(raised.exception.code, "permission_denied")
        require("get_data_element")  # Trusted-local scope is restored.


class RegistryPermissionTest(ServerFixture):
    def test_incomplete_principal_cannot_retrieve_content_from_shared_registry(self):
        for field in ("issuer", "subject"):
            self.evs.calls.clear()
            snapshot = authority("get_concept")
            snapshot = replace(snapshot, principal=replace(snapshot.principal, **{field: ""}))
            with self.subTest(field=field), authority_scope(snapshot):
                result = invoke(self.context, "get_concept", **pinned(code="C3262"))
                self.assertEqual(set(result), {"error"})
                self.assertEqual(result["error"]["code"], "permission_denied")
                self.assertEqual(self.evs.calls, [])

    def test_shared_producers_cannot_select_public_hints_under_caller_authority(self):
        with authority_scope(authority("get_concept")), cache_call() as hint:
            result = invoke(self.context, "get_concept", **pinned(code="C3262"))
        self.assertEqual(result["code"], "C3262")
        self.assertEqual(hint, {"ttlMs": 0, "cacheScope": "private"})

    def test_denied_call_has_no_content_or_upstream_requests(self):
        before = list(self.evs.calls)
        with authority_scope(authority("get_data_element")):
            result = invoke(
                self.context, "get_concept", **pinned(code="C3262"), _correlation_id="denied"
            )
        self.assertEqual(set(result), {"error"})
        self.assertEqual(result["error"]["code"], "permission_denied")
        self.assertEqual(result["error"]["correlationId"], "denied")
        self.assertNotIn("C3262", str(result))
        self.assertEqual(self.evs.calls, before)

    def test_allowed_call_returns_retrieved_content(self):
        with authority_scope(authority("get_concept")):
            result = invoke(self.context, "get_concept", **pinned(code="C3262"))
        self.assertEqual(result["code"], "C3262")
        self.assertEqual(result["name"], "Neoplasm")


class SurfacePermissionTest(ServerFixture):
    def test_expired_policy_refuses_catalogue_instead_of_looking_like_empty_access(self):
        current = authority()

        async def resolve():
            return current

        async def scenario():
            nonlocal current
            server = create_mcp(self.settings, context=self.context, authority_resolver=resolve)
            async with Client(server, cache=None) as client:
                self.assertEqual((await client.list_tools()).tools, [])
                current = replace(current, expires_at=0)
                with self.assertRaises(MCPError) as refused:
                    await client.list_tools()
                self.assertEqual(refused.exception.data["error"]["code"], "permission_denied")

        asyncio.run(scenario())
        self.assertEqual(self.evs.calls, [])

    def test_secured_stdio_cannot_reassign_a_sessions_principal(self):
        current = authority("get_concept")

        async def resolve():
            return current

        async def scenario():
            nonlocal current
            server = create_mcp(self.settings, context=self.context, authority_resolver=resolve)
            async with Client(server) as client:
                first = await client.call_tool(
                    "get_concept", {"terminology": "ncit", "code": "C3262"}
                )
                self.assertFalse(first.is_error)
                current = replace(current, principal=replace(current.principal, tenant="other"))
                second = await client.call_tool(
                    "get_concept", {"terminology": "ncit", "code": "C3262"}
                )
                self.assertEqual(second.structured_content["error"]["code"], "permission_denied")

        asyncio.run(scenario())

    def test_profile_remains_an_upper_bound_even_when_policy_grants_other_groups(self):
        self.settings = replace(self.settings, profile="evs")

        async def inspect(client):
            self.assertEqual((await client.list_tools()).tools, [])
            denied = await client.call_tool("get_data_element", {"publicId": "2200604"})
            self.assertEqual(denied.structured_content["error"]["code"], "permission_denied")

        self.secured_session(["get_data_element"], inspect)

    def test_allowed_resource_returns_content_and_allowed_prompt_renders(self):
        async def inspect(client):
            prompts = await client.list_prompts()
            self.assertEqual([row.name for row in prompts.prompts], ["protocol_authoring"])
            prompt = await client.get_prompt("protocol_authoring", {"concepts": "C3262"})
            self.assertIn("C3262", str(prompt.messages))
            resource = await client.read_resource("ncit://concept/26.06e/C3262")
            self.assertIn('"code": "C3262"', resource.contents[0].text)
            self.assertEqual((resource.ttl_ms, resource.cache_scope), (0, "private"))

        self.secured_session(
            [
                "get_concept",
                "resolve_release",
                "find_data_elements_for_concept",
                "get_data_element",
                "get_form",
            ],
            inspect,
        )

    def secured_session(self, capabilities, interaction):
        async def resolve():
            return authority(*capabilities)

        async def run():
            server = create_mcp(self.settings, context=self.context, authority_resolver=resolve)
            async with Client(server) as client:
                return await interaction(client)

        return asyncio.run(run())

    def test_catalogues_are_filtered_and_private(self):
        async def inspect(client):
            tools = await client.list_tools()
            prompts = await client.list_prompts()
            resources = await client.list_resources()
            templates = await client.list_resource_templates()
            self.assertEqual([tool.name for tool in tools.tools], ["get_concept"])
            self.assertEqual(prompts.prompts, [])
            self.assertEqual(resources.resources, [])
            self.assertEqual(
                [str(row.uri_template) for row in templates.resource_templates],
                ["ncit://concept/{release}/{code}"],
            )
            for result in (tools, prompts, resources, templates):
                self.assertEqual(result.cache_scope, "private")
                self.assertEqual(result.ttl_ms, 0)

        self.secured_session(["get_concept"], inspect)

    def test_policy_is_resolved_again_for_calls_and_catalogues(self):
        current = authority("get_concept")
        # ServerFixture silences logging; this test reads the diagnostic.
        disabled = logging.root.manager.disable
        logging.disable(logging.NOTSET)
        self.addCleanup(logging.disable, disabled)

        async def resolve():
            if isinstance(current, Exception):
                raise current
            return current

        async def run():
            nonlocal current
            async with Client(
                create_mcp(self.settings, context=self.context, authority_resolver=resolve)
            ) as client:
                first = await client.call_tool("get_concept", pinned(code="C3262"))
                self.assertFalse(first.is_error)
                current = authority("get_data_element", policy_version="policy-2")
                denied = await client.call_tool("get_concept", pinned(code="C3262"))
                self.assertEqual(denied.structured_content["error"]["code"], "permission_denied")
                self.assertEqual(
                    [t.name for t in (await client.list_tools()).tools], ["get_data_element"]
                )
                current = PolicyUnavailableError("private policy backend information")
                with self.assertLogs("nci_sip_mcp.server_permissions", level="WARNING") as logs:
                    failed = await client.call_tool("get_concept", pinned(code="C3262"))
                self.assertEqual(failed.structured_content["error"]["code"], "permission_denied")
                self.assertNotIn("backend", str(failed))
                record = json.loads(logs.records[0].getMessage())
                self.assertEqual(record["event"], "policy_unavailable")
                self.assertEqual(record["errorType"], "PolicyUnavailableError")
                self.assertNotIn("backend", logs.records[0].getMessage())
                current = authority("get_concept", policy_version="policy-3")
                self.assertFalse(
                    (await client.call_tool("get_concept", pinned(code="C3262"))).is_error
                )

        asyncio.run(run())

    def test_guessed_tools_are_generic_denials_before_any_request(self):
        async def inspect(client):
            for name in ("get_concept", "invented"):
                result = await client.call_tool(name, {"code": "C3262"})
                self.assertTrue(result.is_error)
                self.assertEqual(result.structured_content["error"]["code"], "permission_denied")
                self.assertEqual(
                    result.content[0].text,
                    json.dumps(result.structured_content, separators=(",", ":"), sort_keys=True),
                )
                self.assertNotIn(name, str(result.structured_content))
                self.assertEqual(result.meta["cacheScope"], "private")
            self.assertEqual(self.evs.calls, [])

        self.secured_session(["get_data_element"], inspect)

    def test_guessed_resource_and_prompt_are_denied(self):
        async def inspect(client):
            with self.assertRaises(MCPError) as resource:
                await client.read_resource("ncit://concept/26.06e/C3262")
            self.assertEqual(resource.exception.data["error"]["code"], "permission_denied")
            with self.assertRaises(MCPError) as prompt:
                await client.get_prompt("protocol_authoring", {"concepts": "C3262"})
            self.assertEqual(prompt.exception.data["error"]["code"], "permission_denied")
            self.assertEqual(self.evs.calls, [])

        self.secured_session(["get_data_element"], inspect)

    def test_explicit_release_content_is_still_private(self):
        async def inspect(client):
            result = await client.call_tool("get_concept", pinned(code="C3262"))
            self.assertFalse(result.is_error)
            self.assertEqual(result.structured_content["code"], "C3262")
            self.assertEqual(result.meta["ttlMs"], 0)
            self.assertEqual(result.meta["cacheScope"], "private")

        self.secured_session(["get_concept"], inspect)


class CompoundPermissionTest(WorkflowFixture):
    def test_expiry_during_cohort_roles_blocks_hierarchy_read(self):
        now = time()
        read = self.evs.get_concepts_by_codes
        selections = []

        def concepts(*args, **kwargs):
            nonlocal now
            selections.append(kwargs.get("include"))
            rows = read(*args, **kwargs)
            now += 120
            return rows

        with (
            authority_scope(
                authority("expand_cohort", "get_concept_neighborhood", "get_concept_hierarchy")
            ),
            patch("nci_sip_mcp.permissions.time", side_effect=lambda: now),
            patch.object(self.evs, "get_concepts_by_codes", side_effect=concepts),
        ):
            result = self.call("expand_cohort", conceptCode="C1")
        self.assertEqual(set(result), {"error"})
        self.assertEqual(result["error"]["code"], "permission_denied")
        self.assertEqual(selections, ["minimal,roles"])

    def test_expiry_during_search_blocks_grounding_concept_read(self):
        now = time()

        def search(*args, **kwargs):
            nonlocal now
            now += 120
            return 1, [concept("C1", active=True)]

        with (
            authority_scope(
                authority(
                    "ground_value",
                    "search_concepts",
                    "get_concept",
                    "find_data_elements_for_concept",
                )
            ),
            patch("nci_sip_mcp.permissions.time", side_effect=lambda: now),
            patch.object(self.evs, "search_concepts", create=True, side_effect=search),
        ):
            result = self.call(text="Neoplasm")
        self.assertEqual(set(result), {"error"})
        self.assertEqual(result["error"]["code"], "permission_denied")
        self.assertNotIn(("get_concept", "ncit_26.06e", "C1"), self.evs.calls)
        self.assertEqual(self.ssis.calls, [])

    def test_expiry_during_cohort_release_discovery_blocks_neighborhood_reads(self):
        now = time()
        read = self.evs.get_terminologies
        release_reads = []

        def releases(*args, **kwargs):
            nonlocal now
            rows = read(*args, **kwargs)
            release_reads[:] = self.evs.calls
            now += 120
            return rows

        with (
            authority_scope(
                authority("expand_cohort", "get_concept_neighborhood", "get_concept_hierarchy")
            ),
            patch("nci_sip_mcp.permissions.time", side_effect=lambda: now),
            patch.object(self.evs, "get_terminologies", side_effect=releases),
        ):
            result = self.call("expand_cohort", conceptCode="C1")
        self.assertEqual(set(result), {"error"})
        self.assertEqual(result["error"]["code"], "permission_denied")
        self.assertEqual(self.evs.calls, release_reads)

    def test_expiry_after_value_resolution_blocks_the_concept_child(self):
        def assert_expired(source):
            now = time()

            def values(*args, **kwargs):
                nonlocal now
                now += 120
                return primary_element(
                    publicId="123",
                    value="Male",
                    codes=[{"conceptCode": "C1", "primaryIndicator": "Yes", **source}],
                )

            with (
                authority_scope(
                    authority(
                        "get_concept_for_permissible_value", "get_data_element", "get_concept"
                    )
                ),
                patch("nci_sip_mcp.permissions.time", side_effect=lambda: now),
                patch.object(self.context.cadsr, "get_data_element", side_effect=values),
            ):
                result = self.call(
                    "get_concept_for_permissible_value", dataElementId="123", value="Male"
                )
            self.assertEqual(set(result), {"error"})
            self.assertEqual(result["error"]["code"], "permission_denied")
            self.assertNotIn(("get_concept", "ncit_26.06e", "C1"), self.evs.calls)

        for source in ({}, {"evsSource": "NCI_CONCEPT_CODE"}):
            with self.subTest(source=source):
                assert_expired(source)

    def test_expiry_during_release_discovery_blocks_text_search(self):
        now = time()
        read = self.evs.get_terminologies

        def releases(*args, **kwargs):
            nonlocal now
            rows = read(*args, **kwargs)
            now += 120
            return rows

        with (
            authority_scope(
                authority(
                    "ground_value",
                    "search_concepts",
                    "get_concept",
                    "find_data_elements_for_concept",
                )
            ),
            patch("nci_sip_mcp.permissions.time", side_effect=lambda: now),
            patch.object(self.evs, "get_terminologies", side_effect=releases),
            patch.object(
                self.evs, "search_concepts", create=True, return_value=(0, [])
            ) as searches,
        ):
            result = self.call(text="Neoplasm")
        self.assertEqual(set(result), {"error"})
        self.assertEqual(result["error"]["code"], "permission_denied")
        self.assertEqual(searches.call_count, 0)

    def test_expiry_during_release_discovery_blocks_permissible_value_read(self):
        now = 1000
        read = self.evs.get_terminologies

        def releases(*args, **kwargs):
            nonlocal now
            rows = read(*args, **kwargs)
            now += 120
            return rows

        with (
            authority_scope(
                authority(
                    "get_concept_for_permissible_value",
                    "get_data_element",
                    "get_concept",
                    expires_at=1060,
                )
            ),
            patch("nci_sip_mcp.permissions.time", side_effect=lambda: now),
            patch.object(self.evs, "get_terminologies", side_effect=releases),
            patch(
                "nci_sip_mcp.http_client._open",
                return_value=FakeResponse(json.dumps(primary_element()).encode()),
            ) as requests,
        ):
            result = self.call(
                "get_concept_for_permissible_value",
                dataElementId="2178439",
                value="To a large extent",
            )
        self.assertEqual(set(result), {"error"})
        self.assertEqual(result["error"]["code"], "permission_denied")
        self.assertTrue(self.evs.calls)
        self.assertEqual(requests.call_args_list, [])

    def test_denied_dictionary_child_precedes_registry_pin_discovery(self):
        with (
            authority_scope(authority("harmonize_data_dictionary")),
            patch.object(self.context.cadsr, "get_registry_releases", return_value=[]) as reads,
        ):
            result = self.call(
                "harmonize_data_dictionary", columns=[{"name": "Q"}], registryRelease="published"
            )
        self.assertEqual(set(result), {"error"})
        self.assertEqual(result["error"]["code"], "permission_denied")
        self.assertEqual(reads.call_count, 0)

    def test_denied_value_concept_child_precedes_graph_and_release_reads(self):
        for child in ("get_concept", "get_data_element"):
            with (
                self.subTest(child=child),
                authority_scope(authority("get_concept_for_permissible_value", child)),
                patch.object(
                    self.context.cadsr,
                    "get_data_element",
                    side_effect=AssertionError("Denied read"),
                ),
            ):
                result = self.call(
                    "get_concept_for_permissible_value", dataElementId="123", value="II"
                )
                self.assertEqual(set(result), {"error"})
                self.assertEqual(result["error"]["code"], "permission_denied")
        self.assertEqual(self.evs.calls, [])
        self.assertEqual(self.ssis.calls, [])

    def test_child_permissions_cannot_grant_the_direct_workflow_parent(self):
        with (
            authority_scope(authority("get_concept", "find_data_elements_for_concept")),
            cache_call(),
            correlated(),
            self.assertRaises(PlatformError) as refused,
        ):
            workflows.ground_value(self.context, conceptCode="C1")
        self.assertEqual(refused.exception.code, "permission_denied")
        self.assertEqual(self.evs.calls, [])
        self.assertEqual(self.ssis.calls, [])

    def test_expiry_between_dictionary_columns_stops_matching_without_partial_content(self):
        now = time()
        requested = []

        def match(entity, headers):
            nonlocal now
            requested.append(entity["entity"])
            now += 120
            return cde_response(entity["entity"], [cde_row()])["matchResults"]

        with (
            authority_scope(authority("harmonize_data_dictionary", "match_data_elements")),
            patch("nci_sip_mcp.permissions.time", side_effect=lambda: now),
            patch.object(self.context.cadsr, "match_data_element", side_effect=match),
        ):
            result = self.call(
                "harmonize_data_dictionary", columns=[{"name": "Q"}, {"name": "Later"}]
            )
        self.assertEqual(set(result), {"error"})
        self.assertEqual(result["error"]["code"], "permission_denied")
        self.assertEqual(requested, ["Q"])

    def test_expiry_between_gdc_pages_stops_grounding_without_partial_content(self):
        now = time()
        requested = []

        def maps(code, offset):
            nonlocal now
            requested.append((code, offset))
            now += 120
            return [gdc_map(code)], 2

        with (
            authority_scope(
                authority(
                    "ground_value",
                    "get_concept",
                    "find_data_elements_for_concept",
                    "resolve_stored_value",
                )
            ),
            patch("nci_sip_mcp.permissions.time", side_effect=lambda: now),
            patch.object(
                self.evs,
                "get_gdc_mapset",
                create=True,
                return_value={"code": "NCIt_Maps_To_GDC", "version": "26.06e"},
            ),
            patch.object(self.evs, "get_gdc_maps", create=True, side_effect=maps),
        ):
            result = self.call(conceptCode="C1", commons="GDC")
        self.assertEqual(set(result), {"error"})
        self.assertEqual(result["error"]["code"], "permission_denied")
        self.assertEqual(requested, [("C1", 0)])

    def test_reused_producers_deny_before_direct_client_access(self):
        actions = [
            lambda: cadsr_matching.match_entities(self.context, [{"entity": "stage"}], {}, {}, 10),
            lambda: cadsr_matching.match_values(self.context, ["II"], {}, {}),
            lambda: cadsr_content.read_code_maps(self.context, {}, None),
            lambda: seam.get_concept_for_permissible_value(
                self.context, dataElementId="123", value="II"
            ),
            lambda: seam.resolve_stored_value(self.context, "C1", "CRDC"),
        ]
        for action in actions:
            with (
                self.subTest(action=action),
                authority_scope(authority()),
                correlated(),
                cache_call(),
            ):
                with self.assertRaises(PlatformError) as raised:
                    action()
                self.assertEqual(raised.exception.code, "permission_denied")
                self.assertEqual(self.evs.calls, [])
                self.assertEqual(self.ssis.calls, [])

    def test_direct_workflow_cannot_bypass_dependency_checks(self):
        with authority_scope(authority("ground_value")), self.assertRaises(PlatformError) as raised:
            workflows.ground_value(self.context, conceptCode="C1")
        self.assertEqual(raised.exception.code, "permission_denied")
        self.assertEqual(self.evs.calls, [])
        self.assertEqual(self.ssis.calls, [])

    def test_expiry_between_concept_and_graph_reads_refuses_new_child(self):
        now = time()
        read = self.evs.get_concept

        def expire(*args, **kwargs):
            nonlocal now
            result = read(*args, **kwargs)
            now += 120
            return result

        with (
            authority_scope(
                authority("ground_value", "get_concept", "find_data_elements_for_concept")
            ),
            patch("nci_sip_mcp.permissions.time", side_effect=lambda: now),
            patch.object(self.evs, "get_concept", side_effect=expire),
        ):
            result = self.call(conceptCode="C1")
        self.assertEqual(result["error"]["code"], "permission_denied")
        self.assertEqual(self.ssis.calls, [])

    def test_grounding_preflights_each_child_before_any_read(self):
        capabilities = {
            "ground_value",
            "get_concept",
            "find_data_elements_for_concept",
            "search_concepts",
            "resolve_stored_value",
            "get_code_map",
        }
        for missing in capabilities - {"ground_value"}:
            with (
                self.subTest(missing=missing),
                authority_scope(authority(*(capabilities - {missing}))),
            ):
                result = self.call(text="Neoplasm", commons="CRDC")
                self.assertEqual(result["error"]["code"], "permission_denied")
                self.assertEqual(self.evs.calls, [])
                self.assertEqual(self.ssis.calls, [])

    def test_code_grounding_needs_no_text_or_stored_value_permissions(self):
        with authority_scope(
            authority("ground_value", "get_concept", "find_data_elements_for_concept")
        ):
            result = self.call(conceptCode="C1", release="26.06e")
        self.assertEqual(result["concept"]["code"], "C1")
        self.assertNotIn("storedValues", result)

    def test_cohort_preflights_both_graph_permissions(self):
        for permitted in ("get_concept_hierarchy", "get_concept_neighborhood"):
            with (
                self.subTest(permitted=permitted),
                authority_scope(authority("expand_cohort", permitted)),
            ):
                result = self.call("expand_cohort", conceptCode="C1")
                self.assertEqual(result["error"]["code"], "permission_denied")
                self.assertEqual(self.evs.calls, [])

    def test_dictionary_samples_require_value_permission_before_matching(self):
        with authority_scope(authority("harmonize_data_dictionary", "match_data_elements")):
            result = self.call(
                "harmonize_data_dictionary", columns=[{"name": "stage", "sampleValues": ["II"]}]
            )
        self.assertEqual(result["error"]["code"], "permission_denied")


class ProtectedHTTPTest(ServerFixture):
    def test_stateless_requests_do_not_bind_unrelated_callers_to_one_principal(self):
        current = authority("get_concept")

        async def resolve():
            return current

        async def scenario():
            nonlocal current
            settings = replace(self.settings, http_sessions="stateless")
            async with http_app(settings, self.context, authority_resolver=resolve) as client:
                headers = await initialize_http(client)
                self.assertNotIn("Mcp-Session-Id", headers)
                first = http_result(await concept_response(client, headers))
                self.assertEqual(first["structuredContent"]["code"], "C3262")
                current = replace(current, principal=replace(current.principal, subject="other"))
                second = http_result(await concept_response(client, headers))
                self.assertEqual(second["structuredContent"]["code"], "C3262")
                current = replace(current, capabilities=frozenset())
                denied = http_result(await concept_response(client, headers))
                self.assertEqual(denied["structuredContent"]["error"]["code"], "permission_denied")

        asyncio.run(scenario())

    def test_session_cannot_reuse_another_issuer_or_tenants_release_pin(self):
        original = authority("get_concept")
        current = original

        async def resolve():
            return current

        async def scenario():
            nonlocal current
            async with http_app(self.settings, self.context, authority_resolver=resolve) as client:
                headers = await initialize_http(client)
                first = http_result(await concept_response(client, headers))
                self.assertEqual(first["structuredContent"]["code"], "C3262")
                before = list(self.evs.calls)
                for field in ("issuer", "subject", "tenant", "client"):
                    current = replace(
                        original, principal=replace(original.principal, **{field: "other"})
                    )
                    denied = http_result(await concept_response(client, headers))
                    self.assertEqual(
                        denied["structuredContent"]["error"]["code"], "permission_denied"
                    )
                    self.assertEqual(self.evs.calls, before)
                current = original
                retained = http_result(await concept_response(client, headers))
                self.assertEqual(
                    retained["structuredContent"]["provenance"]["release"],
                    first["structuredContent"]["provenance"]["release"],
                )

        asyncio.run(scenario())

    def test_http_content_and_refusals_disable_intermediary_caching(self):
        current = authority("get_concept")

        async def resolve():
            return current

        async def scenario():
            nonlocal current
            async with http_app(self.settings, self.context, authority_resolver=resolve) as client:
                headers = await initialize_http(client)
                allowed = await concept_response(client, headers, release="26.06e")
                self.assertEqual(http_result(allowed)["structuredContent"]["code"], "C3262")
                self.assertEqual(allowed.headers["cache-control"], "no-store")
                catalogue = await client.post(
                    "/mcp",
                    headers=headers,
                    json={"jsonrpc": "2.0", "id": 3, "method": "tools/list"},
                )
                self.assertEqual(catalogue.headers["cache-control"], "no-store")
                self.assertEqual(
                    [row["name"] for row in http_result(catalogue)["tools"]], ["get_concept"]
                )
                # Legacy clients may ignore the extra MCP hints; HTTP still forbids storage.
                self.assertEqual(http_result(catalogue)["cacheScope"], "private")
                current = authority()
                denied = await concept_response(client, headers, release="26.06e")
                self.assertEqual(
                    http_result(denied)["structuredContent"]["error"]["code"], "permission_denied"
                )
                self.assertEqual(denied.headers["cache-control"], "no-store")

        asyncio.run(scenario())


class CursorPermissionTest(ServerFixture):
    def test_a_replayed_or_tampered_cursor_cannot_grant_indexed_content(self):
        self.local_index.upsert_concepts(
            normalized(
                [concept("C1", "Kinase", active=True), concept("C2", "Kinase", active=True)]
            ),
            None,
            self.context.embedding_provider,
        )
        arguments = pinned(query="Kinase", mode="semantic", limit=1)
        with authority_scope(authority("search_concepts")):
            first = invoke(self.context, "search_concepts", **arguments)
        self.assertEqual(len(first["results"]), 1)
        with authority_scope(authority("get_concept")):
            for cursor in (first["nextCursor"], "tampered"):
                denied = invoke(self.context, "search_concepts", **arguments, cursor=cursor)
                self.assertEqual(set(denied), {"error"})
                self.assertEqual(denied["error"]["code"], "permission_denied")
        with authority_scope(authority("search_concepts", policy_version="renewed")):
            second = invoke(
                self.context, "search_concepts", **arguments, cursor=first["nextCursor"]
            )
        self.assertNotEqual(
            first["results"][0]["concept"]["code"], second["results"][0]["concept"]["code"]
        )
