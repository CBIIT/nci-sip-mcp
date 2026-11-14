"""The per-tool report: outcomes from the final test outcomes, over both run modes, rendered."""

import json
import os
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import pytest

from nci_si_acceptance import report as report_module
from nci_si_acceptance.report import (
    ATTRIBUTION,
    COLLECTOR,
    UNMATCHED,
    WORKER_TOOLS,
    Collector,
    NativeReport,
    ReportFieldError,
    combine,
    main,
    render,
    tool_outcome,
    withhold_from,
    write_report,
)
from nci_si_acceptance.report import (
    SUITE as SUITE_KEY,
)
from nci_si_acceptance.report import (
    TestResult as NativeTestResult,
)
from nci_si_acceptance.spec import REQUIRED_TOOLS


@pytest.mark.parametrize(
    ("counts", "gates_failed", "gates_unrun", "implemented", "outcome"),
    [
        ({"passed": 3}, False, False, True, "PASS"),
        ({"passed": 2, "failed": 1}, False, False, True, "FAIL"),
        ({"passed": 3}, True, False, True, "FAIL"),
        ({"skipped": 2}, True, False, True, "NOT RUN"),
        ({"passed": 2, "no_fixture": 1}, False, False, True, "NO FIXTURE"),
        ({"no_fixture": 1, "failed": 1}, False, False, True, "FAIL"),
        ({"passed": 2, "skipped": 1}, False, False, True, "INCOMPLETE"),
        ({"passed": 2, "not_implemented": 1}, False, False, True, "INCOMPLETE"),
        ({"passed": 2, "not_live": 1}, False, False, True, "PASS"),
        ({"not_live": 2}, False, False, True, "NOT RUN"),
        ({"not_implemented": 2}, True, False, False, "NOT IMPLEMENTED"),
        ({"failed": 2}, False, False, False, "FAIL"),
        ({"no_fixture": 2}, False, False, False, "NO FIXTURE"),
        ({"not_live": 1}, False, False, None, "NOT RUN"),
        ({}, False, False, False, "NO TESTS"),
        ({"passed": 3}, False, True, True, "INCOMPLETE"),
        ({"passed": 3}, True, True, True, "FAIL"),
        ({"passed": 2, "skipped": 1}, False, True, True, "INCOMPLETE"),
        ({"skipped": 2}, False, True, True, "NOT RUN"),
        ({"passed": 2, "no_fixture": 1}, False, True, True, "NO FIXTURE"),
        ({"not_implemented": 2}, False, True, False, "NOT IMPLEMENTED"),
        ({}, False, True, False, "NO TESTS"),
    ],
)
def test_a_tool_outcome_follows_from_its_test_outcomes_and_the_gates(
    counts, gates_failed, gates_unrun, implemented, outcome
):
    assert tool_outcome(Counter(counts), gates_failed, gates_unrun, implemented) == outcome


# The property a skip site sets to say why the test did not run (not_live, not_implemented).
SKIP = "acceptance_skip"
NI, LIVE = "not_implemented", "not_live"


def phase(when, outcome, nodeid="t.py::test_a", reason="", unmatched=None, skip=None):
    longrepr = ("t.py", 1, f"Skipped: {reason}") if outcome == "skipped" else None
    properties = [(UNMATCHED, unmatched)] if unmatched else []
    properties += [(SKIP, skip)] if skip else []
    return pytest.TestReport(
        nodeid, ("t.py", 1, "test_a"), {}, outcome, longrepr, when, user_properties=properties
    )


def collected(*phases, absent=()):
    """The report of a run whose server has every required tool except those `absent`."""

    collector = Collector()
    collector.note_tools(
        SimpleNamespace(implemented=lambda name: name not in absent, listing_bytes=4096)
    )
    for report, tool, gate in phases:
        report.user_properties.append((ATTRIBUTION, {"tool": tool, "gate": gate}))
        collector.record(report)
    return collector.report("fixture")


def test_each_test_counts_once_with_its_worst_phase():
    report = collected(
        (phase("setup", "passed"), "get_concept", False),
        (phase("call", "passed"), "get_concept", False),
        (phase("teardown", "failed"), "get_concept", False),
        (phase("call", "passed", "t.py::test_b"), "get_concept", False),
    )

    row = report["tools"]["get_concept"]
    assert (row["outcome"], row["counts"]) == ("FAIL", {"failed": 1, "passed": 1})
    assert report["tests"]["t.py::test_a"]["outcome"] == "failed"


@pytest.mark.parametrize("reason", ["scenario", "not_live_capable"])
def test_recorded_skip_reason_roundtrips_and_historical_absence_stays_absent(reason):
    old = collected((phase("setup", "skipped", skip=LIVE), "get_form", False)) | {"suite": SUITE}
    new = json.loads(json.dumps(old))
    new["tests"]["t.py::test_a"]["not_live_reason"] = reason
    assert NativeReport.from_dict(new).to_dict() == new
    assert NativeReport.from_dict(old).to_dict() == old


@pytest.mark.parametrize("skip", ["passed", "failed", "no_fixture", "unknown"])
def test_skip_marker_cannot_claim_a_test_verdict(skip):
    with pytest.raises(ValueError, match="unknown skip outcome"):
        collected((phase("call", "skipped", skip=skip), "get_form", False))


@pytest.mark.parametrize("reason", [None, "unknown", 1, [], {}])
def test_present_invalid_skip_reason_is_rejected(reason):
    row = {"tool": None, "gate": True, "outcome": LIVE, "unmatched": [], "not_live_reason": reason}
    with pytest.raises(ReportFieldError, match=r"tests\[0\].not_live_reason"):
        NativeTestResult.from_dict(row, "tests[0]")


def test_skip_reason_is_incompatible_with_a_non_not_live_outcome():
    row = {
        "tool": None,
        "gate": True,
        "outcome": "passed",
        "unmatched": [],
        "not_live_reason": "scenario",
    }
    with pytest.raises(ReportFieldError, match="not_live_reason"):
        NativeTestResult.from_dict(row, "test")


@pytest.mark.parametrize("later", ["passed", "failed", "skipped"])
def test_only_the_winning_phase_owns_the_skip_reason(later):
    skipped = phase("setup", "skipped", skip=LIVE)
    skipped.user_properties.append(("not_live_reason", "scenario"))
    next_phase = phase("call", later)
    report = collected((skipped, "get_form", False), (next_phase, "get_form", False))
    row = report["tests"]["t.py::test_a"]
    assert row == {
        "tool": "get_form",
        "gate": False,
        "unmatched": [],
        **(
            {"outcome": LIVE, "not_live_reason": "scenario"}
            if later == "passed"
            else {"outcome": later}
        ),
    }


def test_teardown_error_drops_the_live_skip_reason_carried_by_every_item_phase():
    phases = [phase("setup", "passed"), phase("call", "skipped"), phase("teardown", "failed")]
    for report in phases:
        report.user_properties.extend([(SKIP, LIVE), ("not_live_reason", "scenario")])

    report = collected(*((item, "get_form", False) for item in phases)) | {
        "mode": "live",
        "suite": SUITE,
    }

    assert report["tests"]["t.py::test_a"] == {
        "tool": "get_form",
        "gate": False,
        "outcome": "failed",
        "unmatched": [],
    }
    assert NativeReport.from_dict(json.loads(json.dumps(report))).to_dict() == report


def test_a_tool_row_holds_its_verdict_and_counts_and_no_alias_of_the_name():
    report = collected((phase("call", "passed"), "get_concept", False))

    assert set(report["tools"]["get_concept"]) == {"group", "outcome", "gates_only", "counts"}


def test_a_later_skip_does_not_erase_a_failure_or_its_missing_fixture_evidence():
    missing = ["GET evs /api/v1/version {}"]
    report = collected(
        (phase("call", "failed", unmatched=missing), "get_concept", False),
        (phase("teardown", "skipped", reason="cleanup skipped"), "get_concept", False),
    )

    assert report["tools"]["get_concept"]["outcome"] == "NO FIXTURE"
    assert report["tests"]["t.py::test_a"]["unmatched"] == missing
    assert report["tests"]["t.py::test_a"]["outcome"] == "no_fixture"


def test_a_failure_from_a_missing_fixture_is_its_own_outcome_and_names_the_request():
    missing = ["GET evs /api/v1/version {}"]
    report = collected(
        (phase("call", "failed", "t.py::test_a"), "get_concepts", False),
        (phase("teardown", "failed", "t.py::test_a", unmatched=missing), "get_concepts", False),
    )

    assert report["tools"]["get_concepts"]["outcome"] == "NO FIXTURE"
    assert report["tests"]["t.py::test_a"] == {
        "tool": "get_concepts",
        "gate": False,
        "outcome": "no_fixture",
        "unmatched": missing,
    }


def test_skips_are_not_implemented_or_not_run_and_a_failed_gate_fails_every_passing_tool():
    report = collected(
        (
            phase(
                "call",
                "skipped",
                "t.py::b",
                "NOT IMPLEMENTED: get_concepts",
                skip="not_implemented",
            ),
            "get_concepts",
            False,
        ),
        (phase("setup", "skipped", "t.py::c", "needs a prepared index"), "list_contexts", False),
        (phase("call", "passed", "t.py::d"), "get_form", False),
        (phase("call", "failed", "t.py::gate"), None, True),
        absent=("get_concepts",),
    )

    tools = report["tools"]
    assert [tools[name]["outcome"] for name in ("get_concepts", "list_contexts", "get_form")] == [
        "NOT IMPLEMENTED",
        "NOT RUN",
        "FAIL",
    ]
    assert (report["failed_gates"], tools["get_form"]["gates_only"]) == (["t.py::gate"], True)
    assert set(tools) == set(REQUIRED_TOOLS)


def test_a_report_lists_its_tests_and_gates_in_order_whatever_order_they_finished_in():
    report = collected(
        (phase("call", "failed", "t.py::b"), None, True),
        (phase("call", "failed", "t.py::c"), None, True),
        (phase("call", "failed", "t.py::a"), None, True),
    )

    assert report["failed_gates"] == ["t.py::a", "t.py::b", "t.py::c"]
    assert list(report["tests"]) == ["t.py::a", "t.py::b", "t.py::c"]


def test_a_gate_that_did_not_run_is_listed_and_the_listing_size_kept():
    report = collected(
        (phase("call", "passed", "t.py::listed"), None, True),
        (
            phase("call", "skipped", "t.py::called", "NOT IMPLEMENTED: get_concept", skip=NI),
            None,
            True,
        ),
        (
            phase("call", "skipped", "t.py::tool", "NOT IMPLEMENTED: get_form", skip=NI),
            "get_form",
            False,
        ),
    )

    gates = (report["failed_gates"], report["unrun_gates"])
    assert (*gates, report["tools_list_bytes"]) == ([], ["t.py::called"], 4096)


def test_a_live_run_counts_a_fixture_only_test_without_making_its_tool_incomplete():
    report = collected(
        (phase("setup", "skipped", "t.py::a", "fixture mode only", skip=LIVE), "get_form", False),
        (phase("call", "passed", "t.py::b"), "get_form", False),
        (
            phase("setup", "skipped", "t.py::c", "fixture mode only", skip=LIVE),
            "get_concept",
            False,
        ),
    )

    assert report["tests"]["t.py::a"]["outcome"] == "not_live"
    assert report["tools"]["get_form"]["outcome"] == "PASS"
    assert report["tools"]["get_concept"]["outcome"] == "NOT RUN"


def test_a_tool_failing_its_own_test_while_a_gate_fails_is_not_failing_by_the_gates_only():
    report = collected(
        (phase("call", "failed", "t.py::a"), "get_form", False),
        (phase("call", "passed", "t.py::b"), "get_concept", False),
        (phase("call", "failed", "t.py::gate"), None, True),
    )

    tools = report["tools"]
    assert (tools["get_form"]["outcome"], tools["get_form"]["gates_only"]) == ("FAIL", False)
    assert (tools["get_concept"]["outcome"], tools["get_concept"]["gates_only"]) == ("FAIL", True)


SUITE = {"version": "1.2.3", "fixture_set": "ncit_26.09d, recorded 2026-10-03", "digest": "ab" * 32}


def run(outcomes, tests=None):
    """A run's report with the given tool outcomes and test entries."""

    tools = {
        name: {
            "group": group,
            "outcome": "NO TESTS",
            "gates_only": False,
            "counts": {},
        }
        for name, group in REQUIRED_TOOLS.items()
    }
    for name, outcome in outcomes.items():
        tools[name]["outcome"] = outcome
    return {
        "mode": "fixture",
        "transport": "stdio",
        "run": {
            "exit_status": 0,
            "selected": len(tests or {}),
            "finished": len(tests or {}),
            "worker_crashes": 0,
        },
        "failed_gates": [],
        "unrun_gates": [],
        "tools_list_bytes": None,
        "tools": tools,
        "tests": tests or {},
        "suite": SUITE,
    }


def live_test(tool, outcome):
    return {"tool": tool, "gate": False, "outcome": outcome, "unmatched": []}


def test_a_live_failure_is_excused_only_test_by_test():
    fixture = run({"get_form": "PASS", "search_data_elements": "PASS", "get_concept": "PASS"})
    live = run(
        {},
        {
            "t.py::form_known": live_test("get_form", "failed"),
            "t.py::form_new": live_test("get_form", "failed"),
            "t.py::search_known": live_test("search_data_elements", "failed"),
            "t.py::concept": live_test("get_concept", "passed"),
        },
    )
    limitations = {"t.py::form_known": "C-4", "t.py::search_known": "C-2"}

    combined = combine(fixture, live, limitations)

    assert combined["search_data_elements"] == ("PASS (fixture only)", ["C-2"])
    assert combined["get_form"] == ("FAIL", ["C-4"])
    assert combined["get_concept"] == ("PASS", [])


def test_a_live_gate_failure_fails_every_tool_unless_it_is_a_documented_limitation():
    fixture = run({"get_form": "PASS", "get_concept": "PASS"})
    live = run({}, {"t.py::gate": {"tool": None, "gate": True, "outcome": "failed"}})
    live["failed_gates"] = ["t.py::gate"]

    assert combine(fixture, live, {})["get_form"] == ("FAIL", [])
    assert combine(fixture, live, {"t.py::gate": "P-1"})["get_concept"] == (
        "PASS (fixture only)",
        ["P-1"],
    )


def test_the_rendered_report_states_its_modes_counts_and_what_proves_nothing_yet():
    fixture = run(
        {
            "resolve_release": "FAIL",
            "get_concept": "NOT IMPLEMENTED",
            "get_concepts": "NO FIXTURE",
            "get_form": "INCOMPLETE",
        }
    )
    fixture["tools"]["resolve_release"] |= {
        "gates_only": True,
        "counts": {"passed": 4},
    }
    fixture["tools"]["get_form"]["counts"] = {"passed": 3, "not_implemented": 4, "skipped": 1}
    fixture |= {"unrun_gates": ["t.py::correlation"], "tools_list_bytes": 61234}
    fixture["tests"] = {
        "t.py::x": live_test("get_concepts", "no_fixture") | {"unmatched": ["GET evs /x {}"]}
    }
    combined = {name: (row["outcome"], []) for name, row in fixture["tools"].items()}

    text = render(fixture, combined, "fixture only")

    assert "\nRun modes: fixture only.\n" in text
    assert "| `resolve_release` | evs | FAIL (gates only) | 4 / 0 / 0 / 0 |  |" in text
    assert "| `get_form` | cadsr | INCOMPLETE | 3 / 0 / 0 / 5 |  |" in text
    assert "Implemented as" not in text
    assert "Tests run and not passing: resolve_release, get_concepts, get_form." in text
    assert "Requests without a fixture: GET evs /x {}." in text
    assert "Gates not run: t.py::correlation." in text
    assert "Size of the tools/list result: 61,234 bytes." in text
    never_run = next(line for line in text.splitlines() if line.startswith("Tests never run"))
    assert "get_concept" in never_run
    assert "list_contexts" in never_run


def test_the_command_combines_the_runs_with_per_test_limitations(tmp_path, capsys):
    fixture = run({"get_form": "PASS"}, {"t.py::form": live_test("get_form", "passed")})
    live = run({}, {"t.py::form": live_test("get_form", "failed")}) | {
        "mode": "live",
        "transport": "streamable-http",
    }
    (tmp_path / "fixture.json").write_text(json.dumps(fixture), encoding="utf-8")
    (tmp_path / "live.json").write_text(json.dumps(live), encoding="utf-8")
    (tmp_path / "limitations.yaml").write_text('"t.py::form": C-4\n', encoding="utf-8")

    main(
        [
            str(tmp_path / "fixture.json"),
            "--live",
            str(tmp_path / "live.json"),
            "--limitations",
            str(tmp_path / "limitations.yaml"),
        ]
    )

    output = capsys.readouterr().out
    assert "\nRun modes: fixture (stdio) and live (streamable-http).\n" in output
    assert "| `get_form` | cadsr | PASS (fixture only) | 0 / 0 / 0 / 0 | C-4 |" in output


def test_the_command_refuses_reports_of_different_suites(tmp_path):
    live = run({}) | {"mode": "live", "suite": SUITE | {"digest": "cd" * 32}}
    (tmp_path / "fixture.json").write_text(json.dumps(run({})), encoding="utf-8")
    (tmp_path / "live.json").write_text(json.dumps(live), encoding="utf-8")

    with pytest.raises(SystemExit, match="come from different suites"):
        main([str(tmp_path / "fixture.json"), "--live", str(tmp_path / "live.json")])


def test_the_command_refuses_reports_that_differ_only_in_the_fixture_set(tmp_path):
    other = SUITE | {"fixture_set": "ncit_26.09d, recorded 2026-10-04"}
    live = run({}) | {"mode": "live", "suite": other}
    (tmp_path / "fixture.json").write_text(json.dumps(run({})), encoding="utf-8")
    (tmp_path / "live.json").write_text(json.dumps(live), encoding="utf-8")

    with pytest.raises(SystemExit, match="come from different suites"):
        main([str(tmp_path / "fixture.json"), "--live", str(tmp_path / "live.json")])


def test_the_command_refuses_a_report_of_the_wrong_run_mode(tmp_path):
    (tmp_path / "fixture.json").write_text(json.dumps(run({})), encoding="utf-8")

    with pytest.raises(SystemExit, match="is the report of a fixture run, not a live run"):
        main([str(tmp_path / "fixture.json"), "--live", str(tmp_path / "fixture.json")])


def test_an_empty_limitations_file_excuses_nothing(tmp_path, capsys):
    live = run({}, {"t.py::form": live_test("get_form", "failed")}) | {"mode": "live"}
    fixture = run({"get_form": "PASS"}, {"t.py::form": live_test("get_form", "passed")})
    (tmp_path / "fixture.json").write_text(json.dumps(fixture), encoding="utf-8")
    (tmp_path / "live.json").write_text(json.dumps(live), encoding="utf-8")
    (tmp_path / "limitations.yaml").write_text("", encoding="utf-8")

    main(
        [
            str(tmp_path / "fixture.json"),
            "--live",
            str(tmp_path / "live.json"),
            "--limitations",
            str(tmp_path / "limitations.yaml"),
        ]
    )

    assert "| `get_form` | cadsr | FAIL | 0 / 0 / 0 / 0 |  |" in capsys.readouterr().out


def test_the_command_says_a_fixture_run_alone_is_not_the_final_outcome(tmp_path, capsys):
    (tmp_path / "fixture.json").write_text(json.dumps(run({"get_form": "PASS"})), encoding="utf-8")

    main([str(tmp_path / "fixture.json")])

    output = capsys.readouterr().out
    assert "\nRun modes: fixture (stdio) only; the live run is not included" in output
    assert "| `get_form` | cadsr | PASS | 0 / 0 / 0 / 0 |  |" in output
    assert "Size of the tools/list result: not measured, no server started." in output


def test_live_failures_are_not_excused_when_no_limitations_file_is_supplied(tmp_path, capsys):
    fixture = tmp_path / "fixture.json"
    live = tmp_path / "live.json"
    passed = run({"get_form": "PASS"}, {"t.py::form": live_test("get_form", "passed")})
    fixture.write_text(json.dumps(passed), encoding="utf-8")
    failed = run({}, {"t.py::form": live_test("get_form", "failed")}) | {"mode": "live"}
    live.write_text(json.dumps(failed), encoding="utf-8")

    assert main([str(fixture), "--live", str(live)]) == 0

    assert "| `get_form` | cadsr | FAIL | 0 / 0 / 0 / 0 |  |" in capsys.readouterr().out


def test_the_report_records_the_transport_of_the_run():
    assert Collector().report("fixture")["transport"] == "stdio"

    collector = Collector()
    collector.transport = "streamable-http"

    assert collector.report("live")["transport"] == "streamable-http"


def test_a_failure_shows_the_credential_of_a_remote_server_nowhere(monkeypatch):
    monkeypatch.setenv("NCI_SI_ACCEPTANCE_AUTHORIZATION", "Bearer do-not-print")
    failed = pytest.TestReport(
        "t.py::test_a",
        ("t.py", 1, "test_a"),
        {},
        "failed",
        "assert 'x' == 'Bearer do-not-print'",
        "call",
        sections=[("Captured stdout call", "sent Bearer do-not-print\nand more")],
    )
    skipped = phase("setup", "skipped", reason="needs a server")

    withhold_from(failed)
    withhold_from(skipped)

    assert str(failed.longrepr) == "assert 'x' == '[authorization withheld]'"
    assert failed.sections == [("Captured stdout call", "sent [authorization withheld]\nand more")]
    assert skipped.longrepr == ("t.py", 1, "Skipped: needs a server")


def test_an_unrelated_failure_keeps_its_rich_traceback(monkeypatch):
    monkeypatch.setenv("NCI_SI_ACCEPTANCE_AUTHORIZATION", "Bearer private-token")
    traceback = SimpleNamespace(reprcrash="AssertionError: expected concept C1")
    failed = phase("call", "failed")
    failed.longrepr = traceback

    withhold_from(failed)

    assert failed.longrepr is traceback
    assert failed.longrepr.reprcrash == "AssertionError: expected concept C1"


def test_a_credential_with_quotes_and_non_ascii_is_withheld_from_the_report_file(
    monkeypatch, tmp_path
):
    credential = 'Digest username="a\\b", realm="Zürich"'
    monkeypatch.setenv("NCI_SI_ACCEPTANCE_AUTHORIZATION", credential)
    collector = Collector()
    collector.tests["t.py::test_a"] = {
        "tool": "get_form",
        "gate": False,
        "outcome": "no_fixture",
        "unmatched": [f"GET evs /x {credential}"],
    }
    stash = pytest.Stash()
    stash[COLLECTOR], stash[SUITE_KEY] = collector, SUITE
    config = SimpleNamespace(getoption=lambda name: str(tmp_path / "report.json"), stash=stash)

    write_report(config, "fixture")

    written = (tmp_path / "report.json").read_text(encoding="utf-8")
    assert "a\\\\b" not in written
    assert "username" not in written
    assert json.loads(written)["tests"]["t.py::test_a"]["unmatched"] == [
        "GET evs /x [authorization withheld]"
    ]


def test_the_command_refuses_a_report_written_by_an_older_suite(tmp_path):
    older = run({})
    del older["transport"]
    (tmp_path / "old.json").write_text(json.dumps(older), encoding="utf-8")

    with pytest.raises(SystemExit) as refused:
        main([str(tmp_path / "old.json")])

    assert str(refused.value) == f"{tmp_path / 'old.json'} was written by an older suite; re-run it"


def test_a_worker_writes_no_report_for_the_controller_to_overwrite(tmp_path):
    stash = pytest.Stash()
    stash[COLLECTOR], stash[SUITE_KEY] = Collector(), SUITE
    config = SimpleNamespace(
        getoption=lambda name: str(tmp_path / "report.json"), stash=stash, workerinput={}
    )

    write_report(config, "fixture")

    assert not (tmp_path / "report.json").exists()


@pytest.mark.parametrize("fault", ["fsync", "replace"])
@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_atomic_report_preserves_prior_file_and_each_publication_failure(
    tmp_path, monkeypatch, cleanup_fails, fault
):
    destination = tmp_path / "report.json"
    prior = b'{"prior":true}\n'
    destination.write_bytes(prior)
    stash = pytest.Stash()
    stash[COLLECTOR], stash[SUITE_KEY] = Collector(), SUITE
    config = SimpleNamespace(getoption=lambda name: str(destination), stash=stash)
    initiating, cleanup = OSError("owned publication failure"), LookupError("owned unlink failure")
    original_unlink, owned_temporaries = Path.unlink, set()

    def refuse_publication(*_args):
        raise initiating

    def unlink(path, *args, **kwargs):
        if path.parent == tmp_path and path != destination:
            owned_temporaries.add(path)
            if cleanup_fails:
                raise cleanup
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(report_module.os, fault, refuse_publication)
    monkeypatch.setattr(Path, "unlink", unlink)
    try:
        with pytest.raises((OSError, LookupError, BaseExceptionGroup)) as observed:
            write_report(config, "fixture", atomic=True)
        assert destination.read_bytes() == prior
        if cleanup_fails:
            assert isinstance(observed.value, BaseExceptionGroup)
            assert observed.value.exceptions == (initiating, cleanup)
        else:
            assert observed.value is initiating
            assert list(tmp_path.iterdir()) == [destination]
    finally:
        # Only temporaries observed from this writer are ours; keep the prior report.
        for path in owned_temporaries:
            original_unlink(path, missing_ok=True)


def test_atomic_writer_creates_nested_path_and_syncs_complete_bytes(tmp_path, monkeypatch):
    destination = tmp_path / "fresh" / "nested" / "report.json"
    payload = '{"run": {"exit_status": 0}, "tests": {}}\n'
    expected = payload.encode("utf-8")
    synchronized = []
    actual_sync = report_module.os.fsync

    def sync(fd):
        # Observe OS bytes before the existing synchronization, not flush invocations.
        synchronized.append(os.pread(fd, len(expected) + 1, 0))
        actual_sync(fd)

    monkeypatch.setattr(report_module.os, "fsync", sync)
    assert not destination.parent.exists()
    failure = None
    try:
        report_module._publish_native(destination, payload)
    except OSError as error:
        failure = error
    assert failure is None
    assert destination.read_bytes() == expected
    assert synchronized and all(data == expected for data in synchronized)
    assert list(destination.parent.iterdir()) == [destination]


def test_what_a_worker_noted_of_the_server_reaches_the_controller_once():
    worker = Collector()
    worker.note_tools(SimpleNamespace(implemented=lambda name: True, listing_bytes=4096))
    output = {}
    worker.pytest_sessionfinish(
        SimpleNamespace(config=SimpleNamespace(workeroutput=output), testscollected=0), 0
    )
    controller = Collector()

    controller.pytest_testnodedown(SimpleNamespace(workeroutput=output), None)
    controller.pytest_testnodedown(SimpleNamespace(workeroutput={}), None)
    controller.pytest_testnodedown(
        SimpleNamespace(workeroutput={WORKER_TOOLS: {"implemented": {}, "listing_bytes": 1}}), None
    )

    assert (controller.listing_bytes, controller.implemented) == (
        worker.listing_bytes,
        dict.fromkeys(REQUIRED_TOOLS, True),
    )


def test_a_worker_that_started_no_server_forwards_only_environment():
    output = {}

    Collector().pytest_sessionfinish(
        SimpleNamespace(config=SimpleNamespace(workeroutput=output), testscollected=0), 0
    )

    assert output == {"nci_si_environment": None}
    controller = Collector()
    controller.pytest_testnodedown(SimpleNamespace(workeroutput=output), None)
    emitted = controller.report("fixture")
    assert emitted["mode"] == "fixture"
    assert "environment" not in emitted


def test_an_unrun_gate_leaves_no_tool_passing():
    unrun_gate = (phase("setup", "skipped", "t.py::gate", "no security server"), None, True)
    report = collected(
        (phase("call", "passed", "t.py::form"), "get_form", False),
        (phase("call", "passed", "t.py::concept"), "get_concept", False),
        (phase("setup", "skipped", "t.py::index", "needs a prepared index"), "get_concept", False),
        (
            phase("setup", "skipped", "t.py::contexts", "needs a prepared index"),
            "list_contexts",
            False,
        ),
        unrun_gate,
    )

    tools = report["tools"]
    outcomes = {name: tools[name]["outcome"] for name in ("get_form", "get_concept")}
    assert outcomes == {"get_form": "INCOMPLETE", "get_concept": "INCOMPLETE"}
    assert tools["list_contexts"]["outcome"] == "NOT RUN"
    assert tools["get_concepts"]["outcome"] == "NO TESTS"
    assert (report["failed_gates"], report["unrun_gates"]) == ([], ["t.py::gate"])


def test_a_failed_gate_still_fails_a_tool_when_another_gate_did_not_run():
    report = collected(
        (phase("call", "passed", "t.py::form"), "get_form", False),
        (phase("call", "failed", "t.py::failed_gate"), None, True),
        (phase("setup", "skipped", "t.py::unrun_gate", "no security server"), None, True),
    )

    form = report["tools"]["get_form"]
    assert (form["outcome"], form["gates_only"]) == ("FAIL", True)


def test_a_tool_incomplete_only_by_unrun_gates_says_so():
    fixture = run({"get_form": "INCOMPLETE", "get_concept": "INCOMPLETE"})
    fixture["tools"]["get_form"]["counts"] = {"passed": 3}
    fixture["tools"]["get_concept"]["counts"] = {"passed": 2, "skipped": 1}
    fixture["unrun_gates"] = ["t.py::gate"]
    combined = {name: (row["outcome"], []) for name, row in fixture["tools"].items()}

    text = render(fixture, combined, "fixture only")

    assert "| `get_form` | cadsr | INCOMPLETE (gates not run) | 3 / 0 / 0 / 0 |  |" in text
    assert "| `get_concept` | evs | INCOMPLETE | 2 / 0 / 0 / 1 |  |" in text


def test_skips_are_classified_by_the_property_their_site_sets_not_by_reason_text():
    report = collected(
        (phase("call", "skipped", "t.py::a", "tool absent: get_form", skip=NI), "get_form", False),
        (phase("setup", "skipped", "t.py::b", "recordings only", skip=LIVE), "get_form", False),
        (phase("call", "skipped", "t.py::c", "NOT IMPLEMENTED: get_form"), "get_form", False),
        (phase("setup", "skipped", "t.py::d", "fixture mode only"), "get_form", False),
    )

    assert {nodeid: test["outcome"] for nodeid, test in report["tests"].items()} == {
        "t.py::a": "not_implemented",
        "t.py::b": "not_live",
        "t.py::c": "skipped",
        "t.py::d": "skipped",
    }


def test_a_gate_skipped_by_design_in_a_live_run_leaves_a_passing_tool_passing():
    # A fixture-only gate does not run live by design, as a fixture-only test does not.
    report = collected(
        (phase("call", "passed", "t.py::form"), "get_form", False),
        (phase("setup", "skipped", "t.py::gate", "fixture mode only", skip=LIVE), None, True),
    )

    assert report["tools"]["get_form"]["outcome"] == "PASS"
    assert report["unrun_gates"] == ["t.py::gate"]


def test_a_gate_that_found_no_fixture_fails_every_passing_tool():
    missing = ["GET evs /api/v1/version {}"]
    report = collected(
        (phase("call", "passed", "t.py::form"), "get_form", False),
        (phase("call", "failed", "t.py::gate", unmatched=missing), None, True),
    )

    form = report["tools"]["get_form"]
    assert (report["failed_gates"], form["outcome"], form["gates_only"]) == (
        ["t.py::gate"],
        "FAIL",
        True,
    )


def _rendered(report):
    report = report | {"suite": SUITE}
    combined = {name: (row["outcome"], []) for name, row in report["tools"].items()}
    return render(report, combined, "fixture only")


def test_only_an_incomplete_verdict_is_said_to_rest_on_unrun_gates():
    report = collected(
        (
            phase("setup", "skipped", "t.py::live", "fixture mode only", skip=LIVE),
            "get_form",
            False,
        ),
        (phase("call", "passed", "t.py::concept"), "get_concept", False),
        (phase("setup", "skipped", "t.py::gate", "no security server"), None, True),
    )

    text = _rendered(report)

    assert "| `get_form` | cadsr | NOT RUN | 0 / 0 / 0 / 1 |  |" in text
    assert "| `get_concepts` | evs | NO TESTS | 0 / 0 / 0 / 0 |  |" in text
    assert "| `get_concept` | evs | INCOMPLETE (gates not run) | 1 / 0 / 0 / 0 |  |" in text


def test_a_tool_incomplete_by_its_own_absent_tool_is_plainly_incomplete():
    report = collected(
        (phase("call", "passed", "t.py::form"), "get_form", False),
        (phase("call", "skipped", "t.py::other", "NOT IMPLEMENTED: x", skip=NI), "get_form", False),
        (phase("setup", "skipped", "t.py::gate", "no security server"), None, True),
    )

    assert "| `get_form` | cadsr | INCOMPLETE | 1 / 0 / 0 / 1 |  |" in _rendered(report)


def test_a_tool_that_did_not_run_is_not_shown_failing_by_the_gates():
    # gates_only holds for a tool none of whose own tests failed; it is FAIL only if one passed.
    report = collected(
        (phase("setup", "skipped", "t.py::form", "needs a prepared index"), "get_form", False),
        (phase("call", "failed", "t.py::gate"), None, True),
    )

    form = report["tools"]["get_form"]
    assert (form["outcome"], form["gates_only"]) == ("NOT RUN", True)
    assert "| `get_form` | cadsr | NOT RUN | 0 / 0 / 0 / 1 |  |" in _rendered(report)


def test_a_gate_that_calls_an_absent_tool_leaves_no_tool_passing():
    report = collected(
        (phase("call", "passed", "t.py::form"), "get_form", False),
        (phase("call", "skipped", "t.py::gate", "NOT IMPLEMENTED: x", skip=NI), None, True),
    )

    assert report["tools"]["get_form"]["outcome"] == "INCOMPLETE"


def test_a_tool_without_a_fixture_is_not_failing_by_the_gates_only():
    missing = ["GET evs /api/v1/version {}"]
    report = collected(
        (phase("call", "failed", "t.py::form", unmatched=missing), "get_form", False),
        (phase("call", "failed", "t.py::gate"), None, True),
    )

    form = report["tools"]["get_form"]
    assert (form["outcome"], form["gates_only"]) == ("NO FIXTURE", False)
