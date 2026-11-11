"""A run of the suite, end to end, from a copy of the suite's conftest and the furnished server.

These check what only a whole run shows: the missing-fixture guard, at startup too, a
scenario served with its settings to a server process of its own, direct tool calls, and the
per-tool report.
"""

import json
import shlex
import sys
from pathlib import Path

import pytest

from conftest import STAND_IN, stand_in, synthetic_live

pytest_plugins = ["pytester"]

SUITE_CONFTEST = Path(__file__).parent.parent / "tests" / "conftest.py"
DISCOVERY_REQUEST = {"surface": "evs", "method": "GET", "path": "/api/v1/metadata/terminologies"}


def fixture(directory, name, response, **document):
    path = directory / name
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {
        "kind": "crafted",
        "requirement": "self-test",
        "request": DISCOVERY_REQUEST,
    } | document
    path.write_text(json.dumps(document | {"response": response}), encoding="utf-8")


@pytest.fixture
def suite(pytester, monkeypatch):
    """A copy of the suite's conftest, with no fixtures yet."""

    pytester.mkdir("fixtures")
    tests = pytester.mkdir("tests")
    stand_in(pytester)
    (tests / "conftest.py").write_text(SUITE_CONFTEST.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setenv("NCI_SI_ACCEPTANCE_MODE", "fixture")
    monkeypatch.setenv("NCI_SI_ACCEPTANCE_SERVER", f"{sys.executable} -m nci_si_mcp.cli serve")
    return pytester


def run(suite, test, *options):
    (suite.path / "tests" / "test_probe.py").write_text(test, encoding="utf-8")
    return suite.runpytest_subprocess("tests", "-p", "no:cacheprovider", "-p", STAND_IN, *options)


PROBE = """
import pytest

@pytest.mark.tool("list_terminologies")
{marker}
def test_probe(tools):
    assert tools.call("list_terminologies").tool == "list_terminologies"
"""


def test_requests_without_a_fixture_fail_the_test_and_are_listed(suite):
    result = run(suite, PROBE.format(marker=""), "--report=report.json")

    result.assert_outcomes(passed=1, errors=1)
    result.stdout.fnmatch_lines(
        [
            "E * upstream requests without a fixture:",
            "E * GET evs /api/v1/metadata/terminologies {}",
        ]
    )
    report = json.loads((suite.path / "report.json").read_text(encoding="utf-8"))
    assert report["tools"]["list_terminologies"]["outcome"] == "NO FIXTURE"


# A server that asks EVS for its terminology listing before it serves.
EAGER_SERVER = """
import os, runpy, sys, urllib.error, urllib.request
try:
    with urllib.request.urlopen(
        os.environ["NCI_SI_EVS_BASE_URL"] + "/api/v1/metadata/terminologies"
    ):
        pass
except urllib.error.HTTPError as error:
    with error:
        pass
sys.argv = ["nci-si-mcp", "serve"]
runpy.run_module("nci_si_mcp.cli", run_name="__main__")
"""


def eager(suite, monkeypatch):
    (suite.path / "eager.py").write_text(EAGER_SERVER, encoding="utf-8")
    monkeypatch.setenv("NCI_SI_ACCEPTANCE_SERVER", f"{sys.executable} {suite.path / 'eager.py'}")


def test_requests_without_a_fixture_while_the_server_starts_fail_every_test_using_it(
    suite, monkeypatch
):
    eager(suite, monkeypatch)
    test = (
        PROBE.format(marker="@pytest.mark.unmatched_upstream")
        + """
@pytest.mark.tool("list_terminologies")
def test_again(tools):
    tools.call("list_terminologies")
"""
    )
    result = run(suite, test, "--report=report.json")

    result.assert_outcomes(errors=2)
    result.stdout.fnmatch_lines(
        [
            "E * upstream requests without a fixture while the server started:",
            "E * GET evs /api/v1/metadata/terminologies {}",
        ]
    )
    report = json.loads((suite.path / "report.json").read_text(encoding="utf-8"))
    assert report["tools"]["list_terminologies"]["outcome"] == "NO FIXTURE"
    assert {test["outcome"] for test in report["tests"].values()} == {"no_fixture"}
    assert all(
        test["unmatched"] == ["GET evs /api/v1/metadata/terminologies {}"]
        for test in report["tests"].values()
    )


def test_a_test_sees_none_of_the_requests_its_server_made_while_it_started(suite, monkeypatch):
    eager(suite, monkeypatch)
    fixture(
        suite.path / "fixtures",
        "scenarios/probe/up/terminologies.json",
        {"status": 200, "body": []},
    )
    test = """
import pytest

# Every test of a suite is attributed (the report counts a gate against every tool).
pytestmark = pytest.mark.gate

@pytest.mark.scenario("probe/up")
def test_clean(tools, upstream):
    assert upstream.log() == []
"""
    result = run(suite, test)

    result.assert_outcomes(passed=1)


def test_a_server_starting_after_another_test_is_not_charged_with_its_requests(suite):
    test = """
import urllib.error, urllib.request
import pytest

# Every test of a suite is attributed (the report counts a gate against every tool).
pytestmark = pytest.mark.gate

@pytest.mark.unmatched_upstream
def test_first(upstream):
    with pytest.raises(urllib.error.HTTPError) as error:
        with urllib.request.urlopen(upstream.url + "/evs/api/v1/elsewhere"):
            pass
    with error.value:
        pass

def test_second(server):
    assert server
"""
    result = run(suite, test)

    result.assert_outcomes(passed=2)


def test_a_live_run_reports_its_mode_and_a_tool_with_only_fixture_tests_as_not_run(
    suite, monkeypatch
):
    synthetic_live(suite)
    monkeypatch.setenv("NCI_SI_ACCEPTANCE_MODE", "live")

    result = run(suite, PROBE.format(marker=""), "--report=report.json")

    result.assert_outcomes(skipped=1)
    report = json.loads((suite.path / "report.json").read_text(encoding="utf-8"))
    assert (report["mode"], report["tools"]["list_terminologies"]["outcome"]) == ("live", "NOT RUN")


def test_a_test_marked_unmatched_upstream_may_leave_them(suite):
    result = run(suite, PROBE.format(marker="@pytest.mark.unmatched_upstream"))

    result.assert_outcomes(passed=1)


def test_a_scenario_is_served_to_a_server_of_its_own_and_ends_with_the_test(suite):
    fixtures = suite.path / "fixtures"
    fixture(fixtures, "recorded/terminologies.json", {"status": 200, "body": []})
    fixture(
        fixtures,
        "scenarios/probe/other/terminologies.json",
        {"status": 200, "body": []},
    )
    test = """
import pytest

# Every test of a suite is attributed (the report counts a gate against every tool).
pytestmark = pytest.mark.gate

def recordings(upstream):
    return [
        entry["fixture"] for entry in upstream.log()
        if entry["path"] == "/api/v1/metadata/terminologies"
    ]

@pytest.mark.unmatched_upstream
@pytest.mark.scenario("probe/other")
def test_during(tools, server, upstream):
    assert tools is not server
    tools.call("list_terminologies")
    assert recordings(upstream) == ["scenarios/probe/other/terminologies.json"]

@pytest.mark.unmatched_upstream
def test_after(tools, upstream):
    tools.call("list_terminologies")
    assert recordings(upstream) == ["recorded/terminologies.json"]
"""
    result = run(suite, test)

    result.assert_outcomes(passed=2)


def test_a_scenario_starts_its_server_with_its_settings(suite):
    scenario = suite.path / "fixtures" / "scenarios" / "probe" / "single-attempt"
    fixture(scenario, "terminologies.json", {"status": 503, "body": {"message": "down"}})
    (scenario / "settings.json").write_text('{"NCI_SI_EVS_MAX_ATTEMPTS": "1"}', encoding="utf-8")
    test = """
import pytest

# Every test of a suite is attributed (the report counts a gate against every tool).
pytestmark = pytest.mark.gate

def attempts(upstream):
    return [entry for entry in upstream.log() if entry["path"] == "/api/v1/metadata/terminologies"]

@pytest.mark.unmatched_upstream
@pytest.mark.scenario("probe/single-attempt")
def test_once(tools, upstream):
    tools.call("list_terminologies")
    assert len(attempts(upstream)) == 1
"""
    result = run(suite, test)

    result.assert_outcomes(passed=1)


def test_a_failed_gate_fails_every_tool_whose_own_tests_pass(suite):
    fixture(suite.path / "fixtures", "recorded/terminologies.json", {"status": 200, "body": []})
    test = """
import pytest

@pytest.mark.unmatched_upstream
@pytest.mark.tool("list_terminologies")
def test_tool(tools):
    tools.call("list_terminologies")

@pytest.mark.gate
def test_gate():
    assert False, "the gate does not hold"
"""
    result = run(suite, test, "--report=report.json")

    result.assert_outcomes(passed=1, failed=1)
    report = json.loads((suite.path / "report.json").read_text(encoding="utf-8"))
    assert report["failed_gates"] == ["tests/test_probe.py::test_gate"]
    assert (
        report["tools"]["list_terminologies"]["outcome"],
        report["tools"]["list_terminologies"]["gates_only"],
    ) == ("FAIL", True)


def test_the_report_gives_each_required_tool_its_outcome(suite, monkeypatch):
    # Keep the absent-tool case deliberate as the furnished server gains capabilities.
    server = suite.path / "without_form.py"
    server.write_text(
        "import sys\nfrom nci_si_mcp import cli, server\n"
        "server.SPECS = tuple(s for s in server.SPECS if s.name != 'get_form')\n"
        "sys.argv = ['nci-si-mcp', 'serve']\nraise SystemExit(cli.main())\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("NCI_SI_ACCEPTANCE_SERVER", shlex.join([sys.executable, str(server)]))
    test = """
import pytest

@pytest.mark.unmatched_upstream
@pytest.mark.tool("list_terminologies")
def test_direct(tools):
    tools.call("list_terminologies")

@pytest.mark.tool("get_form")
def test_absent(tools):
    tools.call("get_form", {"publicId": "123"})
"""
    result = run(suite, test, "--report=report.json")

    result.assert_outcomes(passed=1, skipped=1)
    report = json.loads((suite.path / "report.json").read_text(encoding="utf-8"))
    tools = report["tools"]
    assert tools["list_terminologies"] == {
        "group": "evs",
        "outcome": "PASS",
        "gates_only": False,
        "counts": {"passed": 1},
    }
    assert tools["get_form"]["outcome"] == "NOT IMPLEMENTED"
    assert tools["list_contexts"]["outcome"] == "NO TESTS"
    assert {nodeid: test["outcome"] for nodeid, test in report["tests"].items()} == {
        "tests/test_probe.py::test_direct": "passed",
        "tests/test_probe.py::test_absent": "not_implemented",
    }


def test_the_report_names_direct_tools_including_unsupported_capabilities(suite):
    test = """
import pytest

@pytest.mark.unmatched_upstream
@pytest.mark.tool("get_concept_hierarchy")
def test_parent(tools):
    result = tools.call("get_concept_hierarchy", {
        "terminology": "ncit", "release": "26.09d", "code": "C3262",
        "direction": "parent", "depth": 1
    })
    assert result.tool == "get_concept_hierarchy"

@pytest.mark.tool("search_concepts")
def test_semantic_without_index(tools):
    result = tools.call("search_concepts", {
        "terminology": "ncit", "release": "26.09d", "query": "neoplasm", "mode": "semantic"
    })
    assert result.is_error
    assert result.content["error"]["code"] == "capability_unavailable"
"""
    result = run(suite, test, "--report=report.json")

    result.assert_outcomes(passed=2)
    tools = json.loads((suite.path / "report.json").read_text(encoding="utf-8"))["tools"]
    assert (
        tools["get_concept_hierarchy"]["outcome"],
        tools["get_concept_hierarchy"]["counts"],
    ) == (
        "PASS",
        {"passed": 1},
    )
    assert tools["search_concepts"]["counts"] == {"passed": 1}


# A fixture set's manifest with an index set: the concepts recorded at an include that holds
# the summary, not those recorded minimal.
INDEXED = """evs:
  release: ncit_26.09d
record:
  concepts:
    full: [C4817]
    summary,parents: [C3262]
    minimal: [C2991]
"""
PREPARED_PROBE = """
import pytest

@pytest.mark.prepared
@pytest.mark.tool("list_terminologies")
def test_shared(tools):
    codes = (tools.process.data / "codes.txt").read_text(encoding="utf-8")
    assert codes.split() == ["C4817", "C3262"]
    # What a server writes stays in its own copy (the next test starts another).
    (tools.process.data / "written.txt").write_text("shared", encoding="utf-8")

@pytest.mark.prepared
@pytest.mark.own_server
@pytest.mark.tool("list_terminologies")
def test_own(tools):
    assert (tools.process.data / "codes.txt").exists()
    assert not (tools.process.data / "written.txt").exists()

@pytest.mark.unprepared
@pytest.mark.tool("list_terminologies")
def test_unprepared(tools):
    assert not (tools.process.data / "codes.txt").exists()
"""


def prepare(suite, monkeypatch, script):
    """The prepare command: `script` run by this Python, with the data directory and the
    index set's file in its environment."""

    (suite.path / "fixtures" / "manifest.yaml").write_text(INDEXED, encoding="utf-8")
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"
    monkeypatch.setenv("NCI_SI_ACCEPTANCE_PREPARE", command)


def test_the_prepare_command_runs_once_and_every_server_starts_from_its_data(suite, monkeypatch):
    runs = suite.path / "runs.txt"
    prepare(
        suite,
        monkeypatch,
        "import os, pathlib\n"
        f"with open({str(runs)!r}, 'a') as runs: runs.write('run\\n')\n"
        "codes = pathlib.Path(os.environ['NCI_SI_ACCEPTANCE_INDEX_CODES']).read_text()\n"
        "pathlib.Path(os.environ['NCI_SI_DATA_DIR'], 'codes.txt').write_text(codes)\n",
    )

    result = run(suite, PREPARED_PROBE)

    result.assert_outcomes(passed=3)
    assert runs.read_text(encoding="utf-8").splitlines() == ["run"]


def test_without_a_prepare_command_a_test_that_needs_it_is_not_run(suite):
    result = run(suite, PREPARED_PROBE, "-rs")

    result.assert_outcomes(passed=1, skipped=2)
    result.stdout.fnmatch_lines(["*NOT RUN: no prepare command*"])


@pytest.mark.parametrize(
    ("script", "said", "outcome"),
    [
        (
            "import sys\nsys.stderr.write('x' * 3000 + 'no index built\\n')\nraise SystemExit(3)",
            "*no index built*",
            "failed",
        ),
        (
            "import os, urllib.error, urllib.request\n"
            "url = os.environ['NCI_SI_EVS_BASE_URL'] + '/api/v1/metadata/terminologies'\n"
            "try:\n"
            "    with urllib.request.urlopen(url): pass\n"
            "except urllib.error.HTTPError as error:\n"
            "    with error: pass\n"
            "except OSError: pass\n",
            "*upstream requests without a fixture while preparing*",
            "no_fixture",
        ),
    ],
    ids=["failing", "unanswered"],
)
@pytest.mark.parametrize("workers", [0, 2])
def test_a_prepare_command_failure_is_reported_for_each_dependent_test(
    suite, monkeypatch, script, said, outcome, workers
):
    prepare(suite, monkeypatch, script)

    result = run(suite, PREPARED_PROBE, "-n", str(workers), "--report=report.json")

    report = json.loads((suite.path / "report.json").read_text(encoding="utf-8"))
    assert {node: test["outcome"] for node, test in report["tests"].items()} == {
        "tests/test_probe.py::test_shared": outcome,
        "tests/test_probe.py::test_own": outcome,
        "tests/test_probe.py::test_unprepared": outcome,
    }
    unmatched = ["GET evs /api/v1/metadata/terminologies {}"] if outcome == "no_fixture" else []
    assert all(test["unmatched"] == unmatched for test in report["tests"].values())
    assert report["tools"]["list_terminologies"]["outcome"] == (
        "NO FIXTURE" if unmatched else "FAIL"
    )
    assert result.ret == 1
    result.assert_outcomes(errors=3)
    result.stdout.fnmatch_lines([said])


@pytest.mark.parametrize("workers", [0, 2])
def test_a_live_prepare_failure_is_reported_without_aborting_the_run(suite, monkeypatch, workers):
    synthetic_live(suite)
    prepare(suite, monkeypatch, "raise SystemExit('live preparation unavailable')")
    monkeypatch.setenv("NCI_SI_ACCEPTANCE_MODE", "live")
    test = (
        PROBE.format(marker="@pytest.mark.live_capable")
        + """
@pytest.mark.live_capable
@pytest.mark.gate
def test_independent():
    assert 2 + 2 == 4
"""
    )

    result = run(suite, test, "-n", str(workers), "--report=report.json")

    report = json.loads((suite.path / "report.json").read_text(encoding="utf-8"))
    assert {node: test["outcome"] for node, test in report["tests"].items()} == {
        "tests/test_probe.py::test_probe": "failed",
        "tests/test_probe.py::test_independent": "passed",
    }
    result.assert_outcomes(passed=1, errors=1)
    assert result.ret == 1
    result.stdout.fnmatch_lines(["*live preparation unavailable*"])
