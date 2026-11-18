"""What the suite's tests read from a result and from the upstream request log."""

import json

import pytest

from nci_sip_acceptance.record import FIXTURES
from nci_sip_acceptance.results import (
    CARRIED,
    EXPORT_LISTING,
    bare_code,
    element_ids,
    error_code,
    export_date,
    hint_fits,
    identity,
    is_count,
    is_iso8601,
    is_name,
    is_timestamp,
    is_url,
    pinned_release,
    provenance_of,
    release_of,
    requests_naming,
    sparql_rows,
    value_ids,
    wrong_fields,
    wrong_origin,
)
from nci_sip_acceptance.spec import items_of
from nci_sip_acceptance.tools import Result

RECORD = {"code": "invalid_request", "message": "no such include value"}


@pytest.mark.parametrize(
    ("is_error", "content", "code"),
    [
        (True, {"error": RECORD}, "invalid_request"),
        # Content beside the record, a record that is no object, or no error flag: no code.
        (True, {"error": RECORD, "concept": {"code": "C4817"}}, None),
        (True, {"error": "invalid_request"}, None),
        (True, ["invalid_request"], None),
        (True, "Error executing tool", None),
        (False, {"error": RECORD}, None),
    ],
)
def test_an_error_result_has_a_code_only_as_one_error_record(is_error, content, code):
    assert error_code(Result("get_concept", is_error, content, {})) == code


def entry(path, **params):
    return {"path": path, "params": {name: [value] for name, value in params.items()}}


def test_a_request_names_a_code_in_its_path_or_a_parameter_but_not_inside_another_code():
    log = [
        entry("/api/v1/concept/ncit_26.09d", list="C4817,C12578", include="summary"),
        entry("/api/v1/concept/ncit_26.09d/C116977"),
        entry("/api/v1/concept/ncit_26.09d/C48170"),
        entry("/api/v1/metadata/terminologies", terminology="ncit"),
    ]

    assert requests_naming(log, ["C4817", "C116977"]) == log[:2]


ELEMENT = {"publicId": "2200604", "version": "4", "longName": "Person Sex Text Type"}


# The first rows are the shapes main compared before resource contents existed, and must not
# change.
@pytest.mark.parametrize(
    ("item", "expected"),
    [
        pytest.param(
            {"code": "C4817", "terminology": "ncit", "version": "26.09d", "name": "Ewing Sarcoma"},
            ("ncit", "C4817"),
            id="concept-record-with-version",
        ),
        pytest.param(
            {"code": "C4817", "terminology": "ncit", "provenance": {}},
            ("ncit", "C4817"),
            id="concept-item-with-provenance",
        ),
        pytest.param(
            {
                "sourceCode": "C4817",
                "targetCode": "C3262",
                "provenance": {"relationship": {"code": "R101"}},
            },
            ("C4817", "C3262", "R101"),
            id="edge",
        ),
        # Mappings were indistinct before this row: each had the identity None.
        pytest.param(
            {"targetCode": "HGNC:1100", "targetTerminology": "hgnc"},
            ("hgnc", "HGNC:1100"),
            id="mapping",
        ),
        pytest.param(ELEMENT, ("2200604", "4"), id="data-element"),
        pytest.param(
            {"dataElement": {"publicId": "2200604", "version": "4"}, "usedBy": ["GDC"]},
            ("2200604", "4"),
            id="data-element-use",
        ),
        pytest.param(
            {"dataElement": {"publicId": "2200604", "version": "4"}, "codeMap": {}},
            ("2200604", "4"),
            id="code-map",
        ),
        pytest.param("NCIP", "NCIP", id="context-as-string"),
        pytest.param({"name": "NCIP"}, "NCIP", id="context-as-record"),
        pytest.param(
            {"terminology": "mdr", "release": "29_0"}, ("mdr", None), id="terminology-record"
        ),
        pytest.param(
            {"terminology": "ncit", "version": "26.09d", "channel": "monthly"},
            ("ncit", "26.09d"),
            id="release-record",
        ),
        pytest.param(
            {"published": False, "generatedAt": "2026-07-01"},
            (False, None, "2026-07-01"),
            id="registry-state",
        ),
        # A data element that is no record names nothing to identify the item by but its name.
        pytest.param(
            {"dataElement": "2200604", "name": "a code map"},
            "a code map",
            id="data-element-that-is-no-record",
        ),
    ],
)
def test_an_item_is_identified_by_what_it_is(item, expected):
    assert identity(item) == expected


def test_two_concepts_of_one_release_are_two_items():
    release = {"terminology": "ncit", "version": "26.09d"}

    assert identity(release | {"code": "C4817"}) != identity(release | {"code": "C3262"})
    # A release's own record is another item than a concept of it.
    assert identity(release) != identity(release | {"code": "C4817"})


@pytest.mark.parametrize(
    ("release", "expected"),
    [
        ({"terminology": "ncit", "identifier": "26.09d"}, ("ncit", "26.09d")),
        ({"registry": "cadsr"}, ("cadsr", None)),
        ({"registry": "cadsr", "identifier": "2026.07.02"}, ("cadsr", "2026.07.02")),
        (None, (None, None)),
    ],
)
def test_an_item_s_release_is_its_terminology_or_registry_and_identifier(release, expected):
    item = {"provenance": {"release": release}} if release else {"provenance": None}

    assert release_of(item) == expected


def test_a_call_names_the_release_it_pins_or_for_cadsr_the_registry_alone():
    pinned = {"terminology": "ncit", "release": "26.09d"}

    assert pinned_release("get_concept", pinned) == ("ncit", "26.09d")
    assert pinned_release("get_data_element", pinned) == ("cadsr", None)
    # A cross-domain tool without a release names the NCIt release its content rests on.
    assert pinned_release("resolve_stored_value", pinned) == ("ncit", "26.09d")


def test_the_export_date_is_the_one_the_folder_listing_gives_the_export():
    listing = json.loads((FIXTURES / EXPORT_LISTING).read_text(encoding="utf-8"))
    # The listing dates releasedCDEsXML-OD.zip 2026-07-01 22:19 (cde-xml-listing.json).
    assert export_date(listing["response"]["body"]) == "2026-07-01"
    with pytest.raises(ValueError, match="unpack"):
        export_date("<a>README</a> 2026-07-01")


def test_a_recorded_sparql_answer_reads_as_rows_of_values_and_bare_codes():
    rows = sparql_rows(
        json.loads((FIXTURES / "recorded/ssis-sparql/values-c4817.json").read_text())
    )

    # The recording's first row (values-c4817.json), each variable by its value.
    assert set(rows[0]) == {"id", "version", "value", "concept"}
    assert {bare_code(row["concept"]) for row in rows} == {"C4817"}


def test_uses_are_named_by_their_data_element_and_value():
    use = {"dataElement": {"publicId": "2200604", "version": "4"}, "value": "Male"}

    assert element_ids([use]) == {"2200604"}
    assert value_ids([use | {"conceptCode": "C20197"}]) == {("2200604", "Male", "C20197")}


PROVENANCE = {
    "correlationId": "test-call",
    "release": {"terminology": "ncit", "identifier": "26.09d"},
    "source": "evs_rest",
    "servedBy": "live",
    "retrievedAt": "2026-10-04T10:00:00+00:00",
}


def test_a_provenance_record_lacking_a_carried_field_or_holding_one_off_its_set_is_named():
    assert wrong_fields(PROVENANCE, CARRIED) == []
    assert wrong_fields(PROVENANCE | {"source": "somewhere"}, CARRIED) == ["source"]
    assert wrong_fields({k: v for k, v in PROVENANCE.items() if k != "release"}, CARRIED) == [
        "release"
    ]


@pytest.mark.parametrize(
    ("item", "provenance"),
    [({"provenance": PROVENANCE}, PROVENANCE), ({"provenance": None}, {}), ("text", {})],
)
def test_an_item_s_provenance_is_its_record_or_none(item, provenance):
    assert provenance_of(item) == provenance


@pytest.mark.parametrize(
    ("value", "valid"),
    [
        ("2026-10-04T10:00:00+00:00", True),
        ("2026-10-04T10:00:00", False),
        ("today", False),
        (None, False),
    ],
)
def test_a_timestamp_is_iso_8601_with_a_time_zone(value, valid):
    assert is_timestamp(value) is valid


@pytest.mark.parametrize(
    ("value", "valid"),
    [("2026-10-04T10:00:00", True), ("2026-10-04", True), ("today", False), (None, False)],
)
def test_a_date_or_timestamp_without_a_time_zone_is_iso_8601(value, valid):
    assert is_iso8601(value) is valid


@pytest.mark.parametrize(
    ("value", "count", "name"),
    [
        (5, True, False),
        (0, False, False),
        (True, False, False),
        ("5", False, True),
        ("", False, False),
    ],
)
def test_a_count_is_a_positive_integer_and_a_name_a_non_empty_string(value, count, name):
    assert (is_count(value), is_name(value)) == (count, name)


@pytest.mark.parametrize(
    ("ttl", "scope", "pinned", "fits"),
    [
        # Release-pinned content may be cached for any positive time, long included.
        (86_400_000, "public", True, True),
        (1, "public", True, True),
        (0, "public", True, False),
        (86_400_000, "private", True, False),
        # Content no release pins is cached briefly, at most an hour.
        (3_600_000, "public", False, True),
        (3_600_001, "public", False, False),
        (0, "public", False, False),
        (None, "public", False, False),
        (3_600_000, "private", False, False),
    ],
)
def test_a_caching_hint_fits_the_class_of_what_the_content_holds(ttl, scope, pinned, fits):
    assert hint_fits(ttl, scope, pinned) is fits


LIVE = {
    "release": {"terminology": "ncit", "identifier": "26.09d"},
    "source": "evs_rest",
    "retrievedAt": "2026-10-11T00:00:00Z",
    "servedBy": "live",
    "correlationId": "call-1",
}


@pytest.mark.parametrize("correlation", [None, "", 5])
def test_every_carried_provenance_needs_a_nonempty_correlation_id(correlation):
    provenance = dict(LIVE, correlationId=correlation)
    if correlation is None:
        del provenance["correlationId"]

    assert wrong_fields(provenance, CARRIED) == ["correlationId"]
    assert wrong_fields(LIVE, CARRIED) == []


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("https://api-evsrest.nci.nih.gov/api/v1/concept/ncit_26.09d/C3262", True),
        ("http://example.invalid/x", True),
        ("https:///x", False),
        ("https://", False),
        ("//example.invalid/x", False),
        ("example.invalid/x", False),
        ("mailto:someone@example.invalid", False),
        ("ftp://example.invalid/x", False),
        ("file:///x", False),
        ("", False),
        (None, False),
    ],
)
def test_a_source_uri_is_an_absolute_http_url_with_a_host(value, expected):
    assert is_url(value) is expected


@pytest.mark.parametrize(
    ("upstream", "wrong"),
    [
        ({"terminology": "ncit", "version": "26.09d"}, []),
        ({}, ["upstream"]),
        ("ncit", ["upstream"]),
        (["ncit"], ["upstream"]),
    ],
)
def test_an_owed_origin_is_a_nonempty_object(upstream, wrong):
    provenance = dict(LIVE, sourceUri="https://example.invalid/x", upstream=upstream)

    assert wrong_origin(provenance, uri=True, origin=True) == wrong


def test_the_items_of_a_result_come_from_every_items_path():
    result = {"nodes": [{"code": "C2"}], "edges": [{"source": "C1", "target": "C2"}]}

    assert items_of("get_concept_neighborhood", result) == [
        {"code": "C2"},
        {"source": "C1", "target": "C2"},
    ]
