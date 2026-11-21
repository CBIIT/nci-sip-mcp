"""Configuration evidence names its actual target and excludes unapproved settings."""

import json
import os
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from nci_sip_mcp.config import PRODUCTION_BASE_URLS, Settings
from nci_sip_mcp.config_snapshot import capture, read_snapshot, serving_snapshot, validate_snapshot


class ConfigurationSnapshotTest(unittest.TestCase):
    def test_capture_time_cannot_be_relabelled_without_invalidating_the_revision(self):
        snapshot = capture(Settings(), set())
        snapshot["captured_at"] = "2000-01-01T00:00:00+00:00"
        with self.assertRaisesRegex(ValueError, "revision mismatch"):
            validate_snapshot(snapshot)

    def test_only_explicit_safe_fields_leave_the_selected_target(self):
        settings = Settings(
            cadsr_credential="PRIVATE:CREDENTIAL",
            evs_license_key="PRIVATE-LICENCE",
            data_dir=Path("PRIVATE-PATH"),
            embedding_model="PRIVATE-MODEL",
            embedding_provider="sentence-transformers",
            timeout_seconds=17,
        )
        snapshot = capture(settings, {"NCI_SIP_TIMEOUT_SECONDS"}, cli_transport=True)
        raw = json.dumps(snapshot)
        self.assertNotIn("PRIVATE", raw)
        self.assertNotIn("credential", raw)
        self.assertNotIn("http_auth", raw)
        self.assertEqual(
            snapshot["settings"]["timeout_seconds"], {"value": 17.0, "origin": "environment"}
        )
        self.assertEqual(snapshot["settings"]["transport"]["origin"], "cli")
        self.assertEqual(snapshot["settings"]["profile"]["origin"], "default")
        self.assertEqual(validate_snapshot(snapshot), snapshot)

    def test_capture_does_not_infer_values_from_the_companion_environment(self):
        with patch.dict(os.environ, {"NCI_SIP_TIMEOUT_SECONDS": "91"}):
            snapshot = capture(Settings(timeout_seconds=12), set())
        self.assertEqual(snapshot["settings"]["timeout_seconds"]["value"], 12.0)

    def test_changed_values_and_changed_target_instances_change_the_revision(self):
        first = capture(Settings(), set())
        second = capture(replace(Settings(), timeout_seconds=21), set())
        third = capture(Settings(), set())
        self.assertNotEqual(first["revision"], second["revision"])
        self.assertNotEqual(first["instance"], third["instance"])
        self.assertNotEqual(first["revision"], third["revision"])

    def test_unknown_fields_and_tampered_values_are_rejected(self):
        for mutation in ("secret", "value", "origin"):
            with self.subTest(mutation=mutation):
                snapshot = capture(Settings(), set())
                if mutation == "secret":
                    snapshot["settings"]["cadsr_credential"] = {"value": "PRIVATE"}
                else:
                    snapshot["settings"]["timeout_seconds"][mutation] = "invalid"
                with self.assertRaises(ValueError):
                    validate_snapshot(snapshot)

    def test_serving_snapshot_uses_exclusive_creation_and_removes_its_own_record(self):
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "target.json"
            with serving_snapshot(path, Settings(), set()) as snapshot:
                self.assertEqual(read_snapshot(path), snapshot)
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                with self.assertRaises(FileExistsError), serving_snapshot(path, Settings(), set()):
                    self.fail("A second target must not overwrite the first")
            self.assertFalse(path.exists())

    def test_shutdown_preserves_a_replacement_snapshot(self):
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "target.json"
            with serving_snapshot(path, Settings(), set()):
                replacement = capture(Settings(timeout_seconds=22), set())
                path.write_text(json.dumps(replacement))
            self.assertEqual(read_snapshot(path), replacement)

    def test_failed_atomic_publication_leaves_no_partial_snapshot_or_scratch(self):
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "target.json"
            with (
                patch("nci_sip_mcp.config_snapshot.os.link", side_effect=OSError("full")),
                self.assertRaises(OSError),
                serving_snapshot(path, Settings(), set()),
            ):
                self.fail("Serving must not start after snapshot publication failure")
            self.assertEqual(list(Path(temporary).iterdir()), [])

    def test_missing_oversized_and_symlink_snapshots_are_not_accepted(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaises(OSError):
                read_snapshot(root / "missing")
            path = root / "target.json"
            path.write_bytes(b" " * 16385)
            with self.assertRaises(ValueError):
                read_snapshot(path)
            link = root / "link"
            link.symlink_to(path)
            with self.assertRaises(ValueError):
                read_snapshot(link)

    def test_invalid_metadata_never_becomes_target_identity(self):
        for name, value in (
            ("schema", True),
            ("schema", 2),
            ("instance", []),
            ("instance", "other"),
            ("revision", "other"),
            ("package_version", "<script>"),
            ("captured_at", None),
            ("captured_at", "x" * 41),
            ("captured_at", "2026-10-08"),
            ("captured_at", "2026-10-08T00:00:00+01:00"),
        ):
            with self.subTest(name=name, value=value):
                snapshot = capture(Settings(), set())
                snapshot[name] = value
                with self.assertRaises(ValueError):
                    validate_snapshot(snapshot)
        with self.assertRaises(ValueError):
            validate_snapshot([])

    def test_invalid_settings_and_unchanged_digest_cannot_hide_tampering(self):
        for name, row in (
            ("timeout_seconds", {"value": True, "origin": "default"}),
            ("timeout_seconds", {"value": 0, "origin": "default"}),
            ("timeout_seconds", {"value": 31, "origin": "default"}),
            ("timeout_seconds", {"value": 30, "origin": "default", "extra": "PRIVATE"}),
            ("upstream_mode", {"value": "invented", "origin": "default"}),
        ):
            with self.subTest(name=name, row=row):
                snapshot = capture(Settings(), set())
                snapshot["settings"][name] = row
                with self.assertRaises(ValueError):
                    validate_snapshot(snapshot)

    def test_fixture_snapshot_requires_no_endpoint_disclosure(self):
        settings = Settings(
            upstream_mode="fixture",
            **dict.fromkeys(PRODUCTION_BASE_URLS, "https://PRIVATE.example"),
        )
        snapshot = capture(settings, {"NCI_SIP_UPSTREAM_MODE"})
        self.assertEqual(snapshot["settings"]["upstream_mode"]["value"], "fixture")
        self.assertNotIn("PRIVATE", json.dumps(snapshot))

    def test_external_removal_and_symlink_replacement_do_not_delete_other_files(self):
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "target.json"
            with serving_snapshot(path, Settings(), set()):
                path.unlink()
            self.assertFalse(path.exists())
            other = Path(temporary) / "other.json"
            with serving_snapshot(path, Settings(), set()):
                other.write_bytes(path.read_bytes())
                path.unlink()
                path.symlink_to(other)
            self.assertTrue(path.is_symlink())
            self.assertTrue(other.exists())
