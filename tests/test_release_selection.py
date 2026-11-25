import asyncio
import json
import logging
import time
from copy import deepcopy
from dataclasses import replace
from unittest.mock import patch

import httpx2
from mcp.client import Client

from fakes import concept, release, terminology_row
from nci_sip_mcp.bounds import Budget
from nci_sip_mcp.evs import EVSClient, EVSReleaseNotFoundError
from nci_sip_mcp.registry import invoke
from nci_sip_mcp.release_selection import SessionRelease, session_scope
from nci_sip_mcp.server import create_mcp
from nci_sip_mcp.traffic_identity import product_user_agent
from nci_sip_mcp.transport import create_http_app
from test_evs_client import FakeResponse
from test_server import ServerFixture


def version(result):
    return result["provenance"]["release"]["identifier"]


class ReleaseSelectionTest(ServerFixture):
    def setUp(self):
        super().setUp()
        self.evs.concepts = deepcopy(self.evs.concepts)

    def get(self, **arguments):
        return invoke(self.context, "get_concept", terminology="ncit", code="C3262", **arguments)

    def move(self):
        self.evs.release = release("26.07d")
        self.evs.concepts["C3262"]["version"] = "26.07d"

    def test_without_a_session_each_call_resolves_the_current_release(self):
        first = self.get()
        self.move()
        second = self.get(release=None)
        self.assertEqual((version(first), version(second)), ("26.06e", "26.07d"))
        self.assertEqual(sum(c[0] == "get_terminologies" for c in self.evs.calls), 2)

    def test_only_ncit_may_omit_release_and_invalid_arguments_do_not_discover(self):
        for arguments, parameter in (
            ({"terminology": "other", "code": "X"}, "release"),
            ({"terminology": "ncit", "code": "bad"}, "code"),
            ({"terminology": "ncit", "code": "C3262", "release": ""}, "release"),
            ({"terminology": "ncit", "code": "C3262", "release": "../bad"}, "release"),
        ):
            with self.subTest(arguments=arguments):
                result = invoke(self.context, "get_concept", **arguments)
                self.assertEqual(result["error"]["code"], "invalid_request")
                self.assertEqual(result["error"]["details"]["parameter"], parameter)
        self.assertEqual(self.evs.calls, [])

    def test_failed_discovery_can_retry_but_a_content_failure_keeps_the_pin(self):
        state = SessionRelease()
        self.evs.rows = []
        with session_scope(state):
            self.assertEqual(self.get()["error"]["code"], "release_not_available")
            self.evs.rows = [terminology_row()]
            self.evs.concepts["C3262"]["version"] = "wrong"
            self.assertEqual(self.get()["error"]["code"], "release_mismatch")
            self.evs.rows = [terminology_row("26.07d")]
            self.evs.concepts["C3262"]["version"] = "26.06e"
            self.assertEqual(version(self.get()), "26.06e")
        self.assertEqual(sum(c[0] == "get_terminologies" for c in self.evs.calls), 2)

    def test_a_withdrawn_session_pin_fails_without_discovering_another(self):
        with session_scope(SessionRelease()):
            self.assertEqual(version(self.get()), "26.06e")
            self.move()
            with patch.object(
                self.evs, "get_concept", side_effect=EVSReleaseNotFoundError("withdrawn")
            ):
                result = self.get()
        self.assertEqual(result["error"]["code"], "release_not_available")
        self.assertEqual(result["error"]["details"]["requested"], "26.06e")
        self.assertIn("start a new session or name a release", result["error"]["message"].lower())
        self.assertEqual(sum(c[0] == "get_terminologies" for c in self.evs.calls), 1)

    def test_explicit_override_never_seeds_or_changes_the_implicit_pin(self):
        with session_scope(SessionRelease()):
            self.assertEqual(version(self.get(release="26.06e")), "26.06e")
            self.move()
            self.assertEqual(version(self.get()), "26.07d")
            self.evs.concepts["C3262"]["version"] = "26.06e"
            self.assertEqual(version(self.get(release="26.06e")), "26.06e")
            self.evs.concepts["C3262"]["version"] = "26.07d"
            self.assertEqual(version(self.get()), "26.07d")
        self.assertEqual(sum(c[0] == "get_terminologies" for c in self.evs.calls), 1)

    def test_withdrawn_implicit_cursors_keep_the_session_pin_without_rediscovery(self):
        self.evs.concepts = {
            "C1": concept("C1", active=True, children=[{"code": "C2"}, {"code": "C3"}]),
            "C2": concept("C2", active=True),
            "C3": concept("C3", active=True),
        }
        for operation, arguments, method in (
            ("search_concepts", {"query": "Q", "mode": "lexical"}, "search_concepts"),
            (
                "get_concept_hierarchy",
                {"code": "C1", "direction": "child"},
                "get_concepts_by_codes",
            ),
        ):
            self.evs.calls.clear()
            self.evs.release = release()
            with (
                self.subTest(operation=operation),
                session_scope(SessionRelease()),
                patch.object(
                    self.evs,
                    "search_concepts",
                    create=True,
                    return_value=(2, [concept("C1", active=True)]),
                ),
            ):
                args = {"terminology": "ncit", "limit": 1} | arguments
                first = invoke(self.context, operation, **args)
                self.evs.release = release("26.07d")
                with patch.object(
                    self.evs, method, side_effect=EVSReleaseNotFoundError("withdrawn")
                ):
                    result = invoke(self.context, operation, **args, cursor=first["nextCursor"])
                self.assertEqual(result["error"]["code"], "release_not_available")
                self.assertEqual(result["error"]["details"]["requested"], "26.06e")
                self.assertIn(
                    "start a new session or name a release", result["error"]["message"].lower()
                )
                self.assertEqual(sum(c[0] == "get_terminologies" for c in self.evs.calls), 1)

    def test_weekly_configuration_drives_implicit_discovery(self):
        self.context.settings = replace(self.settings, release_channel="weekly")
        self.evs.release = release(channel="weekly")
        self.assertEqual(version(self.get()), "26.06e")
        self.assertIn(("get_terminologies", "ncit", (True, "weekly")), self.evs.calls)

    def test_completion_audit_names_all_three_selection_sources(self):
        logging.disable(logging.NOTSET)
        with (
            self.assertLogs("nci_sip_mcp.audit", level="INFO") as captured,
            session_scope(SessionRelease()),
        ):
            self.get()
            self.get()
            self.get(release="26.06e")
        records = [r.structured["release"] for r in captured.records]
        self.assertEqual(
            [r["selection"] for r in records],
            ["freshly-resolved", "session-held", "explicit"],
        )
        self.assertEqual([r["selected"]["version"] for r in records], ["26.06e"] * 3)

    def test_real_mcp_keeps_the_pin_and_separates_sessions_on_one_server(self):
        async def interact():
            server = create_mcp(context=self.context)
            args = {"terminology": "ncit", "code": "C3262"}
            async with Client(server) as first:
                one = await first.call_tool("get_concept", args)
                self.move()
                async with Client(server) as second:
                    two = await second.call_tool("get_concept", args)
                self.evs.concepts["C3262"]["version"] = "26.06e"
                three = await first.call_tool("get_concept", args | {"release": None})
            return [version(r.structured_content) for r in (one, two, three)]

        self.assertEqual(asyncio.run(interact()), ["26.06e", "26.07d", "26.06e"])

    def test_real_mcp_caches_only_the_release_named_in_the_arguments(self):
        async def interact(client):
            args = {"terminology": "ncit", "code": "C3262"}
            implicit = await client.call_tool("get_concept", args)
            explicit = await client.call_tool("get_concept", args | {"release": "26.06e"})
            held = await client.call_tool("get_concept", args | {"release": None})
            return implicit, explicit, held

        results = self.session(interact)
        self.assertEqual([version(r.structured_content) for r in results], ["26.06e"] * 3)
        self.assertEqual(
            [(r.meta["ttlMs"], r.meta["cacheScope"]) for r in results],
            [(0, "private"), (86_400_000, "public"), (0, "private")],
        )

    def test_an_explicit_resource_does_not_seed_the_implicit_session(self):
        async def interact(client):
            addressed = await client.read_resource("ncit://concept/26.06e/C3262")
            self.move()
            implicit = await client.call_tool(
                "get_concept", {"terminology": "ncit", "code": "C3262"}
            )
            return json.loads(addressed.contents[0].text), implicit.structured_content

        addressed, implicit = self.session(interact)
        self.assertEqual((version(addressed), version(implicit)), ("26.06e", "26.07d"))

    def test_simultaneous_first_calls_discover_once_and_share_the_release(self):
        discover = self.evs.get_terminologies

        def slow_discovery(*args, **kwargs):
            # Real metadata I/O releases the GIL, letting the other first calls arrive.
            time.sleep(0.05)
            return discover(*args, **kwargs)

        async def interact(client):
            args = {"terminology": "ncit", "code": "C3262"}
            return await asyncio.gather(*(client.call_tool("get_concept", args) for _ in range(8)))

        with patch.object(self.evs, "get_terminologies", side_effect=slow_discovery):
            results = self.session(interact)
        self.assertEqual([version(r.structured_content) for r in results], ["26.06e"] * 8)
        self.assertEqual(sum(c[0] == "get_terminologies" for c in self.evs.calls), 1)

    def test_empty_batch_still_names_the_resolved_release(self):
        result = invoke(self.context, "get_concepts", terminology="ncit", codes=[])
        self.assertEqual(result["concepts"], [])
        self.assertEqual(version(result), "26.06e")
        self.assertEqual([c[0] for c in self.evs.calls], ["get_terminologies"])

    def test_nested_content_reads_reuse_the_one_call_resolution(self):
        result = invoke(self.context, "resolve_retired_code", terminology="ncit", code="C3262")
        self.assertTrue(result["active"])
        self.assertEqual(result["replacements"], [])
        self.assertEqual(version(result), "26.06e")
        self.assertEqual(sum(c[0] == "get_terminologies" for c in self.evs.calls), 1)

    def test_implicit_discovery_spends_the_same_outbound_budget_as_the_catalogue(self):
        self.context.evs = EVSClient(
            "https://evs.invalid", max_attempts=1, user_agent=product_user_agent()
        )
        with (
            patch("nci_sip_mcp.content.Budget", return_value=Budget(requests=1)),
            patch(
                "nci_sip_mcp.http_client._open",
                side_effect=[
                    FakeResponse(json.dumps([terminology_row()]).encode()),
                    FakeResponse(b"[]"),
                ],
            ) as opened,
        ):
            result = invoke(self.context, "list_relationships", terminology="ncit")
        self.assertEqual(result["error"]["code"], "bound_exceeded")
        self.assertEqual(
            result["error"]["details"], {"bound": "requests", "limit": 1, "reached": 1}
        )
        self.assertEqual(opened.call_count, 1)

    def test_conflicting_discovery_metadata_never_reaches_content(self):
        for changes in (
            {"terminology": "other"},
            {"latest": False},
            {"tags": {}},
            {"version": 7},
            {"version": "../bad"},
            {"terminologyVersion": "other_v1"},
            {"terminologyVersion": ""},
        ):
            with (
                self.subTest(changes=changes),
                patch.object(
                    self.evs, "get_terminologies", return_value=[terminology_row() | changes]
                ),
            ):
                self.assertEqual(self.get()["error"]["code"], "release_not_available")
        self.assertEqual(self.evs.calls, [])

    def test_cursor_continuation_binds_effective_release_not_omission(self):
        args = {"terminology": "ncit", "query": "Neoplasm", "limit": 1, "mode": "hybrid"}
        self.evs.concepts["C4741"]["name"] = "Neoplasm two"
        invoke(self.context, "index_codes", ["C3262", "C4741"])
        for first_pin, next_pin in ((None, "26.06e"), ("26.06e", None)):
            with self.subTest(first=first_pin), session_scope(SessionRelease()):
                first = invoke(self.context, "search_concepts", **args, release=first_pin)
                continued = invoke(
                    self.context,
                    "search_concepts",
                    **args,
                    release=next_pin,
                    cursor=first["nextCursor"],
                )
                self.assertNotIn("error", continued)
                self.assertNotEqual(first["results"], continued["results"])
                changed = invoke(
                    self.context,
                    "search_concepts",
                    **args,
                    release="26.07d",
                    cursor=first["nextCursor"],
                )
                self.assertEqual(changed["error"]["code"], "invalid_request")

    def test_real_stateless_http_resolves_each_call(self):
        async def scenario(client):
            headers = await initialize_http(client)
            first = await http_concept(client, headers)
            self.move()
            return [version(first), version(await http_concept(client, headers))]

        app = create_mcp(context=self.context).streamable_http_app(
            stateless_http=True, json_response=True
        )
        with patch.object(self.evs, "get_concept", side_effect=release_addressed):
            versions = asyncio.run(over_http(app, scenario))
        self.assertEqual(versions, ["26.06e", "26.07d"])

    def test_alternating_stateful_http_sessions_each_keep_their_own_pin(self):
        # Given two sessions pinned to different releases, each later call names its own pin.
        async def scenario(client):
            first = await initialize_http(client)
            versions = [version(await http_concept(client, first))]
            self.move()
            second = await initialize_http(client)
            for headers in (second, first, second):
                versions.append(version(await http_concept(client, headers)))
            return versions

        app = create_mcp(context=self.context).streamable_http_app(
            stateless_http=False, json_response=True
        )
        with patch.object(self.evs, "get_concept", side_effect=release_addressed):
            versions = asyncio.run(over_http(app, scenario))
        self.assertEqual(versions, ["26.06e", "26.07d", "26.06e", "26.07d"])

    def test_modern_http_on_a_stateful_app_resolves_each_call_and_accepts_explicit_release(self):
        async def scenario(client):
            first = await modern_concept(client)
            self.assertNotIn("error", first.json()["result"]["structuredContent"])
            self.move()
            second = await modern_concept(client)
            explicit = await modern_concept(
                client, version(first.json()["result"]["structuredContent"])
            )
            return first, second, explicit, await modern_concept(client)

        app = create_http_app(
            replace(self.settings, http_sessions="stateful"), context=self.context
        )
        with patch.object(self.evs, "get_concept", side_effect=release_addressed):
            replies = asyncio.run(over_http(app, scenario))
        contents = [reply.json()["result"]["structuredContent"] for reply in replies]
        self.assertEqual(
            [version(content) for content in contents],
            ["26.06e", "26.07d", "26.06e", "26.07d"],
        )
        for reply in replies:
            self.assertNotIn("mcp-session-id", reply.headers)


PUBLISHED = {
    "26.06e": concept("C3262", version="26.06e", active=True),
    "26.07d": concept("C3262", version="26.07d", active=True),
}


def release_addressed(_code, release, include=()):
    """EVS answering C3262 at whichever release the request names."""
    return deepcopy(PUBLISHED[release.version])


async def over_http(app, scenario):
    """Run `scenario(client)` against `app` in process, inside its lifespan."""
    async with (
        app.router.lifespan_context(app),
        httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app), base_url="http://127.0.0.1:8000"
        ) as client,
    ):
        return await scenario(client)


async def initialize_http(client):
    headers = {
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": "2025-11-25",
    }
    response = await client.post(
        "/mcp",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "release-test", "version": "1"},
            },
        },
    )
    response.raise_for_status()
    if session_id := response.headers.get("mcp-session-id"):
        headers["Mcp-Session-Id"] = session_id
    initialized = await client.post(
        "/mcp",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
        },
    )
    initialized.raise_for_status()
    return headers


async def http_concept(client, headers):
    response = await client.post(
        "/mcp",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "get_concept",
                "arguments": {"terminology": "ncit", "code": "C3262"},
            },
        },
    )
    response.raise_for_status()
    return response.json()["result"]["structuredContent"]


async def modern_concept(client, release=None):
    response = await client.post(
        "/mcp",
        headers={
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": "2026-07-28",
            "Mcp-Method": "tools/call",
            "Mcp-Name": "get_concept",
        },
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "get_concept",
                "arguments": {"terminology": "ncit", "code": "C3262", "release": release},
                "_meta": {
                    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                    "io.modelcontextprotocol/clientInfo": {"name": "release-test", "version": "1"},
                    "io.modelcontextprotocol/clientCapabilities": {},
                },
            },
        },
    )
    response.raise_for_status()
    return response


class ReleaseSelectionBoundaryTest(ServerFixture):
    def test_expired_search_cursor_discovers_the_configured_weekly_release(self):
        self.context.settings = replace(self.settings, release_channel="weekly")
        arguments = {
            "terminology": "ncit",
            "query": "Q",
            "mode": "lexical",
            "release": "26.06e",
            "limit": 1,
        }
        with patch.object(
            self.evs, "search_concepts", create=True, return_value=(2, [concept("C1", active=True)])
        ):
            first = invoke(self.context, "search_concepts", **arguments)
        self.evs.release = release("26.07d", channel="weekly")
        with patch.object(
            self.evs,
            "search_concepts",
            create=True,
            side_effect=EVSReleaseNotFoundError("withdrawn"),
        ):
            result = invoke(
                self.context, "search_concepts", **arguments, cursor=first["nextCursor"]
            )
        self.assertEqual(result["error"]["code"], "cursor_expired")
        self.assertEqual(
            result["error"]["details"], {"cursorRelease": "26.06e", "currentRelease": "26.07d"}
        )
        self.assertIn(("get_terminologies", "ncit", (True, "weekly")), self.evs.calls)

    def test_each_implicit_content_tool_validates_before_discovery(self):
        cases = (
            ("get_concept", {"code": "bad"}),
            ("get_concepts", {"codes": ["bad"]}),
            ("get_concept_subsets", {"code": "bad"}),
            ("get_concept_mappings", {"code": "bad"}),
            ("expand_value_set", {"code": "bad"}),
            ("search_concepts", {"query": "Q", "mode": "bad"}),
            ("get_concept_hierarchy", {"code": "bad", "direction": "child"}),
            ("get_concept_neighborhood", {"code": "C3262", "kinds": ["bad"]}),
            ("resolve_retired_code", {"code": "bad"}),
            ("list_relationships", {"terminology": "../bad"}),
        )
        for operation, arguments in cases:
            with self.subTest(operation=operation):
                self.evs.calls.clear()
                result = invoke(self.context, operation, **({"terminology": "ncit"} | arguments))
                self.assertEqual(result["error"]["code"], "invalid_request")
                self.assertEqual(self.evs.calls, [])

    def test_discovery_spends_the_hierarchy_and_neighborhood_request_budget(self):
        self.context.evs = EVSClient(
            "https://evs.invalid", max_attempts=1, user_agent=product_user_agent()
        )
        for operation, arguments in (
            ("get_concept_hierarchy", {"direction": "child"}),
            ("get_concept_hierarchy", {"direction": "pathsToRoot"}),
            ("get_concept_neighborhood", {"kinds": ["child"]}),
        ):
            with (
                self.subTest(operation=operation, arguments=arguments),
                patch("nci_sip_mcp.content.Budget", return_value=Budget(requests=1)),
                patch(
                    "nci_sip_mcp.http_client._open",
                    side_effect=[
                        FakeResponse(json.dumps([terminology_row()]).encode()),
                        FakeResponse(b"[]"),
                    ],
                ) as opened,
            ):
                result = invoke(self.context, operation, terminology="ncit", code="C1", **arguments)
                self.assertEqual(result["error"]["code"], "bound_exceeded")
                self.assertEqual(
                    result["error"]["details"], {"bound": "requests", "limit": 1, "reached": 1}
                )
                self.assertEqual(opened.call_count, 1)

    def test_retired_lookup_audit_retains_implicit_selection(self):
        logging.disable(logging.NOTSET)
        with self.assertLogs("nci_sip_mcp.audit", level="INFO") as captured:
            result = invoke(self.context, "resolve_retired_code", terminology="ncit", code="C3262")
        self.assertTrue(result["active"])
        self.assertEqual(
            captured.records[-1].structured["release"]["selection"], "freshly-resolved"
        )

    def test_failed_discovery_is_audited_but_a_nonselecting_tool_has_no_selection(self):
        logging.disable(logging.NOTSET)
        self.evs.rows = []
        with self.assertLogs("nci_sip_mcp.audit", level="INFO") as captured:
            failed = invoke(self.context, "get_concept", terminology="ncit", code="C3262")
            listed = invoke(self.context, "list_terminologies")
        self.assertEqual(failed["error"]["code"], "release_not_available")
        self.assertNotIn("error", listed)
        self.assertEqual(
            captured.records[0].structured.get("release", {}).get("selection"), "freshly-resolved"
        )
        self.assertNotIn("selection", captured.records[1].structured.get("release", {}))

    def test_withdrawn_implicit_release_names_its_source(self):
        with session_scope(SessionRelease()):
            invoke(self.context, "get_concept", terminology="ncit", code="C3262")
            with patch.object(
                self.evs, "get_concept", side_effect=EVSReleaseNotFoundError("withdrawn")
            ):
                result = invoke(self.context, "get_concept", terminology="ncit", code="C3262")
        self.assertEqual(result["error"]["details"], {"requested": "26.06e", "source": "evs"})

    def test_explicit_release_retains_weekly_channel_and_accepts_long_identifiers(self):
        self.context.settings = replace(self.settings, release_channel="weekly")
        self.evs.release = release("2026.09-weekly", channel="weekly")
        self.evs.concepts = {"C1": concept("C1", active=True, version="2026.09-weekly")}
        logging.disable(logging.NOTSET)
        with self.assertLogs("nci_sip_mcp.audit", level="INFO") as captured:
            result = invoke(
                self.context, "get_concept", terminology="ncit", code="C1", release="2026.09-weekly"
            )
        self.assertNotIn("error", result)
        self.assertEqual(result["provenance"]["release"]["identifier"], "2026.09-weekly")
        self.assertEqual(
            captured.records[-1].structured["release"]["selected"]["channel"], "weekly"
        )

    def test_expired_explicit_cursor_discovers_the_configured_weekly_release(self):
        self.context.settings = replace(self.settings, release_channel="weekly")
        self.evs.concepts = {
            "C1": concept("C1", active=True, children=[{"code": "C2"}, {"code": "C3"}]),
            "C2": concept("C2", active=True),
            "C3": concept("C3", active=True),
        }
        arguments = {
            "terminology": "ncit",
            "code": "C1",
            "direction": "child",
            "release": "26.06e",
            "limit": 1,
        }
        first = invoke(self.context, "get_concept_hierarchy", **arguments)
        self.evs.release = release("26.07d", channel="weekly")
        with patch.object(
            self.evs, "get_concepts_by_codes", side_effect=EVSReleaseNotFoundError("withdrawn")
        ):
            result = invoke(
                self.context, "get_concept_hierarchy", **arguments, cursor=first["nextCursor"]
            )
        self.assertEqual(result["error"]["code"], "cursor_expired")
        self.assertEqual(
            result["error"]["details"], {"cursorRelease": "26.06e", "currentRelease": "26.07d"}
        )
        self.assertIn(("get_terminologies", "ncit", (True, "weekly")), self.evs.calls)
