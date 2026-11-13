"""Managed transport paths use real local receivers and never contact an upstream."""

import os
import runpy
import socket
import sys
import threading
import urllib.request
from contextlib import nullcontext
from dataclasses import replace
from http import HTTPStatus
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

import pytest

from conftest import FIXTURES, furnished_child
from nci_si_acceptance import denied_surfaces, environments, live_launcher
from nci_si_acceptance.client import server_environment
from nci_si_acceptance.denied_surfaces import DeniedSurfaces
from nci_si_acceptance.fixture_server import FixtureServer, FixtureSet, Response, load_fixtures
from nci_si_mcp import cli, http_client

FURNISHED_PREFIX = Path(live_launcher.__file__).resolve().parents[3] / ".venv"


def listener_closed(address):
    with socket.socket() as probe:
        probe.settimeout(2)
        return probe.connect_ex(address) != 0


def test_real_furnished_child_ignores_ambient_os_proxy(tmp_path):
    with (
        FixtureServer(load_fixtures(FIXTURES)) as upstream,
        FixtureServer(FixtureSet({}, {})) as proxy,
    ):
        environment = server_environment("fixture", tmp_path, upstream.url)
        shadow = tmp_path / "external"
        shadow.mkdir()
        marker = tmp_path / "external-code-ran"
        (shadow / "sitecustomize.py").write_text(f"open({str(marker)!r}, 'w').close()")
        environment["PYTHONPATH"] = str(shadow)
        # All five core bases are already loopback fixtures before the missing-entrypoint red.
        script = (
            "import sys, urllib.request as u; "
            f"u.getproxies=lambda: {{'http': {proxy.url!r}}}; "
            "u.proxy_bypass=lambda host: False; "
            "from nci_si_acceptance.live_launcher import main; "
            "sys.argv=['launcher','index-sample','C4817']; raise SystemExit(main())"
        )
        ran = furnished_child([sys.executable, "-I", "-c", script], environment)
        assert ran.returncode == 0, ran.stderr
        assert upstream.log(), "the approved local destination must receive the actual request"
        assert proxy.log() == [], "neither environment nor OS proxy discovery may reroute the child"
        assert not marker.exists()


def test_owned_refusal_has_no_echo_or_forwarding_and_closes_on_exception(monkeypatch, capfd):
    timeouts = []
    original = denied_surfaces._Refusal.handle

    def observed(request):
        original(request)
        timeouts.append(request.request.gettimeout())

    monkeypatch.setattr(denied_surfaces._Refusal, "handle", observed)
    denied = DeniedSurfaces()
    with pytest.raises(RuntimeError, match="owner interrupted"), denied as url:
        address = url.removeprefix("http://").split(":")
        port = int(address[1])
        with (
            pytest.raises(HTTPError) as error,
            build_opener(ProxyHandler({})).open(url + "/private-marker?secret-marker", timeout=2),
        ):
            pass
        with error.value:
            assert error.value.code == HTTPStatus.FORBIDDEN
            assert error.value.read() == b"surface disabled by run policy"
        raise RuntimeError("owner interrupted")
    assert timeouts == [1.0]
    output = capfd.readouterr()
    assert "private-marker" not in output.out + output.err
    assert "secret-marker" not in output.out + output.err
    assert not denied.thread.is_alive()
    assert listener_closed(("127.0.0.1", port))


def test_partial_request_cannot_hold_the_refusal_listener_open():
    denied = DeniedSurfaces()
    with denied as url:
        port = int(url.rsplit(":", 1)[1])
        with socket.create_connection(("127.0.0.1", port), timeout=2) as peer:
            peer.sendall(b"GET /incomplete-private-marker")
            expected = b"HTTP/1.0 403 Forbidden\r\n"
            # Bound the status read independently; buffered reads tolerate TCP fragmentation.
            with peer.makefile("rb") as response:
                assert response.readline(len(expected) + 1) == expected
    assert not denied.thread.is_alive()


@pytest.mark.parametrize(
    ("selected", "denied_url"),
    [(False, None), (False, "http://127.0.0.1:1"), (True, None)],
)
def test_live_environment_requires_both_owned_prerequisites(
    monkeypatch, tmp_path, selected, denied_url
):
    for key in list(os.environ):
        if key.startswith(("NCI_SI_", "PYTHON")) or key.lower().endswith("_proxy"):
            monkeypatch.delenv(key)
    monkeypatch.setenv("NCI_SI_ACCEPTANCE_ENVIRONMENT", "evs-only")
    environment = environments.selection("live") if selected else None
    with pytest.raises(ValueError) as error:
        server_environment("live", tmp_path, None, environment=environment, denied_url=denied_url)
    assert (
        str(error.value) == "live mode requires a selected environment and owned refusal endpoint"
    )


START_FAILED = "owned refusal listener failed to start"
FINISH_FAILED = "owned refusal listener did not finish cleanly"


@pytest.mark.parametrize(
    ("boundary", "fault", "stopping", "expected_error", "message"),
    [
        ("start", RuntimeError, False, RuntimeError, START_FAILED),
        ("start", OSError, False, RuntimeError, START_FAILED),
        ("start", KeyboardInterrupt, False, KeyboardInterrupt, "private-marker"),
        ("handle_request", OSError, False, RuntimeError, FINISH_FAILED),
        ("handle_request", RuntimeError, False, RuntimeError, FINISH_FAILED),
        ("server_close", OSError, False, RuntimeError, FINISH_FAILED),
        ("finish_request", OSError, False, RuntimeError, FINISH_FAILED),
        ("handle_request", OSError, True, None, None),
        ("handle_request", ValueError, True, None, None),
        ("handle_request", RuntimeError, True, RuntimeError, FINISH_FAILED),
    ],
)
def test_listener_failures_are_visible_sanitized_and_leave_no_owned_resource(
    monkeypatch, capfd, boundary, fault, stopping, expected_error, message
):
    denied = DeniedSurfaces()
    port = denied._server.server_address[1]
    thread_errors = []
    monkeypatch.setattr(
        threading, "excepthook", lambda failure: thread_errors.append(str(failure.exc_value))
    )
    close = denied._server.server_close
    entered = threading.Event()
    stopped, closed, received = [], [], []

    def fail(*_args):
        if boundary == "server_close":
            close()
        entered.set()
        if stopping:
            stopped.append(denied._stop.wait(2))
        raise fault("private-marker")

    owner = denied.thread if boundary == "start" else denied._server
    monkeypatch.setattr(owner, boundary, fail)
    expectation = pytest.raises(expected_error) if expected_error else nullcontext()
    with expectation as error, denied:
        if boundary == "handle_request":
            assert entered.wait(2)
            if not stopping:
                denied.thread.join(2)
                closed.append(listener_closed(("127.0.0.1", port)))
        elif boundary == "finish_request":
            with socket.create_connection(("127.0.0.1", port), timeout=2) as peer:
                received.append(peer.recv(1))
    if expected_error:
        assert str(error.value) == message
    assert entered.is_set()
    assert stopped == ([True] if stopping else [])
    assert closed == ([True] if boundary == "handle_request" and not stopping else [])
    assert received == ([b""] if boundary == "finish_request" else [])
    assert not denied.thread.is_alive()
    assert listener_closed(("127.0.0.1", port))
    output = capfd.readouterr()
    assert "private-marker" not in output.out + output.err
    assert thread_errors == []


def test_held_handler_keeps_incomplete_shutdown_visible_and_owner_socket_closed(monkeypatch):
    denied = DeniedSurfaces()
    entered, release = threading.Event(), threading.Event()

    def held(_request):
        entered.set()
        assert release.wait(5)

    monkeypatch.setattr(denied_surfaces._Refusal, "handle", held)
    monkeypatch.setattr(denied_surfaces, "JOIN_SECONDS", 0.01)
    url = denied.__enter__()
    address = ("127.0.0.1", int(url.rsplit(":", 1)[1]))
    try:
        with socket.create_connection(address, timeout=2):
            assert entered.wait(2)
            with pytest.raises(RuntimeError) as error:
                denied.__exit__(None, None, None)
            assert str(error.value) == FINISH_FAILED
            assert denied.thread.is_alive()
            assert listener_closed(address)
    finally:
        release.set()
        denied.thread.join(2)
        denied._close()
    assert not denied.thread.is_alive()
    assert listener_closed(address)


@pytest.mark.parametrize(
    ("boundary", "message"),
    [
        ("module", "imported an unexpected module origin"),
        ("prefix", "requires the furnished project environment"),
        ("hook", "transport integration is unavailable"),
    ],
)
def test_launcher_refuses_untrusted_installation_before_cli(
    monkeypatch, tmp_path, boundary, message
):
    marker = tmp_path / "cli-ran"
    monkeypatch.setattr(cli, "main", lambda: marker.write_text("ran"))
    monkeypatch.setattr(sys, "argv", ["launcher", "serve"])
    monkeypatch.setattr(sys, "prefix", str(FURNISHED_PREFIX))
    if boundary == "module":
        monkeypatch.setattr(live_launcher.nci_si_acceptance, "__file__", str(tmp_path / "other.py"))
    elif boundary == "prefix":
        monkeypatch.setattr(sys, "prefix", str(tmp_path))
    else:
        monkeypatch.setattr(http_client, "_OPENER", None)
    with pytest.raises(ValueError) as error:
        live_launcher.main()
    assert (
        str(error.value)
        == "controlled live " + ("launcher " if boundary != "hook" else "") + message
    )
    assert not marker.exists()


@pytest.fixture
def redirect_receivers():
    with FixtureServer(FixtureSet({}, {})) as forbidden:
        redirect = Response(302, {"Location": forbidden.url + "/must-not-arrive"})
        fixtures = load_fixtures(FIXTURES)
        redirected = replace(
            fixtures,
            ordinary={
                key: replace(row, responses=(redirect,)) for key, row in fixtures.ordinary.items()
            },
        )
        with FixtureServer(redirected) as selected:
            yield selected, forbidden


@pytest.mark.parametrize("credential", [None, "private-marker"])
def test_furnished_child_never_forwards_cross_origin_redirect_or_credentials(
    tmp_path, credential, redirect_receivers
):
    selected, forbidden = redirect_receivers
    with DeniedSurfaces() as denied:
        origins = dict.fromkeys(environments.SURFACES)
        origins["evs"] = selected.base_url("evs")
        credentials = {"NCI_SI_EVS_LICENSE_KEY": credential} if credential else {}
        environment = server_environment(
            "live",
            tmp_path,
            None,
            environment=environments.Environment("local-test", origins, credentials, "a" * 64),
            denied_url=denied,
        )
        ran = furnished_child([*environments.LAUNCHER, "index-sample", "C4817"], environment)
        assert ran.returncode != 0
        assert forbidden.log() == []
        received = selected.log()
        assert len(received) == 1
        headers = {key.lower(): value for key, value in received[0]["headers"].items()}
        assert headers.get("x-evsrestapi-license-key") == credential
        assert "private-marker" not in ran.stdout + ran.stderr


@pytest.fixture
def callable_launcher(monkeypatch):
    root = Path(__file__).resolve().parents[2]
    assert (
        Path(live_launcher.__file__).resolve()
        == root / "acceptance/src/nci_si_acceptance/live_launcher.py"
    )
    assert Path(http_client.__file__).resolve() == root / "src/nci_si_mcp/http_client.py"
    monkeypatch.setattr(sys, "prefix", str(root / ".venv"))
    monkeypatch.setattr(http_client, "_OPENER", http_client._OPENER)
    return live_launcher.main


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        (["serve"], ["serve", "--transport", "stdio"]),
        (["index-sample", "C4817", "C2991"], ["index-sample", "C4817", "C2991"]),
        ([], None),
        (["index-sample"], None),
        (["index-sample", "bad"], None),
        (["other"], None),
        (["other", "C4817"], None),
        (["index-sample", "C4817"], ["index-sample", "C4817"]),
    ],
)
def test_imported_launcher_dispatch_and_refusal(
    monkeypatch, callable_launcher, arguments, expected
):
    seen = []
    expected_exit = 17

    def dispatched():
        seen.append(sys.argv.copy())
        return expected_exit

    monkeypatch.setattr(cli, "main", dispatched)
    monkeypatch.setattr(sys, "argv", ["launcher", *arguments])
    if expected is None:
        with pytest.raises(ValueError) as error:
            callable_launcher()
        assert str(error.value) == "controlled live launcher requires a furnished operation"
        assert seen == []
    else:
        assert callable_launcher() == expected_exit
        assert seen == [["nci-si-mcp", *expected]]


def test_imported_launcher_transport_never_proxies_or_forwards_redirects(
    monkeypatch, callable_launcher, redirect_receivers
):
    expected_exit = 23
    selected, forbidden = redirect_receivers
    with FixtureServer(FixtureSet({}, {})) as proxy:
        monkeypatch.setattr(urllib.request, "getproxies", lambda: {"http": proxy.url})
        monkeypatch.setattr(urllib.request, "proxy_bypass", lambda host: False)
        monkeypatch.setattr(http_client, "_OPENER", build_opener(ProxyHandler({"http": proxy.url})))
        monkeypatch.setattr(sys, "argv", ["launcher", "serve"])

        def transported():
            request = Request(  # noqa: S310 - owned loopback fixture URL.
                selected.base_url("evs") + "/api/v1/metadata/terminologies",
                headers={"X-EVSRESTAPI-License-Key": "private-marker"},
            )
            with pytest.raises(HTTPError) as error:
                http_client._OPENER.open(request, timeout=2)
            with error.value:
                assert proxy.log() == []
                assert forbidden.log() == []
                assert error.value.code == HTTPStatus.FOUND
            return expected_exit

        monkeypatch.setattr(cli, "main", transported)
        assert callable_launcher() == expected_exit
        received = selected.log()
        assert len(received) == 1
        assert received[0]["fixture"] is not None
        assert {key.lower(): value for key, value in received[0]["headers"].items()}[
            "x-evsrestapi-license-key"
        ] == "private-marker"


@pytest.mark.parametrize("exit_code", [0, 17])
def test_module_entry_propagates_the_cli_exit(monkeypatch, callable_launcher, exit_code):
    monkeypatch.setattr(sys, "argv", ["launcher", "serve"])
    seen = []
    monkeypatch.setattr(cli, "main", lambda: seen.append(sys.argv.copy()) or exit_code)
    monkeypatch.delitem(sys.modules, "nci_si_acceptance.live_launcher")
    with pytest.raises(SystemExit) as outcome:
        runpy.run_module("nci_si_acceptance.live_launcher", run_name="__main__")
    assert outcome.value.code == exit_code
    assert seen == [["nci-si-mcp", "serve", "--transport", "stdio"]]
