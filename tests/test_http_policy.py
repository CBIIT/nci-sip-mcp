"""Opt-in policy refusals through the caller, real transport and cached lookup."""

import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from threading import Barrier
from unittest.mock import patch

from fakes import concept, terminology_row
from nci_si_mcp import http_client
from nci_si_mcp.errors import correlated
from nci_si_mcp.evs import EVSClient
from nci_si_mcp.invocation import call
from nci_si_mcp.registry import invoke
from test_handlers import HandlerTestCase
from test_http_client import Reply, ServerTestCase

DENIED = {
    "error": {
        "code": "upstream_unavailable",
        "message": "Controlled upstream request refused. Retry later.",
        "correlationId": "policy-test",
    }
}


def scoped(policy):
    # Absence of the new interface drives the existing real caller in the red revision.
    return getattr(http_client, "using_request_policy", lambda _: nullcontext())(policy)


class DeniedPolicy:
    def request(self, request):
        raise http_client.RequestPolicyError("authorization")


class HttpPolicyTest(ServerTestCase):
    def test_policy_is_captured_and_nested_scope_restores_default(self):
        server = self.serve()
        default = self.client(server)
        with scoped(DeniedPolicy()):
            captured = self.client(server)
            with scoped(DeniedPolicy()):
                nested = self.client(server)
            restored = self.client(server)
        with correlated("policy-test"):
            for client in (captured, nested, restored):
                with self.subTest(client=client):
                    self.assertEqual(
                        call("read", lambda client=client: client.get_json("/x")), DENIED
                    )
            self.assertEqual(default.get_json("/x"), {"ok": True})
            self.assertEqual(self.client(server).get_json("/x"), {"ok": True})
        self.assertEqual(len(server.seen), 2)

    def test_concurrent_scopes_do_not_grant_another_callers_authority(self):
        server = self.serve()
        barrier = Barrier(2)

        def read(denied):
            with scoped(DeniedPolicy()) if denied else nullcontext():
                client = self.client(server)
                barrier.wait(timeout=5)
                with correlated("policy-test"):
                    return call("read", lambda client=client: client.get_json("/x"))

        with ThreadPoolExecutor(max_workers=2) as workers:
            denied, allowed = list(workers.map(read, (True, False)))
        self.assertEqual(denied, DENIED)
        self.assertEqual(allowed, {"ok": True})
        self.assertEqual(len(server.seen), 1)


class CachedPolicyTest(HandlerTestCase):
    def test_cached_lookup_cannot_hide_policy_denial(self):
        self.index("C3262")
        server = ServerTestCase.serve(self, Reply(body=json.dumps(concept("C3262")).encode()))
        with scoped(DeniedPolicy()):
            self.context.evs = EVSClient(server.url, 1)
        with (
            patch.object(self.context.evs, "get_terminologies", return_value=[terminology_row()]),
            correlated("policy-test"),
            self.assertLogs("nci_si_mcp", level="INFO") as logs,
        ):
            result = invoke(self.context, "lookup", "C3262")
        self.assertEqual(result, DENIED)
        completions = [
            row
            for entry in logs.records
            if (row := json.loads(entry.getMessage())).get("event") == "call_completed"
        ]
        self.assertEqual(len(completions), 1)
        self.assertEqual(completions[0]["outboundRequests"], 0)
        self.assertEqual(completions[0]["responseCode"], "upstream_unavailable")
        self.assertEqual(server.seen, [])
