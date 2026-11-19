import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from mcp.shared.exceptions import MCPError

from fakes import terminology_row
from nci_sip_mcp.caching import cache_call, select_cache_hint
from nci_sip_mcp.context import Context
from nci_sip_mcp.http_client import UpstreamUnavailableError
from nci_sip_mcp.registry import OPERATIONS, ToolSpec, invoke
from nci_sip_mcp.server import _cache_results
from test_server import ServerFixture, pinned


@patch("nci_sip_mcp.server.configure_logging")
class CachingTest(ServerFixture):
    def test_tool_policy_follows_its_declaration_after_a_rename(self, _):
        def status(context: Context):
            return {"state": "pending"}

        spec = ToolSpec(status, "evs", dict[str, str], True, name="renamed_status")
        with (
            patch("nci_sip_mcp.server.SPECS", (spec,)),
            patch.dict(OPERATIONS, {spec.operation: spec}),
        ):
            result = self.session(lambda client: client.call_tool("renamed_status"))
        self.assertFalse(result.is_error)
        self.assertEqual((result.meta["ttlMs"], result.meta["cacheScope"]), (0, "public"))

    def test_resource_policy_ignores_its_uri_and_payload_shape(self, _):
        def content(context: Context, code: str):
            return {"code": code, "active_index": None}

        spec = ToolSpec(content, "evs", dict, False, uri="nci-si://renamed/{code}")
        with (
            patch("nci_sip_mcp.server.SPECS", (spec,)),
            patch.dict(OPERATIONS, {spec.operation: spec}),
        ):
            result = self.session(lambda client: client.read_resource("nci-si://renamed/C1"))
        self.assertEqual((result.ttl_ms, result.cache_scope), (86_400_000, "public"))

    def test_concurrent_status_and_content_calls_keep_separate_hints(self, _):
        async def calls(client):
            return await asyncio.gather(
                client.call_tool("get_concept", pinned(code="C3262")),
                client.call_tool("resolve_release", {"terminology": "ncit"}),
                client.read_resource("ncit://release/26.06e"),
                client.read_resource("ncit://concept/26.06e/C3262"),
            )

        content, status, pinned_result, concept = self.session(calls)
        self.assertEqual([content.meta["ttlMs"], status.meta["ttlMs"]], [86_400_000, 0])
        self.assertEqual([pinned_result.ttl_ms, concept.ttl_ms], [86_400_000, 86_400_000])

    def test_lists_and_discovery_advertise_long_public_result_fields(self, _):
        async def listed(client):
            return [
                await client.list_tools(),
                await client.list_prompts(),
                await client.list_resources(),
                await client.list_resource_templates(),
                client.session.discover_result,
            ]

        for result in self.session(listed):
            with self.subTest(method=type(result).__name__):
                wire = result.model_dump(by_alias=True)
                self.assertEqual((wire["ttlMs"], wire["cacheScope"]), (86_400_000, "public"))
                self.assertNotIn("ttlMs", wire.get("_meta") or {})
                self.assertNotIn("cacheScope", wire.get("_meta") or {})

    def test_governed_tool_content_has_long_public_protocol_metadata(self, _):
        invoke(self.context, "index_codes", ["C3262"])
        calls = [
            ("get_concept", pinned(code="C3262")),
            ("search_concepts", pinned(query="Neoplasm", mode="semantic")),
            ("get_concept_neighborhood", pinned(code="C3262", depth=1)),
        ]
        for name, arguments in calls:
            with self.subTest(tool=name):
                result = self.session(
                    lambda client, name=name, arguments=arguments: client.call_tool(name, arguments)
                )
                self.assertFalse(result.is_error)
                self.assertEqual(
                    (result.meta["ttlMs"], result.meta["cacheScope"]), (86_400_000, "public")
                )
                self.assertNotIn("ttlMs", result.model_dump(by_alias=True))
                self.assertNotIn("_meta", json.loads(result.content[0].text))
                self.assertEqual(
                    result.meta["io.modelcontextprotocol/serverInfo"]["name"], "nci-sip-mcp"
                )

    def test_empty_retired_search_is_successful_cacheable_content(self, _):
        # Filtering an active-only index gives a genuine empty result, without
        # replacing the ranker. The MCP response owns both success and cache policy.
        invoke(self.context, "index_codes", ["C3262"])
        self.evs.rows = [
            terminology_row()
            | {
                "metadata": {
                    "retiredStatusValue": "Retired_Concept",
                    "conceptStatuses": ["Retired_Concept"],
                }
            }
        ]
        result = self.session(
            lambda client: client.call_tool(
                "search_concepts", pinned(query="Neoplasm", mode="semantic", retired="only")
            )
        )
        self.assertFalse(result.is_error)
        content = result.structured_content
        self.assertEqual(content["results"], [])
        self.assertEqual(content["totalKnown"], 0)
        self.assertNotIn("nextCursor", content)
        self.assertEqual(content["provenance"]["release"]["identifier"], "26.06e")
        self.assertEqual(content["provenance"]["servedBy"], "index")
        self.assertEqual((result.meta["ttlMs"], result.meta["cacheScope"]), (86_400_000, "public"))

    def test_status_tools_are_public_but_not_cached(self, _):
        for name, arguments in [
            ("resolve_release", {"terminology": "ncit"}),
            ("list_terminologies", {}),
        ]:
            with self.subTest(tool=name):
                result = self.session(
                    lambda client, name=name, arguments=arguments: client.call_tool(name, arguments)
                )
                self.assertFalse(result.is_error)
                self.assertEqual((result.meta["ttlMs"], result.meta["cacheScope"]), (0, "public"))

    def test_failed_release_read_is_a_protocol_error_not_cacheable_content(self, _):
        self.evs.errors = {"get_terminologies": UpstreamUnavailableError("down")}
        with self.assertRaises(MCPError) as raised:
            self.read("ncit://release/26.06e")
        self.assertEqual(json.loads(str(raised.exception))["error"]["code"], "upstream_unavailable")

    def test_tool_errors_are_private_and_preserve_correlation(self, _):
        result = self.session(
            lambda client: client.call_tool(
                "get_concept", pinned(code="C999"), meta={"correlationId": "cache-error"}
            )
        )
        self.assertTrue(result.is_error)
        self.assertEqual((result.meta["ttlMs"], result.meta["cacheScope"]), (0, "private"))
        self.assertEqual(result.structured_content["error"]["correlationId"], "cache-error")

    def test_schema_rejections_are_private_and_not_cached(self, _):
        result = self.session(lambda client: client.call_tool("get_concept", {}))
        self.assertTrue(result.is_error)
        self.assertEqual((result.meta["ttlMs"], result.meta["cacheScope"]), (0, "private"))

    def test_error_privacy_overrides_the_resolvers_public_policy(self, _):
        self.evs.errors = {"get_terminologies": UpstreamUnavailableError("down")}
        result = self.session(
            lambda client: client.call_tool("resolve_release", {"terminology": "ncit"})
        )
        self.assertTrue(result.is_error)
        self.assertEqual(result.structured_content["error"]["code"], "upstream_unavailable")
        self.assertEqual((result.meta["ttlMs"], result.meta["cacheScope"]), (0, "private"))

    def test_pinned_resource_content_has_long_public_result_hints(self, _):
        invoke(self.context, "index_codes", ["C3262"])
        cases = {
            "ncit://concept/26.06e/C3262": 86_400_000,
            "ncit://release/26.06e": 86_400_000,
            "ncit://index/manifest/26.06e": 86_400_000,
        }
        for uri, ttl in cases.items():
            with self.subTest(uri=uri):
                result = self.session(lambda client, uri=uri: client.read_resource(uri))
                wire = result.model_dump(by_alias=True)
                self.assertEqual((wire["ttlMs"], wire["cacheScope"]), (ttl, "public"))
                self.assertNotIn("ttlMs", wire.get("_meta") or {})
                self.assertNotIn("cacheScope", wire.get("_meta") or {})
                self.assertNotIn("ttlMs", json.loads(result.contents[0].text))

    def test_absent_index_is_a_protocol_error_not_cacheable_content(self, _):
        with self.assertRaises(MCPError) as raised:
            self.read("ncit://index/manifest/26.06e")
        self.assertEqual(
            json.loads(str(raised.exception))["error"]["code"], "capability_unavailable"
        )


class CacheDeclarationTest(unittest.TestCase):
    def test_an_undeclared_successful_producer_cannot_default_to_long_lived(self):
        async def undeclared(ctx):
            return {"content": []}

        with self.assertRaisesRegex(RuntimeError, "must declare its cache policy"):
            asyncio.run(_cache_results(SimpleNamespace(method="tools/call"), undeclared))

    def test_nested_call_restores_the_outer_policy_even_after_failure(self):
        with cache_call() as outer:
            select_cache_hint(resolution=False)
            with self.assertRaisesRegex(ValueError, "failed"), cache_call() as inner:
                select_cache_hint(resolution=True)
                raise ValueError("failed")
            select_cache_hint(resolution=True)
        self.assertEqual(outer, {"ttlMs": 0, "cacheScope": "public"})
        self.assertEqual(inner, {"ttlMs": 0, "cacheScope": "public"})
        with self.assertRaises(LookupError):
            select_cache_hint(resolution=False)


class PinnedContentCacheTest(ServerFixture):
    def test_three_pinned_content_tools_select_long_public_cache_hints(self):
        for tool, arguments in (
            ("list_relationships", {}),
            ("get_concepts", {"codes": ["C3262"]}),
            ("get_concept_hierarchy", {"code": "C3262", "direction": "child"}),
        ):
            with self.subTest(tool=tool), cache_call() as hint:
                result = invoke(
                    self.context, tool, terminology="ncit", release="26.06e", **arguments
                )
                self.assertNotIn("error", result)
                self.assertEqual(hint, {"ttlMs": 86_400_000, "cacheScope": "public"})
