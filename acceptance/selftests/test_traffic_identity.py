"""Acceptance identity snapshots retain captured values without importing the core package."""

import os
import unittest
from unittest.mock import patch

from nci_si_acceptance import traffic_identity
from nci_si_acceptance.traffic_identity import (
    INSTALLATION_ID_ENV,
    IdentitySnapshot,
    capture_identity,
    installation_label,
)


class AcceptanceIdentitySnapshotTest(unittest.TestCase):
    def test_snapshot_does_not_expose_a_captured_private_version(self):
        canary = "PRIVATE VERSION CANARY"
        with (
            patch.dict(os.environ, {INSTALLATION_ID_ENV: "b" * 32}, clear=True),
            patch.object(traffic_identity, "version", return_value=canary),
        ):
            captured = capture_identity()

        self.assertIsInstance(captured, IdentitySnapshot)
        self.assertNotIn(canary, repr(captured))

    def test_environment_repair_does_not_change_a_captured_invalid_value(self):
        canary = "PRIVATE-ID-CANARY"
        with patch.dict(os.environ, {INSTALLATION_ID_ENV: canary}, clear=True):
            captured = capture_identity()

        self.assertIsInstance(captured, IdentitySnapshot)
        self.assertNotIn(canary, repr(captured))
        with patch.dict(os.environ, {INSTALLATION_ID_ENV: "b" * 32}, clear=True):
            with self.assertRaises(ValueError) as raised:
                installation_label(captured)
            self.assertEqual(
                str(raised.exception),
                "Set NCI_SI_ACCEPTANCE_INSTALLATION_ID to exactly 32 lowercase hexadecimal "
                "characters and retry.",
            )
            self.assertNotIn(canary, str(raised.exception))
            repaired = installation_label(capture_identity())

        self.assertTrue(repaired.startswith("nci-si-acceptance/"))
        self.assertIn(f"(installation={'b' * 32}; ", repaired)

    def test_new_snapshot_observes_rotation_without_changing_the_old_label(self):
        first_id = "a" * 32
        rotated_id = "b" * 32
        with patch.dict(os.environ, {INSTALLATION_ID_ENV: first_id}, clear=True):
            captured = capture_identity()
        with patch.dict(os.environ, {INSTALLATION_ID_ENV: rotated_id}, clear=True):
            rotated = capture_identity()

        first_label = installation_label(captured)
        rotated_label = installation_label(rotated)
        self.assertIn(f"(installation={first_id}; ", first_label)
        self.assertIn(f"(installation={rotated_id}; ", rotated_label)
        self.assertNotEqual(first_label, rotated_label)
