"""A remote server under test: the harness connects to a streamable-HTTP endpoint, announces what
the operator sets on the server, probes it before the first test, and gets the state a test needs
from the operator's state-change hook.

The server is the compliant server over streamable HTTP (`COMPLIANT_SERVER_TRANSPORT=http`),
started once for the tests of this module (a hook that restarts it, restart_stub.py, starts it and
restarts it with a scenario's settings).
"""

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from contextlib import nullcontext
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from restart_stub import stop

from conftest import LICENCE_KEY, STAND_IN
from nci_si_acceptance import client, environments
from nci_si_acceptance import remote as remote_module
from nci_si_acceptance.client import Target, open_remote_session, wait_for_endpoint
from nci_si_acceptance.fixture_server import FixtureServer, FixtureSet
from nci_si_acceptance.remote import StateHook, announcement, probe
from nci_si_acceptance.tools import Process

pytest_plugins = ["pytester"]

RESTART_STUB = Path(__file__).parent / "restart_stub.py"
CREDENTIAL = "Bearer selftest-credential"
PROTOCOL = "tests/test_protocol.py::"
TOOLS_LIST = f"{PROTOCOL}test_tools_list_names_the_tools_of_the_profile_and_no_other"
OWN_SERVER = f"{PROTOCOL}test_a_correlation_identifier_goes_upstream_and_comes_back"
PREPARED = f"{PROTOCOL}test_the_index_manifest_states_what_the_index_holds[index_manifest]"
UNKNOWN_RELEASE = (
    "tests/test_crosscutting.py::test_a_release_the_platform_does_not_serve_fails_closed"
    "[get_concept]"
)
LICENCE_REACHES = (
    "tests/test_crosscutting.py::"
    "test_the_licence_key_reaches_the_platform_and_nothing_the_server_returns_or_logs"
)
# A test that fails with the credential in its message, as a server that echoed it back would.
USAGE_ERROR = 4
# Far less than the minute a wait that ignored the timeout it was given would take.
PROMPTLY_SECONDS = 10
ECHO = """
import os
import pytest

pytestmark = pytest.mark.gate

def test_echo(tools):
    assert "echoed" == os.environ["NCI_SI_ACCEPTANCE_AUTHORIZATION"]


def test_echo_token(tools):
    assert "echoed" == os.environ["NCI_SI_ACCEPTANCE_AUTHORIZATION"].partition(" ")[2]
"""


def free_port() -> int:
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        return taken.getsockname()[1]


@dataclass(frozen=True)
class Stub:
    """The compliant server over HTTP, with what it and its restart command are given."""

    url: str
    fixture_port: int
    environment: dict[str, str]
    credentials: Path
    record: Path


@pytest.fixture(scope="module")
def stub(tmp_path_factory):
    """The compliant server, running over HTTP on a port of its own, which reaches a fixture
    server at `fixture_port`, answers 401 to a request without the credential and keeps the
    credential each request brought."""

    directory = tmp_path_factory.mktemp("stub")
    port, fixture_port = free_port(), free_port()
    environment = {
        "NCI_SI_EVS_BASE_URL": f"http://127.0.0.1:{fixture_port}/evs",
        "COMPLIANT_SERVER_PORT": str(port),
        "COMPLIANT_SERVER_AUTHORIZATION": CREDENTIAL,
        "COMPLIANT_SERVER_AUTH_LOG": str(directory / "credentials.log"),
        "COMPLIANT_SERVER_PROFILE": "evs",
        "COMPLIANT_SERVER_NO_CACHE": "1",
        "RESTART_PID_FILE": str(directory / "server.pid"),
        "RESTART_RECORD": str(directory / "restarts.log"),
    }
    started = Stub(
        f"http://127.0.0.1:{port}/mcp",
        fixture_port,
        environment,
        directory / "credentials.log",
        directory / "restarts.log",
    )
    subprocess.run(  # noqa: S603
        [sys.executable, str(RESTART_STUB)], env=os.environ | environment, check=True
    )
    assert wait_for_endpoint(started.url, CREDENTIAL, 60) is None
    yield started
    stop(Path(environment["RESTART_PID_FILE"]))


@pytest.fixture
def remote(compliant, monkeypatch, stub):
    """The suite's tests, run against the remote server: `run(*arguments)` gives the result of
    the run and its report."""

    monkeypatch.delenv("NCI_SI_ACCEPTANCE_SERVER")
    monkeypatch.delenv("NCI_SI_ACCEPTANCE_PREPARE")
    monkeypatch.setenv("NCI_SI_ACCEPTANCE_URL", stub.url)
    # Unbuffered output reaches the captured file descriptor at once; a harness line written
    # while the test's output is captured is then lost, so the run proves it is shown anyway.
    monkeypatch.setenv("PYTHONUNBUFFERED", "1")
    monkeypatch.setenv("NCI_SI_ACCEPTANCE_AUTHORIZATION", CREDENTIAL)
    monkeypatch.setenv("NCI_SI_ACCEPTANCE_FIXTURE_BIND", f"127.0.0.1:{stub.fixture_port}")
    # What the operator's restart command is given by the environment it runs in.
    for name, value in stub.environment.items():
        monkeypatch.setenv(name, value)
    (compliant.path / "tests" / "test_echo.py").write_text(ECHO, encoding="utf-8")

    def run(*arguments):
        report = compliant.path / "report.json"
        result = compliant.runpytest_subprocess(
            *arguments, "-p", "no:cacheprovider", "-p", STAND_IN, f"--report={report}", "-rs"
        )
        found = json.loads(report.read_text(encoding="utf-8")) if report.exists() else None
        return result, found

    return run


def outcomes_of(report):
    return {nodeid.partition("::")[2]: test["outcome"] for nodeid, test in report["tests"].items()}


def test_a_remote_server_is_tested_over_streamable_http_with_the_credential_on_every_request(
    remote, stub
):
    _, report = remote(TOOLS_LIST, "tests/test_echo.py")

    assert report["transport"] == "streamable-http"
    assert outcomes_of(report)[TOOLS_LIST.partition("::")[2]] == "passed"
    sent = stub.credentials.read_text(encoding="utf-8").splitlines()
    assert sent
    assert set(sent) == {CREDENTIAL}


def test_stateful_remote_calls_establish_and_reuse_an_http_session(stub, monkeypatch):
    exchanges = []
    note_refusal = client._note_refusal

    async def record(statuses, response):
        exchanges.append(
            (response.request.headers.get("Mcp-Session-Id"), response.headers.get("Mcp-Session-Id"))
        )
        await note_refusal(statuses, response)

    monkeypatch.setattr(client, "_note_refusal", record)
    with open_remote_session(stub.url, CREDENTIAL, timeout=30, stateful=True) as session:
        arguments = {"terminology": "ncit", "code": "invalid", "release": "26.09d"}
        first = session.call_tool("get_concept", arguments)
        second = session.call_tool("get_concept", arguments)
    replies = (first, second)
    assert all(reply.is_error for reply in replies)
    assert first.structured_content["error"]["code"] == "invalid_request"
    issued = {received for _, received in exchanges if received}
    assert len(issued) == 1
    assert sum(sent in issued for sent, _ in exchanges) >= len(replies)


def test_the_credential_appears_in_no_output_of_the_harness(remote):
    result, report = remote("tests/test_echo.py")

    assert outcomes_of(report) == {"test_echo": "failed", "test_echo_token": "failed"}
    written = result.stdout.str() + result.stderr.str() + json.dumps(report)
    assert CREDENTIAL.partition(" ")[2] not in written  # neither the value nor its token
    assert "[authorization withheld]" in result.stdout.str()


def test_the_harness_tells_the_operator_what_to_set_on_the_server(remote, stub):
    result, _ = remote(TOOLS_LIST)

    assert result.ret == 0
    result.stdout.fnmatch_lines(
        [
            "settings of the server under test:",
            "*NCI_SI_UPSTREAM_MODE=fixture",
            f"*NCI_SI_EVS_BASE_URL=http://127.0.0.1:{stub.fixture_port}/evs",
        ]
    )


def test_without_a_restart_command_the_tests_that_need_a_server_of_their_own_are_not_run(remote):
    result, report = remote(TOOLS_LIST, UNKNOWN_RELEASE, OWN_SERVER)

    result.stdout.fnmatch_lines(
        ["SKIPPED * needs a server of its own (NCI_SI_ACCEPTANCE_STATE_HOOK)"]
    )
    assert outcomes_of(report) == {
        TOOLS_LIST.partition("::")[2]: "passed",
        UNKNOWN_RELEASE.partition("::")[2]: "skipped",
        OWN_SERVER.partition("::")[2]: "skipped",
    }
    # A tool whose only tests did not run is not accepted: its row reads NOT RUN.
    assert report["tools"]["get_concept"]["outcome"] == "NOT RUN"
    assert report["tools"]["get_concept"]["counts"] == {"skipped": 1}


def test_a_test_that_needs_the_index_is_not_run_until_the_operator_declares_it_prepared(remote):
    result, report = remote(PREPARED)

    result.stdout.fnmatch_lines(
        ["SKIPPED * NOT RUN: the server is not declared prepared (NCI_SI_ACCEPTANCE_PREPARED)"]
    )
    assert outcomes_of(report) == {PREPARED.partition("::")[2]: "skipped"}


def test_the_state_hook_gives_scenario_and_own_server_tests_a_server_of_their_own(
    remote, stub, monkeypatch
):
    monkeypatch.setenv("NCI_SI_ACCEPTANCE_STATE_HOOK", f"{sys.executable} {RESTART_STUB}")
    monkeypatch.setenv("NCI_SI_ACCEPTANCE_PREPARED", "1")
    chosen = (UNKNOWN_RELEASE, LICENCE_REACHES, OWN_SERVER, TOOLS_LIST, PREPARED)

    _, report = remote(*chosen)

    assert {test["outcome"] for test in report["tests"].values()} == {"passed"}
    assert len(report["tests"]) == len(chosen)
    # The server is restarted before the probe; the tests on it as the operator left it run
    # first, then the test that needs a server of its own, then each scenario set; and the server
    # is left as it was found. The licence key is the setting of the license/restricted
    # scenario, and the harness's credential is not given to the hook.
    seen = [json.loads(line) for line in stub.record.read_text(encoding="utf-8").splitlines()]
    assert [each["licenceKey"] for each in seen[1:]] == [None, None, LICENCE_KEY, None, None]
    assert {each["credentialGiven"] for each in seen} == {False}
    assert [each["scenarios"] for each in seen[1:]] == [
        "",
        "",
        "license/restricted",
        "release/unknown",
        "",
    ]


def test_dependent_tests_fail_when_the_server_does_not_reach_the_fixture_server(
    remote, monkeypatch, stub
):
    elsewhere = free_port()
    monkeypatch.setenv("NCI_SI_ACCEPTANCE_FIXTURE_BIND", f"127.0.0.1:{elsewhere}")

    result, report = remote(TOOLS_LIST)

    assert result.ret == 1
    result.stdout.fnmatch_lines(["*the server under test does not reach the fixture server*"])
    assert outcomes_of(report) == {TOOLS_LIST.partition("::")[2]: "failed"}


def test_every_dependent_test_fails_when_the_server_does_not_answer(remote, monkeypatch):
    monkeypatch.setenv("NCI_SI_ACCEPTANCE_URL", f"http://127.0.0.1:{free_port()}/mcp")

    result, report = remote(TOOLS_LIST, "tests/test_echo.py")

    assert outcomes_of(report) == {
        TOOLS_LIST.partition("::")[2]: "failed",
        "test_echo": "failed",
        "test_echo_token": "failed",
    }
    assert result.ret == 1
    result.stdout.fnmatch_lines(["*the server under test does not answer (*"])


def test_selected_live_mode_refuses_a_remote_target_without_a_report(remote, monkeypatch):
    monkeypatch.setenv("NCI_SI_ACCEPTANCE_MODE", "live")
    monkeypatch.setenv(environments.VARIABLE, "evs-only")
    monkeypatch.setenv("NCI_SI_ACCEPTANCE_PROFILE", "unified")
    for key in list(os.environ):
        if environments.external_runtime_setting(key) or key in {
            *client.UPSTREAM_VARIABLES,
            *environments.CREDENTIAL_SURFACES,
            "NCI_SI_ACCEPTANCE_FIXTURE_BIND",
        }:
            monkeypatch.delenv(key)
    result, report = remote(TOOLS_LIST)
    assert result.ret == USAGE_ERROR
    assert report is None
    assert result.stderr.str().strip() == (
        "ERROR: NCI_SI_ACCEPTANCE_URL is incompatible with the selected live environment"
    )


def test_naming_a_command_and_an_endpoint_stops_the_run_with_a_usage_error(compliant, monkeypatch):
    monkeypatch.setenv("NCI_SI_ACCEPTANCE_URL", "http://127.0.0.1:9/mcp")

    result = compliant.runpytest_subprocess("tests", "-p", "no:cacheprovider", "-p", STAND_IN)

    assert result.ret == USAGE_ERROR
    result.stderr.fnmatch_lines(
        ["*NCI_SI_ACCEPTANCE_URL and NCI_SI_ACCEPTANCE_SERVER are both set; name one server*"]
    )


def fetches_at_startup(path):
    """A state hook whose server asks the fixture server something while it starts."""

    code = f"""import os, urllib.error, urllib.request
try:
    with urllib.request.urlopen(os.environ['NCI_SI_EVS_BASE_URL'] + '{path}'):
        pass
except urllib.error.HTTPError as error:
    with error:
        raise
"""
    return f'{sys.executable} -c "{code}" || true'


def test_a_server_that_asks_the_fixture_server_while_the_hook_starts_it_passes_the_probe(
    remote, monkeypatch
):
    # The compliant server keeps asking the fixture server it was given, not this one.
    monkeypatch.setenv("NCI_SI_ACCEPTANCE_FIXTURE_BIND", f"127.0.0.1:{free_port()}")
    monkeypatch.setenv("NCI_SI_ACCEPTANCE_STATE_HOOK", fetches_at_startup("/api/v1/version"))

    result, report = remote(TOOLS_LIST)

    assert result.ret == 0
    assert outcomes_of(report) == {TOOLS_LIST.partition("::")[2]: "passed"}


def test_requests_without_a_fixture_while_the_hook_starts_the_server_fail_dependent_tests(
    remote, monkeypatch
):
    monkeypatch.setenv("NCI_SI_ACCEPTANCE_STATE_HOOK", fetches_at_startup("/api/v1/nothing"))

    result, report = remote(TOOLS_LIST, "tests/test_echo.py")

    assert outcomes_of(report) == {
        TOOLS_LIST.partition("::")[2]: "no_fixture",
        "test_echo": "no_fixture",
        "test_echo_token": "no_fixture",
    }
    assert all(
        test["unmatched"] == ["GET evs /api/v1/nothing {}"] for test in report["tests"].values()
    )
    assert result.ret == 1
    result.stdout.fnmatch_lines(
        ["*upstream requests without a fixture while the server started:*", "*/api/v1/nothing*"]
    )


def test_unmatched_requests_during_a_state_change_are_reported_for_its_test(remote, monkeypatch):
    fetch = fetches_at_startup("/api/v1/nothing")
    command = f'if [ -n "$NCI_SI_ACCEPTANCE_SCENARIOS" ]; then {fetch}; fi'
    monkeypatch.setenv("NCI_SI_ACCEPTANCE_STATE_HOOK", command)

    result, report = remote(TOOLS_LIST, UNKNOWN_RELEASE)

    assert outcomes_of(report) == {
        TOOLS_LIST.partition("::")[2]: "passed",
        UNKNOWN_RELEASE.partition("::")[2]: "no_fixture",
    }
    assert report["tests"][UNKNOWN_RELEASE]["unmatched"] == ["GET evs /api/v1/nothing {}"]
    assert result.ret == 1


def test_a_failed_scenario_state_change_is_reported_for_its_test(remote, monkeypatch):
    command = 'if [ -n "$NCI_SI_ACCEPTANCE_SCENARIOS" ]; then echo "state unavailable"; exit 3; fi'
    monkeypatch.setenv("NCI_SI_ACCEPTANCE_STATE_HOOK", command)

    result, report = remote(TOOLS_LIST, UNKNOWN_RELEASE)

    assert outcomes_of(report) == {
        TOOLS_LIST.partition("::")[2]: "passed",
        UNKNOWN_RELEASE.partition("::")[2]: "failed",
    }
    assert result.ret == 1
    result.stdout.fnmatch_lines(["*state unavailable*"])


def test_a_remote_server_is_tested_by_one_process(remote):
    result, _ = remote("-n", "2", TOOLS_LIST)

    assert result.ret == USAGE_ERROR
    result.stderr.fnmatch_lines(
        ["*a remote server (NCI_SI_ACCEPTANCE_URL) is tested by one process*"]
    )


# ---- the state hook, called directly


def hook_for(stub, tmp_path, command, timeout=30, authorization=CREDENTIAL):
    target = Target(
        "fixture",
        [],
        "evs",
        url=stub.url,
        authorization=authorization,
        state_hook=command,
        state_hook_timeout=timeout,
    )
    return StateHook(target, FixtureServer(FixtureSet({}, {})), tmp_path / "output.log")


def test_a_scenario_set_gets_one_state_change_and_a_server_of_its_own_every_time(stub, tmp_path):
    runs = tmp_path / "runs.txt"
    hook = hook_for(stub, tmp_path, f'echo "$SETTING" >> {runs}')

    hook.apply(("a/one",), {"SETTING": "one"})
    hook.apply(("a/one",), {"SETTING": "one"})
    hook.apply((), {"SETTING": "own"}, fresh=True)
    hook.apply((), {"SETTING": "own"}, fresh=True)
    hook.apply(("a/two",), {"SETTING": "two"})
    hook.restore()
    hook.restore()

    assert runs.read_text(encoding="utf-8").splitlines() == ["one", "own", "own", "two", ""]


def test_the_hook_gets_the_fixture_and_scenario_settings_but_none_of_the_developers_own(
    stub, tmp_path, monkeypatch
):
    seen = tmp_path / "environment.txt"
    monkeypatch.setenv("NCI_SI_EVS_LICENSE_KEY", "developers-own-key")
    monkeypatch.setenv("NCI_SI_UPSTREAM_MODE", "live")
    monkeypatch.setenv("NCI_SI_DATA_DIR", "/developers/data")
    monkeypatch.setenv("NCI_SI_ACCEPTANCE_PREPARED", "1")
    monkeypatch.setenv("NCI_SI_ACCEPTANCE_AUTHORIZATION", CREDENTIAL)
    hook = hook_for(stub, tmp_path, f"env > {seen}")

    hook.apply(("a/one", "a/two"), {"NCI_SI_EVS_LICENSE_KEY": "the-scenarios"})

    given = dict(
        line.split("=", 1) for line in seen.read_text(encoding="utf-8").splitlines() if "=" in line
    )
    assert given["NCI_SI_EVS_LICENSE_KEY"] == "the-scenarios"
    assert given["NCI_SI_UPSTREAM_MODE"] == "fixture"
    assert given["NCI_SI_EVS_BASE_URL"].endswith("/evs")
    assert given["NCI_SI_ACCEPTANCE_SCENARIOS"] == "a/one,a/two"
    assert given["NCI_SI_ACCEPTANCE_PREPARED"] == "1"
    assert "NCI_SI_DATA_DIR" not in given
    assert "NCI_SI_ACCEPTANCE_AUTHORIZATION" not in given


def test_a_state_hook_that_fails_fails_the_test_with_what_it_said_less_the_credential(
    stub, tmp_path, monkeypatch
):
    monkeypatch.setenv("NCI_SI_ACCEPTANCE_AUTHORIZATION", CREDENTIAL)
    command = (
        f'printf "%s" "{"x" * 3000}"; '
        f'echo "told {CREDENTIAL}, has [$NCI_SI_ACCEPTANCE_AUTHORIZATION]"; exit 3'
    )

    with pytest.raises(pytest.fail.Exception) as ended:
        hook_for(stub, tmp_path, command).apply(("a/one",), {})

    said = ended.value.msg
    assert "failed with exit status 3" in said
    assert "told [authorization withheld], has []" in said


def test_a_state_hook_that_does_not_return_fails_the_test(stub, tmp_path):
    with pytest.raises(pytest.fail.Exception, match=r"did not return within 0\.3 s"):
        hook_for(stub, tmp_path, "sleep 5", timeout=0.3).apply(("a/one",), {})


def test_a_server_that_does_not_come_back_fails_the_test(tmp_path):
    gone = Stub(f"http://127.0.0.1:{free_port()}/mcp", 0, {}, tmp_path, tmp_path)

    with pytest.raises(pytest.fail.Exception, match=r"does not answer \(nothing within 0\.3 s\)"):
        hook_for(gone, tmp_path, "true", timeout=0.3).apply(("a/one",), {})


def test_the_configured_timeout_ends_the_wait_for_a_server_that_takes_the_connection_and_is_silent(
    tmp_path,
):
    with socket.socket() as silent:
        silent.bind(("127.0.0.1", 0))
        silent.listen()
        taking = Stub(f"http://127.0.0.1:{silent.getsockname()[1]}/mcp", 0, {}, tmp_path, tmp_path)
        started = time.monotonic()

        with pytest.raises(pytest.fail.Exception, match=r"nothing within 1 s\) after the state"):
            hook_for(taking, tmp_path, "true", timeout=1).apply(("a/one",), {})

    assert time.monotonic() - started < PROMPTLY_SECONDS


def test_a_server_that_refuses_the_credential_fails_the_test_at_once_naming_the_status_only(
    stub, tmp_path
):
    started = time.monotonic()

    with pytest.raises(pytest.fail.Exception) as ended:
        hook_for(stub, tmp_path, "true", timeout=30, authorization="Bearer wrong").apply(
            ("a/one",), {}
        )

    assert ended.value.msg == (
        "the server under test does not answer (HTTP 401) after the state change"
    )
    assert time.monotonic() - started < PROMPTLY_SECONDS


def test_a_session_with_a_server_that_refuses_the_credential_fails(stub):
    with pytest.raises(ExceptionGroup), open_remote_session(stub.url, "Bearer wrong"):
        pass


def test_a_remote_process_has_no_standard_error_or_data_directory_to_read():
    assert Process(None, None, ()).written() == ""


def test_the_fixture_server_listens_where_it_is_told(stub):
    with FixtureServer(FixtureSet({}, {}), ("127.0.0.1", stub.fixture_port)) as server:
        assert server.url == f"http://127.0.0.1:{stub.fixture_port}"
    with FixtureServer(FixtureSet({}, {}), ("0.0.0.0", 0)) as anywhere:  # noqa: S104
        assert anywhere.url.startswith("http://127.0.0.1:")


# ---- the checks before the first test, called directly

PINNED = {"terminology": "ncit"}


def test_a_profile_without_release_discovery_can_pass_the_probe(monkeypatch):
    session = SimpleNamespace(
        list_tools=lambda: SimpleNamespace(tools=[SimpleNamespace(name="get_form")])
    )
    monkeypatch.setattr(
        remote_module, "open_remote_session", lambda *_args, **_kwargs: nullcontext(session)
    )
    upstream = FixtureServer(FixtureSet({}, {}))

    assert probe("http://server.example/mcp", None, upstream, PINNED) is None
    assert upstream.log() == []


@pytest.mark.parametrize("settings", [{"url": "http://server.example/mcp"}, {"state_hook": "true"}])
def test_a_state_hook_requires_both_a_remote_endpoint_and_a_command(tmp_path, settings):
    target = Target("fixture", [], **settings)

    with pytest.raises(ValueError, match="needs a remote server and the operator's hook"):
        StateHook(target, FixtureServer(FixtureSet({}, {})), tmp_path / "hook.log")


def test_the_operator_is_told_the_urls_the_server_reaches_the_fixture_server_by(stub):
    named = Target("fixture", [], url=stub.url, fixture_url="https://fixtures.example")
    unnamed = Target("fixture", [], url=stub.url)

    with FixtureServer(FixtureSet({}, {})) as upstream:
        told, default = announcement(named, upstream), announcement(unnamed, upstream)

    assert told[:3] == [
        "settings of the server under test:",
        "  NCI_SI_UPSTREAM_MODE=fixture",
        "  NCI_SI_EVS_BASE_URL=https://fixtures.example/evs",
    ]
    assert f"  NCI_SI_EVS_BASE_URL={upstream.url}/evs" in default


def test_a_server_that_asks_the_fixture_server_passes_the_probe_and_leaves_its_log_clean(stub):
    with FixtureServer(FixtureSet({}, {}), ("127.0.0.1", stub.fixture_port)) as upstream:
        probe(stub.url, CREDENTIAL, upstream, PINNED)

        assert upstream.log() == []


def test_a_server_that_asks_nothing_of_the_fixture_server_fails_the_probe(stub):
    with (
        FixtureServer(FixtureSet({}, {}), ("127.0.0.1", free_port())) as upstream,
        pytest.raises(pytest.fail.Exception, match="does not reach the fixture server"),
    ):
        probe(stub.url, CREDENTIAL, upstream, PINNED)


def test_a_server_that_gives_no_answer_fails_the_probe_without_its_message(stub):
    with pytest.raises(pytest.fail.Exception) as ended:
        probe(f"http://127.0.0.1:{free_port()}/mcp", CREDENTIAL, None, PINNED)

    assert ended.value.msg.startswith("the server under test does not answer (")
    assert CREDENTIAL not in ended.value.msg


def test_without_a_fixture_server_the_probe_asks_only_for_an_answer(stub):
    assert probe(stub.url, CREDENTIAL, None, PINNED) is None


def reached_directly(upstream):
    """The log entries of a request made to the fixture server, as a server that reaches it
    makes one (the fixture set is empty, so none finds a fixture)."""

    try:
        with urllib.request.urlopen(f"{upstream.url}/evs/api/v1/version"):  # noqa: S310
            pass
    except urllib.error.HTTPError as error:
        with error:
            pass
    return upstream.log()


def test_a_request_to_the_fixture_server_before_the_probe_does_not_pass_it(stub):
    with FixtureServer(FixtureSet({}, {}), ("127.0.0.1", free_port())) as upstream:
        assert reached_directly(upstream)

        with pytest.raises(pytest.fail.Exception, match="does not reach the fixture server"):
            probe(stub.url, CREDENTIAL, upstream, PINNED)


def test_what_the_server_asked_while_it_started_counts_as_reaching_the_fixture_server(stub):
    with FixtureServer(FixtureSet({}, {}), ("127.0.0.1", free_port())) as upstream:
        probe(stub.url, CREDENTIAL, upstream, PINNED, [{"fixture": "recorded/x.json"}])

        assert upstream.log() == []


def test_requests_without_a_fixture_while_the_server_started_fail_the_probe(stub):
    with FixtureServer(FixtureSet({}, {}), ("127.0.0.1", free_port())) as upstream:
        startup = reached_directly(upstream)

        with pytest.raises(pytest.fail.Exception, match="without a fixture while the server star"):
            probe(stub.url, CREDENTIAL, upstream, PINNED, startup)


def test_a_server_that_refuses_the_credential_fails_the_probe_naming_only_the_status(stub):
    with pytest.raises(pytest.fail.Exception) as ended:
        probe(stub.url, "Bearer wrong", None, PINNED)

    assert ended.value.msg == "the server under test does not answer (HTTP 401)"


def test_the_operator_is_warned_when_the_fixture_server_listens_on_every_address(stub):
    anywhere = Target("fixture", [], url=stub.url, fixture_bind=("0.0.0.0", 8099))  # noqa: S104
    named = replace(anywhere, fixture_url="https://fixtures.example")
    local = replace(anywhere, fixture_bind=("127.0.0.1", 8099))

    with FixtureServer(FixtureSet({}, {})) as upstream:
        warned, told, quiet = (announcement(t, upstream) for t in (anywhere, named, local))

    assert [line for line in warned if line.startswith("warning:")] == [
        "warning: the fixture server listens on every address but the URLs name "
        f"{upstream.url}; if the server under test runs elsewhere, set "
        "NCI_SI_ACCEPTANCE_FIXTURE_URL"
    ]
    assert not [line for line in told + quiet if line.startswith("warning:")]
