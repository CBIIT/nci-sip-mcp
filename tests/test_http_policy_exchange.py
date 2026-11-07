"""HttpClient's buffered policy contract, independently of the future run ledger."""

from contextlib import contextmanager
from http import HTTPStatus
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, ProxyHandler, build_opener

from nci_si_mcp import http_client
from nci_si_mcp.audit import audited
from nci_si_mcp.bounds import Budget, budgeted, deadline_scope
from nci_si_mcp.cadsr import CaDSRClient
from nci_si_mcp.config import Settings
from nci_si_mcp.errors import PlatformError, correlated
from nci_si_mcp.http_client import RequestPolicyError, UpstreamRejectedError
from nci_si_mcp.invocation import call
from nci_si_mcp.request_policy import Exchange, using_request_policy
from test_http_client import Reply, ServerTestCase


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class BufferedPolicy:
    """An explicit local transport double for the core protocol's independent contract."""

    def __init__(self, url, *, unreadable=False, deny_wait=False):
        self.url, self.unreadable, self.deny_wait = url, unreadable, deny_wait
        self.waits = []
        self.reads = []

    @contextmanager
    def request(self, request):
        if request.full_url != self.url:
            raise RequestPolicyError("authorization")
        self.selected = request
        yield self

    def check_wait(self, delay_seconds):
        self.waits.append(delay_seconds)
        if self.deny_wait:
            raise RequestPolicyError("deadline")

    def exchange(self, *, timeout_seconds, response_limit, on_start):
        opener = build_opener(ProxyHandler({}), NoRedirect())
        on_start()
        try:
            response = opener.open(self.selected, timeout=timeout_seconds)
        except HTTPError as error:
            response = error
        with response:
            limit = 4096 if response.status >= HTTPStatus.BAD_REQUEST else response_limit + 1
            self.reads.append(limit)
            payload = response.read(limit)
            state = "unreadable" if self.unreadable else "complete"
            return Exchange(response.status, dict(response.headers), payload, state)


class BufferedPolicyTest(ServerTestCase):
    def guarded(self, server, **policy_options):
        policy = BufferedPolicy(server.url + "/x", **policy_options)
        with using_request_policy(policy):
            client = self.client(server)
        return client, policy

    def test_retry_after_case_preserves_wait(self):
        for header in ("retry-after", "rEtRy-AfTeR"):
            for controlled in (False, True):
                with self.subTest(header=header, controlled=controlled):
                    server = self.serve(Reply(429, headers={header: "2"}), Reply())
                    client = self.guarded(server)[0] if controlled else self.client(server)
                    client.retry_backoff_seconds = 0
                    self.assertEqual(client.get_json("/x"), {"ok": True})
                    self.assertEqual(self.waits, [2.0])
                    self.assertEqual(len(server.seen), 2)

    def test_retry_after_case_preserves_exhausted_details(self):
        for header in ("retry-after", "rEtRy-AfTeR"):
            for controlled in (False, True):
                with self.subTest(header=header, controlled=controlled):
                    server = self.serve(Reply(429, headers={header: "2"}))
                    client = self.guarded(server)[0] if controlled else self.client(server)
                    client.max_attempts = 1
                    with self.assertRaises(http_client.UpstreamUnavailableError) as caught:
                        client.get_json("/x")
                    self.assertEqual(
                        caught.exception.details,
                        {"surface": "evs", "attempts": 1, "status": 429, "retryAfter": "2"},
                    )
                    self.assertEqual(len(server.seen), 1)

    def test_error_body_empty_nonempty_and_unreadable_are_distinct(self):
        for body, unreadable, empty, message in (
            (b"", False, True, ""),
            (b'{"message":"not here"}', False, False, "not here"),
            (b"", True, False, ""),
            (b"not JSON", False, False, ""),
        ):
            with self.subTest(body=body, unreadable=unreadable):
                server = self.serve(Reply(404, body=body))
                client, policy = self.guarded(server, unreadable=unreadable)
                with self.assertRaises(UpstreamRejectedError) as caught:
                    client.get_json("/x")
                error = caught.exception
                self.assertEqual((error.empty_body, error.body_message), (empty, message))
                self.assertEqual(error.details, {"surface": "evs", "status": 404, "attempts": 1})
                self.assertEqual(policy.reads, [4096])
                self.assertEqual(len(server.seen), 1)

    def test_retry_after_and_wait_admission_are_separate_from_send_accounting(self):
        server = self.serve(Reply(429, headers={"Retry-After": "1"}), Reply())
        client, policy = self.guarded(server)
        self.assertEqual(client.get_json("/x"), {"ok": True})
        self.assertEqual(policy.waits, [1.0, 0])
        self.assertEqual(self.waits, [1.0])
        self.assertEqual(len(server.seen), 2)
        server = self.serve(Reply(503))
        client, policy = self.guarded(server, deny_wait=True)
        with correlated("policy-test"), audited("read", {}, {}, ()) as record:
            result = call("read", lambda: client.get_json("/x"))
        self.assertEqual(
            result["error"]["message"], "Controlled upstream request refused. Retry later."
        )
        self.assertEqual(record.outbound_requests, 1)
        self.assertEqual(len(server.seen), 1)
        self.assertEqual(self.waits, [])

    def test_existing_budget_and_deadline_gate_do_not_record_unsent_attempts(self):
        server = self.serve()
        client, _ = self.guarded(server)
        for scope in (budgeted(Budget(requests=0)), deadline_scope(0)):
            with self.subTest(scope=scope), scope, audited("read", {}, {}, ()) as record:
                result = call("read", lambda: client.get_json("/x"))
                self.assertIn(result["error"]["code"], ("bound_exceeded", "upstream_unavailable"))
                self.assertEqual(record.outbound_requests, 0)
                self.assertEqual(server.seen, [])

    def test_masked_success_and_form_absence_use_existing_consumers(self):
        server = self.serve(Reply(body=b'{"apiResponse":{"type":"E"}}'))
        client, _ = self.guarded(server)
        with self.assertRaises(PlatformError) as caught:
            client.get_json("/x")
        self.assertEqual(caught.exception.code, "upstream_unavailable")
        body = b'{"form":null,"apiResponse":{"type":"E"}}'
        server = self.serve(Reply(body=body))
        path = "/NCIFormAPI.v2_0:NciFormApiRad/Form/5406471"
        policy = BufferedPolicy(server.url + path)
        with using_request_policy(policy):
            cadsr = CaDSRClient(Settings(cadsr_base_url=server.url))
        with self.assertRaises(PlatformError) as caught:
            cadsr.get_form("5406471")
        self.assertEqual(caught.exception.code, "not_found")
        self.assertEqual(caught.exception.details, {"identifiers": ["5406471"]})
        self.assertEqual([row[0] for row in server.seen], [path])

    def test_exchange_copies_headers_and_hides_private_fields(self):
        headers = {"X-Private": "canary"}
        exchange = Exchange(200, headers, b"private content")
        headers["X-Private"] = "changed"
        self.assertEqual(dict(exchange.headers), {"x-private": "canary"})
        self.assertEqual(repr(exchange), "Exchange(status=200, body_state='complete')")
