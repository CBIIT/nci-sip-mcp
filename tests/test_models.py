import json
import unittest

from nci_sip_mcp.errors import correlated
from nci_sip_mcp.models import (
    NcitConcept,
    SearchHit,
    TraversalEdge,
    TraversalNode,
    TraversalProvenance,
    TraversalResult,
    Truncation,
)

NODES, EDGES = 1000, 5000


def provenance(identifier, depth):
    return TraversalProvenance(
        release={"terminology": "ncit", "identifier": "26.06e", "date": "2026-06-29"},
        source="evs_rest",
        served_by="live",
        retrieved_at="2026-10-01T00:00:00Z",
        correlation_id="walk-1",
        source_uri=f"https://evs.example/concept/C{identifier}",
        upstream={"terminology": "ncit", "version": "26.06e"},
        depth=depth,
        relationship={"name": "subClassOf"} if depth else None,
        direction="out" if depth else None,
    )


def maximum_result():
    nodes = [
        TraversalNode(f"C{i}", f"Name {i}", "ncit", provenance(i, i % 3)) for i in range(NODES)
    ]
    edges = [
        TraversalEdge(
            f"C{i % NODES}",
            f"C{i * 7 % NODES}",
            "child",
            "Is_Child" if i % 5 else "",
            provenance(i, 1),
            f"Target {i}",
            f"Source {i}",
        )
        for i in range(EDGES)
    ]
    truncation = Truncation(
        True,
        "max_edges",
        EDGES,
        EDGES,
        12,
        False,
        {"child": Truncation(False), "role": Truncation(True, "max_nodes", 1, 1, 2, True)},
    )
    return TraversalResult(
        ["C1", "C2"], nodes, edges, truncation, 3, NODES, EDGES, {"C1": {"x": 1}}
    )


class TraversalResultSerialisationTest(unittest.TestCase):
    def test_a_maximum_result_keeps_its_counts_order_and_provenance(self):
        data = maximum_result().to_dict()

        self.assertEqual(
            list(data),
            ["start_codes", "nodes", "edges", "truncation", "max_depth", "max_nodes", "max_edges"],
        )
        self.assertEqual((len(data["nodes"]), len(data["edges"])), (NODES, EDGES))
        self.assertEqual(data["start_codes"], ["C1", "C2"])
        self.assertEqual(
            data["nodes"][4],
            {
                "code": "C4",
                "preferred_name": "Name 4",
                "terminology": "ncit",
                "source_vocabulary": "NCI Thesaurus",
                "provenance": {
                    "release": {
                        "terminology": "ncit",
                        "identifier": "26.06e",
                        "date": "2026-06-29",
                    },
                    "source": "evs_rest",
                    "servedBy": "live",
                    "retrievedAt": "2026-10-01T00:00:00Z",
                    "correlationId": "walk-1",
                    "sourceUri": "https://evs.example/concept/C4",
                    "upstream": {"terminology": "ncit", "version": "26.06e"},
                    "depth": 1,
                    "relationship": {"name": "subClassOf"},
                    "direction": "out",
                },
            },
        )
        self.assertNotIn("relationship_name", data["edges"][0])
        self.assertEqual(data["edges"][1]["relationship_name"], "Is_Child")
        self.assertEqual(data["edges"][1]["target_name"], "Target 1")
        self.assertEqual(
            data["truncation"],
            {
                "occurred": True,
                "bound": "max_edges",
                "limit": EDGES,
                "reached": EDGES,
                "omitted": 12,
                "exact": False,
                "perKind": {
                    "child": {"occurred": False},
                    "role": {
                        "occurred": True,
                        "bound": "max_nodes",
                        "limit": 1,
                        "reached": 1,
                        "omitted": 2,
                        "exact": True,
                    },
                },
            },
        )
        self.assertNotIn("concepts", data)
        json.dumps(data)

    def test_the_dict_shares_nothing_mutable_with_the_result(self):
        result = maximum_result()
        data = result.to_dict()
        data["start_codes"].append("C9")
        data["truncation"]["perKind"]["child"]["occurred"] = True
        self.assertEqual(result.start_codes, ["C1", "C2"])
        self.assertFalse(result.truncation.per_kind["child"].occurred)


class SearchHitSerialisationTest(unittest.TestCase):
    def test_a_hit_keeps_its_fields_in_order_and_copies_its_scores(self):
        concept = NcitConcept(
            "C1",
            "One",
            "NCI Thesaurus",
            "ncit",
            "26.06e",
            "2026-06-29",
            "2026-10-01T00:00:00Z",
            "active_cache",
            {},
            {"version": "26.06e", "huge": ["x"] * 3},
        )
        hit = SearchHit(concept, 0.5, 1, "name", {"bm25": 0.25})

        with correlated("hit-1"):
            data = hit.to_dict("https://evs.example/C1")
        data["score_components"]["bm25"] = 9

        self.assertEqual(list(data), ["concept", "score", "rank", "matched_on", "score_components"])
        self.assertNotIn("raw", data["concept"])
        self.assertEqual((data["score"], data["rank"], data["matched_on"]), (0.5, 1, "name"))
        self.assertEqual(hit.score_components, {"bm25": 0.25})
