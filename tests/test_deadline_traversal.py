"""Deadline truncation preserves verified graphs through the real MCP boundary."""

from unittest.mock import patch

from fakes import concept
from nci_sip_mcp.bounds import check_deadline
from test_server import ServerFixture


class DeadlineTraversalTest(ServerFixture):
    def test_expiry_after_verified_start_preserves_it_and_reports_each_kind(self):
        now = [0.0]
        fetch = self.evs.get_concepts_by_codes

        def spend(*args, **kwargs):
            found = fetch(*args, **kwargs)
            now[0] = 51.0
            return found

        with (
            patch("nci_sip_mcp.bounds.monotonic", side_effect=lambda: now[0]),
            patch.object(self.evs, "get_concepts_by_codes", side_effect=spend),
        ):
            failed, result = self.call(
                "get_concept_neighborhood", code="C3262", kinds=["child", "parent"]
            )
        self.assertFalse(failed)
        self.assertEqual([node["code"] for node in result["nodes"]], ["C3262"])
        self.assertEqual(result["edges"], [])
        expected = {
            "occurred": True,
            "bound": "deadline",
            "limit": 50000,
            "reached": 51000,
            "omitted": 0,
            "exact": False,
        }
        self.assertEqual(
            result["truncation"],
            expected | {"perKind": dict.fromkeys(["child", "parent"], expected)},
        )

    def test_expiry_before_a_verified_start_is_an_error_not_an_empty_graph(self):
        now = [0.0]

        def spend(*args, **kwargs):
            now[0] = 51.0
            check_deadline()

        with (
            patch("nci_sip_mcp.bounds.monotonic", side_effect=lambda: now[0]),
            patch.object(self.evs, "get_concepts_by_codes", side_effect=spend),
        ):
            failed, result = self.call("get_concept_neighborhood", code="C3262", kinds=["child"])
        self.assertTrue(failed)
        self.assertEqual(result["error"]["code"], "upstream_unavailable")
        self.assertNotIn("nodes", result)

    def test_hierarchy_deadline_preserves_available_nodes_without_a_cursor(self):
        self.evs.concepts["C3262"] = concept(
            "C3262",
            active=True,
            children=[{"code": "C1", "name": "One"}, {"code": "C2", "name": "Two"}],
        )
        self.evs.concepts.update(
            {"C1": concept("C1", active=True), "C2": concept("C2", active=True)}
        )
        now = [0.0]
        fetch = self.evs.get_concepts_by_codes
        calls = []
        expiry_batch = 2

        def spend(codes, **kwargs):
            calls.append(codes)
            if len(calls) == expiry_batch:
                now[0] = 51.0
            return fetch(codes, **kwargs)

        with (
            patch("nci_sip_mcp.bounds.monotonic", side_effect=lambda: now[0]),
            patch.object(self.evs, "get_concepts_by_codes", side_effect=spend),
        ):
            failed, result = self.call(
                "get_concept_hierarchy", code="C3262", direction="child", depth=2, limit=1
            )
        self.assertFalse(failed, result)
        self.assertEqual(len(result["nodes"]), 1)
        self.assertEqual(result["truncation"]["bound"], "deadline")
        self.assertNotIn("nextCursor", result)
