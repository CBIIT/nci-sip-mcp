"""The cross-domain tools' own requirements (spec/requirements.yaml), each test against its tool.

A tool answers from any surface that can, and the surfaces answer from
their own content: the Shared SI graph is the caDSR export of 1 July 2026, the caDSR API is
live. So a test reads the surface the result's provenance names and checks the answer against
that surface's recording, never one surface against another. Only descendant expansion
requires the Shared SI NCIt hierarchy version to match the pinned release.

Fixture expectations come from recordings; live discovery checks identities and provenance
against the current NCIt release rather than assuming historical graph content.
"""

from datetime import date, datetime
from itertools import combinations

import pytest

from nci_sip_acceptance.results import (
    EXPORT_LISTING,
    bare_code,
    element_ids,
    error_code,
    export_date,
    is_name,
    release_of,
    sparql_rows,
    value_ids,
)
from nci_sip_acceptance.spec import TOOLS, defaults

FIND = "find_data_elements_for_concept"
PERMISSIBLE = "get_concept_for_permissible_value"
STORED = "resolve_stored_value"
ALIGNMENT = "get_release_alignment"
# Gender: 17 data elements in the caDSR graph, 27 with its descendants (the recordings).
GENDER = "C17357"
SPARQL = "recorded/ssis-sparql"
THESAURUS = "http://ncicb.nci.nih.gov/xml/owl/EVS/Thesaurus.rdf"
CADSR_GRAPH = "http://cbiit.nci.nih.gov/caDSR"
CROSSWALK = "recorded/cadsr/crdc-list.json"


def _ok(result):
    assert not result.is_error, result.content
    return result.content


def _rows(recorded, name):
    return sparql_rows(recorded(f"{SPARQL}/{name}.json"))


def _sources(items):
    return {(item.get("provenance") or {}).get("source") for item in items}


def _find(tools, pinned, **arguments):
    return tools.call(FIND, {"conceptCode": GENDER} | pinned | arguments)


@pytest.mark.tool(FIND)
@pytest.mark.requirement("find_data_elements_for_concept-1")
@pytest.mark.scenario("ssis/merged-versions")
def test_merged_graph_versions_make_only_public_id_associations(tools, pinned):
    content = _ok(_find(tools, pinned, conceptCode="C4817", includePermissibleValues=True))
    assert [use["dataElement"] for use in content["dataElements"]] == [
        {"publicId": "2008092", "longName": "Merged CDE"}
    ]
    assert [use["dataElement"] for use in content["permissibleValues"]] == [{"publicId": "2008092"}]
    assert value_ids(content["permissibleValues"]) == {("2008092", "Tis", "C4817")}


@pytest.mark.tool(FIND)
@pytest.mark.requirement("find_data_elements_for_concept-1")
@pytest.mark.live_capable
@pytest.mark.parametrize(
    ("expand", "recording"),
    [(False, "data-elements-c17357"), (True, "data-elements-c17357-descendants")],
)
def test_the_data_elements_are_the_concept_s_and_with_expansion_its_descendants_too(
    tools, content_pin, target, recorded, expand, recording
):
    content = _ok(_find(tools, content_pin, expandDescendants=expand))

    # Shared SI answers both exact-code and descendant-expanded queries.
    if target.mode == "fixture":
        assert _sources(content["dataElements"]) == {"ssis_sparql"}
        expected = {row["id"] for row in _rows(recorded, recording)}
        assert element_ids(content["dataElements"]) == expected
    else:
        assert isinstance(content["dataElements"], list)
        assert _sources(content["dataElements"]) <= {"ssis_sparql"}
        for item in content["dataElements"]:
            assert all(
                is_name(item.get("dataElement", {}).get(key)) for key in ("publicId", "longName")
            )
            assert release_of(item) == ("ncit", content_pin["release"])
    assert "permissibleValues" not in content


@pytest.mark.tool(FIND)
@pytest.mark.requirement("find_data_elements_for_concept-2")
@pytest.mark.parametrize(
    ("expand", "recording"), [(False, "values-c17357"), (True, "values-c17357-descendants")]
)
def test_with_the_flag_the_permissible_values_that_stand_for_the_concept_come_too(
    tools, pinned, recorded, expand, recording
):
    expected = {
        (row["id"], row["value"], bare_code(row["concept"])) for row in _rows(recorded, recording)
    }

    content = _ok(_find(tools, pinned, expandDescendants=expand, includePermissibleValues=True))

    assert _sources(content["permissibleValues"]) == {"ssis_sparql"}
    assert value_ids(content["permissibleValues"]) == expected


def _iso(text):
    """A graph's date as ISO-8601: the NCIt graphs give it as "September 28, 2026", the caDSR
    graph as "2026-07-01" (graph-identities.json)."""

    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        return datetime.strptime(text, "%B %d, %Y").date().isoformat()


@pytest.mark.tool(FIND)
@pytest.mark.requirement("find_data_elements_for_concept-3")
def test_an_answer_of_the_shared_si_service_names_both_graphs_and_both_content_states(
    tools, pinned, recorded
):
    graphs = {row["graph"]: row for row in _rows(recorded, "graph-identities")}
    expected = [
        {"graph": THESAURUS, "version": graphs[THESAURUS]["version"]}
        | {"date": _iso(graphs[THESAURUS]["date"])},
        {"graph": CADSR_GRAPH, "date": _iso(graphs[CADSR_GRAPH]["date"])},
    ]

    content = _ok(_find(tools, pinned))

    provenances = [use["provenance"] for use in content["dataElements"]]
    assert {
        (each["release"]["terminology"], each["release"]["identifier"]) for each in provenances
    } == {(pinned["terminology"], pinned["release"])}
    # caDSR publishes no registry release: the registry state names neither (A3.8.1).
    assert [each.get("registry") for each in provenances] == [{"registry": "cadsr"}] * len(
        provenances
    )
    by_graph = {"key": lambda graph: graph["graph"]}
    assert all(
        sorted(each["graphs"], **by_graph) == sorted(expected, **by_graph) for each in provenances
    )


@pytest.mark.tool(FIND)
@pytest.mark.requirement("find_data_elements_for_concept-4")
def test_expansion_with_another_ncit_graph_release_fails_closed(tools, pinned, recorded):
    graphs = {row["graph"]: row for row in _rows(recorded, "graph-identities")}
    listing = recorded("recorded/evs/terminologies.json")["response"]["body"]
    # Another monthly release EVS serves, which the graph is not (terminologies.json).
    other = next(
        row["version"]
        for row in listing
        if row["terminology"] == pinned["terminology"]
        and row["version"] != graphs[THESAURUS]["version"]
    )

    result = _find(tools, pinned | {"release": other}, expandDescendants=True)

    assert error_code(result) == "release_mismatch", result.content


@pytest.mark.tool(FIND)
@pytest.mark.requirement("find_data_elements_for_concept-4")
@pytest.mark.parametrize(
    "scenario",
    [
        pytest.param(name, marks=pytest.mark.scenario(f"release/{name}"))
        for name in ("graph-behind", "graph-version-missing")
    ],
)
def test_exact_find_preserves_actual_graph_states_without_hierarchy_alignment(
    tools, pinned, recorded, scenario
):
    content = _ok(_find(tools, pinned))
    assert element_ids(content["dataElements"]) == {
        row["id"] for row in _rows(recorded, "data-elements-c17357")
    }
    filename = (
        "ssis-sparql-graph-identities.json"
        if scenario == "graph-behind"
        else "graph-identities.json"
    )
    graphs = sparql_rows(recorded(f"scenarios/release/{scenario}/{filename}"))
    expected = [row | {"date": _iso(row["date"])} for row in graphs]
    for use in content["dataElements"]:
        assert use["provenance"]["graphs"] == expected
        assert release_of(use) == ("ncit", pinned["release"])


@pytest.mark.tool(FIND)
@pytest.mark.requirement("find_data_elements_for_concept-4")
@pytest.mark.scenario("release/graph-version-missing")
def test_expansion_without_ncit_graph_version_is_release_not_available(tools, pinned):
    assert error_code(_find(tools, pinned, expandDescendants=True)) == "release_not_available"


@pytest.mark.tool(FIND)
@pytest.mark.requirement("find_data_elements_for_concept-4")
@pytest.mark.live_capable
def test_a_terminology_other_than_ncit_is_an_invalid_request(tools, pinned):
    result = _find(tools, pinned | {"terminology": "mdr"})

    assert error_code(result) == "invalid_request", result.content


@pytest.mark.tool(FIND)
@pytest.mark.requirement("find_data_elements_for_concept-5")
@pytest.mark.scenario("ssis/distinct-cap")
def test_descendants_beyond_the_tool_s_maximum_are_truncated_with_how_much_was_left_out(
    tools, pinned, recorded
):
    maximum = TOOLS[FIND]["bounds"]["limit"]["maximum"]
    # Explicitly synthetic: the old bounded Disease capture is not a complete distinct answer.
    rows = sparql_rows(recorded("scenarios/ssis/distinct-cap/data-elements-c2991-descendants.json"))
    assert len({row["id"] for row in rows}) == maximum + 1

    content = _ok(_find(tools, pinned, conceptCode="C2991", expandDescendants=True, limit=maximum))

    record = content["truncation"]
    assert len(content["dataElements"]) == maximum
    assert (record["occurred"], record["bound"], record["limit"], record["reached"]) == (
        True,
        "results",
        maximum,
        maximum,
    )
    assert (record["omitted"] >= 1, record["exact"]) == (True, False)
    assert "nextCursor" not in content


# The API's data element and exact value; EVS supplies the pinned concept record.
ELEMENT, VALUE = "2200604", "Male"
CADSR_ELEMENT = f"recorded/cadsr/data-element-{ELEMENT}.json"


def _value_concept(recorded):
    """The API's primary and item version, never the graph's main concept."""
    element = recorded(CADSR_ELEMENT)["response"]["body"]["DataElement"]
    (code,) = _primary_concepts(element)
    return code, element["version"]


def _primary_concepts(element):
    """The primary concepts of the value meaning of VALUE in a caDSR data element."""

    meanings = [
        value["ValueMeaning"]
        for value in element["ValueDomain"]["PermissibleValues"]
        if value["value"] == VALUE
    ]
    concepts = [concept for meaning in meanings for concept in meaning["Concepts"]]
    return [concept["conceptCode"] for concept in concepts if concept["primaryIndicator"] == "Yes"]


def _permissible(tools, pinned, value=VALUE):
    return tools.call(
        PERMISSIBLE, {"dataElementId": ELEMENT, "value": value, "release": pinned["release"]}
    )


@pytest.mark.tool(PERMISSIBLE)
@pytest.mark.requirement("get_concept_for_permissible_value-1")
def test_a_value_resolves_to_the_concept_it_stands_for_naming_both_content_states(
    tools, pinned, recorded
):
    content = _ok(_permissible(tools, pinned))

    provenance = content["provenance"]
    assert provenance["source"] == "evs_rest"
    code, version = _value_concept(recorded)
    concept = recorded(f"recorded/evs/concepts/{code}.json")["response"]["body"]
    assert (content["code"], content["name"]) == (concept["code"], concept["name"])
    assert (provenance["release"]["terminology"], provenance["release"]["identifier"]) == (
        pinned["terminology"],
        pinned["release"],
    )
    assert provenance["registry"] == {"registry": "cadsr"}
    assert "graphs" not in provenance
    assert "graphs" not in provenance["upstream"]
    assert provenance["upstream"]["cadsr"]["source"] == "cadsr_rest"
    assert content["permissibleValue"] == {
        "dataElement": {"publicId": ELEMENT, "version": version},
        "value": VALUE,
    }


@pytest.mark.tool(PERMISSIBLE)
@pytest.mark.requirement("get_concept_for_permissible_value-1")
@pytest.mark.scenario("cadsr/primary-inversion")
def test_the_api_primary_is_not_the_graphs_main_qualifier(tools, pinned):
    content = _ok(
        tools.call(
            PERMISSIBLE,
            {
                "dataElementId": "2178439",
                "value": "To a large extent",
                "release": pinned["release"],
            },
        )
    )
    assert content["code"] == "C25332"
    assert content["permissibleValue"]["dataElement"] == {"publicId": "2178439", "version": "2"}
    assert "graphs" not in content["provenance"]


@pytest.mark.tool(PERMISSIBLE)
@pytest.mark.requirement("get_concept_for_permissible_value-1")
@pytest.mark.parametrize("version", ["1", "4.0"])
def test_a_requested_api_item_version_keeps_its_own_values_and_spelling(
    tools, pinned, recorded, version
):
    filename = "data-element-2200604-version-1" if version == "1" else "data-element-2200604"
    raw = recorded(f"recorded/cadsr/{filename}.json")["response"]["body"]["DataElement"]
    (code,) = _primary_concepts(raw)
    content = _ok(
        tools.call(
            PERMISSIBLE,
            {
                "dataElementId": ELEMENT,
                "value": VALUE,
                "version": version,
                "release": pinned["release"],
            },
        )
    )
    assert content["code"] == code
    assert content["permissibleValue"]["dataElement"] == {
        "publicId": ELEMENT,
        "version": raw["version"],
    }


@pytest.mark.tool(PERMISSIBLE)
@pytest.mark.requirement("get_concept_for_permissible_value-1")
@pytest.mark.scenario("cadsr/concept-sources")
@pytest.mark.parametrize(
    "identifier,value,code,source,reason",
    [
        ("5024313", "Incident", "LA20347-3", "LOINC_CODE", "non_ncit_source"),
        ("99000003", "Crafted UMLS value", "C0009219", "UMLS_CUI", "non_ncit_source"),
        ("14940584", "10000", "C45255::10000", "NCI_CONCEPT_CODE", "compound_code"),
    ],
    ids=["LOINC", "UMLS", "compound"],
)
def test_non_ncit_references_are_successful_without_invented_concepts(
    tools, pinned, identifier, value, code, source, reason
):
    content = _ok(
        tools.call(
            PERMISSIBLE, {"dataElementId": identifier, "value": value, "release": pinned["release"]}
        )
    )
    assert {key: item for key, item in content.items() if key != "provenance"} == {
        "conceptReference": {"conceptCode": code, "terminology": source},
        "unresolvedReason": reason,
        "permissibleValue": {
            "dataElement": {"publicId": identifier, "version": "2"},
            "value": value,
        },
    }
    provenance = content["provenance"]
    assert provenance["source"] == "cadsr_rest"
    assert provenance["release"] == {"registry": "cadsr"}
    assert provenance["upstream"] == {"publicId": identifier, "version": "2"}
    assert "graphs" not in provenance


@pytest.mark.tool(PERMISSIBLE)
@pytest.mark.requirement("get_concept_for_permissible_value-3")
@pytest.mark.parametrize("value", [VALUE.lower(), VALUE.upper(), f" {VALUE}", f"{VALUE} "])
def test_a_value_the_data_element_does_not_have_as_given_is_not_found(
    tools, pinned, recorded, value
):
    # Each surface holds the value only as given, so no surface's answer has this one.
    rows = _rows(recorded, f"values-of-{ELEMENT}")
    element = recorded(CADSR_ELEMENT)["response"]["body"]["DataElement"]
    held = {row["value"] for row in rows} | {
        each["value"] for each in element["ValueDomain"]["PermissibleValues"]
    }
    assert (VALUE in held, value in held) == (True, False)

    assert error_code(_permissible(tools, pinned, value)) == "not_found"


@pytest.mark.tool(PERMISSIBLE)
@pytest.mark.requirement("get_concept_for_permissible_value-2")
@pytest.mark.live_capable
def test_a_permissible_value_by_its_identifier_is_unavailable(tools, pinned, recorded):
    asked = recorded("recorded/cadsr/permissible-value-9192925.json")
    # The platform's path for a value by its identifier answers 404 (OP-C10).
    assert asked["response"]["status"] == 404  # noqa: PLR2004 - HTTP Not Found
    identifier = asked["request"]["path"].rpartition("/")[2]

    result = tools.call(
        PERMISSIBLE, {"permissibleValueId": identifier, "release": pinned["release"]}
    )

    assert error_code(result) == "capability_unavailable", result.content


@pytest.mark.tool(STORED)
@pytest.mark.requirement("resolve_stored_value-1")
def test_a_gdc_value_resolves_through_the_mapset_its_source_names(tools, pinned, recorded):
    maps = recorded("recorded/evs/mapset-gdc-maps-code.json")["response"]["body"]["maps"]
    # EVS term search also returns the unrelated prefix hit C48177.
    maps = [row for row in maps if row["sourceCode"] == "C4817"]
    mapset = recorded("recorded/evs/mapset-gdc.json")["response"]["body"]
    source = {"mapset": mapset["code"], "version": mapset["version"]}

    content = _ok(
        tools.call(STORED, {"conceptCode": "C4817", "commons": "GDC", "release": pinned["release"]})
    )

    stored = content["storedValues"]
    assert {(value["value"], value["field"]) for value in stored} == {
        (each["targetName"], each["targetCode"]) for each in maps
    }
    evidence = content["evidence"]
    assert [value["source"] for value in stored] == [source] * len(stored)
    assert (content["confidence"], source in evidence["sources"]) == ("asserted", True)
    assert (evidence["valueLevelBinding"], evidence["coverage"]) == (True, len(stored))


def _bound(crosswalk, commons, code):
    """The values of the crosswalk's data elements a commons uses that stand for `code`, each
    with its data element's CRDC name, public id and version."""

    return {
        (
            value["Permissible Value"],
            element["CRDC Name"],
            element["CDE Public ID"],
            element["Version"],
        )
        for element in crosswalk
        if commons in _users(element)
        # A data element without enumerated values has no permissibleValues (crdc-list.json).
        for value in element.get("permissibleValues", [])
        if code in (value["Concept Code"] or "").split(":")
    }


def _users(element):
    """The contexts and commons the crosswalk says use a data element."""

    return [user.strip() for user in (element["Used By"] or "").split(",")]


def _crosswalked(value):
    element = value["source"]["dataElement"]
    return value["value"], value["field"], element["publicId"], element["version"]


@pytest.mark.tool(STORED)
@pytest.mark.requirement("resolve_stored_value-2")
def test_another_commons_value_resolves_through_the_crdc_crosswalk(tools, pinned, recorded):
    crosswalk = recorded(CROSSWALK)["response"]["body"]["CRDCDataElements"]
    # Sex at Birth binds Male to C20197 for Pediatric Cancer (crdc-list.json).
    expected = _bound(crosswalk, "Pediatric Cancer", "C20197")
    assert expected

    arguments = {"conceptCode": "C20197", "commons": "Pediatric Cancer"}
    content = _ok(tools.call(STORED, arguments | {"release": pinned["release"]}))

    stored = content["storedValues"]
    evidence = content["evidence"]
    assert set(map(_crosswalked, stored)) == expected
    assert {value["source"]["crosswalk"] for value in stored} == {"CRDC"}
    assert all(value["source"] in evidence["sources"] for value in stored)
    assert (content["confidence"], evidence["valueLevelBinding"]) == ("asserted", True)
    assert evidence["coverage"] == len(stored)


@pytest.mark.tool(STORED)
@pytest.mark.requirement("resolve_stored_value-3")
def test_a_commons_without_a_value_level_binding_stores_no_value_and_says_so(
    tools, pinned, recorded
):
    crosswalk = recorded(CROSSWALK)["response"]["body"]["CRDCDataElements"]
    # No data element of the crosswalk is used by PDC (crdc-list.json).
    assert not [element for element in crosswalk if "PDC" in _users(element)]

    content = _ok(
        tools.call(STORED, {"conceptCode": "C4817", "commons": "PDC", "release": pinned["release"]})
    )

    evidence = content["evidence"]
    assert (content["storedValues"], content["confidence"]) == ([], "none")
    assert (evidence["valueLevelBinding"], evidence["coverage"]) == (False, 0)


def _datasets(recorded):
    """Each dataset's version and ISO date, as its source gives them."""

    (ncit,) = recorded("recorded/evs/release-monthly.json")["response"]["body"]
    graphs = {row["graph"]: row for row in _rows(recorded, "graph-identities")}
    return {
        "ncit": (ncit["version"], ncit["date"]),
        "ssis_ncit_graph": (graphs[THESAURUS]["version"], _iso(graphs[THESAURUS]["date"])),
        "ssis_cadsr_graph": (None, _iso(graphs[CADSR_GRAPH]["date"])),
        "cadsr_export": (None, export_date(recorded(EXPORT_LISTING)["response"]["body"])),
    }


@pytest.mark.tool(ALIGNMENT)
@pytest.mark.requirement("get_release_alignment-1")
@pytest.mark.parametrize("threshold", [None, 120])
def test_every_dataset_is_dated_with_the_largest_interval_and_a_warning_above_the_threshold(
    tools, recorded, threshold
):
    expected = _datasets(recorded)
    interval = max(
        abs((date.fromisoformat(one) - date.fromisoformat(other)).days)
        for (_, one), (_, other) in combinations(expected.values(), 2)
    )
    maximum = threshold or defaults(ALIGNMENT)["maxIntervalDays"]

    result = tools.call(ALIGNMENT, {} if threshold is None else {"maxIntervalDays": threshold})

    content = _ok(result)
    found = {each["name"]: (each.get("version"), each["date"]) for each in content["datasets"]}
    assert found == expected
    assert content["intervalDays"] == interval
    # The recorded dates are 89 days apart: above the default of 31, below 120.
    assert ("warning" in content, str(maximum) in content.get("warning", "")) == (
        interval > maximum,
        interval > maximum,
    )
    assert result.meta.get("ttlMs") == 0
