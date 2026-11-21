"""The keys of an error's `details` are those of the specification's error record.

spec/records.yaml owns them (`error.detail_keys`); the raise sites and the QUICKSTART table
must name exactly those, so that clients and other servers can rely on them.
"""

import re
import unittest
from collections import defaultdict
from email.message import Message
from io import BytesIO
from typing import get_args
from unittest.mock import patch
from urllib.error import HTTPError

from fakes import terminology_row
from nci_sip_acceptance.spec import RECORDS
from nci_sip_mcp import invocation as invocation_module
from nci_sip_mcp.errors import ErrorCode
from nci_sip_mcp.evs import EVSClient, verify_release
from nci_sip_mcp.permissions import authority_scope
from nci_sip_mcp.registry import invoke
from test_docs import QUICKSTART, section
from test_evs_client import FakeResponse
from test_handlers import NEOPLASM, HandlerTestCase, release

SPEC_KEYS = {code: set(keys) for code, keys in RECORDS["error"]["detail_keys"].items()}
# Codes not exercised by these raise sites; cursor tests cover cursor_expired separately.
UNRAISED = {"cursor_expired"}


def http_error(status, headers=None, body=b""):
    message = Message()
    for name, value in (headers or {}).items():
        message[name] = value
    return HTTPError("https://example.invalid", status, "Reason", message, BytesIO(body))


def from_the_client(answer, call=lambda client: client.get_api_version()):
    """The code and details of the error that an EVS request answered with `answer` raises."""

    with (
        patch("nci_sip_mcp.http_client._open", side_effect=[answer]),
        patch("nci_sip_mcp.http_client.time.sleep"),
    ):
        client = EVSClient("https://example.invalid", max_attempts=1, max_response_bytes=5)
        try:
            call(client)
        except Exception as error:  # noqa: BLE001 - whatever the client raises is the case
            return from_the_exception(error)
    raise AssertionError("the request did not fail")


def from_the_exception(error):
    reported = invocation_module._platform_error(error)
    return reported.code, reported.details


def from_the_record(result):
    record = result["error"]
    return record["code"], record.get("details", {})


class DetailKeysTest(HandlerTestCase):
    def raise_sites(self):
        """(code, details) of an error from each place that raises one with details."""

        with authority_scope(None):
            denied = from_the_record(invoke(self.context, "get_concept", "ncit", "C1"))
        absent_manifest = from_the_record(invoke(self.context, "index_resource", "26.06e"))
        no_index = from_the_record(invoke(self.context, "search", "tumor"))
        self.index()
        self.evs.release = release("26.07d", "2026-07-27")
        self.evs.concepts["C3262"] = dict(NEOPLASM, version="26.07d")
        mismatch = from_the_record(invoke(self.context, "lookup", "C3262"))
        del self.evs.concepts["C40704"]
        return [
            denied,
            absent_manifest,
            no_index,
            from_the_record(invoke(self.context, "search", " ")),
            from_the_record(invoke(self.context, "index_codes", ["C3262", "C40704"])),
            mismatch,
            self.unresolved_release(),
            from_the_client(
                http_error(404, body=b'{"message": "Terminology not found = ncit_9"}'),
                lambda client: client.get_concept("C1", release()),
            ),
            self.verify_release_error(),
            self.missing_exclusions(),
            from_the_client(http_error(429, {"Retry-After": "30"})),
            from_the_client(http_error(403)),
            from_the_client(http_error(404), lambda client: client.get_concept("C1", release())),
            from_the_client(TimeoutError("timed out")),
            from_the_client(FakeResponse(b'{"a": 1}')),
            from_the_client(
                FakeResponse(b"{}", {"Content-Length": "6"}), lambda c: c.get_api_version()
            ),
        ]

    def unresolved_release(self):
        self.evs.rows = [terminology_row(), terminology_row("26.07d")]
        try:
            return from_the_record(invoke(self.context, "lookup", "C3262"))
        finally:
            self.evs.rows = None

    def missing_exclusions(self):
        self.evs.catalogues = {"role": [], "association": []}
        return from_the_record(
            invoke(self.context, "list_relationships", terminology="ncit", release="26.06e")
        )

    def verify_release_error(self):
        try:
            verify_release([{"version": "x"}], "y")
        except Exception as error:  # noqa: BLE001
            return from_the_exception(error)
        raise AssertionError("another release was accepted")

    def test_each_raise_site_puts_the_keys_of_the_specification_in_details(self):
        sites = defaultdict(list)
        for code, details in self.raise_sites():
            sites[code].append(set(details))

        self.assertEqual(set(sites), set(SPEC_KEYS) - UNRAISED)
        for code, cases in sites.items():
            with self.subTest(code):
                self.assertTrue(all(keys <= SPEC_KEYS[code] for keys in cases), cases)
                self.assertEqual(set().union(*cases), SPEC_KEYS[code])


class SpecTest(unittest.TestCase):
    def test_the_specification_names_the_keys_of_every_code(self):
        self.assertEqual(set(SPEC_KEYS), set(get_args(ErrorCode)))

    def test_quickstart_names_the_same_keys_as_the_specification(self):
        rows = {
            re.match(r"\| `(\w+)`", row).group(1): set(re.findall(r"`(\w+)`", row.split("|")[3]))
            for row in section(QUICKSTART, "Errors").splitlines()
            if re.match(r"\| `\w+` \|", row)
        }
        self.assertEqual(rows, SPEC_KEYS)
