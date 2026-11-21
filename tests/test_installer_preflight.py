import unittest
from email.message import Message
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError

from nci_sip_mcp import installer

API = installer


class AggregatePrerequisiteTest(unittest.TestCase):
    @staticmethod
    def local_check(name):
        return next(check for check in API.local_prerequisites() if check.name == name)

    def test_every_check_runs_and_failures_keep_static_fixes(self):
        observed = []

        def check(name, passes):
            def observe():
                observed.append(name)
                return passes

            return API.Prerequisite(name, f"fix {name}", observe)

        results = API.run_prerequisites(
            (check("first", False), check("second", True), check("third", False))
        )

        self.assertEqual(observed, ["first", "second", "third"])
        self.assertEqual(
            [API.result_record(result) for result in results],
            [
                {"name": "first", "status": "failed", "fix": "fix first"},
                {"name": "second", "status": "passed"},
                {"name": "third", "status": "failed", "fix": "fix third"},
            ],
        )

    def test_an_observer_bug_propagates_and_stops_later_checks(self):
        observed = []

        def broken():
            raise AssertionError("PRIVATE-CANARY")

        def later():
            observed.append("later")
            return True

        checks = (
            API.Prerequisite("broken", "repair the check", broken),
            API.Prerequisite("later", "fix later", later),
        )

        with self.assertRaisesRegex(AssertionError, "PRIVATE-CANARY"):
            API.run_prerequisites(checks)
        self.assertEqual(observed, [])

    def test_expected_observer_failures_return_false_without_leaking_details(self):
        canary = "PRIVATE-CANARY"
        with (
            patch("nci_sip_mcp.installer.shutil.which", return_value=None),
            patch("nci_sip_mcp.installer.urlopen", side_effect=OSError(canary)),
        ):
            results = API.run_prerequisites(
                (
                    API.executable_prerequisite("pdm", "Install PDM."),
                    API.reachability_prerequisite(
                        "evs_reachable", f"https://example.invalid/{canary}", "Reach EVS."
                    ),
                )
            )

        records = [API.result_record(result) for result in results]
        self.assertEqual(
            records,
            [
                {"name": "pdm", "status": "failed", "fix": "Install PDM."},
                {"name": "evs_reachable", "status": "failed", "fix": "Reach EVS."},
            ],
        )
        self.assertNotIn(canary, repr(records))

    def test_only_darwin_and_linux_are_supported(self):
        os_check = self.local_check("supported_os")
        for system, expected in (("Darwin", True), ("Linux", True), ("Windows", False)):
            with (
                self.subTest(system=system),
                patch("nci_sip_mcp.installer.platform.system", return_value=system),
            ):
                self.assertEqual(os_check.observe(), expected)

    def test_an_http_error_still_proves_endpoint_reachability(self):
        http_error = HTTPError("http://example", 401, "denied", Message(), None)
        with patch("nci_sip_mcp.installer.urlopen", side_effect=http_error):
            self.assertTrue(API.reachability_prerequisite("x", "http://example", "fix").observe())

    def test_incomplete_fixture_configuration_never_falls_back_to_production_urls(self):
        for environment in (
            {"NCI_SIP_UPSTREAM_MODE": "fixture"},
            {"NCI_SIP_EVS_BASE_URL": "file:///etc/hosts"},
            {"NCI_SIP_EVS_BASE_URL": "https://user:secret@example.test?x=1"},
        ):
            with (
                self.subTest(environment=environment),
                patch.dict("os.environ", environment, clear=True),
                self.assertRaisesRegex(ValueError, "NCI_SIP_EVS_BASE_URL"),
            ):
                API.local_prerequisites()

    def test_server_only_has_the_exact_prerequisite_set_and_needs_only_git_executable(self):
        with (
            patch(
                "nci_sip_mcp.installer.shutil.which",
                side_effect=lambda name: "/usr/bin/git" if name == "git" else None,
            ),
            patch("nci_sip_mcp.installer.urlopen"),
        ):
            results = API.run_prerequisites(API.local_prerequisites())

        by_name = {result.name: result for result in results}
        self.assertEqual(
            list(by_name), ["supported_os", "git", "python", "evs_reachable", "cadsr_reachable"]
        )
        self.assertTrue(by_name["git"].passed)
        self.assertNotIn("pdm", by_name)

    def test_python_prerequisite_requires_3_14(self):
        check = self.local_check("python")
        with patch.object(API.sys, "version_info", (3, 13)):
            self.assertFalse(check.observe())
        with patch.object(API.sys, "version_info", (3, 14)):
            self.assertTrue(check.observe())

    def test_reachability_uses_a_five_second_timeout(self):
        response = MagicMock()
        response.__enter__.return_value = response
        with patch("nci_sip_mcp.installer.urlopen", return_value=response) as opened:
            self.assertTrue(API.reachability_prerequisite("x", "https://example", "fix").observe())
        opened.assert_called_once_with("https://example", timeout=5)
