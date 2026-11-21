import io
import json
import unittest
from dataclasses import replace
from email.message import Message
from http.client import IncompleteRead
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

from fakes import concept, release
from nci_sip_mcp.errors import PlatformError
from nci_sip_mcp.evs import EVSClient, EVSNotFoundError, EVSResponseError, concept_path
from nci_sip_mcp.http_client import (
    UpstreamRejectedError,
    UpstreamTimeoutError,
    UpstreamTooLargeError,
    UpstreamUnavailableError,
)


class FakeResponse:
    def __init__(self, payload, headers=None):
        self.payload = payload
        # Header names are case-insensitive, as on a real response.
        self.status = 200
        self.headers = Message()
        for name, value in (headers or {}).items():
            self.headers[name] = value

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self, limit):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload[:limit]

    def read1(self, limit):
        payload = self.read(limit)
        self.payload = self.payload[len(payload) :]
        return payload


def http_error(status, body=b""):
    return HTTPError("https://example.invalid", status, "Reason", {}, io.BytesIO(body))


@patch("nci_sip_mcp.http_client.time.sleep")
@patch("nci_sip_mcp.http_client._open")
class EVSClientTest(unittest.TestCase):
    def test_gdc_mapset_and_page_use_the_api_paths_and_verify_release(self, urlopen, sleep):
        client = self.client()
        urlopen.return_value = FakeResponse(b'{"code":"NCIt_Maps_To_GDC","version":"26.06e"}')
        self.assertEqual(client.get_gdc_mapset(release())["version"], "26.06e")
        self.assertEqual(
            urlsplit(urlopen.call_args.args[0].full_url).path,
            "/api/v1/mapset/NCIt_Maps_To_GDC",
        )
        urlopen.return_value = FakeResponse(b'{"total":0}')
        self.assertEqual(client.get_gdc_maps("C4817"), ([], 0))
        self.assertTrue(
            urlopen.call_args.args[0].full_url.endswith(
                "/api/v1/mapset/NCIt_Maps_To_GDC/maps?term=C4817&fromRecord=0&pageSize=10"
            )
        )

    def test_gdc_missing_identity_and_incomplete_pages_fail_closed(self, urlopen, sleep):
        client = self.client()
        for payload in ([], {}, {"code": "other", "version": "26.06e"}):
            with self.subTest(payload=payload):
                urlopen.return_value = FakeResponse(json.dumps(payload).encode())
                with self.assertRaises(EVSResponseError):
                    client.get_gdc_mapset(release())
        for payload in (
            {},
            [],
            {"maps": [], "total": True},
            {"maps": [], "total": -1},
            {"maps": [], "total": 1},
            {"maps": ["bad"], "total": 1},
        ):
            with self.subTest(payload=payload):
                urlopen.return_value = FakeResponse(json.dumps(payload).encode())
                with self.assertRaises(EVSResponseError):
                    client.get_gdc_maps("C4817")

    def test_gdc_offset_beyond_total_is_an_empty_page(self, urlopen, sleep):
        urlopen.return_value = FakeResponse(b'{"maps":[],"total":3}')
        self.assertEqual(self.client().get_gdc_maps("C4817", offset=10), ([], 3))
        self.assertEqual(
            urlsplit(urlopen.call_args.args[0].full_url).query,
            "term=C4817&fromRecord=10&pageSize=10",
        )

    def test_filtered_empty_terminology_queries_remain_empty(self, urlopen, sleep):
        urlopen.return_value = FakeResponse(b"[]")
        for arguments in ({"terminology": "unknown"}, {"latest": True}, {"tag": "monthly"}):
            with self.subTest(arguments=arguments):
                self.assertEqual(self.client().get_terminologies(**arguments), [])

    def test_empty_authoritative_listing_is_one_unusable_attempt(self, urlopen, sleep):
        urlopen.return_value = FakeResponse(b"[]")
        client = self.client()
        records = []
        client.http.on_request = records.append
        with self.assertRaises(UpstreamUnavailableError) as raised:
            client.get_terminologies()
        self.assertEqual(raised.exception.details, {"surface": "evs", "status": 200, "attempts": 1})
        self.assertEqual(
            [(item.attempt, item.failure) for item in records], [(1, "unusable_response")]
        )

    def client(self, **options):
        client = EVSClient("https://example.invalid/", **options)
        # No jitter: a backoff is waited in full.
        client.http.jitter = lambda low, high: high
        return client

    def test_transient_failures_are_retried_until_one_succeeds(self, urlopen, sleep):
        failures = {
            "network": URLError("temporary"),
            "timeout": TimeoutError("timed out"),
            "reset": ConnectionResetError("reset"),
            "http 429": http_error(429),
            "http 500": http_error(500),
            "http 503": http_error(503),
            "truncated body": FakeResponse(IncompleteRead(b"")),
            "body shorter than declared": FakeResponse(b'{"vers', {"Content-Length": "20"}),
            # Only a chunked body makes http.client ignore the declared length.
            "short body in another encoding": FakeResponse(
                b'{"vers', {"Content-Length": "20", "Transfer-Encoding": "identity"}
            ),
            "short body in a compound encoding": FakeResponse(
                b'{"vers', {"Content-Length": "20", "Transfer-Encoding": "gzip, chunked"}
            ),
        }
        for label, failure in failures.items():
            with self.subTest(label):
                urlopen.reset_mock()
                urlopen.side_effect = [failure, FakeResponse(b'{"version": "test"}')]

                with self.assertLogs("nci_sip_mcp.http_client", level="WARNING"):
                    result = self.client(max_attempts=2).get_api_version()

                self.assertEqual(result, {"version": "test"})
                self.assertEqual(urlopen.call_count, 2)

    def test_exhausted_attempts_raise_unavailable_after_exponential_backoff(self, urlopen, sleep):
        urlopen.side_effect = [
            URLError("down"),
            http_error(503),
            TimeoutError("timed out"),
            URLError("still down"),
        ]

        with (
            self.assertLogs("nci_sip_mcp.http_client", level="WARNING"),
            self.assertRaises(UpstreamUnavailableError) as raised,
        ):
            self.client(max_attempts=4, retry_backoff_seconds=0.25).get_api_version()

        self.assertEqual(urlopen.call_count, 4)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [0.25, 0.5, 1.0])
        self.assertEqual(
            str(raised.exception),
            "EVS request failed for /api/v1/version: transport failure",
        )

    def test_a_request_that_only_timed_out_is_a_timeout_with_what_was_waited(self, urlopen, sleep):
        for label, failure in {
            "read": TimeoutError("timed out"),
            "connect": URLError(TimeoutError("timed out")),
        }.items():
            with self.subTest(label):
                urlopen.side_effect = [failure, failure]

                with (
                    self.assertLogs("nci_sip_mcp.http_client", level="WARNING"),
                    self.assertRaises(UpstreamTimeoutError) as raised,
                ):
                    self.client(timeout_seconds=7, max_attempts=2).get_api_version()

                self.assertEqual(
                    raised.exception.details, {"surface": "evs", "seconds": 7, "attempts": 2}
                )

    def test_a_timeout_followed_by_another_failure_is_not_a_timeout(self, urlopen, sleep):
        urlopen.side_effect = [TimeoutError("timed out"), URLError("refused")]

        with (
            self.assertLogs("nci_sip_mcp.http_client", level="WARNING"),
            self.assertRaises(UpstreamUnavailableError) as raised,
        ):
            self.client(max_attempts=2).get_api_version()

        self.assertNotIsInstance(raised.exception, UpstreamTimeoutError)

    def test_a_server_error_followed_by_a_timeout_is_not_a_timeout(self, urlopen, sleep):
        urlopen.side_effect = [http_error(500), TimeoutError("timed out")]

        with (
            self.assertLogs("nci_sip_mcp.http_client", level="WARNING"),
            self.assertRaises(UpstreamUnavailableError) as raised,
        ):
            self.client(max_attempts=2).get_api_version()

        self.assertNotIsInstance(raised.exception, UpstreamTimeoutError)
        self.assertEqual(raised.exception.details, {"surface": "evs", "attempts": 2, "status": 500})

    def test_an_unavailable_error_carries_the_status_attempts_and_retry_after(self, urlopen, sleep):
        headers = Message()
        headers["Retry-After"] = "120"
        busy = HTTPError("https://example.invalid", 429, "Too Many Requests", headers, io.BytesIO())
        urlopen.side_effect = [busy, URLError("refused"), http_error(503)]
        client = self.client(max_attempts=1)
        expected = [
            {"surface": "evs", "attempts": 1, "status": 429, "retryAfter": "120"},
            {"surface": "evs", "attempts": 1},
            {"surface": "evs", "attempts": 1, "status": 503},
        ]

        for details in expected:
            with self.subTest(details=details):
                with self.assertRaises(UpstreamUnavailableError) as raised:
                    client.get_api_version()

                self.assertEqual(raised.exception.details, details)

    def test_a_rejected_request_carries_the_status(self, urlopen, sleep):
        urlopen.side_effect = http_error(403)

        with self.assertRaises(UpstreamRejectedError) as raised:
            self.client().get_api_version()

        self.assertEqual(raised.exception.details, {"surface": "evs", "status": 403, "attempts": 1})

    def test_a_missing_concept_carries_its_code(self, urlopen, sleep):
        urlopen.side_effect = http_error(404, b'{"message": "C1 not found"}')

        with self.assertRaises(EVSNotFoundError) as raised:
            self.client().get_concept("C1", release=release())

        self.assertEqual(raised.exception.details, {"identifiers": ["C1"]})

    def test_an_endpoint_that_is_not_served_carries_the_404(self, urlopen, sleep):
        urlopen.side_effect = http_error(404)

        with self.assertRaises(EVSResponseError) as raised:
            self.client().get_api_version()

        self.assertEqual(raised.exception.details, {"surface": "evs", "status": 404, "attempts": 1})

    def test_the_uri_of_a_concept_is_the_url_its_request_goes_to(self, urlopen, sleep):
        urlopen.side_effect = lambda request, timeout: FakeResponse(
            json.dumps(concept("C3262")).encode()
        )
        client = self.client(max_response_bytes=4096)

        client.get_concept("C3262", release=release("26.06e"))

        (request,) = [call.args[0] for call in urlopen.call_args_list]
        uri = client.uri(concept_path("ncit_26.06e", "C3262"))
        self.assertEqual(request.full_url.partition("?")[0], uri)
        self.assertEqual(uri, "https://example.invalid/api/v1/concept/ncit_26.06e/C3262")
        # The limit an oversized answer is held to, which a truncation record reports.
        self.assertEqual(client.max_response_bytes, 4096)

    def test_requests_use_the_configured_timeout(self, urlopen, sleep):
        # The transport answers with the timeout it was given.
        def transport(request, timeout):
            return FakeResponse(json.dumps({"timeout": timeout}).encode())

        urlopen.side_effect = transport

        version = EVSClient("https://example.invalid", timeout_seconds=2.5).get_api_version()

        self.assertEqual(version, {"timeout": 2.5})

    def test_only_a_single_concept_request_reports_a_missing_concept(self, urlopen, sleep):
        client = self.client()
        calls = {
            "version": client.get_api_version,
            "terminologies": client.get_terminologies,
            "batch": lambda: client.get_concepts_by_codes(["C1"], release=release()),
            "descendants": lambda: client.get_descendants("C1", 1, release=release()),
        }
        for label, call in calls.items():
            with self.subTest(label):
                urlopen.side_effect = http_error(404, b'{"message": "No static resource"}')
                with self.assertRaises(EVSResponseError) as raised:
                    call()
                self.assertNotIsInstance(raised.exception, EVSNotFoundError)
                self.assertIn("NCI_SIP_EVS_BASE_URL", str(raised.exception))
        urlopen.side_effect = http_error(404, b'{"message": "C1 not found"}')
        with self.assertRaises(EVSNotFoundError):
            client.get_concept("C1", release=release())

    def test_an_error_without_a_usable_body_is_described_by_its_status(self, urlopen, sleep):
        class BrokenBody(io.BytesIO):
            def __init__(self, error):
                super().__init__()
                self.error = error

            def read(self, *_):
                raise self.error

        bodies = {
            "no body": None,
            "not JSON": io.BytesIO(b"<html>"),
            "a JSON list": io.BytesIO(b"[]"),
            "a JSON string": io.BytesIO(b'"text"'),
            "no message": io.BytesIO(b"{}"),
            "longer than the part that is read": io.BytesIO(
                json.dumps({"message": "x" * 5000}).encode()
            ),
            "unreadable": BrokenBody(OSError("connection reset")),
            "cut short": BrokenBody(IncompleteRead(b"")),
        }
        for case, body in bodies.items():
            with self.subTest(case):
                urlopen.side_effect = HTTPError(
                    "https://example.invalid", 403, "Forbidden", Message(), body
                )

                with self.assertRaises(UpstreamRejectedError) as raised:
                    self.client().get_api_version()

                self.assertTrue(str(raised.exception).endswith("HTTP 403 Forbidden"))

    def test_a_backoff_of_zero_retries_without_waiting(self, urlopen, sleep):
        urlopen.side_effect = [URLError("temporary"), FakeResponse(b'{"version": "test"}')]

        with self.assertLogs("nci_sip_mcp.http_client", level="WARNING"):
            result = self.client(max_attempts=2, retry_backoff_seconds=0).get_api_version()

        self.assertEqual(result, {"version": "test"})
        sleep.assert_not_called()

    def test_not_found_is_distinct_with_server_owned_wording(self, urlopen, sleep):
        urlopen.side_effect = http_error(404, b'{"status": 404, "message": "C999 not found"}')

        with self.assertRaises(EVSNotFoundError) as raised:
            self.client().get_concept("C999", release=release())

        self.assertEqual(urlopen.call_count, 1)
        self.assertEqual(
            str(raised.exception),
            "EVS request failed for /api/v1/concept/ncit_26.06e/C999: HTTP 404 Not Found",
        )

    def test_one_attempt_means_no_retry(self, urlopen, sleep):
        urlopen.side_effect = URLError("down")

        with self.assertRaises(UpstreamUnavailableError):
            self.client(max_attempts=1).get_api_version()

        self.assertEqual(urlopen.call_count, 1)
        sleep.assert_not_called()

    def test_a_complete_body_at_the_size_limit_is_accepted(self, urlopen, sleep):
        client = self.client(max_response_bytes=10)
        for label, headers in {
            "declared": {"Content-Length": "10"},
            "unparsable": {"Content-Length": "ten"},
            "undeclared": {},
            # http.client decodes a chunked body and ignores its Content-Length.
            "chunked": {"content-length": "50", "transfer-encoding": "Chunked"},
        }.items():
            with self.subTest(label):
                urlopen.return_value = FakeResponse(b'{"a": 123}', headers)

                self.assertEqual(client.get_api_version(), {"a": 123})

        self.assertEqual(urlopen.call_count, 4)

    def test_response_size_is_bounded_before_and_while_reading(self, urlopen, sleep):
        class UnreadableResponse(FakeResponse):
            def read(self, limit):
                raise AssertionError("an oversized response must not be read")

        client = self.client(max_response_bytes=10)
        responses = {
            "declared": UnreadableResponse(b"", {"Content-Length": "11"}),
            "undeclared": FakeResponse(b'{"a": 12345}'),
        }
        for label, response in responses.items():
            with self.subTest(label):
                urlopen.reset_mock()
                urlopen.side_effect = None
                urlopen.return_value = response

                with self.assertRaises(UpstreamTooLargeError) as raised:
                    client.get_api_version()

                self.assertEqual(
                    raised.exception.details,
                    {"bound": "NCI_SIP_EVS_MAX_RESPONSE_BYTES", "limit": 10, "reached": 11},
                )
                self.assertEqual(urlopen.call_count, 1)

        urlopen.return_value = FakeResponse(b'{"a": 123}')
        self.assertEqual(client.get_api_version(), {"a": 123})

    def test_unusable_bodies_are_upstream_failures_that_are_not_retried(self, urlopen, sleep):
        for body in (b"<html>", b"\xff"):
            with self.subTest(body=body):
                urlopen.reset_mock()
                urlopen.return_value = FakeResponse(body)

                with self.assertRaises(PlatformError) as raised:
                    self.client().get_api_version()

                self.assertEqual(raised.exception.code, "upstream_unavailable")
                self.assertEqual(urlopen.call_count, 1)

    def test_unexpected_shapes_raise_response_errors(self, urlopen, sleep):
        client = self.client()
        calls = {
            "version as list": (b"[]", client.get_api_version),
            "terminologies as object": (b"{}", client.get_terminologies),
            "terminologies of strings": (b'["ncit"]', client.get_terminologies),
            "concept as list": (b"[]", lambda: client.get_concept("C1", release=release())),
            "concept list as object": (
                b"{}",
                lambda: client.get_concepts_by_codes(["C1"], release=release()),
            ),
            "descendants as object": (
                b"{}",
                lambda: client.get_descendants("C1", 1, release=release()),
            ),
        }
        for label, (body, call) in calls.items():
            with self.subTest(label):
                urlopen.return_value = FakeResponse(body)
                with self.assertRaises(EVSResponseError):
                    call()

    def test_requests_address_the_given_terminology(self, urlopen, sleep):
        client = self.client()
        calls = {
            "/api/v1/concept/ncit_26.06e/C1?include=minimal": (
                json.dumps(concept("C1")).encode(),
                lambda: client.get_concept("C1", release=release("26.06e"), include="minimal"),
            ),
            "/api/v1/concept/ncit_26.06e?list=C1%2CC2&include=minimal": (
                b"[]",
                lambda: client.get_concepts_by_codes(
                    ["C1", "C2"], release=release("26.06e"), include="minimal"
                ),
            ),
            "/api/v1/concept/ncit_26.06e/C1/descendants?maxLevel=2": (
                b"[]",
                lambda: client.get_descendants("C1", 2, release=release("26.06e")),
            ),
            "/api/v1/concept/ncit_26.06e/C1/descendants?maxLevel=1": (
                b"[]",
                lambda: client.get_descendants("C1", 1, release=release()),
            ),
            "/api/v1/concept/ncit_26.06e?list=C1&include=summary%2Cdefinitions"
            "%2Csynonyms%2Cproperties": (
                b"[]",
                lambda: client.get_concepts_by_codes(["C1"], release=release()),
            ),
            "/api/v1/concept/ncit_26.06e/C1?include=summary%2Cdefinitions%2Csynonyms%2Cproperties"
            "%2Cparents%2Cchildren%2Croles%2CinverseRoles%2Cassociations%2CinverseAssociations": (
                json.dumps(concept("C1")).encode(),
                lambda: client.get_concept("C1", release=release()),
            ),
        }
        for path, (body, call) in calls.items():
            with self.subTest(path):
                urlopen.return_value = FakeResponse(body)
                call()
                request = urlopen.call_args.args[0]
                self.assertEqual(request.full_url, f"https://example.invalid{path}")
                self.assertEqual(request.get_header("Accept"), "application/json")

    def test_no_codes_means_no_request(self, urlopen, sleep):
        self.assertEqual(self.client().get_concepts_by_codes([], release=release()), [])
        urlopen.assert_not_called()

    def test_an_empty_body_without_a_declared_length_is_invalid_not_retried(self, urlopen, sleep):
        urlopen.return_value = FakeResponse(b"")

        with self.assertRaises(PlatformError):
            self.client().get_api_version()

        self.assertEqual(urlopen.call_count, 1)

    def test_a_body_one_byte_short_is_retried(self, urlopen, sleep):
        urlopen.side_effect = [
            FakeResponse(b'{"a": 1}', {"Content-Length": "9"}),
            FakeResponse(b'{"a": 1}\n', {"Content-Length": "9"}),
        ]

        with self.assertLogs("nci_sip_mcp.http_client", level="WARNING"):
            result = self.client(max_attempts=2).get_api_version()

        self.assertEqual(result, {"a": 1})
        self.assertEqual(urlopen.call_count, 2)

    def test_a_404_from_a_metadata_request_names_the_endpoint_and_next_step(self, urlopen, sleep):
        urlopen.side_effect = http_error(404, b'{"message": "No static resource"}')

        with self.assertRaises(EVSResponseError) as raised:
            self.client().get_terminologies()

        self.assertEqual(
            str(raised.exception),
            "EVS request failed for /api/v1/metadata/terminologies: HTTP 404 Not Found; "
            "EVS does not serve this endpoint or release, check NCI_SIP_EVS_BASE_URL",
        )

    def test_an_empty_body_with_an_ignored_length_is_invalid_not_retried(self, urlopen, sleep):
        for label, headers in {
            "chunked": {"Content-Length": "5", "Transfer-Encoding": "chunked"},
            "unparsable": {"Content-Length": "five"},
        }.items():
            with self.subTest(label):
                urlopen.reset_mock()
                urlopen.return_value = FakeResponse(b"", headers)

                with self.assertRaises(PlatformError):
                    self.client().get_api_version()

                self.assertEqual(urlopen.call_count, 1)


class EVSContentBoundaryTest(unittest.TestCase):
    def setUp(self):
        self.client = EVSClient("https://evs.invalid", max_attempts=1)
        self.release = release()

    def reply(self, value):
        return patch(
            "nci_sip_mcp.http_client._open", return_value=FakeResponse(json.dumps(value).encode())
        )

    def test_all_terminology_path_segments_are_encoded(self):
        selected = replace(self.release, pinned_terminology="other/version +?")
        cases = (
            (lambda: self.client.get_concept("C1", selected), concept("C1"), "concept", "/C1"),
            (
                lambda: self.client.get_relationship_catalogue(selected, "role"),
                [],
                "metadata",
                "/roles",
            ),
            (
                lambda: self.client.get_replacements("C1", selected),
                [],
                "history",
                "/C1/replacements",
            ),
        )
        for action, body, group, suffix in cases:
            with self.subTest(group=group), self.reply(body) as opened:
                self.assertEqual(action(), body)
                self.assertEqual(
                    urlsplit(opened.call_args.args[0].full_url).path,
                    f"/api/v1/{group}/other%2Fversion%20%2B%3F{suffix}",
                )

    def test_search_offset_past_total_is_a_valid_empty_page(self):
        with self.reply({"total": 1}):
            self.assertEqual(
                self.client.search_concepts(self.release, "q", "lexical", 2, 3), (1, [])
            )

    def test_search_rejects_empty_and_nontext_codes_at_the_client_boundary(self):
        for code in ("", None, 1, []):
            with (
                self.subTest(code=code),
                self.reply({"total": 1, "concepts": [concept("C1") | {"code": code}]}),
                self.assertRaises(EVSResponseError),
            ):
                self.client.search_concepts(self.release, "q", "lexical", 0, 10)

    def test_live_search_requests_page_size_and_highlights(self):
        with self.reply({"total": 0}) as opened:
            self.assertEqual(
                self.client.search_concepts(self.release, "q", "lexical", 0, 7), (0, [])
            )
        params = parse_qs(urlsplit(opened.call_args.args[0].full_url).query)
        self.assertEqual(params["include"], ["minimal,highlights"])
        self.assertEqual(params["pageSize"], ["7"])

    def test_paths_start_with_the_seed_and_every_code_is_nonempty(self):
        for codes in (("C2", "C1"), ("C1", "")):
            with (
                self.subTest(codes=codes),
                self.reply([[concept(code) for code in codes]]),
                self.assertRaises(EVSResponseError),
            ):
                self.client.get_paths_to_root("C1", self.release)

    def test_history_rows_require_their_source_code(self):
        with self.reply([{"replacementCode": "C2"}]), self.assertRaises(EVSResponseError):
            self.client.get_replacements("C1", self.release)

    def test_an_empty_batch_returns_without_a_network_request(self):
        with patch(
            "nci_sip_mcp.http_client._open", side_effect=AssertionError("unexpected request")
        ):
            self.assertEqual(self.client.get_concepts_by_codes([], self.release), [])


if __name__ == "__main__":
    unittest.main()
