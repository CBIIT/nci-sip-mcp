"""The EVS tools' own requirements (spec/requirements.yaml), each test against its tool.

Fixture expectations come from recordings. Live content checks discover the current release
and assert stable contracts instead of comparing historical content.
"""

import json
import math
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import yaml

from nci_sip_acceptance.results import (
    CARRIED,
    error_code,
    is_name,
    provenance_of,
    release_of,
    requests_naming,
    wrong_fields,
    wrong_origin,
)
from nci_sip_acceptance.spec import RECORDS, TOOLS, items_of
from nci_sip_acceptance.suite import index_set

# Recorded in full: recorded/evs/concepts/C4817.json.
CONCEPT = "C4817"
CURRENT = "recorded/evs/concepts/C4817.json"
# Retired in the retired/with-replacement scenario: its concepts/C154421.json.
RETIRED = "scenarios/retired/with-replacement/concepts/C154421.json"
# Recorded with their summary sections, so a batch at the default include is answered; the
# fixture server answers a batch rotated by one (concepts.py), so request order is not its.
BATCH = ["C4817", "C12578", "C116977"]
# A code EVS does not know, which a batch leaves out (batch/silent-drop).
UNKNOWN = "C999999999"
SECTIONS = TOOLS["get_concept"]["values"]["include"]
# NCIt's semantic-type property, by its code (the concept record's semanticType).
SEMANTIC_TYPE = "P106"
ALWAYS = [name for name, field in RECORDS["concept"]["fields"].items() if not field.get("optional")]
# The concept record's fields that are not include sections: status and those always present.
BASE = [name for name in RECORDS["concept"]["fields"] if name not in SECTIONS]


def _release(tools, pinned, **arguments):
    return tools.call("resolve_release", {"terminology": pinned["terminology"], **arguments})


@pytest.mark.scenario("release/two-latest")
@pytest.mark.tool("resolve_release")
@pytest.mark.requirement("resolve_release-1")
@pytest.mark.parametrize("channel", ["monthly", "weekly"])
def test_a_channel_s_release_is_the_row_its_tag_names(tools, pinned, recorded, channel):
    # The scenario's answer to each channel's query names that channel's release; the weekly
    # row comes first wherever both are listed.
    (row,) = recorded(f"scenarios/release/two-latest/{channel}.json")["response"]["body"]

    result = _release(tools, pinned, channel=channel)

    assert not result.is_error, result.content
    assert (result.content.get("channel"), result.content.get("version")) == (
        channel,
        row["version"],
    )


@pytest.mark.scenario("release/duplicate-tag")
@pytest.mark.tool("resolve_release")
@pytest.mark.requirement("resolve_release-2")
def test_a_channel_whose_query_names_two_releases_fails_closed(tools, pinned, recorded):
    rows = recorded("scenarios/release/duplicate-tag/monthly.json")["response"]["body"]

    result = _release(tools, pinned, channel="monthly")

    assert error_code(result) == "release_not_available", result.content
    details = json.dumps(result.content["error"].get("details"))
    assert [row["version"] for row in rows if row["version"] not in details] == []


@pytest.mark.tool("resolve_release")
@pytest.mark.requirement("resolve_release-3")
@pytest.mark.live_capable
def test_a_resolved_release_is_never_cached(tools, pinned):
    result = _release(tools, pinned)

    assert not result.is_error, result.content
    assert result.meta.get("ttlMs") == 0


@pytest.mark.tool("get_concept")
@pytest.mark.requirement("get_concept-3", "X-1", "X-7")
@pytest.mark.live_capable
def test_a_discovered_release_identifies_the_concept_and_its_provenance(tools):
    # Two bounded tool calls; no historical fixture release is assumed in live mode.
    discovery = tools.call("resolve_release", {"terminology": "ncit", "channel": "monthly"})
    assert not discovery.is_error, discovery.content
    release = discovery.content
    assert isinstance(release, dict), release
    assert release.get("terminology") == "ncit"
    version = release.get("version")
    assert isinstance(version, str) and version.strip(), release

    assert wrong_fields(provenance_of(release), CARRIED) == []
    correlation = "acceptance-live-concept"
    result = tools.call(
        "get_concept",
        {"terminology": "ncit", "release": version, "code": CONCEPT},
        meta={"correlationId": correlation},
    )
    assert not result.is_error, result.content
    concept = result.content
    assert isinstance(concept, dict), concept
    assert (concept.get("code"), concept.get("terminology")) == (CONCEPT, "ncit")
    assert isinstance(concept.get("name"), str) and concept["name"].strip(), concept
    assert isinstance(concept.get("active"), bool), concept
    provenance = concept.get("provenance", {})
    assert isinstance(provenance, dict), provenance
    assert provenance.get("correlationId") == correlation, provenance
    assert wrong_origin(provenance, uri=True, origin=True) == [], provenance
    release_identity = provenance.get("release")
    assert isinstance(release_identity, dict), provenance
    assert release_identity.get("identifier") == version, provenance
    assert release_identity.get("terminology") == "ncit", provenance
    assert provenance.get("source") == "evs_rest", provenance
    assert provenance.get("servedBy") == "live", provenance
    assert urlsplit(provenance.get("sourceUri", "")).path.endswith(
        f"/concept/ncit_{version}/{CONCEPT}"
    ), provenance
    assert datetime.fromisoformat(provenance["retrievedAt"]).tzinfo is not None


def _concept(tools, pinned, code, **arguments):
    return tools.call("get_concept", {**pinned, "code": code, **arguments})


def _recorded_section(body, section):
    """A section as the concept record defines it, from EVS's recording of the concept."""

    if section == "semanticType":
        return [
            entry["value"] for entry in body["properties"] if entry.get("code") == SEMANTIC_TYPE
        ]
    return body[section]


@pytest.mark.tool("get_concept")
@pytest.mark.requirement("get_concept-1")
@pytest.mark.live_capable
@pytest.mark.parametrize("section", SECTIONS)
def test_an_include_value_returns_its_section_and_no_other(
    tools, content_pin, target, recorded, section
):
    result = _concept(tools, content_pin, CONCEPT, include=[section])

    assert not result.is_error, result.content
    # Any other key is another section, whether the record names it or not (parents, roles).
    assert set(result.content) - set(BASE) == {section}
    if target.mode == "fixture":
        body = recorded(CURRENT)["response"]["body"]
        assert result.content[section] == _recorded_section(body, section)
    else:
        entries = result.content[section]
        assert isinstance(entries, list)
        kind = str if section == "semanticType" else dict
        assert all(isinstance(entry, kind) for entry in entries)
        assert release_of(result.content) == ("ncit", content_pin["release"])


@pytest.mark.tool("get_concept")
@pytest.mark.requirement("get_concept-2")
@pytest.mark.live_capable
def test_descendants_is_no_include_value(tools, pinned):
    # A server whose input schema lists the include values refuses this in the SDK's argument
    # validation, as text with no error record; that fails here, since M3.2 asks for the record.
    result = _concept(tools, pinned, CONCEPT, include=["descendants"])

    assert error_code(result) == "invalid_request", result.content


@pytest.mark.tool("get_concept")
@pytest.mark.requirement("get_concept-3")
@pytest.mark.live_capable
@pytest.mark.parametrize(
    ("code", "recording"),
    [
        pytest.param(CONCEPT, CURRENT, id="current"),
        pytest.param(
            "C154421", RETIRED, id="retired", marks=pytest.mark.scenario("retired/with-replacement")
        ),
    ],
)
def test_a_concept_carries_its_identity_and_the_status_the_platform_publishes(
    tools, content_pin, target, recorded, code, recording
):
    result = _concept(tools, content_pin, code)

    assert not result.is_error, result.content
    assert [name for name in ALWAYS if name not in result.content] == []
    returned = [result.content[name] for name in ("code", "terminology", "name", "active")]
    if target.mode == "live":
        assert returned[:2] == [code, "ncit"]
        assert is_name(returned[2]) and isinstance(returned[3], bool)
        assert is_name(result.content.get("status"))
        assert release_of(result.content) == ("ncit", content_pin["release"])
        return
    body = recorded(recording)["response"]["body"]
    assert returned == [body[name] for name in ("code", "terminology", "name", "active")]
    # EVS publishes a status for every concept, as conceptStatus; the record passes it on.
    assert result.content.get("status") == body["conceptStatus"]


def _batch(tools, pinned, codes):
    return tools.call("get_concepts", {**pinned, "codes": codes})


# A server that answered the same batch before may serve it from its cache.
@pytest.mark.own_server
@pytest.mark.tool("get_concepts")
@pytest.mark.requirement("get_concepts-1")
def test_a_batch_is_one_upstream_request(tools, upstream, pinned):
    result = _batch(tools, pinned, BATCH)

    assert not result.is_error, result.content
    requests = requests_naming(upstream.log(), BATCH)
    assert len(requests) == 1, requests
    # One request for one code, the others from elsewhere, is no batch.
    assert [code for code in BATCH if not requests_naming(requests, [code])] == []


@pytest.mark.scenario("batch/silent-drop")
@pytest.mark.tool("get_concepts")
@pytest.mark.requirement("get_concepts-2")
def test_a_code_the_platform_leaves_out_of_a_batch_is_named_missing(tools, pinned):
    result = _batch(tools, pinned, [CONCEPT, UNKNOWN])

    assert not result.is_error, result.content
    codes = [concept.get("code") for concept in items_of("get_concepts", result.content)]
    assert (codes, result.content.get("missing")) == ([CONCEPT], [UNKNOWN])


@pytest.mark.tool("get_concepts")
@pytest.mark.requirement("get_concepts-3")
@pytest.mark.parametrize("order", [BATCH, BATCH[::-1]], ids=["as-listed", "reversed"])
@pytest.mark.live_capable
def test_a_batch_comes_back_in_request_order(tools, pinned, order):
    result = _batch(tools, pinned, order)

    assert not result.is_error, result.content
    assert [concept.get("code") for concept in items_of("get_concepts", result.content)] == order


@pytest.mark.tool("list_terminologies")
@pytest.mark.requirement("list_terminologies-1")
def test_every_terminology_the_platform_serves_is_listed_with_its_current_release(
    tools, pinned, recorded
):
    listing = recorded("recorded/evs/terminologies.json")["response"]["body"]

    result = tools.call("list_terminologies", {})

    assert not result.is_error, result.content
    listed = {
        item.get("terminology"): item.get("release")
        for item in items_of("list_terminologies", result.content)
    }
    assert {row["terminology"] for row in listing} - set(listed) == set()
    # NCIt's current release is the monthly one the fixture set is pinned to.
    assert listed[pinned["terminology"]] == pinned["release"]


# Lexical search is EVS's type=contains and typeahead its type=startsWith, each recorded a page
# of 10 at a time with highlights asked for (fixtures/manifest.yaml); EVS returns highlights for
# contains only. The lexical search is recorded for its first two pages.
LEXICAL = ["recorded/evs/search-contains.json", "recorded/evs/search-contains-page-2.json"]
TYPEAHEAD = "recorded/evs/search-starts-with.json"
# A field a record leaves out where the source says nothing: null or false is not absent.
ABSENT = "(absent)"


def _search(tools, pinned, recording, mode, **arguments):
    """The search a recording answers, by its term and page size, in `mode`."""

    params = recording["request"]["params"]
    query, (size,) = params["term"][0], params["pageSize"]
    search = {"query": query, "mode": mode, "limit": int(size)}
    return tools.call("search_concepts", {**pinned, **search, **arguments})


def _matches(result):
    """Each result's code, its matchedOn (ABSENT where it has none) and whether it carries a
    score."""

    assert not result.is_error, result.content
    return [
        (
            (entry.get("concept") or {}).get("code"),
            entry.get("matchedOn", ABSENT),
            "score" in entry,
        )
        for entry in result.content.get("results", [])
    ]


def _recorded_matches(recording):
    """What EVS answered, as the search result record passes it on: no score from EVS."""

    concepts = recording["response"]["body"].get("concepts", [])
    return [(concept["code"], concept.get("highlight", ABSENT), False) for concept in concepts]


@pytest.mark.tool("search_concepts")
@pytest.mark.requirement("search_concepts-1")
def test_lexical_search_returns_the_platform_s_matches_in_its_order_a_page_at_a_time(
    tools, pinned, recorded
):
    first, second = (recorded(file) for file in LEXICAL)

    page = _search(tools, pinned, first, "lexical")
    assert _matches(page) == _recorded_matches(first)
    assert page.content.get("totalKnown") == first["response"]["body"]["total"]
    following = _search(tools, pinned, first, "lexical", cursor=page.content.get("nextCursor"))

    assert _matches(following) == _recorded_matches(second)


@pytest.mark.tool("search_concepts")
@pytest.mark.requirement("search_concepts-2")
def test_typeahead_returns_the_platform_s_prefix_matches_in_its_order(tools, pinned, recorded):
    recording = recorded(TYPEAHEAD)

    result = _search(tools, pinned, recording, "typeahead")

    assert _matches(result) == _recorded_matches(recording)


# A lexical search whose first page holds retired concepts with the others, recorded as the
# platform answers it by default and, two pages, for retired concepts alone (conceptStatus).
RETIRED_SEARCH = "recorded/evs/search-retired-default.json"
RETIRED_ONLY = [
    "recorded/evs/search-retired-only.json",
    "recorded/evs/search-retired-only-page-2.json",
]
LISTING = "recorded/evs/terminologies.json"
# GO, whose listing names a retired status that is no concept status; searched by default.
GO_SEARCH = "recorded/evs/search-go-obsolete.json"
# Typeahead for retired concepts alone.
RETIRED_TYPEAHEAD = "recorded/evs/search-retired-typeahead.json"


def _listed(recorded, terminology, release=None):
    """A terminology's row in the recorded listing: the release given, or the first listed."""

    rows = recorded(LISTING)["response"]["body"]
    return next(
        row
        for row in rows
        if row["terminology"] == terminology and (release is None or row["version"] == release)
    )


def _selectable(row):
    """The retired status a search can select for a listing row, or None."""

    metadata = row.get("metadata", {})
    retired = metadata.get("retiredStatusValue")
    return retired if retired in metadata.get("conceptStatuses", []) else None


def _states(result):
    """Each result's concept by code, active and status."""

    assert not result.is_error, result.content
    concepts = [entry.get("concept") or {} for entry in result.content.get("results", [])]
    return [
        (concept.get("code"), concept.get("active"), concept.get("status")) for concept in concepts
    ]


def _recorded_states(recording):
    concepts = recording["response"]["body"].get("concepts", [])
    return [(concept["code"], concept["active"], concept["conceptStatus"]) for concept in concepts]


@pytest.mark.tool("search_concepts")
@pytest.mark.requirement("search_concepts-6")
@pytest.mark.parametrize("given", [{}, {"retired": "include"}], ids=["default", "include"])
def test_retired_concepts_are_returned_with_the_others_by_default(tools, pinned, recorded, given):
    recording = recorded(RETIRED_SEARCH)
    # The page holds a retired concept and an active one, or the case would show nothing.
    assert {active for _, active, _ in _recorded_states(recording)} == {True, False}

    result = _search(tools, pinned, recording, "lexical", **given)

    assert _states(result) == _recorded_states(recording)


@pytest.mark.tool("search_concepts")
@pytest.mark.requirement("search_concepts-6")
def test_retired_only_returns_the_retired_concepts_alone(tools, pinned, recorded):
    first, second = (recorded(file) for file in RETIRED_ONLY)
    retired = _selectable(_listed(recorded, pinned["terminology"], pinned["release"]))
    # The listing names a retired status among its concept statuses, and the platform is asked
    # by it: the recording's request says so.
    assert retired is not None
    assert first["request"]["params"]["conceptStatus"] == [retired]

    page = _search(tools, pinned, first, "lexical", retired="only")
    following = _search(
        tools, pinned, first, "lexical", retired="only", cursor=page.content.get("nextCursor")
    )

    states = [_states(page), _states(following)]
    assert states == [_recorded_states(first), _recorded_states(second)]
    assert {(active, status) for each in states for _, active, status in each} == {(False, retired)}
    assert page.content.get("totalKnown") == first["response"]["body"]["total"]


@pytest.mark.tool("search_concepts")
@pytest.mark.requirement("search_concepts-6")
def test_typeahead_takes_retired_only_as_lexical_search_does(tools, pinned, recorded):
    recording = recorded(RETIRED_TYPEAHEAD)
    retired = _selectable(_listed(recorded, pinned["terminology"], pinned["release"]))
    assert recording["request"]["params"]["conceptStatus"] == [retired]

    result = _search(tools, pinned, recording, "typeahead", retired="only")

    assert _states(result) == _recorded_states(recording)


@pytest.mark.tool("search_concepts")
@pytest.mark.requirement("search_concepts-6")
# exclude, which the platform cannot serve, and values a lenient server might take for one.
@pytest.mark.parametrize("value", ["exclude", "ONLY", ""])
@pytest.mark.live_capable
def test_a_retired_value_outside_the_two_is_invalid(tools, pinned, recorded, value):
    result = _search(tools, pinned, recorded(RETIRED_SEARCH), "lexical", retired=value)

    assert error_code(result) == "invalid_request", result.content


@pytest.mark.tool("search_concepts")
@pytest.mark.requirement("search_concepts-7")
def test_retired_only_where_the_status_is_none_the_search_selects_is_invalid(tools, recorded):
    recording = recorded(GO_SEARCH)
    row = _listed(recorded, "go")
    # GO names a retired status, but not one of its concept statuses.
    assert row["metadata"].get("retiredStatusValue") and _selectable(row) is None
    pin = {"terminology": row["terminology"], "release": row["version"]}

    # include given, as a caller may give the default, is served wherever search is.
    searched = _search(tools, pin, recording, "lexical", retired="include")
    only = _search(tools, pin, recording, "lexical", retired="only")

    # The terminology is searched by default; only the selection is refused.
    assert _states(searched) == _recorded_states(recording)
    assert error_code(only) == "invalid_request", only.content


@pytest.mark.tool("search_concepts")
@pytest.mark.requirement("search_concepts-7")
def test_retired_only_where_the_listing_names_no_retired_status_is_invalid(tools, recorded):
    row = _listed(recorded, "hgnc")
    assert "retiredStatusValue" not in row["metadata"]
    pin = {"terminology": row["terminology"], "release": row["version"]}

    result = tools.call(
        "search_concepts",
        {**pin, "query": "kinase", "mode": "lexical", "limit": 10, "retired": "only"},
    )

    assert error_code(result) == "invalid_request", result.content


# The interim index: what the operator's prepare command builds from the index set (the
# acceptance README), every concept the fixture set records with its summary.
INDEX_SET = frozenset(
    index_set(yaml.safe_load((Path(__file__).parent.parent / "fixtures/manifest.yaml").read_text()))
)
INDEX_MODES = ["semantic", "hybrid"]
FIELDS = {"name", "synonym", "definition"}
# Recorded at full, retired, in the index set.
RETIRED_INDEXED = "recorded/evs/concepts/C13111.json"


# The page the index searches ask for.
INDEX_LIMIT = 10


def _index_search(tools, pin, query, mode, **arguments):
    search = {"query": query, "mode": mode, "limit": INDEX_LIMIT}
    return tools.call("search_concepts", {**pin, **search, **arguments})


def _index_results(result):
    assert not result.is_error, result.content
    results = result.content.get("results", [])
    assert results, "no result"
    return results


def _index_violations(results, pinned):
    """What results of the index say against search_concepts-3, by code and check."""

    release = {"terminology": pinned["terminology"], "identifier": pinned["release"]}
    return [
        ((entry.get("concept") or {}).get("code"), check)
        for entry in results
        for check, held in _index_checks(entry, release).items()
        if not held
    ]


def _index_checks(entry, release):
    """A result of the index: its concept in the index set, a JSON number for its score, a field for
    its matchedOn, and provenance naming the index's release, source and service."""

    concept = entry.get("concept") or {}
    provenance = concept.get("provenance") or {}
    named = {key: (provenance.get("release") or {}).get(key) for key in release}
    score = entry.get("score")
    return {
        "outside the index set": concept.get("code") in INDEX_SET,
        "score": type(score) in (int, float) and math.isfinite(score),
        "matchedOn": entry.get("matchedOn") in FIELDS,
        "release": named == release,
        "source": provenance.get("source") == "evs_index",
        "servedBy": provenance.get("servedBy") == "index",
        "origin": wrong_origin(provenance, uri=False, origin=False) == [],
    }


@pytest.mark.prepared
@pytest.mark.tool("search_concepts")
@pytest.mark.requirement("search_concepts-3")
@pytest.mark.parametrize("mode", INDEX_MODES)
def test_index_search_returns_scored_indexed_concepts_and_the_named_one_first_page(
    tools, pinned, recorded, mode
):
    name = recorded(CURRENT)["response"]["body"]["name"]
    assert CONCEPT in INDEX_SET

    result = _index_search(tools, pinned, name, mode)

    results = _index_results(result)
    assert _index_violations(results, pinned) == []
    assert len(results) <= INDEX_LIMIT
    # Every indexed concept is ranked; the page bounds what is returned, no score threshold.
    assert result.content.get("totalKnown") == len(INDEX_SET)
    scores = [entry["score"] for entry in results]
    assert scores == sorted(scores, reverse=True)
    # A query equal to a concept's preferred name: that concept on the first page, matched on
    # its name.
    matched = {
        (entry.get("concept") or {}).get("code"): entry.get("matchedOn") for entry in results
    }
    assert matched.get(CONCEPT) == "name"


def _indexed_codes(result):
    return [(entry.get("concept") or {}).get("code") for entry in _index_results(result)]


@pytest.mark.prepared
@pytest.mark.tool("search_concepts")
@pytest.mark.requirement("X-17", "search_concepts-3")
@pytest.mark.parametrize("mode", INDEX_MODES)
def test_an_index_search_s_cursor_continues_with_its_next_ranked_items(
    tools, pinned, recorded, mode
):
    name = recorded(CURRENT)["response"]["body"]["name"]
    first = _index_search(tools, pinned, name, mode)
    cursor = first.content.get("nextCursor")
    assert cursor, "no nextCursor"

    second = _index_search(tools, pinned, name, mode, cursor=cursor)
    both = _index_search(tools, pinned, name, mode, limit=2 * INDEX_LIMIT)

    assert _indexed_codes(first) + _indexed_codes(second) == _indexed_codes(both)


@pytest.mark.tool("search_concepts")
@pytest.mark.requirement("search_concepts-4")
@pytest.mark.parametrize("mode", INDEX_MODES)
# On a server without an index too: the terminology decides before the index does (-8).
@pytest.mark.parametrize(
    "index",
    [
        pytest.param("as-prepared", marks=pytest.mark.live_capable),
        pytest.param("none", marks=pytest.mark.unprepared),
    ],
)
def test_an_index_mode_for_a_terminology_without_an_index_is_invalid(tools, recorded, mode, index):
    # GO: the index is NCIt's alone.
    row = _listed(recorded, "go")
    pin = {"terminology": row["terminology"], "release": row["version"]}

    result = _index_search(tools, pin, "obsolete", mode)

    assert error_code(result) == "invalid_request", result.content


@pytest.mark.prepared
@pytest.mark.tool("search_concepts")
@pytest.mark.requirement("search_concepts-5")
@pytest.mark.parametrize("mode", INDEX_MODES)
def test_an_index_search_for_another_release_fails_closed(tools, pinned, recorded, mode):
    rows = recorded(LISTING)["response"]["body"]
    other = next(
        row["version"]
        for row in rows
        if row["terminology"] == pinned["terminology"] and row["version"] != pinned["release"]
    )
    name = recorded(CURRENT)["response"]["body"]["name"]

    result = _index_search(tools, pinned | {"release": other}, name, mode)

    assert error_code(result) == "release_mismatch", result.content


@pytest.mark.prepared
@pytest.mark.tool("search_concepts")
@pytest.mark.requirement("search_concepts-6")
@pytest.mark.parametrize("mode", INDEX_MODES)
def test_index_search_returns_retired_concepts_with_the_others_or_alone(
    tools, pinned, recorded, mode
):
    body = recorded(RETIRED_INDEXED)["response"]["body"]
    retired = _selectable(_listed(recorded, pinned["terminology"], pinned["release"]))
    assert body["code"] in INDEX_SET and body["active"] is False

    included = _states(_index_search(tools, pinned, body["name"], mode))
    alone = _states(_index_search(tools, pinned, body["name"], mode, retired="only"))

    assert [code for code, _, _ in included + alone if code not in INDEX_SET] == []
    assert (body["code"], False, body["conceptStatus"]) in included
    assert (body["code"], False, body["conceptStatus"]) in alone
    assert {(active, status) for _, active, status in alone} == {(False, retired)}


@pytest.mark.tool("search_concepts")
@pytest.mark.requirement("search_concepts-6")
@pytest.mark.live_capable
@pytest.mark.parametrize("mode", INDEX_MODES)
@pytest.mark.parametrize("value", ["exclude", "ONLY", ""])
def test_a_retired_value_outside_the_two_is_invalid_in_index_modes(tools, pinned, mode, value):
    result = _index_search(tools, pinned, "Ewing Sarcoma", mode, retired=value)

    assert error_code(result) == "invalid_request", result.content


@pytest.mark.unprepared
@pytest.mark.tool("search_concepts")
@pytest.mark.requirement("search_concepts-8")
@pytest.mark.parametrize("mode", INDEX_MODES)
def test_an_index_mode_without_an_index_is_unavailable(tools, pinned, recorded, mode):
    name = recorded(CURRENT)["response"]["body"]["name"]

    result = _index_search(tools, pinned, name, mode)

    assert error_code(result) == "capability_unavailable", result.content


# Value set C85492 (CDISC SDTM Method Terminology), recorded whole: FHIR $expand ignores count,
# offset and activeOnly (fixtures/manifest.yaml).
VALUE_SET = "C85492"
EXPANSION = "recorded/evs-fhir/expand-c85492.json"
COUNT = 10


def _expand(tools, pinned, **arguments):
    return tools.call("expand_value_set", {**pinned, "valueSet": VALUE_SET, **arguments})


def _members(result):
    """Each member's code, name and inactive mark (ABSENT where it has none), and the total."""

    assert not result.is_error, result.content
    members = items_of("expand_value_set", result.content)
    found = [
        (member.get("code"), member.get("name"), member.get("inactive", ABSENT))
        for member in members
    ]
    return found, result.content.get("total")


def _recorded_members(contains):
    return [
        (member["code"], member["display"], member.get("inactive", ABSENT)) for member in contains
    ]


@pytest.mark.tool("expand_value_set")
@pytest.mark.requirement("expand_value_set-1")
@pytest.mark.parametrize("page", ["first", "middle", "last"])
def test_count_and_offset_select_the_members_and_total_counts_them_all(
    tools, pinned, recorded, page
):
    expansion = recorded(EXPANSION)["response"]["body"]["expansion"]
    contains = expansion["contains"]
    # The last page holds fewer members than COUNT.
    offset = {"first": 0, "middle": 100, "last": len(contains) - 4}[page]

    result = _expand(tools, pinned, count=COUNT, offset=offset)

    assert _members(result) == (
        _recorded_members(contains[offset : offset + COUNT]),
        expansion["total"],
    )


INACTIVE = "scenarios/valueset/inactive-members/expand.json"


@pytest.mark.scenario("valueset/inactive-members")
@pytest.mark.tool("expand_value_set")
@pytest.mark.requirement("expand_value_set-2")
@pytest.mark.parametrize(
    "active_only",
    [{"activeOnly": True}, {"activeOnly": False}, {}],
    ids=["true", "false", "default"],
)
def test_active_only_leaves_out_the_members_marked_inactive(tools, pinned, recorded, active_only):
    contains = recorded(INACTIVE)["response"]["body"]["expansion"]["contains"]
    inactive = [member for member in contains if member.get("inactive")]
    # The scenario marks members on the first page, or the test would show nothing.
    assert inactive and all(member in contains[:COUNT] for member in inactive)
    left_out = inactive if active_only.get("activeOnly") else []
    kept = [member for member in contains if member not in left_out]

    result = _expand(tools, pinned, count=COUNT, offset=0, **active_only)

    assert _members(result) == (_recorded_members(kept[:COUNT]), len(kept))


# Traversal. Each tool's defaults and maxima are its bounds in spec/tools.yaml; polarity's
# exclusion sets are the traversal record's.
HIERARCHY, NEIGHBORHOOD = "get_concept_hierarchy", "get_concept_neighborhood"
EXCLUSIONS = RECORDS["traversal"]["fields"]["polarity"]["exclusions"]
# traversal/deep-fanout: a root with 1,001 children, the first heading a chain deeper than the
# depth maximum.
FANOUT = "scenarios/traversal/deep-fanout/concepts"
FANOUT_ROOT, CHAIN_HEAD = "C99000000", "C99000001"
# traversal/starvation: two hubs over the same targets, one with 300 roles and 2 associations,
# the other the other way round.
HUBS = [
    f"scenarios/traversal/starvation/concepts/{code}.json" for code in ("C99200000", "C99200400")
]
# traversal/exclusions: C4817 with a role of every exclusion code, those named as positive ones,
# and two positive roles named as exclusions, in its recording and the catalogue alike.
EXCLUDED = "scenarios/traversal/exclusions/concepts/C4817.json"
# The paths from C4817 to the root, as EVS gives them.
PATHS = "recorded/evs/paths-to-root.json"
# Two steps over roles: far enough to show whether a negative edge's target is followed.
TWO_STEPS = 2


def _maximum(tool, argument):
    return TOOLS[tool]["bounds"][argument]["maximum"]


def _traverse(tools, pinned, tool, code, **arguments):
    result = tools.call(tool, {**pinned, "code": code, **arguments})
    assert not result.is_error, result.content
    return result


def _depth(item):
    return (item.get("provenance") or {}).get("depth")


def _relationship(edge):
    return (edge.get("provenance") or {}).get("relationship") or {}


def _polarity(edge):
    return (edge.get("provenance") or {}).get("polarity")


def _codes_of(items):
    return [item.get("code") for item in items]


def _unmarked(nodes):
    """The nodes without the status the node record requires; EVS publishes one for every
    concept, as conceptStatus."""

    return [
        node.get("code")
        for node in nodes
        if type(node.get("active")) is not bool or not node.get("status")
    ]


@pytest.mark.scenario("traversal/deep-fanout")
@pytest.mark.tool(HIERARCHY)
@pytest.mark.requirement("get_concept_hierarchy-1")
def test_a_depth_above_the_maximum_is_applied_as_the_maximum_and_reported(tools, pinned):
    maximum = _maximum(HIERARCHY, "depth")

    result = _traverse(tools, pinned, HIERARCHY, CHAIN_HEAD, direction="child", depth=maximum + 2)

    # The chain runs deeper than the maximum, so the walk reaches it and no further.
    nodes = result.content.get("nodes", [])
    depths = {_depth(node) for node in nodes}
    assert maximum in depths
    assert [depth for depth in depths if not isinstance(depth, int) or depth > maximum] == []
    truncation = result.content.get("truncation") or {}
    assert (truncation.get("bound"), truncation.get("limit")) == ("depth", maximum)
    assert _unmarked(nodes) == []


@pytest.mark.tool(HIERARCHY)
@pytest.mark.requirement("get_concept_hierarchy-2")
@pytest.mark.live_capable
def test_paths_to_root_are_the_platform_s_paths_in_its_order(tools, content_pin, target, recorded):
    result = _traverse(tools, content_pin, HIERARCHY, CONCEPT, direction="pathsToRoot")
    paths = result.content.get("paths")
    if target.mode == "fixture":
        assert paths == [_codes_of(path) for path in recorded(PATHS)["response"]["body"]]
    else:
        assert isinstance(paths, list) and paths
        assert all(isinstance(path, list) and path and path[0] == CONCEPT for path in paths)
    # Each concept on the paths once among the nodes, the one asked about not among them.
    reached = {code for path in paths for code in path} - {CONCEPT}
    nodes = result.content.get("nodes", [])
    assert sorted(_codes_of(nodes)) == sorted(reached)
    assert _unmarked(nodes) == []
    assert all(release_of(node) == ("ncit", content_pin["release"]) for node in nodes)


@pytest.mark.tool(HIERARCHY)
@pytest.mark.requirement("get_concept_hierarchy-3")
def test_limit_is_a_page_the_cursor_continues_to_the_end(tools, pinned, recorded):
    children = _codes_of(recorded(CURRENT)["response"]["body"]["children"])
    limit = len(children) // 2 + 1

    first = _traverse(tools, pinned, HIERARCHY, CONCEPT, direction="child", limit=limit)
    cursor = first.content.get("nextCursor")
    second = _traverse(
        tools, pinned, HIERARCHY, CONCEPT, direction="child", limit=limit, cursor=cursor
    )

    pages = [_codes_of(page.content.get("nodes", [])) for page in (first, second)]
    assert pages == [children[:limit], children[limit:]]
    assert "nextCursor" not in second.content


# The kinds a starvation hub has, by the relation list each comes from.
LISTS = {"role": "roles", "association": "associations"}


@pytest.mark.scenario("traversal/starvation")
@pytest.mark.tool(NEIGHBORHOOD)
@pytest.mark.requirement("get_concept_neighborhood-1")
@pytest.mark.parametrize("hub", HUBS, ids=["roles", "associations"])
@pytest.mark.parametrize("kinds", [list(LISTS), list(LISTS)[::-1]], ids=["in-order", "reversed"])
def test_a_kind_that_reaches_its_budget_starves_no_other(tools, pinned, recorded, hub, kinds):
    code, sizes, large = _hub(recorded, hub)
    # Fewer nodes than the large kind has, room enough for the small one.
    budget = sizes[large] // 2
    assert min(sizes.values()) < budget // len(LISTS)

    result = _traverse(tools, pinned, NEIGHBORHOOD, code, depth=1, kinds=kinds, maxNodes=budget)

    assert len(result.content.get("nodes", [])) <= budget
    kinds_found = {_relationship(edge).get("kind") for edge in result.content.get("edges", [])}
    assert kinds_found == set(LISTS)
    truncation = result.content.get("truncation") or {}
    assert truncation.get("occurred") is True
    per_kind = truncation.get("perKind") or {}
    assert {kind for kind, record in per_kind.items() if record.get("occurred")} == {large}


def _hub(recorded, hub):
    """A starvation hub's code, the size of each kind it has, and its larger kind."""

    body = recorded(hub)["response"]["body"]
    sizes = {kind: len(body[key]) for kind, key in LISTS.items()}
    return body["code"], sizes, max(sizes, key=sizes.__getitem__)


@pytest.mark.scenario("traversal/starvation")
@pytest.mark.tool(NEIGHBORHOOD)
@pytest.mark.requirement("get_concept_neighborhood-6")
@pytest.mark.parametrize("hub", HUBS, ids=["roles", "associations"])
def test_budget_per_kind_bounds_the_nodes_each_kind_adds(tools, pinned, recorded, hub):
    code, sizes, large = _hub(recorded, hub)
    # Below the large kind's size, room enough for the small one.
    budget = sizes[large] // 2
    assert min(sizes.values()) <= budget

    result = _traverse(
        tools,
        pinned,
        NEIGHBORHOOD,
        code,
        depth=1,
        kinds=list(LISTS),
        budgetPerKind=budget,
        maxNodes=_maximum(NEIGHBORHOOD, "maxNodes"),
        maxEdges=_maximum(NEIGHBORHOOD, "maxEdges"),
    )

    targets = {kind: set() for kind in LISTS}
    for edge in result.content.get("edges", []):
        targets.setdefault(_relationship(edge).get("kind"), set()).add(edge.get("targetCode"))
    assert [kind for kind in LISTS if not targets[kind]] == []
    # The hubs' kinds share targets: those only the large kind reaches are the nodes it added.
    small = set().union(*(found for kind, found in targets.items() if kind != large))
    assert len(targets[large] - small) <= budget
    nodes = set(_codes_of(result.content.get("nodes", [])))
    assert len(nodes - small - {code}) <= budget
    truncation = result.content.get("truncation") or {}
    assert truncation.get("occurred") is True
    per_kind = truncation.get("perKind") or {}
    assert {kind for kind, record in per_kind.items() if record.get("occurred")} == {large}
    assert (per_kind[large].get("bound"), per_kind[large].get("limit")) == ("kind_budget", budget)


TRAVERSALS = [
    pytest.param(name, arguments, id=name, marks=pytest.mark.tool(name))
    for name, arguments in [(NEIGHBORHOOD, {"depth": 1}), (HIERARCHY, {"direction": "child"})]
]


@pytest.mark.scenario("upstream/unavailable")
@pytest.mark.requirement("get_concept_neighborhood-5")
@pytest.mark.parametrize(("name", "arguments"), TRAVERSALS)
def test_the_attempts_a_failed_call_reports_are_the_requests_it_made(
    tools, upstream, pinned, name, arguments
):
    before = len(upstream.log())

    result = tools.call(name, {**pinned, "code": CONCEPT, **arguments})

    made = len(upstream.log()) - before
    assert error_code(result) in {"upstream_unavailable", "timeout"}, result.content
    details = result.content["error"].get("details") or {}
    # A failure that made no request would show nothing of how retries are counted.
    assert made
    assert details.get("attempts") == made


def _negative(code, terminology="ncit"):
    return code in EXCLUSIONS[terminology]


def _outward(result):
    """The edges from the concept asked about, C4817."""

    return [edge for edge in result.content.get("edges", []) if edge.get("sourceCode") == CONCEPT]


@pytest.mark.scenario("traversal/exclusions")
@pytest.mark.tool(NEIGHBORHOOD)
@pytest.mark.requirement("get_concept_neighborhood-2")
def test_polarity_follows_the_relationship_code_not_its_name(tools, pinned, recorded):
    roles = recorded(EXCLUDED)["response"]["body"]["roles"]
    expected = {
        (role["code"], role["relatedCode"]): "negative" if _negative(role["code"]) else "positive"
        for role in roles
    }
    # The scenario holds a role of every code in the set, so that leaving one out shows.
    assert set(EXCLUSIONS["ncit"]) <= {code for code, _ in expected}

    result = _traverse(tools, pinned, NEIGHBORHOOD, CONCEPT, depth=1, kinds=["role"])

    found = {
        (_relationship(edge).get("code"), edge.get("targetCode")): _polarity(edge)
        for edge in _outward(result)
    }
    assert found == expected


def _role_targets(recorded, codes):
    """The concepts the roles of the recorded concepts `codes` name."""

    return {
        role["relatedCode"]
        for code in codes
        for role in recorded(f"recorded/evs/concepts/{code}.json")["response"]["body"].get(
            "roles", []
        )
    }


def _two_steps(recorded):
    """C4817's negative role targets; the concepts only those reach at the second step; and
    those the positive ones reach there."""

    roles = recorded(CURRENT)["response"]["body"]["roles"]
    negative = {role["relatedCode"] for role in roles if _negative(role["code"])}
    positive = {role["relatedCode"] for role in roles if not _negative(role["code"])}
    past_positive = _role_targets(recorded, positive - negative)
    beyond = _role_targets(recorded, negative - positive) - negative - positive - past_positive
    return negative, beyond - {CONCEPT}, past_positive - {CONCEPT}


@pytest.mark.tool(NEIGHBORHOOD)
@pytest.mark.requirement("get_concept_neighborhood-3")
@pytest.mark.parametrize("include", [{}, {"includeNegative": True}], ids=["default", "included"])
def test_negative_edges_are_returned_marked_and_followed_only_when_included(
    tools, pinned, recorded, include
):
    negative, beyond, past_positive = _two_steps(recorded)
    assert beyond

    result = _traverse(
        tools,
        pinned,
        NEIGHBORHOOD,
        CONCEPT,
        depth=TWO_STEPS,
        kinds=["role"],
        maxNodes=_maximum(NEIGHBORHOOD, "maxNodes"),
        maxEdges=_maximum(NEIGHBORHOOD, "maxEdges"),
        **include,
    )

    marked = {edge.get("targetCode") for edge in _outward(result) if _polarity(edge) == "negative"}
    assert marked == negative
    reached = set(_codes_of(result.content.get("nodes", [])))
    assert past_positive - reached == set()
    assert reached & beyond == (beyond if include else set())


@pytest.mark.scenario("traversal/deep-fanout")
@pytest.mark.tool(NEIGHBORHOOD)
@pytest.mark.requirement("get_concept_neighborhood-4")
def test_a_node_limit_above_the_maximum_is_applied_as_the_maximum(tools, pinned, recorded):
    maximum = _maximum(NEIGHBORHOOD, "maxNodes")
    children = recorded(f"{FANOUT}/{FANOUT_ROOT}.json")["response"]["body"]["children"]
    assert len(children) + 1 > maximum

    result = _traverse(
        tools, pinned, NEIGHBORHOOD, FANOUT_ROOT, depth=1, kinds=["child"], maxNodes=maximum * 2
    )

    nodes = result.content.get("nodes", [])
    assert len(nodes) <= maximum
    truncation = result.content.get("truncation") or {}
    assert (truncation.get("bound"), truncation.get("limit")) == ("nodes", maximum)
    assert truncation.get("omitted", 0) >= 1
    assert _unmarked(nodes) == []


@pytest.mark.tool("get_concept_subsets")
@pytest.mark.requirement("get_concept_subsets-1")
def test_the_subsets_are_the_concept_s_subset_associations_in_order(tools, pinned, recorded):
    body = recorded(CURRENT)["response"]["body"]
    expected = [
        (association["relatedCode"], body["terminology"], association["relatedName"])
        for association in body["associations"]
        if association["type"] == "Concept_In_Subset"
    ]

    result = _traverse(tools, pinned, "get_concept_subsets", CONCEPT)

    subsets = items_of("get_concept_subsets", result.content)
    found = [
        (subset.get("code"), subset.get("terminology"), subset.get("name")) for subset in subsets
    ]
    assert found == expected


def _maps(result):
    """Each mapping without its provenance: the platform's map, field for field, a field the
    map lacks absent rather than null."""

    mappings = items_of("get_concept_mappings", result.content)
    return [{key: value for key, value in m.items() if key != "provenance"} for m in mappings]


@pytest.mark.tool("get_concept_mappings")
@pytest.mark.requirement("get_concept_mappings-1")
@pytest.mark.live_capable
def test_the_mappings_are_the_concept_s_maps_unchanged_in_order(
    tools, content_pin, target, recorded
):
    result = _traverse(tools, content_pin, "get_concept_mappings", CONCEPT)
    if target.mode == "fixture":
        assert _maps(result) == recorded(CURRENT)["response"]["body"]["maps"]
    else:
        mappings = result.content.get("mappings")
        assert isinstance(mappings, list)
        for mapping in mappings:
            assert all(
                is_name(mapping.get(key))
                for key in ("targetCode", "targetTerminology", "targetName", "type")
            )
            assert release_of(mapping) == ("ncit", content_pin["release"])


@pytest.mark.tool("get_concept_mappings")
@pytest.mark.requirement("get_concept_mappings-2")
def test_target_terminology_keeps_the_maps_with_that_target_and_no_other(tools, pinned, recorded):
    maps = recorded(CURRENT)["response"]["body"]["maps"]
    # The target the fewest maps name, so that the filter leaves some out.
    target = min(
        {m["targetTerminology"] for m in maps}, key=[m["targetTerminology"] for m in maps].count
    )
    kept = [m for m in maps if m["targetTerminology"] == target]
    assert 0 < len(kept) < len(maps)

    other_case = target.swapcase()
    assert other_case not in {m["targetTerminology"] for m in maps}

    mappings = _maps(
        _traverse(tools, pinned, "get_concept_mappings", CONCEPT, targetTerminology=target)
    )
    by_other_case = _maps(
        _traverse(tools, pinned, "get_concept_mappings", CONCEPT, targetTerminology=other_case)
    )

    assert mappings == kept
    # The platform's name exactly, case included.
    assert by_other_case == []


# Retired codes, EVS's history of each, and whether it names a replacement: C154421 does (its
# scenario), C13111 does not (recorded).
RETIRED_CODES = [
    pytest.param(
        RETIRED,
        "scenarios/retired/with-replacement/replacement.json",
        True,
        id="replaced",
        marks=pytest.mark.scenario("retired/with-replacement"),
    ),
    pytest.param(
        "recorded/evs/concepts/C13111.json",
        "recorded/evs/replacement-retired.json",
        False,
        id="unreplaced",
    ),
]


def _replacement(entry):
    release = (entry.get("provenance") or {}).get("release") or {}
    pin = (release.get("terminology"), release.get("identifier"))
    event = {key: entry[key] for key in ("action", "date") if key in entry}
    return entry.get("code"), entry.get("terminology"), entry.get("name"), pin, event


@pytest.mark.tool("resolve_retired_code")
@pytest.mark.requirement("resolve_retired_code-1")
@pytest.mark.parametrize(("concept", "history", "named"), RETIRED_CODES)
def test_a_retired_code_is_inactive_with_its_status_and_replacements(
    tools, pinned, recorded, concept, history, named
):
    body = recorded(concept)["response"]["body"]
    # Each replacement by code, terminology and name, with the release of its provenance.
    pin = (pinned["terminology"], pinned["release"])
    replacements = [
        (
            entry["replacementCode"],
            body["terminology"],
            entry["replacementName"],
            pin,
            {key: entry[key] for key in ("action", "date") if key in entry},
        )
        for entry in recorded(history)["response"]["body"]
        if "replacementCode" in entry
    ]
    assert body["active"] is False
    assert bool(replacements) is named

    result = _traverse(tools, pinned, "resolve_retired_code", body["code"])

    content = result.content
    assert (content.get("code"), content.get("terminology")) == (body["code"], body["terminology"])
    assert (content.get("active"), content.get("status")) == (False, body["conceptStatus"])
    # An empty list where the platform names none: present, never absent or null.
    found = content.get("replacements")
    assert isinstance(found, list), found
    assert [_replacement(entry) for entry in found] == replacements


@pytest.mark.tool("resolve_retired_code")
@pytest.mark.requirement("resolve_retired_code-2")
def test_an_active_code_is_active_with_its_status_and_no_replacement(tools, pinned, recorded):
    for file in (CURRENT, "recorded/evs/concepts/C65159.json"):
        body = recorded(file)["response"]["body"]
        result = _traverse(tools, pinned, "resolve_retired_code", body["code"])
        content = result.content
        assert (content.get("code"), content.get("terminology")) == (
            body["code"],
            body["terminology"],
        )
        assert (content.get("active"), content.get("status")) == (
            body["active"],
            body["conceptStatus"],
        )
        assert content.get("replacements") == []


@pytest.mark.tool("resolve_retired_code")
@pytest.mark.requirement("resolve_retired_code-1")
def test_a_merge_replacement_preserves_its_own_action_and_date(tools, pinned, recorded):
    (row,) = recorded("recorded/evs/replacement-merge-c15368.json")["response"]["body"]
    result = _traverse(tools, pinned, "resolve_retired_code", row["code"])
    assert not result.is_error, result.content
    (replacement,) = result.content["replacements"]
    assert _replacement(replacement) == (
        row["replacementCode"],
        pinned["terminology"],
        row["replacementName"],
        (pinned["terminology"], pinned["release"]),
        {key: row[key] for key in ("action", "date")},
    )


# The release's relationship catalogue, roles and associations.
CATALOGUE = {"role": "recorded/evs/roles.json", "association": "recorded/evs/associations.json"}


def _relationships(tools, pinned):
    result = tools.call("list_relationships", pinned)
    assert not result.is_error, result.content
    listed = result.content.get("relationships")
    assert isinstance(listed, list), result.content
    return listed


@pytest.mark.tool("list_relationships")
@pytest.mark.requirement("list_relationships-1")
@pytest.mark.live_capable
def test_every_relationship_of_the_catalogue_is_listed_by_code_name_and_kind(
    tools, content_pin, target, recorded
):
    listed = _relationships(tools, content_pin)
    if target.mode == "live":
        for item in listed:
            assert is_name(item.get("code")) and is_name(item.get("name"))
            assert item.get("kind") in ("role", "association")
            assert item.get("polarity") in ("positive", "negative")
            assert item.get("terminology") == "ncit"
            assert release_of(item) == ("ncit", content_pin["release"])
        return
    expected = sorted(
        (
            (entry["code"], entry["name"], kind)
            for kind, file in CATALOGUE.items()
            for entry in recorded(file)["response"]["body"]
        ),
        key=str,
    )

    found = [(item.get("code"), item.get("name"), item.get("kind")) for item in listed]
    assert sorted(found, key=str) == expected


@pytest.mark.scenario("traversal/exclusions")
@pytest.mark.tool("list_relationships")
@pytest.mark.requirement("list_relationships-2")
def test_a_relationship_s_polarity_follows_its_code_not_its_name(tools, pinned, recorded):
    roles = recorded("scenarios/traversal/exclusions/roles.json")["response"]["body"]
    associations = recorded(CATALOGUE["association"])["response"]["body"]
    expected = sorted(
        (
            (entry["code"], "negative" if _negative(entry["code"]) else "positive")
            for entry in [*roles, *associations]
        ),
        key=str,
    )

    listed = _relationships(tools, pinned)

    found = [(item.get("code"), item.get("polarity")) for item in listed]
    assert sorted(found, key=str) == expected


# relationships/exclusion-missing: the role catalogue lacks a code of the exclusion set.
INCOMPLETE = "scenarios/relationships/exclusion-missing/roles.json"


@pytest.mark.scenario("relationships/exclusion-missing")
@pytest.mark.requirement("list_relationships-3")
@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        pytest.param(
            "list_relationships",
            {},
            id="list_relationships",
            marks=pytest.mark.tool("list_relationships"),
        ),
        pytest.param(
            NEIGHBORHOOD,
            {"code": CONCEPT, "depth": 1, "kinds": ["role"]},
            id=NEIGHBORHOOD,
            marks=pytest.mark.tool(NEIGHBORHOOD),
        ),
    ],
)
def test_a_catalogue_without_a_code_of_the_exclusion_set_fails_closed(
    tools, pinned, recorded, tool, arguments
):
    catalogue = {entry["code"] for entry in recorded(INCOMPLETE)["response"]["body"]}
    absent = set(EXCLUSIONS["ncit"]) - catalogue
    assert absent

    result = tools.call(tool, {**pinned, **arguments})

    assert error_code(result) == "internal_error", result.content
    details = result.content["error"].get("details")
    assert isinstance(details, dict), details
    named = set(re.findall(r"\w+", json.dumps(details)))
    # The absent codes, and none of the set the catalogue holds.
    assert (absent - named, named & (set(EXCLUSIONS["ncit"]) - absent)) == (set(), set())
