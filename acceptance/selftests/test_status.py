"""The canonical status tables are generated, path-aware and current."""

import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from nci_si_acceptance import status
from nci_si_acceptance.status import BEGIN, END, STATUS, current, render, tool_outcomes

# A gate, and tests of three tools.
ATTRIBUTION = {
    "t.py::gate": (None, True),
    "t.py::resolve": ("resolve_release", False),
    "t.py::concept": ("get_concept", False),
    "t.py::concept_again": ("get_concept", False),
    "t.py::form": ("get_form", False),
}


def test_a_tool_is_not_implemented_when_every_test_of_it_was_skipped_for_want_of_it():
    expected = {
        "t.py::gate": "passed",
        "t.py::resolve": "passed",
        "t.py::concept": "not_implemented",
        "t.py::concept_again": "not_implemented",
        "t.py::form": "failed",
    }

    outcomes = tool_outcomes(expected, ATTRIBUTION)

    assert outcomes["resolve_release"] == "PASS"
    assert outcomes["get_concept"] == "NOT IMPLEMENTED"
    assert outcomes["get_form"] == "FAIL"
    assert outcomes["list_contexts"] == "NO TESTS"


def test_a_failed_gate_fails_a_tool_that_passes():
    expected = dict.fromkeys(ATTRIBUTION, "passed") | {"t.py::gate": "failed"}

    outcomes = tool_outcomes(expected, ATTRIBUTION)

    assert outcomes["resolve_release"] == "FAIL"


def test_an_unrun_gate_in_the_expected_outcomes_leaves_no_tool_passing():
    expected = dict.fromkeys(ATTRIBUTION, "passed") | {"t.py::gate": "skipped"}

    outcomes = tool_outcomes(expected, ATTRIBUTION)

    assert {name: outcomes[name] for name in ("resolve_release", "get_concept", "get_form")} == {
        "resolve_release": "INCOMPLETE",
        "get_concept": "INCOMPLETE",
        "get_form": "INCOMPLETE",
    }
    assert outcomes["list_contexts"] == "NO TESTS"


def test_a_gate_that_does_not_run_live_by_design_leaves_tools_passing():
    expected = dict.fromkeys(ATTRIBUTION, "passed") | {"t.py::gate": "not_live"}

    outcomes = tool_outcomes(expected, ATTRIBUTION)

    assert outcomes["resolve_release"] == "PASS"


def test_a_gate_that_found_no_fixture_fails_a_tool_that_passes():
    expected = dict.fromkeys(ATTRIBUTION, "passed") | {"t.py::gate": "no_fixture"}

    outcomes = tool_outcomes(expected, ATTRIBUTION)

    assert outcomes["resolve_release"] == "FAIL"


def test_a_gate_that_calls_an_absent_tool_leaves_no_tool_passing():
    expected = dict.fromkeys(ATTRIBUTION, "passed") | {"t.py::gate": "not_implemented"}

    outcomes = tool_outcomes(expected, ATTRIBUTION)

    assert outcomes["resolve_release"] == "INCOMPLETE"


def test_the_table_lists_each_groups_tools_under_their_outcome():
    expected = dict.fromkeys(ATTRIBUTION, "passed") | {"t.py::form": "failed"}

    table = render(expected, ATTRIBUTION)

    assert table.startswith(BEGIN)
    assert table.endswith(END)
    assert "of the 5 tests" in table
    assert "| EVS tools | 12 | PASS: `resolve_release`, `get_concept`<br>NO TESTS: " in table
    assert "| caDSR tools | 10 | FAIL: `get_form`<br>NO TESTS: " in table


def test_only_the_text_between_the_markers_is_replaced():
    readme = f"before\n{BEGIN}\nold\n{END}\nafter\n"

    assert current(readme, f"{BEGIN}\nnew\n{END}") == f"before\n{BEGIN}\nnew\n{END}\nafter\n"


@pytest.mark.parametrize("readme", ["no table here", f"a\n{BEGIN}\nb\n", f"a\n{END}\nb\n"])
def test_a_readme_without_both_markers_is_refused(readme):
    with pytest.raises(SystemExit, match="lacks the markers"):
        current(readme, "table")


ATTRIBUTION_OF_TWO = {"t.py::a": ("get_form", False), "t.py::b": ("get_form", False)}


@pytest.fixture
def suite_of_two(tmp_path, monkeypatch):
    """A suite of two tests and a README with the markers; the result writes the expected
    outcomes and returns the arguments of `main`."""

    monkeypatch.setattr(status, "collect_attribution", lambda: ATTRIBUTION_OF_TWO)
    monkeypatch.setattr(status, "EXPECTED", tmp_path / "expected.json")
    (tmp_path / "README.md").write_text(f"{BEGIN}\n{END}\n", encoding="utf-8")

    def expected(outcomes):
        (tmp_path / "expected.json").write_text(json.dumps(outcomes), encoding="utf-8")
        return ["--check", "--readme", str(tmp_path / "README.md")]

    return expected


def test_default_generation_updates_functional_status_and_preserves_readme(
    tmp_path, monkeypatch, suite_of_two
):
    suite_of_two(dict.fromkeys(ATTRIBUTION_OF_TWO, "passed"))
    readme = tmp_path / "README.md"
    original = readme.read_text()
    target = tmp_path / "docs/functional-status.md"
    target.parent.mkdir()
    target.write_text(f"Public summary\n{BEGIN}\n{END}\nEvidence links\n")
    monkeypatch.setattr(status, "README", readme, raising=False)
    monkeypatch.setattr(status, "STATUS", target, raising=False)

    assert status.main([]) == 0

    assert readme.read_text() == original
    text = target.read_text()
    assert "of the 2 tests" in text
    assert text.startswith("Public summary\n") and text.endswith("Evidence links\n")
    assert status.main(["--check"]) == 0


def test_explicit_nested_destination_resolves_generated_source_links_from_its_page(
    tmp_path, monkeypatch, suite_of_two
):
    suite_of_two(dict.fromkeys(ATTRIBUTION_OF_TWO, "passed"))
    monkeypatch.setattr(status, "ACCEPTANCE", tmp_path / "acceptance")
    target = tmp_path / "docs/functional-status.md"
    target.parent.mkdir()
    target.write_text(f"{BEGIN}\n{END}\n")

    assert status.main(["--readme", str(target)]) == 0

    link = re.search(r"\[acceptance/expected/fixture.json\]\(([^)]+)\)", target.read_text())
    assert link is not None
    assert (target.parent / link.group(1)).resolve() == (
        tmp_path / "acceptance/expected/fixture.json"
    ).resolve()


def test_expected_outcomes_for_a_test_the_suite_lacks_are_refused(suite_of_two):
    arguments = suite_of_two(dict.fromkeys(["t.py::a", "t.py::b", "t.py::extra"], "passed"))

    with pytest.raises(SystemExit, match=r"differ in 1 tests, among them t\.py::extra"):
        status.main(arguments)


def test_a_test_of_the_suite_without_expected_outcomes_is_refused(suite_of_two):
    arguments = suite_of_two({"t.py::a": "passed"})

    with pytest.raises(SystemExit, match=r"differ in 1 tests, among them t\.py::b"):
        status.main(arguments)


def run_status(*options):
    return subprocess.run(  # noqa: S603
        [sys.executable, "-m", "nci_si_acceptance.status", *options],
        capture_output=True,
        text=True,
        check=False,
    )


def test_the_status_page_on_disk_is_what_the_expected_outcomes_generate():
    ran = run_status("--check")

    assert (ran.returncode, ran.stderr) == (0, "")


def test_a_readme_that_is_not_current_fails_the_check_and_is_rewritten_without_it(tmp_path):
    stale = tmp_path / "README.md"
    text = STATUS.read_text(encoding="utf-8")
    stale.write_text(text.replace(BEGIN, BEGIN + "\nstale table"), encoding="utf-8")

    failed = run_status("--check", "--readme", str(stale))
    written = run_status("--readme", str(stale))

    assert failed.returncode == 1
    assert "is not current" in failed.stderr
    assert written.returncode == 0
    assert run_status("--check", "--readme", str(stale)).returncode == 0
    empty = f"{BEGIN}\n{END}"
    assert current(stale.read_text(encoding="utf-8"), empty) == current(text, empty)


def test_failed_collection_cannot_replace_the_existing_readme(tmp_path, monkeypatch):
    readme = tmp_path / "README.md"
    text = f"before\n{BEGIN}\nold status\n{END}\nafter\n"
    readme.write_text(text, encoding="utf-8")
    monkeypatch.setattr(status.pytest, "main", lambda *args, **kwargs: pytest.ExitCode.USAGE_ERROR)

    with pytest.raises(SystemExit, match="collecting the suite failed with exit status 4"):
        status.main(["--readme", str(readme)])

    assert readme.read_text(encoding="utf-8") == text


def test_status_command_includes_stored_scheduled_live_observation(tmp_path):
    readme = tmp_path / "README.md"
    readme.write_text(f"{BEGIN}\n{END}\n", encoding="utf-8")
    ran = run_status("--readme", str(readme))
    assert ran.returncode == 0
    assert "46 passed / 0 failed / 920 skipped" in readme.read_text(encoding="utf-8")


def live_rows():
    cases = [
        ("match_data_elements", "passed", None),
        ("match_data_elements", "not_live", "scenario"),
        ("match_data_elements", "not_live", "not_live_capable"),
        ("list_classification_schemes", "not_live", "not_live_capable"),
        ("resolve_release", "not_live", "not_live_capable"),
        ("match_data_elements", "not_live", None),
        ("resolve_release", "failed", None),
        (None, "passed", None),
        (None, "not_live", "scenario"),
    ]
    tests = {
        f"foreign::{index}": {
            "tool": tool,
            "gate": tool is None,
            "outcome": outcome,
            "unmatched": [],
            **({"not_live_reason": reason} if reason else {}),
        }
        for index, (tool, outcome, reason) in enumerate(cases)
    }
    return {
        "mode": "live",
        "transport": "stdio",
        "tests": tests,
        "tools": {},
        "failed_gates": [],
        "unrun_gates": [],
        "tools_list_bytes": None,
        "suite": {"version": "old", "fixture_set": "old", "digest": "a" * 64},
        "run": {"selected": 9, "finished": 9, "worker_crashes": 0, "exit_status": 1},
    }


def provenance(raw):
    return {
        "sha256": hashlib.sha256(raw).hexdigest(),
        "run": 123,
        "artifact": 456,
        "event": "schedule",
        "branch": "main",
        "sha": "b" * 40,
        "date": "2026-10-19T07:10:38Z",
    }


def test_nested_destination_resolves_stored_live_context_link_and_anchor(tmp_path, suite_of_two):
    suite_of_two(dict.fromkeys(ATTRIBUTION_OF_TWO, "passed"))
    target = tmp_path / "published/status.md"
    target.parent.mkdir()
    target.write_text(f"{BEGIN}\n{END}\n")
    live = tmp_path / "live"
    live.mkdir()
    raw = json.dumps(live_rows()).encode()
    (live / "latest.json").write_bytes(raw)
    (live / "latest.yaml").write_text(yaml.safe_dump(provenance(raw)))

    assert status.main(["--readme", str(target), "--live-dir", str(live)]) == 0

    link = re.search(r"\[cadsr-access\]\(([^)]+)\)", target.read_text())
    assert link is not None
    relative, _, anchor = link.group(1).partition("#")
    assert (target.parent / relative).resolve() == (
        Path(__file__).resolve().parents[2] / "docs/upstream/cadsr.md"
    )
    assert anchor == "cadsr-access"


def test_exact_live_table_with_scenario_and_credential_precedence(monkeypatch):
    monkeypatch.setattr(
        status,
        "REQUIRED_TOOLS",
        {
            "match_data_elements": "cadsr",
            "list_classification_schemes": "cadsr",
            "resolve_release": "evs",
        },
    )
    report = live_rows()
    report["tests"] |= {
        "foreign::not_implemented": {
            "tool": "list_classification_schemes",
            "gate": False,
            "outcome": "not_implemented",
            "unmatched": [],
        },
        "foreign::skipped": {
            "tool": "resolve_release",
            "gate": False,
            "outcome": "skipped",
            "unmatched": [],
        },
    }
    report["run"] |= {"selected": 11, "finished": 11}
    metadata = provenance(json.dumps(report).encode())
    rendered = status.render_live(report, metadata, status.skip_contexts())
    assert rendered == (
        "Latest stored scheduled main live run: "
        "[123](https://github.com/CBIIT/nci-si-mcp/actions/runs/123), "
        "2026-10-19T07:10:38Z; commit [bbbbbbbbbbbb](https://github.com/CBIIT/nci-si-mcp/commit/"
        + "b"
        * 40
        + "). **Complete**.\n"
        "Report SHA-256: `" + metadata["sha256"] + "`; artifact 456.\n\n"
        "| Tool | Live passed / failed / skipped | Skip reason / context |\n|---|---|---|\n"
        "| `match_data_elements` | 1 / 0 / 3 | 1 crafted scenario (fixture-only); "
        "1 credentials not issued ([cadsr-access](docs/upstream/cadsr.md#cadsr-access)); "
        "1 unrecorded |\n"
        "| `list_classification_schemes` | 0 / 0 / 2 | 1 upstream capability gap "
        "([cadsr-discovery](docs/upstream/cadsr.md#cadsr-discovery)); "
        "1 other skip (not not_live) |\n"
        "| `resolve_release` | 0 / 1 / 2 | 1 not yet probed by this live acceptance run; "
        "1 other skip (not not_live) |\n"
        "| Unattributed gates | 1 / 0 / 1 | 1 crafted scenario (fixture-only) |\n"
        "| **Total** | **2 / 1 / 8** | |\n\n"
        "2 passed / 1 failed / 8 skipped. Skipped includes `not_live`, `not_implemented` and "
        "ordinary skips; only `not_live` has the reason contexts above. "
        "Passed validation/refusal tests do not establish live content retrieval. "
        "Unrecorded reasons are never inferred from current test markers."
    )


def test_incomplete_latest_is_disclosed_without_substituting_older_green():
    report = live_rows()
    report["run"]["finished"] = 8
    text = status.render_live(report, provenance(b"incomplete"), status.skip_contexts())
    assert "**Incomplete: selected, finished and recorded outcome counts differ**" in text
    assert "2 passed / 1 failed / 6 skipped" in text


@pytest.mark.parametrize(
    "field,value",
    [
        ("event", "workflow_dispatch"),
        ("branch", "issue/358"),
        ("sha256", "0" * 64),
        ("sha", "bad"),
        ("missing", "date"),
        ("extra", "unexpected"),
        (None, None),
        (None, []),
        ("run", 0),
        ("run", -1),
        ("run", True),
        ("run", "123"),
        ("artifact", 0),
        ("artifact", -1),
        ("artifact", True),
        ("artifact", 456.0),
        ("date", "2026-10-19"),
        ("date", 20261019),
    ],
)
def test_provenance_rejects_invalid_sidecar(tmp_path, field, value):
    raw = json.dumps(live_rows()).encode()
    (tmp_path / "latest.json").write_bytes(raw)
    metadata = provenance(raw)
    if field == "missing":
        del metadata[value]
    elif field is None:
        metadata = value
    else:
        metadata[field] = value
    (tmp_path / "latest.yaml").write_text(yaml.safe_dump(metadata))
    with pytest.raises(ValueError, match=r"^Invalid live evidence provenance$"):
        status.load_live(tmp_path)


def test_check_rejects_fixture_evidence_with_valid_scheduled_main_provenance_without_writing(
    tmp_path,
):
    raw = json.dumps(live_rows() | {"mode": "fixture"}).encode()
    report = tmp_path / "latest.json"
    metadata = tmp_path / "latest.yaml"
    readme = tmp_path / "README.md"
    report.write_bytes(raw)
    metadata.write_text(yaml.safe_dump(provenance(raw)))
    readme.write_text(f"before\n{BEGIN}\nold status\n{END}\nafter\n")
    before = {path.name: path.read_bytes() for path in tmp_path.iterdir()}

    ran = run_status("--check", "--readme", str(readme), "--live-dir", str(tmp_path))

    assert ran.returncode == 1
    assert "ValueError: Live evidence must contain a live report" in ran.stderr
    assert ran.stdout == ""
    assert {path.name: path.read_bytes() for path in tmp_path.iterdir()} == before


@pytest.mark.parametrize("change", ["report", "provenance"])
def test_each_stored_input_independently_makes_check_stale_without_writing(tmp_path, change):
    report, metadata = status.load_live()
    raw = json.dumps(report).encode()
    metadata["sha256"] = hashlib.sha256(raw).hexdigest()
    live = tmp_path / "live"
    live.mkdir()
    (live / "latest.json").write_bytes(raw)
    (live / "latest.yaml").write_text(yaml.safe_dump(metadata))
    readme = tmp_path / "README.md"
    readme.write_text(f"{BEGIN}\n{END}\n")
    options = ["--readme", str(readme), "--live-dir", str(live)]
    assert run_status(*options).returncode == 0
    before = readme.read_bytes()
    if change == "report":
        report["tests"][next(iter(report["tests"]))]["outcome"] = "failed"
        raw = json.dumps(report).encode()
        (live / "latest.json").write_bytes(raw)
        metadata["sha256"] = hashlib.sha256(raw).hexdigest()
    else:
        metadata["run"] += 1
    (live / "latest.yaml").write_text(yaml.safe_dump(metadata))
    assert run_status("--check", *options).returncode == 1
    assert readme.read_bytes() == before


def test_real_command_renders_stored_evidence_with_network_denied(tmp_path):
    readme = tmp_path / "README.md"
    readme.write_text(f"{BEGIN}\n{END}\n")
    script = (
        "import sys, socket; from unittest.mock import patch; "
        "from nci_si_acceptance.status import main; "
        "patcher = patch.object(socket.socket, 'connect', "
        "side_effect=AssertionError('network forbidden')); patcher.start(); "
        "sys.exit(main(sys.argv[1:]))"
    )
    ran = subprocess.run(  # noqa: S603
        [sys.executable, "-c", script, "--readme", str(readme)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert (ran.returncode, ran.stderr) == (0, "")


def test_historical_foreign_ids_do_not_acquire_current_reasons():
    report = live_rows()
    for row in report["tests"].values():
        row.pop("not_live_reason", None)
    text = status.render_live(report, provenance(b"old"), status.skip_contexts())
    assert "| `match_data_elements` | 1 / 0 / 3 | 3 unrecorded |" in text
    assert "credentials not issued" not in text
    assert "upstream capability gap" not in text


def test_catalogue_and_specification_changes_drive_context_instead_of_tool_lists(
    tmp_path, monkeypatch
):
    catalogue = tmp_path / "catalogue.yaml"
    catalogue.write_text(
        yaml.safe_dump(
            {
                "entries": [
                    {"id": "cadsr-access", "team": "cadsr", "requirements": ["resolve_release-1"]},
                    {"id": "new-gap", "team": "evs", "requirements": ["X-1"]},
                ]
            }
        )
    )
    monkeypatch.setattr(status, "CATALOGUE", catalogue)
    with patch.object(
        status,
        "load_requirements",
        return_value={
            "resolve_release-1": {"basis": ["get_form"]},
            "X-1": {"basis": ["get_form", "not_a_tool"]},
        },
    ):
        assert status.skip_contexts() == {
            "resolve_release": "credentials not issued "
            "([cadsr-access](docs/upstream/cadsr.md#cadsr-access))",
            "get_form": "upstream capability gap ([new-gap](docs/upstream/evs.md#new-gap))",
        }
