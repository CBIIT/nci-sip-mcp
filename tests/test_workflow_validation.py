"""Workflow validation identifies fields in the caller's own input record."""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from nci_sip_mcp.config import Settings
from nci_sip_mcp.context import Context
from nci_sip_mcp.registry import invoke


class DictionaryValidationTest(unittest.TestCase):
    def test_invalid_columns_name_public_fields_before_any_upstream_request(self):
        cases = (
            ({}, "columns.name"),
            ({"name": " "}, "columns.name"),
            ({"name": "Q", "description": " "}, "columns.description"),
            ({"name": "Q", "description": None}, "columns.description"),
            ({"name": "Q", "sampleValues": "value"}, "columns.sampleValues"),
            ({"name": "Q", "sampleValues": [" "]}, "columns.sampleValues"),
            ({"name": "Q", "sampleValues": [None]}, "columns.sampleValues"),
        )
        with (
            TemporaryDirectory() as directory,
            patch("nci_sip_mcp.http_client._open", side_effect=AssertionError("Unexpected HTTP")),
        ):
            context = Context(Settings(data_dir=Path(directory)))
            for column, parameter in cases:
                with self.subTest(column=column):
                    result = invoke(
                        context, "harmonize_data_dictionary", columns=[{"name": "Valid"}, column]
                    )
                    self.assertEqual(result["error"]["code"], "invalid_request")
                    self.assertEqual(result["error"]["details"]["parameter"], parameter)
