"""The workflow tools: ground_value, expand_cohort and harmonize_data_dictionary. Each answer
is read from the recordings of the surfaces it rests on."""

import json
from pathlib import Path

import pytest
import yaml

from nci_sip_acceptance.results import (
    UNPINNED_REGISTRY,
    bare_code,
    element_ids,
    error_code,
    sparql_rows,
    value_ids,
)
from nci_sip_acceptance.spec import TOOLS, defaults

GROUND = "ground_value"
COHORT = "expand_cohort"
HARMONIZE = "harmonize_data_dictionary"
HIERARCHY = "get_concept_hierarchy"
NEIGHBORHOOD = "get_concept_neighborhood"
SPARQL = "recorded/ssis-sparql"
# Ewing Sarcoma: 36 data elements and 29 permissible values in the caDSR graph; Disease or
# Disorder: 1,001 data element rows, more than a hop holds, and 461 values (the recordings).
EWING, DISEASE = "C4817", "C2991"
# What a hop holds at most: find_data_elements_for_concept's maximum (ground_value's summary).
HOP_MAXIMUM = TOOLS["find_data_elements_for_concept"]["bounds"]["limit"]["maximum"]
CALLS = yaml.safe_load((Path(__file__).parent / "calls.yaml").read_text(encoding="utf-8"))


def _ok(result):
    assert not result.is_error, result.content
    return result.content


def _rows(recorded, name):
    return sparql_rows(recorded(f"{SPARQL}/{name}.json"))


def _ground(tools, pinned, **arguments):
    return tools.call(GROUND, {"conceptCode": EWING, "release": pinned["release"]} | arguments)


@pytest.mark.tool(GROUND)
@pytest.mark.requirement("ground_value-2")
@pytest.mark.scenario("ssis/merged-versions")
def test_grounding_merged_graph_versions_makes_only_public_id_associations(tools, pinned):
    content = _ok(_ground(tools, pinned))
    assert content["concept"]["code"] == EWING
    assert [use["dataElement"] for use in content["dataElements"]] == [
        {"publicId": "2008092", "longName": "Merged CDE"}
    ]
    assert [use["dataElement"] for use in content["permissibleValues"]] == [{"publicId": "2008092"}]
    assert value_ids(content["permissibleValues"]) == {("2008092", "Tis", EWING)}


@pytest.mark.tool(GROUND)
@pytest.mark.requirement("ground_value-1")
@pytest.mark.live_capable
def test_one_envelope_names_the_release_asked_for_and_the_registry_unpinned(tools, pinned):
    content = _ok(_ground(tools, pinned))

    provenance = content["provenance"]
    release = provenance["release"]
    assert (release["terminology"], release["identifier"]) == (
        pinned["terminology"],
        pinned["release"],
    )
    assert provenance["registry"] == UNPINNED_REGISTRY
    # Every item that rests on caDSR content names the registry too, not only the envelope.
    resting = [*content["dataElements"], *content["permissibleValues"]]
    registries = [item["provenance"].get("registry") for item in resting]
    assert registries
    assert registries == [UNPINNED_REGISTRY] * len(registries)


@pytest.mark.tool(GROUND)
@pytest.mark.requirement("ground_value-1")
@pytest.mark.scenario("release/concept-behind")
def test_evs_concept_of_another_ncit_release_fails_closed(tools, pinned):
    assert error_code(_ground(tools, pinned)) == "release_mismatch"


@pytest.mark.tool(GROUND)
@pytest.mark.requirement("ground_value-1")
@pytest.mark.parametrize(
    "scenario",
    [
        pytest.param(name, id=name, marks=pytest.mark.scenario(f"release/{name}"))
        for name in ("graph-behind", "graph-version-missing")
    ],
)
def test_exact_grounding_accepts_graph_lag_or_absent_version(tools, pinned, recorded, scenario):
    content = _ok(_ground(tools, pinned))
    assert content["concept"]["code"] == EWING
    assert content["concept"]["provenance"]["sourceUri"].endswith(
        f"/ncit_{pinned['release']}/{EWING}"
    )
    assert element_ids(content["dataElements"]) == {
        row["id"] for row in _rows(recorded, "data-elements-c4817")
    }
    assert value_ids(content["permissibleValues"]) == {
        (row["id"], row["value"], bare_code(row["concept"]))
        for row in _rows(recorded, "values-c4817")
    }
    filename = (
        "ssis-sparql-graph-identities.json"
        if scenario == "graph-behind"
        else "graph-identities.json"
    )
    graphs = sparql_rows(recorded(f"scenarios/release/{scenario}/{filename}"))
    for item in [*content["dataElements"], *content["permissibleValues"]]:
        assert item["provenance"]["upstream"]["graphs"] == graphs


@pytest.mark.tool(GROUND)
@pytest.mark.requirement("ground_value-2")
def test_each_hop_is_what_its_surface_answers_for_the_concept(tools, pinned, recorded):
    elements = {row["id"] for row in _rows(recorded, "data-elements-c4817")}
    values = {
        (row["id"], row["value"], bare_code(row["concept"]))
        for row in _rows(recorded, "values-c4817")
    }
    # The concept has both, or the hops would show nothing.
    assert (bool(elements), bool(values)) == (True, True)

    content = _ok(_ground(tools, pinned))

    assert content["concept"]["code"] == EWING
    assert element_ids(content["dataElements"]) == elements
    assert value_ids(content["permissibleValues"]) == values
    # Without commons the stored value hop does not run, and says so by its absence (A2.6).
    assert "storedValues" not in content


@pytest.mark.tool(GROUND)
@pytest.mark.requirement("ground_value-2")
def test_with_a_commons_the_stored_values_are_the_ones_it_uses_for_the_concept(
    tools, pinned, recorded
):
    maps = recorded("recorded/evs/mapset-gdc-maps-code.json")["response"]["body"]["maps"]
    assert maps

    content = _ok(_ground(tools, pinned, commons="GDC"))

    assert {(value["value"], value["field"]) for value in content["storedValues"]} == {
        (each["targetName"], each["targetCode"]) for each in maps if each["sourceCode"] == EWING
    }


# A search whose first result is not the concept named like the text (an invented order).
@pytest.mark.scenario("search/first-not-named")
@pytest.mark.tool(GROUND)
@pytest.mark.requirement("ground_value-3")
def test_a_text_resolves_to_the_first_result_search_concepts_gives_it(tools, pinned, recorded):
    search = recorded("scenarios/search/first-not-named/search-contains.json")
    text = search["request"]["params"]["term"][0]
    named = [
        each for each in search["response"]["body"]["concepts"] if each["name"].lower() == text
    ]
    # Another concept is named like the text, so the first result is not the best name match.
    assert named
    assert named[0] != search["response"]["body"]["concepts"][0]

    content = _ok(tools.call(GROUND, {"text": text, "release": pinned["release"]}))

    assert content["concept"]["code"] == search["response"]["body"]["concepts"][0]["code"]


@pytest.mark.tool(GROUND)
@pytest.mark.requirement("ground_value-4")
@pytest.mark.scenario("ssis/distinct-cap")
def test_a_hop_that_reaches_its_bound_leaves_the_others_complete(tools, pinned, recorded):
    elements = {
        row["id"]
        for row in sparql_rows(recorded("scenarios/ssis/distinct-cap/data-elements-c2991.json"))
    }
    values = {
        (row["id"], row["value"], bare_code(row["concept"]))
        for row in _rows(recorded, "values-c2991")
    }
    # The data element hop exceeds the maximum and the value hop does not.
    assert (len(elements) > HOP_MAXIMUM, len(values) <= HOP_MAXIMUM) == (True, True)

    content = _ok(_ground(tools, pinned, conceptCode=DISEASE))

    truncation = content["truncation"]
    hop = truncation["perHop"]["dataElements"]
    assert truncation["occurred"] is True
    assert (hop["occurred"], hop["bound"], hop["limit"], hop["reached"], hop["exact"]) == (
        True,
        "results",
        HOP_MAXIMUM,
        HOP_MAXIMUM,
        False,
    )
    assert hop["omitted"] >= 1
    assert element_ids(content["dataElements"]) <= elements
    assert len(content["dataElements"]) == HOP_MAXIMUM
    # The values are asked by the concept, not by the data elements cut: all of them come.
    assert truncation["perHop"].get("permissibleValues", {}).get("occurred", False) is False
    assert value_ids(content["permissibleValues"]) == values


def _cohort_parts(tools, pinned):
    """expand_cohort's composition: the hierarchy below the concept to the default maxDepth,
    and the exclusion assertions of the concept itself."""

    depth = defaults(COHORT)["maxDepth"]
    hierarchy = _ok(
        tools.call(HIERARCHY, pinned | {"code": EWING, "direction": "child", "depth": depth})
    )
    neighborhood = _ok(
        tools.call(NEIGHBORHOOD, pinned | {"code": EWING, "depth": 1, "kinds": ["role"]})
    )
    descendants = {node["code"] for node in hierarchy["nodes"]}
    # One pair per exclusion assertion: a code can be named by more than one (C2869 by R135 and
    # R136 in the scenario).
    exclusions = {
        (edge["targetCode"], edge["provenance"]["relationship"]["code"])
        for edge in neighborhood["edges"]
        if edge["sourceCode"] == EWING and edge["provenance"]["polarity"] == "negative"
    }
    return descendants, exclusions


@pytest.mark.scenario("traversal/exclusions")
@pytest.mark.tool(COHORT)
@pytest.mark.requirement("expand_cohort-1")
@pytest.mark.parametrize("negative", [False, True])
def test_the_cohort_is_the_concept_and_its_descendants_less_its_own_exclusions(
    tools, pinned, negative
):
    # The tool asked about first: a server without it shows NOT IMPLEMENTED, whatever the
    # tools it is compared with return.
    content = _ok(
        tools.call(
            COHORT,
            {"release": pinned["release"], "conceptCode": EWING, "includeNegative": negative},
        )
    )
    descendants, exclusions = _cohort_parts(tools, pinned)
    excluded = {code for code, _ in exclusions}
    # The scenario's concept excludes one of its own descendants, so withholding shows.
    assert descendants & excluded

    members = {EWING} | descendants
    assert set(content["codes"]) == (members if negative else members - excluded)
    assert {
        (each["code"], each["edge"]["provenance"]["relationship"]["code"])
        for each in content["excluded"]
    } == exclusions
    assert len(content["excluded"]) == len(exclusions)


@pytest.mark.tool(COHORT)
@pytest.mark.requirement("expand_cohort-1")
def test_max_nodes_counts_the_codes_the_concept_included(tools, pinned):
    maximum = 2

    content = _ok(
        tools.call(
            COHORT, {"release": pinned["release"], "conceptCode": EWING, "maxNodes": maximum}
        )
    )

    codes = content["codes"]
    truncation = content["truncation"]
    assert (EWING in codes, len(codes)) == (True, maximum)
    assert (truncation["occurred"], truncation["reached"]) == (True, len(codes))


# A column CDE Match matches (2200604 among others), with a sample value vmMatch aligns, and one
# with a description it matches to nothing (both crafted to the contract, cadsr/credentialed).
COLUMNS = CALLS[HARMONIZE]["arguments"]["columns"]


def _asked(upstream):
    """Each entity CDE Match was asked for, with its user tip, one per logged request."""

    matching = [entry for entry in upstream.log() if entry["path"].endswith("/cdeMatch")]
    entities = [_entity(entry) for entry in matching]
    return sorted((entity["entity"], entity.get("entityUserTip")) for entity in entities)


def _entity(entry):
    """The one entity a logged CDE Match request asks for: the contract's object, alone or as
    the one element of an array."""

    body = json.loads(entry["body"])
    (entity,) = body if isinstance(body, list) else [body]
    return entity


@pytest.mark.scenario("cadsr/credentialed")
@pytest.mark.tool(HARMONIZE)
@pytest.mark.requirement("harmonize_data_dictionary-1")
def test_each_column_is_matched_once_and_the_unmatched_are_listed(tools, upstream, recorded):
    unmatched = recorded("scenarios/cadsr/credentialed/cde-match-unmatched.json")
    assert unmatched["response"]["body"]["matchResults"]["matches"] == []

    content = _ok(tools.call(HARMONIZE, {"columns": COLUMNS}))

    # Each column is asked for once, in a request of its own, its description as the user tip.
    assert _asked(upstream) == sorted(
        (column["name"], column.get("description")) for column in COLUMNS
    )
    assert content["unmatched"] == ["Freezer Shelf Label"]
    matched = {column["name"]: column for column in content["columns"]}["Patient Gender"]
    registries = [match["dataElement"]["provenance"]["release"] for match in matched["matches"]]
    assert registries
    assert registries == [UNPINNED_REGISTRY] * len(registries)


@pytest.mark.scenario("cadsr/credentialed")
@pytest.mark.tool(HARMONIZE)
@pytest.mark.requirement("harmonize_data_dictionary-1")
def test_a_column_s_sample_values_align_to_the_value_meanings_vmmatch_gives(tools, recorded):
    (result,) = recorded("recorded/cadsr/vm-match-male.json")["response"]["body"]["matchResults"]
    expected = {(match["itemId"], match["concept"]) for match in result["matches"]}

    content = _ok(tools.call(HARMONIZE, {"columns": COLUMNS}))

    matched = {column["name"]: column for column in content["columns"]}["Patient Gender"]
    aligned = matched["permissibleValueAlignment"]
    assert {(each["item"]["publicId"], each["item"].get("concept")) for each in aligned} == expected
