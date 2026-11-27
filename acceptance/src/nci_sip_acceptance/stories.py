"""Render a domain-readable story for every collected MCP acceptance case.

The authored narratives and explicit function assignments live in acceptance/stories.yaml.
Collection supplies exact variants, source locations and requirement citations; it runs no
server or test. The self-tests reject omissions, stale references and a stale generated guide.
"""

from __future__ import annotations

import argparse
import html
import re
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml

from nci_sip_acceptance.requirements import load_requirements, repo_path

ROOT = Path(__file__).parents[2]
SOURCE = ROOT / "stories.yaml"
DOCUMENT = ROOT.parent / "docs" / "behavioural-tests.md"
TEXT_FIELDS = ("id", "title", "goal", "given", "when", "then")


@dataclass(frozen=True)
class Case:
    key: str
    function: str
    line: int
    requirements: tuple[str, ...]


def cases_of(items: list[pytest.Item]) -> list[Case]:
    cases = []
    for item in items:
        path = item.path.relative_to(ROOT).as_posix()
        key = path + "::" + item.nodeid.partition("::")[2]
        requirements = tuple(key for mark in item.iter_markers("requirement") for key in mark.args)
        line = item.location[1]
        if line is None:
            raise ValueError(f"Missing source location: {key}")
        cases.append(Case(key, key.partition("[")[0], line + 1, requirements))
    return sorted(cases, key=lambda case: case.key)


def _nonempty(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _story_problem(story: dict) -> str | None:
    if set(story) != {*TEXT_FIELDS, "tests"}:
        return "needs exactly id, title, goal, given, when, then and tests"
    if not all(_nonempty(story[field]) for field in TEXT_FIELDS):
        return "needs nonempty narrative fields"
    if not re.fullmatch(r"[a-z][a-z0-9-]*", story["id"]):
        return "needs a lowercase hyphenated id"
    return _tests_problem(story["tests"])


def _tests_problem(tests: object) -> str | None:
    if not isinstance(tests, dict) or not tests:
        return "needs explicitly named tests"
    if not all(_nonempty(key) and _nonempty(text) for key, text in tests.items()):
        return "needs a nonempty description for each test"
    return None


def _validate_story(story: object) -> None:
    if not isinstance(story, dict):
        raise ValueError("Each story must be a mapping")
    if problem := _story_problem(story):
        raise ValueError(f"Story {story.get('id', '?')}: {problem}")


def load_stories(path: Path = SOURCE) -> list[dict]:
    stories = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(stories, list) or not stories:
        raise ValueError("The story catalogue must be a nonempty list")
    for story in stories:
        _validate_story(story)
    ids = [story["id"] for story in stories]
    if len(ids) != len(set(ids)):
        raise ValueError("Story ids must be unique")
    return stories


def _assignment_problems(stories: list[dict], cases: list[Case]) -> list[str]:
    assignments: Counter[str] = Counter()
    for story in stories:
        assignments.update(story["tests"].keys())
    collected = {case.function for case in cases}
    problems = [f"Undocumented test: {name}" for name in sorted(collected - assignments.keys())]
    problems += [f"Stale test reference: {name}" for name in sorted(assignments.keys() - collected)]
    problems += [
        f"Several stories claim: {name}" for name, count in assignments.items() if count != 1
    ]
    return problems


def _requirement_problems(cases: list[Case]) -> list[str]:
    known = load_requirements()
    return [
        f"Missing or unknown requirements: {case.key}"
        for case in cases
        if not case.requirements or set(case.requirements) - known.keys()
    ]


def validate(stories: list[dict], cases: list[Case]) -> None:
    """Every collected function belongs to exactly one story, with no catch-all patterns."""
    if not cases or len({case.key for case in cases}) != len(cases):
        raise ValueError("Expected a nonempty collection of unique cases")
    if problems := _assignment_problems(stories, cases) + _requirement_problems(cases):
        raise ValueError("\n".join(problems))


def _text(text: str) -> str:
    return html.escape(" ".join(text.split()), quote=False).replace("|", "&#124;")


def _test_link(case: Case) -> str:
    return f"../acceptance/{case.function.partition('::')[0]}#L{case.line}"


def _example_rows(story: dict, cases: list[Case]) -> list[str]:
    rows = []
    for function, description in story["tests"].items():
        variants = [case for case in cases if case.function == function]
        requirements = sorted({key for case in variants for key in case.requirements})
        basis = ", ".join(f"[`{key}`](specification.md#requirement-{key})" for key in requirements)
        rows.append(
            f"| {_text(description)} | {len(variants)} | {basis} | "
            f"[Test]({_test_link(variants[0])}) |"
        )
    return rows


def _section(story: dict, cases: list[Case]) -> list[str]:
    lines = [f'<a id="{story["id"]}"></a>', "", f"## {_text(story['title'])}", ""]
    for label, key in (
        ("User goal", "goal"),
        ("Given", "given"),
        ("When", "when"),
        ("Then", "then"),
    ):
        lines += [f"**{label}:** {_text(story[key])}", ""]
    lines += ["| Behaviour checked | Cases | Requirements | Evidence |", "|---|---:|---|---|"]
    lines += _example_rows(story, cases)
    lines += ["", "<details>", f"<summary>Exact executable cases ({len(cases)})</summary>", ""]
    lines += [
        f"- <code>{html.escape(repo_path(case.key))}</code> — [source]({_test_link(case)})"
        for case in cases
    ]
    return [*lines, "", "</details>", ""]


def render(stories: list[dict], cases: list[Case]) -> str:
    validate(stories, cases)
    assigned = [
        (story, [case for case in cases if case.function in story["tests"]]) for story in stories
    ]
    lines = [
        "# Behavioural tests: the complete story catalogue",
        "",
        "<!-- Generated by pdm run acceptance-stories; edit acceptance/stories.yaml. -->",
        "",
        f"**{len(cases)} MCP acceptance cases, "
        f"{len({case.function for case in cases})} test functions, "
        f"{len(stories)} user stories. Every case is assigned to exactly one story.**",
        "",
        "Start with a domain task below. Each story gives the user's goal, the starting situation "
        "(Given), the action (When), and the expected answer (Then). Its examples explain every "
        "test function in plain language; the Cases column counts its parameter variants. "
        "Expand the evidence section for every exact test ID and a source link.",
        "",
        "An NCIt concept is a coded terminology entry. A caDSR data element defines a data item "
        "and its permitted values. Provenance names where an answer came from and its version. "
        "Truncation means a limit prevented a complete answer; it is different from no matches.",
        "",
        "The [specification and requirement-to-test map](specification.md#4-requirements) remain "
        "the authority for behaviour. Stories explain the tests; they do not add requirements. "
        "The same shared rule is exercised across different tools and inputs, so a story may "
        "cover many cases.",
        "",
        "This is an inventory, not a passing test report. "
        "[Run reports](../acceptance/README.md#the-report) "
        "say what passed, failed or did not run. Fixture cases include recorded responses and "
        "contract-crafted scenarios; a caDSR credential has been issued for staging only, with its "
        "protected access still unverified; see the "
        "[dated access evidence](functional-status.md). A fixture result does "
        "not establish live access, clinical suitability or production search quality. "
        "Implementation unit tests and harness self-tests are outside this MCP catalogue.",
        "",
        "| User story | Cases |",
        "|---|---:|",
    ]
    lines += [
        f"| [{_text(story['title'])}](#{story['id']}) | {len(group)} |" for story, group in assigned
    ]
    lines += [""]
    for story, group in assigned:
        lines.extend(_section(story, group))
    return "\n".join(lines).rstrip() + "\n"


class Collection:
    def __init__(self) -> None:
        self.cases: list[Case] = []

    def pytest_collection_finish(self, session: pytest.Session) -> None:
        self.cases = cases_of(session.items)


def collect(collection: Collection) -> int:
    # A user's live/profile/filter settings must not silently produce a partial catalogue.
    with pytest.MonkeyPatch.context() as environment:
        environment.setenv("NCI_SIP_ACCEPTANCE_MODE", "fixture")
        environment.setenv("NCI_SIP_ACCEPTANCE_PROFILE", "unified")
        environment.delenv("PYTEST_ADDOPTS", raising=False)
        return int(
            pytest.main(
                [
                    str(ROOT / "tests"),
                    "--collect-only",
                    "-qq",
                    "-o",
                    "addopts=",
                    "-p",
                    "no:cacheprovider",
                ],
                [collection],
            )
        )


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DOCUMENT)
    parser.add_argument("--check", action="store_true", help="fail if the generated guide is stale")
    options = parser.parse_args(arguments)
    collection = Collection()
    if code := collect(collection):
        return code
    rendered = render(load_stories(), collection.cases)
    if options.check:
        if not options.output.is_file() or options.output.read_text(encoding="utf-8") != rendered:
            sys.stderr.write("Story catalogue is stale: run pdm run acceptance-stories.\n")
            return 1
    else:
        options.output.write_text(rendered, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
