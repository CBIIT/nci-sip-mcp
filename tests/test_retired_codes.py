import json
from pathlib import Path
from unittest.mock import patch

from jsonschema import Draft202012Validator

from fakes import concept
from nci_sip_mcp.evs import EVSClient
from nci_sip_mcp.registry import invoke
from nci_sip_mcp.traffic_identity import product_user_agent
from test_evs_client import FakeResponse, http_error
from test_server import ServerFixture


class RetiredCodeTest(ServerFixture):
    def mcp_replies(self):
        responses = [FakeResponse(json.dumps(item).encode()) for item in (self.raw, self.history)]
        with patch("nci_sip_mcp.http_client._open", side_effect=responses):
            return self.call(
                "resolve_retired_code", code=self.raw["code"], release=self.raw["version"]
            )

    def test_recorded_merge_preserves_its_producing_event_metadata(self):
        # Given the source-specific recorded merge, when resolving through MCP,
        # then the replacement carries that event's action and date.
        root = Path(__file__).parents[1] / "acceptance/fixtures/recorded/evs"
        self.raw = json.loads((root / "concepts/C15368.json").read_text())["response"]["body"]
        self.history = json.loads((root / "replacement-merge-c15368.json").read_text())["response"][
            "body"
        ]
        failed, result = self.mcp_replies()
        self.assertFalse(failed, result)
        replacement = result["replacements"][0]
        self.assertEqual(replacement.get("action"), "merge")
        self.assertEqual(replacement.get("date"), "2006-02-24")

    def test_optional_event_fields_stay_on_their_own_rows_unchanged(self):
        # Adjacent retirement metadata must not fill a replacement's omitted fields.
        for fields in (
            {},
            {"action": "  Future_Action  "},
            {"date": "unparsed date"},
            {"action": "", "date": " 2006-02-24 "},
        ):
            with self.subTest(fields=fields):
                self.history = [
                    {"code": "C1", "action": "retire", "date": "another event"},
                    {"code": "C1", "replacementCode": "C2", "replacementName": "Second"} | fields,
                ]
                failed, result = self.mcp_replies()
                self.assertFalse(failed, result)
                self.assertEqual(len(result["replacements"]), 1)
                self.assertEqual(
                    {k: v for k, v in result["replacements"][0].items() if k in ("action", "date")},
                    fields,
                )

    def test_malformed_supplied_event_fields_are_protocol_errors_without_partial_content(self):
        for field in ("action", "date"):
            for value in (None, True, 123, [], {}):
                with self.subTest(field=field, value=value):
                    self.history = [
                        {"code": "C1", "replacementCode": "C2", "replacementName": "Second"},
                        {
                            "code": "C1",
                            "replacementCode": "C3",
                            "replacementName": "Third",
                            field: value,
                        },
                    ]
                    failed, result = self.mcp_replies()
                    self.assertTrue(failed, result)
                    self.assertEqual(set(result), {"error"})
                    self.assertEqual(result["error"]["code"], "upstream_unavailable")
                    self.assertTrue(result["error"]["correlationId"])

    def setUp(self):
        super().setUp()
        self.raw = concept(
            "C1", active=False, conceptStatus="Obsolete", licenseText="Upstream licence"
        )
        self.history = [
            {"code": "C1", "action": "retire", "replacementCode": "C2", "replacementName": "Second"}
        ]
        self.context.evs = EVSClient(
            "https://example.invalid", max_attempts=1, user_agent=product_user_agent()
        )

    def resolve(self, **arguments):
        return invoke(
            self.context,
            "resolve_retired_code",
            **({"terminology": "ncit", "release": "26.06e", "code": "C1"} | arguments),
        )

    def replies(self, **arguments):
        responses = [FakeResponse(json.dumps(item).encode()) for item in (self.raw, self.history)]
        with patch("nci_sip_mcp.http_client._open", side_effect=responses) as opened:
            result = self.resolve(**arguments)
        return result, opened

    def test_retired_code_retains_status_and_ordered_replacements_with_distinct_provenance(self):
        self.history.append(
            {
                "code": "C1",
                "replacementCode": "C3",
                "replacementName": "Third",
                "licenseText": "History licence",
            }
        )
        result, opened = self.replies()
        self.assertEqual(
            {key: result[key] for key in ("code", "terminology", "active", "status")},
            {"code": "C1", "terminology": "ncit", "active": False, "status": "Obsolete"},
        )
        self.assertEqual(
            [(row["code"], row["name"]) for row in result["replacements"]],
            [("C2", "Second"), ("C3", "Third")],
        )
        self.assertEqual(result["provenance"]["attribution"], "Upstream licence")
        first, second = result["replacements"]
        self.assertEqual(first["terminology"], "ncit")
        self.assertNotIn("upstream", first["provenance"])
        self.assertNotIn("attribution", first["provenance"])
        self.assertEqual(second["provenance"]["attribution"], "History licence")
        self.assertEqual(
            first["provenance"]["release"], {"terminology": "ncit", "identifier": "26.06e"}
        )
        self.assertEqual(
            opened.call_args.args[0].full_url,
            "https://example.invalid/api/v1/history/ncit_26.06e/C1/replacements",
        )
        self.assertEqual(first["provenance"]["sourceUri"], opened.call_args.args[0].full_url)

    def test_active_boolean_alone_controls_history_read_and_missing_status_is_omitted(self):
        self.raw.update(active=True, conceptStatus="Retired_Concept")
        result, opened = self.replies()
        self.assertEqual(
            (result["active"], result["status"], result["replacements"]),
            (True, "Retired_Concept", []),
        )
        self.assertEqual(opened.call_count, 1)
        del self.raw["conceptStatus"]
        result, _ = self.replies()
        self.assertNotIn("status", result)
        self.assertEqual(result["replacements"], [])

    def test_empty_history_or_plain_retire_row_means_no_replacement(self):
        self.raw["conceptStatus"] = "Active sounding"
        for history in ([], [{"code": "C1", "action": "retire"}]):
            with self.subTest(history=history):
                self.history = history
                result, opened = self.replies()
                self.assertEqual(
                    (result["active"], result["status"], result["replacements"]),
                    (False, "Active sounding", []),
                )
                self.assertEqual(opened.call_count, 2)

    def test_history_404_is_an_error_not_an_empty_replacement_list(self):
        responses = [
            FakeResponse(json.dumps(self.raw).encode()),
            http_error(404, b'{"message":"No history"}'),
        ]
        with patch("nci_sip_mcp.http_client._open", side_effect=responses):
            result = self.resolve()
        self.assertEqual(result["error"]["code"], "upstream_unavailable")
        self.assertNotIn("replacements", result)

    def test_invalid_history_cannot_be_a_partial_success(self):
        for history in (
            {},
            [None],
            [{"code": "C9"}],
            [{"code": "C1", "replacementCode": "C2"}],
            [{"code": "C1", "replacementCode": "", "replacementName": "Blank"}],
        ):
            with self.subTest(history=history):
                self.history = history
                result, _ = self.replies()
                self.assertEqual(result["error"]["code"], "upstream_unavailable")
                self.assertNotIn("replacements", result)

    def test_optional_history_identity_is_checked_when_present(self):
        for fields, error in (
            ({"version": "other"}, "release_mismatch"),
            ({"terminology": "other"}, "upstream_unavailable"),
        ):
            with self.subTest(fields=fields):
                self.history = [{"code": "C1"} | fields]
                result, _ = self.replies()
                self.assertEqual(result["error"]["code"], error)

    def test_supplied_history_identity_is_preserved_without_changing_replacement_name(self):
        self.history[0].update(
            terminology="ncit", version="26.06e", replacementName="  Upstream name  "
        )
        result, _ = self.replies()
        replacement = result["replacements"][0]
        self.assertEqual(replacement["name"], "  Upstream name  ")
        self.assertEqual(
            replacement["provenance"]["upstream"], {"terminology": "ncit", "version": "26.06e"}
        )

    def test_missing_concept_and_withdrawn_release_keep_distinct_errors(self):
        for body, expected in (
            (b'{"message":"Code not found = C1"}', "not_found"),
            (b'{"message":"Terminology not found = ncit_26.06e"}', "release_not_available"),
        ):
            with (
                self.subTest(expected=expected),
                patch("nci_sip_mcp.http_client._open", side_effect=http_error(404, body)),
            ):
                result = self.resolve()
                self.assertEqual(result["error"]["code"], expected)
                self.assertNotIn("replacements", result)

    def test_history_outage_cannot_return_a_partial_result(self):
        replies = [
            FakeResponse(json.dumps(self.raw).encode()),
            http_error(503, b'{"message":"Unavailable"}'),
        ]
        with patch("nci_sip_mcp.http_client._open", side_effect=replies):
            result = self.resolve()
        self.assertEqual(result["error"]["code"], "upstream_unavailable")
        self.assertNotIn("active", result)

    def test_invalid_identifier_is_rejected_without_network(self):
        with patch("nci_sip_mcp.http_client._open") as opened:
            result = self.resolve(code="C0")
        self.assertEqual(result["error"]["code"], "invalid_request")
        self.assertEqual(result["error"]["details"]["parameter"], "code")
        self.assertEqual(opened.call_count, 0)

    def test_wrong_concept_identity_or_missing_boolean_fails_before_history(self):
        original = self.raw
        for fields in ({"code": "C9"}, {"active": None}, {"version": "other"}):
            with self.subTest(fields=fields):
                self.raw = original | fields
                result, opened = self.replies()
                self.assertIn(result["error"]["code"], ("upstream_unavailable", "release_mismatch"))
                self.assertEqual(opened.call_count, 1)

    def test_other_terminology_codes_are_encoded_and_not_normalized(self):
        self.raw.update(code="A/B?", terminology="other")
        self.history = [
            {"code": "A/B?", "replacementCode": " X/Y ", "replacementName": "As supplied"}
        ]
        result, opened = self.replies(terminology="other", code="A/B?")
        self.assertEqual(
            opened.call_args.args[0].full_url,
            "https://example.invalid/api/v1/history/other_26.06e/A%2FB%3F/replacements",
        )
        self.assertEqual(result["replacements"][0]["code"], " X/Y ")
        self.assertEqual(result["replacements"][0]["terminology"], "other")

    def test_mcp_schema_metadata_and_cache_class_cover_success_and_error(self):
        self.context.evs = self.evs
        tool = next(
            tool
            for tool in self.session(lambda client: client.list_tools()).tools
            if tool.name == "resolve_retired_code"
        )
        validator = Draft202012Validator(tool.output_schema)
        result = self.session(
            lambda client: client.call_tool(
                "resolve_retired_code",
                {"terminology": "ncit", "release": "26.06e", "code": "C3262"},
            )
        )
        self.assertFalse(result.is_error)
        validator.validate(result.structured_content)
        self.assertEqual((result.meta["ttlMs"], result.meta["cacheScope"]), (86_400_000, "public"))
        failed = self.session(
            lambda client: client.call_tool("resolve_retired_code", {"code": "C3262"})
        )
        self.assertTrue(failed.is_error)
        validator.validate(failed.structured_content)
        self.assertEqual(failed.structured_content["error"]["code"], "invalid_request")


class RetiredIdentityTest(ServerFixture):
    def setUp(self):
        super().setUp()
        self.evs.concepts = {
            "C1": concept("C1", "One", active=True, children=[{"code": "C2"}]),
            "C2": concept("C2", "Two", active=True),
        }

    def call(self, tool, **arguments):
        return invoke(
            self.context,
            tool,
            **({"terminology": "ncit", "release": "26.06e"} | arguments),
        )

    def test_null_or_nontext_replacement_code_is_not_silently_dropped(self):
        self.evs.concepts["C1"].update(active=False)
        for code in (None, 123):
            with (
                self.subTest(code=code),
                patch.object(
                    self.evs,
                    "get_replacements",
                    return_value=[{"replacementCode": code, "replacementName": "Two"}],
                    create=True,
                ),
            ):
                result = self.call("resolve_retired_code", code="C1")
                self.assertEqual(result["error"]["code"], "upstream_unavailable")
                self.assertNotIn("replacements", result)

    def test_retired_status_keeps_inactive_and_reads_replacements_without_name(self):
        self.evs.concepts["C1"].update(active=False, conceptStatus="Retired_Concept")
        with patch.object(
            self.evs,
            "get_replacements",
            return_value=[{"replacementCode": "C2", "replacementName": "Two"}],
            create=True,
        ):
            result = self.call("resolve_retired_code", code="C1")
        self.assertFalse(result["active"])
        self.assertEqual(result["status"], "Retired_Concept")
        self.assertEqual(result["replacements"][0]["code"], "C2")
        self.assertNotIn("name", result)
