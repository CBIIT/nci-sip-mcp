"""MCP deadline recovery keeps only verified content, including cursor replay."""

from unittest.mock import patch

from fakes import concept
from nci_sip_mcp import content
from nci_sip_mcp.bounds import Budget, check_deadline, deadline_scope
from nci_sip_mcp.errors import correlated
from nci_sip_mcp.release import ReleaseContext
from nci_sip_mcp.traversal import traverse_ncit
from test_server import ServerFixture


class DeadlineProjectionTest(ServerFixture):
    def test_duplicate_hydration_identity_refuses_the_neighborhood_instead_of_overwriting(self):
        self.children(2)
        self.evs.concepts["C3262"]["inverseRoles"] = [
            {"type": "Related", "relatedCode": "C2", "relatedName": "Child 2"}
        ]
        fetch = self.evs.get_concepts_by_codes
        requested = []

        def duplicate(codes, **kwargs):
            requested.append(tuple(codes))
            rows = fetch(codes, **kwargs)
            return rows + rows if tuple(codes) == ("C2",) else rows

        with patch.object(self.evs, "get_concepts_by_codes", side_effect=duplicate):
            failed, result = self.call(
                "get_concept_neighborhood", code="C3262", kinds=["inverseRole"], depth=1
            )
        self.assertTrue(failed)
        self.assertEqual(result["error"]["code"], "upstream_unavailable")
        self.assertNotIn("nodes", result)
        self.assertEqual(requested, [("C3262",), ("C2",)])

    def test_depth_zero_expiry_preserves_verified_first_batch_of_start_nodes(self):
        """Given multiple seed batches, expiry on the second retains the verified first fifty."""
        self.children(51)
        starts = [f"C{i}" for i in range(1, 52)]
        batches = self.install_deadline(2)
        release = ReleaseContext("ncit", "monthly", "26.06e", None, "ncit_26.06e")
        with correlated(), deadline_scope(50):
            graph = traverse_ncit(
                self.evs, starts, release, ["child"], Budget(depth=0), exclusions=frozenset()
            )
            self.assertEqual(graph.truncation.to_dict(), self.deadline_cut())
        self.assertEqual([node.code for node in graph.nodes], starts[:50])
        self.assertEqual(set(graph.concepts), set(starts[:50]))
        self.assertEqual(graph.edges, [])
        self.assertTrue(graph.deadline_exhausted)
        self.assertEqual(batches, [tuple(starts[:50]), ("C51",)])

    def assert_deadline_projection(self, graph):
        arguments = {
            "terminology": "ncit",
            "release": "26.06e",
            "code": "C3262",
            "direction": "child",
            "depth": 1,
            "limit": 1,
        }
        page = content._hierarchy_page(graph, arguments, 0, 1)
        self.assertEqual([node["code"] for node in page["nodes"]], ["C1"])
        self.assertEqual(page["truncation"]["bound"], "depth")
        self.assertNotIn("nextCursor", page)
        self.assertTrue(graph.deadline_exhausted)
        self.assertNotIn("deadline_exhausted", graph.to_dict())
        self.assertEqual(set(content._graph_record(graph)), {"nodes", "edges", "truncation"})
        empty = content._hierarchy_page(graph, arguments, 50, 1)
        self.assertEqual(empty["nodes"], [])
        self.assertEqual(empty["truncation"], page["truncation"])
        self.assertEqual(empty["provenance"]["release"]["identifier"], "26.06e")
        self.assertNotIn("nextCursor", empty)

    def test_traversal_deadline_retains_depth_without_restarting_hydration_or_paging(self):
        """Given an earlier depth cut, when traversal expires, projection cannot resume it."""
        self.children(101)
        batches = self.install_deadline(3)
        release = ReleaseContext("ncit", "monthly", "26.06e", None, "ncit_26.06e")
        with correlated(), deadline_scope(50):
            graph = content._graph(
                self.context, release, "C3262", ["inverse_role", "child"], Budget(depth=1)
            )
        self.assertEqual(len(batches), 3)
        self.assert_deadline_projection(graph)

    def test_hydration_deadline_retains_depth_and_prevents_restart_or_cursor(self):
        """Given a depth-cut graph, when hydration expires, its partial page has no cursor."""
        self.children(101)
        self.evs.concepts["C3262"]["inverseRoles"] = [
            {"type": "Related", "relatedCode": f"C{i}", "relatedName": f"Child {i}"}
            for i in range(1, 102)
        ]
        batches = self.install_deadline(3)
        release = ReleaseContext("ncit", "monthly", "26.06e", None, "ncit_26.06e")
        budget = Budget(depth=1)
        with correlated(), deadline_scope(50):
            graph = content._graph(self.context, release, "C3262", ["inverse_role"], budget)
        resumed = content._hydrate(self.context, graph, release, budget, ["inverse_role"])
        self.assertEqual(len(batches), 3)
        self.assertEqual(resumed, graph)
        self.assert_deadline_projection(graph)

    def test_depth_cut_without_deadline_still_offers_hierarchy_continuation(self):
        """Given a depth cut alone, when projecting a short page, continuation stays usable."""
        self.children(3)
        self.evs.concepts["C1"]["children"] = [{"code": "C999", "name": "Deeper"}]
        release = ReleaseContext("ncit", "monthly", "26.06e", None, "ncit_26.06e")
        with correlated():
            graph = content._graph(self.context, release, "C3262", ["child"], Budget(depth=1))
        self.assertFalse(graph.deadline_exhausted)
        arguments = {
            "terminology": "ncit",
            "release": "26.06e",
            "code": "C3262",
            "direction": "child",
            "depth": 1,
            "limit": 1,
        }
        first = content._hierarchy_page(graph, arguments, 0, 1)
        self.assertEqual(first["truncation"]["bound"], "depth")
        self.assertIn("nextCursor", first)
        self.assertEqual([node["code"] for node in first["nodes"]], ["C1"])
        second = content._hierarchy_page(graph, arguments, 1, 1)
        self.assertEqual([node["code"] for node in second["nodes"]], ["C2"])

    def install_deadline(self, expiry_batch, *, after=False):
        now = [0.0]
        batches = []
        fetch = self.evs.get_concepts_by_codes

        def guarded(codes, **kwargs):
            batches.append(tuple(codes))
            expires = len(batches) == expiry_batch
            if expires and not after:
                now[0] = 51.0
            check_deadline()
            found = fetch(codes, **kwargs)
            if expires and after:
                now[0] = 51.0
            return found

        self.enterContext(patch("nci_sip_mcp.bounds.monotonic", side_effect=lambda: now[0]))
        self.enterContext(patch.object(self.evs, "get_concepts_by_codes", side_effect=guarded))
        return batches

    def children(self, count):
        targets = [{"code": f"C{i}", "name": f"Child {i}"} for i in range(1, count + 1)]
        self.evs.concepts = {
            item["code"]: concept(item["code"], item["name"], active=False) for item in targets
        }
        self.evs.concepts["C3262"] = concept("C3262", active=True, children=targets)

    def test_deadline_graph_projects_successful_batches_without_retrying_missing_nodes(self):
        """Given verified batches, when the next expires, return only their nodes and edges."""
        self.children(101)
        batches = self.install_deadline(3)
        failed, result = self.call(
            "get_concept_neighborhood", code="C3262", kinds=["child"], depth=2
        )
        self.assertFalse(failed, result)
        self.assertEqual(
            [node["code"] for node in result["nodes"]], ["C3262", *[f"C{i}" for i in range(1, 51)]]
        )
        self.assertEqual([node["active"] for node in result["nodes"]], [True, *([False] * 50)])
        self.assertEqual(
            [(edge["sourceCode"], edge["targetCode"]) for edge in result["edges"]],
            [(f"C{i}", "C3262") for i in range(1, 51)],
        )
        self.assertEqual(result["truncation"], self.deadline_cut())
        self.assertEqual(len(batches), 3)

    @staticmethod
    def deadline_cut():
        return {
            "occurred": True,
            "bound": "deadline",
            "limit": 50000,
            "reached": 51000,
            "omitted": 0,
            "exact": False,
        }

    def test_expiry_during_hydration_preserves_earlier_batches_and_kind_bounds(self):
        """Given unexpanded inverse targets, expiry during hydration retains verified batches."""
        self.children(101)
        self.evs.concepts["C3262"]["inverseRoles"] = [
            {"type": "Related", "relatedCode": f"C{i}", "relatedName": f"Child {i}"}
            for i in range(1, 102)
        ]
        batches = self.install_deadline(2, after=True)
        failed, result = self.call(
            "get_concept_neighborhood",
            code="C3262",
            kinds=["inverseRole", "inverseAssociation"],
            depth=1,
        )
        self.assertFalse(failed, result)
        self.assertEqual(
            [node["code"] for node in result["nodes"]], ["C3262", *[f"C{i}" for i in range(1, 51)]]
        )
        self.assertEqual(len(result["edges"]), 50)
        prior = {
            "occurred": True,
            "bound": "depth",
            "limit": 1,
            "reached": 1,
            "omitted": 0,
            "exact": False,
        }
        self.assertEqual(
            result["truncation"],
            prior | {"perKind": {"inverseRole": prior, "inverseAssociation": prior}},
        )
        self.assertEqual(len(batches), 3)

    def test_hydration_deadline_marks_only_kinds_with_unverified_targets(self):
        """Given unexpanded negative targets, expiry reports a deadline, not invented status."""
        self.children(101)
        seed = self.evs.concepts["C3262"]
        seed["children"] = []
        seed["roles"] = [
            {
                "code": "R135",
                "type": "Excluded",
                "relatedCode": f"C{i}",
                "relatedName": f"Child {i}",
            }
            for i in range(1, 102)
        ]
        self.install_deadline(2, after=True)
        failed, result = self.call(
            "get_concept_neighborhood", code="C3262", kinds=["role", "child"], depth=1
        )
        self.assertFalse(failed, result)
        self.assertEqual(
            [node["code"] for node in result["nodes"]], ["C3262", *[f"C{i}" for i in range(1, 51)]]
        )
        self.assertEqual(len(result["edges"]), 50)
        cut = self.deadline_cut()
        self.assertEqual(
            result["truncation"], cut | {"perKind": {"role": cut, "child": {"occurred": False}}}
        )

    def test_valid_hierarchy_cursor_expiring_before_offset_returns_empty_attributed_page(self):
        """Given a real continuation, replay expiry before its offset is partial, not invalid."""
        self.children(3)
        arguments = {"code": "C3262", "direction": "child", "depth": 2, "limit": 1}
        failed, first = self.call("get_concept_hierarchy", **arguments)
        self.assertFalse(failed, first)
        self.assertEqual([node["code"] for node in first["nodes"]], ["C1"])
        cursor = first["nextCursor"]
        batches = self.install_deadline(2)
        failed, result = self.call("get_concept_hierarchy", **arguments, cursor=cursor)
        self.assertFalse(failed, result)
        self.assertEqual(result["nodes"], [])
        self.assertEqual(result["truncation"], self.deadline_cut())
        self.assertEqual(result["provenance"]["release"]["identifier"], "26.06e")
        self.assertEqual(result["provenance"]["depth"], 0)
        self.assertNotIn("nextCursor", result)
        self.assertEqual(len(batches), 2)
