"""The expected outcomes of the fixture-mode run: the ratchet CI holds the server to.

    pdm run acceptance-expected check acceptance/fixture.json
    pdm run acceptance-expected update acceptance/fixture.json

`acceptance/expected/fixture.json` maps the id of every test to its outcome in the report's own
vocabulary (report.py), and holds nothing else, so a reworded failure is not a change. `check`
compares a report with it and exits with 1 on any difference: a test with another outcome, a
test the report lacks, a test the expected outcomes lack. `update` rewrites the file from a
report, for a change that moves outcomes on purpose; the diff of the file is then what the
review reads. When a supplied base outcome map contains passes that the current expected map no
longer contains as passes, `check` also requires exact base-SHA-bound transition declarations.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, TypeIs, get_args

import yaml
from yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode

from nci_sip_acceptance.record import FIXTURES
from nci_sip_acceptance.report import NonPassingOutcome, is_outcome, load_report

if TYPE_CHECKING:
    from collections.abc import Iterable

EXPECTED = FIXTURES.parent / "expected" / "fixture.json"
ABSENT_FROM_REPORT = "(not in the report)"
ABSENT_FROM_EXPECTED = "(not in the expected outcomes)"
type AbsenceOutcome = Literal["absent"]
ABSENT: AbsenceOutcome = get_args(AbsenceOutcome.__value__)[0]
SHA = re.compile(r"[0-9a-f]{40}")
HINT = (
    "If the change is on purpose, run `pdm run acceptance-expected update "
    "acceptance/fixture.json` on the report of a fixture-mode run and commit "
    "`acceptance/expected/fixture.json`."
)

type ApprovalAfter = NonPassingOutcome | AbsenceOutcome


@dataclass(frozen=True, slots=True)
class Approval:
    """One declared base/test/transition record."""

    base_sha: str
    test: str
    before: Literal["passed"]
    after: ApprovalAfter
    reason: str
    reference: str


def outcomes_of(report: dict[str, Any]) -> dict[str, str]:
    """The outcome of every test of a fixture-mode report, by test id."""

    tests = report.get("tests")
    if not isinstance(tests, dict):
        raise ValueError("fixture report tests must be an object")
    outcomes = {}
    for nodeid, test in sorted(tests.items()):
        if not isinstance(test, dict):
            raise ValueError("fixture report tests must map test IDs to result objects")
        outcome = test.get("outcome")
        if not isinstance(outcome, str) or not is_outcome(outcome):
            raise ValueError("fixture report test has an unknown outcome")
        outcomes[nodeid] = outcome
    return outcomes


def read_outcomes(path: Path, label: str) -> dict[str, str]:
    """Read one outcome map and validate its closed values at the file boundary."""

    value: object = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return dict(_outcome(item, label) for item in value.items())


def _outcome(item: tuple[str, object], label: str) -> tuple[str, str]:
    test, outcome = item
    if not isinstance(outcome, str):
        raise ValueError(f"{label} must map test strings to outcome strings")
    if not is_outcome(outcome):
        raise ValueError(f"{label} has an unknown outcome: {outcome}")
    return test, outcome


def _string_fields(row: object, keys: set[str]) -> dict[str, str]:
    if not isinstance(row, dict):
        raise ValueError("each approval must be an object")
    if set(row) != keys:
        raise ValueError("each approval must have exactly the six documented fields")
    fields = {}
    for key in keys:
        value = row[key]
        if not isinstance(value, str):
            raise ValueError("every approval field must be a string")
        fields[key] = value
    return fields


def _approval(row: object) -> Approval:
    keys = {"base_sha", "test", "before", "after", "reason", "reference"}
    fields = _string_fields(row, keys)
    if SHA.fullmatch(fields["base_sha"]) is None:
        raise ValueError("base_sha must be 40 lowercase hexadecimal characters")
    before = fields["before"]
    if before != "passed":
        raise ValueError("approval transition has an unknown outcome")
    after = _approval_after(fields["after"])
    if not all(fields[key].strip() for key in ("test", "reason", "reference")):
        raise ValueError("test, reason and reference must be nonempty")
    return Approval(
        fields["base_sha"], fields["test"], before, after, fields["reason"], fields["reference"]
    )


def _approval_after(value: str) -> ApprovalAfter:
    if not _is_approval_after(value):
        raise ValueError("approval transition has an unknown outcome")
    return value


def _is_approval_after(value: str) -> TypeIs[ApprovalAfter]:
    return value == ABSENT or (is_outcome(value) and value != "passed")


def _approval_rows(value: object) -> list[object]:
    if not isinstance(value, dict):
        raise ValueError("root must be an object")
    if set(value) != {"version", "approvals"}:
        raise ValueError("root must contain exactly version and approvals")
    if type(value["version"]) is not int or value["version"] != 1:
        raise ValueError("version must be integer 1")
    rows = value["approvals"]
    if not isinstance(rows, list):
        raise ValueError("approvals must be a list")
    return rows


def read_approvals(path: Path) -> list[Approval]:
    """Read exact approval records, rejecting ambiguity for one base and test."""

    text = path.read_text(encoding="utf-8")
    _reject_duplicate_keys(yaml.compose(text, Loader=yaml.SafeLoader))
    value: object = yaml.safe_load(text)
    approvals = [_approval(row) for row in _approval_rows(value)]
    identities = [(row.base_sha, row.test) for row in approvals]
    if len(identities) != len(set(identities)):
        raise ValueError("a base and test may have only one approval")
    return approvals


def _reject_duplicate_keys(node: Node | None) -> None:
    _visit_yaml_node(node, set())


def _visit_yaml_node(node: Node | None, visited: set[int]) -> None:
    if node is None:
        return
    identity = id(node)
    if identity in visited:
        raise ValueError("YAML aliases are not allowed")
    visited.add(identity)
    if isinstance(node, MappingNode):
        _check_mapping(node, visited)
    elif isinstance(node, SequenceNode):
        for value in node.value:
            _visit_yaml_node(value, visited)


def _check_mapping(node: MappingNode, visited: set[int]) -> None:
    keys: set[str] = set()
    for key, value in node.value:
        _visit_yaml_node(key, visited)
        key_text = _mapping_key_text(key)
        if key_text in keys:
            raise ValueError(f"duplicate mapping key: {key_text}")
        keys.add(key_text)
        _visit_yaml_node(value, visited)


def _mapping_key_text(key: Node) -> str:
    if not isinstance(key, ScalarNode):
        raise ValueError("mapping keys must be scalars")
    if key.tag == "tag:yaml.org,2002:merge":
        raise ValueError("YAML merge keys are not allowed")
    return key.value


def regressions(base: dict[str, str], current: dict[str, str]) -> list[tuple[str, str]]:
    """Base passes whose current expected outcome is no longer passed."""

    return [
        (test, current.get(test, ABSENT))
        for test, outcome in sorted(base.items())
        if outcome == "passed" and current.get(test) != "passed"
    ]


def historical_check(
    base: dict[str, str], current: dict[str, str], approvals: list[Approval], base_sha: str
) -> tuple[int, str]:
    """Require exact declarations for regressions from one supplied base outcome map and SHA."""

    changed = regressions(base, current)
    required = _required_transitions(changed)
    supplied = _supplied_transitions(approvals, base_sha)
    missing = sorted(required - supplied)
    stale = sorted(supplied - required)
    lines = list(
        filter(
            None,
            (
                _difference_line(
                    missing, "regression", "regressions", "from passed needs approval"
                ),
                _difference_line(stale, "approval", "approvals", "stale for this base"),
            ),
        )
    )
    if lines:
        return 1, "\n".join(lines) + "\n"
    return 0, f"Approved {len(changed)} regressions from passed outcomes.\n"


def _required_transitions(changed: list[tuple[str, str]]) -> set[tuple[str, str, str]]:
    return {(test, "passed", after) for test, after in changed}


def _supplied_transitions(approvals: list[Approval], base_sha: str) -> set[tuple[str, str, str]]:
    return {(row.test, row.before, row.after) for row in approvals if row.base_sha == base_sha}


def _difference_line(
    rows: list[tuple[str, str, str]], singular: str, plural: str, description: str
) -> str | None:
    if not rows:
        return None
    noun = (plural, singular)[len(rows) == 1]
    names = ", ".join(row[0] for row in rows)
    if description.startswith("stale"):
        return f"Remove {len(rows)} stale {noun} for this base: {names}"
    return f"{len(rows)} {noun} {description}: {names}"


def differences(expected: dict[str, str], actual: dict[str, str]) -> list[tuple[str, str, str]]:
    """Each test whose outcome differs, with the one expected and the one found, in test order."""

    return [
        (nodeid, expected.get(nodeid, ABSENT_FROM_EXPECTED), actual.get(nodeid, ABSENT_FROM_REPORT))
        for nodeid in sorted(expected.keys() | actual.keys())
        if expected.get(nodeid) != actual.get(nodeid)
    ]


def _cell(text: str) -> str:
    return "`" + text.replace("|", "\\|") + "`"


def render(rows: Iterable[tuple[str, str, str]]) -> str:
    """The differences as Markdown: a table and the hint on how to accept them."""

    lines = ["| Test | Expected | Actual |", "|---|---|---|"]
    lines += [f"| {_cell(test)} | {expected} | {actual} |" for test, expected, actual in rows]
    return "\n".join(lines) + "\n"


def _check_with_current(expected_path: Path, report_path: Path) -> tuple[int, str, dict[str, str]]:
    expected = read_outcomes(expected_path, "expected outcomes")
    actual = outcomes_of(load_report(report_path, "fixture"))
    found = differences(expected, actual)
    if not found:
        return 0, f"All {len(actual)} tests have the outcome expected.\n", expected
    heading = f"{len(found)} of {len(actual | expected)} tests differ from the expected outcomes.\n"
    return 1, f"{heading}\n{render(found)}\n{HINT}\n", expected


def update(expected_path: Path, report_path: Path) -> int:
    """Rewrite the expected outcomes from a report; the number of tests written."""

    written = outcomes_of(load_report(report_path, "fixture"))
    expected_path.parent.mkdir(parents=True, exist_ok=True)
    expected_path.write_text(json.dumps(written, indent=2) + "\n", encoding="utf-8")
    return len(written)


def _historical_options(options: argparse.Namespace) -> tuple[Path, str, Path] | None:
    if options.base is None:
        if options.base_sha is not None or options.approvals is not None:
            raise SystemExit("--base, --base-sha and --approvals must be used together")
        return None
    if options.base_sha is None or options.approvals is None:
        raise SystemExit("--base, --base-sha and --approvals must be used together")
    return options.base, options.base_sha, options.approvals


def _valid_base(path: Path) -> dict[str, str]:
    try:
        return read_outcomes(path, "base expected outcomes")
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise SystemExit(f"Invalid base expected outcomes: {error}") from error


def _valid_approvals(path: Path) -> list[Approval]:
    try:
        return read_approvals(path)
    except (OSError, ValueError, yaml.YAMLError) as error:
        raise SystemExit(f"Invalid regression approvals: {error}") from error


def _valid_check(expected_path: Path, report_path: Path) -> tuple[int, str, dict[str, str]]:
    try:
        return _check_with_current(expected_path, report_path)
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise SystemExit(f"Invalid expected outcomes or fixture report: {error}") from error


def _run_check(options: argparse.Namespace, historical: tuple[Path, str, Path] | None) -> int:
    status, text, current = _valid_check(options.expected, options.report)
    if status or historical is None:
        sys.stdout.write(text)
        return status
    base_path, base_sha, approvals_path = historical
    if SHA.fullmatch(base_sha) is None:
        raise SystemExit("--base-sha must be 40 lowercase hexadecimal characters")
    status, historical_text = historical_check(
        _valid_base(base_path), current, _valid_approvals(approvals_path), base_sha
    )
    sys.stdout.write(f"All {len(current)} tests have the outcome expected.\n{historical_text}")
    return status


def _run_update(options: argparse.Namespace, historical: tuple[Path, str, Path] | None) -> int:
    if historical is not None:
        raise SystemExit("historical arguments apply only to check")
    try:
        count = update(options.expected, options.report)
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise SystemExit(f"Invalid expected outcomes or fixture report: {error}") from error
    sys.stdout.write(f"Wrote the outcomes of {count} tests to {options.expected}.\n")
    return 0


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compare a report with the expected outcomes.")
    parser.add_argument("command", choices=["check", "update"])
    parser.add_argument("report", type=Path, help="the report of a fixture-mode run")
    parser.add_argument("--expected", type=Path, default=EXPECTED, help="the expected outcomes")
    parser.add_argument("--base", type=Path, help="the supplied base expected outcomes")
    parser.add_argument("--base-sha", help="the commit identifying the supplied base")
    parser.add_argument("--approvals", type=Path, help="the declared regression approvals")
    options = parser.parse_args(arguments)
    historical = _historical_options(options)
    if options.command == "update":
        return _run_update(options, historical)
    return _run_check(options, historical)


if __name__ == "__main__":
    raise SystemExit(main())
