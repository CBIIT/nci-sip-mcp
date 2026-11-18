"""Exercise only owned local companion containers, including an isolated fixture job."""

from __future__ import annotations

import html
import json
import re
import subprocess
import sys
import uuid
from http import HTTPStatus
from http.client import HTTPConnection
from typing import Any

from scripts.processes import wait_until
from scripts.site_assets import STYLESHEETS, load_footer
from scripts.vocabulary import ACTIVE

COMPOSE = "container/compose.local.yaml"


def docker(*args: str, check: bool = True) -> str:
    result = subprocess.run(  # noqa: S603 - fixed commands and owned Docker resources, no shell
        ["docker", *args],  # noqa: S607 - local container engine executable
        capture_output=True,
        text=True,
        check=check,
        timeout=60,
    )
    return result.stdout.strip()


def docker_result(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed executable and owned diagnostic resources
        ["docker", *args],  # noqa: S607 - local container engine executable
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def request(port: int, path: str, body: str | None = None) -> tuple[int, str]:
    connection = HTTPConnection("127.0.0.1", port, timeout=5)
    headers = {
        "Origin": f"http://127.0.0.1:{port}",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    try:
        connection.request("GET" if body is None else "POST", path, body, headers)
        response = connection.getresponse()
        return response.status, response.read().decode()
    finally:
        connection.close()


def ready(port: int) -> None:
    def healthy() -> bool:
        try:
            return request(port, "/health")[0] == HTTPStatus.OK
        except OSError:
            return False  # Startup may not yet have bound its socket; the deadline remains fixed.

    if not wait_until(healthy, 45, interval=0.2):
        raise TimeoutError("Companion did not become healthy")


def portal_styling(port: int) -> None:
    """The portal page links its stylesheets, each is served, and the footer carries the contact
    link of the documentation configuration."""
    status, page = request(port, "/")
    require(status == HTTPStatus.OK, "Portal page unavailable")
    sheets = re.findall(r'<link rel="stylesheet" href="([^"]+)">', page)
    require(sheets == [f"/assets/{name}" for name in STYLESHEETS], "Portal stylesheets missing")
    for sheet in sheets:
        require(request(port, sheet)[0] == HTTPStatus.OK, f"Portal stylesheet {sheet} unavailable")
    contact = html.escape(load_footer().contact_url, quote=True)  # as the portal renders it
    require(f'href="{contact}"' in page, "Portal footer link missing")


def inspect_boundary(container: str) -> None:
    installer = docker(
        "exec",
        container,
        "python",
        "-c",
        "import importlib.util; print(importlib.util.find_spec('pip'))",
    )
    require(installer == "None", "Build-time package installer remains in runtime")
    record = json.loads(docker("inspect", container))[0]
    config = record["HostConfig"]
    require(config["ReadonlyRootfs"], "Companion root is writable")
    require(record["Config"]["User"] == "65532:65532", "Companion is not nonroot")
    require(config["Memory"] > 0 and config["PidsLimit"] > 0, "Missing resource limits")
    require(
        all(
            mount["Destination"] == "/state"
            for mount in record["Mounts"]
            if mount["Type"] != "tmpfs"
        ),
        "Unexpected companion mount",
    )
    networks = list(record["NetworkSettings"]["Networks"])
    require(len(networks) == 1, "Companion has an extra network")
    require(
        json.loads(docker("network", "inspect", networks[0]))[0]["Internal"],
        "Fixture network has upstream egress",
    )


def blocked_connectivity(container: str, target: str) -> None:
    script = (
        "import socket,sys; s=socket.socket(); s.settimeout(2); "
        "result=s.connect_ex((sys.argv[1],int(sys.argv[2]))); "
        "s.close(); print(result)"
    )
    result = docker("exec", container, "python", "-c", script, target, "8000")
    require(int(result) != 0, "Fixture worker can reach the separate serving MCP")
    result = docker("exec", container, "python", "-c", script, "1.1.1.1", "443")
    require(int(result) != 0, "Fixture worker can reach an external network")


def benchmark(admin: str) -> None:
    run_id = uuid.uuid4().hex
    status, _ = request(8081, "/jobs", f"run_id={run_id}&profile=benchmark-http-fixture")
    require(status == HTTPStatus.SEE_OTHER, "Fixture benchmark was not admitted")

    states: list[str] = []

    def finished() -> bool:
        value: dict[str, Any] = json.loads(
            docker("exec", admin, "cat", "/state/evidence.jobs/jobs.json")
        )
        states.append(next(row for row in value["jobs"] if row["run_id"] == run_id)["state"])
        return states[-1] not in ACTIVE

    if not wait_until(finished, 300, interval=1):
        raise TimeoutError("Container benchmark did not terminate within its smoke bound")
    require(states[-1] == "completed", f"Fixture benchmark ended as {states[-1]}")
    status, page = request(8081, "/runs/" + run_id)
    require(
        status == HTTPStatus.OK and "cold" in page.lower() and "warm" in page.lower(),
        "Benchmark results are unavailable",
    )


def _serving_probe(network: str, name: str) -> tuple[str, int]:
    docker("network", "create", network)
    docker(
        "run",
        "-d",
        "--name",
        name,
        "--network",
        network,
        "--read-only",
        "--tmpfs",
        "/tmp:rw,size=64m,mode=1777",  # noqa: S108 - private tmpfs in the owned container
        "--memory",
        "512m",
        "--pids-limit",
        "64",
        "-p",
        "127.0.0.1::8000",
        "--entrypoint",
        "python",
        "-e",
        "NCI_SIP_DATA_DIR=/tmp/data",
        "-e",
        "NCI_SIP_EMBEDDING_PROVIDER=hashing",
        "-e",
        "NCI_SIP_TRANSPORT=streamable-http",
        "-e",
        "NCI_SIP_HTTP_HOST=0.0.0.0",
        "-e",
        "NCI_SIP_HTTP_AUTH_MODE=trusted-local",
        "nci-si-admin:local",
        "-m",
        "nci_sip_mcp.cli",
        "serve",
    )
    record = json.loads(docker("inspect", name))[0]
    port = int(record["NetworkSettings"]["Ports"]["8000/tcp"][0]["HostPort"])
    return record["NetworkSettings"]["Networks"][network]["IPAddress"], port


def _remove_owned(kind: str, name: str) -> None:
    options = ("--all",) if kind == "container" else ()
    found = docker(kind, "ls", *options, "--quiet", "--filter", f"name=^{name}$")
    if found:
        force = ("--force",) if kind == "container" else ()
        docker(kind, "rm", *force, name)


def cleanup(compose: tuple[str, ...], serving: str, network: str) -> list[Exception]:
    errors: list[Exception] = []
    operations = (
        lambda: docker(*compose, "down", "--volumes"),
        lambda: _remove_owned("container", serving),
        lambda: _remove_owned("network", network),
    )
    for operation in operations:
        try:
            operation()
        except (OSError, subprocess.SubprocessError) as error:
            errors.append(error)
    return errors


def _diagnostics(compose: tuple[str, ...]) -> list[Exception]:
    errors: list[Exception] = []
    for arguments in (("ps", "--all"), ("logs", "--no-color", "--tail", "30")):
        try:
            result = docker_result(*compose, *arguments)
        except (OSError, subprocess.SubprocessError) as error:
            errors.append(error)
            continue
        print(result.stdout.strip())
        if result.returncode:
            errors.append(
                RuntimeError(
                    f"docker {' '.join(arguments)} failed with status {result.returncode}: "
                    f"{result.stderr.strip()}"
                )
            )
    return errors


def _finish(compose: tuple[str, ...], serving: str, network: str) -> None:
    primary = sys.exception()
    errors = _diagnostics(compose) if primary is not None else []
    errors.extend(cleanup(compose, serving, network))
    if primary is not None:
        for error in errors:
            primary.add_note(f"Cleanup or diagnostics also failed: {error!r}")
    elif errors:
        raise ExceptionGroup("Companion cleanup failed", errors)


def verify_plans(compose: tuple[str, ...]) -> None:
    scenarios = (
        ((), []),
        (("docs",), ["documentation"]),
        (("companion",), ["administration", "admin-ingress"]),
        (("docs", "companion"), ["administration", "admin-ingress", "documentation"]),
        (("mcp",), ["mcp"]),
    )
    for profiles, expected in scenarios:
        selected = tuple(option for profile in profiles for option in ("--profile", profile))
        services = docker(*compose, *selected, "config", "--services").splitlines()
        require(
            sorted(services) == sorted(expected),
            f"Profiles {profiles or ('none',)} selected {services}",
        )


def remove_component(compose: tuple[str, ...], *services: str) -> None:
    docker(*compose, "stop", *services)
    docker(*compose, "rm", "-f", *services)


def main() -> None:
    project = "nci-si-smoke-" + uuid.uuid4().hex[:10]
    compose = ("compose", "-f", COMPOSE, "-p", project)
    serving = project + "-serving"
    network = project + "-probe"
    try:
        verify_plans(compose)

        docker(*compose, "--profile", "docs", "up", "-d")
        ready(8080)
        require("National Cancer Institute" in request(8080, "/")[1], "Docs identity missing")
        remove_component(compose, "documentation")

        docker(*compose, "--profile", "companion", "up", "-d")
        ready(8081)
        portal_styling(8081)
        admin = docker(*compose, "ps", "-q", "administration")
        inspect_boundary(admin)
        address, port = _serving_probe(network, serving)
        ready(port)
        blocked_connectivity(admin, address)
        benchmark(admin)
        remove_component(compose, "admin-ingress", "administration")
        require(
            request(port, "/health")[0] == HTTPStatus.OK, "Stopping admin stopped the serving MCP"
        )

        docker(*compose, "--profile", "docs", "--profile", "companion", "up", "-d")
        ready(8080)
        ready(8081)
        remove_component(compose, "documentation")
        require(request(8081, "/health")[0] == HTTPStatus.OK, "Stopping docs stopped companion")
        docker(*compose, "--profile", "docs", "up", "-d", "documentation")
        ready(8080)
        remove_component(compose, "admin-ingress", "administration")
        require(request(8080, "/health")[0] == HTTPStatus.OK, "Stopping companion stopped docs")
    finally:
        _finish(compose, serving, network)
    print("Profile plans, health, isolation, fixture benchmark and independent shutdown passed")


if __name__ == "__main__":
    main()
