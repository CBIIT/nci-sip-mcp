"""The project's specification, as data: the conventions, the records and the required tools
(`spec/`), and the profiles that serve them.

`requirements.py` reads the requirements beside them, and `document.py` renders them all
as the readable specification.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, NamedTuple

import yaml

SPEC = Path(__file__).parents[3] / "spec"


def _load(name: str) -> dict[str, Any]:
    return yaml.safe_load((SPEC / name).read_text(encoding="utf-8"))


CONVENTIONS: dict[str, dict[str, Any]] = _load("conventions.yaml")
TOOLS: dict[str, dict[str, Any]] = _load("tools.yaml")
RECORDS: dict[str, dict[str, Any]] = _load("records.yaml")
# The furnished resources and prompt templates (spec/resources.yaml, spec/prompts.yaml).
RESOURCES: dict[str, dict[str, Any]] = _load("resources.yaml")
PROMPTS: dict[str, dict[str, Any]] = _load("prompts.yaml")
# The group of each required tool: evs, cadsr, cross-domain or workflow.
REQUIRED_TOOLS = {name: tool["group"] for name, tool in TOOLS.items()}


# The profiles of M1.5: one per module, serving its own group, and unified, serving all.
PROFILES = ("evs", "cadsr", "unified")


def profile_tools(profile: str) -> set[str]:
    """The required tools a server of `profile` lists."""

    return {name for name, group in REQUIRED_TOOLS.items() if profile in (group, "unified")}


def prompts_of(profile: str) -> dict[str, dict[str, Any]]:
    """The prompts a server of `profile` lists: those whose every tool the profile has (M5.1)."""

    tools = profile_tools(profile)
    return {name: prompt for name, prompt in PROMPTS.items() if set(prompt["tools"]) <= tools}


def resources_of(profile: str) -> dict[str, dict[str, Any]]:
    """The resources a server of `profile` serves: those of its group, unified serving all."""

    return {
        name: resource
        for name, resource in RESOURCES.items()
        if profile in (resource["group"], "unified")
    }


# A word that can be a tool's name: lowercase parts joined by underscores (A2.1).
WORD = re.compile(r"(?<!\w)[a-z]+(?:_[a-z]+)+(?!\w)")


def tools_named(text: str) -> list[str]:
    """The required tools `text` names, each once, in the order it first names them."""

    return list(dict.fromkeys(word for word in WORD.findall(text) if word in TOOLS))


def uri_variables(template: str) -> list[str]:
    """The variables of a URI template, in order: {release} and {code} of a concept's."""

    return re.findall(r"\{(\w+)\}", template)


class Listing(NamedTuple):
    """What resources/list and resources/templates/list name: the concrete resources, which have
    no variable in their URI, and the URI templates, which have (M5.1)."""

    uris: set[str]
    templates: set[str]


def resources_listed(profile: str) -> Listing:
    """What a server of `profile` lists of the resources it serves."""

    stated = [uri for resource in resources_of(profile).values() for uri in resource["uri"]]
    return Listing(
        {uri for uri in stated if not uri_variables(uri)},
        {uri for uri in stated if uri_variables(uri)},
    )


def resource_call(resource: str, template: str, values: dict[str, str]) -> tuple[str, dict]:
    """The tool a resource's content equals and its arguments for `template` filled with
    `values`: an argument whose variable the template lacks is left out."""

    entry, taken = RESOURCES[resource], set(uri_variables(template))
    arguments = {
        name: _filled(value, values)
        for name, value in entry["arguments"].items()
        if _variables_of(value) <= taken
    }
    return entry["tool"], arguments


def _variables_of(value: Any) -> set[str]:
    return set(uri_variables(value)) if isinstance(value, str) else set()


def _filled(value: Any, values: dict[str, str]) -> Any:
    return value.format_map(values) if isinstance(value, str) else value


def defaults(tool: str) -> dict[str, Any]:
    """The default of each optional argument of `tool` the specification states: those under
    `defaults`, and each bound's."""

    entry = TOOLS[tool]
    bounds = entry.get("bounds", {}).items()
    bounded = {name: bound["default"] for name, bound in bounds if "default" in bound}
    return bounded | entry.get("defaults", {})


def items_of(tool: str, result: Any) -> list[Any]:
    """The items of a result of `tool`, where its `items` in tools.yaml say they are; a part
    of the path the result lacks contributes none."""

    return [item for path in TOOLS[tool].get("items", []) for item in items_at(result, path)]


def items_at(value: Any, path: str) -> list[Any]:
    """Items at one specification path, preserving its independent provenance obligation."""

    found = [value]
    for step in filter(None, path.split(".")):
        found = _step(found, step)
    return [each for each in found if each is not None]


def _step(found: list[Any], step: str) -> list[Any]:
    """One step of a path: a field of each value, and with [] the elements of each list."""

    found = [_field(each, step.removesuffix("[]")) for each in found]
    if not step.endswith("[]"):
        return found
    return [item for each in found if isinstance(each, list) for item in each]


def _field(value: Any, key: str) -> Any:
    return value.get(key) if isinstance(value, dict) else None


def _split(text: str, separator: str) -> list[str]:
    """`text` split at each `separator` outside brackets and braces, the parts stripped."""

    parts, depth, start = [], 0, 0
    for index, character in enumerate(text):
        depth += (character in "[{") - (character in "]}")
        if character == separator and depth == 0:
            parts.append(text[start:index])
            start = index + 1
    return [part.strip() for part in [*parts, text[start:]] if part.strip()]


def _names(alternative: str) -> list[str]:
    """The parameter names of one alternative: a name, or `{ a, b }`, a set of them."""

    if alternative.startswith("{"):
        return [name for part in _split(alternative[1:-1], ",") for name in _names(part)]
    return re.findall(r"\w+", alternative)[:1]


def parameters(tool: str) -> tuple[set[str], set[str]]:
    """The parameters `tool` takes, by `inputs` in tools.yaml, and those it requires.

    `inputs` reads `(a, b?, c | d, e[]?, f[{ g, h? }])`: a trailing ? marks an optional
    parameter, | alternatives, none of which is required, and brackets a list.
    """

    names: set[str] = set()
    required: set[str] = set()
    for part in _split(TOOLS[tool]["inputs"].strip()[1:-1], ","):
        alternatives = _split(part, "|")
        found = [name for alternative in alternatives for name in _names(alternative)]
        names.update(found)
        if len(alternatives) == 1 and not part.endswith("?"):
            required.update(found)
    return names, required


def alternatives(tool: str, name: str) -> set[str]:
    """The parameters `tool` takes in place of `name` (`a | b`); none where it has no
    alternative."""

    for part in _split(TOOLS[tool]["inputs"].strip()[1:-1], ","):
        found = [set(_names(alternative)) for alternative in _split(part, "|")]
        if any(name in each for each in found):
            return set().union(*(each for each in found if name not in each))
    return set()


def is_basis(name: str) -> bool:
    """Whether `name` is a convention (a section such as A6, or a rule such as A3.6.1, or a
    subsection such as A3.6 above rules), a record (provenance) or a required tool."""

    return name in CONVENTIONS or name in RECORDS or name in TOOLS or _is_rule(name)


def _is_rule(name: str) -> bool:
    rules = {rule for section in CONVENTIONS.values() for rule in section["rules"]}
    return any(rule == name or rule.startswith(f"{name}.") for rule in rules)
