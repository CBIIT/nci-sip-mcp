"""Untrusted response parsing and query prefixes at their public caller boundaries."""

import json
import sys
from functools import partial
from json.scanner import py_make_scanner
from unittest.mock import patch

from fakes import concept, release
from nci_si_mcp.cadsr import CaDSRClient
from nci_si_mcp.config import Settings
from nci_si_mcp.errors import correlated
from nci_si_mcp.evs import EVSClient
from nci_si_mcp.invocation import call
from ncit_index.embeddings import EmbeddingProvider
from test_http_client import BufferedPolicy, Reply, ServerTestCase
from test_index import IndexTestCase


class ResponseParsingTest(ServerTestCase):
    def setUp(self):
        # The C decoder's depth limit varies by Python build; use the real stdlib Python parser.
        self.addCleanup(sys.setrecursionlimit, sys.getrecursionlimit())
        sys.setrecursionlimit(1000)
        decoder = json.JSONDecoder()
        self.enterContext(patch.object(decoder, "scan_once", py_make_scanner(decoder)))
        self.enterContext(patch.object(json, "_default_decoder", decoder))

    def test_deep_form_json_reaches_common_unavailable_error_classification(self):
        body = b"[" * 2000 + b"0" + b"]" * 2000
        server = self.serve(Reply(200, body))
        client = CaDSRClient(Settings(cadsr_base_url=server.url))
        with correlated("deep-form"):
            error = call("get_form", lambda: client.get_form("123"))["error"]
        self.assertEqual(error["code"], "upstream_unavailable")
        self.assertEqual(error["correlationId"], "deep-form")
        self.assertEqual(error["details"], {"surface": "cadsr", "attempts": 1})
        self.assertEqual(len(server.seen), 1)

    def test_deep_404_concept_error_body_is_unavailable_not_concept_absence(self):
        body = b"[" * 2000 + b"0" + b"]" * 2000
        server = self.serve(Reply(404, body))
        client = EVSClient(server.url, max_attempts=1)
        with correlated("deep-concept-404"):
            result = call(
                "get_concept",
                lambda: client.get_concept("C1", release=release(), include="minimal"),
            )
        self.assertEqual(result["error"]["code"], "upstream_unavailable")
        self.assertEqual(result["error"]["correlationId"], "deep-concept-404")
        self.assertEqual(
            result["error"]["details"], {"surface": "evs", "status": 404, "attempts": 1}
        )
        self.assertEqual(
            [path for path, _ in server.seen], ["/api/v1/concept/ncit_26.06e/C1?include=minimal"]
        )

    def test_deep_success_json_is_an_upstream_failure_with_attempts(self):
        # Given deep JSON under the byte cap, parsing reports an upstream failure.
        body = b"[" * 2000 + b"0" + b"]" * 2000
        server = self.serve(Reply(503), Reply(200, body), Reply())
        client = self.client(server, max_response_bytes=8192)
        with correlated("deep-success"):
            error = call("read", lambda: client.get_json("/x"))["error"]
        self.assertEqual(error["code"], "upstream_unavailable")
        self.assertEqual(error["details"], {"surface": "evs", "attempts": 2})
        self.assertEqual(error["correlationId"], "deep-success")
        self.assertEqual(len(server.seen), 2)
        self.assertEqual(client.get_json("/x"), {"ok": True})

    def test_controlled_deep_error_body_is_terminal_with_exact_details(self):
        # The real Python decoder setup above makes this 4001-byte body deterministic.
        body = b"[" * 2000 + b"0" + b"]" * 2000
        for status in (400, 404, 503):
            with self.subTest(status=status):
                server = self.serve(Reply(status, body), Reply())
                policy = BufferedPolicy(server.url + "/x")
                client = self.client(server, request_policy=policy)
                with correlated("deep-controlled-error"):
                    result = call("read", partial(client.get_json, "/x"))
                self.assertEqual(
                    result,
                    {
                        "error": {
                            "code": "upstream_unavailable",
                            "message": "The upstream error body exceeds JSON nesting limits. "
                            "Retry later. Retry later.",
                            "details": {"surface": "evs", "status": status, "attempts": 1},
                            "correlationId": "deep-controlled-error",
                        }
                    },
                )
                self.assertEqual([path for path, _ in server.seen], ["/x"])
                self.assertEqual(self.waits, [])

    def test_deep_http_error_body_is_unavailable_without_claiming_absence(self):
        # A parser-depth failure is terminal, even when the status alone suggested absence.
        body = b"[" * 2000 + b"0" + b"]" * 2000
        for status, attempts in ((400, 1), (404, 1), (503, 1)):
            with self.subTest(status=status):
                server = self.serve(*(Reply(status, body) for _ in range(attempts)))
                client = self.client(server)
                with correlated("deep-error"):
                    error = call("read", partial(client.get_json, "/x"))["error"]
                self.assertEqual(error["code"], "upstream_unavailable")
                self.assertEqual(
                    error["details"], {"surface": "evs", "status": status, "attempts": attempts}
                )
                self.assertEqual(len(server.seen), attempts)

    def test_unknown_terminology_requires_a_whole_valid_token_of_at_most_64(self):
        # Given EVS prose, only a complete bounded identifier identifies a release.
        valid = "ncit_A0.-" + "x" * 55
        cases = ((valid, ""), (valid, ") trailing prose"), ("ncit_x", " more prose"))
        cases += tuple((token, "") for token in (valid + "x", valid + "/bad", "ncit_x:bad"))
        for token, suffix in cases:
            with self.subTest(token=token, suffix=suffix):
                body = json.dumps({"message": f"Error: Terminology not found = {token}{suffix}"})
                server = self.serve(Reply(404, body.encode()))
                client = EVSClient(server.url, max_attempts=1)
                with correlated("unknown-terminology"):
                    error = call("version", client.get_api_version)["error"]
                accepted = token in (valid, "ncit_x")
                self.assertEqual(
                    error["code"], "release_not_available" if accepted else "upstream_unavailable"
                )
                expected = (
                    {"requested": token, "source": "evs"}
                    if accepted
                    else {"surface": "evs", "status": 404, "attempts": 1}
                )
                self.assertEqual(error["details"], expected)
                self.assertEqual(len(server.seen), 1)


class TailProvider(EmbeddingProvider):
    """Two independent directions make dropping the final query token change ranking."""

    name = "tail-contract"
    model = "two-directions"

    def embed(self, texts):
        return [[0.0, 1.0] if text.split()[-1] == "tail" else [1.0, 0.0] for text in texts]


class QueryParsingTest(IndexTestCase):
    def test_lexical_search_uses_exactly_the_first_64_tokens(self):
        # Given matches at positions 1, 64 and 65, only the first two are lexical hits.
        index = self.build(
            [concept("C1", "first"), concept("C2", "boundary"), concept("C3", "tail")]
        )
        prefix = ["first"] + [f"absent{number}" for number in range(62)]
        cases = (
            (prefix, ["C1"]),
            ([*prefix, "boundary"], ["C1", "C2"]),
            ([*prefix, "boundary", "tail"], ["C1", "C2"]),
            (["absent"] * 63 + ["boundary", "tail"], ["C2"]),
        )
        for tokens, expected in cases:
            with self.subTest(tokens=len(tokens), expected=expected):
                hits = index.search(", ".join(tokens), self.provider, mode="bm25", limit=10)
                self.assertEqual(sorted(hit.concept.code for hit in hits), expected)

    def test_vector_and_hybrid_embedding_receive_the_query_beyond_token_64(self):
        # Given a decisive 65th token, vector scoring must retain it in both modes.
        provider = TailProvider()
        index = self.build([concept("C1", "head"), concept("C2", "tail")], provider=provider)
        for mode in ("vector", "hybrid"):
            for query, expected in (("head " * 64, "C1"), ("head " * 64 + "tail", "C2")):
                with self.subTest(mode=mode, expected=expected):
                    hits = index.search(query, provider, mode=mode)
                    self.assertEqual(
                        {hit.concept.code: hit.score_components["vector"] for hit in hits},
                        {"C1": float(expected == "C1"), "C2": float(expected == "C2")},
                    )
