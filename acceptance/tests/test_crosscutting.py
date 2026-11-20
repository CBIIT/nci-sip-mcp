"""Cross-cutting tests (the X requirements of spec/requirements.yaml), each against every
content-returning tool whose call is in calls.yaml."""

import base64
import json
import re
from collections import Counter
from pathlib import Path
from urllib.parse import parse_qs, unquote, unquote_plus

import pytest
import yaml
from jsonschema import Draft202012Validator

from nci_sip_acceptance.client import CREDENTIAL_VARIABLES
from nci_sip_acceptance.craft import LICENCE_FIELD
from nci_sip_acceptance.fixture_server import FORM_TYPE
from nci_sip_acceptance.results import (
    CARRIED,
    UNPINNED_REGISTRY,
    error_code,
    hint_fits,
    identity,
    is_timestamp,
    pinned_release,
    provenance_of,
    release_of,
    wrong_fields,
    wrong_origin,
)
from nci_sip_acceptance.spec import (
    TOOLS,
    alternatives,
    defaults,
    items_at,
    items_of,
    parameters,
)

CALLS = yaml.safe_load((Path(__file__).parent / "calls.yaml").read_text(encoding="utf-8"))
# What an item reached by traversal adds to the provenance every item carries (X-7).
REACHED = ("relationship", "direction", "polarity")
# The bare form NCIt publishes its codes in (A1.2: C4817); other terminologies are held only to
# carry no URI, since some publish punctuation in their codes (HGNC:3508, ICD-O-3 8001/3).
BARE = {"ncit": re.compile(r"[A-Z][0-9]+")}


def _per_tool(names, scenario=None, *, live=()):
    """One case per tool, counted for that tool in the report, run with `scenario` or else
    with the scenario its call needs; `live` names eligible ordinary calls."""

    return [
        pytest.param(
            name,
            id=name,
            marks=[
                pytest.mark.tool(name),
                *([pytest.mark.live_capable] if name in live and not _scenarios(name) else []),
                *[
                    pytest.mark.scenario(each)
                    for each in ([scenario] if scenario else _scenarios(name))
                ],
            ],
        )
        for name in names
    ]


def _scenarios(name):
    return [CALLS[name]["scenario"]] if "scenario" in CALLS[name] else []


PINNED_TOOLS = [name for name in CALLS if "release" in parameters(name)[0]]
# The calls that also carry an upstream origin (X-8), match nothing (X-4), exceed a bound
# (X-10), or are larger than their page (X-17).
UPSTREAM, EMPTY, TRUNCATING, PAGED = (
    [n for n in CALLS if key in CALLS[n]] for key in ("upstream", "empty", "truncating", "paged")
)
CALLED = _per_tool(CALLS)
PINNED = _per_tool(PINNED_TOOLS)
# Successful calls whose historical explicit pin still addresses the requested upstream.
# FHIR/GDC are unpinned; keyword search needs the crafted OP-C03 capability.
LIVE_CONTENT = set(CALLS) - {"expand_value_set", "resolve_stored_value", "search_data_elements"}
LIVE_CALLED = _per_tool(CALLS, live=LIVE_CONTENT)
LIVE_PINNED = _per_tool(PINNED_TOOLS, live=LIVE_CONTENT)
# The release the release/unknown scenario answers 404 for on every content path.
UNKNOWN_RELEASE = "99.99z"
# The calls whose answers name the release they come from, where a mismatch can show (X-3).
RELEASED = [name for name in PINNED_TOOLS if not CALLS[name].get("unversioned")]
# A concept of a licensed terminology, served (license/restricted) only with the licence key.
LICENSED = {"terminology": "mdr", "release": "29_0", "code": "10000000"}


def _call(tools, pinned, name, arguments=None, meta=None):
    """The tool's call in calls.yaml, or with `arguments` in place of the call's, pinned to the
    fixture set's release where it takes one."""

    taken = parameters(name)[0]
    release = {key: value for key, value in pinned.items() if key in taken}
    return tools.call(
        name, release | (CALLS[name]["arguments"] if arguments is None else arguments), meta=meta
    )


def _items(tools, pinned, name):
    """The items of the pinned call of `name`, which must succeed."""

    return _successful_items(name, _call(tools, pinned, name))


def _codes(item):
    """Each code an item carries, with the terminology beside it."""

    if not isinstance(item, dict):
        return []
    coded = [key for key in item if key == "code" or key.endswith("Code")]
    return [(item[key], item.get(_beside(key))) for key in coded]


def _beside(key):
    """Where the terminology of a code is: `code` with `terminology`, `targetCode` with
    `targetTerminology`."""

    return "terminology" if key == "code" else key.removesuffix("Code") + "Terminology"


def _bare(code, terminology):
    code = str(code)
    pattern = BARE.get(str(terminology).lower())
    uri = "://" in code or code.lower().startswith("urn:")
    return not uri and (pattern is None or pattern.fullmatch(code) is not None)


@pytest.mark.requirement("X-1")
@pytest.mark.parametrize("name", LIVE_PINNED)
def test_every_item_carries_the_release_requested(tools, pinned, name):
    found = _items(tools, pinned, name)

    assert {release_of(item) for item in found} == {(pinned["terminology"], pinned["release"])}


@pytest.mark.requirement("X-6")
@pytest.mark.parametrize("name", LIVE_CALLED)
def test_a_result_validates_against_the_declared_output_schema(tools, pinned, name):
    result = _call(tools, pinned, name)

    assert not result.is_error, result.content
    assert _schema_errors(tools, result) == []


@pytest.mark.requirement("X-14")
@pytest.mark.parametrize("name", _per_tool(CALLS, live=CALLS))
def test_a_result_is_an_object(tools, pinned, name):
    assert isinstance(_call(tools, pinned, name).content, dict)


@pytest.mark.requirement("X-7")
@pytest.mark.parametrize("name", LIVE_CALLED)
def test_every_item_carries_its_provenance(tools, pinned, name):
    original = _checked_provenances(_call(tools, pinned, name), name)
    correlation = "acceptance-provenance-repeat"
    repeated = _checked_provenances(
        _call(tools, pinned, name, meta={"correlationId": correlation}), name
    )
    assert [each["correlationId"] for each in repeated] == [correlation] * len(repeated)
    # Same pinned fixture call: a cached live answer must keep its original source facts.
    assert _origins(repeated) == _origins(original)


def _checked_provenances(result, name):
    assert not result.is_error, result.content
    found = []
    for path in TOOLS[name]["items"]:
        for item in items_at(result.content, path):
            provenance = provenance_of(item)
            assert wrong_fields(provenance, CARRIED) == []
            assert is_timestamp(provenance.get("retrievedAt"))
            assert (
                wrong_origin(
                    provenance, uri=True, origin=path not in CALLS[name].get("originless", [])
                )
                == []
            )
            found.append(provenance)
    assert found, "the call must return items to test their provenance"
    return found


def _origins(provenances):
    return [(each.get("sourceUri"), each.get("upstream")) for each in provenances]


@pytest.mark.requirement("X-7")
@pytest.mark.parametrize(
    "name", _per_tool((name for name in CALLS if TOOLS[name].get("traversal")), live=LIVE_CONTENT)
)
def test_an_item_reached_by_traversal_says_how(tools, pinned, name):
    provenances = [provenance_of(item) for item in _items(tools, pinned, name)]
    depths = [provenance.get("depth") for provenance in provenances]

    assert all(isinstance(depth, int) and depth >= 0 for depth in depths)
    assert max(depths) > 0, "the call reaches no item by traversal"
    reached = [provenance for provenance in provenances if provenance["depth"] > 0]
    assert [wrong_fields(provenance, REACHED) for provenance in reached] == [[]] * len(reached)


@pytest.mark.requirement("X-9")
@pytest.mark.parametrize("name", LIVE_CALLED)
def test_codes_are_bare_with_their_terminology_beside_them(tools, pinned, name):
    # A1.2 is about the codes items carry, in `code` or a field such as `targetCode`; an item
    # without one, such as an edge, has none to check.
    codes = [pair for item in _items(tools, pinned, name) for pair in _codes(item)]

    assert [code for code, terminology in codes if not _bare(code, terminology)] == []
    assert [code for code, terminology in codes if not terminology] == []


# A server that answered the same call before may serve it from its cache, asking nothing.
@pytest.mark.own_server
@pytest.mark.requirement("X-11")
@pytest.mark.parametrize("name", CALLED)
def test_no_upstream_request_is_repeated_within_a_call(tools, upstream, pinned, name):
    _call(tools, pinned, name)

    requests = Counter(
        json.dumps([entry[key] for key in ("surface", "method", "path", "params", "body")])
        for entry in upstream.log()
    )
    assert requests, "the call made no upstream request"
    assert [request for request, count in requests.items() if count > 1] == []


@pytest.mark.requirement("X-13")
@pytest.mark.parametrize("name", LIVE_PINNED)
def test_a_release_pinned_result_may_be_cached(tools, pinned, name):
    result = _call(tools, pinned, name)

    assert not result.is_error
    assert result.meta.get("ttlMs", 0) > 0
    # Governed content; the results computed from caller-supplied values are caDSR's (M2.3).
    assert result.meta.get("cacheScope") == "public"


# The caDSR calls: each takes a registry release, and none gives one (X-21).
REGISTRY = [name for name in CALLS if "registryRelease" in parameters(name)[0]]
# A registry release caDSR does not publish: it publishes none (registry-releases.json: 404).
UNPUBLISHED = "2026.07.02"
NOT_FOUND = 404


@pytest.mark.requirement("X-21")
@pytest.mark.parametrize("name", _per_tool(REGISTRY, live=LIVE_CONTENT))
def test_a_cadsr_item_without_a_registry_release_names_the_registry_alone(tools, pinned, name):
    provenances = [provenance_of(item) for item in _items(tools, pinned, name)]
    # An item of a tool that also takes the NCIt release names the registry beside it (A1.5),
    # where it rests on caDSR content; a caDSR tool's items name it as their release.
    states = (
        [provenance["registry"] for provenance in provenances if "registry" in provenance]
        if name in PINNED_TOOLS
        else [provenance.get("release") for provenance in provenances]
    )

    assert states
    assert states == [UNPINNED_REGISTRY] * len(states)


@pytest.mark.requirement("X-21")
@pytest.mark.parametrize("name", _per_tool(REGISTRY))
def test_a_registry_release_cadsr_does_not_publish_fails_closed(tools, pinned, recorded, name):
    assert recorded("recorded/cadsr/registry-releases.json")["response"]["status"] == NOT_FOUND

    result = _call(tools, pinned, name, CALLS[name]["arguments"] | {"registryRelease": UNPUBLISHED})

    assert error_code(result) == "release_not_available", result.content


@pytest.mark.requirement("X-13")
@pytest.mark.parametrize("name", _per_tool(REGISTRY, live=LIVE_CONTENT))
def test_a_cadsr_result_is_cached_as_what_it_holds_says(tools, pinned, name):
    result = _call(tools, pinned, name)

    assert not result.is_error, result.content
    ttl, scope = result.meta.get("ttlMs"), result.meta.get("cacheScope")
    if TOOLS[name].get("computed"):
        assert (ttl, scope) == (0, "private")
    else:
        assert hint_fits(ttl, scope, pinned=False), (ttl, scope)


def _schema_errors(tools, result):
    """How the result departs from the outputSchema its tool declares; no schema is one way."""

    schema = tools.available[result.tool].output_schema
    if schema is None:
        return ["the tool declares no outputSchema"]
    return [error.message for error in Draft202012Validator(schema).iter_errors(result.content)]


@pytest.mark.requirement("X-2")
@pytest.mark.parametrize("name", _per_tool(PINNED_TOOLS, "release/unknown"))
def test_a_release_the_platform_does_not_serve_fails_closed(tools, pinned, name):
    result = _call(tools, pinned | {"release": UNKNOWN_RELEASE}, name)

    # A tool that can only verify the release of an unpinned answer reports the mismatch.
    unpinned = {"release_mismatch"} if CALLS[name].get("unpinned") else set()
    assert error_code(result) in {"release_not_available", *unpinned}, result.content


@pytest.mark.requirement("X-3")
@pytest.mark.parametrize("name", _per_tool(RELEASED, "release/mismatch"))
def test_content_of_another_release_fails_closed(tools, pinned, name):
    arguments = CALLS[name]["arguments"]
    if name == "find_data_elements_for_concept":
        arguments = arguments | {"expandDescendants": True}
    result = _call(tools, pinned, name, arguments)

    assert error_code(result) == "release_mismatch", result.content


@pytest.mark.requirement("X-5")
@pytest.mark.parametrize("name", _per_tool(CALLS, "upstream/unavailable"))
def test_an_unavailable_platform_is_an_upstream_error(tools, pinned, name):
    result = _call(tools, pinned, name)

    assert error_code(result) in {"upstream_unavailable", "timeout"}, result.content


@pytest.mark.requirement("X-6")
@pytest.mark.parametrize("name", _per_tool(CALLS, "upstream/unavailable"))
def test_an_error_validates_against_the_declared_output_schema(tools, pinned, name):
    result = _call(tools, pinned, name)

    assert result.is_error, result.content
    assert _schema_errors(tools, result) == []


def _licence_keys(upstream):
    settings = upstream.fixtures.settings_of(("license/restricted",))
    return {settings[name] for name in CREDENTIAL_VARIABLES if name in settings}


def _outputs(result):
    """All a result says: its content, its _meta and every text block."""

    return result.content, result.meta, result.texts


def _carrying(keys, *outputs):
    """The keys that appear in any of `outputs`."""

    text = json.dumps(outputs, default=str)
    return sorted(key for key in keys if key in text)


@pytest.mark.scenario("license/restricted")
@pytest.mark.tool("get_concept")
@pytest.mark.requirement("X-12")
def test_the_licence_key_reaches_the_platform_and_nothing_the_server_returns_or_logs(
    tools, upstream
):
    granted = tools.call("get_concept", LICENSED)

    # Only a request that carries the key is answered.
    assert not granted.is_error, granted.content
    keys = _licence_keys(upstream)
    assert keys
    assert _carrying(keys, *_outputs(granted), tools.process.written()) == []


@pytest.mark.scenario("license/restricted")
@pytest.mark.unmatched_upstream
@pytest.mark.tool("get_concept")
@pytest.mark.requirement("X-12")
def test_an_error_carries_no_licence_key(tools, upstream):
    # No fixture answers this code, and the fixture server's refusal repeats the request's
    # headers: a server that passes the refusal on would pass the key on with it.
    refused = tools.call("get_concept", LICENSED | {"code": "99999999"})

    assert refused.is_error
    keys = _licence_keys(upstream)
    assert _carrying(keys, *_outputs(refused), tools.process.written()) == []


def _basic(credential):
    """The Authorization header a caDSR credential, user:password, is sent as."""

    return "Basic " + base64.b64encode(credential.encode()).decode()


@pytest.mark.requirement("X-12")
@pytest.mark.parametrize("name", _per_tool(["match_data_elements"], "cadsr/credentialed"))
def test_the_cadsr_credential_reaches_the_platform_and_nothing_the_server_returns_or_logs(
    tools, upstream, pinned, name
):
    settings = upstream.fixtures.settings_of(("cadsr/credentialed",))
    credential = settings["NCI_SIP_CADSR_CREDENTIAL"]

    granted = _call(tools, pinned, name)

    # Only a request that carries the credential is answered.
    assert not granted.is_error, granted.content
    sent = {_header(entry, "authorization") for entry in upstream.log()} - {None}
    assert sent == {_basic(credential)}
    secrets = {credential, _basic(credential), _basic(credential).removeprefix("Basic ")}
    assert _carrying(secrets, *_outputs(granted), tools.process.written()) == []


def _header(entry, name):
    return {key.lower(): value for key, value in entry["headers"].items()}.get(name)


# A call of each surface the rate-limited scenario answers with 429: EVS's release query and
# caDSR's data element.
RATE_LIMITED_CALLS = {"resolve_release": {}, "get_data_element": {"publicId": "2200604"}}
RATE_LIMITED = [
    pytest.param(
        name, id=name, marks=[pytest.mark.tool(name), pytest.mark.scenario("upstream/rate-limited")]
    )
    for name in RATE_LIMITED_CALLS
]


def _reached(upstream, scenario, log):
    """The one fixture of `scenario` that the requests in `log` reached."""

    reached = {entry["fixture"] for entry in log}
    (fixture,) = [
        each for each in upstream.fixtures.scenarios[scenario].values() if each.name in reached
    ]
    return fixture


@pytest.mark.requirement("X-16")
@pytest.mark.parametrize("name", RATE_LIMITED)
def test_a_rate_limited_request_is_asked_once_more_after_the_wait(tools, upstream, pinned, name):
    taken = parameters(name)[0]
    terminology = {key: value for key, value in pinned.items() if key in taken - {"release"}}

    result = tools.call(name, terminology | RATE_LIMITED_CALLS[name])

    assert not result.is_error, result.content
    # A server may resolve the release while it starts.
    log = [*tools.process.startup, *upstream.log()]
    limited = _reached(upstream, "upstream/rate-limited", log)
    wait = float(limited.responses[0].headers["Retry-After"])
    asked = [entry["received_at"] for entry in log if entry["fixture"] == limited.name]
    assert len(asked) == len(limited.responses)
    assert asked[1] - asked[0] >= wait


# Every surface that answers a concept's data elements fails inside what looks like an answer:
# the Shared SI façade and the caDSR API with apiResponse type E in an HTTP 200
# (upstream/masked-error), SPARQL with the inspection layer's HTML 403 (ssis/query-rejected).
# C17357's data elements are recorded on each surface (fixtures/README.md).
MASKING = ("upstream/masked-error", "ssis/query-rejected")
MASKED_CALLS = {"find_data_elements_for_concept": {"conceptCode": "C17357"}}
MASKED = [
    pytest.param(name, id=name, marks=[pytest.mark.tool(name), pytest.mark.scenario(*MASKING)])
    for name in MASKED_CALLS
]


@pytest.mark.requirement("X-15")
@pytest.mark.parametrize("name", MASKED)
def test_a_failure_every_surface_masks_as_an_answer_is_an_upstream_error(
    tools, upstream, recorded, pinned, name
):
    for surface in ("ssis", "cadsr"):
        masked = recorded(f"scenarios/upstream/masked-error/{surface}.json")["response"]
        assert (masked["status"], masked["body"]["apiResponse"]["type"]) == (200, "E")
    refused = recorded("scenarios/ssis/query-rejected/sparql.json")["response"]
    assert (refused["status"], refused["body"].startswith("<!DOCTYPE HTML")) == (403, True)

    result = tools.call(name, MASKED_CALLS[name] | pinned)

    assert error_code(result) == "upstream_unavailable", result.content
    # The error comes from the masked answers: the server asked at least one surface.
    masking = {
        fixture.name for each in MASKING for fixture in upstream.fixtures.scenarios[each].values()
    }
    assert masking & {entry["fixture"] for entry in upstream.log()}


@pytest.mark.requirement("X-4")
@pytest.mark.parametrize("name", _per_tool(EMPTY))
def test_a_query_that_matches_nothing_is_an_empty_result_with_provenance(tools, pinned, name):
    result = _call(tools, pinned, name, CALLS[name]["empty"])

    assert not result.is_error, result.content
    assert items_of(name, result.content) == []
    assert "nextCursor" not in result.content
    # With no item to carry it, the result carries the provenance itself.
    provenance = provenance_of(result.content)
    assert wrong_fields(provenance, CARRIED) == []
    assert wrong_origin(provenance, uri=True, origin=CALLS[name].get("emptyOrigin", False)) == []
    assert release_of(result.content) == pinned_release(name, pinned)
    if name in REGISTRY:
        assert provenance["release"] == UNPINNED_REGISTRY


@pytest.mark.requirement("X-8")
@pytest.mark.parametrize("name", _per_tool(UPSTREAM))
def test_what_the_platform_says_of_an_item_s_origin_is_passed_through(tools, pinned, name):
    supplied = _pinned(CALLS[name]["upstream"], pinned)
    passed = [provenance_of(item).get("upstream") or {} for item in _items(tools, pinned, name)]

    assert [{key: each.get(key) for key in supplied} for each in passed] == [supplied] * len(passed)


def _pinned(fields, pinned):
    """`fields` with `$terminology` and `$release` replaced by the fixture set's pin."""

    return {
        key: pinned[value[1:]] if str(value).startswith("$") else value
        for key, value in fields.items()
    }


def _whole(value):
    """Whether `value` is a whole number, and not a boolean."""

    return type(value) is int


@pytest.mark.requirement("X-10")
@pytest.mark.parametrize(
    "name",
    [
        case
        for name in TRUNCATING
        for case in _per_tool(
            [name], "ssis/distinct-cap" if name == "find_data_elements_for_concept" else None
        )
    ],
)
def test_a_bound_reached_is_reported_with_how_much_was_left_out(tools, pinned, name):
    truncating = CALLS[name]["truncating"]
    result = _call(tools, pinned, name, CALLS[name]["arguments"] | truncating["arguments"])

    assert not result.is_error, result.content
    record = result.content.get("truncation") or {} if isinstance(result.content, dict) else {}
    # The bound's value: the one argument that sets it, or, for the platform's own cap, as given.
    (limit,) = [truncating["limit"]] if "limit" in truncating else truncating["arguments"].values()
    assert record.get("occurred") is True, record
    assert record.get("bound") == truncating["bound"]
    assert record.get("limit") == limit
    assert _whole(record.get("reached")) and record["reached"] <= limit
    # How much was left out is a number, exact or a stated lower bound; a flag is not enough.
    assert _whole(record.get("omitted")) and record["omitted"] >= 1
    assert isinstance(record.get("exact"), bool)


def _paged(entry_filter=lambda entry: True):
    """Each paged call, by tool and position: one case each, marked for its tool and, where the
    call searches the interim index, as needing the prepare step."""

    return [
        (name, index, entry)
        for name in PAGED
        for index, entry in enumerate(CALLS[name]["paged"])
        if entry_filter(entry)
    ]


def _marks(name, entry, *, live=False):
    """A paged case's marks: its tool, the scenario its call needs, and the prepare step where
    the call searches the interim index."""

    prepared = [pytest.mark.prepared] if entry.get("prepared") else []
    scenarios = [
        pytest.mark.scenario(each)
        for each in ([entry["scenario"]] if "scenario" in entry else _scenarios(name))
    ]
    capable = [pytest.mark.live_capable] if live and not scenarios and not prepared else []
    return [pytest.mark.tool(name), *scenarios, *prepared, *capable]


PAGES = [
    pytest.param(
        name, entry, id=f"{name}-{index}", marks=_marks(name, entry, live=name in LIVE_CONTENT)
    )
    for name, index, entry in _paged()
]


@pytest.mark.requirement("X-17")
@pytest.mark.parametrize(("name", "entry"), PAGES)
def test_a_cursor_continues_with_the_next_items_of_the_same_release(
    tools, pinned, recorded, name, entry
):
    arguments, first = _first_page(tools, pinned, name, entry)

    continued = arguments | {"cursor": first.content["nextCursor"]}
    second = _call(tools, pinned, name, continued)

    assert not second.is_error, second.content
    before = {identity(item) for item in items_of(name, first.content)}
    after = [identity(item) for item in items_of(name, second.content)]
    assert after, "the cursor's page is empty"
    assert [item for item in after if item in before] == []
    releases = {release_of(item) for item in items_of(name, second.content)}
    assert releases == {pinned_release(name, pinned)}
    if "release" in parameters(name)[0]:
        other = _call(
            tools, pinned | {"release": _other_release(recorded, pinned)}, name, continued
        )
        assert error_code(other) == "invalid_request", other.content


def _first_page(tools, pinned, name, entry, extra=None):
    """A paged call's arguments, with `extra` added, and its first page, which carries a
    cursor."""

    arguments = CALLS[name]["arguments"] | entry["arguments"] | (extra or {})
    first = _call(tools, pinned, name, arguments)
    assert not first.is_error, first.content
    cursor = first.content.get("nextCursor")
    assert isinstance(cursor, str) and cursor, f"no nextCursor: {first.content!r:.300}"
    return arguments, first


CHANGED = [
    pytest.param(
        name,
        entry,
        change,
        id=f"{name}-{index}-{next(iter(change))}",
        marks=_marks(name, entry, live=name in LIVE_CONTENT),
    )
    for name, index, entry in _paged()
    for change in entry.get("changed", [])
]


@pytest.mark.requirement("X-17")
@pytest.mark.parametrize(("name", "entry", "change"), CHANGED)
def test_a_cursor_with_another_argument_is_an_invalid_request(tools, pinned, name, entry, change):
    arguments, first = _first_page(tools, pinned, name, entry)

    result = _call(
        tools, pinned, name, arguments | change | {"cursor": first.content["nextCursor"]}
    )

    assert error_code(result) == "invalid_request", result.content


def _left_out(name, arguments):
    """The optional arguments a call leaves out that have a stated default, as defaults."""

    return {key: value for key, value in defaults(name).items() if key not in arguments}


def _pages(name, *results):
    """Each result's items by identity, every result asserted a success."""

    assert [result.content for result in results if result.is_error] == []
    return [[identity(item) for item in items_of(name, result.content)] for result in results]


# The paged calls that leave out an argument with a stated default, which the cursor's
# comparison of defaults needs (M6.1).
DEFAULTED_PAGES = [
    page
    for page in PAGES
    if _left_out(page.values[0], CALLS[page.values[0]]["arguments"] | page.values[1]["arguments"])
]


@pytest.mark.requirement("X-17")
@pytest.mark.parametrize(("name", "entry"), DEFAULTED_PAGES)
def test_a_cursor_with_a_left_out_argument_given_as_its_default_continues(
    tools, pinned, name, entry
):
    arguments, first = _first_page(tools, pinned, name, entry)
    given = _left_out(name, arguments)
    # The first call leaves out an argument with a stated default, or the case shows nothing.
    assert given
    continued = arguments | {"cursor": first.content["nextCursor"]}

    plain = _call(tools, pinned, name, continued)
    with_defaults = _call(tools, pinned, name, continued | given)

    pages = _pages(name, plain, with_defaults)
    assert pages[1] == pages[0]


@pytest.mark.requirement("X-17")
@pytest.mark.parametrize(("name", "entry"), DEFAULTED_PAGES)
def test_a_cursor_with_a_given_default_left_out_continues(tools, pinned, name, entry):
    given = _left_out(name, CALLS[name]["arguments"] | entry["arguments"])
    assert given
    arguments, explicit = _first_page(tools, pinned, name, entry, given)
    cursor = {"cursor": explicit.content["nextCursor"]}
    without = {key: value for key, value in arguments.items() if key not in given}

    repeated = _call(tools, pinned, name, arguments | cursor)
    left_out = _call(tools, pinned, name, without | cursor)

    pages = _pages(name, repeated, left_out)
    assert pages[1] == pages[0]


# The paged calls' arguments that differ from their stated default: left out at the cursor,
# each applies as its default, so the cursor is presented with another argument.
NON_DEFAULT = [
    pytest.param(
        name,
        entry,
        key,
        id=f"{name}-{index}-{key}",
        marks=_marks(name, entry, live=name in LIVE_CONTENT),
    )
    for name, index, entry in _paged()
    for key, value in (CALLS[name]["arguments"] | entry["arguments"]).items()
    if key in defaults(name) and defaults(name)[key] != value
]


@pytest.mark.requirement("X-17")
@pytest.mark.parametrize(("name", "entry", "key"), NON_DEFAULT)
def test_a_cursor_without_an_argument_the_first_call_gave_is_an_invalid_request(
    tools, pinned, name, entry, key
):
    arguments, first = _first_page(tools, pinned, name, entry)
    presented = {k: v for k, v in arguments.items() if k != key}

    result = _call(tools, pinned, name, presented | {"cursor": first.content["nextCursor"]})

    assert error_code(result) == "invalid_request", result.content


def _as_stated(name, arguments):
    """A call without the arguments that give their stated default, and with every stated
    default given; the arguments that differ from their default are in both."""

    stated = defaults(name)
    kept = {
        key: value for key, value in arguments.items() if key not in stated or stated[key] != value
    }
    return kept, stated | kept


def _defaulted():
    """Each tool's base call, and its further calls that a wrong default would change."""

    cases = []
    for name in (name for name in CALLS if defaults(name)):
        calls = [{"arguments": CALLS[name]["arguments"]}, *CALLS[name].get("defaulted", [])]
        for index, call in enumerate(calls):
            marks = _marks(name, call, live=name in LIVE_CONTENT)
            cases.append(pytest.param(name, call["arguments"], id=f"{name}-{index}", marks=marks))
    return cases


def _outcome(name, result):
    """A successful result's items by identity, and its truncation where the tool reports one."""

    assert not result.is_error, result.content
    items = [identity(item) for item in items_of(name, result.content)]
    return items, result.content.get("truncation")


@pytest.mark.requirement("X-20")
@pytest.mark.parametrize(("name", "arguments"), _defaulted())
def test_a_left_out_argument_is_its_stated_default(tools, pinned, name, arguments):
    left_out, given = _as_stated(name, arguments)
    # The calls differ by at least one stated default, or the case shows nothing.
    assert given != left_out

    implicit = _call(tools, pinned, name, left_out)
    explicit = _call(tools, pinned, name, given)

    assert _outcome(name, implicit) == _outcome(name, explicit)


def _other_release(recorded, pinned):
    """A release of the pinned terminology that the platform serves beside the pinned one."""

    listing = recorded("recorded/evs/terminologies.json")["response"]["body"]
    return next(
        row["version"]
        for row in listing
        if row["terminology"] == pinned["terminology"] and row["version"] != pinned["release"]
    )


BOUNDED = [
    pytest.param(name, argument, id=f"{name}-{argument}", marks=_marks(name, {}, live=True))
    for name in CALLS
    for argument in TOOLS[name].get("bounds", {})
]


@pytest.mark.requirement("X-18")
@pytest.mark.parametrize(("name", "argument"), BOUNDED)
@pytest.mark.parametrize("value", [0, -1])
def test_a_bounded_argument_below_one_is_an_invalid_request(tools, pinned, name, argument, value):
    result = _call(tools, pinned, name, CALLS[name]["arguments"] | {argument: value})

    assert error_code(result) == "invalid_request", result.content


@pytest.mark.requirement("X-22")
@pytest.mark.parametrize("name", PINNED)
def test_a_call_without_release_names_the_resolved_ncit_release(tools, pinned, name):
    unpinned = {key: value for key, value in pinned.items() if key != "release"}
    result = _call(tools, unpinned, name)

    assert not result.is_error, result.content
    items = items_of(name, result.content) or [result.content]
    assert {release_of(item) for item in items} == {pinned_release(name, pinned)}
    assert (result.meta.get("ttlMs"), result.meta.get("cacheScope")) == (0, "private")


# The licensed concept as each tool that returns concept items asks for it, and the licensed
# concepts its items are: the concept, its one child, or both (license/restricted and
# license/attributed). A hierarchy holds the concepts reached, the child.
CHILD = "10000001"
LICENSED_CALLS = {
    "get_concept": ({"code": LICENSED["code"]}, {LICENSED["code"]}),
    "get_concepts": ({"codes": [LICENSED["code"], CHILD]}, {LICENSED["code"], CHILD}),
    "search_concepts": (
        {"query": "placeholder licensed", "mode": "lexical"},
        {LICENSED["code"], CHILD},
    ),
    "get_concept_hierarchy": ({"code": LICENSED["code"], "direction": "child"}, {CHILD}),
}


def _licensed_cases(scenario):
    return [
        pytest.param(name, id=name, marks=[pytest.mark.tool(name), pytest.mark.scenario(scenario)])
        for name in LICENSED_CALLS
    ]


def _successful_items(name, result):
    """The items of a successful call; none is a failure, since every call returns content."""

    assert not result.is_error, result.content
    found = items_of(name, result.content)
    assert found, f"no item where {TOOLS[name]['items']} say: {result.content!r:.300}"
    return found


def _licensed_items(tools, name):
    """The items of the licensed call of `name`, which are the licensed concepts it asks for."""

    release = {key: LICENSED[key] for key in ("terminology", "release")}
    arguments, codes = LICENSED_CALLS[name]
    licensed = _successful_items(name, tools.call(name, release | arguments))
    assert {item.get("code") for item in licensed} == codes
    return licensed


def _attributed(items):
    """The items whose provenance carries an attribution, null or empty included."""

    return [item for item in items if "attribution" in provenance_of(item)]


@pytest.mark.requirement("X-19")
@pytest.mark.parametrize("name", _licensed_cases("license/attributed"))
def test_licence_text_the_platform_gives_with_an_item_is_passed_through_unchanged(
    tools, pinned, recorded, name
):
    concept = recorded("scenarios/license/attributed/granted.json")["response"]["body"]
    text = concept[LICENCE_FIELD]
    # The crafted answers carry text, or the cases would show nothing.
    assert text

    licensed = _licensed_items(tools, name)
    # An NCIt call after it, given no text, must not inherit the licensed answer's.
    plain = _successful_items(name, _call(tools, pinned, name))

    attributions = [provenance_of(item).get("attribution") for item in licensed]
    assert (attributions, _attributed(plain)) == ([text] * len(licensed), [])


@pytest.mark.requirement("X-19")
@pytest.mark.parametrize("name", _licensed_cases("license/restricted"))
def test_no_item_carries_licence_text_the_platform_did_not_give_with_it(tools, recorded, name):
    rows = recorded("recorded/evs/terminologies.json")["response"]["body"]
    (row,) = [
        row
        for row in rows
        if (row["terminology"], row["version"]) == (LICENSED["terminology"], LICENSED["release"])
    ]
    concept = recorded("scenarios/license/restricted/granted.json")["response"]["body"]
    # The listing gives MedDRA licence text that the answers do not carry, as EVS serves them
    # today: a server that joins the listing's text, or holds its own, would show.
    assert (bool(row["metadata"]["licenseText"]), LICENCE_FIELD in concept) == (True, False)

    assert _attributed(_licensed_items(tools, name)) == []


@pytest.mark.requirement("X-19")
@pytest.mark.parametrize("name", PINNED)
def test_an_item_the_platform_gave_no_licence_text_with_carries_none(tools, pinned, name):
    assert _attributed(_items(tools, pinned, name)) == []


# Hostile forms of a well-formed identifier (X-23): a path climb, a query of its own, a fragment,
# a SPARQL clause, a bare climb, and a trailing newline, which a pattern's $ lets through unless
# the whole value is matched. Each is off any form tools.yaml states.
HOSTILE_FORMS = [
    "{}/../../x",
    "{}?include=full",
    "{}#x",
    "{}> }} UNION {{ ?s ?p ?o }}",
    "../",
    "{}\n",
]


# Identifier arguments with a stated form that no call in calls.yaml gives, each in a call built
# from one there: a data element's version (its upstream answer's), a data element of the code
# map (its paged call's), and a permissible value id (the one test_cadsr.py asks for), which is
# capability_unavailable when well-formed, so its form is checked first.
FORMED = {
    ("get_data_element", "version"): CALLS["get_data_element"]["arguments"]
    | {"version": CALLS["get_data_element"]["upstream"]["version"]},
    ("get_code_map", "dataElementId"): next(
        change
        for change in CALLS["get_code_map"]["paged"][0]["changed"]
        if "dataElementId" in change
    ),
    ("get_permissible_value", "permissibleValueId"): {"permissibleValueId": "9192925"},
    ("get_form", "version"): CALLS["get_form"]["arguments"]
    | {"version": CALLS["get_form"]["upstream"]["version"]},
    ("expand_value_set", "code"): {"code": CALLS["expand_value_set"]["arguments"]["valueSet"]},
    ("get_concept_for_permissible_value", "permissibleValueId"): {"permissibleValueId": "9192925"},
    ("get_concept_for_permissible_value", "version"): CALLS["get_concept_for_permissible_value"][
        "arguments"
    ]
    | {"version": "4.0"},
    ("resolve_stored_value", "dataElementId"): CALLS["resolve_stored_value"]["arguments"]
    | {"dataElementId": CALLS["get_concept_for_permissible_value"]["arguments"]["dataElementId"]},
    # The workflow tools have no call yet: their concept is the one resolve_stored_value asks.
    **{
        (name, argument): {"conceptCode": CALLS["resolve_stored_value"]["arguments"]["conceptCode"]}
        for name in ("ground_value", "expand_cohort")
        for argument in ("conceptCode", "release")
    },
}


def _pinned_call(name, pinned, argument=None):
    """The tool's call in calls.yaml, or the one FORMED builds for `argument`, with the fixture
    set's pin where the tool takes one."""

    taken = parameters(name)[0]
    call = FORMED.get((name, argument)) or CALLS[name]["arguments"]
    return {key: value for key, value in pinned.items() if key in taken} | call


def _formed_arguments():
    """Each identifier argument with a stated form that a call gives (the pin's terminology and
    release included), or that FORMED adds, with the marks of its call."""

    pin = {"terminology", "release"}
    given = [
        (name, argument, _marks(name, {}))
        for name in CALLS
        for argument in TOOLS[name].get("patterns", {})
        if argument in CALLS[name]["arguments"] or argument in pin
    ]
    return given + [(name, argument, [pytest.mark.tool(name)]) for name, argument in FORMED]


def _identifiers():
    """Each hostile form of each of those arguments."""

    return [
        pytest.param(name, argument, form, id=f"{name}-{argument}-{index}", marks=marks)
        for name, argument, marks in _formed_arguments()
        for index, form in enumerate(HOSTILE_FORMS)
    ]


def _form(name, argument, arguments):
    """The form `argument` takes in this call: by the call's terminology where the form is."""

    form = TOOLS[name]["patterns"][argument]
    return form.get(arguments.get("terminology")) if isinstance(form, dict) else form


def _strings(entry):
    """Everything a logged upstream request says, decoded: its path, parameters and body."""

    body = entry["body"] or ""
    params = [value for values in entry["params"].values() for value in values]
    decoded = [entry["path"], unquote(entry["raw"]), *params, body, unquote_plus(body)]
    return decoded + _body_values(entry)


@pytest.mark.unmatched_upstream
@pytest.mark.requirement("X-23")
@pytest.mark.parametrize(("name", "argument", "form"), _identifiers())
def test_an_identifier_off_its_stated_form_is_refused_before_any_request_carries_it(
    tools, upstream, pinned, name, argument, form
):
    arguments = _pinned_call(name, pinned, argument)
    given = arguments[argument]
    valid = given[0] if isinstance(given, list) else given
    hostile = form.format(valid)
    pattern = _form(name, argument, arguments)
    # The call's own value has the form and the hostile one does not, or the case shows nothing.
    assert (bool(re.fullmatch(pattern, valid)), re.fullmatch(pattern, hostile)) == (True, None)

    # A list's hostile element comes after a valid one, so that checking the first is not enough.
    result = tools.call(
        name, arguments | {argument: [*given, hostile] if isinstance(given, list) else hostile}
    )

    assert error_code(result) == "invalid_request", result.content
    carrying = [
        entry for entry in upstream.log() if any(hostile in each for each in _strings(entry))
    ]
    assert carrying == []


# A code with a slash, which a path would read as two segments.
SLASHED = "8001/3"


@pytest.mark.unmatched_upstream
@pytest.mark.requirement("X-23")
@pytest.mark.tool("get_concept")
def test_a_code_of_a_terminology_without_a_stated_form_goes_upstream_as_one_segment(
    tools, upstream, recorded
):
    rows = recorded("recorded/evs/terminologies.json")["response"]["body"]
    formed = TOOLS["get_concept"]["patterns"]["code"]
    row = min(
        (row for row in rows if row["terminology"] not in formed),
        key=lambda row: row["terminology"],
    )
    arguments = {"terminology": row["terminology"], "release": row["version"], "code": SLASHED}

    tools.call("get_concept", arguments)

    raws = [entry["raw"] for entry in upstream.log()]
    # One encoded path segment or parameter value (A7.6), and nowhere the slash as it is.
    encoded = [raw for raw in raws if re.search(r"[/=]8001%2[Ff]3(?:[/?&]|$)", raw)]
    assert (bool(encoded), [raw for raw in raws if SLASHED in raw]) == (True, []), raws


# Free text that would change a request if it were not sent as one encoded value (X-24): a
# parameter, a fragment, a path climb and quotes. Every value that holds its climb must be it.
HOSTILE_TEXT = "a&mode=x #b /../c \"d' e"
MARKER = "/../c"


def _texts():
    return [
        pytest.param(name, path, id=f"{name}-{path}", marks=_marks(name, {}))
        for name in CALLS
        for path in TOOLS[name].get("free_text", [])
    ]


def _with_text(arguments, path, text):
    """`arguments` with `text` at `path`: a parameter (`query`), the one element of a list
    parameter (`values[]`), or a field of a list parameter's one element (`entities[].name`,
    `entities[].permissibleValues[]`)."""

    root, listed, field = path.partition("[]")
    if not listed:
        return arguments | {root: text}
    field = field.removeprefix(".")
    if not field:
        return arguments | {root: [text]}
    name = field.removesuffix("[]")
    element = (arguments.get(root) or [{}])[0]
    return arguments | {root: [element | {name: [text] if field.endswith("[]") else text}]}


def _values(entry):
    """The values a logged upstream request carries: its path, each parameter value, and each
    field of its body (a form's or a JSON document's), or the body as sent."""

    params = [value for values in entry["params"].values() for value in values]
    return [entry["path"], *params, *_body_values(entry)]


def _body_values(entry):
    body = entry["body"] or ""
    kind = {key.lower(): value for key, value in entry["headers"].items()}.get("content-type", "")
    if kind.partition(";")[0].strip().lower() == FORM_TYPE:
        return [value for values in parse_qs(body).values() for value in values]
    try:
        return list(_json_strings(json.loads(body)))
    except ValueError:
        return [body]


def _json_strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict | list):
        for each in value.values() if isinstance(value, dict) else value:
            yield from _json_strings(each)


@pytest.mark.unmatched_upstream
@pytest.mark.requirement("X-24")
@pytest.mark.parametrize(("name", "path"), _texts())
def test_free_text_reaches_the_platform_as_one_value_equal_to_it(
    tools, upstream, pinned, name, path
):
    left_out = alternatives(name, path.partition("[")[0])
    arguments = {
        key: value for key, value in _pinned_call(name, pinned).items() if key not in left_out
    }

    tools.call(name, _with_text(arguments, path, HOSTILE_TEXT))

    holding = {value for entry in upstream.log() for value in _values(entry) if MARKER in value}
    assert holding == {HOSTILE_TEXT}
