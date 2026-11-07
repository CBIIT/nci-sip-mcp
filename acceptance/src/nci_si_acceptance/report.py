"""The per-tool report (docs/specification.md §5).

A run of the suite writes one report per run mode (`pytest --report=PATH`), with the
final outcome of every test, the transport of the run (stdio, or streamable-http for a remote
server) and the identity of the suite (suite_identity.py). A test counts for
the tool its `tool` marker names; a test marked `gate` gates every tool (§4). One run gives
each required tool one outcome:

    PASS             every test of the tool ran and passed, and every gate
    FAIL             a test of the tool failed, or a gate did (shown as "gates only")
    NO FIXTURE       the tool's tests failed only because a request found no fixture:
                     a question for the fixture set, not a defect of the server
    INCOMPLETE       the tool's tests that ran passed, but some could not run: skipped, or
                     needing a capability the tool lacks, or a gate could not run (shown as
                     "gates not run"); a hardening candidate, not a pass
    NOT IMPLEMENTED  the server does not expose the required tool name
    NOT RUN          no test of the tool ran (for example, all were skipped or not_live)
    NO TESTS         the suite has no test for the tool: a defect of the suite

In a live run, the tests that run in fixture mode only (not_live) leave a tool NOT RUN, never
INCOMPLETE, and the gates among them change no outcome: they do not run live by design, while a
gate that could not run did not.

Combining the fixture run with the live run gives the final outcome. A tool that
passes against fixtures is PASS (fixture only) when each of its live failures is a
test with a documented upstream limitation, and FAIL otherwise; a gate that fails live
fails every tool in the same way. Limitations are documented per test, in YAML:
`<test id>: <upstream requirement>`.

    python -m nci_si_acceptance.report fixture.json [--live live.json] [--limitations FILE]

PYTEST_DONT_REWRITE: the suite's conftest imports this plugin before pytest could
rewrite its assertions, and it has none.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from dataclasses import asdict, dataclass, fields
from enum import Enum
from html import escape
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, TypeIs, get_args

import pytest
import yaml

from nci_si_acceptance.client import withhold_authorization
from nci_si_acceptance.environments import EnvironmentRecord, environment_record
from nci_si_acceptance.spec import REQUIRED_TOOLS
from nci_si_acceptance.suite import NOT_LIVE_REASON, SKIP, UnmatchedUpstream
from nci_si_acceptance.suite_identity import (
    APPROVED,
    REPOSITORY,
    SuiteError,
    approved_digests,
    heading,
    identity,
)
from nci_si_acceptance.tools import NOT_IMPLEMENTED, NotImplementedSkip

if TYPE_CHECKING:
    from collections.abc import Iterable

    from nci_si_acceptance.tools import Tools

PASS, FAIL, NO_FIXTURE = "PASS", "FAIL", "NO FIXTURE"
INCOMPLETE, NOT_RUN, NO_TESTS = "INCOMPLETE", "NOT RUN", "NO TESTS"
FIXTURE_ONLY = "PASS (fixture only)"
# The property a test failing for want of fixtures carries: the requests concerned.
UNMATCHED = "unmatched_upstream"
# The property that carries a test's tool and gate to wherever the report is collected.
ATTRIBUTION = "acceptance_attribution"
# The key of a worker's output that carries what it noted of the server under test.
WORKER_TOOLS = "acceptance_tools"
# A later phase of a test (setup, call, teardown) overrides an earlier outcome only
# when it ranks higher or equally; the later outcome wins a tie.
type UnrunOutcome = Literal["skipped", "not_implemented", "not_live"]
type FailureOutcome = Literal["failed", "no_fixture"]
type NonPassingOutcome = UnrunOutcome | FailureOutcome
type Outcome = Literal["passed"] | NonPassingOutcome
RANK: dict[Outcome, int] = {
    "passed": 0,
    "skipped": 1,
    "not_implemented": 1,
    "not_live": 1,
    "failed": 2,
    "no_fixture": 3,
}
FAILED: tuple[FailureOutcome, ...] = get_args(FailureOutcome.__value__)
UNRUN: tuple[UnrunOutcome, ...] = get_args(UnrunOutcome.__value__)
# Those of the unrun outcomes that could not run. A live run's fixture-only tests and gates do
# not run there by design (not_live), so they keep no tool from passing.
CANNOT_RUN = ("skipped", "not_implemented")


def is_outcome(value: str) -> TypeIs[Outcome]:
    """Whether a string is in the report's one closed outcome vocabulary."""

    return value in RANK


def is_unrun_outcome(value: str) -> TypeIs[UnrunOutcome]:
    """Whether a string is an outcome that says the test did not run to a verdict."""

    return value in UNRUN


class ReportFieldError(ValueError):
    """A known field location, never an imported value or dynamic key."""


def require_field(condition: bool, path: str) -> None:
    if not condition:
        raise ReportFieldError(f"Invalid acceptance field: {path}")


class Absent(Enum):
    FIELD = "absent"


def record_shape(value: Any, record: type, path: str) -> None:
    require_field(isinstance(value, dict), path or "report")
    declared = fields(record)
    for item in declared:
        if item.default != Absent.FIELD:
            require_field(item.name in value, f"{path}.{item.name}".lstrip("."))
    require_field(set(value) <= {item.name for item in declared}, path or "report")


@dataclass(frozen=True)
class Attribution:
    tool: str | None
    gate: bool


@dataclass(frozen=True)
class TestResult(Attribution):
    outcome: Outcome
    unmatched: list[str]
    not_live_reason: Literal["scenario", "not_live_capable"] | Absent = Absent.FIELD

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        if self.not_live_reason == Absent.FIELD:
            result.pop(NOT_LIVE_REASON)
        return result

    @classmethod
    def from_dict(cls, value: Any, path: str) -> TestResult:
        record_shape(value, cls, path)
        require_field(type(value["gate"]) is bool, f"{path}.gate")
        require_field(
            isinstance(value["outcome"], str) and value["outcome"] in RANK, f"{path}.outcome"
        )
        require_field(isinstance(value["unmatched"], list), f"{path}.unmatched")
        require_field(
            all(isinstance(item, str) for item in value["unmatched"]), f"{path}.unmatched"
        )
        if NOT_LIVE_REASON in value:
            require_field(
                value["outcome"] == "not_live"
                and value[NOT_LIVE_REASON] in ("scenario", "not_live_capable"),
                f"{path}.{NOT_LIVE_REASON}",
            )
        return cls(**value)


@dataclass(frozen=True)
class ToolResult:
    group: str
    outcome: str
    gates_only: bool
    counts: dict[str, int]
    implemented_as: str | Absent | None = Absent.FIELD

    def to_dict(self) -> dict[str, Any]:
        return {name: value for name, value in asdict(self).items() if value != Absent.FIELD}

    @classmethod
    def from_dict(cls, value: Any, path: str) -> ToolResult:
        record_shape(value, cls, path)
        require_field(isinstance(value["counts"], dict), f"{path}.counts")
        require_field(
            all(type(count) is int and count > 0 for count in value["counts"].values()),
            f"{path}.counts",
        )
        require_field(isinstance(value["outcome"], str), f"{path}.outcome")
        require_field(type(value["gates_only"]) is bool, f"{path}.gates_only")
        alias = value.get("implemented_as")
        require_field(alias is None or isinstance(alias, str), f"{path}.implemented_as")
        return cls(**value)


@dataclass(frozen=True)
class RunCompletion:
    exit_status: int
    selected: int
    finished: int
    worker_crashes: int


@dataclass(frozen=True)
class ReportSuite:
    version: str
    fixture_set: str
    digest: str

    @classmethod
    def from_dict(cls, value: Any) -> ReportSuite:
        record_shape(value, cls, "suite")
        for item in fields(cls):
            require_field(isinstance(value[item.name], str), f"suite.{item.name}")
        return cls(**value)


@dataclass(frozen=True, kw_only=True)
class NativeReport:
    mode: str
    transport: str
    run: Any = Absent.FIELD
    failed_gates: list[str]
    unrun_gates: list[str]
    tools_list_bytes: int | None
    tools: dict[str, ToolResult]
    tests: dict[str, TestResult]
    suite: ReportSuite | Absent = Absent.FIELD
    environment: EnvironmentRecord | Absent = Absent.FIELD

    def to_dict(self) -> dict[str, Any]:
        result = {name: value for name, value in asdict(self).items() if value != Absent.FIELD}
        result["tools"] = {name: row.to_dict() for name, row in self.tools.items()}
        result["tests"] = {name: row.to_dict() for name, row in self.tests.items()}
        return result

    @classmethod
    def from_dict(cls, value: Any) -> NativeReport:
        record_shape(value, cls, "")
        require_field("suite" in value, "suite")
        require_field(value["mode"] in ("fixture", "live"), "mode")
        require_field(value["transport"] in ("stdio", "streamable-http"), "transport")
        size = value["tools_list_bytes"]
        require_field(size is None or (type(size) is int and size >= 0), "tools_list_bytes")
        require_field(isinstance(value["tests"], dict), "tests")
        require_field(isinstance(value["tools"], dict), "tools")
        tests = {
            name: TestResult.from_dict(row, f"tests[{index}]")
            for index, (name, row) in enumerate(value["tests"].items())
        }
        tools = {
            name: ToolResult.from_dict(row, f"tools[{index}]")
            for index, (name, row) in enumerate(value["tools"].items())
        }
        return cls(
            **(
                value
                | {
                    "tests": tests,
                    "tools": tools,
                    "suite": ReportSuite.from_dict(value["suite"]),
                    "environment": _report_environment(value["environment"], value["mode"])
                    if "environment" in value
                    else Absent.FIELD,
                }
            )
        )


def _report_environment(value: object, mode: str) -> EnvironmentRecord:
    require_field(mode == "live", "environment")
    try:
        return environment_record(value)
    except ValueError:
        raise ReportFieldError("Invalid acceptance field: environment") from None


def tool_outcome(
    counts: Counter[str], gates_failed: bool, gates_unrun: bool, implemented: bool | None
) -> str:
    """The outcome of one tool in one run, from the final outcomes of its tests and the gates.

    `implemented` is None when no server started, so the run cannot tell. A gate that did not
    run keeps a tool from passing and changes no other outcome.
    """

    if not counts:
        return NO_TESTS
    if counts["failed"] or (gates_failed and counts["passed"]):
        return FAIL
    if counts["no_fixture"]:
        return NO_FIXTURE
    return _without_failures(counts, gates_unrun, implemented)


def _without_failures(counts: Counter[str], gates_unrun: bool, implemented: bool | None) -> str:
    if implemented is False:
        return NOT_IMPLEMENTED
    if not counts["passed"]:
        return NOT_RUN
    return INCOMPLETE if own_unrun(counts) or gates_unrun else PASS


def gate_names(tests: dict[str, dict[str, Any]], outcomes: tuple[str, ...]) -> list[str]:
    """The gates among a run's tests whose final outcome is one of `outcomes`, in order."""

    return sorted(
        nodeid for nodeid, test in tests.items() if test["gate"] and test["outcome"] in outcomes
    )


def fails_by_gates_only(counts: Counter[str], gates_failed: bool) -> bool:
    """Whether a failed gate, and none of the tool's own tests, would fail the tool: none of its
    own tests failed or lacked a fixture."""

    return gates_failed and not (counts["failed"] or counts["no_fixture"])


def own_unrun(counts: Counter[str]) -> int:
    """The tool's own tests that could not run (CANNOT_RUN)."""

    return sum(counts[outcome] for outcome in CANNOT_RUN)


def _skip_outcome(report: pytest.TestReport) -> UnrunOutcome:
    # The site of the skip says why (suite.SKIP); its reason text is for people.
    outcome = str(dict(report.user_properties).get(SKIP, "skipped"))
    if not is_unrun_outcome(outcome):
        raise ValueError(f"unknown skip outcome: {outcome}")
    return outcome


def _phase_outcome(report: pytest.TestReport) -> Outcome | None:
    if report.failed:
        return "no_fixture" if dict(report.user_properties).get(UNMATCHED) else "failed"
    if report.skipped:
        return _skip_outcome(report)
    return "passed" if report.when == "call" else None


class Collector:
    """The final outcome of every test of a run, with its tool and the requests it lacked."""

    def __init__(self) -> None:
        self.tests: dict[str, dict[str, Any]] = {}
        # Which required tools the server lists; None until a server has started.
        self.implemented: dict[str, bool] | None = None
        self.listing_bytes: int | None = None
        # Set from the target at the end of the run; the default is the harness's original one.
        self.transport = "stdio"
        self.environment: EnvironmentRecord | Absent | None = Absent.FIELD
        self._environment_conflict = False
        self.finished: set[str] = set()
        self.worker_crashes = 0
        self.run: dict[str, int] | None = None

    # The collector is itself a plugin, so that it collects in the process that writes the
    # report: under xdist, the controller, which sees the workers' reports here and nothing
    # of their tests, and their notes of the server when they finish.

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        self.record(report)

    def pytest_runtest_logfinish(self, nodeid: str) -> None:
        self.finished.add(nodeid)

    @pytest.hookimpl(tryfirst=True)
    def pytest_sessionfinish(self, session: pytest.Session, exitstatus: int) -> None:
        # xdist sets the controller's testscollected from the workers' agreed collection.
        # Record this before the suite's sessionfinish hook writes the report.
        self.run = asdict(
            RunCompletion(
                int(exitstatus), session.testscollected, len(self.finished), self.worker_crashes
            )
        )
        output = getattr(session.config, "workeroutput", None)  # set on an xdist worker only
        if output is not None:
            output["nci_si_environment"] = (
                None if self.environment is Absent.FIELD else self.environment
            )
        if (noted := self.noted_tools()) is not None and output is not None:
            output[WORKER_TOOLS] = noted

    @pytest.hookimpl(optionalhook=True)
    def pytest_testnodedown(self, node: Any, error: Any) -> None:
        if error is not None:
            self.worker_crashes += 1
        output = getattr(node, "workeroutput", {})
        if not self._worker_environment_matches(output, error):
            self._environment_conflict = True
        if noted := output.get(WORKER_TOOLS):
            self.merge_tools(noted)

    def _worker_environment_matches(self, output: dict[str, object], error: object) -> bool:
        reported = output.get("nci_si_environment", Absent.FIELD)
        # A crashed fixture worker cannot send its header; keep the crash counter/report.
        # A selected live identity or an unexpected reported identity still cannot be lost.
        if self.environment is None and error is not None and reported is Absent.FIELD:
            return True
        return self.environment is Absent.FIELD or reported == self.environment

    def record(self, report: pytest.TestReport) -> None:
        """Take one phase of a test; its tool and gate come in the report's own properties,
        which is all that reaches the controller process of a run on xdist workers."""

        outcome = _phase_outcome(report)
        if outcome is None:
            return
        properties: dict[str, Any] = dict(report.user_properties)
        attribution = properties.get(ATTRIBUTION) or asdict(Attribution(None, False))
        test = self.tests.setdefault(
            report.nodeid, TestResult(**attribution, outcome=outcome, unmatched=[]).to_dict()
        )
        if RANK[outcome] >= RANK[test["outcome"]]:
            test["outcome"] = outcome
            test["unmatched"] = properties.get(UNMATCHED, [])
            test.pop(NOT_LIVE_REASON, None)
            if outcome == "not_live" and NOT_LIVE_REASON in properties:
                test[NOT_LIVE_REASON] = properties[NOT_LIVE_REASON]

    def note_tools(self, tools: Tools) -> None:
        self.implemented = {name: tools.implemented(name) for name in REQUIRED_TOOLS}
        self.listing_bytes = tools.listing_bytes

    def noted_tools(self) -> dict[str, Any] | None:
        """What a worker noted of its server, to send to the controller; None if no server."""

        if self.implemented is None:
            return None
        return {"implemented": self.implemented, "listing_bytes": self.listing_bytes}

    def merge_tools(self, noted: dict[str, Any]) -> None:
        """Take a worker's notes of its server, unless an earlier worker's are already here."""

        if self.implemented is None:
            self.implemented = noted["implemented"]
            self.listing_bytes = noted["listing_bytes"]

    def _row(self, name: str, group: str, gates_failed: bool, gates_unrun: bool) -> dict[str, Any]:
        counts = Counter(test["outcome"] for test in self.tests.values() if test["tool"] == name)
        implemented = None if self.implemented is None else self.implemented.get(name, False)
        return ToolResult(
            group,
            tool_outcome(counts, gates_failed, gates_unrun, implemented),
            fails_by_gates_only(counts, gates_failed),
            dict(counts),
        ).to_dict()

    def _environment_record(self, mode: str) -> EnvironmentRecord | Absent:
        if self._environment_conflict:
            raise ReportFieldError("Invalid acceptance field: environment")
        return (
            _report_environment(self.environment, mode)
            if isinstance(self.environment, dict)
            else Absent.FIELD
        )

    def report(self, mode: str) -> dict[str, Any]:
        gates, unrun = gate_names(self.tests, FAILED), gate_names(self.tests, UNRUN)
        blocked = bool(gate_names(self.tests, CANNOT_RUN))
        rows = {
            name: self._row(name, group, bool(gates), blocked)
            for name, group in REQUIRED_TOOLS.items()
        }
        return NativeReport(
            mode=mode,
            transport=self.transport,
            environment=self._environment_record(mode),
            run=self.run,
            failed_gates=gates,
            unrun_gates=unrun,
            tools_list_bytes=self.listing_bytes,
            tools={name: ToolResult(**row) for name, row in rows.items()},
            # Workers report in the order they finish; a report is the same whatever that was.
            tests={name: TestResult(**row) for name, row in sorted(self.tests.items())},
        ).to_dict()


def combine(
    fixture: dict[str, Any], live: dict[str, Any], limitations: dict[str, str]
) -> dict[str, tuple[str, list[str]]]:
    """Each tool's outcome over both run modes, with the limitations that excuse it."""

    combined = {}
    for name, row in fixture["tools"].items():
        failing = live["failed_gates"] + [
            nodeid
            for nodeid, test in live["tests"].items()
            if test["tool"] == name and test["outcome"] in FAILED
        ]
        combined[name] = _over_both(row["outcome"], failing, limitations)
    return combined


def _over_both(
    outcome: str, failing: list[str], limitations: dict[str, str]
) -> tuple[str, list[str]]:
    """A fixture outcome given the tool's failing live tests: excused only one by one."""

    if outcome != PASS or not failing:
        return outcome, []
    excused = sorted({limitations[nodeid] for nodeid in failing if nodeid in limitations})
    return (FIXTURE_ONLY if all(nodeid in limitations for nodeid in failing) else FAIL), excused


def _row(name: str, row: dict[str, Any], outcome: str, excused: list[str]) -> str:
    counts = Counter(row["counts"])
    # An INCOMPLETE tool whose own tests all ran can only rest on gates that could not run.
    outcome = _label(outcome, row["gates_only"], not own_unrun(counts))
    unrun = sum(counts[kind] for kind in UNRUN)
    tests = f"{counts['passed']} / {counts['failed']} / {counts['no_fixture']} / {unrun}"
    return f"| `{name}` | {row['group']} | {outcome} | {tests} | {', '.join(excused)} |"


def _label(outcome: str, gates_only: bool, by_unrun_gates: bool) -> str:
    """The outcome as a row shows it, saying so when the gates alone decide it."""

    if outcome == FAIL and gates_only:
        return "FAIL (gates only)"
    if outcome == INCOMPLETE and by_unrun_gates:
        return "INCOMPLETE (gates not run)"
    return outcome


def _named(combined: dict[str, tuple[str, list[str]]], *kinds: str) -> str:
    return ", ".join(name for name, (outcome, _) in combined.items() if outcome in kinds) or "none"


def _size(size: int | None) -> str:
    return "not measured, no server started" if size is None else f"{size:,} bytes"


def render(
    report: dict[str, Any],
    combined: dict[str, tuple[str, list[str]]],
    modes: str,
    approved: Path = APPROVED,
    *,
    live_environment: EnvironmentRecord | None = None,
) -> str:
    """The report as Markdown: the suite's identity, which says whether this is an acceptance
    report or MODIFIED (suite_identity.py), then the per-tool table and the tools whose tests
    prove nothing yet."""

    lines = [
        *heading(report["suite"], approved_digests(approved)),
        f"Run modes: {modes}.",
        *_environment_lines(live_environment),
        "",
        "| Tool | Group | Outcome | Tests passed / failed / no fixture / not run "
        "| Upstream limitation |",
        "|---|---|---|---|---|",
    ]
    lines += [_row(name, row, *combined[name]) for name, row in report["tools"].items()]
    missing = sorted(
        {request for test in report["tests"].values() for request in test.get("unmatched", [])}
    )
    lines += [
        "",
        f"Gates failed: {', '.join(report['failed_gates']) or 'none'}.",
        f"Gates not run: {', '.join(report['unrun_gates']) or 'none'}.",
        "Tests never run against an implementation: "
        f"{_named(combined, NOT_IMPLEMENTED, NOT_RUN, NO_TESTS)}.",
        f"Tests run and not passing: {_named(combined, FAIL, NO_FIXTURE, INCOMPLETE)}.",
        f"Requests without a fixture: {'; '.join(missing) or 'none'}.",
        f"Size of the tools/list result: {_size(report['tools_list_bytes'])}.",
    ]
    return "\n".join(lines) + "\n"


def _markdown_cell(value: str | None) -> str:
    # Entities survive Markdown parsing as literal text, including image/link delimiters.
    delimiters = str.maketrans({character: f"&#{ord(character)};" for character in r"\`*_{}[]()!|"})
    return escape("disabled" if value is None else value).translate(delimiters)


def _environment_lines(environment: EnvironmentRecord | None) -> list[str]:
    if environment is None:
        return []
    rows = [
        ("name", environment["name"]),
        *environment["origins"].items(),
        ("policy_sha256", environment["policy_sha256"]),
    ]
    return [
        "",
        "Live environment (recorded configuration; not access approval):",
        "",
        "| Field | Recorded value |",
        "|---|---|",
        *[f"| {escape(name)} | {_markdown_cell(value)} |" for name, value in rows],
    ]


# ---- pytest plugin

COLLECTOR = pytest.StashKey[Collector]()
SUITE = pytest.StashKey[dict[str, str]]()


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--report", metavar="PATH", help="write the per-tool report of the run as JSON"
    )


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Refuse a run that holds a test no row of the report counts: one with neither a `tool`
    nor a `gate` marker would pass or fail unseen."""

    unattributed = [
        item.nodeid
        for item in items
        if item.get_closest_marker("tool") is None and item.get_closest_marker("gate") is None
    ]
    if unattributed:
        raise pytest.UsageError(
            "tests with neither a tool nor a gate marker (the report would not count them): "
            + ", ".join(unattributed)
        )


def pytest_configure(config: pytest.Config) -> None:
    config.stash[COLLECTOR] = Collector()
    config.pluginmanager.register(config.stash[COLLECTOR], "acceptance-collector")


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[None]) -> Any:
    report = yield
    withhold_from(report)
    if call.excinfo is not None and isinstance(call.excinfo.value, UnmatchedUpstream):
        report.user_properties.append((UNMATCHED, call.excinfo.value.requests))
    if call.excinfo is not None and isinstance(call.excinfo.value, NotImplementedSkip):
        report.user_properties.append((SKIP, "not_implemented"))
    marker = item.get_closest_marker("tool")
    attribution = asdict(
        Attribution(marker.args[0] if marker else None, item.get_closest_marker("gate") is not None)
    )
    report.user_properties.append((ATTRIBUTION, attribution))
    return report


def withhold_from(report: pytest.TestReport) -> None:
    """Keep the operator's credential out of a failure's text and captured output, where a
    server that echoes it back would otherwise show it."""

    if report.longrepr is not None and not isinstance(report.longrepr, tuple):
        shown = str(report.longrepr)
        if (clean := withhold_authorization(shown)) != shown:
            report.longrepr = clean
    report.sections = [(name, withhold_authorization(text)) for name, text in report.sections]


def suite_state(config: pytest.Config) -> dict[str, str]:
    """The identity of the suite tree this run is in: the repository around the acceptance
    directory pytest runs in. It must be the tree the harness is installed from, or the report
    would name the harness's files for the tests that ran."""

    root = config.rootpath.parent
    if root != REPOSITORY:
        raise pytest.UsageError(
            f"this run is in the suite at {root}, but the harness is installed from {REPOSITORY}; "
            "install the harness from the checkout under test (pdm install)"
        )
    try:
        return identity(root)
    except SuiteError as error:
        raise pytest.UsageError(str(error)) from error


def pytest_sessionstart(session: pytest.Session) -> None:
    """Take the suite's identity before any test runs: it is the tree that ran."""

    if session.config.getoption("report"):
        session.config.stash[SUITE] = suite_state(session.config)


def write_report(config: pytest.Config, mode: str) -> None:
    """Write the run's report where `--report` says, if it says."""

    path = config.getoption("report")
    # A worker holds part of the run; the controller writes the report.
    if path and not hasattr(config, "workerinput"):
        report = config.stash[COLLECTOR].report(mode) | {"suite": config.stash[SUITE]}
        Path(path).write_text(json.dumps(withheld(report), indent=2) + "\n")


def withheld(value: Any) -> Any:
    """`value` with the operator's credential out of every string in it: before it is written as
    JSON, where a credential with quotes, backslashes or non-ASCII characters would be escaped
    and so no longer found."""

    if isinstance(value, str):
        return withhold_authorization(value)
    if isinstance(value, dict):
        return {key: withheld(item) for key, item in value.items()}
    if isinstance(value, list):
        return [withheld(item) for item in value]
    return value


def load_report(path: Path, mode: str) -> dict[str, Any]:
    """Load a fresh run only when every selected test finished without a worker crash."""
    report = json.loads(path.read_text(encoding="utf-8"))
    if "transport" not in report or "run" not in report:
        raise SystemExit(f"{path} was written by an older suite; re-run it")
    if report["mode"] != mode:
        expected = "a fixture run" if mode == "fixture" else "a live run"
        raise SystemExit(f"{path} is the report of a {report['mode']} run, not {expected}")
    if not complete(report):
        raise SystemExit(f"{path} is an incomplete run; re-run it")
    return report


def complete(report: dict[str, Any]) -> bool:
    """Whether a report's run metadata proves all selected tests finished without a crash."""
    return completion_problem(report) is None


def completion_problem(report: dict[str, Any]) -> str | None:
    """A safe explanation of refused completion, without echoing arbitrary report content."""
    run = report["run"]
    if not _valid_run_metadata(run):
        return "invalid run completion metadata"
    if run["worker_crashes"]:
        return f"worker crashes: {run['worker_crashes']}"
    if run["exit_status"] not in (0, 1):
        return f"run exit status: {run['exit_status']}"
    if not run["selected"] == run["finished"] == len(report["tests"]):
        return "selected, finished and recorded outcome counts differ"
    return None


def _valid_run_metadata(run: Any) -> bool:
    return isinstance(run, dict) and all(
        type(run.get(item.name)) is int and run[item.name] >= 0 for item in fields(RunCompletion)
    )


def drift(fixture: dict[str, Any], live: dict[str, Any]) -> int:
    """Report fixture passes that fail live; inputs have passed the completion guard."""
    changed = sorted(
        test
        for test, result in fixture["tests"].items()
        if result["outcome"] == "passed" and live["tests"][test]["outcome"] in FAILED
    )
    sys.stdout.write(f"{len(changed)} tests pass on the fixtures and fail live.\n")
    for test in changed:
        sys.stdout.write(f"- `{test}`\n")
    return int(bool(changed))


def _live_report(fixture: dict[str, Any], path: Path) -> dict[str, Any]:
    live = load_report(path, "live")
    if live["suite"] != fixture["suite"]:
        raise SystemExit("the fixture and live reports come from different suites")
    if live["tests"].keys() != fixture["tests"].keys():
        raise SystemExit("the fixture and live reports select different tests; re-run them")
    return live


def main(arguments: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render the per-tool acceptance report.")
    parser.add_argument("fixture", type=Path, help="the report of the fixture-mode run")
    parser.add_argument("--live", type=Path, help="the report of the live run")
    parser.add_argument("--limitations", type=Path, help="YAML: test id -> upstream requirement")
    parser.add_argument("--check-complete", choices=("fixture", "live"), help="validate only")
    parser.add_argument("--drift", action="store_true", help="fail on upstream drift")
    options = parser.parse_args(arguments)
    fixture = load_report(options.fixture, options.check_complete or "fixture")
    if options.check_complete:
        return 0
    if options.drift and not options.live:
        parser.error("--drift requires --live")
    return _render_options(options, fixture)


def _render_options(options: argparse.Namespace, fixture: dict[str, Any]) -> int:
    environment = None
    if options.live:
        live = _live_report(fixture, options.live)
        if "environment" in live:
            environment = _report_environment(live["environment"], "live")
        if options.drift:
            return drift(fixture, live)
        limitations = {}
        if options.limitations:
            limitations = yaml.safe_load(options.limitations.read_text(encoding="utf-8")) or {}
        combined = combine(fixture, live, limitations)
        modes = f"fixture ({fixture['transport']}) and live ({live['transport']})"
    else:
        combined = {name: (row["outcome"], []) for name, row in fixture["tools"].items()}
        modes = (
            f"fixture ({fixture['transport']}) only; "
            "the live run is not included, so no outcome here is final"
        )
    sys.stdout.write(render(fixture, combined, modes, live_environment=environment))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
