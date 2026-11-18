"""Every requirement is cited by a test or planned in an issue; every test cites requirements."""

import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest
import yaml

from nci_sip_acceptance import document
from nci_sip_acceptance.document import DOCUMENT, GROUPS, render
from nci_sip_acceptance.requirements import citations, load_requirements, never_runs, problems
from nci_sip_acceptance.spec import PROMPTS, REQUIRED_TOOLS, RESOURCES, TOOLS, is_basis

pytest_plugins = ["pytester"]

SUITE_TESTS = Path(__file__).parent.parent / "tests"
REQUIREMENTS = {
    "X-1": {"statement": "a", "basis": ["A3.1"], "planned": "#52"},
    "X-2": {"statement": "b", "basis": ["A3.4"]},
}


def collect_suite(pytester, monkeypatch):
    monkeypatch.setenv("NCI_SIP_ACCEPTANCE_MODE", "fixture")
    # Audit the retained full contract, including the interim suspended requirements.
    monkeypatch.setenv("NCI_SIP_ACCEPTANCE_REGISTRY_RELEASE", "required")
    # A test that needs the operator's prepare step runs where one is given; collecting runs
    # no command, so any will do.
    monkeypatch.setenv("NCI_SIP_ACCEPTANCE_PREPARE", "true")
    items, _ = pytester.inline_genitems(str(SUITE_TESTS), "-p", "no:cacheprovider")
    return items


def test_the_suite_and_the_requirements_agree(pytester, monkeypatch):
    items = collect_suite(pytester, monkeypatch)
    cited = citations(items)
    idle = [item.nodeid for item in items if never_runs(item)]

    assert cited
    assert problems(cited, load_requirements(), idle) == []


def test_a_test_citing_nothing_or_an_unknown_id_and_an_unanswered_requirement_are_named():
    cited = {"t.py::a": (), "t.py::b": ("X-1", "X-9")}

    assert problems(cited, REQUIREMENTS) == [
        "t.py::a cites no requirement",
        "t.py::b cites X-9, which spec/requirements.yaml does not hold",
        "X-2 is neither cited by a test that runs nor planned",
    ]
    assert problems({"t.py::c": ("X-2",)}, REQUIREMENTS) == []
    assert problems({"t.py::c": ("X-2",)}, REQUIREMENTS, idle=["t.py::c"]) == [
        "X-2 is neither cited by a test that runs nor planned"
    ]


def write(tmp_path, requirements):
    path = tmp_path / "requirements.yaml"
    path.write_text(yaml.safe_dump(requirements), encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("entry", "problem"),
    [
        ("text", "has only basis, planned, statement"),
        (
            {"statement": "s", "basis": ["A1"], "sow": "item 5"},
            "has only basis, planned, statement",
        ),
        ({"statement": " ", "basis": ["A1"]}, "states the behaviour"),
        ({"basis": ["A1"]}, "states the behaviour"),
        ({"statement": "s", "basis": []}, "names its basis"),
        ({"statement": "s", "basis": [3]}, "names its basis"),
        ({"statement": "s", "basis": "A1"}, "names its basis"),
        ({"statement": "s", "basis": ["A3.9"]}, "names its basis among the conventions and tools"),
        (
            {"statement": "s", "basis": ["A1", "A3.9"]},
            "names its basis among the conventions and tools",
        ),
        ({"statement": "s", "basis": ["A1"], "planned": "52"}, "is planned in an issue"),
        ({"statement": "s", "basis": ["A1"], "planned": "#52 later"}, "is planned in an issue"),
    ],
)
def test_an_entry_without_statement_basis_or_a_valid_plan_is_refused(tmp_path, entry, problem):
    with pytest.raises(ValueError, match=f"requirements.yaml: X-1 {re.escape(problem)}"):
        load_requirements(write(tmp_path, {"X-1": entry}))


def test_every_problem_is_reported_at_once_and_a_good_file_loads(tmp_path):
    with pytest.raises(ValueError) as refused:
        load_requirements(write(tmp_path, {"X-1": {"basis": ["A1"]}, "X-2": "text"}))

    assert str(refused.value) == (
        "requirements.yaml: X-1 states the behaviour; X-2 has only basis, planned, statement"
    )
    assert load_requirements(write(tmp_path, REQUIREMENTS)) == REQUIREMENTS


SUITE = """
import pytest

pytestmark = pytest.mark.requirement("X-1")


@pytest.mark.requirement("P-1", "P-2")
@pytest.mark.requirement("P-3")
def test_cited():
    pass


@pytest.mark.skip(reason="for good")
@pytest.mark.requirement("P-4")
def test_skipped():
    pass


@pytest.mark.skipif(True, reason="never")
@pytest.mark.requirement("P-6")
def test_skipped_if_true():
    pass


@pytest.mark.skipif(False, reason="runs")
@pytest.mark.requirement("P-7")
def test_skipped_if_false():
    pass


@pytest.mark.xfail
@pytest.mark.requirement("P-99")
def test_expected_to_fail():
    pass
"""


def test_every_test_cites_but_only_one_that_runs_covers(pytester):
    pytester.makepyfile(test_cites=SUITE)
    pytester.makeini("[pytest]\nmarkers =\n    requirement: cites")

    items, _ = pytester.inline_genitems("-p", "no:cacheprovider")
    cited = citations(items)
    idle = sorted(item.nodeid.split("::")[1] for item in items if never_runs(item))

    assert sorted(cited["test_cites.py::test_cited"]) == ["P-1", "P-2", "P-3", "X-1"]
    assert sorted(cited["test_cites.py::test_expected_to_fail"]) == ["P-99", "X-1"]
    assert idle == ["test_expected_to_fail", "test_skipped", "test_skipped_if_true"]


def test_the_specification_document_is_what_spec_renders(pytester, monkeypatch):
    on_disk = DOCUMENT.read_text(encoding="utf-8")

    assert on_disk == render(citations(collect_suite(pytester, monkeypatch)))
    assert (
        '| <a id="requirement-P-1"></a>P-1 | In trusted-local mode tools/list names every tool'
        in on_disk
    )
    assert (
        "*Why A5.7.* NCIt's exclusion relationships are exactly eight roles, R135 to R142."
        in on_disk
    )
    assert (
        "| `servedBy` | Where the answer came from: one of `live`, `cache`, `index`, `fixture`"
        " | A4.1 |" in on_disk
    )
    assert "| `details` | An object holding what the caller needs for its next step," in on_disk
    assert "failed upstream request (optional) | A2.5 |" in on_disk
    # A field's forms, and an input list's maximum, as tools.yaml and records.yaml state them.
    assert (
        "the terminology form `{ terminology, identifier, date }`: the terminology and the"
        " release the call pinned; the registry form `{ registry, identifier?, date? }`:"
        " registry is cadsr;" in on_disk
    )
    assert "`entities`: at most 10 a call." in on_disk
    assert "`values`: at most 10 a call." in on_disk
    computed = "Computed from caller-supplied values: ttlMs 0, private."
    rows = [line for line in on_disk.splitlines() if computed in line]
    assert sorted(row.split("`")[1] for row in rows) == sorted(
        name for name, tool in TOOLS.items() if tool.get("computed")
    )
    assert (
        "`acceptance/tests/test_protocol.py::test_tools_list_names_the_tools_of_the_profile_and_no_other`"
        in on_disk
    )


def test_the_command_writes_the_rendered_specification(tmp_path):
    output = tmp_path / "specification.md"

    completed = subprocess.run(  # noqa: S603 - this interpreter, a module of the suite
        [sys.executable, "-m", "nci_sip_acceptance.document", "--output", str(output)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert output.read_text(encoding="utf-8") == DOCUMENT.read_text(encoding="utf-8")


def test_failed_collection_cannot_replace_the_existing_specification(tmp_path, monkeypatch):
    output = tmp_path / "specification.md"
    output.write_text("existing specification\n", encoding="utf-8")
    monkeypatch.setattr(
        document.pytest, "main", lambda *args, **kwargs: pytest.ExitCode.USAGE_ERROR
    )

    assert document.main(["--output", str(output)]) == pytest.ExitCode.USAGE_ERROR
    assert output.read_text(encoding="utf-8") == "existing specification\n"


def test_the_required_tools_are_the_twenty_nine_of_four_groups_each_rendered():
    # A group the document does not name would leave its tools and requirements out of it.
    assert set(GROUPS) == set(REQUIRED_TOOLS.values())
    assert Counter(REQUIRED_TOOLS.values()) == {
        "evs": 12,
        "cadsr": 10,
        "cross-domain": 4,
        "workflow": 3,
    }


def test_a_tool_row_states_its_identifier_forms_and_free_text():
    rendered = render({})

    assert "`code` form for ncit `^C[1-9][0-9]*$`." in rendered
    assert "`release` form `^[A-Za-z0-9][A-Za-z0-9._-]*$`." in rendered
    assert "Free text: `entities[].name`, `entities[].userTip`" in rendered


@pytest.mark.parametrize(
    ("metadata", "details"),
    [
        ({}, ""),
        (
            {
                "text_limits": {
                    "query": {"maxLength": 123, "why": "Search phrase budget."},
                    "entities[].permissibleValues[]": {
                        "maxLength": 456,
                        "why": "Individual values | not lists.\nReassess body work.",
                    },
                }
            },
            " `query`: at most 123 Unicode code points. Why: Search phrase budget."
            " `entities[].permissibleValues[]`: at most 456 Unicode code points."
            " Why: Individual values \\| not lists. Reassess body work.",
        ),
        (
            {"encoded_target": {"maximumBytes": 6789, "why": "Remeasure URL capacity."}},
            " Encoded request target (path and query): at most 6789 bytes."
            " Why: Remeasure URL capacity.",
        ),
        (
            {"lexical_tokens": {"maximum": 23, "why": "Reassess FTS cost."}},
            " Local lexical token prefix: at most 23 tokens. Why: Reassess FTS cost.",
        ),
    ],
)
def test_tool_details_render_input_work_limits_and_their_reasons(monkeypatch, metadata, details):
    monkeypatch.setitem(
        TOOLS,
        "search_concepts",
        {
            "title": "Search",
            "group": "evs",
            "inputs": "(query)",
            "returns": "results",
            "summary": "Search terms.",
            **metadata,
        },
    )

    rows = [line for line in render({}).splitlines() if "<br>`search_concepts` |" in line]

    assert rows == [
        f"| Search<br>`search_concepts` | `(query) → results` | Search terms.{details} |"
    ]


def test_tool_display_titles_are_rendered_beside_their_programmatic_names():
    rendered = render({})
    assert "| Current terminology release<br>`resolve_release` |" in rendered


def test_the_resources_and_prompts_are_rendered_with_their_templates_and_the_decision():
    rendered = render({})

    assert [uri for each in RESOURCES.values() for uri in each["uri"] if uri not in rendered] == []
    assert [name for name in PROMPTS if f"#### `{name}`" not in rendered] == []
    assert "there is no prompt for grounding a value" in rendered
    assert "Help author the data capture of a protocol from its concepts: {concepts}." in rendered
    assert (
        "`get_concept(terminology: ncit, release: {release}, code: {code}, include: [" in rendered
    )


@pytest.mark.parametrize(
    ("name", "known"),
    [
        ("A6", True),
        ("A3", True),
        ("A3.6", True),
        ("A3.6.1", True),
        ("M2.2", True),
        ("get_concept", True),
        ("provenance", True),
        ("traversal", True),
        ("A3.9", False),
        ("S-5", False),
        ("MCP API §8.2", False),
        ("A", False),
    ],
)
def test_a_basis_is_a_convention_or_a_tool(name, known):
    assert is_basis(name) is known


@pytest.mark.parametrize("key", ["P-x", "Y-1", "get_concpt-1", "P1", "get_concept"])
def test_an_id_is_a_gate_a_cross_cutting_test_or_a_tools_own(tmp_path, key):
    with pytest.raises(ValueError, match=f"{key} is not P-<n>, X-<n> or <required tool>-<n>"):
        load_requirements(write(tmp_path, {key: {"statement": "s", "basis": ["A1"]}}))
