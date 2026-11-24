"""Forms and CRDC maps against contract-crafted HTTP responses."""

import json
from urllib.parse import parse_qs, urlsplit

from fakes import code_map, form
from test_cadsr_client import reply
from test_cadsr_content import CaDSRFixture
from test_http_client import Reply


class FormsTest(CaDSRFixture):
    def test_crosswalk_resource_uses_the_maximum_page_and_reports_exact_truncation(self):
        for size, truncated in ((0, False), (105, False), (1001, True)):
            with self.subTest(size=size):
                server = self.serve(
                    reply({"CRDCDataElements": [code_map(str(i + 1)) for i in range(size)]})
                )
                result = self.call(server, "crosswalk_resource")
                self.assertEqual(len(result["codeMaps"]), min(size, 1000))
                if truncated:
                    self.assertEqual(
                        result["truncation"],
                        {
                            "occurred": True,
                            "bound": "results",
                            "limit": 1000,
                            "reached": 1000,
                            "omitted": 1,
                            "exact": True,
                        },
                    )
                else:
                    self.assertNotIn("truncation", result)
                self.assertEqual(self.hint, {"ttlMs": 3_600_000, "cacheScope": "public"})

    def test_form_preserves_identity_modules_and_retired_status_without_enrichment(self):
        raw = form()
        server = self.serve(reply({"form": raw}))
        result = self.call(server, "get_form", publicId="123", version="2")
        self.assertEqual(result["publicId"], "123")
        self.assertEqual(result["modules"], raw["modules"])
        self.assertEqual(result["workflowStatus"], "RETIRED ARCHIVED")
        self.assertEqual(
            result["provenance"]["upstream"],
            {"publicID": "123", "version": "2", "dateModified": raw["dateModified"]},
        )
        self.assertEqual(self.hint, {"ttlMs": 3_600_000, "cacheScope": "public"})
        self.assertEqual(parse_qs(urlsplit(server.seen[0][0]).query), {"version": ["2"]})

    def test_excluded_modules_are_absent(self):
        result = self.call(
            self.serve(reply({"form": form()})), "get_form", publicId="123", includeModules=False
        )
        self.assertNotIn("modules", result)
        self.assertEqual(result["version"], "2")

    def test_form_empty_error_is_not_found_without_message_matching_or_another_request(self):
        server = self.serve(
            reply({"form": None, "apiResponse": {"type": "E", "message": "arbitrary text"}})
        )
        result = self.call(server, "get_form", publicId="123")
        self.assertEqual(result["error"]["code"], "not_found")
        self.assertEqual(result["error"]["details"], {"identifiers": ["123"]})
        self.assertEqual(len(server.seen), 1)

    def test_other_form_error_shapes_and_statuses_stay_upstream_failures(self):
        cases = [
            reply({"apiResponse": {"type": "E"}}),
            reply({"form": form(), "apiResponse": {"type": "E"}}),
            Reply(201, json.dumps({"form": None, "apiResponse": {"type": "E"}}).encode()),
            Reply(200, b"<html>Error</html>"),
            reply([]),
        ]
        for response in cases:
            with self.subTest(response=response):
                result = self.call(self.serve(response), "get_form", publicId="123")
                self.assertEqual(result["error"]["code"], "upstream_unavailable")

    def test_a_form_body_with_an_integer_beyond_the_json_digit_limit_is_an_upstream_failure(self):
        server = self.serve(Reply(200, b"1" * 5000))

        try:
            result = self.call(server, "get_form", publicId="123")
        except ValueError as error:
            result = {"error": {"code": type(error).__name__}}

        self.assertEqual(result["error"]["code"], "upstream_unavailable")
        self.assertIn("invalid JSON", result["error"]["message"])
        self.assertEqual(len(server.seen), 1)

    def test_identical_form_error_from_another_operation_is_not_reinterpreted(self):
        result = self.call(
            self.serve(reply({"form": None, "apiResponse": {"type": "E"}})), publicId="123"
        )
        self.assertEqual(result["error"]["code"], "upstream_unavailable")

    def test_nonerror_null_form_is_not_found_and_missing_or_wrong_records_fail(self):
        result = self.call(
            self.serve(reply({"form": None, "apiResponse": {"type": "I"}})),
            "get_form",
            publicId="123",
        )
        self.assertEqual(result["error"]["code"], "not_found")
        self.assertEqual(result["error"]["details"], {"identifiers": ["123"]})
        raw = form()
        del raw["modules"]
        for response in (form(publicID="124"), form(version="3"), raw):
            with self.subTest(response=response):
                result = self.call(
                    self.serve(reply({"form": response})), "get_form", publicId="123", version="2"
                )
                self.assertEqual(result["error"]["code"], "upstream_unavailable")

    def test_form_and_value_invalid_arguments_do_not_send_requests(self):
        server = self.serve()
        cases = [
            ("get_form", {"keyword": "Q"}),
            ("get_form", {}),
            ("get_form", {"publicId": "bad"}),
            ("get_form", {"publicId": "123", "version": "bad"}),
            ("get_form", {"publicId": "123", "includeModules": "false"}),
            ("get_permissible_value", {"permissibleValueId": "bad", "registryRelease": "pin"}),
        ]
        for operation, args in cases:
            with self.subTest(operation=operation, args=args):
                result = self.call(server, operation, **args)
                self.assertEqual(result["error"]["code"], "invalid_request")
        self.assertEqual(server.seen, [])

    def test_form_and_value_pins_fail_closed_before_any_content(self):
        row = {"identifier": "known", "generatedAt": "2026-07-01T22:19"}
        for operation, args in (
            ("get_form", {"publicId": "123"}),
            ("get_permissible_value", {"permissibleValueId": "456"}),
        ):
            for rows, code in (([], "release_not_available"), ([row], "capability_unavailable")):
                with self.subTest(operation=operation, code=code):
                    server = self.serve(reply({"registryReleases": rows}))
                    result = self.call(server, operation, registryRelease="known", **args)
                    self.assertEqual(result["error"]["code"], code)
                    self.assertEqual(len(server.seen), 1)
                    self.assertIn("registry/releases", server.seen[0][0])

    def test_permissible_value_refuses_the_missing_operation_not_the_identifier(self):
        server = self.serve()
        result = self.call(server, "get_permissible_value", permissibleValueId="456")
        self.assertEqual(result["error"]["code"], "capability_unavailable")
        self.assertIn("OP-C10", result["error"]["message"])
        self.assertEqual(server.seen, [])

    def test_code_maps_preserve_values_users_coverage_and_rows_without_binding(self):
        unbound = code_map("124")
        del unbound["permissibleValues"]
        server = self.serve(reply({"CRDCDataElements": [code_map(), unbound]}))
        result = self.call(server, "get_code_map")
        first, second = result["codeMaps"]
        self.assertEqual(first["dataElement"], {"publicId": "123", "version": "2"})
        self.assertEqual(first["usedBy"], ["CRDC", "CIP"])
        self.assertEqual(first["coverage"], 1)
        self.assertEqual(first["values"], [{"value": "A", "conceptCode": "C1:C2"}, {"value": "B"}])
        self.assertEqual(
            (second["valueLevelBinding"], second["values"], second["coverage"]), (False, [], 0)
        )
        self.assertEqual(first["provenance"]["upstream"], {"CDE Public ID": "123", "Version": "2"})
        self.assertEqual(len(server.seen), 1)

    def test_exact_filters_apply_before_paging_and_cursors_bind_every_argument(self):
        rows = [code_map("123", **{"Used By": "NCIP"}), code_map("124"), code_map("125")]
        server = self.serve(*[reply({"CRDCDataElements": rows}) for _ in range(3)])
        first = self.call(server, "get_code_map", targetContext="CIP", limit=1)
        self.assertEqual(first["codeMaps"][0]["dataElement"]["publicId"], "124")
        second = self.call(
            server, "get_code_map", targetContext="CIP", limit=1, cursor=first["nextCursor"]
        )
        self.assertEqual(second["codeMaps"][0]["dataElement"]["publicId"], "125")
        self.assertNotIn("nextCursor", second)
        one = self.call(server, "get_code_map", targetContext="CIP", dataElementId="125")
        self.assertEqual(len(one["codeMaps"]), 1)
        for change in (
            {"targetContext": "NCIP"},
            {"dataElementId": "124"},
            {"limit": 2},
            {"registryRelease": "known"},
        ):
            with self.subTest(change=change):
                result = self.call(
                    server,
                    "get_code_map",
                    **(
                        {"targetContext": "CIP", "limit": 1, "cursor": first["nextCursor"]} | change
                    ),
                )
                self.assertEqual(result["error"]["code"], "invalid_request")
        self.assertEqual(len(server.seen), 3)

    def test_empty_map_page_has_provenance_and_no_synthetic_map(self):
        result = self.call(
            self.serve(reply({"CRDCDataElements": [code_map()]})),
            "get_code_map",
            targetContext="OTHER",
        )
        self.assertEqual(result["codeMaps"], [])
        self.assertEqual(result["provenance"]["release"], {"registry": "cadsr"})
        self.assertEqual(self.hint, {"ttlMs": 3_600_000, "cacheScope": "public"})

    def test_map_validation_and_bad_values_are_errors(self):
        server = self.serve()
        for args in (
            {"sourceSystem": "OTHER"},
            {"targetContext": ""},
            {"dataElementId": "bad"},
            {"limit": 0},
        ):
            with self.subTest(args=args):
                result = self.call(server, "get_code_map", **args)
                self.assertEqual(result["error"]["code"], "invalid_request")
        self.assertEqual(server.seen, [])
        raw = code_map(permissibleValues=[{"Permissible Value": "A", "Concept Code": 5}])
        result = self.call(self.serve(reply({"CRDCDataElements": [raw]})), "get_code_map")
        self.assertEqual(result["error"]["code"], "upstream_unavailable")

    def test_verified_map_pin_is_sent_and_confirmed_and_has_long_cache(self):
        row = {"identifier": "known", "generatedAt": "2026-07-01T22:19"}
        server = self.serve(
            reply({"registryReleases": [row]}),
            reply({"CRDCDataElements": [code_map()], "registryRelease": "known"}),
        )
        result = self.call(server, "get_code_map", registryRelease="known")
        self.assertEqual(
            parse_qs(urlsplit(server.seen[1][0]).query), {"registryRelease": ["known"]}
        )
        self.assertEqual(result["codeMaps"][0]["provenance"]["release"]["identifier"], "known")
        self.assertEqual(self.hint, {"ttlMs": 86_400_000, "cacheScope": "public"})
        for pin in (None, "different"):
            with self.subTest(pin=pin):
                server = self.serve(
                    reply({"registryReleases": [row]}),
                    reply({"CRDCDataElements": [code_map()], "registryRelease": pin}),
                )
                result = self.call(server, "get_code_map", registryRelease="known")
                self.assertEqual(result["error"]["code"], "release_mismatch")


class FormAndCodeMapBoundaryTest(CaDSRFixture):
    def test_form_provenance_uri_keeps_version(self):
        server = self.serve(reply({"form": form()}))
        result = self.call(server, "get_form", publicId="123", version="2.00")
        self.assertEqual(result["provenance"]["source"], "cadsr_rest")
        self.assertEqual(result["provenance"]["sourceUri"], server.url + server.seen[0][0])
        self.assertEqual(
            parse_qs(urlsplit(result["provenance"]["sourceUri"]).query), {"version": ["2.00"]}
        )

    def test_code_map_single_unmapped_value_has_zero_coverage(self):
        raw = code_map(permissibleValues=[{"Permissible Value": "A", "Concept Code": ""}])
        result = self.call(self.serve(reply({"CRDCDataElements": [raw]})), "get_code_map")[
            "codeMaps"
        ][0]
        self.assertEqual(result["crdcName"], "Map")
        self.assertIs(result["valueLevelBinding"], True)
        self.assertEqual(result["coverage"], 0)
        self.assertEqual(result["values"], [{"value": "A"}])

    def test_malformed_code_map_fields_fail_instead_of_returning_content(self):
        for changes in (
            {"permissibleValues": [{"Concept Code": "C1"}]},
            {"CDE Public ID": "0"},
            {"Version": "1.x"},
            {"permissibleValues": {}},
            {"CRDC Name": 1},
            {"Used By": []},
        ):
            with self.subTest(changes=changes):
                result = self.call(
                    self.serve(reply({"CRDCDataElements": [code_map(**changes)]})), "get_code_map"
                )
                self.assertIn("error", result)
                self.assertEqual(result["error"]["code"], "upstream_unavailable")
                self.assertEqual(result["error"]["details"]["surface"], "cadsr")

    def test_code_map_limits_follow_spec_and_filters_are_exact(self):
        rows = [code_map(str(i + 1)) for i in range(1001)]
        for limit, expected in ((101, 101), (1000, 1000), (1001, 1000)):
            with self.subTest(limit=limit):
                result = self.call(
                    self.serve(reply({"CRDCDataElements": rows})), "get_code_map", limit=limit
                )
                self.assertEqual(len(result["codeMaps"]), expected)
        for target, identifier, expected in (("cip", "12", []), ("CIP", "12", ["12"])):
            with self.subTest(target=target):
                result = self.call(
                    self.serve(reply({"CRDCDataElements": [code_map("12"), code_map("123")]})),
                    "get_code_map",
                    targetContext=target,
                    dataElementId=identifier,
                )
                self.assertEqual(
                    [row["dataElement"]["publicId"] for row in result["codeMaps"]], expected
                )

    def test_crosswalk_exact_cap_is_complete_and_only_empty_has_top_provenance(self):
        for size in (0, 1000):
            with self.subTest(size=size):
                result = self.call(
                    self.serve(
                        reply({"CRDCDataElements": [code_map(str(i + 1)) for i in range(size)]})
                    ),
                    "crosswalk_resource",
                )
                self.assertEqual(len(result["codeMaps"]), size)
                self.assertNotIn("truncation", result)
                self.assertEqual("provenance" in result, size == 0)
