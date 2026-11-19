"""The readable specification, rendered from `spec/`, its source of record.

    pdm run spec-render

writes `docs/specification.md`: an introduction, the conventions, the required tools, the
resources and prompts, the requirements with the suite tests that cite each, and the
acceptance rules. The prose parts are `spec/introduction.md`, `spec/prompts.md` and
`spec/acceptance.md`; everything else comes from the data. A self-test fails when the file is
not current. A Word copy is built from it with pandoc where one is wanted; neither is edited
by hand.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import pytest

from nci_sip_acceptance.requirements import citations, load_requirements, repo_path
from nci_sip_acceptance.spec import CONVENTIONS, PROMPTS, RECORDS, RESOURCES, SPEC, TOOLS

DOCUMENT = SPEC.parent / "docs" / "specification.md"
SUITE = Path(__file__).parents[2] / "tests"
GROUPS = {"evs": "EVS", "cadsr": "caDSR", "cross-domain": "Cross-domain", "workflow": "Workflow"}
# The requirements, in the order the suite runs them: the gates, the tests every
# content-returning tool takes, and each tool's own, by group.
SECTIONS = {
    "P": "Protocol gates: once per server, before any tool test",
    "X": "Cross-cutting: against every content-returning tool",
    **{group: f"{title} tools" for group, title in GROUPS.items()},
}


def _text(name: str) -> str:
    """A hand-written part of the document: the introduction, and the acceptance rules."""

    return (SPEC / name).read_text(encoding="utf-8")


def cell(text: str) -> str:
    return " ".join(str(text).split()).replace("|", "\\|")


def _conventions() -> list[str]:
    lines = ["## 1. Conventions", "", "Binding on every tool.", ""]
    for key, section in CONVENTIONS.items():
        rows = [f"| {rule} | {cell(text)} |" for rule, text in section["rules"].items()]
        lines += [f"### {key} · {section['title']}", "", "| Id | Convention |", "|---|---|"]
        lines += [*rows, ""]
        lines += [f"*Why {rule}.* {cell(text)}\n" for rule, text in section.get("why", {}).items()]
    return lines + [line for record in RECORDS.values() for line in _record(record)]


def _values(field: dict[str, Any]) -> str:
    values = field.get("values")
    listed = f": one of {', '.join(f'`{value}`' for value in values)}" if values else ""
    sets = field.get("exclusions", {})
    excluded = "".join(
        f"; exclusion set of {key}: {', '.join(codes)}" for key, codes in sets.items()
    )
    forms = "".join(
        f"; the {name} form `{text.partition(':')[0]}`: {cell(text.partition(':')[2])}"
        for name, text in field.get("forms", {}).items()
    )
    return listed + excluded + forms + (" (optional)" if field.get("optional") else "")


def _record(record: dict[str, Any]) -> list[str]:
    """A record of the specification as a table of its fields."""

    rows = [
        f"| `{name}` | {cell(field['content'])}{_values(field)} | {field['of']} |"
        for name, field in record["fields"].items()
    ]
    header = [f"### {record['title']}", "", cell(record["about"]), ""]
    table = [*header, "| Field | Content | Rule |", "|---|---|---|", *rows, ""]
    return [*table, *_detail_keys(record.get("detail_keys", {}))]


def _detail_keys(keys: dict[str, dict[str, str]]) -> list[str]:
    """The keys of `details` for each code of an error record, each with its meaning."""

    if not keys:
        return []
    rows = [
        f"| `{code}` | "
        + ("; ".join(f"`{key}`: {cell(text)}" for key, text in each.items()) or "none")
        + " |"
        for code, each in keys.items()
    ]
    return ["The keys of `details` for each code:", "", "| Code | Keys |", "|---|---|", *rows, ""]


def _bound(bound: dict[str, Any]) -> str:
    default = f"default {bound['default']}, " if "default" in bound else ""
    return f"{default}at most {bound['maximum']}"


def _limits(tool: dict[str, Any]) -> str:
    """What bounds a tool's call: its bounded arguments, stated defaults, request bound and
    what it does not offer."""

    limits = "".join(
        f" `{argument}`: {_bound(bound)}." for argument, bound in tool.get("bounds", {}).items()
    )
    limits += "".join(
        f" `{argument}`: default {value}." for argument, value in tool.get("defaults", {}).items()
    )
    limits += f" At most {tool['requests']} upstream requests a call." if "requests" in tool else ""
    limits += (
        " Computed from caller-supplied values: ttlMs 0, private." if tool.get("computed") else ""
    )
    limits += "".join(
        f" `{argument}`: at most {count} a call."
        for argument, count in tool.get("lists", {}).items()
    )
    limits += "".join(
        f" Not offered, `{key}`: {', '.join(values)}."
        for key, values in tool.get("not_offered", {}).items()
    )
    return limits + _forms(tool)


def _forms(tool: dict[str, Any]) -> str:
    """Identifier forms, free-text arguments and input work limits (A7.6, A7.7)."""

    forms = "".join(
        f" `{argument}` form{_by_terminology(form)}."
        for argument, form in tool.get("patterns", {}).items()
    )
    texts = tool.get("free_text", [])
    forms += f" Free text: {', '.join(f'`{path}`' for path in texts)}." if texts else ""
    forms += "".join(
        f" `{path}`: at most {bound['maxLength']} Unicode code points. Why: {cell(bound['why'])}"
        for path, bound in tool.get("text_limits", {}).items()
    )
    for key, label, maximum, unit in (
        ("encoded_target", "Encoded request target (path and query)", "maximumBytes", "bytes"),
        ("lexical_tokens", "Local lexical token prefix", "maximum", "tokens"),
    ):
        if bound := tool.get(key):
            forms += f" {label}: at most {bound[maximum]} {unit}. Why: {cell(bound['why'])}"
    return forms


def _by_terminology(form: str | dict[str, str]) -> str:
    if isinstance(form, str):
        return f" `{form}`"
    return "".join(f" for {terminology} `{pattern}`" for terminology, pattern in form.items())


def _tool_row(name: str, tool: dict[str, Any]) -> str:
    values = "".join(
        f" `{argument}`: {', '.join(choices)}."
        for argument, choices in tool.get("values", {}).items()
    )
    signature = cell(f"{tool['inputs']} → {tool['returns']}")
    items = (
        f" Items: {', '.join(f'`{path}`' for path in tool['items'])}." if "items" in tool else ""
    )
    return (
        f"| {cell(tool['title'])}<br>`{name}` | `{signature}` | "
        f"{cell(tool['summary'])}{values}{_limits(tool)}{items} |"
    )


def _tools() -> list[str]:
    lines = ["## 2. Tools", ""]
    for group, title in GROUPS.items():
        rows = [_tool_row(name, tool) for name, tool in TOOLS.items() if tool["group"] == group]
        lines += [f"### {title} tools", "", "| Tool | Inputs → result | What it does |"]
        lines += ["|---|---|---|", *rows, ""]
    return lines


def _argument(value: Any) -> str:
    return f"[{', '.join(value)}]" if isinstance(value, list) else str(value)


def _resource_row(resource: dict[str, Any]) -> str:
    templates = "<br>".join(f"`{uri}`" for uri in resource["uri"])
    arguments = ", ".join(f"{name}: {_argument(v)}" for name, v in resource["arguments"].items())
    selects = resource.get("selects")
    chosen = f" `{', '.join(selects)}` names which release." if selects else ""
    return (
        f"| {resource['title']} | {templates} | {resource['group']} | `{resource['mime']}` "
        f"| `{resource['tool']}({arguments})` | {cell(resource['summary'])}{chosen} |"
    )


def _prompt(name: str, prompt: dict[str, Any]) -> list[str]:
    arguments = ", ".join(
        f"`{each['name']}`{'' if each['required'] else ' (optional)'}"
        for each in prompt["arguments"]
    )
    header = [f"#### `{name}`: {prompt['title']}", "", cell(prompt["adds"]), ""]
    header += [
        f"Arguments: {arguments}.",
        f"The tools it names, in order: {_names(prompt['tools'])}.",
        "",
    ]
    lines = [f"    {line}".rstrip() for line in prompt["template"].rstrip().splitlines()]
    return [*header, *lines, ""]


def _names(tools: list[str]) -> str:
    return ", ".join(f"`{tool}`" for tool in tools)


def _resources_and_prompts() -> list[str]:
    lines = ["## 3. Prompts and resources", "", "### Resources", ""]
    lines += ["Listed by resources/list and resources/templates/list (M5.1); each is read with"]
    lines += ["resources/read, and its content is compared with the answer of the tool it names on"]
    lines += ["identity and release, not section by section.", ""]
    lines += ["| Resource | URI templates | Group | MIME type | Compared with | What it is |"]
    lines += ["|---|---|---|---|---|---|"]
    lines += [_resource_row(resource) for resource in RESOURCES.values()]
    lines += ["", "### Prompts", ""]
    lines += ["Listed by prompts/list (M5.1).", ""]
    lines += [_text("prompts.md"), ""]
    return lines + [line for name, prompt in PROMPTS.items() for line in _prompt(name, prompt)]


def _status(key: str, entry: dict[str, Any], tests: dict[str, list[str]]) -> str:
    cited = ", ".join(f"`{repo_path(test)}`" for test in tests.get(key, []))
    planned = f"planned {entry['planned']}" if "planned" in entry else ""
    return " | ".join([cited or "—", planned or "tested"])


def _section(key: str) -> str:
    """The section a requirement belongs to: P, X, or its tool's group."""

    prefix = key.rpartition("-")[0]
    return prefix if prefix in ("P", "X") else TOOLS[prefix]["group"]


def _requirements(cited: dict[str, tuple[str, ...]]) -> list[str]:
    tests: dict[str, list[str]] = {}
    for test, keys in cited.items():
        for key in keys:
            tests.setdefault(key, []).append(test)
    requirements = load_requirements()
    lines = ["## 4. Requirements", ""]
    for section, title in SECTIONS.items():
        rows = [
            f'| <a id="requirement-{key}"></a>{key} | '
            f"{cell(entry['statement'])} | {', '.join(entry['basis'])} "
            f"| {_status(key, entry, tests)} |"
            for key, entry in requirements.items()
            if _section(key) == section
        ]
        lines += [f"### {title}", "", "| Id | Requirement | Basis | Tests | Status |"]
        lines += ["|---|---|---|---|---|", *rows, ""]
    return lines


def render(cited: dict[str, tuple[str, ...]]) -> str:
    """The specification, given the requirements each suite test cites."""

    lines = [_text("introduction.md"), *_conventions(), *_tools(), *_resources_and_prompts()]
    lines += _requirements(cited)
    lines.append(_text("acceptance.md"))
    return "\n".join(lines).rstrip() + "\n"


class _Collected:
    """A pytest plugin that keeps the collected items."""

    items: list[pytest.Item]

    def pytest_collection_finish(self, session: pytest.Session) -> None:
        self.items = session.items


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render the specification from spec/.")
    parser.add_argument("--output", type=Path, default=DOCUMENT, help="where to write it")
    options = parser.parse_args(arguments)
    collected = _Collected()
    code = pytest.main([str(SUITE), "--collect-only", "-q", "-p", "no:cacheprovider"], [collected])
    if code != pytest.ExitCode.OK:
        return int(code)
    options.output.write_text(render(citations(collected.items)), encoding="utf-8")
    sys.stdout.write(f"Wrote {options.output}.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
