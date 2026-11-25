"""Exercise the built amd64 image, owning and reaping every container it starts."""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import socket
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from mcp.client import Client
from scripts.processes import wait_until

from nci_sip_mcp.registry import SPECS

ROOT = Path(__file__).resolve().parents[1]
DOCKER = shutil.which("docker") or "docker"
MCP_TOOLS = {spec.name for spec in SPECS if spec.name}
STARTUP_SECONDS = 180


def docker(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    # Fixed executable and argument list, never a shell or caller-supplied command.
    try:
        return subprocess.run(  # noqa: S603
            [DOCKER, *args], check=check, capture_output=True, text=True, timeout=300
        )
    except subprocess.CalledProcessError as error:
        # CalledProcessError's own text omits what docker said, the part that explains it.
        raise RuntimeError(
            f"docker {args[0]} failed (exit {error.returncode}): {error.stderr.strip()}"
        ) from error


def reap(image: str, assets: Path) -> str | None:
    """Remove the files the containers created; the failure, if any, as a message."""
    try:
        docker(
            *mounted(image, assets, "--network", "none", "--entrypoint", "python"),
            "-c",
            "import shutil; from pathlib import Path; "
            "[shutil.rmtree(p) if p.is_dir() else p.unlink() "
            "for p in Path('/assets').iterdir()]",
        )
    except (RuntimeError, subprocess.TimeoutExpired) as error:
        return f"Cleanup of the smoke assets failed: {error}"
    return None


def mounted(image: str, assets: Path, *args: str) -> list[str]:
    return [
        "run",
        "--rm",
        "--platform",
        "linux/amd64",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid",  # noqa: S108 - private tmpfs inside the disposable container
        "-v",
        f"{assets}:/assets",
        "-e",
        "NCI_SIP_DATA_DIR=/assets/data",
        "-e",
        "NCI_SIP_EMBEDDING_MODEL=/assets/model",
        "-e",
        "NCI_SIP_HTTP_AUTH_MODE=trusted-local",
        *args,
        image,
    ]


def prepare(image: str, assets: Path) -> None:
    result = docker(
        *mounted(
            image,
            assets,
            "--network",
            "none",
            "--entrypoint",
            "python",
            "-v",
            f"{ROOT / 'container/smoke_assets.py'}:/prepare.py:ro",
            "-v",
            f"{ROOT / 'acceptance/fixtures/recorded/evs/concepts/C3262.json'}:/concept.json:ro",
        ),
        "/prepare.py",
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Offline fixture preparation failed: {result.stderr}")
    for line in result.stderr.splitlines():
        json.loads(line)


def failure(image: str, assets: Path, expected: str, *extra: str) -> None:
    result = docker(*mounted(image, assets, "--network", "none", *extra), check=False)
    records = [json.loads(line) for line in result.stderr.splitlines() if line.startswith("{")]
    outcome = result.returncode, [(r.get("event"), r.get("asset")) for r in records]
    if outcome != (1, [("container_startup_failed", expected)]):
        raise RuntimeError(f"Expected one {expected} startup error: {result.stderr}")


def request(port: int, path: str) -> dict:
    url = f"http://127.0.0.1:{port}{path}"
    with urllib.request.urlopen(url, timeout=5) as response:
        return json.load(response)


def default_auth_refusal(image: str, assets: Path) -> None:
    name = "nci-si-auth-" + uuid.uuid4().hex
    command = mounted(image, assets, "--name", name, "--network", "none")
    explicit = command.index("NCI_SIP_HTTP_AUTH_MODE=trusted-local")
    del command[explicit - 1 : explicit + 1]  # Exercise the image default, not the smoke opt-out.
    try:
        result = docker(*command, check=False)
        if result.returncode == 0 or "Required HTTP authentication needs" not in result.stderr:
            raise RuntimeError("Image did not refuse startup without its auth integration")
    finally:
        docker("rm", "-f", name, check=False)


def wait_healthy(name: str) -> None:
    def healthy() -> bool:
        status = docker("inspect", "--format", "{{.State.Health.Status}}", name).stdout.strip()
        if status not in {"healthy", "starting"}:
            raise RuntimeError(f"Docker health is {status}, not healthy")
        return status == "healthy"

    if not wait_until(healthy, STARTUP_SECONDS, interval=1):
        raise RuntimeError(f"Docker health did not become healthy within {STARTUP_SECONDS} seconds")


def wait_ready(port: int) -> None:
    def ready() -> bool:
        try:
            return request(port, "/ready") == {"status": "ready"}
        except urllib.error.URLError, TimeoutError, ConnectionError:
            return False  # A bounded startup poll; failure is reported below, never ready.

    if not wait_until(ready, STARTUP_SECONDS, interval=1):
        raise RuntimeError(f"Container never became ready within {STARTUP_SECONDS} seconds")


async def surface(port: int, assets: Path) -> None:
    async with Client(f"http://127.0.0.1:{port}/mcp") as client:
        tools = await client.list_tools()
        if {tool.name for tool in tools.tools} != MCP_TOOLS:
            raise RuntimeError("The container did not expose the unified profile")
        manifest = json.loads((assets / "manifest.json").read_text())
        if not str(manifest.get("embedding_revision")).startswith("sha256:"):
            raise RuntimeError("The directory model was not identified by its content hash")
        release = manifest["release_version"]
        resource = await client.read_resource(f"ncit://index/manifest/{release}")
        content = json.loads(resource.model_dump(mode="json")["contents"][0]["text"])
        if (content["version"], content["concepts"]) != (release, 1):
            raise RuntimeError("HTTP returned another index manifest")
        reply = await client.call_tool(
            "get_concept",
            {
                "terminology": "ncit",
                "code": "not-a-code",
                "release": release,
            },
        )
        error = (reply.structured_content or {}).get("error", {})
        if error.get("code") != "invalid_request":
            raise RuntimeError("HTTP tool validation did not fail before upstream I/O")


def serve(image: str, assets: Path) -> None:
    name = "nci-si-smoke-" + uuid.uuid4().hex
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    try:
        command = mounted(image, assets, "--name", name, "-d", "-p", f"127.0.0.1:{port}:8000")
        command.remove("--rm")
        docker(*command)
        wait_ready(port)
        if request(port, "/health") != {"status": "ok"}:
            raise RuntimeError("Health endpoint failed")
        asyncio.run(surface(port, assets))
        wait_healthy(name)
        docker("stop", "--time", "20", name)
        if docker("inspect", "--format", "{{.State.ExitCode}}", name).stdout.strip() != "0":
            raise RuntimeError("Container did not stop gracefully")
        logs = docker("logs", name)
        for line in (logs.stdout + logs.stderr).splitlines():
            json.loads(line)
    finally:
        try:
            logs = docker("logs", name, check=False)
            (ROOT / "tmp/container-smoke.log").write_text(logs.stdout + logs.stderr)
        finally:
            docker("rm", "-f", name, check=False)


def retained_builds(image: str, assets: Path) -> None:
    code = (
        "import json; from pathlib import Path; from ncit_index.index import LocalIndex; "
        "print(json.dumps([b.build_id for b in LocalIndex(Path('/assets/data')).list_builds()]))"
    )
    result = docker(
        *mounted(image, assets, "--network", "none", "--entrypoint", "python"), "-c", code
    )
    if json.loads(result.stdout) != json.loads((assets / "builds.json").read_text()):
        raise RuntimeError("Serving changed the active build or its predecessor")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image")
    args = parser.parse_args()
    (ROOT / "tmp").mkdir(exist_ok=True)
    # Removal errors are ignored: a root-owned leftover must not replace the step failure, and
    # reap() has already reported why it could not remove it.
    with tempfile.TemporaryDirectory(
        prefix="container-smoke-", dir=ROOT / "tmp", ignore_cleanup_errors=True
    ) as directory:
        assets = Path(directory)
        assets.chmod(0o777)  # Let the unprivileged image UID create and reap scratch assets.
        completed = False
        try:
            failure(args.image, assets, "index")
            prepare(args.image, assets)
            default_auth_refusal(args.image, assets)
            failure(args.image, assets, "index", "-e", "NCI_SIP_EMBEDDING_MODEL=wrong-model")
            serve(args.image, assets)
            retained_builds(args.image, assets)
            (assets / "model").rename(assets / "held-model")
            failure(args.image, assets, "model")
            completed = True
        finally:
            # Native Linux preserves container ownership on bind mounts. Reap our files
            # as their creating UID before the host removes its temporary directory. A
            # failure here is reported but never replaces the error that got us here.
            cleanup_failure = reap(args.image, assets)
            if cleanup_failure and not completed:
                print(cleanup_failure, file=sys.stderr)
    if cleanup_failure:
        raise RuntimeError(cleanup_failure)
    print("Image smoke passed: offline external model/index, HTTP, missing assets, graceful stop")


if __name__ == "__main__":
    main()
