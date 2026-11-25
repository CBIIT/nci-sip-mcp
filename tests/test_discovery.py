from dataclasses import replace
from unittest.mock import patch

from jsonschema import Draft202012Validator

from fakes import terminology_row
from nci_sip_mcp import cli
from nci_sip_mcp.evs import EVSClient
from nci_sip_mcp.http_client import UpstreamUnavailableError
from nci_sip_mcp.registry import invoke
from nci_sip_mcp.traffic_identity import product_user_agent
from test_evs_client import FakeResponse, http_error
from test_server import ServerFixture


class DiscoveryTest(ServerFixture):
    def test_resolve_uses_the_channel_and_returns_other_served_versions_once(self):
        self.evs.rows = [
            terminology_row("weekly", monthly="false", weekly="true"),
            terminology_row("monthly"),
            terminology_row("old", latest=False),
            terminology_row("old", latest=False),
            dict(terminology_row("foreign"), terminology="other"),
        ]
        self.context.settings = replace(self.settings, release_channel="weekly")
        default = invoke(self.context, "resolve_release", "ncit", _correlation_id="discovery")
        monthly = invoke(self.context, "resolve_release", "ncit", "monthly")
        self.assertEqual((default["channel"], default["version"]), ("weekly", "weekly"))
        self.assertEqual(default["alternatives"], ["monthly", "old"])
        self.assertEqual(monthly["alternatives"], ["weekly", "old"])
        self.assertEqual(default["provenance"]["release"]["identifier"], "weekly")
        self.assertEqual(default["provenance"]["correlationId"], "discovery")
        self.assertNotIn("pinned_terminology", default)

    def test_discovery_is_fresh_and_has_public_zero_ttl(self):
        for tool, arguments in (
            ("resolve_release", {"terminology": "ncit"}),
            ("list_terminologies", {}),
        ):
            with self.subTest(tool=tool):
                first = self.session(
                    lambda client, tool=tool, arguments=arguments: client.call_tool(tool, arguments)
                )
                self.evs.rows = [terminology_row("next")]
                second = self.session(
                    lambda client, tool=tool, arguments=arguments: client.call_tool(tool, arguments)
                )
                self.assertNotEqual(first.structured_content, second.structured_content)
                self.assertIn("next", str(second.structured_content))
                self.assertEqual((second.meta["ttlMs"], second.meta["cacheScope"]), (0, "public"))
                self.evs.rows = None

    def test_invalid_discovery_inputs_fail_before_any_network_request(self):
        for arguments in (
            {"terminology": "ncit/../x"},
            {"terminology": "ncit\n"},
            {"terminology": "NCIT"},
            {"terminology": "ncit", "channel": ""},
            {"terminology": "ncit", "channel": "daily"},
        ):
            with self.subTest(arguments=arguments):
                result = invoke(self.context, "resolve_release", **arguments)
                self.assertEqual(result["error"]["code"], "invalid_request")
        self.assertEqual(self.evs.calls, [])

    def test_unknown_or_ambiguous_release_is_a_top_level_error(self):
        for rows in ([], [terminology_row("one"), terminology_row("two")]):
            with self.subTest(rows=rows):
                self.evs.rows = rows
                failed, result = self.call("resolve_release", terminology="ncit")
                self.assertTrue(failed)
                self.assertEqual(result["error"]["code"], "release_not_available")
                self.assertNotIn("selected_release", result)

    def test_unusable_alternative_is_not_silently_omitted(self):
        self.evs.rows = [terminology_row(), terminology_row("", latest=False)]
        failed, result = self.call("resolve_release", terminology="ncit")
        self.assertTrue(failed)
        self.assertEqual(result["error"]["code"], "upstream_unavailable")

    def test_listing_uses_monthly_ncit_and_every_other_terminologys_latest(self):
        self.evs.rows = [
            terminology_row("weekly", weekly="true"),
            dict(terminology_row("old", latest=False), terminology="other", tags=None),
            terminology_row("monthly"),
            dict(terminology_row("current"), terminology="other", tags=None, date=None),
        ]
        failed, result = self.call("list_terminologies")
        self.assertFalse(failed)
        items = result["terminologies"]
        self.assertEqual(
            [(r["terminology"], r["release"]) for r in items],
            [("ncit", "monthly"), ("other", "current")],
        )
        for item in items:
            provenance = item["provenance"]
            self.assertEqual(provenance["release"]["identifier"], item["release"])
            self.assertEqual(
                provenance["upstream"],
                {"terminology": item["terminology"], "version": item["release"]},
            )
            self.assertEqual((provenance["source"], provenance["servedBy"]), ("evs_rest", "live"))
        self.assertNotIn("date", items[1]["provenance"]["release"])

    def test_listing_does_not_guess_missing_or_ambiguous_current_rows(self):
        cases = (
            [terminology_row(latest=False)],
            [terminology_row(weekly="true")],
            [dict(terminology_row(), terminology="")],
            [
                dict(terminology_row("one"), terminology="other"),
                dict(terminology_row("two"), terminology="other"),
            ],
        )
        for rows in cases:
            with self.subTest(rows=rows):
                self.evs.rows = rows
                failed, result = self.call("list_terminologies")
                self.assertTrue(failed)
                self.assertEqual(result["error"]["code"], "release_not_available")

    def test_empty_authoritative_listing_reports_actual_status_and_retry_count(self):
        self.context.evs = EVSClient(
            "https://example.invalid", retry_backoff_seconds=0, user_agent=product_user_agent()
        )
        responses = [http_error(503), FakeResponse(b"[]")]
        with patch("nci_sip_mcp.http_client._open", side_effect=responses):
            failed, result = self.call("list_terminologies")
        self.assertTrue(failed)
        self.assertEqual(result["error"]["code"], "upstream_unavailable")
        self.assertEqual(
            result["error"]["details"], {"surface": "evs", "status": 200, "attempts": 2}
        )
        self.assertIn("EVS listed no terminologies", result["error"]["message"])
        self.assertNotIn("provenance", result)

    def test_outages_are_protocol_errors_with_private_cache_hints_and_valid_schema(self):
        self.evs.errors["get_terminologies"] = UpstreamUnavailableError(
            "offline", surface="evs", attempts=3
        )
        tools = {tool.name: tool for tool in self.session(lambda client: client.list_tools()).tools}
        for name, arguments in (
            ("resolve_release", {"terminology": "ncit"}),
            ("list_terminologies", {}),
        ):
            with self.subTest(tool=name):
                result = self.session(
                    lambda client, name=name, arguments=arguments: client.call_tool(name, arguments)
                )
                self.assertTrue(result.is_error)
                self.assertEqual(result.structured_content["error"]["code"], "upstream_unavailable")
                self.assertEqual(
                    result.structured_content["error"]["details"], {"surface": "evs", "attempts": 3}
                )
                self.assertEqual((result.meta["ttlMs"], result.meta["cacheScope"]), (0, "private"))
                Draft202012Validator(tools[name].output_schema).validate(result.structured_content)

    def test_discovery_commands_share_the_tool_inputs_and_results(self):
        parser = cli.build_parser()
        resolved = cli._run(
            self.context, parser.parse_args(["resolve-release", "ncit", "--channel", "monthly"])
        )
        listed = cli._run(self.context, parser.parse_args(["list-terminologies"]))
        self.assertEqual(resolved["version"], "26.06e")
        self.assertEqual(resolved["alternatives"], [])
        self.assertEqual(listed["terminologies"][0]["release"], resolved["version"])
