import logging
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from nci_sip_mcp.config import (
    DEFAULT_EVS_BASE_URL,
    DEFAULT_EXCLUSION_ROLE_CODES,
    PRODUCTION_BASE_URLS,
    Settings,
    configure_logging,
)


def settings_from(**environment):
    with patch.dict(os.environ, environment, clear=True):
        return Settings.from_env()


class SettingsTest(unittest.TestCase):
    def test_http_settings_select_sessions_limits_and_public_authorities(self):
        settings = settings_from(
            NCI_SIP_TRANSPORT="streamable-http",
            NCI_SIP_HTTP_SESSIONS="stateless",
            NCI_SIP_HTTP_HOST="::1",
            NCI_SIP_HTTP_PORT="8080",
            NCI_SIP_HTTP_MAX_REQUEST_BYTES="512",
            NCI_SIP_HTTP_ALLOWED_HOSTS="service.example:443,localhost:*",
            NCI_SIP_HTTP_ALLOWED_ORIGINS="https://service.example",
            NCI_SIP_HTTP_REQUIRE_INDEX="1",
        )
        self.assertEqual(
            (settings.transport, settings.http_sessions), ("streamable-http", "stateless")
        )
        self.assertEqual((settings.http_host, settings.http_port), ("::1", 8080))
        self.assertEqual(settings.http_max_request_bytes, 512)
        self.assertIs(settings.http_require_index, True)
        self.assertEqual(settings.http_allowed_hosts, ("service.example:443", "localhost:*"))
        self.assertEqual(settings.http_allowed_origins, ("https://service.example",))

    def test_invalid_http_configuration_is_rejected_at_startup(self):
        for variable, value in (
            ("NCI_SIP_TRANSPORT", "websocket"),
            ("NCI_SIP_HTTP_SESSIONS", "shared"),
            ("NCI_SIP_HTTP_HOST", "http://example"),
            ("NCI_SIP_HTTP_PORT", "0"),
            ("NCI_SIP_HTTP_PORT", "65536"),
            ("NCI_SIP_HTTP_MAX_REQUEST_BYTES", "0"),
            ("NCI_SIP_HTTP_REQUIRE_INDEX", "2"),
            ("NCI_SIP_HTTP_ALLOWED_HOSTS", ""),
            ("NCI_SIP_HTTP_ALLOWED_HOSTS", "*"),
            ("NCI_SIP_HTTP_ALLOWED_HOSTS", "host/path"),
            ("NCI_SIP_HTTP_ALLOWED_HOSTS", "user:secret@host"),
            ("NCI_SIP_HTTP_ALLOWED_ORIGINS", "https://host/path"),
        ):
            with (
                self.subTest(variable=variable, value=value),
                self.assertRaisesRegex(ValueError, variable),
            ):
                settings_from(**{variable: value})

    def test_defaults(self):
        settings = settings_from()

        self.assertEqual(settings, Settings())
        self.assertEqual(settings.data_dir, Path(".nci-si-mcp"))

    def test_legacy_namespace_is_not_an_environment_alias(self):
        settings = settings_from(NCI_SI_DATA_DIR="/legacy/index")

        self.assertEqual(settings.data_dir, Path(".nci-si-mcp"))

    def test_environment_overrides(self):
        settings = settings_from(
            NCI_SIP_EVS_BASE_URL="http://localhost:8080/",
            NCI_SIP_DATA_DIR="/data/index",
            NCI_SIP_TIMEOUT_SECONDS="2.5",
            NCI_SIP_EVS_MAX_ATTEMPTS="1",
            NCI_SIP_INDEX_BATCH_SIZE="7",
            NCI_SIP_LOG_LEVEL="debug",
            NCI_SIP_EMBEDDING_PROVIDER="sentence-transformers",
            NCI_SIP_EMBEDDING_MODEL="all-MiniLM-L6-v2",
        )

        self.assertEqual(settings.evs_base_url, "http://localhost:8080")
        self.assertEqual(settings.data_dir, Path("/data/index"))
        self.assertEqual(settings.timeout_seconds, 2.5)
        self.assertEqual(settings.evs_max_attempts, 1)
        self.assertEqual(settings.index_batch_size, 7)
        self.assertEqual(settings.log_level, "DEBUG")
        self.assertEqual(settings.embedding_provider, "sentence-transformers")
        self.assertEqual(settings.embedding_model, "all-MiniLM-L6-v2")

    def test_each_invalid_value_names_its_variable(self):
        for variable, value in (
            ("NCI_SIP_EVS_BASE_URL", "api-evsrest.nci.nih.gov"),
            ("NCI_SIP_EVS_BASE_URL", "file:///etc/passwd"),
            ("NCI_SIP_EVS_BASE_URL", "file://localhost/etc/passwd"),
            ("NCI_SIP_EVS_BASE_URL", "ftp://example.org"),
            ("NCI_SIP_EVS_BASE_URL", "https:///path"),
            ("NCI_SIP_EVS_BASE_URL", "http://"),
            ("NCI_SIP_EVS_BASE_URL", "https://example.org:abc"),
            ("NCI_SIP_EVS_BASE_URL", "https://example.org?x=1"),
            ("NCI_SIP_EVS_BASE_URL", "https://example.org/api#top"),
            ("NCI_SIP_EVS_BASE_URL", "https://exa mple.org"),
            ("NCI_SIP_EVS_BASE_URL", "https://api-evsrest..nci.nih.gov"),
            ("NCI_SIP_EVS_BASE_URL", "https://.example.org"),
            ("NCI_SIP_EVS_BASE_URL", "https://" + "a" * 64 + ".org"),
            ("NCI_SIP_EVS_BASE_URL", "https://example.org\r"),
            ("NCI_SIP_EVS_BASE_URL", "https://user:secret@example.org"),
            ("NCI_SIP_EVS_BASE_URL", "https://example.org/pr\u00e4"),
            ("NCI_SIP_EVS_BASE_URL", "https://example.org:0"),
            ("NCI_SIP_EVS_BASE_URL", "https://example.org?"),
            ("NCI_SIP_EVS_BASE_URL", "https://example.org/api#"),
            ("NCI_SIP_DATA_DIR", "~no_such_user_xyz/data"),
            ("NCI_SIP_EVS_MAX_RESPONSE_BYTES", str(1024**3 + 1)),
            ("NCI_SIP_DATA_DIR", ""),
            ("NCI_SIP_DATA_DIR", "  "),
            ("NCI_SIP_EVS_MAX_ATTEMPTS", "11"),
            ("NCI_SIP_EVS_BASE_URL", ""),
            ("NCI_SIP_TIMEOUT_SECONDS", "0"),
            ("NCI_SIP_TIMEOUT_SECONDS", "nan"),
            ("NCI_SIP_TIMEOUT_SECONDS", "inf"),
            ("NCI_SIP_TIMEOUT_SECONDS", "1e300"),
            ("NCI_SIP_TIMEOUT_SECONDS", "soon"),
            ("NCI_SIP_EVS_MAX_ATTEMPTS", "0"),
            ("NCI_SIP_EVS_MAX_ATTEMPTS", "three"),
            ("NCI_SIP_EVS_MAX_ATTEMPTS", "3.0"),
            ("NCI_SIP_EVS_RETRY_BACKOFF_SECONDS", "-1"),
            ("NCI_SIP_EVS_RETRY_BACKOFF_SECONDS", "nan"),
            ("NCI_SIP_EVS_RETRY_BACKOFF_SECONDS", "inf"),
            ("NCI_SIP_EMBEDDING_PROVIDER", "bogus"),
            ("NCI_SIP_EMBEDDING_MODEL", "all-MiniLM-L6-v2"),
            ("NCI_SIP_EVS_MAX_RESPONSE_BYTES", "0"),
            ("NCI_SIP_INDEX_BATCH_SIZE", "0"),
            ("NCI_SIP_LOG_LEVEL", "loud"),
        ):
            with self.subTest(variable=variable, value=value):
                with self.assertRaises(ValueError) as raised:
                    settings_from(**{variable: value})
                self.assertIn(variable, str(raised.exception))
                if (variable, value) in {
                    ("NCI_SIP_TIMEOUT_SECONDS", "soon"),
                    ("NCI_SIP_EVS_MAX_ATTEMPTS", "three"),
                    ("NCI_SIP_EVS_MAX_ATTEMPTS", "3.0"),
                }:
                    kind = "a number" if variable == "NCI_SIP_TIMEOUT_SECONDS" else "an integer"
                    self.assertEqual(
                        str(raised.exception), f"{variable} must be {kind}, not {value!r}"
                    )

    def test_bounds_are_inclusive(self):
        settings = settings_from(
            NCI_SIP_EVS_MAX_ATTEMPTS="10",
            NCI_SIP_EVS_RETRY_BACKOFF_SECONDS="0",
            NCI_SIP_TIMEOUT_SECONDS="3600",
            NCI_SIP_EVS_MAX_RESPONSE_BYTES=str(1024**3),
        )

        self.assertEqual(
            (
                settings.evs_max_attempts,
                settings.evs_retry_backoff_seconds,
                settings.timeout_seconds,
                settings.evs_max_response_bytes,
            ),
            (10, 0.0, 3600.0, 1024**3),
        )

    def test_usable_base_urls_are_accepted(self):
        for url in (
            "https://api-evsrest.nci.nih.gov",
            "HTTPS://EXAMPLE.ORG",
            "http://localhost:8080/prefix/",
            "http://127.0.0.1:9",
            "http://[::1]:8080",
            "https://example.org.",
        ):
            with self.subTest(url):
                self.assertEqual(
                    settings_from(NCI_SIP_EVS_BASE_URL=url).evs_base_url, url.rstrip("/")
                )

    def test_data_dir_expands_the_home_directory(self):
        settings = settings_from(NCI_SIP_DATA_DIR="~/nci-index", HOME="/home/someone")

        self.assertEqual(settings.data_dir, Path("/home/someone/nci-index"))


BASE_URL_VARIABLES = (
    "NCI_SIP_EVS_BASE_URL",
    "NCI_SIP_EVS_FHIR_BASE_URL",
    "NCI_SIP_CADSR_BASE_URL",
    "NCI_SIP_CADSR_FTP_URL",
    "NCI_SIP_SSIS_SPARQL_URL",
)
FIXTURE_URLS = {name: f"http://127.0.0.1:9/{name[7:].lower()}" for name in BASE_URL_VARIABLES}
CREDENTIAL = "reviewer:hunter2-secret"
LICENCE_KEY = "licence-key-4711"


class ProfileAndChannelTest(unittest.TestCase):
    def test_each_closed_value_is_accepted(self):
        for variable, values in (
            ("NCI_SIP_PROFILE", ("evs", "cadsr", "unified")),
            ("NCI_SIP_RELEASE_CHANNEL", ("monthly", "weekly")),
        ):
            for value in values:
                with self.subTest(variable=variable, value=value):
                    settings = settings_from(**{variable: value})
                    self.assertIn(value, (settings.profile, settings.release_channel))

    def test_a_value_outside_the_closed_set_names_the_variable(self):
        for variable, value in (
            ("NCI_SIP_PROFILE", "everything"),
            ("NCI_SIP_PROFILE", "EVS"),
            ("NCI_SIP_PROFILE", ""),
            ("NCI_SIP_UPSTREAM_MODE", "mock"),
            ("NCI_SIP_RELEASE_CHANNEL", "daily"),
            ("NCI_SIP_RELEASE_CHANNEL", ""),
        ):
            with self.subTest(variable=variable, value=value):
                with self.assertRaises(ValueError) as raised:
                    settings_from(**{variable: value})
                self.assertIn(variable, str(raised.exception))

    def test_match_timeout_is_validated_like_the_timeout(self):
        self.assertEqual(
            settings_from(NCI_SIP_MATCH_TIMEOUT_SECONDS="60").match_timeout_seconds, 60
        )
        for value in ("0", "-1", "nan", "3601", "soon"):
            with self.subTest(value):
                with self.assertRaises(ValueError) as raised:
                    settings_from(NCI_SIP_MATCH_TIMEOUT_SECONDS=value)
                self.assertIn("NCI_SIP_MATCH_TIMEOUT_SECONDS", str(raised.exception))

    def test_exclusion_role_codes_are_a_list_of_role_codes(self):
        settings = settings_from(NCI_SIP_EXCLUSION_ROLE_CODES="R135, R201")

        self.assertEqual(settings.exclusion_role_codes, ("R135", "R201"))
        for value in ("", "R135,", "R135,C12", "r135", "R", "135", "R135;R136"):
            with self.subTest(value):
                with self.assertRaises(ValueError) as raised:
                    settings_from(NCI_SIP_EXCLUSION_ROLE_CODES=value)
                self.assertIn("NCI_SIP_EXCLUSION_ROLE_CODES", str(raised.exception))


class UpstreamUrlSetTest(unittest.TestCase):
    def test_live_mode_gives_each_unset_url_its_production_default(self):
        settings = settings_from()

        self.assertEqual(settings.evs_base_url, DEFAULT_EVS_BASE_URL)
        self.assertEqual(settings.ssis_sparql_url, "https://shared.semantics.cancer.gov")
        self.assertEqual(settings.evs_fhir_base_url, "https://api-evsrest.nci.nih.gov/fhir/r4")
        self.assertEqual(settings.cadsr_base_url, "https://cadsrapi.cancer.gov/rad")
        self.assertEqual(settings.cadsr_ftp_url, "https://cadsr.nci.nih.gov/ftp/caDSR_Downloads")

    def test_a_url_given_in_live_mode_replaces_its_default_only(self):
        settings = settings_from(
            NCI_SIP_SSIS_SPARQL_URL="http://localhost:8080/", NCI_SIP_UPSTREAM_MODE="live"
        )

        self.assertEqual(settings.ssis_sparql_url, "http://localhost:8080")
        for name, default in PRODUCTION_BASE_URLS.items():
            if name != "ssis_sparql_url":
                self.assertEqual(getattr(settings, name), default)

    def test_fixture_mode_takes_exactly_the_urls_given(self):
        settings = settings_from(NCI_SIP_UPSTREAM_MODE="fixture", **FIXTURE_URLS)

        self.assertEqual(settings.upstream_mode, "fixture")
        self.assertEqual(settings.evs_base_url, FIXTURE_URLS["NCI_SIP_EVS_BASE_URL"])
        self.assertEqual(settings.cadsr_ftp_url, FIXTURE_URLS["NCI_SIP_CADSR_FTP_URL"])
        self.assertEqual(settings.ssis_sparql_url, FIXTURE_URLS["NCI_SIP_SSIS_SPARQL_URL"])

    def test_fixture_mode_without_a_url_fails_naming_it(self):
        for missing in BASE_URL_VARIABLES:
            with self.subTest(missing):
                given = {name: url for name, url in FIXTURE_URLS.items() if name != missing}
                with self.assertRaises(ValueError) as raised:
                    settings_from(NCI_SIP_UPSTREAM_MODE="fixture", **given)
                self.assertIn(missing, str(raised.exception))

    def test_every_url_variable_is_validated(self):
        for variable in BASE_URL_VARIABLES:
            for value in (
                "not a url",
                "ftp://example.org",
                "https://user:pw@example.org",
                "https://x?y=1",
            ):
                with self.subTest(variable=variable, value=value):
                    with self.assertRaises(ValueError) as raised:
                        settings_from(**{variable: value})
                    self.assertIn(variable, str(raised.exception))
                    self.assertNotIn(value, str(raised.exception))


class SecretsTest(unittest.TestCase):
    def test_secrets_are_loaded(self):
        settings = settings_from(
            NCI_SIP_EVS_LICENSE_KEY=LICENCE_KEY, NCI_SIP_CADSR_CREDENTIAL=CREDENTIAL
        )

        self.assertEqual(
            (settings.evs_license_key, settings.cadsr_credential), (LICENCE_KEY, CREDENTIAL)
        )

    def test_no_string_form_of_the_settings_shows_a_secret(self):
        settings = settings_from(
            NCI_SIP_EVS_LICENSE_KEY=LICENCE_KEY, NCI_SIP_CADSR_CREDENTIAL=CREDENTIAL
        )

        for text in (repr(settings), str(settings), f"{settings}", repr([settings])):
            self.assertNotIn(LICENCE_KEY, text)
            self.assertNotIn("hunter2", text)
        self.assertIn("evs_base_url", repr(settings))

    def test_an_invalid_secret_is_rejected_without_its_value(self):
        for variable, value in (
            ("NCI_SIP_CADSR_CREDENTIAL", "nopasswordsecret"),
            ("NCI_SIP_CADSR_CREDENTIAL", ":secret-only"),
            ("NCI_SIP_CADSR_CREDENTIAL", "user-only-secret:"),
            ("NCI_SIP_CADSR_CREDENTIAL", "  "),
            ("NCI_SIP_EVS_LICENSE_KEY", "two part-secret"),
            ("NCI_SIP_EVS_LICENSE_KEY", "keyä-secret"),
            ("NCI_SIP_EVS_LICENSE_KEY", ""),
        ):
            with self.subTest(variable=variable, value=value):
                with self.assertRaises(ValueError) as raised:
                    settings_from(**{variable: value})
                self.assertIn(variable, str(raised.exception))
                self.assertNotIn("secret", str(raised.exception))

    def test_a_secret_too_short_to_redact_safely_is_rejected(self):
        # A short secret becomes a global substring replacement in every log record.
        for variable, short, enough in (
            ("NCI_SIP_EVS_LICENSE_KEY", "k" * 7, "k" * 8),
            ("NCI_SIP_CADSR_CREDENTIAL", "user:" + "p" * 7, "user:" + "p" * 8),
        ):
            with self.subTest(variable=variable):
                with self.assertRaises(ValueError) as raised:
                    settings_from(**{variable: short})
                self.assertIn(variable, str(raised.exception))
                self.assertNotIn(short, str(raised.exception))
                settings_from(**{variable: enough})

    def test_the_index_requirement_is_a_boolean_read_from_zero_or_one(self):
        self.assertIs(settings_from(NCI_SIP_HTTP_REQUIRE_INDEX="1").http_require_index, True)
        self.assertIs(settings_from(NCI_SIP_HTTP_REQUIRE_INDEX="0").http_require_index, False)
        self.assertIs(settings_from().http_require_index, False)
        with self.assertRaisesRegex(ValueError, "NCI_SIP_HTTP_REQUIRE_INDEX"):
            settings_from(NCI_SIP_HTTP_REQUIRE_INDEX="maybe")

    def test_loading_settings_logs_no_secret(self):
        with self.assertNoLogs(level=logging.DEBUG):
            settings_from(NCI_SIP_EVS_LICENSE_KEY=LICENCE_KEY, NCI_SIP_CADSR_CREDENTIAL=CREDENTIAL)


class SettingsEdgeCaseTest(unittest.TestCase):
    def assert_rejected(self, variable, secret=None, **fields):
        with self.assertRaises(ValueError) as raised:
            Settings(**fields)
        self.assertIn(variable, str(raised.exception))
        if secret:
            self.assertNotIn(secret, str(raised.exception))

    def test_direct_construction_rejects_empty_values(self):
        self.assert_rejected("NCI_SIP_EVS_LICENSE_KEY", evs_license_key="")
        self.assert_rejected("NCI_SIP_EXCLUSION_ROLE_CODES", exclusion_role_codes=())

    def test_control_characters_and_non_ascii_digits_are_rejected_without_the_value(self):
        self.assert_rejected("NCI_SIP_EVS_LICENSE_KEY", "key\x01", evs_license_key="key\x01")
        self.assert_rejected(
            "NCI_SIP_CADSR_CREDENTIAL", "pa\x00ss", cadsr_credential="user:pa\x00ss"
        )
        fullwidth = "".join(chr(0xFF10 + digit) for digit in (1, 3, 5))
        self.assert_rejected(
            "NCI_SIP_EXCLUSION_ROLE_CODES", fullwidth, exclusion_role_codes=(f"R{fullwidth}",)
        )

    def test_the_credential_splits_at_the_first_colon(self):
        for credential in ("user:pass:word", "user:password:"):
            with self.subTest(credential):
                settings = Settings(cadsr_credential=credential)
                self.assertEqual(settings.cadsr_credential, credential)

    def test_role_code_order_is_kept(self):
        settings = settings_from(NCI_SIP_EXCLUSION_ROLE_CODES="R201,R135")

        self.assertEqual(settings.exclusion_role_codes, ("R201", "R135"))

    def test_trailing_slashes_are_stripped_and_a_lone_slash_is_not_a_default(self):
        self.assertEqual(
            settings_from(NCI_SIP_EVS_BASE_URL="https://h//").evs_base_url, "https://h"
        )
        with self.assertRaises(ValueError) as raised:
            settings_from(NCI_SIP_EVS_BASE_URL="/")
        self.assertIn("NCI_SIP_EVS_BASE_URL", str(raised.exception))

    def test_a_closed_value_must_match_exactly(self):
        for variable, field in (
            ("NCI_SIP_PROFILE", "profile"),
            ("NCI_SIP_UPSTREAM_MODE", "upstream_mode"),
            ("NCI_SIP_RELEASE_CHANNEL", "release_channel"),
        ):
            for value in ("EVS", "Live", "Weekly", " live", "live ", "all"):
                with self.subTest(variable=variable, value=value):
                    self.assert_rejected(variable, **{field: value})
                    with self.assertRaises(ValueError):
                        settings_from(**{variable: value})


REPOSITORY = Path(__file__).parent.parent


class DefaultsMatchTheirSourcesTest(unittest.TestCase):
    """The server never reads acceptance/ or spec/ at runtime, so config keeps copies of these
    facts and this test ties them to their sources."""

    def test_production_urls_and_exclusion_roles_equal_the_repository_data(self):
        manifest = yaml.safe_load((REPOSITORY / "acceptance/fixtures/manifest.yaml").read_text())
        records = yaml.safe_load((REPOSITORY / "spec/records.yaml").read_text())
        surfaces = manifest["surfaces"]
        spec_roles = records["traversal"]["fields"]["polarity"]["exclusions"]["ncit"]
        for setting, ours, theirs in (
            ("evs_base_url", PRODUCTION_BASE_URLS["evs_base_url"], surfaces["evs"]),
            ("evs_fhir_base_url", PRODUCTION_BASE_URLS["evs_fhir_base_url"], surfaces["evs-fhir"]),
            ("cadsr_base_url", PRODUCTION_BASE_URLS["cadsr_base_url"], surfaces["cadsr"]),
            ("cadsr_ftp_url", PRODUCTION_BASE_URLS["cadsr_ftp_url"], surfaces["cadsr-ftp"]),
            ("ssis_sparql_url", PRODUCTION_BASE_URLS["ssis_sparql_url"], surfaces["ssis-sparql"]),
            ("exclusion_role_codes", DEFAULT_EXCLUSION_ROLE_CODES, tuple(spec_roles)),
        ):
            with self.subTest(setting):
                self.assertEqual(ours, theirs, f"{setting}: config has {ours!r}, source {theirs!r}")


class LoggingTest(unittest.TestCase):
    def test_diagnostics_go_to_stderr_at_the_configured_level(self):
        root = logging.getLogger()
        self.addCleanup(setattr, root, "handlers", root.handlers[:])
        self.addCleanup(root.setLevel, root.level)
        root.handlers.clear()

        configure_logging("warning")

        self.assertEqual(root.level, logging.WARNING)
        self.assertEqual([handler.stream for handler in root.handlers], [sys.stderr])


if __name__ == "__main__":
    unittest.main()
