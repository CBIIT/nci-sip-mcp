"""Run the furnished server through the remote fixture suite, with owned process cleanup.

The harness invokes this file's `state` command as its normal operator state hook. A private
Unix socket hands the fixture settings to the parent, which owns and reaps the server process.
No PID files, detached children or production services are involved.
"""

from __future__ import annotations

import json
import os
import shlex
import socket
import socketserver
import subprocess
import sys
from contextlib import chdir
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from typing import Any

import yaml
from scripts.processes import exit_on_sigterm, print_server_log_tail, stop

from nci_sip_acceptance.client import OwnedServer, server_environment, without_nci_sip_settings
from nci_sip_acceptance.fixture_server import FixtureServer, load_fixtures
from nci_sip_acceptance.report import load_report
from nci_sip_acceptance.suite import index_set, unmatched_requests

ROOT = Path(__file__).resolve().parents[1]
ACCEPTANCE = ROOT / "acceptance"
FIXTURES = ACCEPTANCE / "fixtures"
REPORT = ACCEPTANCE / "http-fixture.json"
# The remote contract deliberately cannot remove the operator's prepared index.
UNPREPARED = {
    f"tests/test_evs.py::{function}[{argument}]"
    for function, arguments in (
        ("test_an_index_mode_without_an_index_is_unavailable", ("hybrid", "semantic")),
        (
            "test_an_index_mode_for_a_terminology_without_an_index_is_invalid",
            ("none-hybrid", "none-semantic"),
        ),
    )
    for argument in arguments
}


def hook_handler(server: OwnedServer) -> type[socketserver.StreamRequestHandler]:
    class Handler(socketserver.StreamRequestHandler):
        def handle(self) -> None:
            settings = json.loads(self.rfile.readline())
            try:
                server.restart(settings)
            except (RuntimeError, TimeoutError) as error:
                self.wfile.write(f"{error}\n".encode())
            else:
                self.wfile.write(b"ok\n")

    return Handler


def state(socket_path: str) -> None:
    settings = {
        name: value
        for name, value in os.environ.items()
        if name.startswith("NCI_SIP_") and not name.startswith("NCI_SIP_ACCEPTANCE_")
    }
    with socket.socket(socket.AF_UNIX) as connection:
        connection.settimeout(30)
        path = Path(socket_path).resolve()
        # This hook is a dedicated child; short addresses avoid AF_UNIX's path limit.
        with chdir(path.parent):
            connection.connect(path.name)
        connection.sendall(json.dumps(settings).encode() + b"\n")
        with connection.makefile("rb") as replies:
            reply = replies.readline()
        if reply != b"ok\n":
            raise RuntimeError(f"The fixture server restart failed: {reply.decode().strip()}")


def prepare(directory: Path, *, environment: dict[str, str] | None = None) -> None:
    manifest = yaml.safe_load((FIXTURES / "manifest.yaml").read_text())
    with FixtureServer(load_fixtures(FIXTURES)) as upstream:
        command = [sys.executable, "-m", "nci_sip_mcp.cli", "index-sample", *index_set(manifest)]
        settings = server_environment("fixture", directory / "index", upstream.url)
        if environment is not None:
            settings = environment | {
                key: value for key, value in settings.items() if key.startswith("NCI_SIP_")
            }
        subprocess.run(  # noqa: S603 - fixture preparation with the project interpreter
            command,
            env=settings,
            check=True,
            stdout=subprocess.DEVNULL,
            timeout=120,
        )
        if unmatched_requests(upstream.log()):
            raise RuntimeError("Index preparation requested an unrecorded upstream response")


NO_USABLE_RUN = 2


def report_skips(tests: dict[str, Any]) -> None:
    skipped = {name for name, row in tests.items() if row["outcome"] == "skipped"}
    print(f"Remote fixture run: {len(tests) - len(skipped)} executed, {len(skipped)} skipped.")
    for name in sorted(skipped):
        print(f"Skipped (remote unprepared contract): {name}")


def _outcome_differences(tests: dict[str, Any], expected: set[str]) -> list[str]:
    observed = {name: row["outcome"] for name, row in tests.items()}
    wanted = {name: "skipped" if name in UNPREPARED else "passed" for name in expected}
    return [
        f"{name}: expected {wanted.get(name, 'absent')}, observed {observed.get(name, 'missing')}"
        for name in sorted(observed.keys() | wanted.keys())
        if observed.get(name) != wanted.get(name)
    ]


def differences(report: dict[str, Any], expected: set[str]) -> list[str]:
    """One line per way the report differs from the expected remote fixture run."""
    lines = _outcome_differences(report["tests"], expected)
    if report["transport"] != "streamable-http":
        lines.append(f"transport: expected streamable-http, observed {report['transport']}")
    if report["failed_gates"]:
        lines.append("failed gates: " + ", ".join(report["failed_gates"]))
    return lines


def run_suite(
    port: int,
    socket_path: Path,
    *,
    report: Path | None = None,
    environment: dict[str, str] | None = None,
) -> int:
    output = REPORT if report is None else report
    command = shlex.join(
        [sys.executable, "-m", "scripts.acceptance_http", "state", str(socket_path)]
    )
    base = without_nci_sip_settings(os.environ if environment is None else environment)
    environment = base | {
        "NCI_SIP_ACCEPTANCE_URL": f"http://127.0.0.1:{port}/mcp",
        "NCI_SIP_ACCEPTANCE_MODE": "fixture",
        "NCI_SIP_ACCEPTANCE_PREPARED": "1",
        "NCI_SIP_ACCEPTANCE_REGISTRY_RELEASE": "required",
        "NCI_SIP_ACCEPTANCE_SECURITY_SERVER": shlex.join(
            [sys.executable, str(ROOT / "scripts/permissions_fixture.py")]
        ),
        "NCI_SIP_ACCEPTANCE_STATE_HOOK": command,
        # The hook runs `-m scripts.acceptance_http` from the suite's directory.
        "PYTHONPATH": os.pathsep.join(filter(None, [str(ROOT), base.get("PYTHONPATH")])),
    }
    output.unlink(missing_ok=True)
    suite = subprocess.Popen(  # noqa: S603 - the existing suite in this checkout
        [sys.executable, "-m", "pytest", "tests", "-q", "-rs", "--report=" + str(output)],
        env=environment,
        cwd=ACCEPTANCE,
    )
    try:
        status = suite.wait()
    finally:
        stop(suite)
    return _judge(status, output)


def _judge(status: int, output: Path) -> int:
    """0 for the expected run; 1 for a run that differs from it; 2 for no usable run."""
    if not output.exists():
        return _failed(NO_USABLE_RUN, f"The suite exited {status} and wrote no report.")
    if status not in (0, 1):
        return _failed(NO_USABLE_RUN, f"The suite exited {status} without a usable run.")
    try:
        observed = load_report(output, "fixture")
    except SystemExit as error:
        return _failed(NO_USABLE_RUN, str(error))
    report_skips(observed["tests"])
    expected = set(json.loads((ACCEPTANCE / "expected/fixture.json").read_text()))
    if lines := differences(observed, expected):
        return _failed(1, "\n".join(lines))
    if status:
        return _failed(1, "The suite exited 1 although every outcome is the expected one.")
    return 0


def _failed(code: int, reason: str) -> int:
    print(reason, file=sys.stderr)
    return code


def run(*, report: Path | None = None, environment: dict[str, str] | None = None) -> int:
    (ROOT / "tmp").mkdir(exist_ok=True)
    with TemporaryDirectory(prefix="http-", dir=ROOT / "tmp") as temporary:
        directory = Path(temporary)
        status = 1  # an exception or SIGTERM ends the run unsuccessfully too
        try:
            status = _serve_and_run(directory, report, environment)
            return status
        finally:
            if status:
                print_server_log_tail(directory / "server.log")


def _serve_and_run(directory: Path, report: Path | None, environment: dict[str, str] | None) -> int:
    prepare(directory, environment=environment)
    socket_path = directory / "control"
    # The runner owns the server. A restart answers the hook within its 30 s.
    with OwnedServer(
        os.environ if environment is None else environment,
        directory / "server.log",
        data_dir=directory / "index",
        require_index=True,
        ready_seconds=25,
    ) as server:
        # Restore cwd before starting the control thread.
        with chdir(directory):
            control = socketserver.UnixStreamServer("control", hook_handler(server))
        with control:
            thread = Thread(target=control.serve_forever, daemon=True)
            thread.start()
            try:
                return run_suite(server.port, socket_path, report=report, environment=environment)
            finally:
                control.shutdown()
                thread.join()


if __name__ == "__main__":
    if sys.argv[1:] and sys.argv[1] == "state":
        state(sys.argv[2])
    else:
        exit_on_sigterm()
        raise SystemExit(run())
