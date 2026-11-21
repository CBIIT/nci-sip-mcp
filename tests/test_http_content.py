"""POST and explicit export text use the existing bounded, audited HTTP path."""

import json
from urllib.parse import parse_qs

from nci_sip_mcp.errors import PlatformError, correlated
from nci_sip_mcp.http_client import (
    UpstreamRejectedError,
    UpstreamTooLargeError,
    UpstreamUnavailableError,
)
from test_http_client import Reply, ServerTestCase


class HttpContentTest(ServerTestCase):
    def test_form_retries_preserve_encoding_headers_and_request_accounting(self):
        server = self.serve(Reply(503), Reply(body=b'{"results": {"bindings": []}}'))
        client = self.client(server)
        records = []
        client.on_request = records.append
        query = 'SELECT ?x WHERE { ?x ?p "alpha & beta / λ + %" } LIMIT 2'
        with correlated("form-call"):
            result = client.post_form(
                "/sparql", {"query": query}, accept="application/sparql-results+json"
            )
        self.assertEqual(result, {"results": {"bindings": []}})
        self.assertEqual(server.bodies[0], server.bodies[1])
        self.assertEqual(parse_qs(server.bodies[0].decode()), {"query": [query]})
        self.assertEqual([(r.method, r.attempt) for r in records], [("POST", 1), ("POST", 2)])
        for _, headers in server.seen:
            self.assertEqual(headers["content-type"], "application/x-www-form-urlencoded")
            self.assertEqual(headers["accept"], "application/sparql-results+json")
            self.assertEqual(headers["x-correlation-id"], "form-call")

    def test_form_redirect_never_forwards_credentials_or_changes_method(self):
        target = self.serve(Reply(body=b"{}"))
        for location in ("/other", target.url + "/other"):
            with self.subTest(location=location):
                server = self.serve(Reply(302, headers={"Location": location}))
                client = self.client(server)
                client.credentials = {"Authorization": "fixture-only"}
                with self.assertRaises(UpstreamRejectedError) as raised:
                    client.post_form(
                        "/sparql",
                        {"query": "SELECT * {} LIMIT 2"},
                        accept="application/sparql-results+json",
                    )
                self.assertEqual(raised.exception.details["status"], 302)
                self.assertEqual(len(server.bodies), 1)
                self.assertEqual(target.seen, [])

    def test_form_responses_keep_the_size_bound_and_masked_errors(self):
        for reply, error in (
            (Reply(body=b"x" * 1001), UpstreamTooLargeError),
            (Reply(body=b'{"apiResponse":{"type":"E"}}'), PlatformError),
        ):
            with self.subTest(error=error):
                server = self.serve(reply)
                with self.assertRaises(error) as raised:
                    self.client(server).post_form(
                        "/sparql",
                        {"query": "SELECT * {} LIMIT 2"},
                        accept="application/sparql-results+json",
                    )
                self.assertTrue(raised.exception.details)
                self.assertEqual(len(server.bodies), 1)

    def test_post_redirect_never_changes_the_method_or_resubmits_the_body(self):
        for status in (301, 302, 303, 307, 308):
            with self.subTest(status=status):
                server = self.serve(Reply(status, headers={"Location": "/other"}))
                with self.assertRaises(UpstreamRejectedError) as raised:
                    self.client(server).post_json("/match", {"entity": "x"})
                self.assertEqual(raised.exception.details["status"], status)
                self.assertEqual([path for path, _ in server.seen], ["/match"])
                self.assertEqual(len(server.bodies), 1)

    def test_post_retries_the_same_json_body_and_endpoint_headers(self):
        server = self.serve(Reply(503), Reply(body=b'{"matches": []}'))
        client = self.client(server)
        records = []
        client.on_request = records.append
        body = {"entity": "alpha & beta / λ", "entityUserTip": "exact text"}
        with correlated("post-call"):
            result = client.post_json("/match", body, headers={"matchLimit": "7"})
        self.assertEqual(result, {"matches": []})
        self.assertEqual(server.bodies, [json.dumps(body).encode()] * 2)
        self.assertEqual([(r.method, r.attempt) for r in records], [("POST", 1), ("POST", 2)])
        for _, headers in server.seen:
            self.assertEqual(headers["content-type"], "application/json")
            self.assertEqual(headers["accept"], "application/json")
            self.assertEqual(headers["matchlimit"], "7")
            self.assertEqual(headers["x-correlation-id"], "post-call")

    def test_post_keeps_masked_failures_as_errors(self):
        server = self.serve(Reply(body=b'{"apiResponse":{"type":"E"}}'))
        with self.assertRaises(PlatformError) as raised:
            self.client(server).post_json("/match", {"entity": "x"})
        self.assertEqual(raised.exception.code, "upstream_unavailable")
        self.assertEqual(len(server.bodies), 1)

    def test_text_is_explicit_and_retried_but_json_still_rejects_html(self):
        listing = b'<a href="file.zip">file.zip</a>'
        server = self.serve(Reply(503), Reply(body=listing), Reply(body=listing))
        client = self.client(server)
        self.assertEqual(client.get_text("/exports/"), listing.decode())
        with self.assertRaises(PlatformError) as raised:
            client.get_json("/api")
        self.assertEqual(raised.exception.code, "upstream_unavailable")
        self.assertEqual(
            [headers["accept"] for _, headers in server.seen],
            ["text/html", "text/html", "application/json"],
        )

    def test_text_has_the_same_size_bound_and_utf8_failure_is_audited(self):
        for reply, error, failure in (
            (Reply(body=b"x" * 1001), UpstreamTooLargeError, "too_large"),
            (Reply(body=b"\xff"), UpstreamUnavailableError, "unusable_response"),
        ):
            with self.subTest(failure=failure):
                client = self.client(self.serve(reply))
                records = []
                client.on_request = records.append
                with self.assertRaises(error):
                    client.get_text("/exports/")
                self.assertEqual([(r.attempt, r.failure) for r in records], [(1, failure)])
