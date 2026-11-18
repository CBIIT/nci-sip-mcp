"""Callers share an elapsed-time budget, not a fresh timeout for every request."""

import logging
from unittest.mock import patch

from fakes import data_element
from test_cadsr_client import reply
from test_http_client import ServerTestCase
from test_server import ServerFixture


class DeadlineBoundaryTest(ServerFixture, ServerTestCase):
    def test_discovery_and_content_share_the_call_deadline(self):
        """Given a spent budget, a second upstream request never starts."""
        server = self.serve(
            reply({"DataElements": [{"publicId": "123"}]}),
            reply({"DataElement": data_element()}),
        )
        self.context.cadsr.http.base_url = server.url
        now = [0.0]
        self.context.cadsr.http.on_request = lambda record: now.__setitem__(0, 51.0)
        logging.disable(logging.NOTSET)
        with (
            patch("nci_sip_mcp.bounds.monotonic", side_effect=lambda: now[0], create=True),
            self.assertLogs("nci_sip_mcp.audit", level="INFO") as captured,
        ):
            failed, result = self.call("get_data_element", questionText="Patient question")
        self.assertTrue(failed)
        self.assertEqual(result["error"]["code"], "upstream_unavailable")
        self.assertEqual(result["error"]["details"], {"surface": "cadsr", "attempts": 1})
        self.assertTrue(result["error"]["correlationId"])
        self.assertEqual(len(server.seen), 1)
        self.assertEqual(captured.records[-1].structured["outboundRequests"], 1)

    def test_a_fresh_call_can_succeed_after_an_expired_call(self):
        """Given an earlier expiry, the next caller still receives its content."""
        server = self.serve(
            reply({"DataElements": [{"publicId": "123"}]}),
            reply({"DataElement": data_element()}),
            reply({"DataElement": data_element()}),
        )
        self.context.cadsr.http.base_url = server.url
        now = [0.0]
        self.context.cadsr.http.on_request = lambda record: now.__setitem__(0, 51.0)
        with patch("nci_sip_mcp.bounds.monotonic", side_effect=lambda: now[0], create=True):
            failed, _ = self.call("get_data_element", questionText="Patient question")
            self.context.cadsr.http.on_request = None
            allowed, result = self.call("get_data_element", publicId="123")
        self.assertTrue(failed)
        self.assertFalse(allowed)
        self.assertEqual(result["publicId"], "123")
        self.assertEqual(len(server.seen), 2)
