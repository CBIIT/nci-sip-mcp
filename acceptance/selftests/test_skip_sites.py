"""Each skip site says why a test did not run, so the report needs no reason text (#265)."""

import json

from conftest import STAND_IN, synthetic_live

pytest_plugins = ["pytester"]

ABSENT_TOOL = """import pytest


@pytest.mark.tool("get_form")
def test_calls_a_tool_the_server_does_not_list(tools):
    tools.call("get_form", {"publicId": "2200604"})
"""
FIXTURE_ONLY = """import pytest


@pytest.mark.tool("get_concept")
def test_runs_against_fixtures_only():
    pass
"""


def _outcome(compliant, source):
    (compliant.path / "tests" / "test_site.py").write_text(source, encoding="utf-8")
    report = compliant.path / "report.json"
    compliant.runpytest_subprocess(
        "tests/test_site.py", "-p", "no:cacheprovider", "-p", STAND_IN, f"--report={report}"
    )
    (test,) = json.loads(report.read_text(encoding="utf-8"))["tests"].values()
    return test


def test_a_call_to_a_tool_the_server_does_not_list_is_not_implemented(compliant):
    # The compliant server serves the EVS profile here, which has no get_form.
    assert _outcome(compliant, ABSENT_TOOL)["outcome"] == "not_implemented"


def test_a_live_run_counts_a_test_that_is_not_live_capable_as_not_live(compliant, monkeypatch):
    synthetic_live(compliant)
    monkeypatch.setenv("NCI_SIP_ACCEPTANCE_MODE", "live")

    assert _outcome(compliant, FIXTURE_ONLY) == {
        "tool": "get_concept",
        "gate": False,
        "outcome": "not_live",
        "unmatched": [],
        "not_live_reason": "not_live_capable",
    }
