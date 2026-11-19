"""The server the harness owns is ready when `start` returns and gone when its scope ends."""

import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from nci_sip_acceptance.client import OwnedServer, server_environment

# The fixture upstream is never contacted: the tests only start and stop the server.
UPSTREAM = "http://127.0.0.1:9"


def listening(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


def owned(directory: Path, **options) -> OwnedServer:
    return OwnedServer(
        server_environment("fixture", directory / "data", UPSTREAM),
        directory / "server.log",
        data_dir=directory / "data",
        require_index=False,
        **options,
    )


def settings(directory: Path) -> dict[str, str]:
    environment = server_environment("fixture", directory / "data", UPSTREAM)
    return {key: value for key, value in environment.items() if key.startswith("NCI_SIP_")}


def test_owned_server_is_reaped_when_the_scope_raises(tmp_path):
    with pytest.raises(KeyError), owned(tmp_path) as server:
        server.start(settings(tmp_path))
        assert listening(server.port)
        raise KeyError
    assert not listening(server.port)


def test_restart_serves_again_and_stop_is_final(tmp_path):
    with owned(tmp_path) as server:
        server.start(settings(tmp_path))
        server.restart(settings(tmp_path))
        assert listening(server.port)
        server.stop()
        assert not listening(server.port)
        server.stop()


def test_a_server_that_exits_before_readiness_fails_naming_its_log(tmp_path):
    with owned(tmp_path) as server:
        with pytest.raises(
            RuntimeError, match=f"exited before readiness; see {tmp_path}/server.log"
        ):
            server.start(settings(tmp_path) | {"NCI_SIP_LOG_LEVEL": "not-a-level"})
        assert not listening(server.port)
    assert (tmp_path / "server.log").read_text()


def test_a_server_not_ready_in_time_is_reaped_and_reported(tmp_path):
    with owned(tmp_path, ready_seconds=0) as server:
        with pytest.raises(TimeoutError, match=f"did not become ready; see {tmp_path}/server.log"):
            server.start(settings(tmp_path))
        assert not listening(server.port)


def test_stop_kills_a_server_that_ignores_termination(tmp_path, monkeypatch):
    # sitecustomize runs in the child before the server: it records the child's pid and makes
    # SIGTERM stick, even against the server's own handler installation.
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    (hooks / "sitecustomize.py").write_text(
        "import os, signal\n"
        f"open({str(tmp_path / 'child.pid')!r}, 'w').write(str(os.getpid()))\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "signal.signal = lambda *_arguments: None\n"
    )
    monkeypatch.setattr("nci_sip_acceptance.client.STOP_SECONDS", 1)
    environment = server_environment("fixture", tmp_path / "data", UPSTREAM) | {
        "PYTHONPATH": str(hooks)
    }
    server = OwnedServer(
        environment, tmp_path / "server.log", data_dir=tmp_path / "data", require_index=False
    )
    server.start(settings(tmp_path))
    pid = int((tmp_path / "child.pid").read_text())
    server.stop()
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_a_second_start_is_refused_and_the_first_server_stays_owned(tmp_path):
    with owned(tmp_path) as server:
        server.start(settings(tmp_path))
        with pytest.raises(RuntimeError, match="already running"):
            server.start(settings(tmp_path))
        assert listening(server.port)
    assert not listening(server.port)


def test_a_foreign_listener_on_the_port_is_not_taken_for_the_server(tmp_path):
    with owned(tmp_path) as server, socket.socket() as foreign:
        foreign.bind(("127.0.0.1", server.port))
        foreign.listen()
        expected = f"^The owned server's port {server.port} is already in use by another process$"
        with pytest.raises(RuntimeError, match=expected):
            server.start(settings(tmp_path))


LISTENS = (
    "import os, socket, time; listener = socket.socket(); "
    "listener.bind(('127.0.0.1', int(os.environ['NCI_SIP_HTTP_PORT']))); "
    "listener.listen(); time.sleep(300)"
)


def stand_in_children(monkeypatch, script):
    """Replace the server child by `script`; returns the environments and processes it got."""
    spawn = subprocess.Popen
    launched = []

    def launch(_command, **options):
        process = spawn([sys.executable, "-c", script], **options)
        launched.append((options["env"], process))
        return process

    monkeypatch.setattr("nci_sip_acceptance.client.subprocess.Popen", launch)
    return launched


@pytest.mark.parametrize("required", [False, True])
def test_the_child_is_told_whether_an_index_is_required(tmp_path, monkeypatch, required):
    launched = stand_in_children(monkeypatch, LISTENS)
    server = OwnedServer(
        {}, tmp_path / "server.log", data_dir=tmp_path / "data", require_index=required
    )
    with server:
        server.start({})
    assert launched[0][0]["NCI_SIP_HTTP_REQUIRE_INDEX"] == ("1" if required else "0")


def test_an_ambient_server_setting_does_not_reach_the_child(tmp_path, monkeypatch):
    launched = stand_in_children(monkeypatch, LISTENS)
    ambient = {"PATH": "/bin", "NCI_SIP_LOG_LEVEL": "ambient"}
    with OwnedServer(
        ambient, tmp_path / "server.log", data_dir=tmp_path / "data", require_index=False
    ) as server:
        server.start({})
    assert "NCI_SIP_LOG_LEVEL" not in launched[0][0]
    assert launched[0][0]["PATH"] == "/bin"


def test_a_failed_start_outside_a_scope_still_reaps_the_child(tmp_path, monkeypatch):
    launched = stand_in_children(monkeypatch, "import time; time.sleep(300)")
    server = OwnedServer(
        {},
        tmp_path / "server.log",
        data_dir=tmp_path / "data",
        require_index=False,
        ready_seconds=0,
    )
    with pytest.raises(TimeoutError):
        server.start({})
    assert launched[0][1].poll() is not None
