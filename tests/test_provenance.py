"""The provenance of every item a tool returns, and the truncation record of a bounded result.

The fields come from spec/records.yaml (`provenance`, `traversal`, `truncation`); the tests read
the specification, so a field added or renamed there fails here.
"""

import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest import TestCase

import yaml
from pydantic import TypeAdapter

from fakes import FakeEVS, concept, release
from nci_sip_mcp import results
from nci_sip_mcp.config import Settings
from nci_sip_mcp.context import Context
from nci_sip_mcp.errors import call_correlation_id, correlated
from nci_sip_mcp.http_client import UpstreamTooLargeError, UpstreamUnavailableError
from nci_sip_mcp.index_adapter import LocalIndexAdapter, results_truncation
from nci_sip_mcp.models import NcitConcept, TraversalProvenance, utc_now_iso
from nci_sip_mcp.registry import invoke
from ncit_index.embeddings import HashingEmbeddingProvider
from ncit_index.index import LocalIndex
from test_index import normalized, synthetic_concepts
from test_traversal import complete_graph

RECORDS = yaml.safe_load((Path(__file__).parent.parent / "spec/records.yaml").read_text())
PROVENANCE = RECORDS["provenance"]["fields"]
TRAVERSAL = RECORDS["traversal"]["fields"]
TRUNCATION = RECORDS["truncation"]["fields"]
# Fields the record lists but a server supplies only where its source does: graphs belong to
# items the Shared SI Service serves, and qualifiers and
# evidence are what the platform attaches to a relation.
CONDITIONAL = {"graphs", "qualifiers", "evidence"}
REQUIRED = {name for name, spec in PROVENANCE.items() if not spec.get("optional")} - CONDITIONAL
REQUIRED_TRAVERSAL = {name for name in TRAVERSAL if name not in CONDITIONAL}
ALLOWED = set(PROVENANCE)
ALLOWED_TRAVERSAL = ALLOWED | set(TRAVERSAL)


def related(name, code, relationship_code="R1"):
    return {
        "code": relationship_code,
        "type": name,
        "relatedCode": code,
        "relatedName": f"Concept {code}",
    }


def child(code):
    return {"code": code, "name": f"Concept {code}"}


NEOPLASM = concept(
    "C3262",
    "Neoplasm",
    parents=[child("C2991")],
    children=[child("C4741"), child("C4742")],
    roles=[related("Disease_Has_Abnormal_Cell", "C12922", "R100")],
    inverseRoles=[related("Gene_Associated_With_Disease", "C16612", "R101")],
    associations=[related("Concept_In_Subset", "C165258", "A8")],
)
KINASE = concept("C40704", "Receptor Tyrosine Kinase Inhibition")


class ProvenanceTestCase(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name)
        # The tests change the payloads, so each gets its own.
        payloads = [NEOPLASM, KINASE, *(concept(code) for code in ("C2991", "C4741", "C4742"))]
        self.evs = complete_graph(FakeEVS(deepcopy(payloads)))
        self.context = Context(
            Settings(data_dir=self.path),
            evs=self.evs,
            index=LocalIndexAdapter(LocalIndex(self.path)),
            embedding_provider=HashingEmbeddingProvider(),
        )

    def assert_record(self, provenance, required, allowed):
        """The provenance holds exactly the record's fields: all it requires, none beyond."""

        self.assertLessEqual(required, set(provenance), provenance)
        self.assertLessEqual(set(provenance), allowed, provenance)

    def traversal(self, **arguments):
        arguments = {"start_codes": ["C3262"], "max_depth": 1} | arguments
        complete_graph(self.evs)
        result = invoke(self.context, "traverse", **arguments)
        self.assertNotIn("error", result, result)
        return result

    def edge(self, result, target):
        (edge,) = [edge for edge in result["edges"] if edge["target_code"] == target]
        return edge


class EveryItemCarriesItsProvenanceTest(ProvenanceTestCase):
    def test_a_looked_up_concept_carries_the_provenance_record(self):
        provenance = invoke(self.context, "lookup", "C3262")["provenance"]

        self.assert_record(provenance, REQUIRED, ALLOWED)
        self.assertEqual(
            provenance["release"],
            {"terminology": "ncit", "identifier": "26.06e", "date": "2026-06-29"},
        )
        self.assertEqual((provenance["source"], provenance["servedBy"]), ("evs_rest", "live"))
        self.assertEqual(
            provenance["sourceUri"], "https://evs.test/api/v1/concept/ncit_26.06e/C3262"
        )
        self.assertRegex(provenance["retrievedAt"], r"^\d{4}-\d\d-\d\dT[\d:.]+Z$")

    def test_every_source_and_way_of_serving_is_a_value_of_the_record(self):
        invoke(self.context, "index_codes", ["C3262"])
        live = invoke(self.context, "lookup", "C3262")["provenance"]
        indexed = invoke(self.context, "search", "neoplasm")["hits"][0]["concept"]["provenance"]
        nodes = self.traversal()["nodes"]

        for provenance in (live, indexed, nodes[0]["provenance"]):
            self.assertIn(provenance["source"], PROVENANCE["source"]["values"])
            self.assertIn(provenance["servedBy"], PROVENANCE["servedBy"]["values"])

    def test_every_node_and_edge_of_a_traversal_carries_the_traversal_record(self):
        result = self.traversal(direction="both")

        for item in [*result["nodes"], *result["edges"]]:
            provenance = item["provenance"]
            required = REQUIRED | REQUIRED_TRAVERSAL
            # The concept asked about is reached by no relationship.
            if provenance["depth"] == 0:
                required -= {"relationship", "direction", "polarity"}
            self.assert_record(provenance, required, ALLOWED_TRAVERSAL)
            self.assertIn(provenance.get("polarity", "positive"), TRAVERSAL["polarity"]["values"])

    def test_a_search_hit_carries_the_record_and_a_result_with_hits_does_not(self):
        invoke(self.context, "index_codes", ["C3262"])

        result = invoke(self.context, "search", "neoplasm")

        self.assert_record(result["hits"][0]["concept"]["provenance"], REQUIRED, ALLOWED)
        self.assertNotIn("provenance", result)

    def test_a_search_without_hits_carries_the_provenance_itself(self):
        invoke(self.context, "index_codes", ["C3262"])

        result = invoke(self.context, "search", "zzzz", mode="bm25")

        self.assertEqual(result["hits"], [])
        # Nothing was produced from an upstream URL, so there is none to name.
        self.assert_record(result["provenance"], REQUIRED - {"sourceUri"}, ALLOWED)
        self.assertEqual(result["provenance"]["servedBy"], "index")

    def test_the_release_report_and_the_index_manifest_carry_it(self):
        invoke(self.context, "index_codes", ["C3262"])

        report = invoke(
            self.context,
            "release_info",
        )["provenance"]
        manifest = invoke(
            self.context,
            "index_resource",
            "26.06e",
        )["provenance"]

        self.assert_record(report, REQUIRED, ALLOWED)
        self.assertEqual(report["release"]["identifier"], "26.06e")
        self.assertEqual(report["sourceUri"], "https://evs.test/api/v1/metadata/terminologies")
        self.assert_record(manifest, REQUIRED - {"sourceUri"}, ALLOWED)
        self.assertEqual((manifest["source"], manifest["servedBy"]), ("evs_index", "index"))

    def test_the_release_report_names_no_release_when_evs_selected_none(self):
        self.evs.errors = {"get_terminologies": UpstreamUnavailableError("down")}

        report = invoke(
            self.context,
            "release_info",
        )

        self.assertEqual(report["selected_release"]["error"]["code"], "upstream_unavailable")
        self.assertNotIn("provenance", report)


class TheReleaseOfAnItemIsTheReleaseServedTest(ProvenanceTestCase):
    def test_every_item_names_the_release_the_call_was_pinned_to(self):
        self.evs.release = release("26.07d", "2026-07-27")
        for code in self.evs.concepts:
            self.evs.concepts[code] = dict(
                self.evs.concepts.get(code, concept(code)), version="26.07d"
            )
        self.evs.concepts["C3262"]["children"] = [child("C4741"), child("C4742")]

        items = [invoke(self.context, "lookup", "C3262")]
        result = self.traversal()
        items += [*result["nodes"], *result["edges"]]

        self.assertEqual(
            {item["provenance"]["release"]["identifier"] for item in items}, {"26.07d"}
        )
        self.assertEqual({item["provenance"]["release"]["date"] for item in items}, {"2026-07-27"})

    def test_an_indexed_concept_names_the_release_it_was_indexed_from(self):
        invoke(self.context, "index_codes", ["C3262"])
        self.evs.release = release("26.07d", "2026-07-27")

        hit = invoke(self.context, "search", "neoplasm")["hits"][0]

        self.assertEqual(hit["concept"]["provenance"]["release"]["identifier"], "26.06e")


class CorrelationTest(ProvenanceTestCase):
    def test_every_item_of_a_call_carries_the_calls_identifier(self):
        with correlated("call-42"):
            result = self.traversal(direction="both")
            lookup = invoke(self.context, "lookup", "C3262")

        items = [lookup, *result["nodes"], *result["edges"]]
        self.assertEqual({item["provenance"]["correlationId"] for item in items}, {"call-42"})

    def test_a_call_outside_any_adapter_is_one_call_with_one_identifier(self):
        result = self.traversal(direction="both")

        identifiers = {item["provenance"]["correlationId"] for item in result["nodes"]}
        identifiers |= {item["provenance"]["correlationId"] for item in result["edges"]}
        self.assertEqual(len(identifiers), 1)
        self.assertTrue(identifiers.pop())

    def test_an_item_cannot_be_built_outside_any_call(self):
        with self.assertRaises(RuntimeError):
            call_correlation_id()


class AnItemReachedByTraversalSaysHowTest(ProvenanceTestCase):
    def test_the_concept_asked_about_has_depth_zero_and_no_relationship(self):
        start = self.traversal()["nodes"][0]["provenance"]

        self.assertEqual(start["depth"], 0)
        for name in ("relationship", "direction", "polarity"):
            self.assertNotIn(name, start)

    def test_a_role_names_its_relationship_by_code_and_name(self):
        edge = self.edge(self.traversal(edge_types=["role"]), "C12922")["provenance"]

        self.assertEqual(
            (edge["depth"], edge["direction"], edge["polarity"]), (1, "out", "positive")
        )
        self.assertEqual(
            edge["relationship"],
            {"kind": "role", "code": "R100", "name": "Disease_Has_Abnormal_Cell"},
        )

    def test_an_inverse_association_is_followed_inward_and_an_association_outward(self):
        self.evs.concepts["C3262"]["inverseAssociations"] = [related("Has_Value", "C777", "A9")]

        result = self.traversal(direction="both", edge_types=["association", "inverse_association"])

        by_target = {edge["target_code"]: edge["provenance"] for edge in result["edges"]}
        self.assertEqual(by_target["C165258"]["direction"], "out")
        self.assertEqual(by_target["C777"]["direction"], "in")
        self.assertEqual(by_target["C777"]["relationship"]["kind"], "association")

    def test_a_hierarchy_link_has_a_kind_an_empty_name_and_no_invented_code(self):
        result = self.traversal(direction="both", edge_types=["child", "parent"])

        child_edge = self.edge(result, "C4741")["provenance"]
        parent_edge = self.edge(result, "C2991")["provenance"]
        self.assertEqual(child_edge["relationship"], {"kind": "child", "name": ""})
        self.assertEqual(parent_edge["relationship"], {"kind": "parent", "name": ""})
        self.assertEqual((child_edge["direction"], parent_edge["direction"]), ("out", "in"))
        self.assertEqual(child_edge["polarity"], "positive")

    def test_a_node_carries_the_edge_that_reached_it_and_its_own_uri(self):
        result = self.traversal(edge_types=["role"])

        (node,) = [n["provenance"] for n in result["nodes"] if n["code"] == "C12922"]
        edge = self.edge(result, "C12922")["provenance"]

        self.assertEqual(
            {key: node[key] for key in ("depth", "relationship", "direction", "polarity")},
            {key: edge[key] for key in ("depth", "relationship", "direction", "polarity")},
        )
        self.assertEqual(node["sourceUri"], "https://evs.test/api/v1/concept/ncit_26.06e/C12922")
        # The relation was read from the concept that holds it.
        self.assertEqual(edge["sourceUri"], "https://evs.test/api/v1/concept/ncit_26.06e/C3262")

    def test_an_edge_has_the_depth_of_the_node_it_reaches(self):
        self.evs.concepts["C4741"]["children"] = [child("C9")]
        self.evs.concepts["C9"] = concept("C9")

        result = self.traversal(max_depth=2, edge_types=["child"])

        self.assertEqual(
            {(edge["target_code"], edge["provenance"]["depth"]) for edge in result["edges"]},
            {("C4741", 1), ("C4742", 1), ("C9", 2)},
        )
        nodes = {node["code"]: node["provenance"]["depth"] for node in result["nodes"]}
        self.assertEqual(nodes, {"C3262": 0, "C4741": 1, "C4742": 1, "C9": 2})

    def test_a_descendant_edge_is_read_from_the_descendants_of_its_start_code(self):
        self.evs.descendants = {"C3262": [{"code": "C4741", "name": "N", "level": 1}]}

        edge = self.edge(self.traversal(edge_types=["descendant"]), "C4741")["provenance"]

        self.assertEqual(edge["relationship"], {"kind": "descendant", "name": ""})
        self.assertTrue(edge["sourceUri"].endswith("/ncit_26.06e/C3262/descendants"))

    def test_polarity_follows_the_relationship_code_never_its_name(self):
        exclusion = related("Disease_Excludes_Finding", "C500", "R135")
        lookalike = related("Disease_Excludes_Finding", "C501", "R999")
        self.evs.concepts["C3262"]["roles"] = [exclusion, lookalike]

        result = self.traversal(edge_types=["role"])

        self.assertEqual(self.edge(result, "C500")["provenance"]["polarity"], "negative")
        self.assertEqual(self.edge(result, "C501")["provenance"]["polarity"], "positive")


class UpstreamPassThroughTest(ProvenanceTestCase):
    def test_what_evs_says_of_a_concepts_origin_is_passed_through_unchanged(self):
        provenance = invoke(self.context, "lookup", "C3262")["provenance"]

        self.assertEqual(provenance["upstream"], {"terminology": "ncit", "version": "26.06e"})

    def test_an_indexed_concept_does_not_claim_a_live_upstream_origin(self):
        invoke(self.context, "index_codes", ["C3262"])

        provenance = invoke(self.context, "search", "neoplasm")["hits"][0]["concept"]["provenance"]

        self.assertEqual({"sourceUri", "upstream"} & provenance.keys(), set())

    def test_upstream_is_absent_where_the_platform_supplied_nothing(self):
        bare = NcitConcept(
            code="C1",
            preferred_name="One",
            source_vocabulary="NCI Thesaurus",
            terminology="ncit",
            release_version="26.06e",
            release_date=None,
            retrieved_at="2026-07-09T00:00:00Z",
            source="live_evs",
            raw={"code": "C1", "name": "One", "unrelated": "x"},
        )

        with correlated("call-1"):
            provenance = bare.to_dict("https://evs.test/c1")["provenance"]

        self.assertNotIn("upstream", provenance)
        # A release EVS gave no date for is named without one, never with null.
        self.assertEqual(provenance["release"], {"terminology": "ncit", "identifier": "26.06e"})

    def test_a_start_code_passes_through_what_its_payload_gave_and_nothing_else(self):
        result = self.traversal(direction="both")

        by_code = {node["code"]: node["provenance"] for node in result["nodes"]}
        self.assertEqual(by_code["C3262"]["upstream"], {"terminology": "ncit", "version": "26.06e"})
        # A node named by a relation list was told nothing of its origin, nor was an edge.
        reached = [by_code[code] for code in by_code if code != "C3262"]
        for provenance in [*reached, *(edge["provenance"] for edge in result["edges"])]:
            self.assertNotIn("upstream", provenance)

    def test_a_full_start_payload_without_its_terminology_fails_closed(self):
        del self.evs.concepts["C3262"]["terminology"]

        result = invoke(self.context, "traverse", ["C3262"])

        self.assertEqual(result["error"]["code"], "upstream_unavailable")


class RawIsKeptBehindTheFlagTest(ProvenanceTestCase):
    def test_a_result_carries_no_raw_payload_unless_asked_for(self):
        invoke(self.context, "index_codes", ["C3262"])

        self.assertNotIn("raw", invoke(self.context, "lookup", "C3262"))
        self.assertIn("raw", invoke(self.context, "lookup", "C3262", include_raw=True))
        self.assertNotIn("raw", invoke(self.context, "search", "neoplasm")["hits"][0]["concept"])
        self.assertIn(
            "raw",
            invoke(self.context, "search", "neoplasm", include_raw=True)["hits"][0]["concept"],
        )


class TruncationTest(ProvenanceTestCase):
    def assert_record(self, truncation, expected):
        self.assertEqual(truncation, expected)
        self.assertLessEqual(set(truncation), set(TRUNCATION))
        if truncation["occurred"]:
            self.assertIsInstance(truncation["omitted"], int)
            self.assertIn(truncation["bound"], TRUNCATION["bound"]["values"])
            self.assertIsInstance(truncation["exact"], bool)

    def test_a_walk_no_bound_stopped_holds_occurred_false_and_nothing_else(self):
        self.assert_record(self.traversal()["truncation"], {"occurred": False})

    def test_the_node_limit_reports_the_concepts_it_dropped(self):
        truncation = self.traversal(edge_types=["child"], max_nodes=2)["truncation"]

        # Two of the three nodes fit: the start code and one child; one child was dropped.
        self.assert_record(
            truncation,
            {
                "occurred": True,
                "bound": "nodes",
                "limit": 2,
                "reached": 2,
                "omitted": 1,
                "exact": False,
            },
        )

    def test_the_edge_limit_reports_the_edges_it_dropped(self):
        truncation = self.traversal(edge_types=["child", "role"], max_edges=1)["truncation"]

        per_kind = truncation.pop("perKind")
        for kind in ("child", "role"):
            self.assert_record(
                per_kind[kind],
                {
                    "occurred": True,
                    "bound": "edges",
                    "limit": 1,
                    "reached": 1,
                    "omitted": 1,
                    "exact": False,
                },
            )

        self.assert_record(
            truncation,
            {
                "occurred": True,
                "bound": "edges",
                "limit": 1,
                "reached": 1,
                "omitted": 2,
                "exact": False,
            },
        )

    def test_an_edge_to_a_node_the_node_limit_dropped_counts_for_the_node_bound(self):
        truncation = self.traversal(edge_types=["child", "role"], max_nodes=1, max_edges=1)

        self.assertEqual(truncation["truncation"]["bound"], "nodes")
        self.assertEqual(truncation["edges"], [])

    def test_the_first_bound_reached_is_the_one_reported(self):
        # A parallel edge to the child kept is dropped by the edge limit before the node limit
        # drops the role's new target: the edge limit is the bound reported.
        self.evs.concepts["C3262"]["children"] = [child("C4741")]
        self.evs.concepts["C3262"]["roles"] = [
            related("Is_Parallel_To", "C4741", "R50"),
            related("Has_Other", "C12922", "R51"),
        ]

        result = self.traversal(edge_types=["child", "role"], max_nodes=2, max_edges=1)

        self.assertEqual(result["truncation"]["bound"], "edges")
        self.assertEqual(result["truncation"]["omitted"], 1)

    def test_a_concept_too_large_to_read_is_reported_against_the_upstream_cap(self):
        class Hub(FakeEVS):
            def get_concepts_by_codes(self, codes, release, include=""):
                if include != "minimal":
                    raise UpstreamTooLargeError("too large")
                return super().get_concepts_by_codes(codes, release, include)

        self.context.evs = Hub([NEOPLASM])
        with self.assertLogs("nci_sip_mcp.traversal", level="WARNING"):
            truncation = self.traversal(edge_types=["child"])["truncation"]

        self.assert_record(
            truncation,
            {
                "occurred": True,
                "bound": "upstream_cap",
                "limit": 1_000_000,
                "reached": 1_000_000,
                "omitted": 1,
                "exact": False,
            },
        )

    def test_depth_zero_reports_the_known_unseen_children(self):
        self.assert_record(
            self.traversal(max_depth=0, edge_types=["child"])["truncation"],
            {
                "occurred": True,
                "bound": "depth",
                "limit": 0,
                "reached": 0,
                "omitted": 2,
                "exact": False,
            },
        )

    def test_the_search_limit_reports_the_concepts_it_left_out(self):
        invoke(self.context, "index_codes", ["C3262", "C40704"])

        result = invoke(self.context, "search", "tumor", limit=1, mode="vector")

        # Every concept of a small index is scored, so the count is exact.
        self.assert_record(
            result["truncation"],
            {
                "occurred": True,
                "bound": "results",
                "limit": 1,
                "reached": 1,
                "omitted": 1,
                "exact": True,
            },
        )
        self.assert_record(
            invoke(self.context, "search", "tumor", mode="vector")["truncation"],
            {"occurred": False},
        )


class Clock:
    """The call times before and after a call: every live item was retrieved between them."""

    def __enter__(self):
        self.before = utc_now_iso()
        return self

    def __exit__(self, *exc):
        self.after = utc_now_iso()

    def holds(self, retrieved_at):
        return self.before <= retrieved_at <= self.after


class NoUpstreamUrlTest(ProvenanceTestCase):
    def test_a_result_no_upstream_url_produced_names_none(self):
        invoke(self.context, "index_codes", ["C3262"])

        empty = invoke(self.context, "search", "zzzz", mode="bm25")["provenance"]
        manifest = invoke(
            self.context,
            "index_resource",
            "26.06e",
        )["provenance"]

        self.assertNotIn("sourceUri", empty)
        self.assertNotIn("sourceUri", manifest)


class RetrievedAtTest(ProvenanceTestCase):
    def test_an_indexed_concept_and_the_manifest_keep_the_time_they_were_stored_with(self):
        invoke(self.context, "index_codes", ["C3262"])
        stored = self.context.index.get_concept_snapshot("C3262")[1].retrieved_at
        built = self.context.index.get_active_manifest().built_at

        first = invoke(self.context, "search", "neoplasm")["hits"][0]["concept"]["provenance"]
        second = invoke(self.context, "search", "neoplasm")["hits"][0]["concept"]["provenance"]
        manifest = invoke(
            self.context,
            "index_resource",
            "26.06e",
        )["provenance"]
        empty = invoke(self.context, "search", "zzzz", mode="bm25")["provenance"]

        self.assertEqual((first["retrievedAt"], second["retrievedAt"]), (stored, stored))
        self.assertEqual((manifest["retrievedAt"], empty["retrievedAt"]), (built, built))

    def test_a_live_item_was_retrieved_during_its_call(self):
        with Clock() as lookup:
            concept_provenance = invoke(self.context, "lookup", "C3262")["provenance"]
        with Clock() as walk:
            result = self.traversal(direction="both")

        self.assertTrue(lookup.holds(concept_provenance["retrievedAt"]))
        for item in [*result["nodes"], *result["edges"]]:
            self.assertTrue(walk.holds(item["provenance"]["retrievedAt"]))

    def test_the_release_report_was_retrieved_during_its_call(self):
        with Clock() as call:
            report = invoke(
                self.context,
                "release_info",
            )["provenance"]

        self.assertTrue(call.holds(report["retrievedAt"]))
        self.assertEqual(
            (report["servedBy"], report["source"], report["release"]["date"]),
            ("live", "evs_rest", "2026-06-29"),
        )


class CorrelationOfEveryResultTest(ProvenanceTestCase):
    def test_every_kind_of_result_carries_the_identifier_of_the_call(self):
        invoke(self.context, "index_codes", ["C3262"])
        self.evs.errors = {"get_concept": UpstreamUnavailableError("down")}

        with correlated("call-42"), self.assertLogs("nci_sip_mcp", level="WARNING"):
            fallback = invoke(self.context, "lookup", "C3262")["provenance"]
        with correlated("call-42"):
            report = invoke(
                self.context,
                "release_info",
            )["provenance"]
            manifest = invoke(
                self.context,
                "index_resource",
                "26.06e",
            )["provenance"]
            empty = invoke(self.context, "search", "zzzz", mode="bm25")["provenance"]
            hit = invoke(self.context, "search", "neoplasm")["hits"][0]["concept"]["provenance"]

        ids = {each["correlationId"] for each in (fallback, report, manifest, empty, hit)}
        self.assertEqual(ids, {"call-42"})

    def test_the_manifest_of_a_build_and_of_the_report_carry_the_record_too(self):
        built = invoke(self.context, "index_codes", ["C3262"])
        report = invoke(
            self.context,
            "release_info",
        )["active_index"]

        for manifest in (built, report):
            self.assertEqual(manifest["provenance"]["source"], "evs_index")
            self.assertEqual(manifest["provenance"]["release"]["identifier"], "26.06e")
        self.assertEqual(
            invoke(
                self.context,
                "index_resource",
                "26.06e",
            )["provenance"]["servedBy"],
            "index",
        )


class ResultShapesTest(ProvenanceTestCase):
    """No field of the old shape comes back, and nothing is added without a record for it."""

    CONCEPT = frozenset(
        {
            "code",
            "preferred_name",
            "source_vocabulary",
            "terminology",
            "evidence",
            "provenance",
        }
    )

    def test_a_concept_item_has_exactly_these_fields(self):
        invoke(self.context, "index_codes", ["C3262"])
        self.evs.errors = {"get_concept": UpstreamUnavailableError("down")}

        with self.assertLogs("nci_sip_mcp", level="WARNING"):
            fallback = invoke(self.context, "lookup", "C3262")
        hit = invoke(self.context, "search", "neoplasm")["hits"][0]
        del self.evs.errors["get_concept"]
        live = invoke(self.context, "lookup", "C3262")

        self.assertEqual(set(live), self.CONCEPT)
        self.assertEqual(set(fallback), self.CONCEPT | {"fallback"})
        self.assertEqual(set(hit["concept"]), self.CONCEPT)
        self.assertEqual(set(hit), {"concept", "score", "rank", "score_components", "matched_on"})

    def test_a_search_result_has_exactly_these_fields(self):
        invoke(self.context, "index_codes", ["C3262"])

        found = invoke(self.context, "search", "neoplasm")
        empty = invoke(self.context, "search", "zzzz", mode="bm25")

        self.assertEqual(set(found), {"query", "mode", "hits", "truncation"})
        self.assertEqual(set(empty), {"query", "mode", "hits", "truncation", "provenance"})

    def test_a_traversal_result_and_its_items_have_exactly_these_fields(self):
        result = self.traversal()

        self.assertEqual(
            set(result),
            {"start_codes", "nodes", "edges", "truncation", "max_depth", "max_nodes", "max_edges"},
        )
        self.assertEqual(
            set(result["nodes"][0]),
            {"code", "preferred_name", "terminology", "source_vocabulary", "provenance"},
        )
        self.assertEqual(
            set(result["edges"][0]),
            {
                "source_code",
                "target_code",
                "edge_type",
                "target_name",
                "source_name",
                "provenance",
            },
        )


class EdgeTypeProvenanceTest(ProvenanceTestCase):
    def test_each_edge_type_says_its_kind_and_direction(self):
        self.evs.concepts["C3262"].update(
            parents=[child("C1")],
            children=[child("C2")],
            roles=[related("Has_Role", "C3", "R1")],
            inverseRoles=[related("Role_Of", "C4", "R2")],
            associations=[related("Has_Assoc", "C5", "A1")],
            inverseAssociations=[related("Assoc_Of", "C6", "A2")],
        )
        self.evs.descendants = {"C3262": [{"code": "C7", "name": "D", "level": 1}]}
        expected = {
            "parent": ("parent", "in"),
            "child": ("child", "out"),
            "descendant": ("descendant", "out"),
            "role": ("role", "out"),
            "inverse_role": ("role", "in"),
            "association": ("association", "out"),
            "inverse_association": ("association", "in"),
        }

        result = self.traversal(direction="both", edge_types=sorted(expected))

        seen = {
            edge["edge_type"]: (
                edge["provenance"]["relationship"]["kind"],
                edge["provenance"]["direction"],
            )
            for edge in result["edges"]
        }
        self.assertEqual(seen, expected)

    def test_a_relation_without_a_code_or_a_type_is_named_after_its_edge_type(self):
        bare = {"relatedCode": "C5", "relatedName": "Five"}
        self.evs.concepts["C3262"].update(roles=[bare], associations=[bare])

        result = self.traversal(edge_types=["role", "association"])

        kinds = [edge["provenance"]["relationship"] for edge in result["edges"]]
        self.assertEqual(
            kinds,
            [{"kind": "role", "name": "role"}, {"kind": "association", "name": "association"}],
        )
        self.assertEqual({edge["provenance"]["polarity"] for edge in result["edges"]}, {"positive"})

    def test_traversal_items_are_live_and_index_items_are_indexed(self):
        invoke(self.context, "index_codes", ["C3262"])
        result = self.traversal(direction="both")

        for item in [*result["nodes"], *result["edges"]]:
            self.assertEqual(
                (item["provenance"]["source"], item["provenance"]["servedBy"]),
                ("evs_rest", "live"),
            )
        indexed = [
            invoke(self.context, "search", "neoplasm")["hits"][0]["concept"]["provenance"],
            invoke(self.context, "search", "zzzz", mode="bm25")["provenance"],
            invoke(
                self.context,
                "index_resource",
                "26.06e",
            )["provenance"],
        ]
        for provenance in indexed:
            self.assertEqual((provenance["source"], provenance["servedBy"]), ("evs_index", "index"))


class ExactnessBoundaryTest(ProvenanceTestCase):
    def search(self, index, limit, mode):
        _, total, _ = LocalIndexAdapter(index).search_page(
            "alpha1", HashingEmbeddingProvider(), limit, mode
        )
        return results_truncation(total, limit)

    def build(self, count):
        index = LocalIndex(self.path)
        index.upsert_concepts(
            normalized(synthetic_concepts(count), "2026-06-29"),
            "2026-06-29",
            HashingEmbeddingProvider(),
        )
        return index

    def test_vector_truncation_counts_every_indexed_concept(self):
        index = self.build(30)
        truncation = self.search(index, 1, "hybrid")
        self.assertEqual((truncation.exact, truncation.omitted), (True, 29))

    def test_term_truncation_counts_all_matches_across_page_sizes(self):
        index = self.build(1200)

        # Every one of the 120 term matches counts, for either page size.
        at_cap = self.search(index, 12, "bm25")
        below_cap = self.search(index, 13, "bm25")

        self.assertEqual((at_cap.exact, at_cap.omitted), (True, 108))
        self.assertEqual((below_cap.exact, below_cap.omitted), (True, 107))


ROOT = Path(__file__).resolve().parents[1]


class ProvenanceRecordSchemaTest(TestCase):
    def test_provenance_graphs_are_optional_in_the_record_and_in_the_schema(self):
        # EVS items carry no graphs; only the Shared SI Service's do.
        fields = yaml.safe_load((ROOT / "spec/records.yaml").read_text())["provenance"]["fields"]
        schema = TypeAdapter(results.Provenance).json_schema()
        self.assertTrue(fields["graphs"].get("optional"))
        self.assertNotIn("graphs", schema["required"])

    def test_a_traversal_relationship_requires_its_kind_and_name(self):
        # The record gives the shape as { code?, name, kind } in prose.
        schema = TypeAdapter(results.Relationship).json_schema()
        self.assertEqual(set(schema["required"]), {"kind", "name"})
        self.assertEqual(set(schema["properties"]), {"kind", "name", "code"})

    def test_empty_qualifiers_and_evidence_pass_through_unchanged(self):
        for empty in ({}, [], "", False, 0):
            with self.subTest(empty=empty):
                record = TraversalProvenance(
                    release={"terminology": "ncit", "identifier": "26.06e"},
                    source="evs_rest",
                    served_by="live",
                    retrieved_at="2026-10-01T00:00:00Z",
                    correlation_id="test",
                    depth=1,
                    qualifiers=empty,
                    evidence=empty,
                ).to_dict()
                self.assertEqual(record["qualifiers"], empty)
                self.assertEqual(record["evidence"], empty)
