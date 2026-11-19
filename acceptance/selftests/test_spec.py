"""The specification read as data: where a result's items are, the parameters each tool takes,
and the tools of a profile."""

import re
from pathlib import Path

import pytest

from nci_sip_acceptance import client, environments
from nci_sip_acceptance.client import (
    ADAPTER_AUTHORITY_VARIABLE,
    ADAPTER_PORT_VARIABLE,
    CREDENTIAL_VARIABLES,
    INDEX_CODES_VARIABLE,
)
from nci_sip_acceptance.fixture_server import (
    HARNESS_VARIABLES,
    SCENARIOS,
    SETTINGS,
    fixture_documents,
)
from nci_sip_acceptance.record import FIXTURES
from nci_sip_acceptance.spec import (
    PROFILES,
    PROMPTS,
    RECORDS,
    REQUIRED_TOOLS,
    RESOURCES,
    SPEC,
    TOOLS,
    Listing,
    alternatives,
    defaults,
    items_of,
    parameters,
    profile_tools,
    prompts_of,
    resource_call,
    resources_listed,
    resources_of,
    tools_named,
    uri_variables,
)

CONCEPT = {"code": "C4817"}


@pytest.mark.parametrize(
    ("tool", "result", "items"),
    [
        ("get_concept", CONCEPT, [CONCEPT]),
        ("get_concepts", {"concepts": [CONCEPT, CONCEPT], "missing": []}, [CONCEPT, CONCEPT]),
        (
            "resolve_retired_code",
            {**CONCEPT, "replacements": [CONCEPT]},
            [{**CONCEPT, "replacements": [CONCEPT]}, CONCEPT],
        ),
        ("search_concepts", {"results": [{"concept": CONCEPT, "score": 1}]}, [CONCEPT]),
        (
            "get_concept_neighborhood",
            {"nodes": [CONCEPT], "edges": [{"code": "R1"}]},
            [CONCEPT, {"code": "R1"}],
        ),
        # A result without the parts its items name has none: the tests then fail, saying so.
        ("search_concepts", {"hits": [CONCEPT]}, []),
        ("search_concepts", {"results": [{"concept": None}]}, []),
        ("get_concepts", "an error, in words", []),
        ("get_concept", None, []),
        ("get_data_element", {"publicId": "2200604"}, [{"publicId": "2200604"}]),
        # A tool that declares no items: its result is a record of the registry, not an item.
        ("resolve_registry_release", {"published": False}, []),
    ],
)
def test_the_items_of_a_result_are_where_the_tool_says(tool, result, items):
    assert items_of(tool, result) == items


@pytest.mark.parametrize(
    ("tool", "names", "required"),
    [
        (
            "get_concept",
            {"terminology", "release", "code", "include"},
            {"terminology", "code"},
        ),
        ("resolve_release", {"terminology", "channel"}, {"terminology"}),
        # Alternatives: none of them is required.
        (
            "expand_value_set",
            {"terminology", "release", "valueSet", "code", "count", "offset", "activeOnly"},
            {"terminology"},
        ),
        (
            "get_form",
            {"publicId", "keyword", "version", "includeModules", "registryRelease"},
            set(),
        ),
        # Three ways to name a data element, of which a caller gives one.
        (
            "get_data_element",
            {"publicId", "version", "longName", "questionText", "include", "registryRelease"},
            set(),
        ),
        # An alternative of several parameters, in braces.
        (
            "get_concept_for_permissible_value",
            {"permissibleValueId", "dataElementId", "value", "release", "version"},
            set(),
        ),
        # A list of objects is one parameter; its fields are not.
        (
            "harmonize_data_dictionary",
            {"columns", "registryRelease", "filters"},
            {"columns"},
        ),
        ("list_terminologies", set(), set()),
    ],
)
def test_a_tool_takes_the_parameters_its_inputs_name(tool, names, required):
    assert parameters(tool) == (names, required)


def test_a_module_profile_serves_its_own_group_and_unified_serves_every_tool():
    evs, cadsr, unified = (profile_tools(profile) for profile in ("evs", "cadsr", "unified"))

    assert {REQUIRED_TOOLS[name] for name in evs} == {"evs"}
    assert {REQUIRED_TOOLS[name] for name in cadsr} == {"cadsr"}
    assert (len(evs), len(cadsr), unified) == (12, 10, set(REQUIRED_TOOLS))


@pytest.mark.parametrize("tool", sorted(TOOLS))
def test_a_tool_s_defaults_are_its_stated_defaults_and_its_bounds_defaults(tool):
    entry = TOOLS[tool]
    bounds = entry.get("bounds", {})

    found = defaults(tool)

    assert {name: found.get(name) for name in entry.get("defaults", {})} == entry.get(
        "defaults", {}
    )
    # A bound without a default (budgetPerKind) gives none.
    assert {name for name in bounds if name in found} == {
        name for name, bound in bounds.items() if "default" in bound
    }
    assert {name: found[name] for name in bounds if name in found} == {
        name: bound["default"] for name, bound in bounds.items() if "default" in bound
    }


def _section(heading):
    """The text of the specification's section `heading`, up to the next one."""

    text = (SPEC / "acceptance.md").read_text(encoding="utf-8")
    return text.partition(f"### {heading}")[2].partition("\n### ")[0]


def _settings_rows():
    """Each setting the specification says the suite gives a server, with when it does."""

    rows = _section("Settings the suite gives a server").splitlines()
    cells = [row.split(" | ") for row in rows if row.startswith("| `NCI_SIP_")]
    return {
        name: when for first, when, _ in cells for name in re.findall(r"`(NCI_SIP_\w+)`", first)
    }


def _scenario_settings():
    """Each setting a scenario's settings.json gives its server, with the scenarios that do."""

    found = {}
    for name, settings in sorted(fixture_documents(FIXTURES).items()):
        group, *scenario, file = name.split("/")
        if group == SCENARIOS and file == SETTINGS:
            for setting in settings:
                found.setdefault(setting, []).append("/".join(scenario))
    return found


def test_the_specification_names_every_setting_the_suite_gives_a_server_and_when():
    rows, scenarios = _settings_rows(), _scenario_settings()
    given = {
        *HARNESS_VARIABLES,
        *CREDENTIAL_VARIABLES,
        INDEX_CODES_VARIABLE,
        ADAPTER_PORT_VARIABLE,
        ADAPTER_AUTHORITY_VARIABLE,
        *scenarios,
    }

    assert set(rows) == given
    # A row names exactly the scenarios that set its setting, so that a server's team can read
    # when each is set.
    named = {setting: set(re.findall(r"`(\w+/[\w-]+)`", when)) for setting, when in rows.items()}
    assert named == {setting: set(scenarios.get(setting, [])) for setting in rows}


def _list_inputs(tool):
    """The parameters `tool` takes as lists: those its inputs write with brackets."""

    return set(re.findall(r"(\w+)\[", TOOLS[tool]["inputs"]))


def _returned_fields(tool):
    """The names what `tool` returns holds: those its returns text names, and the fields of
    each record it names."""

    names = set(re.findall(r"\w+", TOOLS[tool]["returns"]))
    return names.union(*(set(RECORDS[name]["fields"]) for name in names & set(RECORDS)))


@pytest.mark.parametrize("tool", sorted(TOOLS))
def test_what_a_tool_states_of_its_arguments_and_items_names_what_it_takes_and_returns(tool):
    entry, (names, _) = TOOLS[tool], parameters(tool)
    stated = {*entry.get("defaults", {}), *entry.get("bounds", {}), *entry.get("values", {})}
    stated |= {*entry.get("patterns", {})}
    # A free-text path names a parameter, and within a list's elements a field its inputs name.
    texts = {path.partition("[")[0] for path in entry.get("free_text", [])}
    fields = {
        path.split(".")[1].removesuffix("[]") for path in entry.get("free_text", []) if "." in path
    }
    assert {field for field in fields if not re.search(rf"\b{field}\b", entry["inputs"])} == set()
    steps = {step.removesuffix("[]") for path in entry.get("items", []) for step in path.split(".")}

    assert (stated | texts) - names == set()
    assert set(entry.get("lists", {})) - _list_inputs(tool) == set()
    assert steps - {""} - _returned_fields(tool) == set()


@pytest.mark.parametrize(
    ("tool", "name", "others"),
    [
        ("get_data_element", "longName", {"publicId", "questionText"}),
        # value is given together with dataElementId, in place of permissibleValueId.
        ("get_concept_for_permissible_value", "value", {"permissibleValueId"}),
        ("get_concept_for_permissible_value", "permissibleValueId", {"dataElementId", "value"}),
        ("search_concepts", "query", set()),
        ("list_terminologies", "code", set()),
    ],
)
def test_the_alternatives_of_a_parameter_are_those_given_in_its_place(tool, name, others):
    assert alternatives(tool, name) == others


def _variables(value):
    return uri_variables(value) if isinstance(value, str) else []


@pytest.mark.parametrize("key", sorted(RESOURCES))
def test_a_resource_names_a_tool_of_its_group_and_fills_the_tool_s_parameters(key):
    resource = RESOURCES[key]
    names, required = parameters(resource["tool"])
    arguments = set(resource["arguments"])
    used = {variable for value in resource["arguments"].values() for variable in _variables(value)}
    selected = set(resource.get("selects", []))
    templated = {variable for uri in resource["uri"] for variable in uri_variables(uri)}

    assert REQUIRED_TOOLS[resource["tool"]] == resource["group"]
    # The arguments are the tool's, and give those it requires.
    assert arguments <= names
    assert required <= arguments
    # Each variable of a template fills an argument or selects the release, not both.
    assert templated == used | selected
    assert not used & selected
    # A record of records.yaml, or the shape of a result as tools.yaml writes it.
    assert resource["returns"] in RECORDS or resource["returns"].startswith("{")


def test_a_uri_template_belongs_to_one_resource_and_a_resource_has_its_mime_type():
    templates = [uri for resource in RESOURCES.values() for uri in resource["uri"]]

    assert len(templates) == len(set(templates))
    assert {resource["mime"] for resource in RESOURCES.values()} == {"application/json"}


@pytest.mark.parametrize("key", sorted(RESOURCES))
def test_a_resource_states_the_class_of_what_it_holds_and_what_it_is_compared_on(key):
    resource = RESOURCES[key]

    # EVS content is pinned by the release in its URI; caDSR registry content is pinned by none.
    assert resource["holds"] == ("release-pinned" if resource["group"] == "evs" else "unpinned")
    assert resource.get("compared", "identity and release") in ("identity", "identity and release")
    # What the provenance adds to X-7 is fields of the record, with values of their closed sets.
    stated = resource.get("provenance", {})
    fields = RECORDS["provenance"]["fields"]
    assert [name for name in stated if name not in fields] == []
    assert [name for name, value in stated.items() if value not in fields[name]["values"]] == []


def test_the_export_folder_listing_is_a_source_of_provenance_of_the_registry_resource():
    assert RESOURCES["registry"]["provenance"]["source"] == "cadsr_export"
    assert "cadsr_export" in RECORDS["provenance"]["fields"]["source"]["values"]


def test_the_manifest_is_the_index_s_own_record_with_its_embedding_and_build_time():
    fields = RECORDS["index_manifest"]["fields"]

    assert set(fields) == {
        "terminology",
        "version",
        "concepts",
        "embedding",
        "builtAt",
        "provenance",
    }
    assert RESOURCES["index_manifest"]["compared"] == "identity"


def test_a_profile_serves_the_resources_of_its_group_and_unified_serves_all_six():
    assert set(resources_of("evs")) == {"concept", "release", "index_manifest"}
    assert set(resources_of("cadsr")) == {"data_element", "registry", "crosswalk"}
    assert resources_of("unified") == RESOURCES


def test_resources_and_templates_are_listed_apart_by_whether_the_uri_has_a_variable():
    assert resources_listed("cadsr") == Listing(
        {"cadsr://registry/release", "cadsr://crosswalk/crdc"},
        {"cadsr://data-element/{publicId}", "cadsr://data-element/{publicId}/{version}"},
    )
    assert resources_listed("evs") == Listing(
        set(),
        {
            "ncit://concept/{release}/{code}",
            "ncit://release/{version}",
            "ncit://index/manifest/{release}",
        },
    )
    assert resources_listed("unified").uris == resources_listed("cadsr").uris


@pytest.mark.parametrize(
    ("key", "template", "values", "call"),
    [
        (
            "concept",
            "ncit://concept/{release}/{code}",
            {"release": "26.09d", "code": "C4817"},
            {"terminology": "ncit", "release": "26.09d", "code": "C4817"},
        ),
        # The latest version names none; the template that names one passes it on.
        ("data_element", "cadsr://data-element/{publicId}", {"publicId": "88"}, {"publicId": "88"}),
        (
            "data_element",
            "cadsr://data-element/{publicId}/{version}",
            {"publicId": "88", "version": "5.1"},
            {"publicId": "88", "version": "5.1"},
        ),
        # A variable that only selects the release is no argument.
        ("release", "ncit://release/{version}", {"version": "26.09d"}, {"terminology": "ncit"}),
        ("registry", "cadsr://registry/release", {}, {}),
    ],
)
def test_a_resource_is_read_by_calling_its_tool_with_the_template_filled(
    key, template, values, call
):
    tool, arguments = resource_call(key, template, values)

    assert tool == RESOURCES[key]["tool"]
    assert {name: value for name, value in arguments.items() if name != "include"} == call


@pytest.mark.parametrize("name", sorted(PROMPTS))
def test_a_prompt_names_its_tools_in_order_and_fills_its_arguments(name):
    prompt = PROMPTS[name]
    declared = {each["name"] for each in prompt["arguments"]}

    # The tools its text names, in the order it first names them, starting from the release.
    assert tools_named(prompt["template"]) == prompt["tools"]
    assert prompt["tools"][0] == "resolve_release"
    assert set(prompt["tools"]) <= profile_tools("unified")
    # A placeholder is a declared argument, and an argument is used. Each argument is plain:
    # a name, whether it is required, and its description.
    assert set(uri_variables(prompt["template"])) == declared
    assert all(set(each) == {"name", "required", "description"} for each in prompt["arguments"])
    assert any(each["required"] for each in prompt["arguments"])


def test_a_prompt_is_listed_only_in_a_profile_with_every_tool_it_names():
    modules = [prompts_of(profile) for profile in PROFILES if profile != "unified"]

    assert modules == [{}, {}]
    assert prompts_of("unified") == PROMPTS
    assert set(PROMPTS) == {
        "protocol_authoring",
        "crdc_model_alignment",
        "uscdi_cancer_curation",
        "cross_program_harmonization",
    }


def test_the_names_a_text_holds_are_the_required_tools_once_each_in_order():
    text = "Call get_form, then resolve_release, get_form again, ncit_traverse and plain_words."

    assert tools_named(text) == ["get_form", "resolve_release"]


@pytest.mark.parametrize(
    ("text", "names_get_form"),
    [
        ("Xget_form", False),
        ("get_form_", False),
        ("get_form2", False),
        ("GET_FORM", False),
        ("get_form_x", False),
        ("get_form", True),
        ("get_form.", True),
        ("**get_form**", True),
    ],
)
def test_a_word_names_a_tool_only_as_a_whole_word(text, names_get_form):
    assert (tools_named(text) == ["get_form"]) is names_get_form


def test_the_specification_names_every_setting_of_a_run_and_the_readme_links_it():
    operator = {
        value
        for name, value in vars(client).items()
        if name.endswith("_VARIABLE")
        and value.startswith("NCI_SIP_ACCEPTANCE_")
        and value != INDEX_CODES_VARIABLE
    }
    operator.add(environments.VARIABLE)
    rows = _section("Settings of a run")
    readme = (Path(__file__).parents[1] / "README.md").read_text(encoding="utf-8")

    assert set(re.findall(r"^\| `(NCI_SIP_ACCEPTANCE_\w+)`", rows, re.MULTILINE)) == operator
    # The README restates no setting table; it points to the one owner.
    assert "(../spec/acceptance.md#settings-of-a-run)" in readme
    assert "| Setting |" not in readme
    assert "\n### Settings of a run\n" in (SPEC / "acceptance.md").read_text(encoding="utf-8")
