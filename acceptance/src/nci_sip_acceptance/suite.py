"""The rules of a suite run that do not depend on pytest: what runs in which mode, and
which upstream requests a test may leave without a fixture."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
import yaml

from nci_sip_acceptance.fixture_server import MANIFEST

if TYPE_CHECKING:
    from collections.abc import Iterable

FIXTURES = Path(__file__).parents[2] / "fixtures"
LIVE_CAPABLE = "live_capable"
SCENARIO = "scenario"
# A test marked so runs against a server process of its own, as a scenario test does.
OWN_SERVER = "own_server"
FIXTURE_ONLY = "fixture mode only"
# The property a skip site sets to say why a test did not run: not_live or not_implemented.
# The report reads it, never the reason text.
SKIP = "acceptance_skip"
NOT_LIVE_REASON = "not_live_reason"
# A test marked so expects requests without a fixture, and checks them itself.
UNMATCHED_UPSTREAM = "unmatched_upstream"
# A test marked so needs what the operator's prepare command produced (the interim index).
PREPARED = "prepared"
# A test marked so runs on a server of its own started without it, as a server finds itself
# before its operator has prepared anything.
UNPREPARED = "unprepared"
NOT_PREPARED = "NOT RUN: no prepare command (NCI_SIP_ACCEPTANCE_PREPARE)"
NOT_DECLARED_PREPARED = "NOT RUN: the server is not declared prepared (NCI_SIP_ACCEPTANCE_PREPARED)"
# What a remote server cannot give a test: a state of its own without an operator's state-change
# hook, and one without the index at all.
NEEDS_STATE_HOOK = "needs a server of its own (NCI_SIP_ACCEPTANCE_STATE_HOOK)"
CANNOT_UNPREPARE = "needs a server without the index, which a remote server cannot be made"
REGISTRY_RELEASE = "registry_release"
REGISTRY_SUSPENDED = (
    "registry-release requirement suspended pending sponsor decision "
    "(NCI_SIP_ACCEPTANCE_REGISTRY_RELEASE=optional)"
)


class UnmatchedUpstream(pytest.fail.Exception):
    """A test fails because upstream requests found no fixture; `requests` names them.

    The report counts such a failure as NO FIXTURE, for each test it reaches: a server
    whose startup requests lacked fixtures fails every test that uses it.
    """

    def __init__(self, requests: list[str], when: str = "") -> None:
        super().__init__(f"upstream requests without a fixture{when}:\n" + "\n".join(requests))
        self.requests = requests


def scenarios_of(item: pytest.Item) -> tuple[str, ...]:
    """The scenarios a test selects, over all its `scenario` markers."""

    return tuple(name for marker in item.iter_markers(SCENARIO) for name in marker.args)


def select_registry_checks(items: list[pytest.Item], required: bool) -> list[pytest.Item]:
    """Suspend only marked registry requirements; keep them out of later live selection."""
    if required:
        return items
    active = []
    for item in items:
        if item.get_closest_marker(REGISTRY_RELEASE) is not None:
            item.add_marker(pytest.mark.skip(reason=REGISTRY_SUSPENDED))
        else:
            active.append(item)
    return active


def skip_fixture_only(items: Iterable[pytest.Item]) -> None:
    """What a live run does: skip every test not marked live-capable, and every scenario."""

    skip = pytest.mark.skip(reason=FIXTURE_ONLY)
    for item in items:
        scenario = bool(scenarios_of(item))
        if scenario or item.get_closest_marker(LIVE_CAPABLE) is None:
            item.add_marker(skip)
            item.user_properties.append((SKIP, "not_live"))
            item.user_properties.append(
                (NOT_LIVE_REASON, "scenario" if scenario else "not_live_capable")
            )


def skip_unprepared(items: Iterable[pytest.Item], reason: str = NOT_PREPARED) -> None:
    """What a run without a prepare command (or a remote server not declared prepared) does:
    skip every test that needs its result."""

    skip = pytest.mark.skip(reason=reason)
    for item in items:
        if item.get_closest_marker(PREPARED) is not None:
            item.add_marker(skip)


def _needs_own_server(item: pytest.Item) -> bool:
    return bool(scenarios_of(item)) or any(
        item.get_closest_marker(mark) for mark in (OWN_SERVER, "mcp_session")
    )


def skip_remote_own_servers(items: Iterable[pytest.Item], has_hook: bool) -> None:
    """What a fixture-mode run against a remote server does: skip the tests that need a server
    without the index, which cannot be made of it, and, without the operator's state-change
    hook, those that need a server of their own."""

    for item in items:
        if item.get_closest_marker(UNPREPARED) is not None:
            item.add_marker(pytest.mark.skip(reason=CANNOT_UNPREPARE))
        elif not has_hook and _needs_own_server(item):
            item.add_marker(pytest.mark.skip(reason=NEEDS_STATE_HOOK))


def order_for_state_changes(items: list[pytest.Item]) -> None:
    """Order a run so that the operator's state-change hook runs as seldom as it can: the tests
    on the server as the operator left it first, then those that need a server of their own,
    then each scenario's together. The order within each group is kept."""

    def rank(item: pytest.Item) -> tuple[int, tuple[str, ...]]:
        if scenarios := scenarios_of(item):
            return 2, scenarios
        return int(_needs_own_server(item)), ()

    items.sort(key=rank)


def index_set(manifest: dict[str, Any]) -> list[str]:
    """The concepts the prepare command indexes: each the fixture set records at an include
    that holds its summary, so that an indexer's request for the summary sections finds it."""

    lists = manifest["record"]["concepts"].items()
    return [code for include, codes in lists if _holds_summary(include) for code in codes]


def _holds_summary(include: str) -> bool:
    return bool({"summary", "full"} & set(include.split(",")))


def unmatched_requests(log: Iterable[dict[str, Any]]) -> list[str]:
    """The upstream requests in `log` that no fixture answered, one line each.

    The server under test may treat the fixture server's refusal as an outage and
    answer with a plausible degraded result, so a test that passed while one of its
    requests found no fixture proves nothing.
    """

    return [
        f"{entry['method']} {entry['surface']} {entry['path']} {entry['params']}"
        for entry in log
        if entry["fixture"] is None
    ]


def main() -> None:
    """Print the index set, one code per line: what an operator prepares a remote server with."""

    manifest = yaml.safe_load((FIXTURES / MANIFEST).read_text(encoding="utf-8"))
    sys.stdout.write("".join(f"{code}\n" for code in index_set(manifest)))


if __name__ == "__main__":
    main()
