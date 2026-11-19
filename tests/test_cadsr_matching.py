"""Matching behavior against crafted published-contract responses, without credentials."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fakes import cde_response, cde_row, vm_response, vm_row
from nci_sip_mcp.caching import cache_call
from nci_sip_mcp.cadsr import CDE_MATCH, VM_MATCH
from nci_sip_mcp.config import Settings
from nci_sip_mcp.context import Context
from nci_sip_mcp.registry import invoke
from test_cadsr_client import reply
from test_http_client import Reply, ServerTestCase


class MatchingTest(ServerTestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)

    def context(self, server, **settings):
        return Context(Settings(data_dir=self.directory, cadsr_base_url=server.url, **settings))

    def call(self, server, operation="match_data_elements", **args):
        with cache_call() as hint:
            result = invoke(self.context(server), operation, **args)
        self.hint = hint
        return result

    def test_each_entity_is_a_single_object_with_exact_text_and_bounded_ordered_matches(self):
        name = "Q é\n&?"
        server = self.serve(
            reply(cde_response(name, [cde_row(), cde_row("124")])),
            reply(cde_response("second", [cde_row("125")])),
        )
        result = self.call(
            server,
            entities=[
                {"name": name, "userTip": "é tip\n", "permissibleValues": ["v &"]},
                {"name": "second"},
            ],
            matchLimit=1,
        )
        self.assertEqual(
            [(m["entity"], m["dataElement"]["publicId"]) for m in result["matches"]],
            [(name, "123"), ("second", "125")],
        )
        self.assertEqual(
            [json.loads(b) for b in server.bodies],
            [
                {"entity": name, "entityUserTip": "é tip\n", "pvvmData": [{"name": "v &"}]},
                {"entity": "second"},
            ],
        )
        self.assertEqual([p for p, _ in server.seen], [CDE_MATCH, CDE_MATCH])
        self.assertEqual([h["matchlimit"] for _, h in server.seen], ["1", "1"])
        self.assertEqual(result["matches"][0]["score"], 0.75)
        self.assertEqual(result["matches"][0]["rule"], "Exact")
        self.assertEqual(result["matches"][0]["matchedText"], "Q")
        self.assertEqual(self.hint, {"ttlMs": 0, "cacheScope": "private"})

    def test_all_supported_filters_reach_their_headers_and_limit_is_capped(self):
        server = self.serve(reply(cde_response()))
        filters = {
            "context": "TEST",
            "workflowStatus": "RELEASED",
            "registrationStatus": "Qualified",
            "valueDomainType": "Enumerated",
            "classificationScheme": {"publicId": "123", "version": "2.0"},
        }
        result = self.call(server, entities=[{"name": "Q"}], matchLimit=101, filters=filters)
        headers = server.seen[0][1]
        self.assertEqual(
            {
                k: headers[k.lower()]
                for k in (
                    "contexts",
                    "workflowStatus",
                    "registrationStatus",
                    "valueDomainType",
                    "classificationSchemePublicId",
                    "classificationSchemeVersion",
                    "matchLimit",
                )
            },
            {
                "contexts": "TEST",
                "workflowStatus": "RELEASED",
                "registrationStatus": "Qualified",
                "valueDomainType": "Enumerated",
                "classificationSchemePublicId": "123",
                "classificationSchemeVersion": "2.0",
                "matchLimit": "100",
            },
        )
        self.assertEqual(result["matches"], [])
        self.assertEqual(result["provenance"]["release"], {"registry": "cadsr"})

    def test_duplicate_entities_reuse_the_same_request_but_keep_both_result_positions(self):
        server = self.serve(reply(cde_response(matches=[cde_row()])))
        result = self.call(server, entities=[{"name": "Q"}, {"name": "Q"}])
        self.assertEqual([match["entity"] for match in result["matches"]], ["Q", "Q"])
        self.assertEqual([json.loads(body) for body in server.bodies], [{"entity": "Q"}])

    def test_rejected_model_controls_cite_the_upstream_requirement(self):
        server = self.serve()
        result = self.call(server, entities=[{"name": "Q"}], modelVariant="model")
        self.assertIn("requirements package C-6", result["error"]["message"])
        self.assertEqual(server.seen, [])

    def test_same_name_with_different_descriptions_uses_distinct_matching_requests(self):
        entities = [
            {"name": "Q", "userTip": "first", "permissibleValues": ["A"]},
            {"name": "Q", "userTip": "second", "permissibleValues": ["A"]},
            {"name": "Q", "userTip": "second", "permissibleValues": ["B"]},
        ]
        identifiers = ["123", "124", "125"]
        server = self.serve(*(reply(cde_response(matches=[cde_row(code)])) for code in identifiers))
        result = self.call(server, entities=entities)
        self.assertEqual(
            [match["dataElement"]["publicId"] for match in result["matches"]], identifiers
        )
        self.assertEqual(
            [json.loads(body) for body in server.bodies],
            [
                {"entity": "Q", "entityUserTip": "first", "pvvmData": [{"name": "A"}]},
                {"entity": "Q", "entityUserTip": "second", "pvvmData": [{"name": "A"}]},
                {"entity": "Q", "entityUserTip": "second", "pvvmData": [{"name": "B"}]},
            ],
        )

    def test_a_later_entity_failure_is_not_a_partial_success(self):
        server = self.serve(
            reply(cde_response("Q", [cde_row()])), reply({"apiResponse": {"type": "E"}})
        )
        result = self.call(server, entities=[{"name": "Q"}, {"name": "second"}])
        self.assertEqual(result["error"]["code"], "upstream_unavailable")
        self.assertNotIn("matches", result)
        self.assertEqual(len(server.bodies), 2)

    def test_malformed_cde_matches_are_errors_not_incomplete_results(self):
        for response in (
            cde_response("other"),
            {"matchResults": {"entity": "Q"}},
            cde_response(matches=[cde_row(score=True)]),
            cde_response(matches=[cde_row(ruleDescription=None)]),
        ):
            with self.subTest(response=response):
                server = self.serve(reply(response))
                result = self.call(server, entities=[{"name": "Q"}])
                self.assertEqual(result["error"]["code"], "upstream_unavailable")

    def test_vm_preserves_wire_headers_item_identity_and_real_crosswalk(self):
        row = vm_row(crosswalkCode="mapped", crosswalkDescription="Mapping", score=0.25)
        server = self.serve(reply(vm_response("Q é\n", [row])))
        result = self.call(
            server,
            "match_value_meanings",
            values=["Q é\n"],
            strictness="unrestricted",
            terminologyScope=["NCI", "SNOMED"],
        )
        self.assertEqual(json.loads(server.bodies[0]), [{"name": "Q é\n"}])
        path, headers = server.seen[0]
        self.assertEqual(path, VM_MATCH)
        self.assertEqual(
            [headers[k.lower()] for k in ("matchType", "function", "evsTerminologyCodes")],
            ["Unrestricted", "match", "NCI,SNOMED"],
        )
        match = result["matches"][0]
        self.assertEqual(match["crosswalk"], {"code": "mapped", "description": "Mapping"})
        self.assertEqual(match["score"], 0.25)
        self.assertEqual(match["rule"], "Exact")
        self.assertEqual(match["item"]["provenance"]["upstream"], {"itemId": "123", "version": "2"})
        self.assertEqual(match["item"]["workflowStatus"], "RETIRED ARCHIVED")
        self.assertNotIn("registrationStatus", match["item"])

    def test_matching_preserves_finite_integer_scores_without_float_conversion(self):
        score = 10**400
        for operation, args, response in (
            (
                "match_data_elements",
                {"entities": [{"name": "Q"}]},
                cde_response(matches=[cde_row(score=score)]),
            ),
            ("match_value_meanings", {"values": ["Q"]}, vm_response(matches=[vm_row(score=score)])),
        ):
            with self.subTest(operation=operation):
                result = self.call(self.serve(reply(response)), operation, **args)
                self.assertEqual(result["matches"][0]["score"], score)

    def test_nonfinite_scores_fail_the_whole_matching_result(self):
        for score in (float("nan"), float("inf"), float("-inf")):
            for operation, args, response in (
                (
                    "match_data_elements",
                    {"entities": [{"name": "Q"}]},
                    cde_response(matches=[cde_row(), cde_row(score=score)]),
                ),
                (
                    "match_value_meanings",
                    {"values": ["Q"]},
                    vm_response(matches=[vm_row(), vm_row(score=score)]),
                ),
            ):
                with self.subTest(operation=operation, score=score):
                    result = self.call(self.serve(reply(response)), operation, **args)
                    self.assertIn("error", result)
                    self.assertEqual(result["error"]["code"], "upstream_unavailable")
                    self.assertNotIn("matches", result)

    def test_vm_absent_concepts_scores_and_na_crosswalks_remain_absent(self):
        rows = [vm_row(), vm_row(itemType="ValueMeaning", concept=None, evsSource=None)]
        server = self.serve(reply(vm_response(matches=rows)))
        result = self.call(server, "match_value_meanings", values=["Q"])
        first, second = result["matches"]
        self.assertEqual(first["item"]["concept"], "C123")
        self.assertEqual(first["item"]["evsSource"], "NCI_CONCEPT_CODE")
        self.assertEqual(second["item"]["itemType"], "ValueMeaning")
        self.assertNotIn("concept", second["item"])
        self.assertNotIn("evsSource", second["item"])
        self.assertNotIn("score", first)
        self.assertNotIn("crosswalk", first)
        self.assertEqual(server.seen[0][1]["matchtype"], "Restricted")
        self.assertNotIn("evsterminologycodes", server.seen[0][1])

    def test_vm_preserves_platform_group_order_and_checks_every_requested_occurrence(self):
        groups = [
            {"name": "B", "matches": [vm_row(itemId="201")]},
            {"name": "A", "matches": [vm_row(itemId="202")]},
            {"name": "A", "matches": []},
        ]
        server = self.serve(reply({"matchResults": groups}))
        result = self.call(server, "match_value_meanings", values=["A", "B", "A"])
        self.assertEqual([m["item"]["publicId"] for m in result["matches"]], ["201", "202"])
        server = self.serve(reply({"matchResults": groups}))
        result = self.call(server, "match_value_meanings", values=["A", "B", "B"])
        self.assertEqual(result["error"]["code"], "upstream_unavailable")

    def test_vm_missing_groups_or_malformed_matches_never_look_complete(self):
        for response in (
            {"matchResults": []},
            vm_response("other"),
            {"matchResults": [{"name": "Q"}]},
            vm_response(matches=[vm_row(itemType="unknown")]),
            vm_response(matches=[vm_row(matchedName=None)]),
        ):
            with self.subTest(response=response):
                server = self.serve(reply(response))
                result = self.call(server, "match_value_meanings", values=["Q"])
                self.assertEqual(result["error"]["code"], "upstream_unavailable")

    def test_matching_pins_fail_before_any_match_and_distinguish_the_capability_gap(self):
        for operation, args in (
            ("match_data_elements", {"entities": [{"name": "Q"}]}),
            ("match_value_meanings", {"values": ["Q"]}),
        ):
            for rows, code in (
                ([], "release_not_available"),
                (
                    [{"identifier": "known", "generatedAt": "2026-07-01T22:19"}],
                    "capability_unavailable",
                ),
            ):
                with self.subTest(operation=operation, code=code):
                    server = self.serve(reply({"registryReleases": rows}))
                    result = self.call(server, operation, registryRelease="known", **args)
                    self.assertEqual(result["error"]["code"], code)
                    self.assertEqual(server.bodies, [])
                    self.assertEqual(len(server.seen), 1)
                    if rows:
                        self.assertEqual(
                            result["error"]["details"], {"capability": "pinned matching"}
                        )
                        self.assertIn("C-1", result["error"]["message"])

    def test_both_matching_timeouts_name_the_matching_setting_and_keep_details(self):
        for operation, args in (
            ("match_data_elements", {"entities": [{"name": "Q"}]}),
            ("match_value_meanings", {"values": ["Q"]}),
        ):
            with self.subTest(operation=operation):
                context = self.context(self.serve(), match_timeout_seconds=0.01)
                with patch("nci_sip_mcp.http_client._open", side_effect=TimeoutError):
                    result = invoke(context, operation, **args)
                self.assertEqual(result["error"]["code"], "timeout")
                self.assertIn("NCI_SIP_MATCH_TIMEOUT_SECONDS", result["error"]["message"])
                self.assertEqual(result["error"]["details"]["seconds"], 0.01)
                self.assertEqual(result["error"]["details"]["surface"], "cadsr")

    def test_empty_matches_have_complete_provenance_and_no_cache(self):
        server = self.serve(reply(vm_response()))
        result = self.call(server, "match_value_meanings", values=["Q"])
        self.assertEqual(result["matches"], [])
        self.assertEqual(result["provenance"]["release"], {"registry": "cadsr"})
        self.assertTrue(result["provenance"]["sourceUri"].endswith(VM_MATCH))
        self.assertEqual(self.hint, {"ttlMs": 0, "cacheScope": "private"})

    def test_access_denied_names_credentials_and_does_not_retry_a_different_shape(self):
        server = self.serve(Reply(401))
        result = self.call(server, entities=[{"name": "Q"}])
        self.assertEqual(result["error"]["code"], "upstream_unavailable")
        self.assertIn("credentials", result["error"]["message"])
        self.assertEqual([json.loads(body) for body in server.bodies], [{"entity": "Q"}])


class MatchingBoundariesTest(ServerTestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)

    def call(self, server, operation="match_data_elements", **args):
        context = Context(Settings(data_dir=self.directory, cadsr_base_url=server.url))
        return invoke(context, operation, **args)

    def assert_invalid(self, operation, defaults, cases):
        server = self.serve()
        for overrides, parameter in cases:
            with self.subTest(overrides=overrides):
                result = self.call(
                    server, operation, **(defaults | overrides | {"registryRelease": "unlisted"})
                )
                self.assertEqual(result["error"]["code"], "invalid_request")
                self.assertEqual(result["error"]["details"]["parameter"], parameter)
                self.assertEqual(server.seen, [])

    def test_invalid_entities_are_rejected_before_pinned_registry_discovery(self):
        cases = [
            ({"entities": None}, "entities"),
            ({"entities": "Q"}, "entities"),
            ({"entities": []}, "entities"),
            ({"entities": [{"name": "Q"}] * 11}, "entities"),
            ({"entities": [[]]}, "entities"),
            ({"entities": ["Q"]}, "entities"),
            ({"entities": [{"name": "Q", "unknown": "x"}]}, "entities"),
            ({"entities": [{}]}, "entities.name"),
            ({"entities": [{"name": ""}]}, "entities.name"),
            ({"entities": [{"name": " \t\n"}]}, "entities.name"),
            ({"entities": [{"name": 5}]}, "entities.name"),
            ({"matchLimit": 0}, "matchLimit"),
            ({"modelVariant": ""}, "modelVariant"),
            ({"modelVariant": "model"}, "modelVariant"),
            ({"similarityThreshold": 0}, "similarityThreshold"),
            ({"similarityThreshold": 0.5}, "similarityThreshold"),
        ]
        self.assert_invalid("match_data_elements", {"entities": [{"name": "Q"}]}, cases)

    def test_invalid_descriptions_are_rejected_before_pinned_registry_discovery(self):
        cases = [
            ({"entities": [{"name": "Q", "permissibleValues": "x"}]}, "entities.permissibleValues")
        ]
        for value in ("", " \t\n", None, 5):
            cases.append(({"entities": [{"name": "Q", "userTip": value}]}, "entities.userTip"))
            cases.append(
                (
                    {"entities": [{"name": "Q", "permissibleValues": [value]}]},
                    "entities.permissibleValues",
                )
            )
        self.assert_invalid("match_data_elements", {"entities": [{"name": "Q"}]}, cases)

    def test_invalid_filter_headers_are_rejected_before_pinned_registry_discovery(self):
        cases = [
            ({"filters": {key: value}}, f"filters.{key}")
            for key in ("context", "workflowStatus", "registrationStatus", "valueDomainType")
            for value in ("", " \t", None, 5)
        ]
        cases.extend(
            [
                ({"filters": []}, "filters"),
                ({"filters": {"other": "x"}}, "filters"),
                ({"filters": {"context": "a\nb"}}, "filters.context"),
                ({"filters": {"context": "é"}}, "filters.context"),
            ]
        )
        self.assert_invalid("match_data_elements", {"entities": [{"name": "Q"}]}, cases)

    def test_invalid_scheme_identity_is_rejected_before_pinned_registry_discovery(self):
        schemes = [None, [], {"publicId": "123", "version": "1", "extra": "x"}]
        schemes.append({"publicId": "123"})
        schemes.extend(
            {"publicId": value, "version": "1"} for value in (0, 123, "0", "007", "", "bad")
        )
        schemes.extend(
            {"publicId": "123", "version": value} for value in (1, "", "1.", "1.2.3", "bad")
        )
        self.assert_invalid(
            "match_data_elements",
            {"entities": [{"name": "Q"}]},
            [
                ({"filters": {"classificationScheme": value}}, "filters.classificationScheme")
                for value in schemes
            ],
        )

    def test_invalid_values_and_headers_are_rejected_before_pinned_registry_discovery(self):
        cases = [
            ({"values": value}, "values")
            for value in (None, "Q", [], [5], [""], [" \t\n"], ["Q"] * 11)
        ]
        cases.extend(
            ({"terminologyScope": value}, "terminologyScope")
            for value in ([], "NCI", ["NCI,SNOMED"])
        )
        cases.extend(
            ({"terminologyScope": [value]}, "terminologyScope")
            for value in ("é", "NCI\n", "NCI\x00", 5)
        )
        cases.append(({"strictness": "other"}, "strictness"))
        self.assert_invalid("match_value_meanings", {"values": ["Q"]}, cases)

    def test_ten_entities_are_accepted_with_default_match_limit(self):
        names = [f"Q{number}" for number in range(10)]
        server = self.serve(*(reply(cde_response(name, [cde_row()])) for name in names))
        result = self.call(
            server, entities=[{"name": name, "permissibleValues": []} for name in names]
        )
        self.assertIn("matches", result)
        self.assertEqual([match["entity"] for match in result["matches"]], names)
        self.assertEqual([h["matchlimit"] for _, h in server.seen], ["10"] * 10)

    def test_explicit_empty_permissible_values_are_preserved_in_request(self):
        server = self.serve(reply(cde_response(matches=[cde_row()])))
        result = self.call(server, entities=[{"name": "Q", "permissibleValues": []}])
        self.assertIn("matches", result)
        self.assertEqual(
            json.loads(server.bodies[0]),
            {"entity": "Q", "pvvmData": []},
        )

    def test_ten_values_keep_request_occurrences_and_platform_response_order(self):
        names = ["Z", "A", "Z", "B", "C", "D", "E", "F", "G", "H"]
        groups = [
            {"name": name, "matches": [vm_row(itemId=str(100 + i))]} for i, name in enumerate(names)
        ]
        server = self.serve(reply({"matchResults": groups}))
        result = self.call(server, "match_value_meanings", values=names)
        self.assertIn("matches", result)
        self.assertEqual(json.loads(server.bodies[0]), [{"name": name} for name in names])
        self.assertEqual(
            [m["item"]["publicId"] for m in result["matches"]], [str(100 + i) for i in range(10)]
        )

    def test_nonempty_matches_have_only_item_provenance_with_the_correct_endpoint(self):
        cases = (
            (
                "match_data_elements",
                {"entities": [{"name": "Q"}]},
                cde_response(matches=[cde_row()]),
                "dataElement",
                CDE_MATCH,
            ),
            (
                "match_value_meanings",
                {"values": ["Q"]},
                vm_response(matches=[vm_row()]),
                "item",
                VM_MATCH,
            ),
        )
        for operation, args, response, key, endpoint in cases:
            with self.subTest(operation=operation):
                server = self.serve(reply(response))
                result = self.call(server, operation, **args)
                self.assertNotIn("provenance", result)
                provenance = result["matches"][0][key]["provenance"]
                self.assertEqual(provenance["source"], "cadsr_rest")
                self.assertEqual(provenance["sourceUri"], server.url + endpoint)

    def assert_malformed(self, operation, args, responses):
        for response in responses:
            with self.subTest(response=response):
                result = self.call(self.serve(reply(response)), operation, **args)
                self.assertIn("error", result)
                self.assertEqual(result["error"]["code"], "upstream_unavailable")
                self.assertNotIn("matches", result)

    def test_cde_required_match_text_and_score_are_validated(self):
        rows = [
            cde_row(**{key: value})
            for key in ("matchedText", "ruleDescription")
            for value in (None, 5)
        ]
        rows.extend(cde_row(score=value) for value in (None, "0.5"))
        absent = cde_row()
        del absent["score"]
        rows.append(absent)
        self.assert_malformed(
            "match_data_elements",
            {"entities": [{"name": "Q"}]},
            [cde_response(matches=[row]) for row in rows],
        )

    def test_vm_item_identity_and_closed_item_type_are_validated(self):
        rows = [vm_row(itemId=value) for value in (None, 123, "0", "007", "x")]
        rows.extend(vm_row(version=value) for value in (None, 1, "", "1.", "1.2.3"))
        rows.extend(vm_row(itemType=value) for value in ("concept", "DataElement", None))
        self.assert_malformed(
            "match_value_meanings", {"values": ["Q"]}, [vm_response(matches=[row]) for row in rows]
        )

    def test_vm_optional_text_and_required_rule_are_validated(self):
        rows = [
            vm_row(**{key: 5})
            for key in ("concept", "evsSource", "context", "workflowStatus", "registrationStatus")
        ]
        missing = vm_row()
        del missing["ruleDescription"]
        rows.append(missing)
        rows.append(vm_row(ruleDescription=5))
        rows.append(vm_row(crosswalkCode="mapped", crosswalkDescription=None))
        rows.append(vm_row(crosswalkCode=123))
        self.assert_malformed(
            "match_value_meanings", {"values": ["Q"]}, [vm_response(matches=[row]) for row in rows]
        )

    def test_vm_context_and_workflow_status_are_required_never_null(self):
        rows = []
        for key in ("context", "workflowStatus"):
            missing = vm_row()
            del missing[key]
            rows.extend((missing, vm_row(**{key: None})))
        self.assert_malformed(
            "match_value_meanings",
            {"values": ["Q"]},
            [vm_response(matches=[row]) for row in rows],
        )

    def test_vm_present_optional_fields_survive_and_null_scores_and_crosswalks_are_absent(self):
        absent = vm_row(score=None, registrationStatus="Qualified")
        del absent["crosswalkCode"]
        rows = [
            absent,
            vm_row(score=None, crosswalkCode=None, crosswalkDescription=None),
            vm_row(score=None, crosswalkCode="", crosswalkDescription=""),
        ]
        result = self.call(
            self.serve(reply(vm_response(matches=rows))), "match_value_meanings", values=["Q"]
        )
        self.assertIn("matches", result)
        first = result["matches"][0]["item"]
        self.assertTrue({"context", "workflowStatus", "registrationStatus"}.issubset(first))
        self.assertEqual(first["context"], "TEST")
        self.assertEqual(first["workflowStatus"], "RETIRED ARCHIVED")
        self.assertEqual(first["registrationStatus"], "Qualified")
        for match in result["matches"]:
            self.assertNotIn("score", match)
            self.assertNotIn("crosswalk", match)
