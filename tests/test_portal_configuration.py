"""Local configuration proposals are revision-bound advice, never writes."""

import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from scripts.portal_configuration import (
    ConfigurationConflictError,
    LocalConfiguration,
    SnapshotInvalidError,
)

from nci_sip_mcp.config import Settings
from nci_sip_mcp.config_snapshot import capture


class PortalConfigurationTest(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "target.json"
        self.snapshot = capture(Settings(timeout_seconds=17), {"NCI_SIP_TIMEOUT_SECONDS"})
        self.path.write_text(json.dumps(self.snapshot))
        self.configuration = LocalConfiguration(self.path)

    def proposal(self, setting="timeout_seconds", value="23", **changes):
        arguments = {
            "instance": self.snapshot["instance"],
            "revision": self.snapshot["revision"],
            "setting": setting,
            "value": value,
        }
        return self.configuration.propose(**(arguments | changes))

    def test_proposal_has_typed_diff_and_changes_no_target_or_environment_state(self):
        before = self.path.read_bytes()
        environment = dict(os.environ)
        proposal = self.proposal()
        self.assertEqual((proposal["before"], proposal["after"]), (17, 23.0))
        self.assertFalse(proposal["applied"])
        self.assertTrue(proposal["restart_required"])
        self.assertEqual(proposal["instance"], self.snapshot["instance"])
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(dict(os.environ), environment)

    def test_old_form_cannot_prepare_a_proposal_for_a_replacement_target(self):
        replacement = capture(Settings(), set())
        self.path.write_text(json.dumps(replacement))
        with self.assertRaises(ConfigurationConflictError):
            self.proposal()
        self.assertEqual(self.configuration.read()["instance"], replacement["instance"])

    def test_stale_revision_is_rejected_even_with_matching_instance(self):
        with self.assertRaises(ConfigurationConflictError):
            self.proposal(revision="0" * 64)

    def test_security_urls_paths_unknown_and_read_only_fields_are_not_proposable(self):
        for name in (
            "http_auth_mode",
            "http_allowed_origins",
            "data_dir",
            "evs_base_url",
            "embedding_provider",
            "invented",
            "transport",
            "release_channel",
        ):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.proposal(setting=name)

    def test_proposals_reuse_numeric_and_closed_value_settings_validation(self):
        for name, value in (
            ("timeout_seconds", "NaN"),
            ("timeout_seconds", "0"),
            ("evs_max_attempts", "1.5"),
            ("evs_max_attempts", "99999"),
            ("log_level", "PRIVATE"),
            ("index_batch_size", "0"),
        ):
            with self.subTest(name=name, value=value), self.assertRaises(ValueError):
                self.proposal(name, value)
        self.assertEqual(self.proposal("evs_max_attempts", "2")["after"], 2)
        self.assertEqual(self.proposal("log_level", "DEBUG")["after"], "DEBUG")

    def test_unreadable_and_malformed_snapshots_fail_apart_without_naming_the_file(self):
        self.path.unlink()
        self.path.mkdir()  # present but unreadable as a file
        with self.assertRaises(OSError) as unreadable:
            self.configuration.read()
        self.path.rmdir()
        self.path.write_text("<html>unavailable</html>")
        with self.assertRaises(SnapshotInvalidError) as malformed:
            self.configuration.read()
        self.assertNotIsInstance(unreadable.exception, SnapshotInvalidError)
        for error in (unreadable.exception, malformed.exception):
            self.assertNotIn(str(self.path), str(error))
            self.assertIsNone(error.__cause__)
            self.assertTrue(error.__suppress_context__)

    def test_unknown_and_malformed_target_configuration_is_not_a_default_snapshot(self):
        with self.assertRaises(OSError):
            LocalConfiguration(None).read()
        self.path.write_text("<html>unavailable</html>")
        with self.assertRaises(OSError):
            self.configuration.read()
        with self.assertRaises(OSError):
            self.proposal()
