import io
import json
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from fakes import FakeEVS, release, terminology_row
from nci_sip_mcp.errors import PlatformError, is_error_record, serialise
from nci_sip_mcp.evs import EVSClient, EVSNotFoundError, EVSReleaseNotFoundError
from nci_sip_mcp.invocation import call
from nci_sip_mcp.registry import invoke
from nci_sip_mcp.release import registry_state, resolve_evs_release, served_evs_release
from nci_sip_mcp.traffic_identity import product_user_agent
from test_evs_client import FakeResponse
from test_handlers import NEOPLASM, HandlerTestCase

# The weekly build is listed first, as EVS may list it, and both are latest for their channel.
WEEKLY = terminology_row("26.09c", "2026-09-21", weekly="true")
MONTHLY = terminology_row("26.09d", "2026-09-28", monthly="true")
EXPORT = "releasedCDEsXML-OD.zip"


def registry_result(date, identifier=None, distribution=EXPORT):
    return call(
        "registry_state",
        lambda: registry_state(date, identifier, source_distribution=distribution).to_dict(),
    )


def fake_with(*rows):
    evs = FakeEVS()
    evs.rows = list(rows)
    return evs


class ResolveEvsReleaseTest(unittest.TestCase):
    def test_every_unavailable_release_is_one_record_with_its_own_next_step(self):
        ambiguous = {**MONTHLY, "tags": {}}
        cases = (
            (lambda: served_evs_release([], "ncit", "26.09d", "monthly"), "resolve_release"),
            (lambda: served_evs_release([ambiguous], "ncit", "26.09d", "monthly"), "metadata"),
            (
                lambda: resolve_evs_release(fake_with(), "ncit", "monthly"),
                "NCI_SIP_RELEASE_CHANNEL",
            ),
        )
        for raiser, next_step in cases:
            with self.subTest(next_step=next_step), self.assertRaises(PlatformError) as raised:
                raiser()
            error = raised.exception
            self.assertEqual(error.code, "release_not_available")
            self.assertEqual(error.details["source"], "evs")
            self.assertIn(next_step, str(error))

    def test_explicit_served_identity_keeps_its_channel_date_and_pinned_path(self):
        resolved = served_evs_release([WEEKLY, MONTHLY], "ncit", "26.09c", "monthly")
        self.assertEqual(
            resolved.to_dict(),
            {"terminology": "ncit", "channel": "weekly", "version": "26.09c", "date": "2026-09-21"},
        )
        self.assertEqual(resolved.pinned_terminology, "ncit_26.09c")

    def test_the_one_row_a_channel_names_is_the_release(self):
        resolved = resolve_evs_release(fake_with(MONTHLY), "ncit", "monthly")

        self.assertEqual(
            (resolved.terminology, resolved.channel, resolved.version, resolved.date),
            ("ncit", "monthly", "26.09d", "2026-09-28"),
        )
        self.assertEqual(resolved.pinned_terminology, "ncit_26.09d")
        self.assertEqual(
            resolved.to_dict(),
            {
                "terminology": "ncit",
                "channel": "monthly",
                "version": "26.09d",
                "date": "2026-09-28",
            },
        )

    def test_each_channel_takes_the_row_its_tag_names(self):
        evs = fake_with(WEEKLY, MONTHLY)

        self.assertEqual(resolve_evs_release(evs, "ncit", "monthly").version, "26.09d")
        self.assertEqual(resolve_evs_release(evs, "ncit", "weekly").version, "26.09c")

    def test_a_row_tagged_for_both_channels_is_the_release_of_each(self):
        both = terminology_row("26.09d", "2026-09-28", monthly="true", weekly="true")
        evs = fake_with(both, terminology_row("26.09c", latest=False, weekly="true"))

        for channel in ("monthly", "weekly"):
            self.assertEqual(resolve_evs_release(evs, "ncit", channel).version, "26.09d")

    def test_latest_is_scoped_to_the_channel_not_to_the_terminology(self):
        evs = fake_with(WEEKLY, dict(MONTHLY, latest=False))

        self.assertEqual(resolve_evs_release(evs, "ncit", "weekly").version, "26.09c")
        with self.assertRaises(PlatformError) as raised:
            resolve_evs_release(evs, "ncit", "monthly")
        self.assertEqual(raised.exception.code, "release_not_available")

    def test_the_terminology_version_the_row_names_is_what_requests_are_pinned_to(self):
        row = dict(MONTHLY, terminologyVersion="ncit_2609d_monthly")

        self.assertEqual(
            resolve_evs_release(fake_with(row), "ncit", "monthly").pinned_terminology,
            "ncit_2609d_monthly",
        )
        row.pop("terminologyVersion")
        self.assertEqual(
            resolve_evs_release(fake_with(row), "ncit", "monthly").pinned_terminology,
            "ncit_26.09d",
        )

    def test_zero_and_several_rows_fail_closed_naming_what_was_found_and_the_next_step(self):
        for rows, found in (
            ([WEEKLY], []),
            ([MONTHLY, terminology_row("26.08e", monthly="true")], ["26.09d", "26.08e"]),
        ):
            with self.subTest(found=found), self.assertRaises(PlatformError) as raised:
                resolve_evs_release(fake_with(*rows), "ncit", "monthly")

            error = raised.exception
            self.assertEqual(error.code, "release_not_available")
            details = {"requested": "ncit monthly", "source": "evs"}
            if found:
                details["found"] = found
            self.assertEqual(serialise(error)["error"]["details"], details)
            count = 2 if found else 0
            self.assertEqual(
                error.message,
                f"EVS lists {count} releases as latest, not exactly one. "
                "Retry later, or set NCI_SIP_RELEASE_CHANNEL "
                "to the other channel (monthly or weekly)",
            )

    def test_a_row_without_a_version_is_not_a_release(self):
        absent = {key: value for key, value in MONTHLY.items() if key != "version"}
        for row in (absent, dict(MONTHLY, version=None), dict(MONTHLY, version="")):
            with self.subTest(row=row), self.assertRaises(PlatformError) as raised:
                resolve_evs_release(fake_with(row), "ncit", "monthly")

            self.assertEqual(raised.exception.code, "release_not_available")

    def test_the_query_names_the_terminology_latest_and_the_channel(self):
        with (
            patch("nci_sip_mcp.http_client._open", return_value=FakeResponse(b"[]")) as opened,
            self.assertRaises(PlatformError) as raised,
        ):
            resolve_evs_release(
                EVSClient("https://example.invalid", user_agent=product_user_agent()),
                "ncit",
                "weekly",
            )

        # An answer without a row is no release, whatever the query was.
        self.assertEqual(raised.exception.code, "release_not_available")
        self.assertEqual(
            opened.call_args.args[0].full_url,
            "https://example.invalid/api/v1/metadata/terminologies"
            "?terminology=ncit&latest=true&tag=weekly",
        )

    def test_the_unfiltered_listing_sends_no_filter(self):
        response = FakeResponse(json.dumps([MONTHLY]).encode())
        with patch("nci_sip_mcp.http_client._open", return_value=response) as opened:
            rows = EVSClient(
                "https://example.invalid", user_agent=product_user_agent()
            ).get_terminologies()

        self.assertEqual(rows, [MONTHLY])
        self.assertEqual(
            opened.call_args.args[0].full_url,
            "https://example.invalid/api/v1/metadata/terminologies",
        )


class UnknownReleaseTest(HandlerTestCase):
    def answer_404(self, message):
        body = io.BytesIO(f'{{"message": "{message}"}}'.encode())
        error = HTTPError("https://example.invalid", 404, "Not Found", {}, body)
        return patch("nci_sip_mcp.http_client._open", side_effect=[error])

    def test_a_release_evs_does_not_serve_is_a_release_failure_naming_it(self):
        with (
            self.answer_404("Terminology not found = ncit_99.99z"),
            self.assertRaises(EVSReleaseNotFoundError) as raised,
        ):
            EVSClient(
                "https://example.invalid", max_attempts=1, user_agent=product_user_agent()
            ).get_concept("C4817", release=release("99.99z"))

        self.assertEqual(raised.exception.details, {"requested": "ncit_99.99z", "source": "evs"})

    def test_any_other_404_of_a_concept_request_is_still_a_missing_concept(self):
        with (
            self.answer_404("C4817 not found"),
            self.assertRaises(EVSNotFoundError),
        ):
            EVSClient(
                "https://example.invalid", max_attempts=1, user_agent=product_user_agent()
            ).get_concept("C4817", release=release())

    def test_a_pinned_release_no_longer_served_tells_the_caller_why_to_retry(self):
        self.context.evs = EVSClient(
            "https://example.invalid", max_attempts=1, user_agent=product_user_agent()
        )
        with (
            patch.object(self.context.evs, "get_terminologies", return_value=[MONTHLY]),
            self.answer_404("Terminology not found = ncit_26.09d"),
        ):
            result = invoke(self.context, "lookup", "C4817", live_only=True)

        self.assertEqual(result["error"]["code"], "release_not_available")
        self.assertEqual(result["error"]["details"], {"requested": "ncit_26.09d", "source": "evs"})
        self.assertEqual(
            result["error"]["message"],
            "EVS request failed for /api/v1/concept/ncit_26.09d/C4817: HTTP 404 Not Found. "
            "Retry later: EVS no longer serves the release the request was pinned to.",
        )

    def test_prose_before_the_unknown_terminology_still_classifies_a_release_failure(self):
        # Given a prefixed EVS message, when a concept is read, then the release
        # failure names the upstream requested terminology rather than a missing concept.
        client = EVSClient(
            "https://example.invalid", max_attempts=1, user_agent=product_user_agent()
        )
        with self.answer_404("Error: Terminology not found = ncit_x"):
            result = call("get_concept", lambda: client.get_concept("C4817", release=release()))

        self.assertEqual(result["error"]["code"], "release_not_available")
        self.assertEqual(result["error"]["details"], {"requested": "ncit_x", "source": "evs"})


class ServiceReleaseTest(HandlerTestCase):
    def test_an_index_mismatch_names_the_configured_weekly_channel(self):
        self.index()
        self.evs.release = release("26.07a", "2026-07-06", channel="weekly")

        result = invoke(self.make_context(release_channel="weekly"), "lookup", "C3262")

        self.assertEqual(result["error"]["code"], "release_mismatch")
        self.assertIn("current weekly release", result["error"]["message"])
        self.assertEqual(result["error"]["details"]["requested"], "26.07a")

    def test_a_resolved_release_is_not_kept_between_calls(self):
        first = invoke(self.context, "lookup", "C3262", live_only=True)
        self.evs.release = release("26.07d", "2026-07-27")
        self.evs.concepts["C3262"] = dict(NEOPLASM, version="26.07d")

        second = invoke(self.context, "lookup", "C3262", live_only=True)

        self.assertEqual(first["provenance"]["release"]["identifier"], "26.06e")
        self.assertEqual(second["provenance"]["release"]["identifier"], "26.07d")

    def test_every_call_resolves_the_release_before_it_reads_content(self):
        invoke(self.context, "lookup", "C3262", live_only=True)
        invoke(self.context, "traverse", ["C3262"], max_depth=1)

        methods = [call[0] for call in self.evs.calls]
        self.assertEqual(methods.count("get_terminologies"), 2)
        self.assertEqual(methods[0], "get_terminologies")

    def test_the_configured_channel_decides_the_release(self):
        self.evs.release = release("26.07a", "2026-07-06", channel="weekly")
        self.evs.concepts["C3262"] = dict(NEOPLASM, version="26.07a")
        weekly = self.make_context(release_channel="weekly")

        reported = invoke(
            weekly,
            "release_info",
        )["selected_release"]
        result = invoke(weekly, "lookup", "C3262", live_only=True)

        self.assertEqual((reported["channel"], reported["version"]), ("weekly", "26.07a"))
        self.assertEqual(result["provenance"]["release"]["identifier"], "26.07a")
        self.assertIn(("get_terminologies", "ncit", (True, "weekly")), self.evs.calls)
        # The default context, on the monthly channel, finds no monthly row here.
        self.assertTrue(is_error_record(invoke(self.context, "lookup", "C3262", live_only=True)))

    def test_the_payload_version_is_a_second_guard_against_the_resolved_release(self):
        self.evs.concepts["C3262"] = dict(NEOPLASM, version="26.05d")

        result = invoke(self.context, "lookup", "C3262", live_only=True)

        self.assertEqual(result["error"]["code"], "release_mismatch")
        self.assertEqual(result["error"]["details"]["requested"], "26.06e")


class RegistryStateTest(unittest.TestCase):
    def test_a_blank_generation_date_is_reported_as_missing(self):
        for identifier in (None, "R2026.3"):
            for date in ("", "   "):
                with self.subTest(identifier=identifier, date=date):
                    error = registry_result(date, identifier)["error"]
                    self.assertEqual(error["code"], "upstream_unavailable")
                    self.assertIn("missing", error["message"])

    def test_no_registry_identifier_is_made_up_and_the_export_date_is_iso(self):
        state = registry_result("2026-07-01T22:19")

        self.assertEqual(
            state,
            {
                "published": False,
                "generatedAt": "2026-07-01T22:19",
                "sourceDistribution": EXPORT,
            },
        )

    def test_export_dates_never_assume_a_zone_or_change_precision(self):
        for date in (
            "2026-07-01T22:19Z",
            "2026-07-01T22:19-04:00",
            "2026-07-01",
            "2026-07-01T22:19:00",
            "Thu, 02 Jul 2026 02:19:40 GMT",
        ):
            with self.subTest(date=date):
                self.assertEqual(registry_result(date)["error"]["code"], "upstream_unavailable")

    def test_a_missing_or_invalid_generation_date_fails_closed_in_either_state(self):
        for identifier in (None, "R2026.3"):
            for date in (None, "", "  ", 7, "last Tuesday", "2026-02-30T14:00"):
                with self.subTest(identifier=identifier, date=date):
                    result = registry_result(date, identifier)
                    self.assertEqual(set(result), {"error"})
                    self.assertEqual(result["error"]["code"], "upstream_unavailable")
                    self.assertEqual(result["error"]["details"], {"surface": "cadsr"})
                    self.assertIn("Retry later", result["error"]["message"])
                    self.assertTrue(result["error"]["correlationId"])

    def test_an_identifier_the_registry_publishes_is_carried_as_it_is(self):
        state = registry_result("2026-07-01T22:19", " R2026.3 ", "registry-releases.json")

        self.assertEqual(
            state,
            {
                "published": True,
                "identifier": " R2026.3 ",
                "generatedAt": "2026-07-01T22:19",
                "sourceDistribution": "registry-releases.json",
            },
        )

    def test_a_published_release_date_keeps_its_precision_and_offset(self):
        for date in ("2026-09-28", "2026-09-28T16:03:00+02:00", "2026-09-28T14:03:00Z"):
            with self.subTest(date=date):
                self.assertEqual(registry_result(date, "R2026.3")["generatedAt"], date)

    def test_a_published_identifier_that_is_blank_or_not_text_fails_closed(self):
        for identifier in ("", "  ", 7):
            with self.subTest(identifier=identifier):
                error = registry_result("2026-09-28T14:03:00Z", identifier)["error"]
                self.assertEqual(error["code"], "upstream_unavailable")
                self.assertEqual(error["details"], {"surface": "cadsr"})
                self.assertIn("identifier", error["message"])

    def test_a_missing_source_distribution_is_not_a_registry_state(self):
        for distribution in (None, "", " ", 7):
            with self.subTest(distribution=distribution):
                error = registry_result("2026-07-01T22:19", distribution=distribution)["error"]
                self.assertEqual(error["code"], "upstream_unavailable")
                self.assertEqual(error["details"], {"surface": "cadsr"})
                self.assertIn("distribution", error["message"])


class SelectionMetadataTest(unittest.TestCase):
    def test_invalid_tags_versions_and_segments_are_release_errors(self):
        for change in (
            {"tags": None},
            {"tags": {"monthly": "false"}},
            {"version": ".26"},
            {"version": "_26"},
            {"terminologyVersion": "ncit_.26"},
            {"terminologyVersion": "ncit_-26"},
            {"terminologyVersion": 26},
        ):
            with (
                patch(
                    "nci_sip_mcp.evs.EVSClient.get_terminologies", return_value=[MONTHLY | change]
                ),
                self.subTest(change=change),
                self.assertRaises(PlatformError) as raised,
            ):
                resolve_evs_release(
                    EVSClient("https://example.invalid", user_agent=product_user_agent()),
                    "ncit",
                    "monthly",
                )
            self.assertEqual(raised.exception.code, "release_not_available")


class ServedReleaseIdentityTest(unittest.TestCase):
    def test_another_terminology_with_the_same_version_is_not_the_requested_release(self):
        row = terminology_row() | {"terminology": "other"}
        with self.assertRaises(PlatformError) as raised:
            served_evs_release([row], "ncit", "26.06e", "monthly")
        self.assertEqual(raised.exception.code, "release_not_available")

    def test_a_served_release_retains_its_upstream_pinned_identifier(self):
        row = terminology_row() | {"terminologyVersion": "upstream-specific-pin"}
        selected = served_evs_release([row], "ncit", "26.06e", "monthly")
        self.assertEqual(selected.pinned_terminology, "upstream-specific-pin")
        self.assertEqual(selected.version, "26.06e")
