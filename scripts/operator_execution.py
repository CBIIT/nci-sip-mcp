"""Fixed job dispatch over a committed source snapshot, with independently retained evidence."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from scripts.benchmark_limits import approved_target
from scripts.operator_evidence import bind_original, finish_evidence
from scripts.operator_process import run_owned, watch_parent
from scripts.operator_source import archive_source
from scripts.operator_worker import clean_environment
from scripts.portal_jobs import PROFILES
from scripts.portal_store import EvidenceStore
from scripts.processes import exit_on_sigterm
from scripts.vocabulary import JobOutcome

ROOT = Path(__file__).resolve().parents[1]
REMOTE_TARGET = "NCI_SIP_OPERATOR_REMOTE_TARGET"
AUTHORIZATION = "NCI_SIP_BENCHMARK_AUTHORIZATION"


@dataclass(frozen=True)
class RemoteProbe:
    target: str
    authorization: str | None = field(repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "target", approved_target(self.target, (self.target,)))
        header = self.authorization
        if header is not None and not (header.isascii() and header.isprintable()):
            raise ValueError("Remote authorization must be a printable ASCII header")


def _remote_environment(profile: str, remote: RemoteProbe | None) -> dict[str, str]:
    if profile != "benchmark-http-remote":
        return {}
    if remote is None:
        raise ValueError("Remote probes require explicit startup configuration")
    environment = {REMOTE_TARGET: remote.target}
    if remote.authorization is not None:
        environment[AUTHORIZATION] = remote.authorization
    return environment


def execute_job(
    job: dict[str, Any],
    directory: Path,
    cancelled: threading.Event,
    *,
    store: EvidenceStore,
    remote: RemoteProbe | None = None,
) -> JobOutcome:
    settings = _remote_environment(job["profile"], remote)
    directory = directory.resolve()
    directory.mkdir(mode=0o700)
    command = [
        sys.executable,
        "-m",
        "scripts.operator_execution",
        "--directory",
        str(directory),
        "--commit",
        job["commit"],
        "--profile",
        job["profile"],
    ]
    seconds = 900 if job["profile"] == "acceptance-http-fixture" else 240
    try:
        result = run_owned(
            command,
            directory=directory,
            environment=clean_environment(directory) | settings,
            seconds=seconds,
            cancelled=cancelled,
        )
        return finish_evidence(job, directory, result, store)
    finally:
        _clean_scratch(directory)


def _clean_scratch(directory: Path) -> None:
    # A killed worker cannot run TemporaryDirectory cleanup; the parent owns these paths.
    paths = [directory / "source", *(directory / "bundle").glob("worker-*")]
    for path in paths:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)


def _worker_command(profile: str, output: Path) -> list[str]:
    if profile == "benchmark-http-remote":
        target = os.environ[REMOTE_TARGET]
        return [
            sys.executable,
            "-m",
            "scripts.benchmark_http_cli",
            "--allow-remote",
            "--target",
            target,
            "--allow-target",
            target,
            "--output",
            str(output),
        ]
    return [sys.executable, "-m", "scripts.operator_worker", profile, "--output", str(output)]


def _run(command: list[str], source: Path, environment: dict[str, str]) -> int:
    return subprocess.run(  # noqa: S603 - fixed inventory or reviewed worker command, no shell
        command,
        cwd=source,
        env=environment,
        check=False,
        stdin=subprocess.DEVNULL,  # output is inherited: the controller keeps it in worker.log
    ).returncode


def _perform(directory: Path, commit: str, profile: str) -> int:
    source = directory / "source"
    archive_source(ROOT, commit, source)
    environment = clean_environment(directory, source=source)
    kind = "acceptance" if profile == "acceptance-http-fixture" else "benchmark"
    mode = "live" if profile == "benchmark-http-remote" else "fixture"
    bundle = directory / "bundle"
    status = _run(
        [
            sys.executable,
            "-m",
            "scripts.operator_inventory",
            "--output",
            str(bundle),
            "--kind",
            kind,
            "--mode",
            mode,
        ],
        source,
        environment,
    )
    if status:
        return status
    bind_original(directory, commit)
    if profile == "benchmark-http-remote" and AUTHORIZATION in os.environ:
        environment[AUTHORIZATION] = os.environ[AUTHORIZATION]
    return _run(_worker_command(profile, bundle / "report.json"), source, environment)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--profile", choices=PROFILES, required=True)
    args = parser.parse_args()
    return _perform(args.directory, args.commit, args.profile)


if __name__ == "__main__":
    exit_on_sigterm()
    watch_parent()
    raise SystemExit(main())
