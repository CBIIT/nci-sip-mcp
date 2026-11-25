"""Identity snapshots retain their captured settings without exposing rejected values."""

import json
import os
import unittest
from contextlib import suppress
from importlib.metadata import version
from unittest.mock import patch

from nci_sip_mcp import traffic_identity
from nci_sip_mcp.errors import PlatformError, serialise
from nci_sip_mcp.traffic_identity import (
    INSTALLATION_ID_ENV,
    IdentitySnapshot,
    capture_identity,
    installation_label,
    user_agent,
)


class IdentitySnapshotTest(unittest.TestCase):
    def test_captured_values_cannot_change_the_rendered_label(self):
        with patch.dict(os.environ, {INSTALLATION_ID_ENV: "a" * 32}, clear=True):
            for name, replacement in (("_raw_id", "c" * 32), ("version", "99.0")):
                with self.subTest(field=name):
                    captured = capture_identity()
                    label = installation_label(captured)

                    with suppress(AttributeError):
                        setattr(captured, name, replacement)
                    self.assertEqual(installation_label(captured), label)

                    state = getattr(captured, "__dict__", None)
                    if isinstance(state, dict):
                        state[name] = replacement
                        self.assertEqual(installation_label(captured), label)

    def test_snapshot_does_not_expose_a_captured_private_version(self):
        canary = "PRIVATE VERSION CANARY"
        with (
            patch.dict(os.environ, {INSTALLATION_ID_ENV: "a" * 32}, clear=True),
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
        with patch.dict(os.environ, {INSTALLATION_ID_ENV: "a" * 32}, clear=True):
            with self.assertRaises(PlatformError) as raised:
                installation_label(captured)
            self.assertEqual(
                raised.exception.details,
                {
                    "parameter": INSTALLATION_ID_ENV,
                    "reason": "must be exactly 32 lowercase hexadecimal characters",
                },
            )
            self.assertNotIn(canary, json.dumps(serialise(raised.exception)))
            repaired = installation_label(capture_identity())

        self.assertTrue(repaired.startswith("nci-sip-mcp/"))
        self.assertIn(f"(installation={'a' * 32}; ", repaired)

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


class ProductUserAgentTest(unittest.TestCase):
    def test_the_label_is_the_product_alone_even_when_an_installation_is_configured(self):
        with patch.dict(os.environ, {INSTALLATION_ID_ENV: "a" * 32}, clear=True):
            label = traffic_identity.product_user_agent()

        self.assertEqual(
            label,
            f"nci-sip-mcp/{version('nci-sip-mcp')} (+https://github.com/CBIIT/nci-sip-mcp)",
        )

    def test_an_unsafe_installed_version_is_refused_as_the_server_label_refuses_it(self):
        for unsafe in ("not a version", "1." + "0" * 300):
            with (
                self.subTest(version=unsafe[:20]),
                patch.dict(os.environ, {}, clear=True),
                patch.object(traffic_identity, "version", return_value=unsafe),
            ):
                with self.assertRaises(PlatformError) as product:
                    traffic_identity.product_user_agent()
                with self.assertRaises(PlatformError) as server:
                    user_agent(capture_identity())

                refusal = (product.exception.code, product.exception.message)
                self.assertEqual(refusal, (server.exception.code, server.exception.message))
                self.assertEqual(product.exception.details, server.exception.details)
                self.assertEqual(
                    product.exception.details["parameter"], "nci-sip-mcp installed version"
                )
