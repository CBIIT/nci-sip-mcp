"""Workflow acceptance calls and expected values obey their own public contracts."""

import json
import runpy
from pathlib import Path
from types import SimpleNamespace

import pytest

from nci_sip_acceptance.record import FIXTURES
from nci_sip_acceptance.spec import parameters

CHECKS = runpy.run_path(str(Path(__file__).parents[1] / "tests/test_workflow.py"))
PINNED = {"terminology": "ncit", "release": "26.09d"}


class WorkflowAnswers:
    def __init__(self):
        self.undeclared = set()

    def call(self, tool, arguments):
        allowed, _ = parameters(tool)
        self.undeclared.update(set(arguments) - allowed)
        edge = {
            "sourceCode": "C4817",
            "targetCode": "C3",
            "provenance": {"polarity": "negative", "relationship": {"code": "R135"}},
        }
        results = {
            "get_concept_hierarchy": {"nodes": [{"code": "C2"}, {"code": "C3"}]},
            "get_concept_neighborhood": {"edges": [edge]},
            "expand_cohort": {
                "codes": ["C4817", "C2", "C3"]
                if arguments.get("includeNegative")
                else ["C4817", "C2"],
                "excluded": [{"code": "C3", "edge": edge}],
                "truncation": {"occurred": True, "reached": 2},
            },
        }
        return SimpleNamespace(is_error=False, content=results[tool])


@pytest.mark.parametrize("negative", [False, True])
def test_cohort_comparison_uses_only_declared_arguments(negative):
    tools = WorkflowAnswers()
    CHECKS["test_the_cohort_is_the_concept_and_its_descendants_less_its_own_exclusions"](
        tools, PINNED, negative
    )
    assert tools.undeclared == set()


def test_cohort_node_cut_uses_only_declared_arguments():
    tools = WorkflowAnswers()
    CHECKS["test_max_nodes_counts_the_codes_the_concept_included"](tools, PINNED)
    assert tools.undeclared == set()


def recording(name):
    return json.loads((FIXTURES / name).read_text())


class StoredAnswer:
    def __init__(self, substring):
        self.substring = substring

    def call(self, tool, arguments):
        maps = recording("recorded/evs/mapset-gdc-maps-code.json")["response"]["body"]["maps"]
        values = [
            {"value": row["targetName"], "field": row["targetCode"]}
            for row in maps
            if self.substring or row["sourceCode"] == arguments["conceptCode"]
        ]
        return SimpleNamespace(is_error=False, content={"storedValues": values})


def test_stored_grounding_accepts_exact_matches_and_rejects_substring_matches():
    check = CHECKS["test_with_a_commons_the_stored_values_are_the_ones_it_uses_for_the_concept"]
    check(StoredAnswer(False), PINNED, recording)
    with pytest.raises(AssertionError):
        check(StoredAnswer(True), PINNED, recording)


def test_default_cohort_frontiers_have_recorded_child_lists():
    frontier, seen = {"C4817"}, set()
    for _ in range(3):
        frontier = _children(frontier - seen, seen)
    assert "C6623" in seen


def _children(frontier, seen):
    following = set()
    for code in frontier:
        document = recording(f"recorded/evs/concepts/{code}.json")
        includes = document["request"]["params"]["include"][0].split(",")
        assert {"children", "full"} & set(includes), code
        following.update(row["code"] for row in document["response"]["body"].get("children", []))
    seen.update(frontier)
    return following
