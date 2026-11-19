"""caDSR tool behavior against offline contract-crafted HTTP responses."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import parse_qs, urlsplit

from fakes import code_map
from fakes import data_element as element
from nci_sip_mcp import cursor
from nci_sip_mcp.caching import cache_call
from nci_sip_mcp.cadsr import EXPORT_FOLDER
from nci_sip_mcp.config import Settings
from nci_sip_mcp.context import Context
from nci_sip_mcp.registry import invoke
from test_cadsr_client import LISTING, reply
from test_http_client import Reply, ServerTestCase


class CaDSRFixture(ServerTestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)

    def context(self, server):
        return Context(
            Settings(
                data_dir=Path(self.directory.name),
                cadsr_base_url=server.url,
                cadsr_ftp_url=server.url,
            )
        )

    def call(self, server, operation="get_data_element", **arguments):
        with cache_call() as hint:
            result = invoke(
                self.context(server), operation, _correlation_id="fixture-call", **arguments
            )
        self.hint = hint
        return result


class CaDSRContentTest(CaDSRFixture):
    def test_unpinned_element_preserves_own_fields_without_export_metadata(self):
        raw = element(AlternateNames=[{"name": "Alias"}])
        server = self.serve(reply({"DataElement": raw}))
        result = self.call(server, publicId="123")
        self.assertEqual({k: v for k, v in result.items() if k != "provenance"}, element())
        self.assertEqual(result["provenance"]["release"], {"registry": "cadsr"})
        self.assertEqual(result["provenance"]["correlationId"], "fixture-call")
        self.assertEqual(self.hint, {"ttlMs": 3_600_000, "cacheScope": "public"})
        self.assertEqual([path for path, _ in server.seen], ["/NCIAPI/1.0/api/DataElement/123"])

    def test_unpublished_pin_fails_before_content_access(self):
        server = self.serve(Reply(404, body=b""))
        result = self.call(server, publicId="123", registryRelease="2026.07.02")
        self.assertEqual(result["error"]["code"], "release_not_available")
        self.assertEqual(result["error"]["details"], {"requested": "2026.07.02", "source": "cadsr"})
        self.assertEqual([path for path, _ in server.seen], ["/NCIAPI/1.0/api/registry/releases"])

    def test_verified_pin_reaches_the_content_request_and_cache_policy(self):
        row = {"identifier": "2026.07.02", "generatedAt": "2026-07-01T22:19", "latest": True}
        server = self.serve(
            reply({"registryReleases": [row]}),
            reply({"DataElement": element(), "registryRelease": row["identifier"]}),
        )
        result = self.call(server, publicId="123", version="2", registryRelease=row["identifier"])
        self.assertEqual(
            parse_qs(urlsplit(server.seen[-1][0]).query),
            {"version": ["2"], "registryRelease": [row["identifier"]], "publicId": ["123"]},
        )
        self.assertEqual(urlsplit(server.seen[-1][0]).path, "/NCIAPI/1.0/api/DataElement")
        self.assertEqual(
            result["provenance"]["release"],
            {"registry": "cadsr", "identifier": row["identifier"], "date": row["generatedAt"]},
        )
        self.assertEqual(self.hint, {"ttlMs": 86_400_000, "cacheScope": "public"})

    def test_question_text_resolves_the_full_item_and_preserves_requested_version(self):
        server = self.serve(
            reply({"DataElements": [{"publicId": "123"}]}), reply({"DataElement": element()})
        )
        result = self.call(server, questionText="A & B", version="2")
        self.assertEqual(result["dateModified"], "2026-08-25")
        self.assertEqual(parse_qs(urlsplit(server.seen[0][0]).query)["documentText"], ["A & B"])
        self.assertEqual(server.seen[1][0], "/NCIAPI/1.0/api/DataElement/123?version=2")

    def test_ambiguous_question_names_candidates_without_fetching_one(self):
        server = self.serve(reply({"DataElements": [{"publicId": "123"}, {"publicId": "456"}]}))
        result = self.call(server, questionText="Shared question")
        self.assertEqual(result["error"]["code"], "invalid_request")
        self.assertIn("123", json.dumps(result))
        self.assertIn("456", json.dumps(result))
        self.assertEqual(len(server.seen), 1)

    def test_live_shaped_search_error_gives_fixed_actionable_guidance(self):
        server = self.serve(reply({"apiResponse": {"type": "E", "message": "untrusted wording"}}))
        result = self.call(server, "search_data_elements", query="patient")
        self.assertEqual(result["error"]["code"], "upstream_unavailable")
        self.assertEqual(result["error"]["details"]["surface"], "cadsr")
        message = result["error"]["message"]
        self.assertIn("webMethods error envelope", message)
        self.assertIn("OP-C03", message)
        self.assertIn("get_data_element", message)
        self.assertTrue(
            message.endswith(
                "if this persists, the cause is likely that, and get_data_element by "
                "publicId or questionText is available."
            )
        )
        self.assertEqual(len(server.seen), 1)

    def test_unsupported_filters_never_reach_upstream(self):
        server = self.serve()
        result = self.call(
            server, "search_data_elements", query="patient", filters={"context": "TEST"}
        )
        self.assertEqual(result["error"]["code"], "capability_unavailable")
        self.assertEqual(server.seen, [])

    def test_search_failures_keep_their_diagnostic_and_only_the_masked_answer_gets_the_hint(self):
        row = {"identifier": "known", "generatedAt": "2026-07-01T22:19"}
        for response, arguments, diagnostic in (
            (Reply(401), {}, "credentials"),
            (reply({"DataElements": [element()], "numRecords": 0}), {}, "malformed search count"),
            (
                reply({"registryReleases": [row, row]}),
                {"registryRelease": "known"},
                "more than once",
            ),
        ):
            with self.subTest(diagnostic=diagnostic):
                server = self.serve(response)
                result = self.call(server, "search_data_elements", query="patient", **arguments)
                self.assertEqual(result["error"]["code"], "upstream_unavailable")
                self.assertIn(diagnostic, result["error"]["message"])
                self.assertNotIn("OP-C03", result["error"]["message"])
                self.assertNotIn("get_data_element", result["error"]["message"])
                self.assertEqual(len(server.seen), 1)

    def test_question_search_and_contexts_require_the_verified_pin_echo(self):
        row = {"identifier": "known", "generatedAt": "2026-07-01T22:19"}
        for operation, arguments, payload in (
            ("get_data_element", {"questionText": "Q"}, {"DataElements": [{"publicId": "123"}]}),
            ("search_data_elements", {"query": "Q"}, {"DataElements": [element()]}),
            ("list_contexts", {}, {"contextNames": ["TEST"]}),
        ):
            for echo, served in (
                ({}, []),
                ({"registryRelease": "other"}, ["other"]),
                ({"registryRelease": 42}, []),
                ({"registryRelease": ""}, []),
            ):
                with self.subTest(operation=operation, echo=echo):
                    server = self.serve(reply({"registryReleases": [row]}), reply(payload | echo))
                    result = self.call(server, operation, registryRelease="known", **arguments)
                    self.assertEqual(result["error"]["code"], "release_mismatch")
                    self.assertEqual(
                        result["error"]["details"],
                        {
                            "requested": "known",
                            "served": served,
                            "source": "cadsr",
                        },
                    )
                    self.assertEqual(set(result), {"error"})
                    self.assertEqual(len(server.seen), 2)

    def test_capped_search_continues_without_inventing_a_count(self):
        rows = [element(str(1000 + i)) for i in range(1000)]
        server = self.serve(reply({"DataElements": rows}), reply({"DataElements": rows}))
        first = self.call(server, "search_data_elements", query="patient", limit=3)
        second = self.call(
            server, "search_data_elements", query="patient", limit=3, cursor=first["nextCursor"]
        )
        self.assertEqual(
            [r["dataElement"]["publicId"] for r in second["results"]], ["1003", "1004", "1005"]
        )
        self.assertEqual(
            first["truncation"],
            {
                "occurred": True,
                "bound": "upstream_cap",
                "limit": 1000,
                "reached": 1000,
                "omitted": 1,
                "exact": False,
            },
        )
        self.assertNotIn("totalKnown", first)

    def test_export_registry_tool_is_uncached_and_keeps_local_time(self):
        server = self.serve(Reply(404, body=b""), Reply(body=LISTING.encode()))
        result = self.call(server, "resolve_registry_release")
        self.assertEqual(
            result,
            {
                "published": False,
                "generatedAt": "2026-07-01T22:19",
                "sourceDistribution": "releasedCDEsXML-OD.zip",
            },
        )
        self.assertEqual(self.hint, {"ttlMs": 0, "cacheScope": "public"})

    def test_each_selected_section_has_the_spec_shape_and_nested_provenance(self):
        raw = element(
            ValueDomain={
                "type": "Enumerated",
                "PermissibleValues": [
                    {
                        "publicId": "800",
                        "value": "A",
                        "ValueMeaning": {
                            "publicId": "801",
                            "version": "1",
                            "longName": "Fixture meaning",
                            "Concepts": [
                                {
                                    "conceptCode": "C1",
                                    "longName": "Alpha",
                                    "primaryIndicator": "Yes",
                                }
                            ],
                        },
                    }
                ],
            },
            ClassificationSchemes=[
                {
                    "publicId": "700",
                    "version": "1",
                    "longName": "Fixture scheme",
                    "context": "TEST",
                    "ClassificationSchemeItems": [
                        {"publicId": "701", "version": "1", "longName": "Item"}
                    ],
                }
            ],
            DataElementConcept={
                "ObjectClass": {
                    "Concepts": [
                        {"conceptCode": "C2", "longName": "Object", "primaryIndicator": "Yes"}
                    ]
                },
                "Property": {
                    "Concepts": [
                        {"conceptCode": "C3", "longName": "Property", "primaryIndicator": "No"}
                    ]
                },
            },
            AlternateNames=[{"name": "Verbatim alias", "type": "Fixture"}],
        )
        expected = {
            "valueDomain": {"type": "Enumerated"},
            "alternateNames": raw["AlternateNames"],
            "conceptAssociations": [
                {"conceptCode": "C2", "longName": "Object", "role": "objectClass", "primary": True},
                {"conceptCode": "C3", "longName": "Property", "role": "property", "primary": False},
            ],
            "permissibleValues": [
                {
                    "publicId": "800",
                    "value": "A",
                    "valueMeaning": {
                        "publicId": "801",
                        "version": "1",
                        "longName": "Fixture meaning",
                        "concepts": [{"conceptCode": "C1", "longName": "Alpha", "primary": True}],
                    },
                }
            ],
            "classificationSchemes": [
                {
                    "publicId": "700",
                    "version": "1",
                    "longName": "Fixture scheme",
                    "context": "TEST",
                    "items": [{"publicId": "701", "version": "1", "longName": "Item"}],
                }
            ],
        }
        for section, value in expected.items():
            with self.subTest(section=section):
                server = self.serve(reply({"DataElement": raw}))
                result = self.call(server, publicId="123", include=[section, section])
                self.assertEqual(set(result) & set(expected), {section})
                actual = result[section]
                if section in ("permissibleValues", "classificationSchemes"):
                    self.assertEqual(actual[0].pop("provenance")["release"], {"registry": "cadsr"})
                self.assertEqual(actual, value)

    def test_each_section_item_carries_only_its_own_origin_and_licence(self):
        def scheme(identifier, **extra):
            row = {"publicId": identifier, "version": "1", "longName": "S", "context": "TEST"}
            return row | {"ClassificationSchemeItems": []} | extra

        raw = element(
            ClassificationSchemes=[
                scheme("700", dateModified="2026-01-01", licenseText="Scheme licence"),
                scheme("710"),
            ]
        )
        server = self.serve(reply({"DataElement": raw}))
        result = self.call(server, publicId="123", include=["classificationSchemes"])
        first, second = (row["provenance"] for row in result["classificationSchemes"])

        self.assertEqual(
            first["upstream"], {"publicId": "700", "version": "1", "dateModified": "2026-01-01"}
        )
        self.assertEqual(first["attribution"], "Scheme licence")
        self.assertEqual(second["upstream"], {"publicId": "710", "version": "1"})
        self.assertNotIn("attribution", second)
        self.assertNotIn("attribution", result["provenance"])
        self.assertEqual(result["provenance"]["upstream"]["publicId"], "123")

    def test_known_unavailable_capabilities_make_no_content_request(self):
        for tool, args in (
            ("get_data_element", {"longName": "Name"}),
            ("list_classification_schemes", {"context": "TEST"}),
            ("search_data_elements", {"query": "Q", "mode": "semantic"}),
            ("search_data_elements", {"query": "Q", "mode": "hybrid"}),
        ):
            with self.subTest(tool=tool, args=args):
                server = self.serve()
                result = self.call(server, tool, **args)
                self.assertEqual(result["error"]["code"], "capability_unavailable")
                self.assertEqual(server.seen, [])

    def test_informational_absence_and_no_question_candidates_are_not_found(self):
        for body, args in (
            ({"DataElement": None, "apiResponse": {"type": "I"}}, {"publicId": "123"}),
            ({"DataElements": []}, {"questionText": "Nothing"}),
        ):
            with self.subTest(args=args):
                result = self.call(self.serve(reply(body)), **args)
                self.assertEqual(result["error"]["code"], "not_found")
                self.assertEqual(result["error"]["details"], {"identifiers": list(args.values())})

    def test_wrong_item_identity_or_version_is_never_returned(self):
        for raw in (
            element("456"),
            element(version="3"),
            element(version=None),
            element(version="4e0"),
            element(workflowStatus=[]),
        ):
            with self.subTest(raw=raw):
                result = self.call(
                    self.serve(reply({"DataElement": raw})), publicId="123", version="2"
                )
                self.assertEqual(result["error"]["code"], "upstream_unavailable")

    def test_malformed_question_candidates_are_upstream_failures(self):
        for row in ({}, {"publicId": "0"}, {"publicId": 123}):
            with self.subTest(row=row):
                result = self.call(self.serve(reply({"DataElements": [row]})), questionText="Q")
                self.assertEqual(result["error"]["code"], "upstream_unavailable")

    def test_malformed_requested_sections_are_errors_not_empty_results(self):
        for extra, section in (
            ({"AlternateNames": None}, "alternateNames"),
            ({"ValueDomain": None}, "valueDomain"),
            ({"ClassificationSchemes": [1]}, "classificationSchemes"),
            (
                {"ValueDomain": {"PermissibleValues": [{"ValueMeaning": {"Concepts": [{}]}}]}},
                "permissibleValues",
            ),
        ):
            with self.subTest(section=section):
                result = self.call(
                    self.serve(reply({"DataElement": element(**extra)})),
                    publicId="123",
                    include=[section],
                )
                self.assertEqual(result["error"]["code"], "upstream_unavailable")

    def test_unlisted_or_duplicate_pins_never_request_content(self):
        row = {"identifier": "known", "generatedAt": "2026-01-01"}
        for rows, requested, code in (
            ([row], "other", "release_not_available"),
            ([row, row], "known", "upstream_unavailable"),
        ):
            with self.subTest(requested=requested):
                server = self.serve(reply({"registryReleases": rows}))
                result = self.call(server, publicId="123", registryRelease=requested)
                self.assertEqual(result["error"]["code"], code)
                self.assertEqual(len(server.seen), 1)

    def test_a_verified_pin_missing_from_content_is_not_claimed_in_provenance(self):
        row = {"identifier": "known", "generatedAt": "2026-01-01"}
        for returned in (None, "different"):
            with self.subTest(returned=returned):
                server = self.serve(
                    reply({"registryReleases": [row]}),
                    reply({"DataElement": element(), "registryRelease": returned}),
                )
                result = self.call(server, publicId="123", registryRelease="known")
                self.assertEqual(result["error"]["code"], "release_mismatch")

    def test_empty_search_has_provenance_and_only_an_upstream_count(self):
        for body, counted in (
            ({"DataElements": []}, False),
            ({"DataElements": [], "numRecords": 0}, True),
        ):
            with self.subTest(counted=counted):
                result = self.call(self.serve(reply(body)), "search_data_elements", query="none")
                self.assertEqual(result["results"], [])
                self.assertEqual(result["provenance"]["release"], {"registry": "cadsr"})
                self.assertEqual(result["truncation"], {"occurred": False})
                self.assertEqual("totalKnown" in result, counted)
                self.assertEqual(self.hint["ttlMs"], 3_600_000)

    def test_malformed_or_incomplete_search_count_fails(self):
        for count in (True, "1", -1, 2):
            with self.subTest(count=count):
                server = self.serve(reply({"DataElements": [element()], "numRecords": count}))
                result = self.call(server, "search_data_elements", query="Q")
                self.assertEqual(result["error"]["code"], "upstream_unavailable")

    def test_search_cursor_binds_query_limit_mode_filters_and_pin(self):
        server = self.serve(reply({"DataElements": [element(), element("456")]}))
        first = self.call(server, "search_data_elements", query="Q", limit=1)
        base = {"query": "Q", "limit": 1, "cursor": first["nextCursor"]}
        for changed in (
            {"query": "other"},
            {"limit": 2},
            {"mode": "semantic"},
            {"filters": {"context": "TEST"}},
            {"registryRelease": "known"},
        ):
            with self.subTest(changed=changed):
                result = self.call(server, "search_data_elements", **(base | changed))
                self.assertEqual(result["error"]["code"], "invalid_request")
        self.assertEqual(len(server.seen), 1)

    def test_context_paging_keeps_names_and_never_adds_definitions(self):
        server = self.serve(
            reply({"contextNames": ["B", "A"]}), reply({"contextNames": ["B", "A"]})
        )
        first = self.call(server, "list_contexts", limit=1)
        second = self.call(server, "list_contexts", limit=1, cursor=first["nextCursor"])
        self.assertEqual([first["contexts"][0]["name"], second["contexts"][0]["name"]], ["B", "A"])
        self.assertEqual(set(second["contexts"][0]), {"name", "provenance"})
        self.assertNotIn("nextCursor", second)

    def test_cursor_past_the_end_of_a_shrunk_list_is_invalid_not_an_empty_last_page(self):
        server = self.serve(reply({"contextNames": ["B", "A"]}), reply({"contextNames": ["B"]}))
        first = self.call(server, "list_contexts", limit=1)
        second = self.call(server, "list_contexts", limit=1, cursor=first["nextCursor"])
        self.assertEqual(second["error"]["code"], "invalid_request")
        self.assertEqual(second["error"]["details"]["parameter"], "cursor")
        self.assertIn("beyond this list", second["error"]["message"])
        self.assertNotIn("contexts", second)

    def test_empty_context_list_is_short_lived_and_has_provenance(self):
        result = self.call(self.serve(reply({"contextNames": []})), "list_contexts")
        self.assertEqual(result["contexts"], [])
        self.assertEqual(result["provenance"]["release"], {"registry": "cadsr"})
        self.assertEqual(self.hint["ttlMs"], 3_600_000)

    def test_every_request_of_pinned_question_lookup_names_the_registry(self):
        row = {"identifier": "known", "generatedAt": "2026-01-01"}
        server = self.serve(
            reply({"registryReleases": [row]}),
            reply({"DataElements": [{"publicId": "123"}], "registryRelease": "known"}),
            reply({"DataElement": element(), "registryRelease": "known"}),
        )
        with self.assertLogs("nci_sip_mcp", level="INFO") as logs:
            result = self.call(server, questionText="Q", registryRelease="known")
        self.assertEqual(result["publicId"], "123")
        self.assertEqual(
            [parse_qs(urlsplit(path).query)["registryRelease"] for path, _ in server.seen[1:]],
            [["known"], ["known"]],
        )
        completed = next(
            record.structured
            for record in logs.records
            if record.structured["event"] == "call_completed"
        )
        self.assertEqual(completed["release"]["requested"], "known")
        self.assertEqual(completed["outboundRequests"], 3)

    def test_pinned_search_and_context_list_send_and_verify_the_pin(self):
        row = {"identifier": "known", "generatedAt": "2026-01-01"}
        for tool, args, body, key in (
            ("search_data_elements", {"query": "Q"}, {"DataElements": []}, "results"),
            ("list_contexts", {}, {"contextNames": []}, "contexts"),
        ):
            with self.subTest(tool=tool):
                server = self.serve(
                    reply({"registryReleases": [row]}), reply(body | {"registryRelease": "known"})
                )
                result = self.call(server, tool, **args, registryRelease="known")
                self.assertEqual(result[key], [])
                self.assertEqual(result["provenance"]["release"]["identifier"], "known")
                self.assertEqual(
                    parse_qs(urlsplit(server.seen[-1][0]).query)["registryRelease"], ["known"]
                )
                self.assertEqual(self.hint["ttlMs"], 86_400_000)

    def test_invalid_search_options_fail_without_a_request(self):
        for args in (
            {"query": " "},
            {"query": "Q", "filters": []},
            {"query": "Q", "filters": {"context": []}},
            {"query": "Q", "filters": {"other": "x"}},
            {"query": "Q", "mode": "other"},
        ):
            with self.subTest(args=args):
                server = self.serve()
                result = self.call(server, "search_data_elements", **args)
                self.assertEqual(result["error"]["code"], "invalid_request")
                self.assertEqual(server.seen, [])

    def test_invalid_search_identity_is_an_upstream_error_not_returned_content(self):
        server = self.serve(reply({"DataElements": [element(version="wrong")]}))
        result = self.call(server, "search_data_elements", query="Q")
        self.assertEqual(result["error"]["code"], "upstream_unavailable")

    def test_upstream_attribution_passes_through_without_local_licence_data(self):
        raw = element(licenseText="Fixture attribution supplied by the platform")
        result = self.call(self.serve(reply({"DataElement": raw})), publicId="123")
        self.assertEqual(result["provenance"]["attribution"], raw["licenseText"])

    def test_refused_cadsr_access_names_credentials_without_an_evs_remedy(self):
        result = self.call(self.serve(Reply(401)), "list_contexts")
        self.assertEqual(result["error"]["code"], "upstream_unavailable")
        self.assertIn("credentials", result["error"]["message"])
        self.assertNotIn("EVS", result["error"]["message"])

    def test_value_meaning_primary_is_upstream_fact_and_malformed_values_fail(self):
        for value, code, primary, valid in (
            ("A", "C1", "No", True),
            (None, "C1", "Yes", False),
            ("A", None, "Yes", False),
            ("A", "C1", None, False),
        ):
            with self.subTest(value=value, code=code, primary=primary):
                meaning = {
                    "publicId": "801",
                    "version": "1",
                    "longName": "Meaning",
                    "Concepts": [
                        {"conceptCode": code, "longName": "Concept", "primaryIndicator": primary}
                    ],
                }
                raw = element(
                    ValueDomain={
                        "PermissibleValues": [
                            {"publicId": "800", "value": value, "ValueMeaning": meaning},
                        ]
                    }
                )
                result = self.call(
                    self.serve(reply({"DataElement": raw})),
                    publicId="123",
                    include=["permissibleValues"],
                )
                if valid:
                    self.assertIs(
                        result["permissibleValues"][0]["valueMeaning"]["concepts"][0]["primary"],
                        False,
                    )
                else:
                    self.assertEqual(result["error"]["code"], "upstream_unavailable")


PIN = {"identifier": "26.1", "generatedAt": "2026-07-01T22:19"}


class CaDSRContentBoundaryTest(CaDSRFixture):
    def test_versioned_resource_retrieves_named_version_and_records_its_uri(self):
        server = self.serve(reply({"DataElement": element(version="1.5")}))
        result = self.call(server, "data_element_version_resource", publicId="123", version="1.50")
        self.assertEqual(result["version"], "1.5")
        self.assertEqual(server.seen[0][0], "/NCIAPI/1.0/api/DataElement/123?version=1.50")
        self.assertEqual(result["provenance"]["sourceUri"], server.url + server.seen[0][0])

    def test_invalid_arguments_precede_registry_discovery_for_every_content_tool(self):
        cases = (
            ("get_data_element", {}, "publicId"),
            ("get_data_element", {"publicId": "123", "questionText": "Q"}, "publicId"),
            ("get_data_element", {"publicId": ""}, "publicId"),
            ("get_data_element", {"publicId": "1/2"}, "publicId"),
            ("get_data_element", {"questionText": " "}, "questionText"),
            ("get_data_element", {"publicId": "0"}, "publicId"),
            ("get_data_element", {"publicId": 123}, "publicId"),
            ("get_data_element", {"questionText": 123}, "questionText"),
            ("get_data_element", {"publicId": "123", "version": "1.x"}, "version"),
            ("get_data_element", {"publicId": "123", "include": ["bad"]}, "include"),
            ("search_data_elements", {"query": "", "mode": "semantic"}, "query"),
            ("search_data_elements", {"query": "Q", "limit": 0}, "limit"),
            ("search_data_elements", {"query": "Q", "cursor": "bad"}, "cursor"),
            ("list_contexts", {"limit": 0}, "limit"),
            ("list_contexts", {"cursor": "bad"}, "cursor"),
            ("list_classification_schemes", {"limit": 0}, "limit"),
            ("list_classification_schemes", {"cursor": "bad"}, "cursor"),
            ("get_form", {"publicId": "123", "version": "1.x"}, "version"),
            ("get_form", {"publicId": "123", "keyword": "Q"}, "keyword"),
            ("get_permissible_value", {"permissibleValueId": "0"}, "permissibleValueId"),
            ("get_code_map", {"dataElementId": "0"}, "dataElementId"),
            ("get_code_map", {"limit": 0}, "limit"),
            ("get_code_map", {"cursor": "bad"}, "cursor"),
        )
        for tool, args, parameter in cases:
            with self.subTest(tool=tool, args=args):
                server = self.serve(reply({"registryReleases": []}))
                result = self.call(server, tool, registryRelease="unlisted", **args)
                self.assertEqual(result["error"]["code"], "invalid_request")
                self.assertEqual(result["error"]["details"]["parameter"], parameter)
                self.assertEqual(server.seen, [])

    def test_classification_context_is_validated_before_pin_and_capability(self):
        for value in ("", " ", 2, []):
            with self.subTest(value=value):
                server = self.serve(reply({"registryReleases": []}))
                result = self.call(
                    server, "list_classification_schemes", context=value, registryRelease="unknown"
                )
                self.assertEqual(result["error"]["code"], "invalid_request")
                self.assertEqual(result["error"]["details"]["parameter"], "context")
                self.assertEqual(server.seen, [])

    def test_unlisted_pin_precedes_unavailable_capability(self):
        for tool, args in (
            ("search_data_elements", {"query": "Q", "mode": "semantic"}),
            ("list_classification_schemes", {"context": "TEST"}),
        ):
            with self.subTest(tool=tool):
                server = self.serve(reply({"registryReleases": []}))
                result = self.call(server, tool, registryRelease="unlisted", **args)
                self.assertEqual(result["error"]["code"], "release_not_available")
                self.assertEqual(len(server.seen), 1)

    def test_blank_and_nontext_pins_are_invalid_without_discovery(self):
        for pin in ("", 26, []):
            with self.subTest(pin=pin):
                server = self.serve(reply({"DataElement": element()}))
                result = self.call(server, publicId="123", registryRelease=pin)
                self.assertEqual(result["error"]["code"], "invalid_request")
                self.assertEqual(result["error"]["details"]["parameter"], "registryRelease")
                self.assertEqual(server.seen, [])

    def test_pin_identity_is_exact_and_case_sensitive(self):
        for listed, requested in (("26.1", "26"), ("Release", "release")):
            with self.subTest(listed=listed):
                server = self.serve(reply({"registryReleases": [PIN | {"identifier": listed}]}))
                result = self.call(server, publicId="123", registryRelease=requested)
                self.assertEqual(result["error"]["code"], "release_not_available")
                self.assertEqual(result["error"]["details"]["requested"], requested)
                self.assertEqual(len(server.seen), 1)

    def test_question_versions_are_one_candidate_and_text_is_verbatim(self):
        server = self.serve(
            reply(
                {
                    "DataElements": [
                        {"publicId": "123", "version": "1"},
                        {"publicId": "123", "version": "2"},
                    ]
                }
            ),
            reply({"DataElement": element()}),
        )
        result = self.call(server, questionText="  A & B  ")
        self.assertNotIn("error", result)
        self.assertEqual(result["publicId"], "123")
        self.assertEqual(parse_qs(urlsplit(server.seen[0][0]).query)["documentText"], ["  A & B  "])

    def test_ambiguous_question_names_the_argument(self):
        server = self.serve(reply({"DataElements": [{"publicId": "123"}, {"publicId": "124"}]}))
        result = self.call(server, questionText="Q")
        self.assertEqual(result["error"]["details"]["parameter"], "questionText")

    def test_multiple_requested_sections_are_all_returned(self):
        raw = element(AlternateNames=[{"name": "Alias"}], ValueDomain={"type": "Text"})
        result = self.call(
            self.serve(reply({"DataElement": raw})),
            publicId="123",
            include=["alternateNames", "valueDomain"],
        )
        self.assertTrue({"alternateNames", "valueDomain"}.issubset(result))
        self.assertEqual(result["alternateNames"], [{"name": "Alias"}])
        self.assertEqual(result["valueDomain"], {"type": "Text"})

    def test_invalid_nested_concepts_fail_with_cadsr_surface(self):
        for code, primary in (("C1", "Maybe"), (" ", "Yes")):
            with self.subTest(code=code, primary=primary):
                meaning = {
                    "publicId": "801",
                    "version": "1",
                    "longName": "Meaning",
                    "Concepts": [
                        {"conceptCode": code, "longName": "Concept", "primaryIndicator": primary}
                    ],
                }
                raw = element(
                    ValueDomain={
                        "PermissibleValues": [
                            {"publicId": "800", "value": "A", "ValueMeaning": meaning}
                        ]
                    }
                )
                result = self.call(
                    self.serve(reply({"DataElement": raw})),
                    publicId="123",
                    include=["permissibleValues"],
                )
                self.assertIn("error", result)
                self.assertEqual(result["error"]["code"], "upstream_unavailable")
                self.assertEqual(result["error"]["details"]["surface"], "cadsr")

    def test_nonobject_domain_fails_with_cadsr_surface(self):
        result = self.call(
            self.serve(reply({"DataElement": element(ValueDomain=[])})),
            publicId="123",
            include=["valueDomain"],
        )
        self.assertIn("error", result)
        self.assertEqual(result["error"]["code"], "upstream_unavailable")
        self.assertEqual(result["error"]["details"]["surface"], "cadsr")

    def test_pinned_content_provenance_records_source_uri_and_modified_date(self):
        cases = (
            (
                "get_data_element",
                {"publicId": "123", "version": "2"},
                {"DataElement": element()},
                None,
            ),
            ("search_data_elements", {"query": "Q"}, {"DataElements": [element()]}, "results"),
            ("list_contexts", {}, {"contextNames": ["TEST"]}, "contexts"),
            ("get_code_map", {}, {"CRDCDataElements": [code_map()]}, "codeMaps"),
        )
        for tool, args, payload, key in cases:
            with self.subTest(tool=tool):
                server = self.serve(
                    reply({"registryReleases": [PIN]}),
                    reply(payload | {"registryRelease": PIN["identifier"]}),
                )
                result = self.call(server, tool, registryRelease=PIN["identifier"], **args)
                item = result if key is None else result[key][0]
                item = item.get("dataElement", item) if key == "results" else item
                provenance = item["provenance"]
                self.assertEqual(provenance["source"], "cadsr_rest")
                self.assertEqual(provenance["sourceUri"], server.url + server.seen[-1][0])
                self.assertEqual(
                    parse_qs(urlsplit(provenance["sourceUri"]).query)["registryRelease"], ["26.1"]
                )
                if key in (None, "results"):
                    self.assertEqual(
                        provenance["upstream"],
                        {
                            "publicId": "123",
                            "version": "2",
                            "dateModified": element()["dateModified"],
                        },
                    )
                if key is not None:
                    self.assertNotIn("provenance", result)

    def test_export_resource_provenance_uses_export_folder(self):
        server = self.serve(reply({"registryReleases": []}))
        export = self.serve(Reply(body=LISTING.encode()))
        context = self.context(server)
        context.cadsr.export_http.base_url = export.url
        result = invoke(context, "registry_resource")
        self.assertEqual(result["provenance"]["source"], "cadsr_export")
        self.assertEqual(result["provenance"]["sourceUri"], export.url + EXPORT_FOLDER)

    def test_capability_details_name_the_unserved_operation(self):
        for tool, args, expected in (
            ("get_data_element", {"longName": "Name"}, "longName lookup (OP-C02)"),
            (
                "search_data_elements",
                {"query": "Q", "mode": "semantic"},
                "semantic data-element search (OP-C04)",
            ),
            ("get_form", {"publicId": "123", "registryRelease": "26.1"}, "pinned form lookup"),
        ):
            with self.subTest(tool=tool):
                server = self.serve(reply({"registryReleases": [PIN]}))
                result = self.call(server, tool, **args)
                self.assertEqual(result["error"]["code"], "capability_unavailable")
                self.assertEqual(result["error"]["details"]["capability"], expected)

    def test_every_unpinnable_capability_refuses_a_published_pin_in_one_wording(self):
        refusals = {}
        for tool, args in (
            ("get_form", {"publicId": "123"}),
            ("match_data_elements", {"entities": [{"name": "Q"}]}),
            ("match_value_meanings", {"values": ["Q"]}),
            ("ground_value", {"conceptCode": "C1"}),
        ):
            server = self.serve(reply({"registryReleases": [PIN]}))
            error = self.call(server, tool, registryRelease="26.1", **args)["error"]
            self.assertEqual(error["code"], "capability_unavailable")
            refusals[tool] = error["message"].replace(error["details"]["capability"], "<it>")
        self.assertEqual(len(set(refusals.values())), 1, refusals)
        self.assertIn("C-1", refusals["get_form"])
        self.assertIn("Omit registryRelease", refusals["get_form"])

    def test_search_page_size_caps_at_one_hundred(self):
        rows = [element(str(i + 1)) for i in range(102)]
        for limit in (100, 101):
            with self.subTest(limit=limit):
                result = self.call(
                    self.serve(reply({"DataElements": rows})),
                    "search_data_elements",
                    query="Q",
                    limit=limit,
                )
                self.assertEqual(len(result["results"]), 100)
                self.assertIn("nextCursor", result)

    def test_search_cap_boundaries_counts_and_final_page(self):
        for length, count, capped in ((999, 999, False), (1000, 2500, True), (1001, 2500, True)):
            with self.subTest(length=length):
                rows = [element(str(i + 1)) for i in range(length)]
                args = {
                    "tool": "search_data_elements",
                    "query": "Q",
                    "mode": "lexical",
                    "filters": {},
                    "limit": 100,
                    "registryRelease": None,
                }
                result = self.call(
                    self.serve(reply({"DataElements": rows, "numRecords": count})),
                    "search_data_elements",
                    query="Q",
                    limit=100,
                    cursor=cursor.encode(args, 900),
                )
                self.assertEqual(len(result["results"]), min(length, 1000) - 900)
                self.assertNotIn("nextCursor", result)
                self.assertEqual(result["totalKnown"], count)
                expected = (
                    {"occurred": False}
                    if not capped
                    else {
                        "occurred": True,
                        "bound": "upstream_cap",
                        "limit": 1000,
                        "reached": 1000,
                        "omitted": 1500,
                        "exact": False,
                    }
                )
                self.assertEqual(result["truncation"], expected)

    def test_context_page_limit_is_one_thousand_and_cursor_binds_pin_and_limit(self):
        server = self.serve(reply({"contextNames": [str(i) for i in range(1001)]}))
        result = self.call(server, "list_contexts", limit=1001)
        self.assertEqual(len(result["contexts"]), 1000)
        for change in ({"limit": 999}, {"registryRelease": "26.1"}):
            with self.subTest(change=change):
                changed = self.call(
                    server,
                    "list_contexts",
                    **({"limit": 1000, "cursor": result["nextCursor"]} | change),
                )
                self.assertEqual(changed["error"]["code"], "invalid_request")
        self.assertEqual(len(server.seen), 1)

    def test_classification_cursor_binds_context_and_clamped_limit(self):
        args = {
            "tool": "list_classification_schemes",
            "context": "TEST",
            "limit": 1000,
            "registryRelease": None,
        }
        token = cursor.encode(args, 1000)
        server = self.serve()
        result = self.call(
            server, "list_classification_schemes", context="TEST", limit=1001, cursor=token
        )
        self.assertEqual(result["error"]["code"], "capability_unavailable")
        result = self.call(
            server, "list_classification_schemes", context="OTHER", limit=1000, cursor=token
        )
        self.assertEqual(result["error"]["code"], "invalid_request")
        self.assertEqual(server.seen, [])
