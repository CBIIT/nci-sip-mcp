"""The package version follows installed metadata, and a corrupt first read can be repaired."""

import importlib
import unittest
from importlib.metadata import PackageNotFoundError
from unittest.mock import patch

import nci_sip_mcp

BAD = UnicodeDecodeError("utf-8", b"private-\xff-canary", 8, 9, "invalid start byte")


class PackageVersionTest(unittest.TestCase):
    def setUp(self):
        self.addCleanup(importlib.reload, nci_sip_mcp)

    def reload_with(self, installed):
        with patch("importlib.metadata.version", side_effect=installed):
            importlib.reload(nci_sip_mcp)
            return nci_sip_mcp.__version__

    def test_version_is_the_installed_distribution_version(self):
        self.assertEqual(self.reload_with(lambda name: "1.2.3"), "1.2.3")
        self.assertNotIn("__getattr__", vars(nci_sip_mcp))

    def test_distribution_name_is_nci_sip_mcp(self):
        names = []
        self.reload_with(lambda name: names.append(name) or "1.2.3")

        self.assertEqual(names, ["nci-sip-mcp"])

    def test_a_missing_distribution_fails_the_import(self):
        def missing(name):
            raise PackageNotFoundError(name)

        with self.assertRaises(PackageNotFoundError):
            self.reload_with(missing)

    def test_corrupt_metadata_fails_a_reload_and_leaves_no_stale_version(self):
        self.reload_with(lambda name: "1.2.3")

        with (
            patch("importlib.metadata.version", side_effect=BAD),
            self.assertRaises(UnicodeDecodeError),
        ):
            importlib.reload(nci_sip_mcp)

        self.assertNotIn("__version__", vars(nci_sip_mcp))

    def test_corrupt_first_import_defers_the_error_until_the_version_is_read(self):
        sentinel = object()
        nci_sip_mcp.__dict__.pop("__version__", None)
        with patch("importlib.metadata.version", side_effect=BAD):
            importlib.reload(nci_sip_mcp)
            self.assertIs(vars(nci_sip_mcp).get("__version__", sentinel), sentinel)
            with self.assertRaises(UnicodeDecodeError):
                _ = nci_sip_mcp.__version__
            with self.assertRaisesRegex(AttributeError, "no attribute 'other'"):
                _ = nci_sip_mcp.other

    def test_repair_after_a_corrupt_first_import_restores_ordinary_lookup(self):
        nci_sip_mcp.__dict__.pop("__version__", None)
        with patch("importlib.metadata.version", side_effect=BAD):
            importlib.reload(nci_sip_mcp)
        self.assertEqual(self.reload_with(lambda name: "7.7.7"), "7.7.7")
        del nci_sip_mcp.__version__
        self.assertFalse(hasattr(nci_sip_mcp, "__version__"))
