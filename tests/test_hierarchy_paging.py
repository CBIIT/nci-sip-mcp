from unittest.mock import patch

from fakes import concept, release
from nci_sip_mcp import cursor as cursors
from nci_sip_mcp.bounds import Budget, current_budget
from nci_sip_mcp.evs import EVSReleaseNotFoundError
from nci_sip_mcp.registry import invoke
from test_search_paging import reposition
from test_server import ServerFixture, pinned
from test_traversal import HubEVS


class HierarchyPagingTest(ServerFixture):
    def setUp(self):
        super().setUp()
        self.evs.concepts = {
            "C1": concept("C1", active=True, children=[{"code": f"C{i}"} for i in range(2, 6)]),
            **{f"C{i}": concept(f"C{i}", active=True) for i in range(2, 6)},
        }

    def page(self, **arguments):
        return invoke(
            self.context,
            "get_concept_hierarchy",
            **(
                {
                    "terminology": "ncit",
                    "release": "26.06e",
                    "code": "C1",
                    "direction": "child",
                    "limit": 2,
                }
                | arguments
            ),
        )

    def test_historical_pages_continue_without_discovery_and_end_without_cursor(self):
        self.evs.release = release("new")
        first = self.page()
        second = self.page(cursor=first["nextCursor"])
        self.assertEqual(
            [[node["code"] for node in page["nodes"]] for page in (first, second)],
            [["C2", "C3"], ["C4", "C5"]],
        )
        self.assertNotIn("nextCursor", second)
        self.assertEqual(first["truncation"], {"occurred": False})
        self.assertNotIn("get_terminologies", [call[0] for call in self.evs.calls])

    def test_withdrawn_cursor_resolves_current_only_after_pinned_failure(self):
        first = self.page()
        self.evs.release = release("new")
        self.evs.errors["get_concepts_by_codes"] = EVSReleaseNotFoundError("withdrawn")
        second = self.page(cursor=first["nextCursor"])
        self.assertEqual(second["error"]["code"], "cursor_expired")
        self.assertEqual(
            second["error"]["details"], {"cursorRelease": "26.06e", "currentRelease": "new"}
        )
        self.assertEqual(self.evs.calls[-1], ("get_terminologies", "ncit", (True, "monthly")))
        self.assertEqual(self.page()["error"]["code"], "release_not_available")

    def test_cursor_rejects_changed_applied_arguments_before_network(self):
        first = self.page()
        for changed in (
            {"limit": 3},
            {"depth": 2},
            {"code": "C2"},
            {"direction": "parent"},
            {"release": "new"},
        ):
            with self.subTest(changed=changed):
                self.evs.calls.clear()
                result = self.page(cursor=first["nextCursor"], **changed)
                self.assertEqual(result["error"]["code"], "invalid_request")
                self.assertEqual(self.evs.calls, [])

    def test_malformed_cursors_are_invalid_requests(self):
        for cursor in ("", "broken", "e30=", "W10=", "x" * 9000):
            with self.subTest(cursor=cursor[:20]):
                self.assertEqual(self.page(cursor=cursor)["error"]["code"], "invalid_request")
        self.assertEqual(self.evs.calls, [])

    def test_invalid_positions_cannot_be_used_to_claim_an_empty_last_page(self):
        arguments = {
            "terminology": "ncit",
            "release": "26.06e",
            "code": "C1",
            "direction": "child",
            "depth": 1,
            "limit": 2,
        }
        for offset in (-1, 0, True, 100):
            with self.subTest(offset=offset):
                result = self.page(cursor=cursors.encode(arguments, offset))
                self.assertEqual(result["error"]["code"], "invalid_request")
                self.assertNotIn("nodes", result)

    def test_defaults_and_clamped_depths_continue_as_the_same_arguments(self):
        first = self.page()
        second = self.page(depth=1, cursor=first["nextCursor"])
        self.assertEqual([node["code"] for node in second["nodes"]], ["C4", "C5"])
        first = self.page(depth=99)
        second = self.page(depth=4, cursor=first["nextCursor"])
        self.assertEqual([node["code"] for node in second["nodes"]], ["C4", "C5"])

    def test_partial_replay_exhaustion_does_not_return_a_short_page(self):
        first = self.page()
        fetch = self.evs.get_concepts_by_codes

        def counted(*args, **kwargs):
            current_budget().request()
            return fetch(*args, **kwargs)

        with (
            patch("nci_sip_mcp.content.Budget", return_value=Budget(requests=1, paged=True)),
            patch.object(self.evs, "get_concepts_by_codes", side_effect=counted),
        ):
            result = self.page(cursor=first["nextCursor"])
        self.assertEqual(result["error"]["code"], "bound_exceeded")
        self.assertNotIn("nodes", result)

    def test_other_terminology_codes_survive_cursor_and_provenance_encoding(self):
        children = ["B/C?", "name #2"]
        self.evs.concepts = {
            code: concept(code, active=True, terminology="other") for code in ["A/B", *children]
        }
        self.evs.concepts["A/B"]["children"] = [{"code": code} for code in children]
        first = self.page(terminology="other", code="A/B", limit=1)
        second = self.page(terminology="other", code="A/B", limit=1, cursor=first["nextCursor"])
        self.assertEqual(
            [node["code"] for page in (first, second) for node in page["nodes"]], children
        )
        self.assertTrue(first["nodes"][0]["provenance"]["sourceUri"].endswith("/B%2FC%3F"))

    def test_replay_exhaustion_is_not_a_last_page(self):
        first = self.page()
        fetch = self.evs.get_concepts_by_codes

        def exhausted(*args, **kwargs):
            budget = current_budget()
            budget.attempts = budget.requests
            budget.request()
            return fetch(*args, **kwargs)

        with patch.object(self.evs, "get_concepts_by_codes", side_effect=exhausted):
            result = self.page(cursor=first["nextCursor"])
        self.assertEqual(result["error"]["code"], "bound_exceeded")
        self.assertEqual(result["error"]["details"]["bound"], "requests")
        self.assertIn("narrow", result["error"]["message"].lower())

    def test_more_than_one_thousand_nodes_can_be_paged(self):
        children = [concept(f"C{i}", active=True) for i in range(2, 1003)]
        self.evs.concepts.update({item["code"]: item for item in children})
        self.evs.concepts["C1"]["children"] = children
        first = self.page(limit=1000)
        second = self.page(limit=1000, cursor=first["nextCursor"])
        self.assertEqual([len(page["nodes"]) for page in (first, second)], [1000, 1])
        self.assertEqual(second["nodes"][0]["code"], "C1002")
        self.assertNotIn("nextCursor", second)

    def test_page_window_does_not_hide_a_later_upstream_cap(self):
        self.context.evs = HubEVS(list(self.evs.concepts.values()))
        self.context.evs.hubs = frozenset({"C2"})
        result = self.page(limit=1, depth=2)
        self.assertEqual([node["code"] for node in result["nodes"]], ["C2"])
        self.assertIn("nextCursor", result)
        self.assertEqual(result["truncation"]["bound"], "upstream_cap")
        self.assertEqual(result["truncation"]["omitted"], 1)

    def test_shared_descendants_and_cycles_are_not_repeated_across_pages(self):
        self.evs.concepts["C2"]["children"] = [{"code": "C5"}, {"code": "C1"}]
        self.evs.concepts["C3"]["children"] = [{"code": "C5"}]
        first = self.page(depth=2)
        second = self.page(depth=2, cursor=first["nextCursor"])
        self.assertEqual(
            [node["code"] for page in (first, second) for node in page["nodes"]],
            ["C2", "C3", "C4", "C5"],
        )
        self.assertNotIn("nextCursor", second)

    def test_hierarchy_clamps_page_limit_for_both_directions(self):
        for direction, relation in (("child", "children"), ("parent", "parents")):
            with self.subTest(direction=direction):
                children = [f"C{i}" for i in range(2, 1003)]
                self.evs.concepts = {code: concept(code, active=True) for code in children}
                self.evs.concepts["C1"] = concept(
                    "C1", active=True, **{relation: [{"code": code} for code in children]}
                )
                result = invoke(
                    self.context,
                    "get_concept_hierarchy",
                    terminology="ncit",
                    code="C1",
                    direction=direction,
                    limit=1001,
                )
                self.assertEqual(len(result["nodes"]), 1000)
                self.assertIn("nextCursor", result)

    def test_hierarchy_clamps_depth_for_both_directions(self):
        for direction, relation in (("child", "children"), ("parent", "parents")):
            with self.subTest(direction=direction):
                self.evs.concepts = {
                    f"C{i}": concept(f"C{i}", active=True, **{relation: [{"code": f"C{i + 1}"}]})
                    for i in range(1, 7)
                }
                result = invoke(
                    self.context,
                    "get_concept_hierarchy",
                    terminology="ncit",
                    code="C1",
                    direction=direction,
                    depth=5,
                )
                self.assertEqual(
                    {node["code"] for node in result["nodes"]}, {"C2", "C3", "C4", "C5"}
                )
                self.assertEqual(result["truncation"]["bound"], "depth")
                self.assertEqual(result["truncation"]["limit"], 4)

    def test_cursor_binds_the_applied_depth_for_both_directions(self):
        for direction, relation in (("child", "children"), ("parent", "parents")):
            with self.subTest(direction=direction):
                self.evs.concepts = {
                    "C1": concept(
                        "C1", active=True, **{relation: [{"code": "C2"}, {"code": "C3"}]}
                    ),
                    "C2": concept("C2", active=True),
                    "C3": concept("C3", active=True),
                }
                arguments = {
                    "terminology": "ncit",
                    "code": "C1",
                    "direction": direction,
                    "limit": 1,
                }
                first = invoke(self.context, "get_concept_hierarchy", **arguments, depth=5)
                continued = invoke(
                    self.context,
                    "get_concept_hierarchy",
                    **arguments,
                    depth=4,
                    cursor=first["nextCursor"],
                )
                self.assertNotIn("error", continued)
                self.assertEqual([node["code"] for node in continued["nodes"]], ["C3"])


class HierarchyContinuationTest(ServerFixture):
    def setUp(self):
        super().setUp()
        self.evs.concepts = {
            "C1": concept("C1", "One", active=True, children=[{"code": "C2"}]),
            "C2": concept("C2", "Two", active=True),
        }

    def call(self, tool, **arguments):
        return invoke(
            self.context,
            tool,
            **({"terminology": "ncit", "release": "26.06e"} | arguments),
        )

    def hierarchy(self, **arguments):
        return self.call(
            "get_concept_hierarchy",
            **({"code": "C1", "direction": "child", "limit": 1} | arguments),
        )

    def test_hierarchy_cursor_binds_terminology(self):
        self.evs.concepts["C1"]["children"].append({"code": "C3"})
        self.evs.concepts["C3"] = concept("C3", active=True)
        first = self.hierarchy()
        for raw in self.evs.concepts.values():
            raw["terminology"] = "other"
        result = self.hierarchy(cursor=first["nextCursor"], terminology="other")
        self.assertEqual(result["error"]["code"], "invalid_request")
        self.assertEqual(result["error"]["details"]["parameter"], "cursor")

    def test_hierarchy_cursor_at_total_is_invalid(self):
        self.evs.concepts["C1"]["children"].append({"code": "C3"})
        self.evs.concepts["C3"] = concept("C3", active=True)
        first = self.hierarchy()
        result = self.hierarchy(cursor=reposition(first["nextCursor"], 2))
        self.assertEqual(result["error"]["code"], "invalid_request")
        self.assertNotIn("nodes", result)


class HierarchyDefaultsTest(ServerFixture):
    def content(self, operation, **arguments):
        return invoke(self.context, operation, **pinned(**arguments))

    def children(self, count):
        targets = [{"code": f"C{i}", "name": str(i)} for i in range(2, count + 2)]
        self.evs.concepts = {item["code"]: concept(item["code"], active=True) for item in targets}
        self.evs.concepts["C1"] = concept("C1", active=True, children=targets)

    def test_hierarchy_default_depth_is_one_and_large_depth_clamps_to_four(self):
        self.evs.concepts = {
            f"C{i}": concept(f"C{i}", active=True, children=[{"code": f"C{i + 1}"}])
            for i in range(1, 7)
        }
        default = self.content("get_concept_hierarchy", code="C1", direction="child")
        self.assertEqual([node["code"] for node in default["nodes"]], ["C2"])
        result = self.content("get_concept_hierarchy", code="C1", direction="child", depth=9)
        self.assertEqual([node["code"] for node in result["nodes"]], ["C2", "C3", "C4", "C5"])
        self.assertEqual(result["truncation"]["limit"], 4)
        self.assertEqual(result["truncation"]["bound"], "depth")

    def test_hierarchy_default_limit_allows_one_hundred_fifty_children(self):
        self.children(150)
        result = self.content("get_concept_hierarchy", code="C1", direction="child")
        self.assertIn("nodes", result)
        self.assertEqual(len(result["nodes"]), 150)
        self.assertEqual(result["truncation"], {"occurred": False})
