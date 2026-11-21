"""The project's requirements (`spec/requirements.yaml`) and the tests that cite them.

A suite test cites the requirements it enforces, `@pytest.mark.requirement("X-2")`. Every
requirement is cited by a test or planned in an issue, and every test cites requirements
the file holds; `selftests/test_requirements.py` checks both over the collected suite.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

import yaml

from nci_sip_acceptance.spec import SPEC, TOOLS, is_basis

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable
    from pathlib import Path

    import pytest

REQUIREMENTS = SPEC / "requirements.yaml"
MARKER = "requirement"
FIELDS = frozenset({"statement", "basis", "planned"})
ISSUE = re.compile(r"#\d+")


def load_requirements(path: Path = REQUIREMENTS) -> dict[str, dict[str, Any]]:
    """The requirements by id, refused unless each states its behaviour and its basis."""

    requirements = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if problems := [
        f"{key} {problem}"
        for key, entry in requirements.items()
        if (problem := _key_problem(key) or _problem(entry))
    ]:
        raise ValueError(f"{path.name}: " + "; ".join(problems))
    return requirements


# What an entry must hold, in order, and what it is told when it does not.
RULES: tuple[tuple[Callable[[Any], bool], str], ...] = (
    (
        lambda entry: isinstance(entry, dict) and not set(entry) - FIELDS,
        f"has only {', '.join(sorted(FIELDS))}",
    ),
    (
        lambda entry: isinstance(entry.get("statement"), str) and bool(entry["statement"].strip()),
        "states the behaviour",
    ),
    (
        lambda entry: (
            isinstance(entry.get("basis"), list)
            and bool(entry["basis"])
            and all(isinstance(b, str) for b in entry["basis"])
        ),
        "names its basis, a list of conventions, records or tools",
    ),
    (
        lambda entry: all(is_basis(name) for name in entry["basis"]),
        "names its basis among the conventions and tools of spec/",
    ),
    (
        lambda entry: "planned" not in entry or bool(ISSUE.fullmatch(str(entry["planned"]))),
        "is planned in an issue, named #<number>",
    ),
)


def _key_problem(key: str) -> str | None:
    """An id is P-n (protocol gate), X-n (cross-cutting) or <tool>-n."""

    prefix, _, number = key.rpartition("-")
    if number.isdigit() and (prefix in ("P", "X") or prefix in TOOLS):
        return None
    return "is not P-<n>, X-<n> or <required tool>-<n>"


def _problem(entry: Any) -> str | None:
    return next((message for holds, message in RULES if not holds(entry)), None)


def repo_path(test_id: str) -> str:
    """A test id as documents outside acceptance/ name it. Ids are relative to acceptance/; from
    docs/ a bare tests/ path would read as the unit suite."""

    return f"acceptance/{test_id}"


def citations(items: Iterable[pytest.Item]) -> dict[str, tuple[str, ...]]:
    """The requirements each test cites, by test id."""

    return {
        item.nodeid: tuple(key for mark in item.iter_markers(MARKER) for key in mark.args)
        for item in items
    }


def never_runs(item: pytest.Item) -> bool:
    """Whether a test is skipped for good or expected to fail: it covers nothing."""

    skipped = item.get_closest_marker("skip") or item.get_closest_marker("xfail")
    return bool(skipped) or any(
        mark.args and mark.args[0] is True for mark in item.iter_markers("skipif")
    )


def problems(
    cited: dict[str, tuple[str, ...]], requirements: dict[str, Any], idle: Iterable[str] = ()
) -> list[str]:
    """Tests citing nothing or an unknown id, and requirements neither planned nor cited by a
    test that runs (`idle` names those that never run)."""

    running = {test: keys for test, keys in cited.items() if test not in set(idle)}
    return _citing_problems(cited, requirements) + _unanswered(running, requirements)


def _citing_problems(cited: dict[str, tuple[str, ...]], requirements: dict[str, Any]) -> list[str]:
    found = [f"{test} cites no requirement" for test, keys in cited.items() if not keys]
    return found + [
        f"{test} cites {key}, which spec/requirements.yaml does not hold"
        for test, keys in cited.items()
        for key in keys
        if key not in requirements
    ]


def _unanswered(cited: dict[str, tuple[str, ...]], requirements: dict[str, Any]) -> list[str]:
    citing = {key for keys in cited.values() for key in keys}
    return [
        f"{key} is neither cited by a test that runs nor planned"
        for key, entry in requirements.items()
        if key not in citing and "planned" not in entry
    ]
