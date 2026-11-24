import json
from unittest.mock import patch
from urllib.parse import urlsplit

from jsonschema import Draft202012Validator

from fakes import concept
from nci_sip_mcp.bounds import HARD_MAX_BATCH_CODES, MAX_BATCH_TARGET_BYTES
from nci_sip_mcp.evs import EVSClient
from nci_sip_mcp.registry import invoke
from nci_sip_mcp.traffic_identity import product_user_agent
from test_evs_client import FakeResponse
from test_server import ServerFixture


class BatchContentTest(ServerFixture):
    def setUp(self):
        super().setUp()
        self.evs.concepts = {
            "C1": concept("C1", active=True, conceptStatus="Header_Concept"),
            "C2": concept("C2", active=False, conceptStatus="Retired_Concept"),
        }

    def batch(self, codes, **arguments):
        return invoke(
            self.context,
            "get_concepts",
            **({"terminology": "ncit", "release": "26.06e", "codes": codes} | arguments),
        )

    def test_unordered_reply_is_reconciled_with_duplicates_and_missing_in_input_order(self):
        result = self.batch(["C2", "C9", "C1", "C8", "C2", "C9"])
        self.assertEqual([item["code"] for item in result["concepts"]], ["C2", "C1", "C2"])
        self.assertEqual(result["missing"], ["C9", "C8", "C9"])
        self.assertEqual([item["active"] for item in result["concepts"]], [False, True, False])
        self.assertEqual(result["concepts"][0]["status"], "Retired_Concept")
        self.assertEqual(
            self.evs.calls,
            [("get_concepts_by_codes", "ncit_26.06e", ["C2", "C9", "C1", "C8"])],
        )

    def test_empty_input_is_empty_without_upstream_work(self):
        result = self.batch([])
        self.assertEqual(result["concepts"], [])
        self.assertEqual(result["missing"], [])
        self.assertEqual(result["provenance"]["release"]["identifier"], "26.06e")
        self.assertEqual(result["provenance"]["source"], "evs_rest")
        self.assertEqual(self.evs.calls, [])

    def test_all_missing_is_success_and_preserves_occurrences(self):
        result = self.batch(["C9", "C8", "C9"])
        self.assertEqual(result["concepts"], [])
        self.assertEqual(result["missing"], ["C9", "C8", "C9"])
        self.assertEqual(result["provenance"]["release"]["identifier"], "26.06e")
        self.assertEqual(len(self.evs.calls), 1)

    def test_each_concept_gets_only_selected_sections_and_its_own_provenance(self):
        self.evs.concepts["C1"].update(
            synonyms=[{"name": "One"}],
            definitions=[{"definition": "First"}],
            properties=[{"code": "P106", "value": "Kind"}],
            licenseText="Upstream text",
        )
        result = self.batch(["C1", "C2"], include=["synonyms", "semanticType", "synonyms"])
        self.assertNotIn("provenance", result)
        first, second = result["concepts"]
        self.assertEqual(first["synonyms"], [{"name": "One"}])
        self.assertEqual(first["semanticType"], ["Kind"])
        self.assertEqual(second["synonyms"], [])
        self.assertEqual(second["semanticType"], [])
        self.assertNotIn("properties", first)
        self.assertNotIn("definitions", first)
        self.assertEqual(first["provenance"]["attribution"], "Upstream text")
        self.assertNotIn("attribution", second["provenance"])
        self.assertTrue(second["provenance"]["sourceUri"].endswith("/ncit_26.06e/C2"))
        self.assertEqual(self.evs.includes, ["minimal,synonyms,properties"])

    def test_invalid_codes_and_options_are_rejected_before_any_request(self):
        for arguments in (
            {"codes": ["C1", "c2"]},
            {"include": ["descendants"]},
            {"release": "../old"},
            {"terminology": "NCIT"},
        ):
            with self.subTest(arguments=arguments):
                result = self.batch(**({"codes": ["C1"]} | arguments))
                self.assertEqual(result["error"]["code"], "invalid_request")
        self.assertEqual(self.evs.calls, [])

    def test_count_bound_accepts_exact_maximum_and_counts_duplicates_before_deduplication(self):
        self.assertEqual(HARD_MAX_BATCH_CODES, 650)
        result = self.batch(["C1"] * HARD_MAX_BATCH_CODES)
        self.assertEqual(len(result["concepts"]), HARD_MAX_BATCH_CODES)
        self.assertEqual(self.evs.calls[0][2], ["C1"])
        self.evs.calls.clear()
        rejected = self.batch(["C1"] * (HARD_MAX_BATCH_CODES + 1))
        self.assertEqual(rejected["error"]["code"], "invalid_request")
        self.assertEqual(rejected["error"]["details"]["parameter"], "codes")
        self.assertIn("650", rejected["error"]["message"])
        self.assertEqual(self.evs.calls, [])

    def test_default_projection_has_no_optional_sections(self):
        result = self.batch(["C1"])
        self.assertEqual(
            set(result["concepts"][0]),
            {"code", "terminology", "name", "active", "status", "provenance"},
        )

    def test_mcp_batch_success_and_failure_match_the_advertised_schema(self):
        tools = self.session(lambda client: client.list_tools()).tools
        tool = next(tool for tool in tools if tool.name == "get_concepts")
        validator = Draft202012Validator(tool.output_schema)
        for codes, failed in ((["C2", "C9", "C1"], False), (["bad"], True)):
            with self.subTest(codes=codes):
                is_error, result = self.call("get_concepts", codes=codes)
                self.assertEqual(is_error, failed)
                self.assertEqual(list(validator.iter_errors(result)), [])
        self.assertEqual(result["error"]["details"]["parameter"], "code")


class BatchBoundaryTest(ServerFixture):
    def setUp(self):
        super().setUp()
        self.client = EVSClient(
            "https://example.invalid/proxy",
            max_response_bytes=10000,
            user_agent=product_user_agent(),
        )
        self.context.evs = self.client

    def batch(self, codes, **arguments):
        return invoke(
            self.context,
            "get_concepts",
            **({"terminology": "ncit", "release": "26.06e", "codes": codes} | arguments),
        )

    def test_unusable_identity_never_returns_a_partial_batch(self):
        good = concept("C1", active=True)
        malformed = [
            good | {"code": "C9"},
            good | {"code": None},
            good | {"code": ["C1"]},
            good | {"code": "C2", "active": None},
            good | {"code": "C2", "name": ""},
        ]
        for raw in (*([good, item] for item in malformed), [good, good]):
            with (
                self.subTest(raw=raw),
                patch.object(self.client.http, "get_json", return_value=raw),
            ):
                result = self.batch(["C1", "C2"])
                self.assertEqual(result["error"]["code"], "upstream_unavailable")
                self.assertNotIn("concepts", result)

    def test_all_returned_concepts_are_checked_for_terminology_and_release(self):
        for changes, error in (
            ({"version": "old"}, "release_mismatch"),
            ({"version": None}, "release_mismatch"),
            ({"terminology": "other"}, "upstream_unavailable"),
            ({"terminology": None}, "upstream_unavailable"),
        ):
            raw = [concept("C1", active=True), concept("C2", active=True, **changes)]
            with (
                self.subTest(changes=changes),
                patch.object(self.client.http, "get_json", return_value=raw),
            ):
                result = self.batch(["C1", "C2"])
                self.assertEqual(result["error"]["code"], error)
                self.assertNotIn("concepts", result)

    def test_other_terminology_codes_are_encoded_without_normalization(self):
        code = " A/B?#% "
        raw = concept(code, terminology="other", version="old", active=True)
        with patch(
            "nci_sip_mcp.http_client._open", return_value=FakeResponse(json.dumps([raw]).encode())
        ) as opened:
            result = self.batch([code, code], terminology="other", release="old")
        self.assertEqual([item["code"] for item in result["concepts"]], [code, code])
        self.assertEqual(
            opened.call_args.args[0].full_url,
            "https://example.invalid/proxy/api/v1/concept/other_old?list=+A%2FB%3F%23%25+&include=minimal",
        )
        self.assertEqual(opened.call_count, 1)

    def test_encoded_target_bound_includes_base_path_release_and_selected_details(self):
        self.assertEqual(MAX_BATCH_TARGET_BYTES, 7000)
        params = {"list": "C1", "include": "minimal,synonyms,definitions,properties"}
        url = urlsplit(self.client.uri("/api/v1/concept/ncit_26.06e", params))
        length = len(f"{url.path}?{url.query}".encode())
        code = "C" + "1" * (MAX_BATCH_TARGET_BYTES - length + 1)
        with patch("nci_sip_mcp.http_client._open", return_value=FakeResponse(b"[]")) as opened:
            accepted = self.batch([code], include=["synonyms", "definitions", "properties"])
            refused = self.batch([code + "1"], include=["synonyms", "definitions", "properties"])
        self.assertEqual(accepted["concepts"], [])
        self.assertEqual(accepted["missing"], [code])
        self.assertEqual(refused["error"]["code"], "invalid_request")
        self.assertEqual(opened.call_count, 1)
        target = urlsplit(opened.call_args.args[0].full_url)
        self.assertEqual(len(f"{target.path}?{target.query}".encode()), 7000)

    def test_percent_encoding_counts_bytes_instead_of_input_characters(self):
        code = "é" * 1200
        with patch("nci_sip_mcp.http_client._open") as opened:
            result = self.batch([code], terminology="other")
        self.assertEqual(result["error"]["code"], "invalid_request")
        self.assertEqual(opened.call_count, 0)

    def test_oversized_body_within_input_bounds_names_the_response_cap_never_partial(self):
        self.client.http.max_response_bytes = 100
        raw = [concept("C1", active=True), concept("C2", active=True)]
        with patch(
            "nci_sip_mcp.http_client._open", return_value=FakeResponse(json.dumps(raw).encode())
        ) as opened:
            result = self.batch(["C1", "C2"])
        self.assertEqual(result["error"]["code"], "bound_exceeded")
        self.assertEqual(result["error"]["details"]["bound"], "NCI_SIP_EVS_MAX_RESPONSE_BYTES")
        self.assertEqual(result["error"]["details"]["limit"], 100)
        self.assertNotIn("concepts", result)
        self.assertEqual(opened.call_count, 1)
