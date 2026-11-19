"""The cross-cutting tests pass against a server that meets them, and each fails on its own
defect (compliant_server.py)."""

import ast
from pathlib import Path

import pytest
import yaml

from nci_sip_acceptance.spec import TOOLS, profile_tools

pytest_plugins = ["pytester"]

SUITE = Path(__file__).parent.parent / "tests"
CROSS_CUTTING = "tests/test_crosscutting.py"
UNKNOWN_RELEASE = "test_a_release_the_platform_does_not_serve_fails_closed"
LICENCE_ERROR = "test_an_error_carries_no_licence_key"
EMPTY = "test_a_query_that_matches_nothing_is_an_empty_result_with_provenance"
TRUNCATED = "test_a_bound_reached_is_reported_with_how_much_was_left_out"
LICENCE_REACHES = "test_the_licence_key_reaches_the_platform_and_nothing_the_server_returns_or_logs"
BACKOFF = "test_a_rate_limited_request_is_asked_once_more_after_the_wait"
PAGED = "test_a_cursor_continues_with_the_next_items_of_the_same_release"
PASSED_THROUGH = "test_licence_text_the_platform_gives_with_an_item_is_passed_through_unchanged"
NOT_GIVEN = "test_no_item_carries_licence_text_the_platform_did_not_give_with_it"
CODES = "test_codes_are_bare_with_their_terminology_beside_them"
OFF_FORM = "test_an_identifier_off_its_stated_form_is_refused_before_any_request_carries_it"
# Each defect of the compliant server, and the cross-cutting test whose every case must fail.
DEFECTS = [
    ("wrong-release", "test_every_item_carries_the_release_requested"),
    ("wrong-terminology", "test_every_item_carries_the_release_requested"),
    ("invalid-result", "test_a_result_validates_against_the_declared_output_schema"),
    ("list-result", "test_a_result_is_an_object"),
    ("no-served-by", "test_every_item_carries_its_provenance"),
    ("bad-timestamp", "test_every_item_carries_its_provenance"),
    ("no-polarity", "test_an_item_reached_by_traversal_says_how"),
    ("nothing-reached", "test_an_item_reached_by_traversal_says_how"),
    ("prefixed-code", CODES),
    ("repeated-request", "test_no_upstream_request_is_repeated_within_a_call"),
    ("asks-nothing", "test_no_upstream_request_is_repeated_within_a_call"),
    ("uncached-result", "test_a_release_pinned_result_may_be_cached"),
    ("private-scope", "test_a_release_pinned_result_may_be_cached"),
    ("unknown-as-empty", UNKNOWN_RELEASE),
    ("content-with-error", UNKNOWN_RELEASE),
    ("accepts-mismatch", "test_content_of_another_release_fails_closed"),
    ("outage-as-empty", "test_an_unavailable_platform_is_an_upstream_error"),
    ("unshaped-error", "test_an_error_validates_against_the_declared_output_schema"),
    ("keyless", LICENCE_REACHES),
    ("logs-key", LICENCE_REACHES),
    ("logs-key", LICENCE_ERROR),
    ("files-key", LICENCE_REACHES),
    ("key-in-text", LICENCE_REACHES),
    ("leaks-key", LICENCE_ERROR),
    ("key-in-meta", LICENCE_ERROR),
    ("gives-up", BACKOFF),
    ("no-backoff", BACKOFF),
    ("empty-as-error", EMPTY),
    ("empty-without-provenance", EMPTY),
    ("upstream-renamed", "test_what_the_platform_says_of_an_item_s_origin_is_passed_through"),
    ("upstream-altered", "test_what_the_platform_says_of_an_item_s_origin_is_passed_through"),
    ("truncation-flag-only", TRUNCATED),
    ("omitted-unknown", TRUNCATED),
    ("exact-missing", TRUNCATED),
    ("reached-over", TRUNCATED),
    ("no-next-cursor", PAGED),
    ("cursor-repeats", PAGED),
    ("cursor-other-release", PAGED),
    ("cursor-empty", PAGED),
    ("cursor-ignores-release", PAGED),
    ("cursor-offset-only", "test_a_cursor_with_another_argument_is_an_invalid_request"),
    (
        "cursor-refuses-default",
        "test_a_cursor_with_a_left_out_argument_given_as_its_default_continues",
    ),
    ("cursor-refuses-default", "test_a_cursor_with_a_given_default_left_out_continues"),
    (
        "cursor-inherits",
        "test_a_cursor_without_an_argument_the_first_call_gave_is_an_invalid_request",
    ),
    ("empty-with-cursor", EMPTY),
    ("wrong-default", "test_a_left_out_argument_is_its_stated_default"),
    ("raised-to-one", "test_a_bounded_argument_below_one_is_an_invalid_request"),
    ("release-refused", "test_a_call_without_release_names_the_resolved_ncit_release"),
    ("implicit-public", "test_a_call_without_release_names_the_resolved_ncit_release"),
    ("unchecked-identifiers", OFF_FORM),
    ("checks-after-asking", OFF_FORM),
    ("asks-in-path-then-refuses", OFF_FORM),
    ("asks-in-form-then-refuses", OFF_FORM),
    ("text-twice", "test_free_text_reaches_the_platform_as_one_value_equal_to_it"),
    (
        "unencoded-code",
        "test_a_code_of_a_terminology_without_a_stated_form_goes_upstream_as_one_segment",
    ),
    ("unencoded-text", "test_free_text_reaches_the_platform_as_one_value_equal_to_it"),
    ("joins-listing", NOT_GIVEN),
    ("attribution-everywhere", NOT_GIVEN),
    ("drops-attribution", PASSED_THROUGH),
    ("alters-attribution", PASSED_THROUGH),
    ("attributes-all-but-last", PASSED_THROUGH),
    ("sticky-attribution", PASSED_THROUGH),
    ("attributes-ncit", "test_an_item_the_platform_gave_no_licence_text_with_carries_none"),
]


# The compliant server serves the evs profile: a case of a tool of another group is NOT
# IMPLEMENTED there, and shows nothing of the test.
OTHER_GROUPS = set(TOOLS) - profile_tools("evs")


def _served(found, ignoring=frozenset()):
    """The outcomes of the cases of tools the compliant server serves, less those of `ignoring`:
    a case's id names its tool among its dash-separated parts (`get_code_map`,
    `-1-get_code_map-limit`)."""

    def other(case):
        return (OTHER_GROUPS | ignoring) & set(case.partition("[")[2].rstrip("]").split("-"))

    return {case: outcome for case, outcome in found.items() if not other(case)}


def _tree():
    return ast.parse((SUITE / "test_crosscutting.py").read_text(encoding="utf-8"))


def _functions():
    """The test functions of the cross-cutting file, read from it."""

    return [
        node.name
        for node in _tree().body
        if isinstance(node, ast.FunctionDef) and node.name.startswith("test_")
    ]


FUNCTIONS = _functions()
DEFECTIVE = {test for _, test in DEFECTS}


def test_every_defect_names_a_cross_cutting_test():
    assert FUNCTIONS
    assert sorted(DEFECTIVE - set(FUNCTIONS)) == []


def test_every_cross_cutting_test_is_a_function_of_the_file_and_in_one_chunk():
    # A test in a class, or an async one, would run in no chunk.
    named = [
        node.name
        for node in ast.walk(_tree())
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        and node.name.lower().startswith("test")
    ]
    chunked = [test for chunk in range(CHUNKS) for test in FUNCTIONS[chunk::CHUNKS]]

    assert sorted(named) == sorted(FUNCTIONS) == sorted(chunked)


# The functions in as many nested runs as the self-tests have workers over all shards (three
# CI jobs of four), so that the runs spread over them; each run a subprocess, which costs its
# start.
CHUNKS = 12


def _function_of(case):
    return case.partition("[")[0]


# A test with a defect of its own passes in every case the compliant server serves, and in at
# least one; any other test serves none.
@pytest.mark.parametrize("chunk", range(CHUNKS))
def test_every_cross_cutting_test_passes_against_a_server_that_meets_it(outcomes, chunk):
    tests = FUNCTIONS[chunk::CHUNKS]

    served = _served(outcomes(*(f"{CROSS_CUTTING}::{test}" for test in tests)))

    assert {test for test in tests if test in DEFECTIVE} == set(map(_function_of, served))
    assert {case: outcome for case, outcome in served.items() if outcome != "passed"} == {}


def test_only_a_call_without_a_pinned_form_may_answer_an_unknown_release_with_the_mismatch(
    outcomes, monkeypatch
):
    calls = yaml.safe_load((SUITE / "calls.yaml").read_text(encoding="utf-8"))
    # The compliant server serves the evs profile; another group's call shows nothing here.
    unpinned = {
        name for name, call in calls.items() if call.get("unpinned") and name not in OTHER_GROUPS
    }
    monkeypatch.setenv("COMPLIANT_SERVER_DEFECT", "unpinned-mismatch")

    found = _served(outcomes(CROSS_CUTTING, "-k", UNKNOWN_RELEASE))

    passed = {
        case.partition("[")[2].rstrip("]") for case, outcome in found.items() if outcome == "passed"
    }
    assert unpinned
    assert passed == unpinned
    assert set(found.values()) == {"passed", "failed"}


# A release record holds no code, so a defect in the codes of an answer shows in no case of it.
CODELESS = {CODES: {"resolve_release"}}


@pytest.mark.parametrize(("defect", "test"), DEFECTS)
def test_each_cross_cutting_test_fails_on_its_own_defect(outcomes, monkeypatch, defect, test):
    monkeypatch.setenv("COMPLIANT_SERVER_DEFECT", defect)

    failing = _served(outcomes(CROSS_CUTTING, "-k", test), CODELESS.get(test, frozenset()))

    assert failing
    assert set(failing.values()) == {"failed"}


@pytest.mark.parametrize(
    "defect",
    [
        "no-correlation",
        "wrong-correlation",
        "no-source-uri",
        "no-upstream",
        "uri-not-a-url",
        "live-as-index",
        "cache-loses-origin",
        "cache-swaps-origin",
    ],
)
def test_provenance_contract_rejects_missing_or_substituted_source_facts(
    outcomes, monkeypatch, defect
):
    """Given deficient live provenance, the suite must report the owning X-7 case failed."""

    monkeypatch.setenv("COMPLIANT_SERVER_DEFECT", defect)
    case = "test_every_item_carries_its_provenance[get_concept]"

    assert outcomes(f"{CROSS_CUTTING}::{case}") == {case: "failed"}


def test_empty_result_provenance_cannot_invent_an_item_origin(outcomes, monkeypatch):
    monkeypatch.setenv("COMPLIANT_SERVER_DEFECT", "itemless-origin")
    case = "test_a_query_that_matches_nothing_is_an_empty_result_with_provenance[search_concepts]"

    assert outcomes(f"{CROSS_CUTTING}::{case}") == {case: "failed"}
