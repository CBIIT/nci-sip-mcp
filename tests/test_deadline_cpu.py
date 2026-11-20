"""Traversal's independent CPU contract stops processing at elapsed-time bounds."""

import json
import unittest
from unittest.mock import patch

from fakes import FakeEVS, concept, release
from nci_sip_mcp.bounds import Budget, deadline_scope
from nci_sip_mcp.errors import correlated
from nci_sip_mcp.traversal import _Walk, traverse_ncit
from test_deadline_http import Clock


class CostlyText:
    """Model time spent decoding one upstream value, without checkpoint-call counting."""

    def __init__(self, value: str, clock: Clock):
        self.value = value
        self.clock = clock

    def __str__(self):
        self.clock.advance(51)
        return self.value


class CostlyDescendants(list):
    """Producing the next response item can consume the remaining processing budget."""

    def __init__(self, items, clock: Clock):
        super().__init__(items)
        self.clock = clock

    def __iter__(self):
        yield from super().__iter__()
        self.clock.advance(51)
        yield {"code": "C3", "level": "unprocessed invalid level"}


class ReconciliationClock:
    """Start charging identity encoding after another kind admits the shared target."""

    def __init__(self, clock: Clock):
        self.clock = clock
        self.admitted = False
        self.encode = json.dumps

    def __str__(self):
        self.admitted = True
        return "Shared target"

    def encode_identity(self, value, *args, **kwargs):
        result = self.encode(value, *args, **kwargs)
        if self.admitted and value == [None, "Shared"]:
            self.clock.advance(51)
        return result


class CostlyRelation:
    """Reading a verified relation's kind models time spent grouping response data."""

    def __init__(self, clock):
        self.clock = clock
        self.read = False

    def __hash__(self):
        if not self.read:
            self.read = True
            self.clock.advance(51)
        return hash("child")

    def __eq__(self, other):
        return other == "child"


class DeadlineCpuTest(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.enterContext(patch("nci_sip_mcp.bounds.monotonic", self.clock.monotonic))

    def walk(self, evs, kinds, **limits):
        with correlated(), deadline_scope(50):
            return traverse_ncit(
                evs, ["C1"], release(), kinds, Budget(**limits), exclusions=frozenset()
            )

    def assert_graph(self, graph, nodes, edges):
        self.assertEqual([node.code for node in graph.nodes], nodes)
        self.assertEqual(
            [(edge.source_code, edge.target_code, edge.edge_type) for edge in graph.edges], edges
        )
        self.assertTrue(graph.deadline_exhausted)

    def assert_deadline(self, graph):
        self.assertEqual(
            graph.truncation.to_dict(),
            {
                "occurred": True,
                "bound": "deadline",
                "limit": 50000,
                "reached": 51000,
                "omitted": 0,
                "exact": False,
            },
        )

    def test_relation_grouping_expiry_stops_before_the_next_verified_relation(self):
        evs = FakeEVS([concept("C1", children=[{"code": "C2"}, {"code": "C3"}])])
        relation_kinds = [CostlyRelation(self.clock), CostlyRelation(self.clock)]
        original = _Walk._payload_relations

        def costly(frontier, concepts, kinds):
            found = original(frontier, concepts, kinds)
            return [
                (code, relation_kinds[index], item)
                for index, (code, edge, item) in enumerate(found)
            ]

        with patch.object(_Walk, "_payload_relations", side_effect=costly):
            graph = self.walk(evs, ["child"], depth=2)
        self.assert_graph(graph, ["C1"], [])
        self.assert_deadline(graph)
        self.assertEqual(evs.calls, [("get_concepts_by_codes", "ncit_26.06e", ["C1"])])

    def test_expiry_while_emitting_an_edge_stops_before_the_next_edge(self):
        """Given verified relations, when emitting one consumes time, later edges stay absent."""
        evs = FakeEVS(
            [
                concept(
                    "C1",
                    children=[
                        {"code": "C2", "name": CostlyText("Child", self.clock)},
                        {"code": "C3", "name": "Later"},
                    ],
                )
            ]
        )
        graph = self.walk(evs, ["child"], depth=2)
        self.assert_graph(graph, ["C1", "C2"], [("C1", "C2", "child")])
        self.assert_deadline(graph)
        self.assertEqual(evs.calls, [("get_concepts_by_codes", "ncit_26.06e", ["C1"])])

    def test_expiry_during_reconciliation_keeps_the_first_kind_cut(self):
        """Given dropped roles, expiry restoring a shared target stops before the next retry."""
        processing = ReconciliationClock(self.clock)
        evs = FakeEVS(
            [
                concept(
                    "C1",
                    roles=[
                        {"relatedCode": "C2", "type": "First"},
                        {"relatedCode": "C3", "type": "Shared"},
                        {"relatedCode": "C3", "type": "Later"},
                    ],
                    associations=[
                        {"relatedCode": "C1", "type": "Self"},
                        {"relatedCode": "C1", "type": "Another self relation"},
                        {
                            "relatedCode": "C3",
                            "type": "Admits shared target",
                            "relatedName": processing,
                        },
                    ],
                ),
                concept("C2"),
                concept("C3"),
            ]
        )
        with patch("nci_sip_mcp.traversal.json.dumps", side_effect=processing.encode_identity):
            graph = self.walk(evs, ["role", "association"], depth=2, per_kind=1)
        self.assert_graph(
            graph,
            ["C1", "C2", "C3"],
            [
                ("C1", "C2", "role"),
                ("C1", "C1", "association"),
                ("C1", "C1", "association"),
                ("C1", "C3", "association"),
                ("C1", "C3", "role"),
            ],
        )
        prior = {
            "occurred": True,
            "bound": "kind_budget",
            "limit": 1,
            "reached": 1,
            "omitted": 0,
            "exact": False,
        }
        expired = {
            "occurred": True,
            "bound": "deadline",
            "limit": 50000,
            "reached": 51000,
            "omitted": 0,
            "exact": False,
        }
        self.assertEqual(
            graph.truncation.to_dict(),
            prior | {"perKind": {"role": prior, "association": expired}},
        )
        self.assertEqual(evs.calls, [("get_concepts_by_codes", "ncit_26.06e", ["C1"])])

    def test_descendant_ingestion_expiry_does_not_interpret_the_next_item(self):
        """Given a verified seed, expiry during iteration stops before validating later data."""
        evs = FakeEVS(
            [concept("C1")], descendants={"C1": [{"code": "C2", "name": "Child", "level": 1}]}
        )
        fetch = evs.get_descendants

        def ingest(*args, **kwargs):
            return CostlyDescendants(fetch(*args, **kwargs), self.clock)

        with patch.object(evs, "get_descendants", side_effect=ingest):
            graph = self.walk(evs, ["descendant"], depth=2)
        self.assert_graph(graph, ["C1"], [])
        self.assert_deadline(graph)
        self.assertEqual(
            evs.calls,
            [
                ("get_concepts_by_codes", "ncit_26.06e", ["C1"]),
                ("get_descendants", "ncit_26.06e", ("C1", 2)),
            ],
        )

    def test_final_depth_expiry_keeps_only_the_continuation_already_examined(self):
        """Given a final frontier, expiry inspecting its first child leaves later cuts unknown."""
        evs = FakeEVS(
            [
                concept("C1", children=[{"code": "C2"}]),
                concept(
                    "C2",
                    children=[
                        {"code": CostlyText("C3", self.clock)},
                        {"code": "C4"},
                    ],
                ),
            ]
        )
        graph = self.walk(evs, ["child"], depth=1)
        self.assert_graph(graph, ["C1", "C2"], [("C1", "C2", "child")])
        self.assertEqual(
            graph.truncation.to_dict(),
            {
                "occurred": True,
                "bound": "depth",
                "limit": 1,
                "reached": 1,
                "omitted": 1,
                "exact": False,
            },
        )
        self.assertEqual(
            evs.calls,
            [
                ("get_concepts_by_codes", "ncit_26.06e", ["C1"]),
                ("get_concepts_by_codes", "ncit_26.06e", ["C2"]),
            ],
        )
