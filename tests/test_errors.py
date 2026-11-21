import json
import unittest
from typing import get_args

from nci_sip_mcp.errors import (
    ErrorCode,
    InputValidationError,
    PlatformError,
    correlated,
    is_error_record,
    serialise,
    with_next_step,
)
from nci_sip_mcp.server import INSTRUCTIONS

# The `code` values of the error record in spec/records.yaml.
SPEC_CODES = {
    "invalid_request",
    "permission_denied",
    "not_found",
    "release_not_available",
    "release_mismatch",
    "upstream_unavailable",
    "timeout",
    "bound_exceeded",
    "capability_unavailable",
    "cursor_expired",
    "internal_error",
}


class SerialiseTest(unittest.TestCase):
    def test_the_server_tells_clients_every_code(self):
        for code in get_args(ErrorCode):
            with self.subTest(code):
                self.assertIn(code, INSTRUCTIONS)

    def test_the_codes_are_those_of_the_specification(self):
        self.assertEqual(set(get_args(ErrorCode)), SPEC_CODES)

    def test_every_code_serialises_to_the_error_record(self):
        for code in get_args(ErrorCode):
            with self.subTest(code):
                with correlated("call-1"):
                    record = serialise(PlatformError(code, "It failed. Try again."))

                self.assertEqual(
                    record,
                    {
                        "error": {
                            "code": code,
                            "message": "It failed. Try again.",
                            "correlationId": "call-1",
                        }
                    },
                )
                self.assertTrue(is_error_record(record))

    def test_details_are_carried_when_given(self):
        error = PlatformError("bound_exceeded", "Too large.", bound="b", limit=10, reached=11)

        record = serialise(error)

        self.assertEqual(record["error"]["details"], {"bound": "b", "limit": 10, "reached": 11})
        self.assertEqual(json.loads(json.dumps(record)), record)

    def test_a_validation_error_names_its_parameter_and_reason(self):
        self.assertEqual(
            InputValidationError("limit is too low", "limit").details,
            {"parameter": "limit", "reason": "limit is too low"},
        )
        self.assertEqual(InputValidationError("bad").details, {})

    def test_a_result_is_not_an_error_record(self):
        for result in ({}, {"hits": []}, {"error": 1, "hits": []}, {"edges": [], "nodes": []}):
            with self.subTest(result=result):
                self.assertFalse(is_error_record(result))

    def test_a_next_step_follows_the_message_after_one_full_stop(self):
        self.assertEqual(with_next_step("It failed", "Retry."), "It failed. Retry.")
        self.assertEqual(with_next_step("It failed.", "Retry."), "It failed. Retry.")


class CorrelationTest(unittest.TestCase):
    def identifier(self):
        return serialise(PlatformError("internal_error", "x"))["error"]["correlationId"]

    def test_the_caller_s_identifier_is_returned(self):
        with correlated("caller-7") as value:
            self.assertEqual((value, self.identifier()), ("caller-7", "caller-7"))

    def test_an_identifier_is_generated_once_per_call(self):
        with correlated() as value:
            self.assertTrue(value)
            self.assertEqual(self.identifier(), value)
            self.assertEqual(self.identifier(), value)
        with correlated() as other:
            self.assertNotEqual(other, value)

    def test_a_value_that_is_not_a_non_empty_string_is_replaced(self):
        for given in ("", None, 7, ["x"]):
            with self.subTest(given=given), correlated(given) as value:
                self.assertIsInstance(value, str)
                self.assertTrue(value)
                self.assertEqual(self.identifier(), value)

    def test_the_identifier_ends_with_the_call(self):
        with correlated("inside"):
            pass

        self.assertNotEqual(self.identifier(), "inside")

    def test_failures_outside_a_call_each_get_their_own_identifier(self):
        first, second = self.identifier(), self.identifier()

        self.assertTrue(first)
        self.assertTrue(second)
        self.assertNotEqual(first, second)

    def test_a_failure_outside_a_call_still_gets_one(self):
        self.assertTrue(self.identifier())
