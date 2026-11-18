"""Only complete subprocess runs may supply acceptance verdicts or rewrite the ratchet."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from conftest import STAND_IN
from nci_sip_acceptance.report import load_report

pytest_plugins = ["pytester"]
SELECTED = 8
ARGUMENT_ERROR = 2

PROBE = """
import os
import pytest

@pytest.mark.live_capable
@pytest.mark.tool("get_form")
@pytest.mark.parametrize("case", range(8))
def test_probe(case, request):
    {body}
"""


def produce(compliant, *options, body="assert case != 0", workers=2):
    (compliant.path / "tests/test_completion.py").write_text(PROBE.format(body=body))
    report = compliant.path / "report.json"
    report.unlink(missing_ok=True)
    result = compliant.runpytest_subprocess(
        "tests/test_completion.py",
        "-n",
        str(workers),
        "-p",
        "no:cacheprovider",
        "-p",
        STAND_IN,
        f"--report={report}",
        *options,
    )
    assert report.is_file(), "subprocess did not produce its own report"
    return result, report, json.loads(report.read_text())


def command(module, *arguments):
    return subprocess.run(  # noqa: S603 - fixed package entry points and test-owned paths
        [sys.executable, "-m", f"nci_sip_acceptance.{module}", *map(str, arguments)],
        capture_output=True,
        text=True,
        check=False,
    )


def test_two_workers_record_selected_count_and_accept_complete_failures(compliant):
    result, path, report = produce(compliant)

    assert report.get("run") == {
        "exit_status": 1,
        "selected": 8,
        "finished": 8,
        "worker_crashes": 0,
    }
    assert len(report["tests"]) == SELECTED
    assert result.ret == 1
    rendered = command("report", path)
    assert rendered.returncode == 0, rendered.stderr
    assert "| `get_form` | cadsr | FAIL |" in rendered.stdout


def test_two_workers_refuse_an_early_stop_with_status_two(compliant):
    result, path, report = produce(compliant, "-x")

    refused = command("report", path)
    assert "incomplete run; re-run it" in refused.stderr
    assert refused.returncode != 0
    assert "Tests run and not passing:" not in refused.stdout
    assert result.ret == pytest.ExitCode.INTERRUPTED
    assert report["run"]["selected"] == SELECTED
    assert report["run"]["finished"] < SELECTED


def test_serial_early_stop_is_refused_even_with_status_one(compliant):
    result, path, report = produce(compliant, "-x", workers=0)

    refused = command("report", path)
    assert "incomplete run; re-run it" in refused.stderr
    assert refused.returncode != 0
    assert "Tests run and not passing:" not in refused.stdout
    assert result.ret == 1
    assert report["run"]["exit_status"] == 1
    assert report["run"]["selected"] == SELECTED
    assert report["run"]["finished"] == 1


def test_a_restarted_worker_crash_cannot_become_a_complete_run(compliant):
    result, path, report = produce(compliant, body="os._exit(7) if case == 0 else None")

    refused = command("report", path)
    assert "incomplete run; re-run it" in refused.stderr
    assert refused.returncode != 0
    assert report["run"]["selected"] == SELECTED
    assert report["run"]["worker_crashes"] == 1
    assert result.ret == 1


def test_a_reported_case_that_crashes_in_its_finalizer_never_counts_as_finished(compliant):
    result, path, report = produce(
        compliant, body="request.addfinalizer(lambda: os._exit(7)) if case == 0 else None"
    )
    assert "incomplete run; re-run it" in command("report", path).stderr
    assert report["run"]["finished"] == SELECTED - 1
    assert len(report["tests"]) == report["run"]["selected"] == SELECTED
    assert result.ret == 1
    # Isolate finished-count validation from the independently tested crash refusal.
    report["run"]["worker_crashes"] = 0
    path.write_text(json.dumps(report))
    assert "incomplete run; re-run it" in command("report", path).stderr


def test_complete_skipped_tests_are_still_a_completed_run(compliant):
    result, path, report = produce(compliant, body="pytest.skip('not applicable')")

    assert report.get("run") == {
        "exit_status": 0,
        "selected": 8,
        "finished": 8,
        "worker_crashes": 0,
    }
    assert result.ret == 0
    rendered = command("report", path)
    assert rendered.returncode == 0, rendered.stderr
    assert "NOT RUN" in rendered.stdout


@pytest.fixture
def complete(compliant):
    _, path, report = produce(compliant)
    return path, report


@pytest.mark.parametrize(
    "changes", [{"worker_crashes": 1}, *({"exit_status": status} for status in (2, 3, 4))]
)
def test_crash_and_exit_status_refuse_even_equal_counts(complete, changes):
    path, report = complete
    report["run"].update(changes)
    path.write_text(json.dumps(report))
    with pytest.raises(SystemExit, match="incomplete run; re-run it"):
        load_report(path, "fixture")


ONE_RUN = {"exit_status": 0, "selected": 1, "finished": 1, "worker_crashes": 0}


@pytest.mark.parametrize(
    "run",
    [None, [], ONE_RUN | {"worker_crashes": False}]
    + [ONE_RUN | {field: True} for field in ONE_RUN]
    + [{key: value for key, value in ONE_RUN.items() if key != field} for field in ONE_RUN],
)
def test_invalid_run_metadata_is_refused_cleanly(compliant, run):
    _, path, report = produce(compliant, "-k", "[1]")
    assert load_report(path, "fixture")["run"] == ONE_RUN
    report["run"] = run
    path.write_text(json.dumps(report))
    with pytest.raises(SystemExit, match="incomplete run; re-run it"):
        load_report(path, "fixture")


@pytest.mark.parametrize("operation", ["check", "update"])
@pytest.mark.parametrize("finished", [8, 9], ids=["unfinished", "unrecorded"])
def test_the_ratchet_refuses_an_aborted_fixture_report_without_rewriting_it(
    complete, operation, finished
):
    path, report = complete
    expected = path.with_name("expected.json")
    expected.write_text('{"keep-this-evidence": "passed"}\n')
    original = expected.read_bytes()
    report["run"] = {"exit_status": 1, "selected": 9, "finished": finished, "worker_crashes": 0}
    path.write_text(json.dumps(report))

    refused = command("expected", operation, path, "--expected", expected)

    assert "incomplete run; re-run it" in refused.stderr
    assert expected.read_bytes() == original
    assert refused.returncode != 0


def test_the_ratchet_accepts_a_complete_failing_run(complete):
    path, report = complete
    expected = path.with_name("expected.json")

    updated = command("expected", "update", path, "--expected", expected)
    checked = command("expected", "check", path, "--expected", expected)

    assert updated.returncode == checked.returncode == 0
    assert json.loads(expected.read_text()) == {
        node: test["outcome"] for node, test in report["tests"].items()
    }


@pytest.mark.parametrize("operation", ["render", "check", "update"])
def test_reports_without_completion_evidence_are_refused(complete, operation):
    path, report = complete
    report.pop("run", None)
    path.write_text(json.dumps(report))
    module, arguments = (
        ("report", [path])
        if operation == "render"
        else ("expected", [operation, path, "--expected", path.with_name("expected.json")])
    )
    path.with_name("expected.json").write_text("{}")

    refused = command(module, *arguments)

    assert "older suite; re-run it" in refused.stderr
    assert refused.returncode != 0


@pytest.mark.parametrize("mode", ["fixture", "live"])
def test_workflow_completion_check_refuses_partial_reports(complete, mode):
    path, report = complete
    report["mode"] = mode
    report["run"] = {"exit_status": 1, "selected": 9, "finished": 8, "worker_crashes": 0}
    path.write_text(json.dumps(report))

    refused = command("report", path, "--check-complete", mode)

    assert "incomplete run; re-run it" in refused.stderr
    assert refused.returncode != 0


def test_combination_and_drift_refuse_an_empty_live_report(complete):
    path, report = complete
    live = path.with_name("live.json")
    report |= {
        "mode": "live",
        "tests": {},
        "run": {
            "exit_status": 1,
            "selected": 8,
            "finished": 0,
            "worker_crashes": 0,
        },
    }
    live.write_text(json.dumps(report))

    rendered = command("report", path, "--live", live)
    drift = command("report", path, "--live", live, "--drift")

    assert "incomplete run; re-run it" in rendered.stderr
    assert "incomplete run; re-run it" in drift.stderr
    assert rendered.returncode != 0 and drift.returncode != 0
    assert "Tests run and not passing:" not in rendered.stdout
    assert "0 tests pass" not in drift.stdout


@pytest.mark.parametrize(
    ("fixture_outcome", "live_outcome", "count"),
    [
        ("passed", "failed", 1),
        ("skipped", "failed", 0),
        ("not_live", "failed", 0),
        ("passed", "no_fixture", 1),
    ],
)
def test_drift_reports_only_fixture_passes_that_fail_live(
    complete, fixture_outcome, live_outcome, count
):
    path, report = complete
    live = path.with_name("live.json")
    report["tests"]["tests/test_completion.py::test_probe[1]"]["outcome"] = fixture_outcome
    path.write_text(json.dumps(report))
    report["mode"] = "live"
    report["tests"]["tests/test_completion.py::test_probe[1]"]["outcome"] = live_outcome
    live.write_text(json.dumps(report))

    drift = command("report", path, "--live", live, "--drift")

    listed = "- `tests/test_completion.py::test_probe[1]`\n" if count else ""
    assert drift.stdout == f"{count} tests pass on the fixtures and fail live.\n{listed}"
    assert drift.returncode == count


def test_check_complete_is_silent_and_drift_requires_live(complete):
    path, _ = complete
    checked = command("report", path, "--check-complete", "fixture")
    assert checked.stdout == ""
    assert checked.returncode == 0
    refused = command("report", path, "--drift")
    assert refused.returncode == ARGUMENT_ERROR
    assert "--drift requires --live" in refused.stderr


def test_wrong_mode_names_the_expected_run(complete):
    path, _ = complete
    with pytest.raises(SystemExit, match="not a live run"):
        load_report(path, "live")


@pytest.mark.parametrize("options", [(), ("--drift",)], ids=["combined", "drift"])
def test_combined_consumers_refuse_different_complete_selections(compliant, options):
    _, path, _ = produce(compliant)
    fixture = path.with_name("fixture.json")
    fixture.write_bytes(path.read_bytes())
    _, _, historical = produce(compliant, "--deselect=tests/test_completion.py::test_probe[1]")
    # Synthetic legacy-shaped consumer input, NOT evidence of a live execution.
    historical["mode"] = "live"
    live = path.with_name("synthetic-historical-live.json")
    live.write_text(json.dumps(historical))

    refused = command("report", fixture, "--live", live, *options)

    assert "reports select different tests; re-run them" in refused.stderr
    assert refused.returncode != 0
    assert "Tests run and not passing:" not in refused.stdout


@pytest.mark.parametrize("mode", ["fixture", "live"])
def test_workflow_run_steps_reject_status_one_with_an_incomplete_report(complete, mode):
    path, report = complete
    root = path.parent
    acceptance = root / "acceptance"
    acceptance.mkdir()
    report |= {
        "mode": mode,
        "run": {
            "exit_status": 1,
            "selected": 9,
            "finished": 8,
            "worker_crashes": 0,
        },
    }
    (acceptance / f"{mode}.json").write_text(json.dumps(report))
    pdm = root / "pdm"
    pdm.write_text(
        f"#!{sys.executable}\nimport os, sys\n"
        "if sys.argv[1:3] == ['run', 'acceptance']: sys.exit(1)\n"
        "os.execv(sys.executable, [sys.executable, *sys.argv[3:]])\n"
    )
    pdm.chmod(0o755)
    workflow = Path(__file__).parents[2] / ".github/workflows/acceptance-live.yml"
    steps = yaml.safe_load(workflow.read_text())["jobs"]["acceptance-live"]["steps"]
    name = "Run the suite against " + ("the fixtures" if mode == "fixture" else "the live services")
    script = next(step["run"] for step in steps if step.get("name") == name)
    environment = os.environ | {
        "PATH": f"{root}:{os.environ['PATH']}",
        "GITHUB_STEP_SUMMARY": str(root / "summary.md"),
        "NCI_SIP_EVS_LICENSE_KEY": "",
        "NCI_SIP_CADSR_CREDENTIAL": "",
    }

    result = subprocess.run(  # noqa: S603 - execute the checked-in workflow step with a test stand-in
        ["/bin/bash", "-e", "-c", script],
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0, result.stdout
    assert "incomplete run; re-run it" in result.stderr
