"""Workflows preserve their constituent tools' results, states and independent bounds."""

import hashlib
import json
import logging
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fakes import FakeEVS, FakeSSIS, cde_response, cde_row, concept, release, vm_response, vm_row
from nci_sip_mcp.bounds import Budget, current_budget
from nci_sip_mcp.caching import cache_call
from nci_sip_mcp.config import Settings
from nci_sip_mcp.context import Context
from nci_sip_mcp.http_client import UpstreamTooLargeError
from nci_sip_mcp.registry import invoke
from nci_sip_mcp.release_selection import SessionRelease, session_scope
from test_bounds import BudgetEVS
from test_cadsr_client import reply
from test_http_client import Reply, ServerTestCase
from test_seam import NCIT, element, gdc_map

HOP_MAXIMUM = 1000


def role(target, code="R135"):
    return {"code": code, "type": "Excludes", "relatedCode": target, "relatedName": target}


class WorkflowFixture(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.settings = Settings(data_dir=Path(directory.name))
        self.evs = FakeEVS([concept("C1", active=True), concept("C2", active=True)])
        self.ssis = FakeSSIS(self.settings)
        self.context = Context(self.settings, evs=self.evs, ssis=self.ssis)
        self.addCleanup(patch.stopall)
        patch(
            "nci_sip_mcp.http_client._open", side_effect=AssertionError("Unexpected HTTP")
        ).start()

    def call(self, operation="ground_value", **arguments):
        with cache_call() as hint:
            result = invoke(self.context, operation, _correlation_id="workflow", **arguments)
        self.hint = hint
        return result


class GroundTest(WorkflowFixture):
    def test_empty_hops_are_success_but_no_commons_omits_stored_values(self):
        result = self.call(conceptCode="C1", release="26.06e")
        self.assertEqual(result["concept"]["code"], "C1")
        self.assertEqual(result["dataElements"], [])
        self.assertEqual(result["permissibleValues"], [])
        self.assertNotIn("storedValues", result)
        self.assertEqual(result["truncation"], {"occurred": False})
        self.assertEqual(result["provenance"]["registry"], {"registry": "cadsr"})
        self.assertEqual(self.hint, {"ttlMs": 3_600_000, "cacheScope": "public"})

    def test_each_sentinel_cut_is_independent_with_full_hop_records(self):
        for element_count, value_count in ((1001, 1), (1, 1001), (1001, 1001), (1000, 1000)):
            with self.subTest(elements=element_count, values=value_count):
                self.ssis.elements = [element(i) for i in range(1, element_count + 1)]
                self.ssis.values = [
                    {"id": "123", "version": "1", "value": str(i), "concept": NCIT + "C1"}
                    for i in range(value_count)
                ]
                result = self.call(conceptCode="C1")
                self.assertEqual(len(result["dataElements"]), min(element_count, 1000))
                self.assertEqual(len(result["permissibleValues"]), min(value_count, 1000))
                value_use = result["permissibleValues"][0]
                self.assertEqual(
                    set(value_use),
                    {"dataElement", "value", "conceptCode", "conceptTerminology", "provenance"},
                )
                self.assertEqual(value_use["dataElement"], {"publicId": "123"})
                cut = result["truncation"]
                self.assertEqual(cut["occurred"], max(element_count, value_count) > HOP_MAXIMUM)
                self.assert_hop_records(cut, element_count, value_count)

    def assert_hop_records(self, cut, elements, values):
        if not cut["occurred"]:
            self.assertEqual(cut, {"occurred": False})
            return
        for name, count in (("dataElements", elements), ("permissibleValues", values)):
            expected = {"occurred": False}
            if count > HOP_MAXIMUM:
                expected = {
                    "occurred": True,
                    "bound": "results",
                    "limit": 1000,
                    "reached": 1000,
                    "omitted": 1,
                    "exact": False,
                }
            self.assertEqual(cut["perHop"][name], expected)

    def test_invalid_inputs_are_rejected_before_discovery_or_graph_requests(self):
        for args in (
            {},
            {"conceptCode": "C1", "text": "Q"},
            {"conceptCode": "C0"},
            {"text": " "},
            {"conceptCode": "C1", "commons": ""},
        ):
            with self.subTest(args=args):
                self.assertEqual(self.call(**args)["error"]["code"], "invalid_request")
        self.assertEqual(self.evs.calls, [])
        self.assertEqual(self.ssis.calls, [])

    def test_text_uses_first_default_search_result_and_pins_its_concept_read(self):
        calls = []

        def search(selected, query, mode, offset, limit, status):
            calls.append((selected.version, query, mode, offset, limit, status))
            return 2, [concept("C2", active=True), concept("C1", "query", active=True)]

        with patch.object(self.evs, "search_concepts", search, create=True):
            result = self.call(text="query")
        self.assertEqual(result["concept"]["code"], "C2")
        self.assertEqual(calls, [("26.06e", "query", "lexical", 0, 10, None)])
        self.assertIn(("get_concept", "ncit_26.06e", "C2"), self.evs.calls)

    def test_no_text_match_is_not_found_with_next_step(self):
        with patch.object(self.evs, "search_concepts", return_value=(0, []), create=True):
            result = self.call(text="unknown")
        self.assertEqual(result["error"]["code"], "not_found")
        self.assertIn("other text", result["error"]["message"])
        self.assertIn("conceptCode", result["error"]["message"])
        self.assertEqual(self.ssis.calls, [])

    def test_evs_concept_mismatch_fails_closed(self):
        self.evs.concepts["C1"]["version"] = "wrong"
        self.assertEqual(self.call(conceptCode="C1")["error"]["code"], "release_mismatch")

    def test_session_pin_is_reused_and_implicit_cache_stays_private(self):
        with session_scope(SessionRelease()):
            first = self.call(conceptCode="C1")
            self.evs.release = release("26.07d")
            second = self.call(conceptCode="C1")
        self.assertEqual(first["provenance"]["release"], second["provenance"]["release"])
        self.assertEqual(self.hint, {"ttlMs": 0, "cacheScope": "private"})
        self.assertEqual(sum(c[0] == "get_terminologies" for c in self.evs.calls), 1)

    def test_stored_hop_filters_exact_codes_and_preserves_literals_and_attribution(self):
        with (
            patch.object(
                self.evs,
                "get_gdc_mapset",
                create=True,
                return_value={"code": "NCIt_Maps_To_GDC", "version": "26.06e"},
            ),
            patch.object(
                self.evs,
                "get_gdc_maps",
                create=True,
                return_value=([gdc_map() | {"licenseText": "from upstream"}, gdc_map("C12")], 2),
            ),
        ):
            result = self.call(conceptCode="C1", commons="GDC")
        self.assertEqual([v["value"] for v in result["storedValues"]], ["Stored C1"])
        self.assertEqual(result["storedValues"][0]["provenance"]["attribution"], "from upstream")

    def test_large_gdc_hop_pages_to_sentinel_without_cutting_other_hops(self):
        def maps(code, offset=0):
            return [
                gdc_map(code) | {"targetName": str(i)}
                for i in range(offset, min(offset + 10, 1001))
            ], 1001

        self.ssis.elements = [element(123)]
        with (
            patch.object(
                self.evs,
                "get_gdc_mapset",
                create=True,
                return_value={"code": "NCIt_Maps_To_GDC", "version": "26.06e"},
            ),
            patch.object(self.evs, "get_gdc_maps", create=True, side_effect=maps),
        ):
            result = self.call(conceptCode="C1", commons="GDC")
        self.assertEqual(len(result["storedValues"]), 1000)
        self.assertEqual(len(result["dataElements"]), 1)
        self.assertEqual(result["truncation"]["perHop"]["storedValues"]["omitted"], 1)
        self.assertEqual(result["truncation"]["perHop"]["dataElements"], {"occurred": False})

    def test_shared_request_budget_exhaustion_is_not_an_empty_hop(self):
        def charged_identity():
            current_budget().request()
            return self.ssis.graphs

        def charged_rows(*args, **kwargs):
            current_budget().request()
            return []

        with (
            patch("nci_sip_mcp.workflows.Budget", return_value=Budget(requests=1)),
            patch.object(self.ssis, "get_graph_identities", charged_identity),
            patch.object(self.ssis, "find_data_elements", charged_rows),
        ):
            result = self.call(conceptCode="C1")
        self.assertEqual(result["error"]["code"], "bound_exceeded")
        self.assertEqual(result["error"]["details"]["reached"], 1)

    def test_bad_release_precedes_a_registry_lookup(self):
        result = self.call(conceptCode="C1", release="../bad", registryRelease="published")
        self.assertEqual(result["error"]["code"], "invalid_request")
        self.assertEqual(result["error"]["details"]["parameter"], "release")
        self.assertEqual(self.evs.calls, [])

    def test_crosswalk_grounding_preserves_value_bindings_and_missing_binding_is_empty(self):
        rows = [
            {
                "CDE Public ID": "123",
                "Version": "1",
                "CRDC Name": "sex",
                "Used By": "Other, Pediatric Cancer",
                "permissibleValues": [{"Permissible Value": "Male", "Concept Code": "C9:C1"}],
            }
        ]
        with patch.object(self.context.cadsr, "get_crdc_list", return_value=rows):
            result = self.call(conceptCode="C1", commons="Pediatric Cancer")
            absent = self.call(conceptCode="C1", commons="Pediatric")
        self.assertEqual(
            [(v["value"], v["field"]) for v in result["storedValues"]], [("Male", "sex")]
        )
        self.assertEqual(result["storedValues"][0]["provenance"]["registry"], {"registry": "cadsr"})
        self.assertEqual(absent["storedValues"], [])

    def test_changing_gdc_total_is_not_a_complete_grounding(self):
        with (
            patch.object(
                self.evs,
                "get_gdc_mapset",
                create=True,
                return_value={"code": "NCIt_Maps_To_GDC", "version": "26.06e"},
            ),
            patch.object(
                self.evs,
                "get_gdc_maps",
                create=True,
                side_effect=[([gdc_map()] * 10, 11), ([gdc_map()], 12)],
            ),
        ):
            result = self.call(conceptCode="C1", commons="GDC")
        self.assertEqual(result["error"]["code"], "upstream_unavailable")
        self.assertEqual(result["error"]["details"]["surface"], "evs")
        self.assertNotIn("storedValues", result)

    def test_malformed_join_is_an_error_with_no_partial_hops(self):
        self.ssis.elements = [element(123)]
        self.ssis.values = [{"id": "0", "version": "1", "value": "Male", "concept": NCIT + "C1"}]
        result = self.call(conceptCode="C1")
        self.assertEqual(result["error"]["code"], "upstream_unavailable")
        self.assertNotIn("dataElements", result)


class CohortTest(WorkflowFixture):
    def test_unread_exclusions_keep_cohort_incomplete_even_when_children_are_complete(self):
        original = self.evs.get_concepts_by_codes

        def read(codes, release, include):
            if "roles" in include.split(","):
                raise UpstreamTooLargeError("Role response exceeds the limit")
            return original(codes, release, include)

        with patch.object(self.evs, "get_concepts_by_codes", side_effect=read):
            result = self.call("expand_cohort", conceptCode="C1")
        self.assertEqual(set(result["codes"]), {"C1", "C2", "C3", "C4"})
        self.assertEqual(result["excluded"], [])
        self.assertEqual(
            result["truncation"],
            {
                "occurred": True,
                "bound": "upstream_cap",
                "limit": self.evs.max_response_bytes,
                "reached": self.evs.max_response_bytes,
                "omitted": 1,
                "exact": False,
            },
        )

    def setUp(self):
        super().setUp()
        self.evs.concepts = {
            "C1": concept(
                "C1",
                active=True,
                children=[{"code": "C2"}, {"code": "C3"}],
                roles=[role("C2"), role("C2", "R136")],
            ),
            "C2": concept("C2", active=True, children=[{"code": "C4"}], roles=[role("C3")]),
            "C3": concept("C3", active=True),
            "C4": concept("C4", active=True),
        }

    def test_equals_fine_grained_composition_with_both_modes_and_node_cut(self):
        base = {"terminology": "ncit", "release": "26.06e", "code": "C1"}
        hierarchy = invoke(
            self.context, "get_concept_hierarchy", **base, direction="child", depth=2
        )
        roles = invoke(self.context, "get_concept_neighborhood", **base, kinds=["role"], depth=1)
        exclusions = [e for e in roles["edges"] if e["provenance"]["polarity"] == "negative"]
        members = ["C1"] + [n["code"] for n in hierarchy["nodes"]]
        withheld = {e["targetCode"] for e in exclusions}
        positive = [code for code in members if code not in withheld]
        self.assert_composition(False, positive, exclusions)
        self.assert_composition(True, members, exclusions)

    def test_edges_preserve_hierarchy_and_each_exclusion_after_member_cut(self):
        for negative in (False, True):
            for maximum in (2, 200):
                with self.subTest(negative=negative, maximum=maximum):
                    result = self.call(
                        "expand_cohort",
                        conceptCode="C1",
                        includeNegative=negative,
                        maxNodes=maximum,
                    )
                    self.assert_cohort_edges(result)

    def test_cohort_member_window_preserves_the_hard_edge_bound(self):
        """Given two complete levels, a large member window still admits only 5,000 edges."""
        cases = (
            (5000, 0, {"occurred": False}),
            (
                5001,
                1,
                {
                    "occurred": True,
                    "bound": "edges",
                    "limit": 5000,
                    "reached": 5000,
                    "omitted": 1,
                    "exact": False,
                },
            ),
        )
        for edge_count, omitted, truncation in cases:
            with self.subTest(upstream_edges=edge_count):
                expected_pairs = self.cohort_edge_fixture(omitted)
                self.assertEqual(len(expected_pairs), edge_count)
                result = self.call(
                    "expand_cohort",
                    conceptCode="C1",
                    maxDepth=2,
                    maxNodes=1000,
                )
                self.assertEqual(len(result["codes"]), 201)
                self.assertEqual(set(result["codes"]), {f"C{i}" for i in range(1, 202)})
                self.assertEqual(result["excluded"], [])
                self.assertEqual(len(result["edges"]), 5000)
                children = self.edges_of(result, "child")
                actual_pairs = {(edge["sourceCode"], edge["targetCode"]) for edge in children}
                self.assertEqual(len(actual_pairs), 5000)
                self.assertLessEqual(actual_pairs, expected_pairs)
                self.assertEqual(len(expected_pairs - actual_pairs), omitted)
                self.assertEqual(result["truncation"], truncation)

    def cohort_edge_fixture(self, extra_edge):
        """Specify public child/parent pairs, then build full, exclusion-free EVS concepts."""
        pairs = {(f"C{child}", "C1") for child in range(2, 102)}
        pairs.update(
            (f"C{leaf}", f"C{middle}") for middle in range(2, 51) for leaf in range(102, 202)
        )
        if extra_edge:
            pairs.add(("C102", "C51"))
        self.evs.concepts = {
            f"C{i}": concept(f"C{i}", active=True, children=[], roles=[]) for i in range(1, 202)
        }
        for child, parent in sorted(pairs):
            self.evs.concepts[parent]["children"].append({"code": child})
        return pairs

    def assert_cohort_edges(self, result):
        present = set(result["codes"])
        expected = {
            pair for pair in (("C2", "C1"), ("C3", "C1"), ("C4", "C2")) if set(pair) <= present
        }
        children = self.edges_of(result, "child")
        self.assertEqual({(e["sourceCode"], e["targetCode"]) for e in children}, expected)
        roles = self.edges_of(result, "role")
        self.assertEqual(roles, [row["edge"] for row in result["excluded"]])
        self.assertEqual(
            [e["provenance"]["relationship"]["code"] for e in roles],
            ["R135", "R136"],
        )

    @staticmethod
    def edges_of(result, kind):
        return [e for e in result["edges"] if e["provenance"]["relationship"]["kind"] == kind]

    def assert_composition(self, negative, expected, exclusions):
        for maximum, truncated in ((2, True), (200, False)):
            with self.subTest(negative=negative, maximum=maximum):
                result = self.call(
                    "expand_cohort", conceptCode="C1", includeNegative=negative, maxNodes=maximum
                )
                self.assertEqual(result["codes"], expected[:maximum])
                self.assertEqual(len(result["excluded"]), len(exclusions))
                self.assertEqual({r["code"] for r in result["excluded"]}, {"C2"})
                expected_cut = {"occurred": False}
                if truncated:
                    expected_cut = {
                        "occurred": True,
                        "bound": "nodes",
                        "limit": maximum,
                        "reached": maximum,
                        "omitted": 1,
                        "exact": False,
                    }
                self.assertEqual(result["truncation"], expected_cut)

    def test_singleton_is_complete_and_start_counts_in_node_limit(self):
        self.evs.concepts["C1"] = concept("C1", active=True)
        result = self.call("expand_cohort", conceptCode="C1", maxNodes=1)
        self.assertEqual(result["codes"], ["C1"])
        self.assertEqual(result["truncation"], {"occurred": False})
        self.assertEqual(result["edges"], [])
        self.assertEqual(result["excluded"], [])

    def test_depth_cut_is_preserved(self):
        result = self.call("expand_cohort", conceptCode="C1", maxDepth=1, includeNegative=True)
        self.assertEqual(set(result["codes"]), {"C1", "C2", "C3"})
        self.assertEqual(result["truncation"]["bound"], "depth")
        self.assertEqual(result["truncation"]["limit"], 1)

    def test_request_exhaustion_is_reported_instead_of_an_unverified_complete_cohort(self):
        self.context.evs = BudgetEVS(list(self.evs.concepts.values()))
        with patch("nci_sip_mcp.workflows.Budget", return_value=Budget(requests=7)):
            result = self.call("expand_cohort", conceptCode="C1", includeNegative=True)
        self.assertEqual(result["truncation"]["bound"], "requests")
        self.assertEqual(result["truncation"]["reached"], 7)
        self.assertFalse(result["truncation"]["exact"])

    def test_invalid_negative_flag_never_reaches_evs(self):
        result = self.call("expand_cohort", conceptCode="C1", includeNegative="yes")
        self.assertEqual(result["error"]["code"], "invalid_request")
        self.assertEqual(self.evs.calls, [])


class HarmonizationTest(ServerTestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)

    def call(self, server, **arguments):
        context = Context(Settings(data_dir=self.directory, cadsr_base_url=server.url))
        with cache_call() as hint:
            result = invoke(context, "harmonize_data_dictionary", **arguments)
        if "error" not in result:
            self.assertEqual(hint, {"ttlMs": 0, "cacheScope": "private"})
        return result

    def test_columns_keep_order_and_descriptions_and_align_samples(self):
        server = self.serve(
            reply(cde_response("Q", [cde_row()])),
            reply(cde_response("Empty")),
            reply(vm_response("Male", [vm_row()])),
        )
        result = self.call(
            server,
            columns=[
                {"name": "Q", "description": "Question", "sampleValues": ["Male"]},
                {"name": "Empty"},
            ],
        )
        self.assertEqual(result["unmatched"], ["Empty"])
        self.assertEqual([c["name"] for c in result["columns"]], ["Q", "Empty"])
        self.assertEqual(
            result["columns"][0]["permissibleValueAlignment"][0]["item"]["concept"], "C123"
        )
        self.assertEqual(result["columns"][1]["permissibleValueAlignment"], [])
        self.assertEqual(json.loads(server.bodies[0]), {"entity": "Q", "entityUserTip": "Question"})
        self.assertEqual(result["provenance"]["release"], {"registry": "cadsr"})

    def test_bad_later_column_is_rejected_before_any_request(self):
        server = self.serve()
        result = self.call(server, columns=[{"name": "Q"}, {"name": "Bad", "sampleValues": [None]}])
        self.assertEqual(result["error"]["code"], "invalid_request")
        self.assertEqual(server.seen, [])

    def test_duplicate_columns_reuse_requests_without_losing_rows(self):
        server = self.serve(
            reply(cde_response("Q", [cde_row()])), reply(vm_response("Male", [vm_row()]))
        )
        result = self.call(server, columns=[{"name": "Q", "sampleValues": ["Male"]}] * 2)
        self.assertEqual(len(result["columns"]), 2)
        self.assertEqual(result["columns"][0], result["columns"][1])
        self.assertEqual(len(server.seen), 2)

    def test_sample_batches_preserve_all_alignments_and_reuse_shared_batches(self):
        values = [f"Value {i}" for i in range(11)]
        responses = [
            {"name": value, "matches": [vm_row(itemId=str(i + 1), matchedName=value)]}
            for i, value in enumerate(values)
        ]
        server = self.serve(
            reply(cde_response("First", [cde_row()])),
            reply(cde_response("Second", [cde_row()])),
            reply({"matchResults": responses[:10]}),
            reply({"matchResults": responses[10:]}),
        )
        result = self.call(
            server,
            columns=[
                {"name": "First", "sampleValues": values},
                {"name": "Second", "sampleValues": values[:10]},
            ],
        )
        self.assertEqual(
            [
                [m["item"]["name"] for m in c["permissibleValueAlignment"]]
                for c in result["columns"]
            ],
            [values, values[:10]],
        )
        self.assertEqual(
            [json.loads(body) for body in server.bodies[2:]],
            [[{"name": value} for value in values[:10]], [{"name": values[10]}]],
        )

    def test_unknown_and_published_but_unaddressable_pins_fail_closed(self):
        for rows, expected in (
            ([], "release_not_available"),
            ([{"identifier": "r1", "generatedAt": "2026-07-01T22:19"}], "capability_unavailable"),
        ):
            with self.subTest(expected=expected):
                server = self.serve()
                context = Context(Settings(data_dir=self.directory, cadsr_base_url=server.url))
                with patch.object(context.cadsr, "get_registry_releases", return_value=rows):
                    result = invoke(
                        context,
                        "harmonize_data_dictionary",
                        columns=[{"name": "Q"}],
                        registryRelease="r1",
                    )
                    ground = invoke(context, "ground_value", conceptCode="C1", registryRelease="r1")
                self.assertEqual(result["error"]["code"], expected)
                self.assertEqual(ground["error"]["code"], expected)
                self.assertEqual(server.seen, [])

    def test_retry_and_later_matching_share_budget_and_one_audit_completion(self):
        server = self.serve(Reply(503), reply(cde_response("Q", [cde_row()])))
        context = Context(Settings(data_dir=self.directory, cadsr_base_url=server.url))
        context.cadsr.http.sleep = lambda _: None
        logging.disable(logging.NOTSET)
        with (
            patch("nci_sip_mcp.workflows.Budget", return_value=Budget(requests=2)),
            self.assertLogs("nci_sip_mcp.audit", level="INFO") as captured,
        ):
            result = invoke(
                context,
                "harmonize_data_dictionary",
                _correlation_id="chain",
                columns=[{"name": "Q", "sampleValues": ["Male"]}],
            )
        self.assertEqual(result["error"]["code"], "bound_exceeded")
        self.assertEqual(len(server.seen), 2)
        records = [record.structured for record in captured.records]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["outboundRequests"], 2)
        self.assertEqual(records[0]["correlationId"], "chain")

    def test_missing_matches_is_not_an_unmatched_column(self):
        server = self.serve(reply({"matchResults": {"entity": "Q"}}))
        result = self.call(server, columns=[{"name": "Q"}])
        self.assertEqual(result["error"]["code"], "upstream_unavailable")
        self.assertNotIn("unmatched", result)

    def test_columns_and_filters_are_validated_before_requests(self):
        for arguments in (
            {"columns": [{"name": "Q", "unknown": "x"}]},
            {"columns": [{"name": "Q"}], "filters": {"context": "bad\nheader"}},
            {"columns": []},
            {"columns": [{"name": "Q"}] * 11},
        ):
            with self.subTest(arguments=arguments):
                server = self.serve()
                result = self.call(server, **arguments)
                self.assertEqual(result["error"]["code"], "invalid_request")
                self.assertEqual(server.seen, [])


class GroundReleaseTest(WorkflowFixture):
    def test_nested_reads_preserve_implicit_selection_and_its_release_date(self):
        logging.disable(logging.NOTSET)
        for arguments in ({"conceptCode": "C1"}, {"text": "query"}):
            with (
                self.subTest(arguments=arguments),
                session_scope(SessionRelease()),
                self.assertLogs("nci_sip_mcp.audit", level="INFO") as captured,
                patch.object(
                    self.evs,
                    "search_concepts",
                    create=True,
                    return_value=(1, [concept("C1", active=True)]),
                ),
            ):
                first = self.call(**arguments)
                held = self.call(**arguments)
                explicit = self.call(**arguments, release="26.06e")
            self.assertNotIn("error", explicit)
            self.assertEqual(
                [result["concept"]["provenance"]["release"] for result in (first, held)],
                [{"terminology": "ncit", "identifier": "26.06e", "date": "2026-06-29"}] * 2,
            )
            records = [record.structured["release"] for record in captured.records]
            self.assertEqual(
                [record["selection"] for record in records],
                ["freshly-resolved", "session-held", "explicit"],
            )
            self.assertEqual(
                [record["selected"]["date"] for record in records[:2]], ["2026-06-29"] * 2
            )

    def test_dictionary_input_is_hashed_even_when_a_later_column_is_invalid(self):
        columns = [
            {
                "name": "sensitive-name",
                "description": "sensitive-description",
                "sampleValues": ["sensitive-value"],
            },
            {"name": None},
        ]
        logging.disable(logging.NOTSET)
        with self.assertLogs("nci_sip_mcp.audit", level="INFO") as captured:
            result = self.call("harmonize_data_dictionary", columns=columns)
        self.assertEqual(result["error"]["code"], "invalid_request")
        record = captured.records[-1].structured
        encoded = json.dumps(
            columns, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode()
        self.assertEqual(
            record["parameters"]["columns"], {"sha256": hashlib.sha256(encoded).hexdigest()}
        )
        self.assertNotIn("sensitive-", json.dumps(record))
