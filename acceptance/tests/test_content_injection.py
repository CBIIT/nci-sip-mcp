"""X-29: source instructions survive projection without entering instruction channels."""

import json

import pytest

from nci_sip_acceptance.spec import prompts_of

CASES = {
    "evs": ("get_concept", {"terminology": "ncit", "code": "C4817", "include": ["definitions"]}),
    "cadsr": ("get_data_element", {"publicId": "2200604"}),
    "cross-domain": ("find_data_elements_for_concept", {"conceptCode": "C17357"}),
    "workflow": ("ground_value", {"conceptCode": "C4817"}),
}


def _strings(value, path=()):
    if isinstance(value, dict):
        return _nested_strings(value.items(), path)
    if isinstance(value, (list, tuple)):
        return _nested_strings(enumerate(value), path)
    return [(path, value)] if isinstance(value, str) else []


def _nested_strings(items, path):
    return [found for key, item in items for found in _strings(item, (*path, key))]


def _instructions(tools, profile):
    prompts = tools.list_prompts().model_dump()
    rendered = {
        name: tools.get_prompt(
            name, {a["name"]: "caller text" for a in prompt["arguments"]}
        ).model_dump()
        for name, prompt in prompts_of(profile).items()
    }
    return tools.list_again().model_dump(), prompts, rendered


def _expected(group, recorded):
    files = {
        "evs": "concepts/C4817.json",
        "cadsr": "data-element-2200604.json",
        "cross-domain": "data-elements-c17357.json",
        "workflow": "values-c4817.json",
    }
    body = recorded(f"scenarios/injection/{group}/{files[group]}")["response"]["body"]
    return next(text for _, text in _strings(body) if f"X29-{group}:" in text)


def _data_path(group, content):
    if group == "evs":
        return ("definitions", _definition_index(content), "definition")
    if group == "cadsr":
        return ("definition",)
    field, identifier = {
        "cross-domain": ("dataElements", "10748236"),
        "workflow": ("permissibleValues", "13606049"),
    }[group]
    index = next(
        i for i, item in enumerate(content[field]) if item["dataElement"]["publicId"] == identifier
    )
    return (
        (field, index, "dataElement", "longName")
        if group == "cross-domain"
        else (field, index, "value")
    )


def _definition_index(content):
    return next(i for i, item in enumerate(content["definitions"]) if item["source"] == "NICHD")


@pytest.mark.requirement("X-29")
@pytest.mark.parametrize(
    "group",
    [
        pytest.param(
            group,
            id=group,
            marks=[pytest.mark.scenario(f"injection/{group}"), pytest.mark.tool(tool)],
        )
        for group, (tool, _) in CASES.items()
    ],
)
def test_source_instructions_stay_exactly_in_data_fields(tools, target, pinned, recorded, group):
    # Given hostile upstream text, when its content tool is called, then only its data field
    # and the identical JSON mirror carry that decoded UTF-8 text.
    before = _instructions(tools, target.profile)
    tool, arguments = CASES[group]
    arguments = arguments if group == "cadsr" else arguments | {"release": pinned["release"]}
    result = tools.call(tool, arguments)

    assert not result.is_error, result.content
    expected = _expected(group, recorded)
    found = [(path, text) for path, text in _strings(result.content) if "X29-" in text]
    assert [(path, text.encode("utf-8")) for path, text in found] == [
        (_data_path(group, result.content), expected.encode("utf-8"))
    ]
    assert [json.loads(text) for text in result.texts] == [result.content]
    assert _instructions(tools, target.profile) == before
    assert [text for _, text in _strings(before) if "X29-" in text] == []
