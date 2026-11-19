import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from jsonschema import Draft202012Validator

from nci_sip_mcp.context import Context
from nci_sip_mcp.http_client import UpstreamTooLargeError, UpstreamUnavailableError
from nci_sip_mcp.registry import invoke
from test_evs_client import FakeResponse, http_error
from test_server import ServerFixture

FIXTURE = Path(__file__).parents[1] / "acceptance/fixtures/recorded/evs-fhir/expand-c85492.json"
CANONICAL = "http://ncicb.nci.nih.gov/xml/owl/EVS/Thesaurus.owl?fhir_vs=C85492"


class ValueSetTest(ServerFixture):
    def setUp(self):
        super().setUp()
        self.raw = json.loads(FIXTURE.read_text())["response"]["body"]

    def expand(self, **arguments):
        return invoke(
            self.context,
            "expand_value_set",
            **({"terminology": "ncit", "release": "26.09d", "valueSet": "C85492"} | arguments),
        )

    def reply(self, **arguments):
        with patch(
            "nci_sip_mcp.http_client._open",
            return_value=FakeResponse(json.dumps(self.raw).encode()),
        ) as opened:
            result = self.expand(**arguments)
        return result, opened

    def test_recorded_version_is_verified_unchanged_and_pages_keep_platform_order(self):
        self.assertEqual(self.raw["version"], "26.09d")
        self.assertEqual(self.raw["expansion"]["total"], 534)
        result, opened = self.reply(count=10, offset=100)
        expected = self.raw["expansion"]["contains"][100:110]
        self.assertEqual(
            [(r["code"], r["name"]) for r in result["members"]],
            [(r["code"], r["display"]) for r in expected],
        )
        self.assertEqual(result["total"], 534)
        self.assertEqual(result["truncation"], {"occurred": False})
        provenance = result["members"][0]["provenance"]
        self.assertNotIn("provenance", result)
        self.assertEqual(provenance["upstream"], {"url": CANONICAL, "version": "26.09d"})
        self.assertEqual(provenance["source"], "evs_fhir")
        self.assertEqual(provenance["release"], {"terminology": "ncit", "identifier": "26.09d"})
        self.assertEqual(opened.call_count, 1)
        self.assertNotIn("system-version", opened.call_args.args[0].full_url)
        self.assertNotIn("count=", opened.call_args.args[0].full_url)

    def test_filtering_precedes_paging_and_false_inactive_is_omitted(self):
        contains = self.raw["expansion"]["contains"]
        contains[0]["inactive"] = True
        contains[1]["inactive"] = False
        result, _ = self.reply(activeOnly=True, count=2, offset=1)
        self.assertEqual([m["code"] for m in result["members"]], [m["code"] for m in contains[2:4]])
        self.assertEqual(result["total"], 533)
        result, _ = self.reply(count=2)
        self.assertTrue(result["members"][0]["inactive"])
        self.assertNotIn("inactive", result["members"][1])

    def test_clamped_page_can_continue_without_truncation(self):
        base = self.raw["expansion"]["contains"][0]
        self.raw["expansion"].update(
            total=1200, contains=[base | {"code": f"C{index + 1}"} for index in range(1200)]
        )
        result, _ = self.reply(count=5000)
        self.assertEqual((len(result["members"]), result["total"]), (1000, 1200))
        self.assertEqual(result["truncation"], {"occurred": False})
        next_page, _ = self.reply(count=5000, offset=1000)
        self.assertEqual(
            [r["code"] for r in next_page["members"]],
            [f"C{index + 1}" for index in range(1000, 1200)],
        )

    def test_empty_or_past_end_page_keeps_total_and_provenance(self):
        for offset in (534, 999):
            with self.subTest(offset=offset):
                result, _ = self.reply(offset=offset)
                self.assertEqual((result["members"], result["total"]), ([], 534))
                self.assertEqual(result["provenance"]["upstream"]["url"], CANONICAL)
        self.raw["expansion"] = {"total": 0}
        result, _ = self.reply()
        self.assertEqual((result["members"], result["total"]), ([], 0))

    def test_both_aliases_work_but_both_or_neither_is_invalid(self):
        result, _ = self.reply(valueSet=None, code="C85492")
        self.assertEqual(len(result["members"]), 200)
        for arguments in ({"valueSet": None}, {"code": "C85492"}):
            with (
                self.subTest(arguments=arguments),
                patch("nci_sip_mcp.http_client._open") as opened,
            ):
                result = self.expand(**arguments)
                self.assertEqual(result["error"]["code"], "invalid_request")
                self.assertEqual(opened.call_count, 0)

    def test_invalid_arguments_fail_without_network(self):
        cases = [
            {"count": 0},
            {"count": -1},
            {"count": True},
            {"offset": -1},
            {"offset": True},
            {"activeOnly": "yes"},
            {"valueSet": "C0"},
            {"valueSet": None, "code": "C1\n"},
            {"release": "bad?"},
        ]
        for arguments in cases:
            with (
                self.subTest(arguments=arguments),
                patch("nci_sip_mcp.http_client._open") as opened,
            ):
                result = self.expand(**arguments)
                self.assertEqual(result["error"]["code"], "invalid_request")
                self.assertEqual(opened.call_count, 0)

    def test_mismatch_is_exact_with_requested_and_served_and_no_members(self):
        for version in ("26.09D", " 26.09d", "26.08e"):
            with self.subTest(version=version):
                self.raw["version"] = version
                result, opened = self.reply()
                self.assertEqual(result["error"]["code"], "release_mismatch")
                self.assertEqual(
                    result["error"]["details"],
                    {"requested": "26.09d", "served": [version], "source": "evs"},
                )
                self.assertNotIn("members", result)
                self.assertEqual(opened.call_count, 1)

    def test_wrong_value_set_or_terminology_fails_before_members(self):
        original = self.raw
        for fields in ({"url": "other"}, {"title": "other"}, {"resourceType": "Parameters"}):
            with self.subTest(fields=fields):
                self.raw = original | fields
                result, _ = self.reply()
                self.assertEqual(result["error"]["code"], "upstream_unavailable")

    def test_incomplete_nested_or_malformed_expansion_cannot_look_complete(self):
        member = self.raw["expansion"]["contains"][0]
        cases = [
            None,
            {"contains": None},
            {"total": 2, "contains": [member]},
            {"offset": 1, "contains": [member]},
            {"total": True, "contains": [member]},
            {"contains": [member | {"contains": [member]}]},
            {"contains": [None]},
        ]
        for expansion in cases:
            with self.subTest(expansion=expansion):
                self.raw["expansion"] = expansion
                result, _ = self.reply()
                self.assertEqual(result["error"]["code"], "upstream_unavailable")
                self.assertNotIn("members", result)

    def test_bad_member_cannot_be_hidden_by_paging_or_active_filter(self):
        base = self.raw["expansion"]["contains"][0]
        for fields in ({"code": None}, {"display": ""}, {"inactive": None}, {"system": "other"}):
            with self.subTest(fields=fields):
                self.raw["expansion"] = {"contains": [base, base | fields]}
                result, _ = self.reply(count=1, activeOnly=True)
                self.assertEqual(result["error"]["code"], "upstream_unavailable")

    def test_operation_outcome_and_http_failure_are_errors(self):
        self.raw = {"resourceType": "OperationOutcome", "issue": [{"severity": "error"}]}
        result, _ = self.reply()
        self.assertEqual(result["error"]["code"], "upstream_unavailable")
        with patch("nci_sip_mcp.http_client._open", side_effect=http_error(404, b"{}")):
            result = self.expand()
        self.assertEqual(result["error"]["code"], "upstream_unavailable")

    def test_protocol_success_and_mismatch_have_distinct_cache_hints(self):
        tool = next(
            t for t in self.session(lambda c: c.list_tools()).tools if t.name == "expand_value_set"
        )
        validator = Draft202012Validator(tool.output_schema)
        arguments = {"terminology": "ncit", "release": "26.09d", "code": "C85492"}
        with patch(
            "nci_sip_mcp.http_client._open",
            side_effect=[
                FakeResponse(json.dumps(self.raw).encode()),
                FakeResponse(json.dumps(self.raw).encode()),
            ],
        ):
            result = self.session(lambda c: c.call_tool("expand_value_set", arguments))
            mismatch = self.session(
                lambda c: c.call_tool("expand_value_set", arguments | {"release": "old"})
            )
        validator.validate(result.structured_content)
        validator.validate(mismatch.structured_content)
        self.assertFalse(result.is_error)
        self.assertEqual(result.meta["ttlMs"], 86_400_000)
        self.assertTrue(mismatch.is_error)
        self.assertEqual((mismatch.meta["ttlMs"], mismatch.meta["cacheScope"]), (0, "private"))

    def test_byte_cap_is_an_error_naming_its_actual_setting(self):
        self.context.fhir.max_response_bytes = 10
        result, _ = self.reply()
        self.assertEqual(result["error"]["code"], "bound_exceeded")
        self.assertEqual(result["error"]["details"]["bound"], "NCI_SIP_EVS_MAX_RESPONSE_BYTES")
        self.assertIn("NCI_SIP_EVS_MAX_RESPONSE_BYTES", result["error"]["message"])

    def test_configured_fhir_origin_credentials_and_correlation_reach_the_request(self):
        self.context = Context(
            replace(
                self.settings,
                evs_fhir_base_url="https://fhir.invalid/r4",
                evs_license_key="test-only",
                timeout_seconds=7,
            ),
            evs=self.evs,
        )
        self.raw["copyright"] = "Platform copyright text"
        result, opened = self.reply()
        request, timeout = opened.call_args.args
        self.assertTrue(request.full_url.startswith("https://fhir.invalid/r4/ValueSet/$expand?"))
        self.assertEqual(request.get_header("X-evsrestapi-license-key"), "test-only")
        self.assertEqual(
            request.get_header("X-correlation-id"),
            result["members"][0]["provenance"]["correlationId"],
        )
        self.assertEqual(timeout, 7)
        self.assertEqual(
            result["members"][0]["provenance"]["attribution"], "Platform copyright text"
        )
        self.assertNotIn("test-only", json.dumps(result))

    def test_non_object_resource_is_an_upstream_error(self):
        self.raw = []
        result, _ = self.reply()
        self.assertEqual(result["error"]["code"], "upstream_unavailable")

    def test_invalid_version_type_is_not_normalized_into_a_matching_release(self):
        for version in (26, None, ""):
            with self.subTest(version=version):
                self.raw["version"] = version
                result, _ = self.reply(release="26")
                self.assertEqual(result["error"]["code"], "upstream_unavailable")

    def test_non_integer_upstream_offset_is_not_accepted_as_zero(self):
        self.raw["expansion"]["offset"] = False
        result, _ = self.reply()
        self.assertEqual(result["error"]["code"], "upstream_unavailable")


def http_failure():
    raise http_error(503, b"{}")


class FHIRTransportSettingsTest(ServerFixture):
    def fhir_context(self):
        return Context(
            replace(self.settings, evs_max_attempts=2, evs_max_response_bytes=8),
            evs=self.evs,
            index=self.context.index,
            embedding_provider=self.context.embedding_provider,
        )

    def test_fhir_retries_use_configured_attempts_and_surface_label(self):
        context = self.fhir_context()
        with (
            patch("nci_sip_mcp.http_client._open", side_effect=lambda *args: http_failure()),
            patch("nci_sip_mcp.http_client.time.sleep"),
            self.assertRaises(UpstreamUnavailableError) as raised,
        ):
            context.fhir.get_json("/ValueSet/$expand")
        self.assertEqual(raised.exception.details["attempts"], 2)
        self.assertEqual(raised.exception.details["surface"], "evs_fhir")

    def test_fhir_response_limit_uses_configured_bytes(self):
        context = self.fhir_context()
        with (
            patch("nci_sip_mcp.http_client._open", return_value=FakeResponse(b'{"large":"value"}')),
            self.assertRaises(UpstreamTooLargeError) as raised,
        ):
            context.fhir.get_json("/ValueSet/$expand")
        self.assertEqual(raised.exception.details["limit"], 8)
        self.assertEqual(raised.exception.details["reached"], 9)
        self.assertEqual(raised.exception.details["bound"], "NCI_SIP_EVS_MAX_RESPONSE_BYTES")
