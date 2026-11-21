import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import patch

from fakes import FakeEVS, concept
from nci_sip_acceptance.spec import TOOLS
from nci_sip_mcp.bounds import (
    DEFAULT_MAX_DEPTH,
    DEFAULT_MAX_EDGES,
    DEFAULT_MAX_NODES,
    HARD_MAX_DEPTH,
    HARD_MAX_EDGES,
    HARD_MAX_NODES,
    HARD_MAX_PER_KIND,
    MAX_TRAVERSAL_REQUESTS,
    Budget,
    RequestBudgetError,
    budgeted,
    current_budget,
)
from nci_sip_mcp.errors import correlated
from nci_sip_mcp.evs import EVSNotFoundError
from nci_sip_mcp.registry import invoke
from test_handlers import HandlerTestCase
from test_http_client import Reply, ServerTestCase
from test_traversal import HubEVS, child, codes, complete_graph, descendant, related, walk


class BudgetTest(unittest.TestCase):
    def test_traversal_limits_match_the_specification(self):
        bounds = TOOLS["get_concept_neighborhood"]["bounds"]
        self.assertEqual(
            [
                (bounds[key]["default"], bounds[key]["maximum"])
                for key in ("depth", "maxNodes", "maxEdges")
            ],
            [
                (DEFAULT_MAX_DEPTH, HARD_MAX_DEPTH),
                (DEFAULT_MAX_NODES, HARD_MAX_NODES),
                (DEFAULT_MAX_EDGES, HARD_MAX_EDGES),
            ],
        )
        self.assertEqual(bounds["budgetPerKind"]["maximum"], HARD_MAX_PER_KIND)
        for name in ("get_concept_neighborhood", "get_concept_hierarchy"):
            self.assertEqual(TOOLS[name]["requests"], MAX_TRAVERSAL_REQUESTS)

    def test_nested_context_restores_the_outer_budget_even_after_an_error(self):
        outer = Budget(requests=2)
        with budgeted(outer):
            with self.assertRaises(RequestBudgetError), budgeted(Budget(requests=0)):
                current_budget().request()
            current_budget().request()
            self.assertEqual(outer.attempts, 1)
        self.assertIsNone(current_budget())


def roles(*targets):
    return [related(f"r{n}", code) for n, code in enumerate(targets)]


class BoundPrecedenceTest(unittest.TestCase):
    def setUp(self):
        self.enterContext(correlated("bound-precedence"))

    def test_node_bound_precedes_later_request_exhaustion(self):
        client = BudgetEVS([concept("C1", children=[child("C2"), child("C3"), child("C4")])])
        result = walk(client, requests=1, max_nodes=3, max_depth=2, edge_types=["child"])
        self.assertEqual(result.truncation.bound, "nodes")

    def test_global_node_bound_precedes_a_simultaneous_kind_bound(self):
        result = walk(
            complete_graph(FakeEVS([concept("C1", roles=roles("C2", "C3", "C4"))])),
            max_depth=1,
            max_nodes=3,
            budget_per_kind=2,
        )
        self.assertEqual(result.truncation.bound, "nodes")

    def test_kind_exhaustion_leaves_other_kinds_free_to_expand_at_the_next_depth(self):
        client = FakeEVS(
            [
                concept("C1", roles=roles("C2", "C3")),
                concept("C2", associations=[related("a", "C5")]),
            ]
        )
        result = walk(complete_graph(client), budget_per_kind=1, max_depth=2)
        self.assertIn("C5", codes(result))

    def test_request_exhaustion_stops_before_later_descendant_levels(self):
        client = BudgetEVS(
            [concept("C1", children=[child("C2")])],
            descendants={"C1": [descendant("C2", 1), descendant("C3", 2), descendant("C4", 3)]},
        )
        result = walk(client, requests=2, max_depth=3, edge_types=["child", "descendant"])
        self.assertNotIn("C4", codes(result))
        self.assertEqual(result.truncation.to_dict()["perKind"]["descendant"]["bound"], "requests")

    def test_per_kind_edge_omissions_count_parallel_edges(self):
        result = walk(
            FakeEVS([concept("C1", roles=roles("C2", "C2", "C2"))]), max_depth=1, max_edges=1
        )
        self.assertEqual(result.truncation.to_dict()["perKind"]["role"]["omitted"], 2)

    def test_top_kind_omission_does_not_include_another_kinds_nodes(self):
        client = FakeEVS(
            [
                concept(
                    "C1",
                    roles=roles("C2", "C3"),
                    associations=[related("a1", "C4"), related("a2", "C5")],
                )
            ]
        )
        result = walk(complete_graph(client), max_depth=1, budget_per_kind=1)
        self.assertEqual(result.truncation.omitted, 1)

    def test_per_kind_first_bound_survives_later_global_node_exhaustion(self):
        client = FakeEVS(
            [
                concept("C1", roles=roles("C2", "C3")),
                concept("C2", children=[child("C4")], roles=roles("C5")),
            ]
        )
        result = walk(complete_graph(client), max_depth=2, max_nodes=3, budget_per_kind=1)
        self.assertEqual(result.truncation.to_dict()["perKind"]["role"]["bound"], "kind_budget")

    def test_reconciliation_refreshes_the_first_kind(self):
        client = FakeEVS(
            [
                concept(
                    "C1",
                    roles=roles("C2", "C3"),
                    associations=[related("a1", "C1"), related("a2", "C3"), related("a3", "C5")],
                )
            ]
        )
        result = walk(complete_graph(client), budget_per_kind=1, max_depth=1)
        self.assertEqual(result.truncation.omitted, 1)

    def test_reconciliation_removes_a_now_present_node_from_omissions(self):
        client = FakeEVS(
            [
                concept(
                    "C1",
                    roles=roles("C2", "C3", "C4"),
                    associations=[related("a1", "C1"), related("a2", "C3")],
                )
            ]
        )
        result = walk(complete_graph(client), max_nodes=3, budget_per_kind=1, max_depth=1)
        self.assertEqual((result.truncation.bound, result.truncation.omitted), ("nodes", 1))

    def test_upstream_cap_precedes_later_node_exhaustion(self):
        client = BudgetHub(
            [
                concept("C1", children=[child("C2"), child("C3")]),
                concept("C2"),
                concept("C3", children=[child("C4"), child("C5")]),
            ]
        )
        result = walk(complete_graph(client), max_nodes=4, max_depth=2)
        self.assertEqual(result.truncation.bound, "upstream_cap")

    def test_per_kind_node_reached_counts_starts_and_not_edges(self):
        result = walk(
            complete_graph(FakeEVS([concept("C1", roles=roles("C2", "C3", "C4"))])),
            max_depth=1,
            max_nodes=3,
        )
        self.assertEqual(result.truncation.to_dict()["perKind"]["role"]["reached"], 3)

    def test_missing_minimal_fallback_still_reports_not_found(self):
        with self.assertRaises(EVSNotFoundError):
            walk(BudgetHub(), start_codes=["C2"])

    def test_reconciled_drops_do_not_hide_an_upstream_cap(self):
        client = BudgetHub(
            [
                concept("C1", roles=roles("C2", "C3"), associations=[related("a", "C4")]),
                concept("C2"),
                concept("C4", children=[child("C3")]),
            ]
        )
        result = walk(complete_graph(client), budget_per_kind=1, max_depth=2)
        self.assertEqual(result.truncation.bound, "upstream_cap")

    def test_an_oversized_split_precedes_a_later_request_cap_in_the_same_fetch(self):
        client = BudgetHub([concept("C2"), concept("C1")])
        result = walk(client, start_codes=["C2", "C1"], requests=3)
        self.assertEqual(result.truncation.bound, "upstream_cap")
        self.assertEqual(result.truncation.to_dict()["perKind"]["role"]["bound"], "upstream_cap")

    def test_surviving_later_drop_does_not_hide_an_intervening_upstream_cap(self):
        client = BudgetHub(
            [
                concept("C1", roles=roles("C2", "C3"), associations=[related("a", "C4")]),
                concept("C2"),
                concept("C4", children=[child("C3"), child("C5")]),
            ]
        )
        result = walk(complete_graph(client), budget_per_kind=1, max_depth=2)
        self.assertEqual(result.truncation.bound, "upstream_cap")
        self.assertEqual(result.truncation.to_dict()["perKind"]["child"]["bound"], "upstream_cap")

    def test_unread_kind_bound_precedes_its_later_known_node_drop(self):
        client = BudgetHub(
            [
                concept("C1", children=[child("C2"), child("C3")]),
                concept("C2"),
                concept("C3", roles=roles("C4", "C5")),
            ]
        )
        result = walk(complete_graph(client), max_nodes=4, max_depth=2)
        self.assertEqual(result.truncation.to_dict()["perKind"]["role"]["bound"], "upstream_cap")

    def test_reconciled_target_still_reports_an_edge_cap(self):
        client = FakeEVS(
            [
                concept(
                    "C1",
                    roles=roles("C2", "C3"),
                    associations=[related("a1", "C2"), related("a2", "C3")],
                )
            ]
        )
        result = walk(client, max_depth=1, budget_per_kind=1, max_edges=3)
        expected = {
            "occurred": True,
            "bound": "edges",
            "limit": 3,
            "reached": 3,
            "omitted": 1,
            "exact": False,
        }
        record = result.truncation.to_dict()
        self.assertEqual(record.pop("perKind")["role"], expected)
        self.assertEqual(record, expected)
        self.assertEqual(codes(result), ["C1", "C2", "C3"])
        self.assertEqual(len(result.edges), 3)

    def test_duplicate_omissions_retain_their_original_bound(self):
        client = FakeEVS(
            [
                concept(
                    "C1",
                    roles=[related("r1", "C2"), related("r2", "C3"), related("r2", "C3")],
                    associations=[related("a1", "C1"), related("a2", "C4")],
                )
            ]
        )
        result = walk(complete_graph(client), max_depth=1, max_nodes=3, budget_per_kind=1)
        self.assertEqual(result.truncation.bound, "kind_budget")
        self.assertEqual(result.truncation.to_dict()["perKind"]["role"]["bound"], "kind_budget")

    def test_oversized_relations_precede_a_blocked_minimal_fallback(self):
        client = BudgetHub([concept("C1", children=[child("C2")]), concept("C2")])
        result = walk(client, requests=2, max_depth=2)
        self.assertEqual(result.truncation.bound, "upstream_cap")
        self.assertEqual(result.truncation.to_dict()["perKind"]["child"]["bound"], "upstream_cap")

    def test_edge_stop_reports_the_next_frontiers_unread_kinds(self):
        client = FakeEVS(
            [
                concept("C1", children=[child("C2")], roles=roles("C3")),
                concept("C2", children=[child("C4")]),
            ]
        )
        result = walk(client, max_depth=2, max_edges=1, edge_types=["child", "role"])
        self.assertEqual(
            result.truncation.to_dict()["perKind"]["child"],
            {
                "occurred": True,
                "bound": "edges",
                "limit": 1,
                "reached": 1,
                "omitted": 0,
                "exact": False,
            },
        )

    def test_upstream_cap_counts_concepts_but_leaves_kind_omissions_unknown(self):
        result = walk(
            BudgetHub([concept("C2")]),
            start_codes=["C2"],
            max_depth=1,
            edge_types=["child", "role"],
        )
        self.assertEqual(result.truncation.bound, "upstream_cap")
        self.assertEqual(result.truncation.omitted, 1)
        self.assertFalse(result.truncation.exact)
        for kind in ["child", "role"]:
            with self.subTest(kind=kind):
                record = result.truncation.to_dict()["perKind"][kind]
                self.assertEqual(record["bound"], "upstream_cap")
                self.assertEqual(record["omitted"], 0)
                self.assertIs(record["exact"], False)

    def test_edge_stop_prevents_final_checks_for_other_kinds(self):
        result = walk(
            FakeEVS([concept("C1", children=[child("C2")], roles=roles("C3"))]),
            max_depth=1,
            max_edges=1,
            edge_types=["child", "role"],
        )
        records = result.truncation.to_dict()["perKind"]
        self.assertEqual(records["child"]["bound"], "edges")
        self.assertEqual(records["child"]["omitted"], 0)
        self.assertFalse(records["child"]["exact"])
        self.assertEqual(records["role"]["bound"], "edges")
        self.assertEqual(codes(result), ["C1", "C2"])

    def test_edge_stop_without_new_nodes_leaves_undropped_kinds_complete(self):
        result = walk(
            FakeEVS([concept("C1", children=[child("C1")], roles=roles("C1"))]),
            max_depth=2,
            max_edges=1,
            edge_types=["child", "role"],
        )
        records = result.truncation.to_dict()["perKind"]
        self.assertEqual(records["child"], {"occurred": False})
        self.assertEqual(records["role"]["bound"], "edges")
        self.assertEqual(codes(result), ["C1"])

    def test_partial_starts_report_requests_without_any_relation_reads(self):
        with patch("nci_sip_mcp.traversal.BATCH_SIZE", 1):
            result = walk(
                BudgetEVS([concept("C1"), concept("C2")]),
                start_codes=["C1", "C2"],
                max_depth=0,
                requests=1,
                edge_types=["descendant"],
            )
        self.assertEqual(codes(result), ["C1"])
        self.assertEqual(
            result.truncation.to_dict(),
            {
                "occurred": True,
                "bound": "requests",
                "limit": 1,
                "reached": 1,
                "omitted": 1,
                "exact": False,
            },
        )


class RequestBudgetTest(ServerTestCase):
    def test_retries_and_later_requests_share_the_same_allowance(self):
        server = self.serve(Reply(503))
        client = self.client(server)
        budget = Budget(requests=2)
        records = []
        client.on_request = records.append
        with budgeted(budget):
            self.assertEqual(client.get_json("/retry"), {"ok": True})
            with self.assertRaises(RequestBudgetError) as raised:
                client.get_json("/next")
        self.assertEqual(raised.exception.details, {"bound": "requests", "limit": 2, "reached": 2})
        self.assertEqual((len(server.seen), len(records), budget.attempts), (2, 2, 2))
        self.assertEqual([r.attempt for r in records], [1, 2])

    def test_a_retry_cannot_send_request_201(self):
        server = self.serve(*[Reply(503)] * MAX_TRAVERSAL_REQUESTS)
        client = self.client(server, max_attempts=MAX_TRAVERSAL_REQUESTS + 1)
        budget = Budget()
        with budgeted(budget), self.assertRaises(RequestBudgetError):
            client.get_json("/retry")
        self.assertEqual(len(server.seen), MAX_TRAVERSAL_REQUESTS)
        self.assertEqual(budget.attempts, MAX_TRAVERSAL_REQUESTS)

    def test_an_upstream_failure_counts_the_same_attempts_as_the_budget(self):
        server = self.serve(Reply(503), Reply(503), Reply(503))
        budget = Budget()
        with budgeted(budget):
            error = self.failure(self.client(server))
        self.assertEqual(error.details["attempts"], budget.attempts)
        self.assertEqual(budget.attempts, len(server.seen))

    def test_simultaneous_calls_and_later_unbounded_calls_are_independent(self):
        server = self.serve()
        client = self.client(server)
        barrier = Barrier(2)

        def call():
            with budgeted(Budget(requests=1)):
                barrier.wait()
                return client.get_json("/one")

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(call) for _ in range(2)]
            self.assertEqual([future.result() for future in futures], [{"ok": True}] * 2)
        self.assertEqual(client.get_json("/outside"), {"ok": True})
        self.assertEqual(len(server.seen), 3)


class BudgetEVS(FakeEVS):
    def _record(self, *args):
        current_budget().request()
        super()._record(*args)


class BudgetHub(HubEVS, BudgetEVS):
    hubs = frozenset({"C2"})


class TraversalBudgetTest(unittest.TestCase):
    def setUp(self):
        self.enterContext(correlated("budget-test"))

    def test_a_later_batch_cannot_discard_the_start_nodes_already_read(self):
        client = BudgetEVS([concept("C1"), concept("C2")])
        with patch("nci_sip_mcp.traversal.BATCH_SIZE", 1):
            result = walk(client, start_codes=["C1", "C2"], max_depth=1, requests=1)
        self.assertEqual(codes(result), ["C1"])
        self.assertEqual(result.truncation.bound, "requests")
        self.assertEqual((result.truncation.limit, result.truncation.reached), (1, 1))
        self.assertGreaterEqual(result.truncation.omitted, 1)
        self.assertFalse(result.truncation.exact)

    def test_start_codes_left_unread_by_the_budget_are_reported_for_inverse_only_walks(self):
        client = BudgetEVS([concept("C1"), concept("C2")])
        with patch("nci_sip_mcp.traversal.INVERSE_BATCH_SIZE", 1):
            result = walk(
                client,
                start_codes=["C1", "C2"],
                direction="in",
                max_depth=0,
                edge_types=["inverse_role"],
                requests=1,
            )
        self.assertEqual(codes(result), ["C1"])
        self.assertEqual(result.truncation.bound, "requests")
        self.assertGreaterEqual(result.truncation.omitted, 1)

    def test_request_exhaustion_does_not_hide_an_already_confirmed_missing_start(self):
        client = BudgetEVS([concept("C2")])
        with (
            patch("nci_sip_mcp.traversal.BATCH_SIZE", 1),
            self.assertRaises(EVSNotFoundError) as raised,
        ):
            walk(client, start_codes=["C1", "C2"], requests=1)
        self.assertEqual(raised.exception.details["identifiers"], ["C1"])

    def test_parallel_dropped_edges_count_one_omitted_node(self):
        client = FakeEVS(
            [
                concept(
                    "C1",
                    roles=[related("r1", "C2"), related("r2", "C3"), related("r3", "C3")],
                )
            ]
        )
        result = walk(complete_graph(client), max_depth=1, budget_per_kind=1)
        self.assertEqual(result.truncation.omitted, 1)
        self.assertEqual(result.truncation.to_dict()["perKind"]["role"]["omitted"], 1)

    def test_omitted_nodes_do_not_include_existing_targets_of_edge_drops(self):
        client = FakeEVS(
            [
                concept(
                    "C1",
                    roles=[
                        related("r1", "C2"),
                        related("r2", "C3"),
                        related("r3", "C1"),
                    ],
                )
            ]
        )
        result = walk(client, max_depth=1, budget_per_kind=1, max_edges=1)
        self.assertEqual(result.truncation.bound, "kind_budget")
        self.assertEqual(result.truncation.omitted, 1)
        self.assertEqual(result.truncation.to_dict()["perKind"]["role"]["omitted"], 1)

    def test_unread_kinds_do_not_claim_completeness_at_the_request_cap(self):
        client = BudgetEVS([concept("C1", children=[child("C2")]), concept("C2")])
        result = walk(client, requests=1)
        kinds = result.truncation.to_dict()["perKind"]
        for kind in ("child", "role", "association"):
            self.assertEqual(
                kinds[kind],
                {
                    "occurred": True,
                    "bound": "requests",
                    "limit": 1,
                    "reached": 1,
                    "omitted": 0,
                    "exact": False,
                },
            )

    def test_reconciled_drops_do_not_hide_request_exhaustion(self):
        client = BudgetEVS(
            [
                concept(
                    "C1",
                    roles=[related("r1", "C2"), related("r2", "C3")],
                    associations=[related("a", "C4")],
                ),
                concept("C2", children=[child("C3")]),
                concept("C4"),
            ]
        )
        with patch("nci_sip_mcp.traversal.BATCH_SIZE", 1):
            result = walk(client, budget_per_kind=1, requests=2, max_depth=2)
        self.assertEqual(result.truncation.bound, "requests")

    def test_a_split_batch_keeps_its_successful_half_at_the_request_cap(self):
        client = BudgetHub([concept("C1"), concept("C2")])
        result = walk(client, start_codes=["C1", "C2"], max_depth=1, requests=2)
        self.assertEqual(codes(result), ["C1"])
        self.assertEqual(result.truncation.bound, "requests")
        self.assertEqual(len(client.calls), 2)

    def test_no_graph_is_an_error_and_a_later_bound_preserves_graph_content(self):
        client = BudgetEVS([concept("C1", children=[child("C2")]), concept("C2")])
        with self.assertRaises(RequestBudgetError):
            walk(client, requests=0)
        result = walk(client, max_depth=2, requests=1)
        self.assertEqual(codes(result), ["C1", "C2"])
        self.assertEqual(result.truncation.bound, "requests")

    def test_exact_request_allowance_is_not_itself_truncation(self):
        result = walk(BudgetEVS([concept("C1")]), max_depth=1, requests=1)
        self.assertEqual(result.truncation.to_dict(), {"occurred": False})

    def test_descendant_requests_preserve_the_first_starts_graph_at_the_cap(self):
        client = BudgetEVS(
            [concept("C1"), concept("C2")],
            descendants={"C1": [descendant("C3", 1)], "C2": [descendant("C4", 1)]},
        )
        result = walk(client, start_codes=["C1", "C2"], edge_types=["descendant"], requests=2)
        self.assertEqual(codes(result), ["C1", "C2", "C3"])
        self.assertEqual(result.truncation.bound, "requests")
        self.assertEqual(result.truncation.reached, 2)
        self.assertEqual(len(client.calls), 2)

    def test_filtered_duplicates_and_existing_nodes_spend_no_kind_allowance(self):
        client = FakeEVS(
            [
                concept(
                    "C1",
                    roles=[
                        related("drop", "C9"),
                        related("keep", "C1"),
                        related("keep", "C2"),
                        related("keep", "C2"),
                        related("keep", "C3"),
                        related("keep", "C4"),
                    ],
                )
            ]
        )
        result = walk(
            complete_graph(client), max_depth=1, budget_per_kind=2, relationship_names=["keep"]
        )
        self.assertEqual(codes(result), ["C1", "C2", "C3"])
        self.assertEqual(len(result.edges), 3)
        self.assertEqual(result.truncation.reached, 2)
        self.assertEqual(result.truncation.omitted, 1)

    def test_kinds_rotate_across_start_nodes_before_the_global_node_limit(self):
        client = FakeEVS(
            [
                concept("C1", roles=[related("role", "C3"), related("role", "C4")]),
                concept("C2", associations=[related("assoc", "C5")]),
            ]
        )
        result = walk(complete_graph(client), start_codes=["C1", "C2"], max_depth=1, max_nodes=4)
        self.assertEqual(set(codes(result)), {"C1", "C2", "C3", "C5"})
        kinds = result.truncation.to_dict()["perKind"]
        self.assertTrue(kinds["role"]["occurred"])
        self.assertEqual(kinds["association"], {"occurred": False})

    def test_another_kind_admitting_a_target_restores_its_previously_blocked_edge(self):
        client = FakeEVS(
            [
                concept(
                    "C1",
                    roles=[related("r1", "C2"), related("r2", "C3")],
                    associations=[related("a1", "C2"), related("a2", "C3")],
                )
            ]
        )
        result = walk(complete_graph(client), max_depth=1, budget_per_kind=1)
        self.assertEqual(set(codes(result)), {"C1", "C2", "C3"})
        self.assertEqual(len(result.edges), 4)
        self.assertEqual(result.truncation.to_dict(), {"occurred": False})

    def test_explicit_kind_limit_keeps_small_kinds_complete(self):
        client = FakeEVS(
            [
                concept(
                    "C1",
                    roles=[related("role", f"C{n}") for n in range(2, 6)],
                    associations=[related("assoc", "C9")],
                )
            ]
        )
        result = walk(complete_graph(client), max_depth=1, budget_per_kind=2)
        self.assertEqual(set(codes(result)), {"C1", "C2", "C3", "C9"})
        record = result.truncation.to_dict()["perKind"]["role"]
        self.assertEqual(
            (record["bound"], record["limit"], record["reached"]), ("kind_budget", 2, 2)
        )
        self.assertEqual(record["omitted"], 2)


class ServiceBudgetTest(HandlerTestCase):
    def test_kind_budget_validation_happens_before_network_access(self):
        for value in (0, -1, True, 1.5, "3"):
            with self.subTest(value=value):
                result = invoke(self.context, "traverse", ["C3262"], budget_per_kind=value)
                self.assertEqual(result["error"]["code"], "invalid_request")
                self.assertEqual(result["error"]["details"]["parameter"], "budget_per_kind")
                self.assertEqual(self.evs.calls, [])

    def test_release_discovery_spends_the_same_budget_as_the_start_concepts(self):
        self.context.evs = BudgetEVS([concept("C1")])
        with patch("nci_sip_mcp.handlers.Budget", return_value=Budget(requests=1)):
            result = invoke(self.context, "traverse", ["C1"])
        self.assertEqual(result["error"]["code"], "bound_exceeded")
        self.assertIn("fewer start codes", result["error"]["message"])
        self.assertEqual(
            result["error"]["details"], {"bound": "requests", "limit": 1, "reached": 1}
        )
        self.assertIsNone(current_budget())
