"""The caDSR tools' own requirements (spec/requirements.yaml), and the cross-cutting ones only a
caDSR answer shows, each test against its tool.

Fixture expectations come from recordings. Anonymous live content checks assert identity,
version, status and include contracts without assuming recorded values.
"""

import json
from decimal import Decimal
from http import HTTPStatus

import pytest

from nci_sip_acceptance.citations import verify_citations
from nci_sip_acceptance.results import (
    EXPORT,
    EXPORT_LISTING,
    error_code,
    export_date,
    is_name,
    release_of,
    requests_naming,
)
from nci_sip_acceptance.spec import RECORDS, TOOLS

# Recorded both ways: recorded/cadsr/data-element-2200604.json answers a request that names
# Accept: application/json, data-element-2200604-html.json, with HTML, any other.
DATA_ELEMENT = "2200604"
JSON_ANSWER = "recorded/cadsr/data-element-2200604.json"
HTML_ANSWER = "recorded/cadsr/data-element-2200604-html.json"
# Unknown to caDSR, which answers HTTP 200 with DataElement null (data-element-unknown.json).
UNKNOWN = "99999999"
# No number, which caDSR refuses with HTTP 200 and apiResponse type E (data-element-refused.json).
REFUSED = "notanumber"


# A server that answered the same call before may serve it from its cache, asking nothing.
@pytest.mark.own_server
@pytest.mark.tool("get_data_element")
@pytest.mark.requirement("X-15")
def test_a_server_that_leaves_out_accept_gets_html_and_never_parses_it(tools, upstream):
    before = len(upstream.log())

    result = tools.call("get_data_element", {"publicId": DATA_ELEMENT})

    # The recording that answered says whether the request named Accept: application/json.
    reached = {e["fixture"] for e in upstream.log()[before:] if e["surface"] == "cadsr"}
    assert reached in ({JSON_ANSWER}, {HTML_ANSWER}), reached
    if reached == {JSON_ANSWER}:
        assert not result.is_error, result.content
        assert result.content.get("publicId") == DATA_ELEMENT
    else:
        assert error_code(result) == "upstream_unavailable", result.content


@pytest.mark.tool("get_data_element")
@pytest.mark.requirement("X-15")
@pytest.mark.live_capable
def test_a_failure_inside_an_http_200_is_an_error_never_an_empty_success(tools, recorded):
    answer = recorded("recorded/cadsr/data-element-unknown.json")["response"]
    # caDSR answers the unknown id with HTTP 200, no data element and a note that says so.
    assert (answer["status"], answer["body"]["DataElement"]) == (200, None)

    result = tools.call("get_data_element", {"publicId": UNKNOWN})

    assert error_code(result) == "not_found", result.content


@pytest.mark.scenario("cadsr/html-for-json")
@pytest.mark.tool("get_data_element")
@pytest.mark.requirement("X-15")
def test_html_where_json_was_asked_for_is_an_upstream_error(tools, recorded):
    answer = recorded("scenarios/cadsr/html-for-json/data-element-2200604.json")["response"]
    # The scenario answers the request for JSON with HTML and HTTP 200.
    assert answer["status"] == HTTPStatus.OK
    assert answer["body"].startswith("<BODY")

    result = tools.call("get_data_element", {"publicId": DATA_ELEMENT})

    assert error_code(result) == "upstream_unavailable", result.content


@pytest.mark.tool("get_data_element")
@pytest.mark.requirement("X-15")
@pytest.mark.live_capable
def test_a_refusal_inside_an_http_200_is_an_invalid_request(tools, recorded):
    answer = recorded("recorded/cadsr/data-element-refused.json")["response"]
    assert (answer["status"], answer["body"]["apiResponse"]["type"]) == (200, "E")

    result = tools.call("get_data_element", {"publicId": REFUSED})

    assert error_code(result) == "invalid_request", result.content


# The registry's state. The export folder dates releasedCDEsXML-OD.zip; /registry/releases
# answers 404 (registry-releases.json).
ELEMENT = JSON_ANSWER
VERSION_1 = "recorded/cadsr/data-element-2200604-version-1.json"
# What a data element record holds without include, and the sections include adds.
SECTIONS = TOOLS["get_data_element"]["values"]["include"]
OWN = [name for name in RECORDS["data_element"]["fields"] if name not in SECTIONS]


def _element(recorded, fixture=ELEMENT):
    return recorded(fixture)["response"]["body"]["DataElement"]


@pytest.mark.tool("get_data_element")
@pytest.mark.requirement("get_data_element-1")
def test_latest_version_can_be_retired_or_draft(tools, recorded):
    for identifier, status in [("2413218", "RETIRED ARCHIVED"), ("3113185", "DRAFT MOD")]:
        raw = _element(recorded, f"recorded/cadsr/data-element-{identifier}.json")
        assert (raw["latestVersionIndicator"], raw["workflowStatus"]) == ("Yes", status)
        content = _ok(tools.call("get_data_element", {"publicId": identifier}))
        assert _own(content) == _own(raw)


@pytest.mark.tool("get_data_element")
@pytest.mark.requirement("get_data_element-1")
def test_non_enumerated_domain_is_not_inferred_from_empty_values(tools, recorded):
    for identifier, domain_type in [("2179689", "Non-enumerated"), ("2180822", "Enumerated")]:
        raw = _element(recorded, f"recorded/cadsr/data-element-{identifier}.json")
        assert raw["ValueDomain"]["type"] == domain_type
        content = _ok(
            tools.call(
                "get_data_element",
                {"publicId": identifier, "include": ["valueDomain", "permissibleValues"]},
            )
        )
        assert content["valueDomain"] == _section(raw, "valueDomain")
        assert len(content["permissibleValues"]) == len(
            raw["ValueDomain"].get("PermissibleValues", [])
        )
        if identifier == "2179689":
            assert "PermissibleValues" not in raw["ValueDomain"]
            assert content["permissibleValues"] == []


@pytest.mark.tool("get_data_element")
@pytest.mark.requirement("get_data_element-1")
def test_reference_enumerated_domain_preserves_terminology_and_uri_values(tools, recorded):
    raw = _element(recorded, "recorded/cadsr/data-element-14883075.json")
    domain = raw["ValueDomain"]
    assert (domain["type"], domain["referenceTerminology"]) == (
        "Enumerated by Reference",
        "Uberon|C190111",
    )
    assert [row["value"] for row in domain["PermissibleValues"]] == [
        "http://purl.obolibrary.org/obo/UBERON_0002465"
    ]
    content = _ok(
        tools.call(
            "get_data_element",
            {"publicId": "14883075", "include": ["valueDomain", "permissibleValues"]},
        )
    )
    assert content["valueDomain"] == _section(raw, "valueDomain")
    assert _returned(content, "permissibleValues") == _section(raw, "permissibleValues")


@pytest.mark.tool("get_data_element")
@pytest.mark.requirement("get_data_element-1")
@pytest.mark.live_capable
def test_live_component_citations_match_the_explicit_api_version(tools, upstream):
    if upstream is None:
        raise ValueError("live caDSR citation comparison is disabled by run policy")
    content = verify_citations(tools, upstream.base_url("cadsr"))
    assert (content["publicId"], content["version"]) == (DATA_ELEMENT, "4")


def _ok(result):
    assert not result.is_error, result.content
    return result.content


# A server that answered the same call before may serve it from its cache, asking nothing.
@pytest.mark.own_server
@pytest.mark.tool("resolve_registry_release")
@pytest.mark.requirement("resolve_registry_release-1")
@pytest.mark.registry_release
def test_without_a_registry_release_the_export_date_stands_for_it(tools, upstream, recorded):
    dated = export_date(recorded(EXPORT_LISTING)["response"]["body"])
    assert (
        recorded("recorded/cadsr/registry-releases.json")["response"]["status"]
        == HTTPStatus.NOT_FOUND
    )

    result = tools.call("resolve_registry_release", {})

    content = _ok(result)
    assert (content.get("published"), "identifier" in content) == (False, False)
    assert str(content.get("generatedAt", "")).startswith(dated)
    assert EXPORT in str(content.get("sourceDistribution"))
    assert result.meta.get("ttlMs") == 0
    asked = [(entry["surface"], entry["path"], str(entry["params"])) for entry in upstream.log()]
    assert len(asked) == len(set(asked))


@pytest.mark.tool("get_data_element")
@pytest.mark.requirement("get_data_element-1")
@pytest.mark.live_capable
@pytest.mark.parametrize("version", [None, "1", "4.0"], ids=["latest", "version-1", "version-4.0"])
def test_a_data_element_is_its_own_fields_alone_with_its_version_and_statuses(
    tools, target, recorded, version
):
    pinned = {"version": version} if version else {}

    content = _ok(tools.call("get_data_element", {"publicId": DATA_ELEMENT, **pinned}))

    assert set(content) - set(OWN) == set()
    if target.mode == "fixture":
        assert _own(content) == _own(_element(recorded, VERSION_1 if version == "1" else ELEMENT))
    else:
        assert content.get("publicId") == DATA_ELEMENT
        assert all(_live_scalar(content, key) for key in OWN if key != "provenance")
        assert version is None or Decimal(content["version"]) == Decimal(version)
        assert release_of(content) == ("cadsr", None)
    assert content["provenance"]["upstream"]["version"] == content["version"]


def _live_scalar(content, key):
    field = RECORDS["data_element"]["fields"][key]
    value = content.get(key)
    if field.get("nullable"):
        return (
            (key not in content and field.get("optional"))
            or value is None
            or isinstance(value, str)
        )
    return is_name(value)


def _own(element):
    """A data element's own fields, provenance aside, as the record names them."""

    return {key: element[key] for key in OWN if key != "provenance" and key in element}


def _concepts(element):
    """Ordered API assertions, including representation, with explicit flags and order."""

    concept = element["DataElementConcept"]
    components = [
        ("objectClass", concept["ObjectClass"]),
        ("property", concept["Property"]),
        ("representationTerm", element["ValueDomain"]["RepresentationTerm"]),
    ]
    return [
        _derivation(each) | {"role": role} for role, part in components for each in part["Concepts"]
    ]


def _derivation(raw):
    result = _fields(raw, ("conceptCode", "longName")) | {
        "primary": raw["primaryIndicator"] == "Yes"
    }
    if "evsSource" in raw:
        result["terminology"] = raw["evsSource"]
    if "displayOrder" in raw:
        result["displayOrder"] = int(raw["displayOrder"])
    return result


def _component(raw):
    return _fields(raw, ("publicId", "version", "longName", "dateModified", "beginDate", "endDate"))


def _dec(element):
    raw = element["DataElementConcept"]
    return _component(raw) | {
        "objectClass": _component(raw["ObjectClass"]),
        "property": _component(raw["Property"]),
    }


def _section(element, include):
    """What a section holds, read from the recording, in the form the test compares."""

    domain = element["ValueDomain"]
    sections = {
        "permissibleValues": lambda: [
            (value["publicId"], value["value"], _meaning(value["ValueMeaning"]))
            for value in domain["PermissibleValues"]
        ],
        "valueDomain": lambda: {k: v for k, v in domain.items() if k != "PermissibleValues"},
        "conceptAssociations": lambda: _concepts(element),
        "alternateNames": lambda: element["AlternateNames"],
        "referenceDocuments": lambda: element["ReferenceDocuments"],
        "classificationSchemes": lambda: [
            (
                _fields(scheme, SCHEME),
                [_fields(i, ITEM) for i in scheme["ClassificationSchemeItems"]],
            )
            for scheme in element["ClassificationSchemes"]
        ],
    }
    return sections[include]()


# The fields of a classification scheme and of its items the record names.
SCHEME, ITEM = ("publicId", "version", "longName", "context"), ("publicId", "version", "longName")


def _fields(entry, names):
    return {name: entry[name] for name in names if name in entry}


def _meaning(meaning):
    """A value meaning by its identity and name, with its concepts by code and name."""

    concepts = (
        [_derivation(row) for row in meaning["Concepts"]]
        if "Concepts" in meaning
        else meaning.get("concepts", [])
    )
    return _component(meaning), concepts


def _returned(content, include):
    """What a section of a result holds, in the form the test compares."""

    found = content.get(include)
    forms = {
        "permissibleValues": lambda: [
            (value.get("publicId"), value.get("value"), _meaning(value.get("valueMeaning") or {}))
            for value in found
        ],
        "conceptAssociations": lambda: found,
        "classificationSchemes": lambda: [
            (_fields(scheme, SCHEME), [_fields(i, ITEM) for i in scheme.get("items", [])])
            for scheme in found
        ],
    }
    return forms.get(include, lambda: found)() if isinstance(found, (list, dict)) else found


@pytest.mark.tool("get_data_element")
@pytest.mark.requirement("get_data_element-1")
@pytest.mark.live_capable
@pytest.mark.parametrize(
    "include", [section for section in SECTIONS if section != "dataElementConcept"]
)
def test_each_include_returns_its_section_as_the_platform_gives_it(
    tools, target, recorded, include
):
    # All sections come from the anonymous DataElement response, not CDE Match or NCILovAPI.
    content = _ok(tools.call("get_data_element", {"publicId": DATA_ELEMENT, "include": [include]}))

    if target.mode == "fixture":
        expected = _section(_element(recorded), include)
        assert expected
        assert _returned(content, include) == expected
    else:
        assert include in content
        section = content[include]
        assert isinstance(section, dict if include == "valueDomain" else list)
        assert content.get("publicId") == DATA_ELEMENT
        assert release_of(content) == ("cadsr", None)
    assert [name for name in SECTIONS if name != include and name in content] == []
    # A permissible value and a scheme are items of their own, each with its provenance.
    nested = content[include] if include in ("permissibleValues", "classificationSchemes") else []
    assert [(entry.get("provenance") or {}).get("release") for entry in nested] == [
        {"registry": "cadsr"}
    ] * len(nested)


@pytest.mark.tool("get_data_element")
@pytest.mark.requirement("get_data_element-1")
@pytest.mark.parametrize("version", [None, "1"], ids=["latest", "version-1"])
def test_cited_items_keep_their_own_identity_dates_and_derivations(tools, recorded, version):
    raw = _element(recorded, VERSION_1 if version else ELEMENT)
    args = {"version": version} if version else {}
    content = _ok(
        tools.call(
            "get_data_element",
            {
                "publicId": DATA_ELEMENT,
                "include": [
                    "dataElementConcept",
                    "permissibleValues",
                    "conceptAssociations",
                    "valueDomain",
                ],
                **args,
            },
        )
    )
    assert content.get("dataElementConcept") == _dec(raw)
    assert content.get("conceptAssociations") == _concepts(raw)
    assert content.get("valueDomain") == _section(raw, "valueDomain")
    values = content["permissibleValues"]
    expected = raw["ValueDomain"]["PermissibleValues"]
    assert len(values) == len(expected)
    for value, source in zip(values, expected, strict=True):
        assert _fields(value, ("publicId", "dateModified", "beginDate", "endDate")) == _fields(
            source, ("publicId", "dateModified", "beginDate", "endDate")
        )
        assert "version" not in value
        assert _meaning(value["valueMeaning"]) == _meaning(source["ValueMeaning"])
        assert value["provenance"]["upstream"] == _fields(source, ("publicId", "dateModified"))


@pytest.mark.scenario("cadsr/omitted-citation-fields")
@pytest.mark.tool("get_data_element")
@pytest.mark.requirement("get_data_element-1")
def test_omitted_citation_fields_are_absent_and_explicit_null_stays_null(tools):
    content = _ok(
        tools.call(
            "get_data_element",
            {"publicId": DATA_ELEMENT, "include": ["dataElementConcept", "permissibleValues"]},
        )
    )
    items = [
        content,
        content["dataElementConcept"],
        content["dataElementConcept"]["objectClass"],
        content["dataElementConcept"]["property"],
        content["permissibleValues"][0],
        content["permissibleValues"][0]["valueMeaning"],
    ]
    assert ["dateModified" in item for item in items] == [False] * 6
    assert [item["endDate"] for item in items] == [None] * 6


@pytest.mark.tool("get_data_element")
@pytest.mark.requirement("get_data_element-2")
def test_a_question_text_one_data_element_has_finds_it(tools, recorded):
    found = recorded("recorded/cadsr/question-text-sex-of-a-person.json")["response"]["body"]
    (element,) = found["DataElements"]

    content = _ok(tools.call("get_data_element", {"questionText": "Sex of a Person"}))

    # The full data element, not the search's header record.
    assert set(content) - set(OWN) == set()
    assert _own(content) == _own(_element(recorded))
    assert content.get("publicId") == element["publicId"]


@pytest.mark.tool("get_data_element")
@pytest.mark.requirement("get_data_element-2")
def test_a_question_text_several_have_is_an_invalid_request_naming_them(tools, recorded):
    found = recorded("recorded/cadsr/question-text-date-of-birth.json")["response"]["body"]
    candidates = [element["publicId"] for element in found["DataElements"]]
    assert len(candidates) > 1

    result = tools.call("get_data_element", {"questionText": "Date of birth"})

    assert error_code(result) == "invalid_request", result.content
    said = json.dumps(result.content["error"])
    assert [
        candidate
        for candidate in candidates
        if f'"{candidate}"' not in said and f" {candidate}" not in said
    ] == []


@pytest.mark.tool("get_data_element")
@pytest.mark.requirement("get_data_element-2")
@pytest.mark.live_capable
def test_a_long_name_lookup_is_unavailable_never_empty(tools, recorded):
    result = tools.call("get_data_element", {"longName": _element(recorded)["longName"]})

    assert error_code(result) == "capability_unavailable", result.content


FORM = "recorded/cadsr/form-5406471.json"


@pytest.mark.tool("get_form")
@pytest.mark.requirement("get_form-1")
@pytest.mark.parametrize("version", [None, "1", "1.0"], ids=["latest", "version-1", "version-1.0"])
def test_a_form_returns_its_modules_and_its_status_unchanged_a_retired_one_too(
    tools, recorded, version
):
    form = recorded(FORM)["response"]["body"]["form"]
    assert form["workflowStatus"] == "RETIRED ARCHIVED"

    selected = {"version": version} if version is not None else {}
    content = _ok(tools.call("get_form", {"publicId": form["publicID"], **selected}))

    named = [name for name in RECORDS["form"]["fields"] if name not in ("provenance", "modules")]
    expected = {name: form.get(name) for name in named} | {"publicId": form["publicID"]}
    assert {name: content.get(name) for name in named} == expected
    assert content.get("modules") == form["modules"]
    module = content["modules"][0]
    question = module["questions"][0]
    assert _fields(module, ("publicID", "version", "dateModified", "beginDate", "endDate")) == {
        "publicID": "5406474",
        "version": "1.0",
        "dateModified": "2016-08-12",
    }
    assert _fields(question, ("publicID", "version", "dateModified", "beginDate", "endDate")) == {
        "publicID": "5406475",
        "version": "1.0",
        "dateModified": "2016-08-12",
    }
    assert content["provenance"]["upstream"]["version"] == form["version"]


@pytest.mark.tool("get_form")
@pytest.mark.requirement("get_form-1")
@pytest.mark.live_capable
def test_a_form_without_its_modules_has_none(tools, recorded):
    form = recorded(FORM)["response"]["body"]["form"]

    content = _ok(tools.call("get_form", {"publicId": form["publicID"], "includeModules": False}))

    assert content.get("publicId") == form["publicID"]
    assert "modules" not in content


@pytest.mark.tool("get_form")
@pytest.mark.requirement("get_form-1")
@pytest.mark.live_capable
def test_a_form_keyword_is_an_invalid_request_saying_an_identifier_is_needed(tools):
    result = tools.call("get_form", {"keyword": "Patient Safety Event Report"})

    assert error_code(result) == "invalid_request", result.content
    assert "identifier" in result.content["error"]["message"].lower()


@pytest.mark.tool("get_form")
@pytest.mark.requirement("X-15")
@pytest.mark.live_capable
def test_an_unknown_form_answered_inside_an_http_200_is_not_found(tools, recorded):
    answer = recorded("recorded/cadsr/form-unknown.json")["response"]
    # The Form API says type E for an id it does not know, where the data element API says I.
    assert (answer["status"], answer["body"]["form"], answer["body"]["apiResponse"]["type"]) == (
        200,
        None,
        "E",
    )

    result = tools.call("get_form", {"publicId": "99999999"})

    assert error_code(result) == "not_found", result.content


VM_MATCH = "recorded/cadsr/vm-match-male.json"


# The fields of a matched item the record names, with vmMatch's name for each.
MATCHED = {
    "itemType": "itemType",
    "publicId": "itemId",
    "version": "version",
    "name": "matchedName",
    "concept": "concept",
    "evsSource": "evsSource",
    "context": "context",
    "workflowStatus": "workflowStatus",
    "registrationStatus": "registrationStatus",
}


def _matched(match):
    """A vmMatch match as the record names it, a null field absent."""

    return {key: match[name] for key, name in MATCHED.items() if match[name] is not None}


@pytest.mark.tool("match_value_meanings")
@pytest.mark.requirement("match_value_meanings-1")
def test_value_meaning_matches_are_the_platform_s_in_its_order_with_their_rule(tools, recorded):
    (answer,) = recorded(VM_MATCH)["response"]["body"]["matchResults"]
    matches = answer["matches"]
    # Every recorded crosswalk is NA: none of the matches has one.
    assert {match["crosswalkCode"] for match in matches} == {"NA"}

    content = _ok(tools.call("match_value_meanings", {"values": [answer["name"]]}))

    found = content.get("matches", [])
    items = [match.get("item") or {} for match in found]
    assert [{key: item[key] for key in MATCHED if key in item} for item in items] == [
        _matched(match) for match in matches
    ]
    # What vmMatch says of each item's origin, passed through (A4.3).
    assert [(item.get("provenance") or {}).get("upstream") for item in items] == [
        {"itemId": match["itemId"], "version": match["version"]} for match in matches
    ]
    assert [match.get("rule") for match in found] == [match["ruleDescription"] for match in matches]
    # vmMatch scores nothing, and NA is no crosswalk: absent, never null.
    assert [key for match in found for key in ("score", "crosswalk") if key in match] == []


@pytest.mark.tool("match_value_meanings")
@pytest.mark.requirement("match_value_meanings-1")
@pytest.mark.live_capable
def test_more_values_than_the_tool_takes_is_an_invalid_request(tools):
    most = TOOLS["match_value_meanings"]["lists"]["values"]

    result = tools.call("match_value_meanings", {"values": ["Male"] * (most + 1)})

    assert error_code(result) == "invalid_request", result.content


CRDC = "recorded/cadsr/crdc-list.json"


def _crdc(recorded):
    return recorded(CRDC)["response"]["body"]["CRDCDataElements"]


def _used_by(entry):
    return [name.strip() for name in (entry["Used By"] or "").split(",") if name.strip()]


@pytest.mark.tool("get_code_map")
@pytest.mark.requirement("get_code_map-1")
def test_a_code_map_is_a_data_element_s_values_users_and_coverage(tools, recorded):
    # A data element with an uncoded value and a colon-joined code, so that both show.
    entry = next(entry for entry in _crdc(recorded) if _uncoded_and_joined(entry))
    values = [_value(value) for value in entry["permissibleValues"]]

    content = _ok(tools.call("get_code_map", {"dataElementId": entry["CDE Public ID"]}))

    (found,) = content.get("codeMaps", [])
    assert found.get("dataElement") == {
        "publicId": entry["CDE Public ID"],
        "version": entry["Version"],
    }
    assert (found.get("crdcName"), found.get("usedBy")) == (entry["CRDC Name"], _used_by(entry))
    assert found.get("valueLevelBinding") is True
    assert found.get("coverage") == sum(1 for value in values if "conceptCode" in value)
    assert found.get("values") == values
    upstream = (found.get("provenance") or {}).get("upstream")
    assert upstream == {"CDE Public ID": entry["CDE Public ID"], "Version": entry["Version"]}


def _codes(entry):
    return [value.get("Concept Code") for value in entry.get("permissibleValues") or []]


def _uncoded_and_joined(entry):
    codes = _codes(entry)
    return None in codes and any(":" in (code or "") for code in codes)


def _value(value):
    """A value as the record names it, its code absent where the platform gives none."""

    code = value["Concept Code"]
    return {"value": value["Permissible Value"]} | ({"conceptCode": code} if code else {})


@pytest.mark.tool("get_code_map")
@pytest.mark.requirement("get_code_map-1")
def test_a_data_element_without_value_level_binding_says_so(tools, recorded):
    entry = next(entry for entry in _crdc(recorded) if "permissibleValues" not in entry)

    content = _ok(tools.call("get_code_map", {"dataElementId": entry["CDE Public ID"]}))

    (found,) = content.get("codeMaps", [])
    assert (found.get("valueLevelBinding"), found.get("values")) == (False, [])


@pytest.mark.tool("get_code_map")
@pytest.mark.requirement("get_code_map-1")
# CIP: a name that is part of another (NCIP), which a match on part of the text would confuse.
@pytest.mark.parametrize("context", ["GDC", "CIP"])
def test_a_context_selects_the_code_maps_it_uses(tools, recorded, context):
    users = [entry["CDE Public ID"] for entry in _crdc(recorded) if context in _used_by(entry)]
    # Within one page of the limit's default, so that the whole selection shows.
    assert 1 < len(users) <= TOOLS["get_code_map"]["bounds"]["limit"]["default"]

    content = _ok(tools.call("get_code_map", {"targetContext": context}))

    found = [(m.get("dataElement") or {}).get("publicId") for m in content.get("codeMaps", [])]
    assert sorted(found) == sorted(users)


@pytest.mark.tool("get_code_map")
@pytest.mark.requirement("get_code_map-1")
@pytest.mark.live_capable
def test_a_source_system_other_than_crdc_is_an_invalid_request(tools):
    assert TOOLS["get_code_map"]["values"]["sourceSystem"] == ["CRDC"]

    result = tools.call("get_code_map", {"sourceSystem": "GDC"})

    assert error_code(result) == "invalid_request", result.content


# Capabilities the platform does not serve yet: each answers capability_unavailable (M1.2).
UNAVAILABLE = [
    pytest.param(
        "get_permissible_value",
        {"permissibleValueId": "9192925"},
        id="permissible-value",
        marks=[
            pytest.mark.tool("get_permissible_value"),
            pytest.mark.requirement("get_permissible_value-1"),
        ],
    ),
    pytest.param(
        "list_classification_schemes",
        {},
        id="classification-schemes",
        marks=[
            pytest.mark.tool("list_classification_schemes"),
            pytest.mark.requirement("list_contexts-1"),
        ],
    ),
    *[
        pytest.param(
            "search_data_elements",
            {"query": "gender", "mode": mode},
            id=f"search-{mode}",
            marks=[
                pytest.mark.tool("search_data_elements"),
                pytest.mark.requirement("search_data_elements-1"),
            ],
        )
        for mode in ("semantic", "hybrid")
    ],
]


@pytest.mark.parametrize(("name", "arguments"), UNAVAILABLE)
@pytest.mark.live_capable
def test_a_capability_the_platform_lacks_is_unavailable_never_empty(tools, name, arguments):
    result = tools.call(name, arguments)

    assert error_code(result) == "capability_unavailable", result.content


@pytest.mark.tool("get_data_element")
@pytest.mark.requirement("get_data_element-2")
def test_a_question_text_no_data_element_has_is_not_found(tools, recorded):
    found = recorded("recorded/cadsr/question-text-unmatched.json")["response"]["body"]
    assert found["DataElements"] == []

    result = tools.call("get_data_element", {"questionText": "qqxyzzyqq"})

    assert error_code(result) == "not_found", result.content


# Crafted (PASS fixture only): no keyword search exists today, and the crafted answer of the
# inventory's form holds the contract's cap of 1,000 data elements.
OVER_CAP = "crafted/OP-C03/search-over-cap.json"


@pytest.mark.tool("search_data_elements")
@pytest.mark.requirement("search_data_elements-1")
def test_a_search_s_page_is_the_limit_given(tools, recorded):
    capped = len(recorded(OVER_CAP)["response"]["body"]["DataElements"])
    limit = 25
    assert limit < capped

    content = _ok(tools.call("search_data_elements", {"query": "patient", "limit": limit}))

    assert len(content.get("results", [])) == limit


@pytest.mark.tool("search_data_elements")
@pytest.mark.requirement("search_data_elements-1")
def test_a_search_the_platform_caps_reports_the_cap_and_no_total(tools, recorded):
    capped = len(recorded(OVER_CAP)["response"]["body"]["DataElements"])

    content = _ok(tools.call("search_data_elements", {"query": "patient"}))

    truncation = content.get("truncation") or {}
    assert (truncation.get("occurred"), truncation.get("bound")) == (True, "upstream_cap")
    assert (truncation.get("limit"), truncation.get("exact")) == (capped, False)
    assert truncation.get("omitted", 0) >= 1
    assert truncation.get("reached") == capped
    assert "totalKnown" not in content
    assert (
        len(content.get("results", []))
        == TOOLS["search_data_elements"]["bounds"]["limit"]["default"]
    )


# Crafted to the published contracts (PASS fixture only until credentials let the recorder
# replace them): CDE Match and the context list refuse an anonymous caller.
CREDENTIALED = "scenarios/cadsr/credentialed"


@pytest.mark.scenario("cadsr/credentialed")
@pytest.mark.tool("match_data_elements")
@pytest.mark.requirement("match_data_elements-1")
def test_data_element_matches_are_scored_and_rule_attributed_as_the_platform_says(tools, recorded):
    answer = recorded(f"{CREDENTIALED}/cde-match.json")["response"]["body"]["matchResults"]

    content = _ok(tools.call("match_data_elements", {"entities": [{"name": answer["entity"]}]}))

    found = [
        (
            m.get("entity"),
            m.get("score"),
            m.get("rule"),
            m.get("matchedText"),
            (m.get("dataElement") or {}).get("publicId"),
        )
        for m in content.get("matches", [])
    ]
    assert found == [
        (answer["entity"], m["score"], m["ruleDescription"], m["matchedText"], m["publicId"])
        for m in answer["matches"]
    ]


@pytest.mark.scenario("cadsr/credentialed")
@pytest.mark.tool("match_data_elements")
@pytest.mark.requirement("match_data_elements-1")
def test_each_entity_s_matches_are_named_for_it_in_the_order_given(tools, recorded):
    answers = [
        recorded(f"{CREDENTIALED}/{file}")["response"]["body"]["matchResults"]
        for file in ("cde-match.json", "cde-match-donor.json")
    ]
    entities = [{"name": answer["entity"]} for answer in answers]

    content = _ok(tools.call("match_data_elements", {"entities": entities}))

    found = [
        (m.get("entity"), (m.get("dataElement") or {}).get("publicId"))
        for m in content.get("matches", [])
    ]
    assert found == [(a["entity"], m["publicId"]) for a in answers for m in a["matches"]]


@pytest.mark.tool("match_data_elements")
@pytest.mark.requirement("match_data_elements-1")
@pytest.mark.parametrize(
    "arguments",
    [
        {"modelVariant": "default"},
        {"similarityThreshold": 0.5},
        {
            "entities": [{"name": "Patient Gender"}]
            * (TOOLS["match_data_elements"]["lists"]["entities"] + 1)
        },
    ],
    ids=["model-variant", "similarity-threshold", "too-many-entities"],
)
@pytest.mark.live_capable
def test_what_the_platform_does_not_take_is_an_invalid_request_never_ignored(tools, arguments):
    call = {"entities": [{"name": "Patient Gender"}]} | arguments

    result = tools.call("match_data_elements", call)

    assert error_code(result) == "invalid_request", result.content


@pytest.mark.scenario("cadsr/credentialed")
@pytest.mark.tool("match_data_elements")
@pytest.mark.requirement("match_data_elements-1")
def test_at_most_match_limit_matches_come_for_an_entity(tools, recorded):
    answer = recorded(f"{CREDENTIALED}/cde-match.json")["response"]["body"]["matchResults"]
    assert len(answer["matches"]) > 1

    content = _ok(
        tools.call(
            "match_data_elements", {"entities": [{"name": answer["entity"]}], "matchLimit": 1}
        )
    )

    found = [(m.get("dataElement") or {}).get("publicId") for m in content.get("matches", [])]
    assert found == [answer["matches"][0]["publicId"]]


TIMEOUTS = [
    pytest.param(
        name,
        arguments,
        id=name,
        marks=[pytest.mark.tool(name), pytest.mark.requirement(f"{name}-1")],
    )
    for name, arguments in [
        ("match_data_elements", {"entities": [{"name": "Patient Gender"}]}),
        ("match_value_meanings", {"values": ["Male"]}),
    ]
]


@pytest.mark.scenario("cadsr/match-timeout")
@pytest.mark.parametrize(("name", "arguments"), TIMEOUTS)
def test_matching_slower_than_the_match_timeout_is_a_timeout(tools, recorded, name, arguments):
    settings = recorded("scenarios/cadsr/match-timeout/settings.json")
    delays = {
        recorded(f"scenarios/cadsr/match-timeout/{file}")["response"]["delay_seconds"]
        for file in ("vm-match.json", "cde-match.json")
    }
    assert min(delays) > float(settings["NCI_SIP_MATCH_TIMEOUT_SECONDS"])

    result = tools.call(name, arguments)

    assert error_code(result) == "timeout", result.content


@pytest.mark.tool("match_value_meanings")
@pytest.mark.requirement("match_value_meanings-1")
def test_a_match_with_no_concept_has_none_never_null(tools, recorded):
    crafted = recorded("crafted/OP-M02/vm-match-no-concept.json")
    (value,) = crafted["request"]["body"]
    (match,) = crafted["response"]["body"]["matchResults"][0]["matches"]
    assert (match["concept"], match["evsSource"]) == (None, None)

    content = _ok(tools.call("match_value_meanings", {"values": [value["name"]]}))

    (found,) = content.get("matches", [])
    item = found.get("item") or {}
    assert item.get("publicId") == match["itemId"]
    assert [key for key in ("concept", "evsSource") if key in item] == []


@pytest.mark.scenario("cadsr/credentialed")
@pytest.mark.tool("list_contexts")
@pytest.mark.requirement("list_contexts-1")
def test_the_contexts_are_the_registry_s_context_names(tools, recorded):
    names = recorded(f"{CREDENTIALED}/context-names.json")["response"]["body"]["contextNames"]
    assert len(names) <= TOOLS["list_contexts"]["bounds"]["limit"]["default"]

    content = _ok(tools.call("list_contexts", {}))

    contexts = content.get("contexts", [])
    assert [context.get("name") for context in contexts] == names
    assert [(c.get("provenance") or {}).get("release") for c in contexts] == [
        {"registry": "cadsr"}
    ] * len(names)


# Crafted (PASS fixture only): caDSR publishes no registry release today.
PUBLISHED = "scenarios/cadsr/with-registry-release/registry-releases.json"


def _published(recorded):
    (release,) = [
        r for r in recorded(PUBLISHED)["response"]["body"]["registryReleases"] if r["latest"]
    ]
    return release


@pytest.mark.scenario("cadsr/with-registry-release")
@pytest.mark.tool("resolve_registry_release")
@pytest.mark.requirement("resolve_registry_release-1")
@pytest.mark.registry_release
def test_a_published_registry_release_is_returned(tools, recorded):
    release = _published(recorded)

    result = tools.call("resolve_registry_release", {})

    content = _ok(result)
    assert (content.get("published"), content.get("identifier")) == (True, release["identifier"])
    assert content.get("generatedAt") == release["generatedAt"]
    assert result.meta.get("ttlMs") == 0


@pytest.mark.scenario("cadsr/with-registry-release")
@pytest.mark.tool("get_data_element")
@pytest.mark.requirement("X-21")
@pytest.mark.registry_release
def test_a_published_registry_release_is_asked_for_and_named_with_its_date(
    tools, upstream, recorded
):
    release = _published(recorded)

    content = _ok(
        tools.call(
            "get_data_element", {"publicId": DATA_ELEMENT, "registryRelease": release["identifier"]}
        )
    )

    named = (content.get("provenance") or {}).get("release") or {}
    assert (named.get("registry"), named.get("identifier")) == ("cadsr", release["identifier"])
    assert str(named.get("date", "")).startswith(release["generatedAt"][:10])
    # Asked for, not only named: a content request carried the release.
    content_requests = [e for e in upstream.log() if "registry/releases" not in e["path"]]
    assert requests_naming(content_requests, [release["identifier"]])


@pytest.mark.scenario("cadsr/with-registry-release")
@pytest.mark.tool("get_data_element")
@pytest.mark.requirement("X-21")
def test_a_registry_release_cadsr_does_not_list_fails_closed_where_it_lists_some(tools, recorded):
    listed = [r["identifier"] for r in recorded(PUBLISHED)["response"]["body"]["registryReleases"]]
    unlisted = "2026.06.01"
    assert unlisted not in listed

    result = tools.call("get_data_element", {"publicId": DATA_ELEMENT, "registryRelease": unlisted})

    assert error_code(result) == "release_not_available", result.content


@pytest.mark.scenario("cadsr/with-registry-release")
@pytest.mark.tool("get_code_map")
@pytest.mark.requirement("X-17")
@pytest.mark.parametrize(
    "pinned_first", [True, False], ids=["pinned-then-not", "unpinned-then-pinned"]
)
@pytest.mark.registry_release
def test_a_cursor_keeps_the_registry_release_it_was_issued_with(
    tools, upstream, recorded, pinned_first
):
    pin = {"registryRelease": _published(recorded)["identifier"]}
    first_arguments = {"limit": 40} | (pin if pinned_first else {})
    first = _ok(tools.call("get_code_map", first_arguments))
    asked = requests_naming(
        [e for e in upstream.log() if "registry/releases" not in e["path"]],
        [pin["registryRelease"]],
    )
    assert bool(asked) is pinned_first
    cursor = first.get("nextCursor")
    assert cursor, "no nextCursor"

    presented = {"limit": 40, "cursor": cursor} | ({} if pinned_first else pin)
    result = tools.call("get_code_map", presented)

    assert error_code(result) == "invalid_request", result.content
