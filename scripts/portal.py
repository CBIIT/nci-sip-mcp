"""Import bounded local evidence and serve the loopback validation companion."""

from __future__ import annotations

import argparse
import os
from contextlib import closing, suppress
from functools import partial
from pathlib import Path

from scripts.operator_execution import AUTHORIZATION, ROOT, RemoteProbe, execute_job
from scripts.operator_source import head_commit
from scripts.portal_configuration import LocalConfiguration
from scripts.portal_http import create_server
from scripts.portal_jobs import JobController
from scripts.portal_store import MAX_BUNDLE_BYTES, EvidenceStore, load_bundle

from nci_sip_mcp.config import configure_logging


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", type=Path, default=Path(".nci-si-portal/evidence.sqlite"))
    parser.add_argument("--retention", type=int, default=100)
    commands = parser.add_subparsers(dest="command", required=True)
    imported = commands.add_parser("import", help="Import a bound original evidence bundle")
    imported.add_argument("directory", type=Path)
    legacy = commands.add_parser("legacy", help="Retain old bytes as unverified, without verdicts")
    legacy.add_argument("report", type=Path)
    legacy.add_argument("--kind", choices=("acceptance", "benchmark"), required=True)
    serve = commands.add_parser(
        "serve", help="Serve only on 127.0.0.1; no login is required locally"
    )
    serve.add_argument("--port", type=int, default=8081)
    serve.add_argument("--allow-remote", action="store_true")
    serve.add_argument("--remote-target", help="Explicitly allowed exact HTTPS MCP endpoint")
    serve.add_argument(
        "--configuration-snapshot",
        type=Path,
        help="Selected local target startup evidence; not live telemetry",
    )
    return parser


def _legacy(store: EvidenceStore, path: Path, kind: str) -> str:
    if path.is_symlink():
        raise ValueError("Legacy evidence cannot be a symlink")
    with path.open("rb") as stream:
        raw = stream.read(MAX_BUNDLE_BYTES + 1)
    return store.import_legacy(raw, kind)


def main() -> None:
    parser = _parser()
    args = parser.parse_args()
    store = EvidenceStore(args.store, retention=args.retention)
    if args.command == "import":
        print(store.import_bundle(load_bundle(args.directory)))
    elif args.command == "legacy":
        print(_legacy(store, args.report, args.kind))
    else:
        remote = _remote(parser, args)
        configure_logging("INFO")
        serve_portal(
            store,
            args.port,
            retention=args.retention,
            remote=remote,
            configuration=LocalConfiguration(args.configuration_snapshot),
        )


def _remote(parser: argparse.ArgumentParser, args: argparse.Namespace) -> RemoteProbe | None:
    if args.allow_remote != bool(args.remote_target):
        parser.error("Remote probes require both --allow-remote and --remote-target")
    if not args.allow_remote:
        return None
    try:
        return RemoteProbe(args.remote_target, os.environ.get(AUTHORIZATION))
    except ValueError:
        parser.error("Remote probes require an exact HTTPS endpoint and a valid header")


def serve_portal(
    store: EvidenceStore,
    port: int,
    *,
    retention: int,
    remote: RemoteProbe | None,
    configuration: LocalConfiguration,
    host: str = "127.0.0.1",
) -> None:
    controller = JobController(
        store.path.with_suffix(".jobs"),
        execute=partial(execute_job, store=store, remote=remote),
        commit=partial(head_commit, ROOT),
        remote=remote is not None,
        retention=retention,
    )
    with (
        closing(controller),
        create_server(
            store, host=host, port=port, jobs=controller, configuration=configuration
        ) as server,
    ):
        print(f"Local validation: http://127.0.0.1:{server.server_port}/", flush=True)
        # Local Ctrl+C stops owned workers and closes both listener and ownership lease.
        with suppress(KeyboardInterrupt):
            server.serve_forever()


if __name__ == "__main__":
    main()
