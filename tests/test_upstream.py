import json
import unittest

from nci_sip_mcp.errors import PlatformError
from nci_sip_mcp.upstream import parse_upstream_json

OUTCOME = {"resourceType": "OperationOutcome", "issue": [{"severity": "error", "code": "x"}]}


def body(value):
    return json.dumps(value).encode()


class ParseUpstreamJsonTest(unittest.TestCase):
    def details(self, payload):
        with self.assertRaises(PlatformError) as raised:
            parse_upstream_json(payload, "EVS /x")
        return raised.exception.details

    def failure(self, payload):
        with self.assertRaises(PlatformError) as raised:
            parse_upstream_json(payload, "EVS /api/v1/version")
        self.assertEqual(raised.exception.code, "upstream_unavailable")
        return raised.exception.message

    def test_content_is_returned_parsed(self):
        for value in ({"version": "1"}, [], [{"code": "C1"}], "text", None):
            with self.subTest(value=value):
                self.assertEqual(parse_upstream_json(body(value), "EVS /x"), value)

    def test_an_empty_result_is_content_not_a_failure(self):
        self.assertEqual(parse_upstream_json(b"[]", "EVS /x"), [])
        self.assertEqual(parse_upstream_json(b'{"concepts": []}', "EVS /x"), {"concepts": []})

    def test_html_where_json_was_asked_for(self):
        for page in (b"<html></html>", b"  \n<!DOCTYPE html><title>x</title>"):
            with self.subTest(page=page):
                self.assertIn("an HTML page", self.failure(page))

    def test_text_that_is_not_json(self):
        for payload in (b"", b"not json", b"\xff\xfe\x00"):
            with self.subTest(payload=payload):
                self.assertIn("invalid JSON", self.failure(payload))

    def test_a_webmethods_error_envelope_uses_server_owned_wording(self):
        envelope = {"DataElement": None, "apiResponse": {"type": "E", "message": "bad id"}}

        self.assertEqual(
            self.failure(body(envelope)),
            "EVS /api/v1/version answered with a webMethods error envelope in a success response. "
            "Retry later; if it persists, check that the platform's base URL is the right one.",
        )
        self.assertEqual(self.details(body(envelope)), {"surface": "evs"})

    def test_a_webmethods_envelope_that_is_not_an_error_is_content(self):
        for kind in ("S", "W", None):
            with self.subTest(kind=kind):
                payload = {"apiResponse": {"type": kind}, "items": []}

                self.assertEqual(parse_upstream_json(body(payload), "x"), payload)

    def test_an_issue_list_is_an_error_only_in_an_operation_outcome(self):
        for payload in (
            {"issue": [{"severity": "error"}]},
            {"resourceType": "Bundle", "issue": [{"severity": "error"}]},
            {**OUTCOME, "issue": [{"severity": "information"}]},
        ):
            with self.subTest(payload=payload):
                self.assertEqual(parse_upstream_json(body(payload), "x"), payload)

    def test_a_fhir_operation_outcome_with_an_error_or_fatal_issue(self):
        for severity in ("error", "fatal"):
            with self.subTest(severity=severity):
                issues = [{"severity": "information"}, {"severity": severity}]
                outcome = {**OUTCOME, "issue": issues}
                self.assertIn("OperationOutcome", self.failure(body(outcome)))

    def test_malformed_sibling_severity_does_not_hide_a_fhir_error(self):
        for malformed in ([], {}, ["error"], {"severity": "fatal"}):
            with self.subTest(severity=malformed):
                malformed_issue = {"severity": malformed}
                for severity in ("error", "fatal"):
                    outcome = {**OUTCOME, "issue": [malformed_issue, {"severity": severity}]}
                    self.assertIn("OperationOutcome", self.failure(body(outcome)))
                without_error = {**OUTCOME, "issue": [malformed_issue]}
                self.assertEqual(parse_upstream_json(body(without_error), "EVS /x"), without_error)

    def test_a_fhir_operation_outcome_without_an_error_is_content(self):
        for issues in ([{"severity": "warning"}], [], "oops", [7]):
            with self.subTest(issues=issues):
                outcome = {**OUTCOME, "issue": issues}
                self.assertEqual(parse_upstream_json(body(outcome), "x"), outcome)

    def test_the_message_names_the_source_and_the_next_step(self):
        message = self.failure(b"<html>")
        self.assertEqual(self.details(b"<html>"), {"surface": "evs"})

        self.assertIn("EVS /api/v1/version", message)
        self.assertIn("Retry later", message)
