"""Graph role claims, read through public tools against independently decoded recordings."""

from urllib.parse import unquote

import pytest

from nci_sip_acceptance.results import sparql_rows

FIND = "find_data_elements_for_concept"
GROUND = "ground_value"


def expected_roles(recorded, code, expand=False):
    suffix = "-descendants" if expand else ""
    source = recorded(f"recorded/ssis-sparql/data-elements-statuses-{code.lower()}{suffix}.json")
    return {row["id"]: decoded(row["roles"]) for row in sparql_rows(source)}


def decoded(text):
    evidence = {tuple(unquote(part) for part in token.split("|")) for token in text.split()}
    return sorted(
        [
            {"role": role, "conceptCode": uri.split("#")[1]}
            | ({"displayOrder": int(order)} if order else {})
            for role, uri, order in evidence
        ],
        key=lambda item: (item["role"], item["conceptCode"], item.get("displayOrder", -1)),
    )


@pytest.mark.tool(FIND)
@pytest.mark.requirement("find_data_elements_for_concept-1")
@pytest.mark.parametrize("expand", [False, True], ids=["exact", "descendants"])
def test_every_matching_graph_role_and_actual_concept_is_preserved(tools, pinned, recorded, expand):
    result = tools.call(
        FIND,
        pinned
        | {
            "conceptCode": "C25332",
            "expandDescendants": expand,
            "limit": 1000,
        },
    )
    assert not result.is_error, result.content
    uses = result.content["dataElements"]
    assert {item["dataElement"]["publicId"]: item.get("roles") for item in uses} == expected_roles(
        recorded,
        "C25332",
        expand,
    )
    assert {role["role"] for item in uses for role in item["roles"]} == {
        "objectClass",
        "property",
        "valueMeaning",
    }


@pytest.mark.tool(FIND)
@pytest.mark.requirement("find_data_elements_for_concept-1")
def test_graph_main_at_order_one_never_claims_primary(tools, pinned):
    for code, order in (("C49508", 1), ("C25332", 0)):
        result = tools.call(FIND, pinned | {"conceptCode": code, "limit": 1000})
        assert not result.is_error, result.content
        use = next(
            row
            for row in result.content["dataElements"]
            if row["dataElement"]["publicId"] == "2178439"
        )
        assert use.get("roles") == [
            {"role": "valueMeaning", "conceptCode": code, "displayOrder": order}
        ]


@pytest.mark.tool(FIND)
@pytest.mark.requirement("find_data_elements_for_concept-1")
@pytest.mark.scenario("ssis/missing-order")
def test_missing_graph_order_stays_absent(tools, pinned):
    result = tools.call(FIND, pinned | {"conceptCode": "C17357"})
    assert not result.is_error, result.content
    assert result.content["dataElements"][0].get("roles") == [
        {"role": "property", "conceptCode": "C17357"},
    ]


@pytest.mark.tool(GROUND)
@pytest.mark.requirement("ground_value-2")
def test_grounding_preserves_the_same_complete_role_record(tools, pinned, recorded):
    result = tools.call(GROUND, {"release": pinned["release"], "conceptCode": "C4817"})
    assert not result.is_error, result.content
    assert {
        item["dataElement"]["publicId"]: item.get("roles")
        for item in result.content["dataElements"]
    } == expected_roles(recorded, "C4817")


def expected_statuses(raw):
    return {
        field: sorted({unquote(token[1:]) for token in raw[field].split(" ") if token})
        for field in ("workflowStatuses", "registrationStatuses")
        if raw.get(field)
    }


def public_statuses(use):
    return {
        field: use[field] for field in ("workflowStatuses", "registrationStatuses") if field in use
    }


@pytest.mark.tool(FIND, GROUND)
@pytest.mark.requirement("find_data_elements_for_concept-1", "ground_value-2")
def test_graph_status_sets_preserve_unpaired_public_id_evidence(tools, pinned, recorded):
    source = recorded("recorded/ssis-sparql/data-elements-statuses-c48179.json")
    raw = next(row for row in sparql_rows(source) if row["id"] == "2672191")
    expected = expected_statuses(raw)
    assert expected == {
        "workflowStatuses": ["RELEASED", "RETIRED ARCHIVED"],
        "registrationStatuses": ["Standard", "Superceded"],
    }
    result = tools.call(FIND, pinned | {"conceptCode": "C48179", "limit": 1000})
    assert not result.is_error, result.content
    use = next(
        row for row in result.content["dataElements"] if row["dataElement"]["publicId"] == "2672191"
    )
    assert public_statuses(use) == expected
    assert set(use) == {
        "dataElement",
        "roles",
        "provenance",
        "workflowStatuses",
        "registrationStatuses",
    }
    assert set(use["dataElement"]) == {"publicId", "longName"}
    grounded = tools.call(GROUND, {"release": pinned["release"], "conceptCode": "C4817"})
    assert not grounded.is_error, grounded.content
    source = recorded("recorded/ssis-sparql/data-elements-statuses-c4817.json")
    expected_grounding = {row["id"]: expected_statuses(row) for row in sparql_rows(source)}
    assert {
        row["dataElement"]["publicId"]: public_statuses(row)
        for row in grounded.content["dataElements"]
    } == expected_grounding
