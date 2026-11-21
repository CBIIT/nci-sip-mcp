"""A run on several xdist workers writes the report a serial run writes: the controller process
that writes it sees only what the workers forward."""

import json

from conftest import STAND_IN, synthetic_live

pytest_plugins = ["pytester"]


def written(compliant, *options):
    report = compliant.path / "report.json"
    report.unlink(missing_ok=True)
    compliant.runpytest_subprocess(
        "tests", "-p", "no:cacheprovider", "-p", STAND_IN, f"--report={report}", *options
    )
    return json.loads(report.read_text(encoding="utf-8"))


def test_a_report_of_a_run_on_two_workers_equals_the_report_of_a_serial_run(compliant):
    selection = ("-k", "test_protocol or test_each_tool_has_a_call")

    serial = written(compliant, *selection)
    parallel = written(compliant, *selection, "-n", "2")

    assert serial["tests"]
    assert parallel["tests"] == serial["tests"]
    assert parallel["tools"] == serial["tools"]
    assert parallel == serial
    assert list(parallel["tests"]) == sorted(parallel["tests"])


def test_a_report_of_a_real_run_attributes_each_test_to_its_tool_and_gate(compliant):
    report = written(compliant, "-k", "test_protocol or test_crosscutting and get_concepts")

    attributed = {(test["tool"], test["gate"]) for test in report["tests"].values()}
    assert attributed == {(None, True), ("get_concepts", False)}
    tool_tests = [test for test in report["tests"].values() if test["tool"] == "get_concepts"]
    assert report["tools"]["get_concepts"]["counts"] == {"passed": len(tool_tests)}
    assert report["tools"]["get_concepts"]["outcome"] == "PASS"
    assert report["tools"]["get_concept"]["counts"] == {}


def test_live_skip_reasons_cross_two_worker_boundary_without_an_upstream_call(
    compliant, monkeypatch
):
    synthetic_live(compliant)
    monkeypatch.setenv("NCI_SIP_ACCEPTANCE_MODE", "live")
    (compliant.path / "tests" / "test_reasons.py").write_text(
        'import pytest\npytestmark = pytest.mark.tool("get_form")\n'
        'def test_fixture_only():\n    pytest.fail("must not execute")\n'
        '@pytest.mark.scenario("fault")\n'
        'def test_scenario():\n    pytest.fail("must not execute")\n'
        '@pytest.mark.parametrize("value", [\n'
        '    pytest.param("live", marks=pytest.mark.live_capable),\n'
        '    pytest.param("scenario", marks=pytest.mark.scenario("fault")),\n'
        "])\n"
        'def test_siblings(value):\n    assert value == "live"\n',
        encoding="utf-8",
    )
    serial = written(compliant, "-k", "test_reasons")
    parallel = written(compliant, "-k", "test_reasons", "-n", "2")
    assert {
        row.get("not_live_reason")
        for row in parallel["tests"].values()
        if row["outcome"] == "not_live"
    } == {
        "scenario",
        "not_live_capable",
    }
    assert parallel == serial
    siblings = {
        node.rsplit("[", 1)[-1]: (row["outcome"], row.get("not_live_reason"))
        for node, row in parallel["tests"].items()
        if "test_siblings[" in node
    }
    assert siblings == {"live]": ("passed", None), "scenario]": ("not_live", "scenario")}
