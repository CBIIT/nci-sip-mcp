"""The protocol gates pass against a server that meets them, and each fails on its own defect
(compliant_server.py)."""

import pytest

pytest_plugins = ["pytester"]

# The gate tests, by requirement.
P1 = "test_tools_list_names_the_tools_of_the_profile_and_no_other"
P2 = "test_every_output_schema_admits_the_error_record_and_refuses_a_malformed_one"
P3 = "test_no_description_holds_placeholder_or_debug_text"
P4 = "test_tool_names_are_verb_led_lowercase_and_underscore_separated"
P5 = "test_each_list_may_be_cached_and_shared"
P6_CALLED = "test_tools_list_is_the_same_after_a_content_call"
P6_UNAVAILABLE = "test_tools_list_is_the_same_while_the_platform_is_unavailable"
P7 = "test_a_correlation_identifier_goes_upstream_and_comes_back"
P10 = "test_every_tool_is_annotated_read_only_idempotent_and_open_world"
P11 = "test_no_description_or_schema_shows_what_the_tool_does_not_offer"
P12 = "test_each_tool_takes_the_parameters_the_specification_names"
P8_PROMPTS = "test_prompts_list_names_the_prompts_of_the_profile_with_their_arguments"
P8_MESSAGES = (
    "test_a_prompt_returns_messages_naming_the_tools_it_states_and_only_tools_of_the_profile"
)
P8_RESOURCES = (
    "test_resources_list_the_concrete_resources_and_templates_list_the_templates_of_the_profile"
)
P9_CONTENT = "test_a_resource_read_is_json_in_its_mime_type_and_matches_its_tool_s_answer"
P9_PROVENANCE = "test_a_resource_read_carries_a_provenance_record"
P9_CACHING = "test_a_resource_read_carries_the_caching_hint_of_what_it_holds"
P9_MANIFEST = "test_the_index_manifest_states_what_the_index_holds"
P9_UNMATCHED = "test_a_uri_no_resource_or_template_matches_is_an_error_and_never_content"
# Each defect of the gate server, the gate test that must fail on it, and the cases of that test
# that must (the others pass); a defect that names none fails every case.
DEFECTS = [
    ("missing-tool", P1),
    ("no-error-shape", P2),
    ("declares-no-shape", P2),
    ("one-code", P2),
    ("placeholder", P3),
    ("generated-title", P3),
    ("misnamed", P4),
    ("no-ttl", P5, "tools"),
    ("prompts-no-ttl", P5, "prompts"),
    ("resources-no-ttl", P5, "resources"),
    ("templates-no-ttl", P5, "templates"),
    ("lists-private", P5),
    ("listing-changes", P6_CALLED),
    ("redescribed", P6_CALLED),
    ("hides-tools", P6_UNAVAILABLE),
    ("no-correlation", P7),
    ("destructive", P10),
    ("not-idempotent", P10),
    ("closed-world", P10),
    ("parameter-renamed", P12),
    ("release-required-schema", P12),
    ("quotes-not-offered", P11),
    ("lists-not-offered", P11),
    ("patterns-not-offered", P11),
    ("no-prompts", P8_PROMPTS),
    ("prompt-outside-profile", P8_PROMPTS),
    ("prompt-outside-profile", P8_MESSAGES),
    ("no-resources", P8_RESOURCES),
    ("uri-differs", P8_RESOURCES),
    ("templates-as-resources", P8_RESOURCES),
    ("no-resources", P9_CONTENT),
    ("resource-other-content", P9_CONTENT),
    ("resource-wrong-mime", P9_CONTENT),
    ("manifest-other-release", P9_CONTENT, "index_manifest"),
    ("resource-no-provenance", P9_PROVENANCE),
    ("bad-timestamp", P9_PROVENANCE),
    ("resource-no-ttl", P9_CACHING),
    ("resource-private-scope", P9_CACHING),
    ("resource-other-ttl", P9_CACHING),
    ("resource-ttl-in-meta", P9_CACHING),
    ("manifest-no-embedding", P9_MANIFEST),
    ("manifest-unbuilt", P9_MANIFEST),
    ("unmatched-uri-content", P9_UNMATCHED),
]


def _cases(found, gate):
    """The outcomes of the cases of `gate` by case: a parametrized gate has one for each."""

    return {
        case.partition("[")[2].removesuffix("]"): outcome
        for case, outcome in found.items()
        if case.partition("[")[0] == gate
    }


def _expected(cases, only):
    """What each case must give on a defect: failed for those it is to fail, else passed."""

    assert set(only) <= set(cases), f"{sorted(set(only) - set(cases))} is no case of the gate"
    return {case: "failed" if not only or case in only else "passed" for case in cases}


def test_every_gate_passes_against_a_server_that_meets_them(outcomes):
    passed = outcomes("tests/test_protocol.py")

    assert set(passed.values()) == {"passed"}
    # A parametrized gate has a case for each resource the profile serves.
    assert {case.partition("[")[0] for case in passed} == {entry[1] for entry in DEFECTS}


@pytest.mark.parametrize("entry", DEFECTS, ids=lambda entry: "/".join(entry[:2]))
def test_each_gate_fails_on_its_own_defect(outcomes, monkeypatch, entry):
    defect, gate, *only = entry
    monkeypatch.setenv("COMPLIANT_SERVER_DEFECT", defect)

    cases = _cases(outcomes("tests/test_protocol.py", "-k", gate), gate)

    assert cases
    assert cases == _expected(cases, only)


# The prompt and resource gates, and the caching hints of the lists.
SURFACE = (
    P5,
    P8_PROMPTS,
    P8_MESSAGES,
    P8_RESOURCES,
    P9_CONTENT,
    P9_PROVENANCE,
    P9_CACHING,
    P9_MANIFEST,
    P9_UNMATCHED,
)
# The cases of the resource gates in the unified profile: six resources, the data element with
# two URI templates.
READS = [
    "concept",
    "release",
    "index_manifest",
    "data_element-publicId",
    "data_element-publicId-version",
    "registry",
    "crosswalk",
]
# Defects seen in the unified profile, which lists all four prompts and serves every resource:
# those that need a prompt to show in, and the resource defects in the resource each is of.
UNIFIED_DEFECTS = [
    ("prompt-missing", P8_PROMPTS),
    ("prompt-missing", P8_MESSAGES),
    ("argument-undeclared", P8_PROMPTS),
    ("prompt-omits-tool", P8_MESSAGES),
    ("prompt-reordered", P8_MESSAGES),
    ("prompt-empty", P8_MESSAGES),
    ("uri-differs", P8_RESOURCES),
    ("templates-as-resources", P8_RESOURCES),
    ("resource-wrong-mime", P9_CONTENT),
    ("resource-other-content", P9_CONTENT),
    ("resource-ignores-version", P9_CONTENT, "data_element-publicId-version"),
    ("crosswalk-cut", P9_CONTENT, "crosswalk"),
    ("manifest-other-release", P9_CONTENT, "index_manifest"),
    ("registry-identifier", P9_CONTENT, "registry"),
    ("registry-identifier", P9_PROVENANCE, "registry"),
    ("resource-no-provenance", P9_PROVENANCE),
    ("bad-timestamp", P9_PROVENANCE),
    ("resource-no-ttl", P9_CACHING),
    ("resource-private-scope", P9_CACHING),
    ("resource-other-ttl", P9_CACHING),
    ("resource-ttl-in-meta", P9_CACHING),
    ("manifest-no-embedding", P9_MANIFEST),
    ("manifest-unbuilt", P9_MANIFEST),
    ("unmatched-uri-content", P9_UNMATCHED),
]


@pytest.fixture
def unified(compliant, monkeypatch):
    """The compliant server serving the unified profile, tested as such."""

    monkeypatch.setenv("NCI_SIP_ACCEPTANCE_PROFILE", "unified")
    monkeypatch.setenv("COMPLIANT_SERVER_PROFILE", "unified")


def _run_surface(outcomes):
    return outcomes(*(f"tests/test_protocol.py::{gate}" for gate in SURFACE))


def test_the_prompt_and_resource_gates_pass_in_the_unified_profile(unified, outcomes):
    found = _run_surface(outcomes)

    assert set(found.values()) == {"passed"}
    assert set(_cases(found, P9_CONTENT)) == set(READS)
    assert set(_cases(found, P5)) == {"tools", "prompts", "resources", "templates"}


def test_a_server_that_pages_its_lists_passes_the_gates_all_the_same(
    unified, outcomes, monkeypatch
):
    # One item to a page: four prompts, two resources, five templates, each in several pages.
    monkeypatch.setenv("COMPLIANT_SERVER_PAGE_SIZE", "1")

    found = _run_surface(outcomes)

    assert set(found.values()) == {"passed"}


@pytest.mark.parametrize("entry", UNIFIED_DEFECTS, ids=lambda entry: "/".join(entry[:2]))
def test_each_prompt_and_resource_gate_fails_on_its_own_defect_in_the_unified_profile(
    unified, outcomes, monkeypatch, entry
):
    defect, gate, *only = entry
    monkeypatch.setenv("COMPLIANT_SERVER_DEFECT", defect)

    cases = _cases(outcomes("tests/test_protocol.py", "-k", gate), gate)

    assert cases
    assert cases == _expected(cases, only)
