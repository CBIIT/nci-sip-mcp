"""The functional-status page's fixture and stored live tables, generated offline.

    pdm run acceptance-status [--check]

The table shows each tool group's tools by their fixture-mode outcome, which the per-tool report
derives from the outcomes of the tests (report.py); the tests' tools and gates are read from
the suite's markers, so the front page reports what the suite measures and not what someone last
wrote. It is written between two markers of the destination page. `--check` writes nothing and
exits with 1 when that page is not current, and a self-test fails then as well.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import pytest
import yaml

from nci_si_acceptance.expected import EXPECTED
from nci_si_acceptance.report import (
    CANNOT_RUN,
    FAIL,
    FAILED,
    INCOMPLETE,
    NO_FIXTURE,
    NO_TESTS,
    NOT_IMPLEMENTED,
    NOT_RUN,
    PASS,
    NativeReport,
    completion_problem,
    tool_outcome,
)
from nci_si_acceptance.requirements import load_requirements
from nci_si_acceptance.spec import REQUIRED_TOOLS, TOOLS

ACCEPTANCE = EXPECTED.parents[1]
STATUS = ACCEPTANCE.parent / "docs/functional-status.md"
LIVE = ACCEPTANCE.parent / "docs/evidence/acceptance-live"
CATALOGUE = ACCEPTANCE.parent / "docs/upstream/catalogue.yaml"
# Stored pre-rename live evidence retains its historical run/commit URL identity.
# This base makes no claim about the provenance of a future live run.
GITHUB = "https://github.com/CBIIT/nci-si-mcp"
BEGIN, END = "<!-- acceptance-status:begin -->", "<!-- acceptance-status:end -->"
GROUPS = {
    "evs": "EVS tools",
    "cadsr": "caDSR tools",
    "cross-domain": "Cross-domain tools",
    "workflow": "Workflow tools",
}
# The order a group's tools are listed in: best outcome first.
ORDER = (PASS, INCOMPLETE, NO_FIXTURE, FAIL, NOT_RUN, NOT_IMPLEMENTED, NO_TESTS)

Attribution = dict[str, tuple[str | None, bool]]


class _Marks:
    """A pytest plugin that notes the tool and gate markers of every test it collects."""

    def __init__(self) -> None:
        self.found: Attribution = {}

    def pytest_collection_modifyitems(self, items: list[pytest.Item]) -> None:
        for item in items:
            marker = item.get_closest_marker("tool")
            gate = item.get_closest_marker("gate") is not None
            self.found[item.nodeid] = (marker.args[0] if marker else None, gate)


def collect_attribution() -> Attribution:
    """The tool and gate of every test of the suite, by collecting it: no test runs."""

    marks = _Marks()
    arguments = [
        *("--collect-only", "-q", "-p", "no:cacheprovider"),
        *("-c", str(ACCEPTANCE / "pyproject.toml"), "--rootdir", str(ACCEPTANCE)),
        str(ACCEPTANCE / "tests"),
    ]
    with contextlib.redirect_stdout(io.StringIO()):
        status = pytest.main(arguments, plugins=[marks])
    if status:
        raise SystemExit(f"collecting the suite failed with exit status {status}")
    return marks.found


def tool_outcomes(expected: dict[str, str], attribution: Attribution) -> dict[str, str]:
    """The outcome of each required tool, by the rule of the per-tool report. No server has
    started, so a tool is taken as not implemented when every test of it was skipped for want
    of it."""

    failed_gates, unrun_gates = _gates(expected, attribution)
    outcomes = {}
    for name in REQUIRED_TOOLS:
        counts = Counter(
            expected[nodeid] for nodeid, (tool, _) in attribution.items() if tool == name
        )
        implemented = not (counts and set(counts) == {"not_implemented"})
        outcomes[name] = tool_outcome(counts, failed_gates, unrun_gates, implemented)
    return outcomes


def _gates(expected: dict[str, str], attribution: Attribution) -> tuple[bool, bool]:
    """Whether a gate failed, and whether one could not run, in the expected outcomes."""

    gates = [expected.get(nodeid) for nodeid, (_, gate) in attribution.items() if gate]
    failed = any(outcome in FAILED for outcome in gates)
    return failed, any(outcome in CANNOT_RUN for outcome in gates)


def _cell(outcome: str, names: list[str]) -> str:
    return f"{outcome}: " + ", ".join(f"`{name}`" for name in names)


def _listed(names: list[str], outcomes: dict[str, str]) -> dict[str, list[str]]:
    return {outcome: [n for n in names if outcomes[n] == outcome] for outcome in ORDER}


def _row(group: str, title: str, outcomes: dict[str, str]) -> str:
    names = [name for name, tool_group in REQUIRED_TOOLS.items() if tool_group == group]
    listed = _listed(names, outcomes)
    cells = [_cell(outcome, found) for outcome, found in listed.items() if found]
    return f"| {title} | {len(names)} | {'<br>'.join(cells)} |"


def render(expected: dict[str, str], attribution: Attribution) -> str:
    """For each group, its tools listed under their fixture outcome."""

    outcomes = tool_outcomes(expected, attribution)
    lines = [
        BEGIN,
        f"Fixture-mode outcome of each tool, from the expected outcomes of the {len(expected)} "
        "tests of the acceptance suite "
        "([acceptance/expected/fixture.json](acceptance/expected/fixture.json)); "
        "[acceptance/README.md](acceptance/README.md#the-report) defines the outcomes. "
        "This table is generated by `pdm run acceptance-status`.",
        "",
        "| Group | Tools | Outcome of each tool |",
        "|---|---|---|",
    ]
    lines += [_row(group, title, outcomes) for group, title in GROUPS.items()]
    return "\n".join([*lines, END])


def load_live(directory: Path = LIVE) -> tuple[dict[str, Any], dict[str, Any]]:
    """Read original native evidence and its reviewed scheduled-main provenance, offline."""
    raw = (directory / "latest.json").read_bytes()
    provenance = yaml.safe_load((directory / "latest.yaml").read_text(encoding="utf-8"))
    if not _valid_provenance(provenance, raw):
        raise ValueError("Invalid live evidence provenance")
    report = NativeReport.from_dict(json.loads(raw)).to_dict()
    if report["mode"] != "live":
        raise ValueError("Live evidence must contain a live report")
    return report, provenance


def _valid_provenance(value: Any, raw: bytes) -> bool:
    if not isinstance(value, dict) or set(value) != {
        "sha256",
        "run",
        "artifact",
        "event",
        "branch",
        "sha",
        "date",
    }:
        return False
    origin = value["event"] == "schedule" and value["branch"] == "main"
    identifiers = all(type(value[key]) is int and value[key] > 0 for key in ("run", "artifact"))
    formats = {"sha": r"[0-9a-f]{40}", "date": r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z"}
    return all(
        (
            origin,
            identifiers,
            value["sha256"] == hashlib.sha256(raw).hexdigest(),
            all(_matches(value[key], pattern) for key, pattern in formats.items()),
        )
    )


def _matches(value: Any, pattern: str) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(pattern, value))


def _entry_tools(entry: dict[str, Any], requirements: dict[str, Any]) -> tuple[set[str], set[str]]:
    owners = {key.rpartition("-")[0] for key in entry["requirements"]} & TOOLS.keys()
    basis = {name for key in entry["requirements"] for name in requirements[key]["basis"]}
    return owners, basis & TOOLS.keys()


def skip_contexts() -> dict[str, str]:
    """Current blocker context, never a reconstruction of a historical skip mechanism.

    Access belongs only to the cited requirement's owner. Capability requests additionally
    apply to tools explicitly named in its specification basis.
    """
    requirements = load_requirements()
    entries = yaml.safe_load(CATALOGUE.read_text(encoding="utf-8"))["entries"]
    gaps: dict[str, list[str]] = {}
    credentials = {}
    for entry in entries:
        link = f"[{entry['id']}](docs/upstream/{entry['team']}.md#{entry['id']})"
        owners, basis = _entry_tools(entry, requirements)
        if entry["id"] == "cadsr-access":
            credentials.update(dict.fromkeys(owners, f"credentials not issued ({link})"))
        else:
            for name in owners | basis:
                gaps.setdefault(name, []).append(link)
    return {
        name: f"upstream capability gap ({', '.join(links)})" for name, links in gaps.items()
    } | credentials


def _live_counts(rows: list[dict[str, Any]]) -> tuple[int, int, int]:
    counts = Counter(row["outcome"] for row in rows)
    return (
        counts["passed"],
        sum(counts[kind] for kind in FAILED),
        sum(counts[kind] for kind in ("not_live", "not_implemented", "skipped")),
    )


def _skip_context(row: dict[str, Any], contexts: dict[str, str]) -> str:
    if row["outcome"] != "not_live":
        return "other skip (not not_live)"
    reason = row.get("not_live_reason")
    if reason is None:
        return "unrecorded"
    if reason == "scenario":
        return "crafted scenario (fixture-only)"
    return contexts.get(row["tool"], "not yet probed by this live acceptance run")


def _live_row(label: str, rows: list[dict[str, Any]], contexts: dict[str, str]) -> str:
    passed, failed, skipped = _live_counts(rows)
    reasons = Counter(
        _skip_context(row, contexts)
        for row in rows
        if row["outcome"] in ("not_live", "not_implemented", "skipped")
    )
    shown = "; ".join(f"{count} {reason}" for reason, count in reasons.items()) or "—"
    return f"| {label} | {passed} / {failed} / {skipped} | {shown} |"


def render_live(
    report: dict[str, Any], provenance: dict[str, Any], contexts: dict[str, str]
) -> str:
    """Count stored test rows once, including unattributed gates, preserving failed/partial runs."""
    problem = completion_problem(report) if "run" in report else "completion unrecorded"
    completion = f"Incomplete: {problem}" if problem else "Complete"
    sha, run = provenance["sha"], provenance["run"]
    lines = [
        f"Latest stored scheduled main live run: [{run}]({GITHUB}/actions/runs/{run}), "
        f"{provenance['date']}; commit [{sha[:12]}]({GITHUB}/commit/{sha}). **{completion}**.",
        f"Report SHA-256: `{provenance['sha256']}`; artifact {provenance['artifact']}.",
        "",
        "| Tool | Live passed / failed / skipped | Skip reason / context |",
        "|---|---|---|",
    ]
    rows = list(report["tests"].values())
    lines += _live_tool_rows(rows, contexts)
    passed, failed, skipped = _live_counts(rows)
    lines += [
        f"| **Total** | **{passed} / {failed} / {skipped}** | |",
        "",
        f"{passed} passed / {failed} failed / {skipped} skipped. "
        "Skipped includes `not_live`, `not_implemented` and ordinary skips; only `not_live` "
        "has the reason contexts above. Passed validation/refusal tests do not establish "
        "live content retrieval. Unrecorded reasons are never inferred from current test markers.",
    ]
    return "\n".join(lines)


def _live_tool_rows(rows: list[dict[str, Any]], contexts: dict[str, str]) -> list[str]:
    grouped: dict[str | None, list[dict[str, Any]]] = {name: [] for name in REQUIRED_TOOLS}
    grouped[None] = []
    for row in rows:
        grouped.setdefault(row["tool"], []).append(row)
    unattributed = grouped.pop(None)
    lines = [_live_row(f"`{name}`", tests, contexts) for name, tests in grouped.items()]
    return [*lines, _live_row("Unattributed gates", unattributed, contexts)]


def current(readme: str, table: str) -> str:
    """The destination page with only the text between its markers replaced."""

    before, found, rest = readme.partition(BEGIN)
    _, end, after = rest.partition(END)
    if not (found and end):
        raise SystemExit(f"the destination page lacks the markers {BEGIN} and {END}")
    return before + table + after


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write the functional-status page's tables.")
    parser.add_argument("--check", action="store_true", help="write nothing; fail if not current")
    parser.add_argument("--readme", type=Path, default=STATUS, help="destination Markdown page")
    parser.add_argument("--live-dir", type=Path, default=LIVE, help="stored native live evidence")
    options = parser.parse_args(arguments)
    expected: dict[str, Any] = json.loads(EXPECTED.read_text(encoding="utf-8"))
    readme = options.readme.read_text(encoding="utf-8")
    attribution = collect_attribution()
    if stale := sorted(attribution.keys() ^ expected.keys()):
        raise SystemExit(
            f"the expected outcomes and the suite differ in {len(stale)} tests, among them "
            f"{stale[0]}; run the suite and pdm run acceptance-expected update"
        )
    live, provenance = load_live(options.live_dir)
    fixture = render(expected, attribution).removesuffix(END)
    table = fixture + "\n" + render_live(live, provenance, skip_contexts()) + "\n" + END
    prefix = Path(os.path.relpath(ACCEPTANCE.parent, options.readme.parent)).as_posix()
    if prefix != ".":
        for directory in ("acceptance", "docs"):
            table = table.replace(f"]({directory}/", f"]({prefix}/{directory}/")
    written = current(readme, table)
    if written == readme:
        return 0
    if options.check:
        sys.stderr.write(f"{options.readme} is not current; run pdm run acceptance-status\n")
        return 1
    options.readme.write_text(written, encoding="utf-8")
    sys.stdout.write(f"Wrote the status table to {options.readme}.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
