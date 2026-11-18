"""A remote server under test: what the harness checks before the first test, and how it gets
the state a test needs from the operator's state-change hook.

The harness starts no process of a remote server and cannot set its environment, so the operator
sets the fixture server's URLs on it (`announcement`), `probe` makes sure it reaches them, and
`StateHook` runs the operator's `NCI_SIP_ACCEPTANCE_STATE_HOOK` command wherever a test needs a
server in another state (the acceptance README, "Remote server").
"""

from __future__ import annotations

import os
import subprocess
from typing import TYPE_CHECKING, Any

import pytest

from nci_sip_acceptance.client import (
    AUTHORIZATION_VARIABLE,
    open_remote_session,
    remote_settings,
    wait_for_endpoint,
    withhold_authorization,
    without_nci_sip_settings,
)
from nci_sip_acceptance.suite import UnmatchedUpstream, unmatched_requests
from nci_sip_acceptance.tools import Tools

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from nci_sip_acceptance.client import Target
    from nci_sip_acceptance.fixture_server import FixtureServer

NOT_ANSWERING = "the server under test does not answer"
NOT_REACHING = "the server under test does not reach the fixture server"
RESOLVE_RELEASE = "resolve_release"
# The scenario set a state change is for, as the hook finds it: names separated by commas.
SCENARIOS_SETTING = "NCI_SIP_ACCEPTANCE_SCENARIOS"
# A server listening here is reachable by many names, none of which the harness can know.
WILDCARD = "0.0.0.0"  # noqa: S104


def fixture_url(target: Target, upstream: FixtureServer) -> str:
    """The base URL the server under test reaches the fixture server by."""

    return target.fixture_url or upstream.url


def announcement(target: Target, upstream: FixtureServer) -> list[str]:
    """The settings the operator sets on the server under test, one line each, and a warning
    where the URLs named may not reach the fixture server from where the server runs."""

    lines = [
        f"{name}={value}" for name, value in remote_settings(fixture_url(target, upstream)).items()
    ]
    told = ["settings of the server under test:", *(f"  {line}" for line in lines)]
    if target.fixture_bind and target.fixture_bind[0] == WILDCARD and not target.fixture_url:
        told.append(
            "warning: the fixture server listens on every address but the URLs name "
            f"{upstream.url}; if the server under test runs elsewhere, set "
            "NCI_SIP_ACCEPTANCE_FIXTURE_URL"
        )
    return told


def probe(
    url: str,
    authorization: str | None,
    upstream: FixtureServer | None,
    pinned: dict[str, str],
    startup: Sequence[dict[str, Any]] = (),
) -> None:
    """Fail dependent tests unless the server answers and reaches the fixtures in fixture mode.

    The probe is a resolve_release call, which asks the platform for the current release; a
    server of a profile without that tool is only asked for its tools. The requests the server
    made while the state hook started it (`startup`) count as proof that it reaches the fixture
    server, with those of the call; a server that has asked nothing by then, having answered
    from a cache filled before the run, fails the probe as one that cannot. Startup requests
    without a fixture are reported as they are for any server the harness starts.
    """

    refused: list[int] = []
    try:
        asked = _ask(url, authorization, upstream, pinned, refused)
    except Exception as error:  # noqa: BLE001 - the type is shown, the message may hold secrets
        why = f"HTTP {refused[0]}" if refused else type(error).__name__
        pytest.fail(f"{NOT_ANSWERING} ({why})", pytrace=False)
    if upstream:
        _require_fixture_requests(upstream, asked, startup)
        upstream.reset()


def _require_fixture_requests(
    upstream: FixtureServer, asked: bool, startup: Sequence[dict[str, Any]]
) -> None:
    if unmatched := unmatched_requests(startup):
        raise UnmatchedUpstream(unmatched, " while the server started")
    if asked and not (startup or upstream.log()):
        pytest.fail(NOT_REACHING, pytrace=False)


def _ask(
    url: str,
    authorization: str | None,
    upstream: FixtureServer | None,
    pinned: dict[str, str],
    refused: list[int],
) -> bool:
    """Call resolve_release on the server, where it has the tool; whether it was called."""

    with open_remote_session(url, authorization, statuses=refused) as session:
        tools = Tools(session)
        if upstream:
            upstream.reset()
        if not tools.implemented(RESOLVE_RELEASE):
            return False
        tools.call(RESOLVE_RELEASE, {"terminology": pinned["terminology"]})
        return True


class StateHook:
    """Puts the server in the state a test needs by running the operator's state-change hook.

    The hook's contract: apply these settings and forget every upstream answer cached so far
    (a restart does both; a server that can flush its cache and reread its settings may do that
    instead). It runs with the harness's environment less the credential and the server's
    `NCI_SIP_*` settings, plus the fixture settings, the names of the scenario set
    (`NCI_SIP_ACCEPTANCE_SCENARIOS`, empty for none) and the set's settings, and the harness then
    waits for the endpoint to answer. A scenario set's tests share one state change, as they share
    their settings; a test that must see the server ask upstream (`own_server`) gets one of its
    own; and the hook is called again without settings when the run ends, to leave the server as
    the operator started it.
    """

    def __init__(self, target: Target, upstream: FixtureServer, log: Path) -> None:
        if target.url is None or target.state_hook is None:
            raise ValueError("a state change needs a remote server and the operator's hook")
        self._url, self._command = target.url, target.state_hook
        self._authorization, self._seconds = target.authorization, target.state_hook_timeout
        self._upstream = upstream
        self._log = log
        self._fixture_settings = remote_settings(fixture_url(target, upstream))
        # The scenarios the server runs with now; none, as the operator started it.
        self._scenarios: tuple[str, ...] = ()
        self._startup: tuple[dict[str, Any], ...] = ()

    def apply(
        self, scenarios: tuple[str, ...], settings: dict[str, str], *, fresh: bool = False
    ) -> tuple[dict[str, Any], ...]:
        """The requests the server made while the state changed, after a call of the hook with
        `settings` unless the server already runs with `scenarios` (and `fresh` does not ask
        for another)."""

        if fresh or scenarios != self._scenarios:
            self._startup = self._run(scenarios, settings)
            self._scenarios = scenarios
        return self._startup

    def restore(self) -> None:
        """Leave the server as the operator started it."""

        if self._scenarios:
            self.apply((), {})

    def _environment(self, scenarios: tuple[str, ...], settings: dict[str, str]) -> dict[str, str]:
        # The harness's own settings are the hook's to read, but for the credential, which it
        # has no use for; the server's `NCI_SIP_*` settings come from the run, not the developer.
        harness = {
            name: value
            for name, value in os.environ.items()
            if name.startswith("NCI_SIP_ACCEPTANCE_") and name != AUTHORIZATION_VARIABLE
        }
        named = {SCENARIOS_SETTING: ",".join(scenarios)}
        return (
            without_nci_sip_settings(os.environ)
            | harness
            | self._fixture_settings
            | named
            | settings
        )

    def _run(
        self, scenarios: tuple[str, ...], settings: dict[str, str]
    ) -> tuple[dict[str, Any], ...]:
        seconds = self._seconds
        self._upstream.reset()
        # Output goes to a file, not a pipe, so that a server the command leaves running
        # cannot hold the harness waiting for the pipe to close.
        with self._log.open("w", encoding="utf-8") as out:
            try:
                ran = subprocess.run(  # noqa: S602 - the operator's own command line
                    self._command,
                    shell=True,
                    env=self._environment(scenarios, settings),
                    stdout=out,
                    stderr=subprocess.STDOUT,
                    check=False,
                    timeout=seconds,
                )
            except subprocess.TimeoutExpired:
                pytest.fail(f"the state hook did not return within {seconds:g} s", pytrace=False)
        if ran.returncode:
            said = withhold_authorization(self._log.read_text(encoding="utf-8", errors="replace"))
            pytest.fail(
                f"the state hook failed with exit status {ran.returncode}:\n{said[-2000:]}",
                pytrace=False,
            )
        if why := wait_for_endpoint(self._url, self._authorization, seconds):
            pytest.fail(f"{NOT_ANSWERING} ({why}) after the state change", pytrace=False)
        startup = tuple(self._upstream.log())
        self._upstream.reset()
        return startup
