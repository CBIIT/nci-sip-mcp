import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from nci_sip_mcp import installer_state

API = installer_state


class InstallerStateTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.data_dir = Path(directory.name)
        self.path = self.data_dir / "installer-state.json"
        self.state = API.InstallationState(
            version=2,
            target="local",
            components=("server",),
            outcomes=(API.StepOutcome("check_prerequisites", "completed"),),
        )

    @staticmethod
    def encoded(**changes):
        value = {
            "version": 2,
            "target": "local",
            "components": ["server"],
            "transport": "stdio",
            "profile": "unified",
            "outcomes": [],
        }
        value.update(changes)
        return json.dumps(value, separators=(",", ":")).encode()

    def test_state_round_trips_through_the_closed_versioned_json_record(self):
        API.write_state(self.data_dir, self.state)
        self.assertEqual(API.read_state(self.data_dir, "local", ("server",)), self.state)
        expected = {
            "version": 2,
            "target": "local",
            "components": ["server"],
            "transport": "stdio",
            "profile": "unified",
        }
        expected["outcomes"] = [{"step": "check_prerequisites", "status": "completed"}]
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), expected)

    def test_failed_atomic_replace_preserves_the_previous_state(self):
        original = b'{"old":"state"}\n'
        self.path.write_bytes(original)
        with (
            patch("nci_sip_mcp.installer_state.os.replace", side_effect=OSError("PRIVATE-CANARY")),
            self.assertRaisesRegex(API.InstallerStateError, "Cannot write state"),
        ):
            API.write_state(self.data_dir, self.state)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(list(self.data_dir.glob(".installer-state.*.tmp")), [])

    def test_state_write_validates_before_replacing_previous_state(self):
        original = b'{"old":"state"}\n'
        self.path.write_bytes(original)
        invalid = (
            API.InstallationState(3, "local", ("server",), ()),
            API.InstallationState(
                2, "local", ("server",), (API.StepOutcome("check_prerequisites", "blocked", ("",)),)
            ),
        )
        for state in invalid:
            with self.assertRaisesRegex(API.InstallerStateError, "malformed"):
                API.write_state(self.data_dir, state)
        self.assertEqual(self.path.read_bytes(), original)

    def test_malformed_or_unsupported_state_fails_closed_without_rewriting(self):
        cases = (
            (b"{", "malformed"),
            (
                b'{"version":1,"target":"local","components":["server"],"outcomes":[]}',
                "unsupported version",
            ),
            (self.encoded(version=3), "unsupported version"),
            (self.encoded(extra=1), "malformed"),
            (self.encoded(target="aws"), "malformed"),
            (self.encoded(components=["server", "index"]), "malformed"),
        )
        for content, message in cases:
            with self.subTest(content=content):
                self.path.write_bytes(content)
                with self.assertRaisesRegex(API.InstallerStateError, message) as error:
                    API.read_state(self.data_dir, "local", ("server",))
                self.assertEqual(self.path.read_bytes(), content)
                self.assertNotIn(content.decode(errors="ignore"), str(error.exception))

    def test_profile_alone_is_part_of_the_installation_identity(self):
        state = API.InstallationState(2, "local", ("server",), (), profile="evs")
        API.write_state(self.data_dir, state)
        before = self.path.read_bytes()
        with self.assertRaisesRegex(API.InstallerStateError, "another selection"):
            API.read_state(self.data_dir, "local", ("server",), profile="cadsr")
        self.assertEqual(self.path.read_bytes(), before)

    def test_components_are_decoded_only_from_a_json_list(self):
        content = self.encoded(components="index")
        self.path.write_bytes(content)
        with (
            patch.object(API, "_valid_identity", return_value=True),
            self.assertRaisesRegex(API.InstallerStateError, "malformed"),
        ):
            API.read_state(self.data_dir, "local", ("server",))
        self.assertEqual(self.path.read_bytes(), content)

    def test_unreadable_state_names_the_file_and_access_next_step(self):
        self.path.write_text("{}", encoding="utf-8")
        with (
            patch.object(Path, "read_text", side_effect=PermissionError("PRIVATE-CANARY")),
            self.assertRaisesRegex(API.InstallerStateError, str(self.path)) as error,
        ):
            API.read_state(self.data_dir, "local", ("server",))
        self.assertIn("Fix access", str(error.exception))
        self.assertNotIn("PRIVATE-CANARY", str(error.exception))
        self.path.unlink()
        self.path.symlink_to(self.data_dir / "missing-target")
        with self.assertRaisesRegex(API.InstallerStateError, "Cannot read"):
            API.read_state(self.data_dir, "local", ("server",))

    def assert_invalid_state(self, cases):
        for content in cases:
            with self.subTest(content=content):
                self.path.write_bytes(content)
                with self.assertRaisesRegex(API.InstallerStateError, "malformed"):
                    state = API.read_state(self.data_dir, "local", ("server",))
                    API.validate_outcomes(state, ("check_prerequisites",))
                self.assertEqual(self.path.read_bytes(), content)

    def test_invalid_state_identity_is_refused_without_changing_bytes(self):
        cases = (
            b"\xff\xfe",
            b'{"version":'
            + (b"1" * 4301)
            + b',"target":"local","components":["server"],"outcomes":[]}',
            self.encoded(version=True),
            b'{"version":2,"version":2,"target":"local","components":["server"],"transport":"stdio","profile":"unified","outcomes":[]}',
            self.encoded(components=["server", "server"]),
            self.encoded(components=["alien"]),
            self.encoded(components="server"),
            self.encoded(components="index"),
            self.encoded(components=[]),
            self.encoded(transport="tcp"),
            self.encoded(profile="other"),
        )
        self.assert_invalid_state(cases)

    def test_invalid_outcome_records_are_refused_without_changing_bytes(self):
        completed = {"step": "check_prerequisites", "status": "completed"}
        cases = (
            self.encoded(outcomes=[{"step": "unknown", "status": "completed"}]),
            self.encoded(outcomes=[completed | {"extra": 1}]),
            self.encoded(outcomes=[completed, completed]),
            self.encoded(outcomes=[{"step": "check_prerequisites", "status": "blocked"}]),
            self.encoded(
                outcomes=[{"step": "check_prerequisites", "status": "blocked", "missing": [""]}]
            ),
            self.encoded(outcomes=[completed | {"missing": ["x"]}]),
        )
        self.assert_invalid_state(cases)

    def test_atomic_state_write_fsyncs_and_closes_the_parent_directory(self):
        events, opened_descriptors = [], []
        real_open = API.os.open

        def open_directory(*args):
            descriptor = real_open(*args)
            opened_descriptors.append(descriptor)
            return descriptor

        with (
            patch("nci_sip_mcp.installer_state.os.open", side_effect=open_directory) as opened,
            patch(
                "nci_sip_mcp.installer_state.os.fsync",
                side_effect=lambda fd: events.append(("fsync", fd)),
            ),
            patch(
                "nci_sip_mcp.installer_state.os.replace",
                side_effect=lambda *_: events.append(("replace",)),
            ),
            patch("nci_sip_mcp.installer_state.os.close", wraps=API.os.close) as closed,
        ):
            API.write_state(self.data_dir, self.state)
        opened.assert_any_call(self.data_dir, API.os.O_RDONLY)
        directory_descriptor = opened_descriptors[-1]
        self.assertEqual([event[0] for event in events], ["fsync", "replace", "fsync"])
        self.assertEqual(events[-1], ("fsync", directory_descriptor))
        closed.assert_called_once_with(directory_descriptor)

    def test_a_second_owner_cannot_acquire_the_installation_lock(self):
        with (
            API.state_lock(self.data_dir),
            self.assertRaisesRegex(API.InstallerStateError, "already running"),
            API.state_lock(self.data_dir),
        ):
            self.fail("second lock acquired")

    def test_installation_lock_access_failures_are_operator_refusals(self):
        file_path = self.data_dir / "not-a-directory"
        file_path.write_text("x", encoding="utf-8")
        with (
            self.assertRaisesRegex(API.InstallerStateError, "fix access"),
            API.state_lock(file_path),
        ):
            self.fail("lock acquired")
