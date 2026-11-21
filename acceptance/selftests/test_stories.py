"""The domain catalogue must document the collected suite, never just an old baseline."""

import html
import os
import re
import subprocess
import sys
from dataclasses import replace

import pytest
import yaml

from nci_sip_acceptance import document, stories
from nci_sip_acceptance.requirements import load_requirements

pytest_plugins = ["pytester"]

FUNCTION = "tests/test_example.py::test_pinned"
STORY = {
    "id": "pinned-content",
    "title": "Keep the selected release",
    "goal": "As a curator, I want a consistent release.",
    "given": "Two source releases exist.",
    "when": "I ask for one of them.",
    "then": "The answer identifies that release.",
    "tests": {FUNCTION: "The returned item names the requested release."},
}
CASES = [stories.Case(f"{FUNCTION}[{mode}]", FUNCTION, 20, ("X-1",)) for mode in ("a", "b")]


def unresolved_uat_nodes(document, cases):
    linked = set(re.findall(r"`acceptance/(tests/[^`]+::[^`]+)`", document))
    collected = {case.function for case in cases} | {case.key for case in cases}
    return linked - collected


def source(tmp_path, value):
    path = tmp_path / "stories.yaml"
    path.write_text(yaml.safe_dump(value), encoding="utf-8")
    return path


def test_every_actual_case_is_documented_and_the_generated_guide_is_current(pytester, monkeypatch):
    monkeypatch.setenv("NCI_SIP_ACCEPTANCE_MODE", "fixture")
    monkeypatch.setenv("NCI_SIP_ACCEPTANCE_PROFILE", "unified")
    items, _ = pytester.inline_genitems(str(stories.ROOT / "tests"), "-p", "no:cacheprovider")
    cases = stories.cases_of(items)
    rendered = stories.render(stories.load_stories(), cases)

    assert cases
    uat = (stories.ROOT.parent / "docs/user-acceptance.md").read_text(encoding="utf-8")
    assert re.findall(r"`acceptance/tests/[^`]+::[^`]+`", uat)
    assert unresolved_uat_nodes(uat, cases) == set()
    invalid = "tests/test_missing.py::test_does_not_exist"
    assert unresolved_uat_nodes(uat + f"\n`acceptance/{invalid}`", cases) == {invalid}
    assert stories.DOCUMENT.read_text(encoding="utf-8") == rendered
    assert {f"acceptance/{case.key}" for case in cases} == {
        html.unescape(line.split("<code>", 1)[1].split("</code>", 1)[0])
        for line in rendered.splitlines()
        if line.startswith("- <code>")
    }


def test_multiple_parameter_cases_share_a_story_with_exact_evidence():
    rendered = stories.render([STORY], CASES)

    assert "2 MCP acceptance cases, 1 test functions, 1 user stories" in rendered
    assert (
        "| The returned item names the requested release. | 2 | "
        "[`X-1`](specification.md#requirement-X-1) |" in rendered
    )
    assert '<a id="pinned-content"></a>\n\n## Keep the selected release' in rendered
    assert "../acceptance/tests/test_example.py#L20" in rendered
    assert "<summary>Exact executable cases (2)</summary>" in rendered
    for case in CASES:
        # From docs/ a bare tests/ path would read as the unit suite; the suite is acceptance/.
        assert rendered.count(f"<code>acceptance/{case.key}</code>") == 1


def test_every_requirement_link_has_one_authoritative_destination():
    keys = tuple(load_requirements())
    rendered = stories.render([STORY], [replace(CASES[0], requirements=keys)])
    specification = document.render({FUNCTION: keys})

    for key in keys:
        assert rendered.count(f"[`{key}`](specification.md#requirement-{key})") == 1
        assert specification.count(f'<a id="requirement-{key}"></a>{key} |') == 1


@pytest.mark.parametrize(
    ("catalogue", "cases", "message"),
    [
        ([], CASES, "Undocumented test"),
        (
            [STORY | {"tests": {"tests/test_old.py::test_old": "Removed example"}}],
            CASES,
            "Stale test reference",
        ),
        ([STORY, STORY | {"id": "another-story"}], CASES, "Several stories claim"),
        ([STORY], [], "nonempty collection"),
        ([STORY], [CASES[0], CASES[0]], "unique cases"),
        ([STORY], [replace(CASES[0], requirements=())], "Missing or unknown requirements"),
        ([STORY], [replace(CASES[0], requirements=("X-999",))], "Missing or unknown requirements"),
    ],
)
def test_incomplete_or_ambiguous_mapping_is_rejected(catalogue, cases, message):
    with pytest.raises(ValueError, match=message):
        stories.render(catalogue, cases)


@pytest.mark.parametrize(
    ("catalogue", "message"),
    [
        ([], "nonempty list"),
        ({}, "nonempty list"),
        (["not a record"], "must be a mapping"),
        ([STORY | {"extra": "ignored"}], "needs exactly"),
        ([STORY | {"goal": " "}], "nonempty narrative"),
        ([STORY | {"id": 'bad" id'}], "hyphenated id"),
        ([STORY | {"tests": []}], "explicitly named tests"),
        ([STORY | {"tests": {}}], "explicitly named tests"),
        ([STORY | {"tests": {FUNCTION: " "}}], "description for each test"),
        ([STORY, STORY], "ids must be unique"),
    ],
)
def test_incomplete_narratives_cannot_be_loaded(tmp_path, catalogue, message):
    with pytest.raises(ValueError, match=message):
        stories.load_stories(source(tmp_path, catalogue))


def test_authored_text_and_hostile_parameter_ids_are_readable_not_active_markup(tmp_path):
    story = STORY | {"tests": {FUNCTION: 'Return <script> | "value" & text intact.'}}
    case = replace(CASES[0], key=f"{FUNCTION}[<script>alert(1)</script>|value]")
    loaded = stories.load_stories(source(tmp_path, [story]))
    rendered = stories.render(loaded, [case])

    assert loaded == [story]
    assert "<script>" not in rendered
    assert 'Return &lt;script&gt; &#124; "value" &amp; text intact.' in rendered
    assert f"<code>acceptance/{html.escape(case.key)}</code>" in rendered


def test_new_parameter_case_changes_the_document_and_new_function_needs_a_description():
    extra = replace(CASES[0], key=f"{FUNCTION}[new]")
    rendered = stories.render([STORY], [*CASES, extra])

    assert rendered != stories.render([STORY], CASES)
    assert "3 MCP acceptance cases" in rendered
    with pytest.raises(ValueError, match=r"Undocumented test: tests/test_example\.py::test_new"):
        stories.render(
            [STORY], [*CASES, replace(extra, function="tests/test_example.py::test_new")]
        )


def test_failed_collection_never_overwrites_the_existing_document(tmp_path, monkeypatch):
    output = tmp_path / "guide.md"
    output.write_text("existing guide\n", encoding="utf-8")
    monkeypatch.setattr(stories, "collect", lambda _collection: int(pytest.ExitCode.USAGE_ERROR))

    assert stories.main(["--output", str(output)]) == pytest.ExitCode.USAGE_ERROR
    assert output.read_text(encoding="utf-8") == "existing guide\n"


@pytest.mark.parametrize("existing", [None, "stale guide\n", "current"])
def test_check_mode_reports_staleness_without_rewriting(tmp_path, monkeypatch, existing, capsys):
    output = tmp_path / "guide.md"
    current = stories.render([STORY], CASES)
    if existing is not None:
        output.write_text(current if existing == "current" else existing, encoding="utf-8")

    def collect(collection):
        collection.cases = CASES
        return 0

    monkeypatch.setattr(stories, "collect", collect)
    monkeypatch.setattr(stories, "load_stories", lambda: [STORY])

    result = stories.main(["--output", str(output), "--check"])

    assert result == (0 if existing == "current" else 1)
    assert output.exists() is (existing is not None)
    if existing is not None:
        assert output.read_text(encoding="utf-8") == (
            current if existing == "current" else existing
        )
    assert ("run pdm run acceptance-stories" in capsys.readouterr().err) is (result == 1)


def test_command_writes_the_complete_guide_even_with_profile_or_pytest_filters(tmp_path):
    output = tmp_path / "guide.md"
    environment = os.environ | {
        "NCI_SIP_ACCEPTANCE_PROFILE": "cadsr",
        "NCI_SIP_ACCEPTANCE_MODE": "live",
        "PYTEST_ADDOPTS": "-k no_tests_match_this_expression",
    }

    completed = subprocess.run(  # noqa: S603 - same interpreter, suite module, no server execution
        [sys.executable, "-m", "nci_sip_acceptance.stories", "--output", str(output)],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert output.read_text(encoding="utf-8") == stories.DOCUMENT.read_text(encoding="utf-8")
