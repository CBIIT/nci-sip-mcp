"""The harness starts the server under test against the run mode's upstream."""

import socket
import sys
from pathlib import Path

import pytest
from mcp.shared.exceptions import MCPError

from conftest import STAND_IN
from nci_sip_acceptance import client
from nci_sip_acceptance.client import (
    AUTHORIZATION_VARIABLE,
    FIXTURE_BIND_VARIABLE,
    FIXTURE_URL_VARIABLE,
    MODE_VARIABLE,
    PREPARE_VARIABLE,
    PREPARED_VARIABLE,
    PROFILE_VARIABLE,
    REGISTRY_RELEASE_VARIABLE,
    REMOTE_ONLY,
    SECURITY_SERVER_VARIABLE,
    SERVER_VARIABLE,
    STATE_HOOK_TIMEOUT_VARIABLE,
    STATE_HOOK_VARIABLE,
    URL_VARIABLE,
    Target,
    open_session,
    remote_settings,
    server_environment,
    wait_for_endpoint,
    withhold_authorization,
)
from nci_sip_acceptance.fixture_server import UPSTREAM_VARIABLES, FixtureServer, FixtureSet
from nci_sip_acceptance.spec import PROMPTS, resources_listed

pytest_plugins = ["pytester"]

# The furnished server, started from the environment the tests run in.
BASELINE_SERVER = [sys.executable, "-m", "nci_sip_mcp.cli", "serve"]
COMPLIANT_SERVER = Path(__file__).parent / "compliant_server.py"


@pytest.mark.parametrize("remote", [False, True], ids=["fixture", "remote"])
def test_standalone_harness_collects_without_server(compliant, monkeypatch, remote):
    (compliant.path / "without_server.py").write_text(
        "import sys\nfrom pathlib import Path\n"
        "class MissingServer:\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        if name.partition('.')[0] == 'nci_sip_mcp':\n"
        "            raise ModuleNotFoundError(name, name=name)\n"
        "sys.meta_path.insert(0, MissingServer())\n"
        "try: import nci_sip_mcp\n"
        "except ModuleNotFoundError: Path('core-availability').write_text('absent')\n"
        "else: Path('core-availability').write_text('present')\n"
    )
    with FixtureServer(FixtureSet({}, {})) as endpoint:
        if remote:
            monkeypatch.delenv(SERVER_VARIABLE)
            monkeypatch.delenv(PREPARE_VARIABLE)
            monkeypatch.setenv(URL_VARIABLE, endpoint.url + "/mcp")
        result = compliant.runpytest_subprocess(
            "tests/test_protocol.py", "--collect-only", "-q", "-p", "without_server", "-p", STAND_IN
        )
        assert endpoint.log() == []
    assert (compliant.path / "core-availability").read_text() == "absent"
    assert result.ret == 0, result.stdout.str() + result.stderr.str()


def test_the_default_target_is_the_installed_server_against_fixtures(monkeypatch):
    monkeypatch.delenv(MODE_VARIABLE, raising=False)
    monkeypatch.delenv(SERVER_VARIABLE, raising=False)
    monkeypatch.delenv(PROFILE_VARIABLE, raising=False)

    assert Target.from_env() == Target("fixture", ["nci-sip-mcp", "serve"], "unified")


def test_the_target_is_read_from_the_environment(monkeypatch):
    monkeypatch.setenv(MODE_VARIABLE, "fixture")
    monkeypatch.setenv(SERVER_VARIABLE, "python -m 'my server' --flag")
    monkeypatch.setenv(PROFILE_VARIABLE, "evs")

    assert Target.from_env() == Target("fixture", ["python", "-m", "my server", "--flag"], "evs")


@pytest.mark.parametrize("value", [None, "optional", "required", "", "REQUIRED", "other"])
@pytest.mark.parametrize("remote", [False, True])
def test_registry_selection_is_validated_and_never_given_to_the_server(
    monkeypatch, tmp_path, value, remote
):
    monkeypatch.delenv(SERVER_VARIABLE, raising=False)
    monkeypatch.delenv(URL_VARIABLE, raising=False)
    if remote:
        monkeypatch.setenv(URL_VARIABLE, "http://server.example/mcp")
    monkeypatch.delenv(REGISTRY_RELEASE_VARIABLE, raising=False)
    if value is not None:
        monkeypatch.setenv(REGISTRY_RELEASE_VARIABLE, value)
    if value not in (None, "optional", "required"):
        with pytest.raises(ValueError) as error:
            Target.from_env()
        assert str(error.value) == (
            f"{REGISTRY_RELEASE_VARIABLE} must be required or optional, not {value!r}"
        )
    else:
        assert Target.from_env().registry_release == (value or "optional")
    assert REGISTRY_RELEASE_VARIABLE not in server_environment(
        "fixture", tmp_path, "http://127.0.0.1:9"
    )


def test_the_secured_fixture_adapter_is_read_for_a_local_and_a_remote_target(monkeypatch):
    monkeypatch.delenv(URL_VARIABLE, raising=False)
    monkeypatch.setenv(SECURITY_SERVER_VARIABLE, "python adapter.py")
    local = Target.from_env()
    monkeypatch.setenv(URL_VARIABLE, "http://server.example/mcp")
    remote = Target.from_env()
    monkeypatch.setenv(SECURITY_SERVER_VARIABLE, "")
    blank = Target.from_env()

    assert (local.security_server, remote.security_server, blank.security_server) == (
        "python adapter.py",
        "python adapter.py",
        None,
    )


@pytest.mark.parametrize(
    ("variable", "value", "message"),
    [
        (MODE_VARIABLE, "both", "must be fixture or live, not 'both'"),
        (SERVER_VARIABLE, "  ", "must name a command"),
        (PROFILE_VARIABLE, "both", "must be one of evs, cadsr, unified"),
    ],
)
def test_a_wrong_target_is_refused_naming_its_variable(monkeypatch, variable, value, message):
    monkeypatch.setenv(variable, value)

    with pytest.raises(ValueError, match=f"{variable} {message}"):
        Target.from_env()


def test_in_fixture_mode_every_upstream_names_the_fixture_server(monkeypatch, tmp_path):
    monkeypatch.setenv("NCI_SIP_EVS_BASE_URL", "https://developer.example")
    monkeypatch.setenv("NCI_SIP_LOG_LEVEL", "DEBUG")

    environment = server_environment("fixture", tmp_path, "http://127.0.0.1:9")

    # The upstream settings of docs/implementation-plan.md §8, each naming its fixture surface.
    assert {name: environment[name] for name in UPSTREAM_VARIABLES} == {
        "NCI_SIP_EVS_BASE_URL": "http://127.0.0.1:9/evs",
        "NCI_SIP_EVS_FHIR_BASE_URL": "http://127.0.0.1:9/evs-fhir",
        "NCI_SIP_CADSR_BASE_URL": "http://127.0.0.1:9/cadsr",
        "NCI_SIP_CADSR_FTP_URL": "http://127.0.0.1:9/cadsr-ftp",
        "NCI_SIP_SSIS_SPARQL_URL": "http://127.0.0.1:9/ssis-sparql",
    }
    assert (environment["NCI_SIP_UPSTREAM_MODE"], environment["NCI_SIP_DATA_DIR"]) == (
        "fixture",
        str(tmp_path),
    )
    assert "NCI_SIP_LOG_LEVEL" not in environment


def test_fixture_mode_strips_credentials_and_preserves_custom_runtime_settings(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("NCI_SIP_EVS_BASE_URL", "https://developer.example")
    # The credentials of docs/implementation-plan.md §8.
    credentials = {"NCI_SIP_EVS_LICENSE_KEY": "evs key", "NCI_SIP_CADSR_CREDENTIAL": "cadsr key"}
    for name, value in credentials.items():
        monkeypatch.setenv(name, value)

    monkeypatch.setenv("LD_LIBRARY_PATH", "synthetic-never-executed")
    fixture = server_environment("fixture", tmp_path, "http://127.0.0.1:9")

    assert fixture["NCI_SIP_EVS_BASE_URL"] == "http://127.0.0.1:9/evs"
    assert fixture["NCI_SIP_UPSTREAM_MODE"] == "fixture"
    assert fixture["LD_LIBRARY_PATH"] == "synthetic-never-executed"
    assert not set(credentials) & set(fixture)


def test_fixture_mode_needs_the_fixture_server(tmp_path):
    with pytest.raises(ValueError, match="needs the fixture server's URL"):
        server_environment("fixture", tmp_path, None)


def test_every_upstream_request_of_the_server_under_test_is_recorded(tmp_path):
    with FixtureServer(FixtureSet({}, {})) as upstream:
        environment = server_environment("fixture", tmp_path, upstream.url)
        with open_session(BASELINE_SERVER, environment) as session:
            tools = session.list_tools().tools
            # A tool without required arguments that reaches upstream; with no
            # fixtures every request is refused, and each attempt is recorded.
            reaching = [tool.name for tool in tools if not tool.input_schema.get("required")]
            results = [session.call_tool(name, {}) for name in reaching]

    assert tools
    assert results
    log = upstream.log()
    assert log
    assert {entry["surface"] for entry in log} <= set(UPSTREAM_VARIABLES.values())
    assert all(entry["fixture"] is None for entry in log)


def test_a_session_lists_and_gets_prompts_and_lists_and_reads_resources(tmp_path, monkeypatch):
    monkeypatch.setattr(client, "READ_TIMEOUT_SECONDS", 0.001)
    command = [sys.executable, str(COMPLIANT_SERVER)]
    with FixtureServer(FixtureSet({}, {})) as upstream:
        environment = server_environment("fixture", tmp_path, upstream.url)
        environment |= {"COMPLIANT_SERVER_PROFILE": "unified"}
        with open_session(command, environment, timeout=30) as session:
            prompts = session.list_prompts().prompts
            got = session.get_prompt("crdc_model_alignment", {"field": "f", "commons": "GDC"})
            resources = session.list_resources().resources
            templates = session.list_resource_templates().resource_templates
            # The upstream has no fixture, so the server refuses the read; the refusal arrives.
            with pytest.raises(MCPError, match="upstream_unavailable"):
                session.read_resource("cadsr://registry/release")

    assert sorted(prompt.name for prompt in prompts) == sorted(PROMPTS)
    assert "the commons GDC" in got.messages[0].content.text
    assert {resource.uri for resource in resources} == {
        "cadsr://registry/release",
        "cadsr://crosswalk/crdc",
    }
    assert "ncit://concept/{release}/{code}" in {each.uri_template for each in templates}


def test_a_session_follows_the_cursor_of_every_list_it_reads_to_the_last_page(tmp_path):
    command = [sys.executable, str(COMPLIANT_SERVER)]
    with FixtureServer(FixtureSet({}, {})) as upstream:
        environment = server_environment("fixture", tmp_path, upstream.url)
        # One item to a page: without the cursor a list would hold its first item only.
        environment |= {"COMPLIANT_SERVER_PROFILE": "unified", "COMPLIANT_SERVER_PAGE_SIZE": "1"}
        with open_session(command, environment) as session:
            prompts = session.list_prompts()
            resources = session.list_resources()
            templates = session.list_resource_templates()

    assert sorted(prompt.name for prompt in prompts.prompts) == sorted(PROMPTS)
    assert len(resources.resources) == len(resources_listed("unified").uris)
    assert len(templates.resource_templates) == len(resources_listed("unified").templates)
    # The list is whole: no cursor is left to follow, and the hint is the first page's.
    assert [each.next_cursor for each in (prompts, resources, templates)] == [None] * 3
    assert prompts.ttl_ms > 0


def test_a_server_that_never_answers_ends_the_session_with_a_timeout(monkeypatch, tmp_path):
    monkeypatch.setattr(client, "READ_TIMEOUT_SECONDS", 1)

    with (
        pytest.raises(ExceptionGroup) as raised,
        open_session(
            ["sleep", "30"], server_environment("fixture", tmp_path, "http://127.0.0.1:9")
        ) as session,
    ):
        session.list_tools()

    assert raised.group_contains(MCPError, match="timed out", depth=None)


ENDPOINT = "https://server.example/mcp"
EVERY_VARIABLE = (
    MODE_VARIABLE,
    SERVER_VARIABLE,
    PROFILE_VARIABLE,
    PREPARE_VARIABLE,
    URL_VARIABLE,
    FIXTURE_BIND_VARIABLE,
    *REMOTE_ONLY,
)


@pytest.fixture
def remote(monkeypatch):
    """An environment that names a remote server and nothing else of the harness's settings."""

    for name in EVERY_VARIABLE:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(URL_VARIABLE, ENDPOINT)
    return monkeypatch


def test_a_remote_target_is_read_from_the_environment(remote):
    remote.setenv(PROFILE_VARIABLE, "evs")
    remote.setenv(AUTHORIZATION_VARIABLE, "Bearer abc")
    remote.setenv(FIXTURE_BIND_VARIABLE, "0.0.0.0:8099")
    remote.setenv(FIXTURE_URL_VARIABLE, "https://fixtures.example/")
    remote.setenv(STATE_HOOK_VARIABLE, "hook-it")
    remote.setenv(STATE_HOOK_TIMEOUT_VARIABLE, "90")
    remote.setenv(PREPARED_VARIABLE, "1")

    target = Target.from_env()

    assert target == Target(
        "fixture",
        [],
        "evs",
        None,
        ENDPOINT,
        "Bearer abc",
        ("0.0.0.0", 8099),  # noqa: S104
        "https://fixtures.example",
        "hook-it",
        90.0,
        True,
    )
    assert (target.transport, target.has_index) == ("streamable-http", True)


def test_a_remote_target_has_no_state_hook_and_no_index_unless_the_operator_gives_them(remote):
    target = Target.from_env()

    assert (target.state_hook, target.authorization, target.fixture_bind) == (None, None, None)
    assert (target.transport, target.has_index, target.state_hook_timeout) == (
        "streamable-http",
        False,
        60.0,
    )


def test_a_stdio_target_has_an_index_only_where_the_harness_prepares_one(monkeypatch):
    monkeypatch.delenv(URL_VARIABLE, raising=False)
    monkeypatch.setenv(PREPARE_VARIABLE, "true")

    prepared = Target.from_env()
    monkeypatch.delenv(PREPARE_VARIABLE)

    assert (prepared.transport, prepared.has_index) == ("stdio", True)
    assert Target.from_env().has_index is False


def test_naming_a_command_and_an_endpoint_is_a_usage_error(remote):
    remote.setenv(SERVER_VARIABLE, "nci-sip-mcp serve")

    with pytest.raises(ValueError, match=f"{URL_VARIABLE} and {SERVER_VARIABLE} are both set"):
        Target.from_env()


def test_the_harness_never_runs_a_prepare_command_against_a_remote_server(remote):
    remote.setenv(PREPARE_VARIABLE, "true")

    with pytest.raises(ValueError, match=f"{PREPARE_VARIABLE} is never run against a remote"):
        Target.from_env()


@pytest.mark.parametrize("variable", REMOTE_ONLY)
def test_a_setting_for_a_remote_server_alone_is_refused_without_one_naming_only_the_variable(
    remote, variable
):
    remote.delenv(URL_VARIABLE)
    remote.setenv(variable, "secret-value")

    with pytest.raises(ValueError, match=f"^{variable} apply only with {URL_VARIABLE}$"):
        Target.from_env()


@pytest.mark.parametrize(
    ("variable", "value", "message"),
    [
        (URL_VARIABLE, "ftp://server.example/mcp", "must be an http or https URL"),
        (URL_VARIABLE, "http://", "must be an http or https URL"),
        (FIXTURE_BIND_VARIABLE, "host:", "must be HOST or HOST:PORT, not 'host:'"),
        (FIXTURE_BIND_VARIABLE, ":80", "must be HOST or HOST:PORT, not ':80'"),
        (FIXTURE_BIND_VARIABLE, "host:http", "must be HOST or HOST:PORT, not 'host:http'"),
        (STATE_HOOK_TIMEOUT_VARIABLE, "soon", "must be a positive number of seconds, not 'soon'"),
        (STATE_HOOK_TIMEOUT_VARIABLE, "0", "must be a positive number of seconds, not '0'"),
        (PREPARED_VARIABLE, "yes", "must be 1 or unset, not 'yes'"),
    ],
)
def test_a_wrong_remote_setting_is_refused_naming_its_variable(remote, variable, value, message):
    remote.setenv(variable, value)

    with pytest.raises(ValueError, match=f"{variable} {message}"):
        Target.from_env()


def test_a_fixture_server_address_without_a_port_takes_any_port(remote):
    remote.setenv(FIXTURE_BIND_VARIABLE, "0.0.0.0")  # noqa: S104

    assert Target.from_env().fixture_bind == ("0.0.0.0", 0)  # noqa: S104


def test_the_credential_is_not_in_the_target_s_representation(remote):
    remote.setenv(AUTHORIZATION_VARIABLE, "Bearer do-not-print")

    assert "do-not-print" not in repr(Target.from_env())


def test_the_credential_is_withheld_from_text_and_other_text_is_kept(remote):
    remote.setenv(AUTHORIZATION_VARIABLE, "Bearer do-not-print")

    shown = withhold_authorization("sent Bearer do-not-print, twice: Bearer do-not-print")

    assert shown == "sent [authorization withheld], twice: [authorization withheld]"
    remote.delenv(AUTHORIZATION_VARIABLE)
    assert withhold_authorization("Bearer do-not-print") == "Bearer do-not-print"


def test_the_token_after_the_scheme_is_withheld_too_where_it_is_long_enough(remote):
    remote.setenv(AUTHORIZATION_VARIABLE, "Bearer do-not-print")

    assert (
        withhold_authorization("echoed do-not-print only") == "echoed [authorization withheld] only"
    )
    remote.setenv(AUTHORIZATION_VARIABLE, "Bearer short")
    assert withhold_authorization("Bearer short, short") == "[authorization withheld], short"


def test_the_operator_sets_the_fixture_mode_and_every_upstream_on_a_remote_server():
    settings = remote_settings("https://fixtures.example")

    assert settings["NCI_SIP_UPSTREAM_MODE"] == "fixture"
    assert settings["NCI_SIP_EVS_BASE_URL"] == "https://fixtures.example/evs"
    assert set(settings) - {"NCI_SIP_UPSTREAM_MODE"} == set(UPSTREAM_VARIABLES)


def test_waiting_for_an_endpoint_that_does_not_answer_ends_with_the_time_given():
    with socket.socket() as closed:
        closed.bind(("127.0.0.1", 0))
        url = f"http://127.0.0.1:{closed.getsockname()[1]}/mcp"

    assert wait_for_endpoint(url, None, 0.2) == "nothing within 0.2 s"
