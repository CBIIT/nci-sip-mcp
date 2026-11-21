from unittest.mock import patch

from jsonschema import Draft202012Validator

from fakes import concept
from nci_sip_mcp.bounds import Budget, current_budget
from nci_sip_mcp.registry import invoke
from test_server import ServerFixture


def assertion(target, code="R1", name="Same name", **fields):
    return {"relatedCode": target, "code": code, "type": name, **fields}


class NegativeExpansionTest(ServerFixture):
    def setUp(self):
        super().setUp()
        self.evs.concepts = {
            "C1": concept("C1", active=True, roles=[assertion("C2", "R135")]),
            "C2": concept("C2", active=True, roles=[assertion("C3")]),
            "C3": concept("C3", active=True),
            "C4": concept("C4", active=True, roles=[assertion("C2")]),
        }

    def walk(self, **arguments):
        return invoke(
            self.context,
            "get_concept_neighborhood",
            **(
                {
                    "terminology": "ncit",
                    "release": "26.06e",
                    "code": "C1",
                    "kinds": ["role"],
                    "depth": 3,
                }
                | arguments
            ),
        )

    def test_negative_target_is_visible_but_only_hydrated_until_opted_in(self):
        stopped = self.walk()
        self.assertEqual([node["code"] for node in stopped["nodes"]], ["C1", "C2"])
        self.assertEqual(stopped["truncation"], {"occurred": False})
        self.assertEqual(self.evs.includes, ["minimal,roles", "minimal"])
        expanded = self.walk(includeNegative=True)
        self.assertEqual([node["code"] for node in expanded["nodes"]], ["C1", "C2", "C3"])
        self.assertEqual(expanded["edges"][0]["provenance"]["polarity"], "negative")

    def test_same_named_positive_and_negative_assertions_both_survive_in_any_order(self):
        for roles in (
            [assertion("C2", "R135"), assertion("C2")],
            [assertion("C2"), assertion("C2", "R135")],
        ):
            with self.subTest(roles=roles):
                self.evs.concepts["C1"]["roles"] = roles
                result = self.walk()
                self.assertEqual({node["code"] for node in result["nodes"]}, {"C1", "C2", "C3"})
                self.assertEqual(
                    {edge["provenance"]["polarity"] for edge in result["edges"]},
                    {"positive", "negative"},
                )
                self.assertEqual(len(result["edges"]), 3)

    def test_later_positive_path_expands_a_previously_negative_only_target(self):
        self.evs.concepts["C1"]["roles"].append(assertion("C4"))
        shallow = self.walk(depth=2)
        deeper = self.walk(depth=3)
        self.assertEqual({node["code"] for node in shallow["nodes"]}, {"C1", "C2", "C4"})
        self.assertEqual(shallow["truncation"]["bound"], "depth")
        self.assertEqual({node["code"] for node in deeper["nodes"]}, {"C1", "C2", "C3", "C4"})
        self.assertEqual(deeper["truncation"], {"occurred": False})

    def test_positive_edge_readmitted_after_kind_budget_reopens_negative_target(self):
        self.evs.concepts["C1"]["roles"] = [assertion("C4"), assertion("C2")]
        self.evs.concepts["C1"]["associations"] = [assertion("C4", "R135"), assertion("C2", "R135")]
        self.evs.concepts["C4"]["roles"] = []
        self.evs.concepts["C2"]["roles"] = [assertion("C1")]
        result = self.walk(kinds=["role", "association"], budgetPerKind=1)
        self.assertIn(
            ("C2", "C1"), [(edge["sourceCode"], edge["targetCode"]) for edge in result["edges"]]
        )
        self.assertEqual(
            {
                edge["provenance"]["relationship"]["code"]
                for edge in result["edges"]
                if edge["targetCode"] == "C2"
            },
            {"R1", "R135"},
        )
        self.assertEqual(result["truncation"], {"occurred": False})

    def test_missing_code_keeps_upstream_qualifiers_and_positive_polarity_in_schema(self):
        raw = assertion(
            "C2",
            evidence={"source": "upstream"},
            qualifiers=[{"type": "computed", "value": "upstream"}],
        )
        del raw["code"]
        self.evs.concepts["C1"]["roles"] = [raw]
        result = self.walk()
        provenance = result["edges"][0]["provenance"]
        self.assertEqual(provenance["relationship"], {"kind": "role", "name": "Same name"})
        self.assertEqual(provenance["polarity"], "positive")
        self.assertEqual(provenance["qualifiers"], raw["qualifiers"])
        self.assertEqual(provenance["evidence"], raw["evidence"])
        tool = next(
            tool
            for tool in self.session(lambda client: client.list_tools()).tools
            if tool.name == "get_concept_neighborhood"
        )
        Draft202012Validator(tool.output_schema).validate(result)

    def test_hydrating_negative_targets_exhaustion_reports_requests(self):
        fetch = self.evs.get_concepts_by_codes

        def counted(*args, **kwargs):
            current_budget().request()
            return fetch(*args, **kwargs)

        with (
            patch("nci_sip_mcp.content.Budget", return_value=Budget(requests=1)),
            patch.object(self.evs, "get_concepts_by_codes", side_effect=counted),
        ):
            result = self.walk()
        self.assertEqual([node["code"] for node in result["nodes"]], ["C1"])
        self.assertEqual(result["edges"], [])
        self.assertEqual(result["truncation"]["bound"], "requests")
        self.assertEqual(result["truncation"]["omitted"], 1)
