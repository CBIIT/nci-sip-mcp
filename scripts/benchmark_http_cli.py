"""Explicit, bounded, read-only remote HTTP probes using furnished representative calls."""

import argparse
import asyncio
import os
from pathlib import Path

from scripts.benchmark import CASES, cases
from scripts.benchmark_http import run_http
from scripts.benchmark_limits import DEFAULT_MAX_REQUESTS, Limits, approved_target
from scripts.processes import exit_on_sigterm


def arguments() -> tuple[argparse.Namespace, Limits, str]:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", required=True)
    parser.add_argument("--allow-target", action="append", required=True)
    parser.add_argument("--allow-remote", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--case", choices=CASES, action="append", dest="names")
    parser.add_argument("--repetitions", type=int, default=5)
    parser.add_argument("--warmups", type=int, default=1)
    parser.add_argument("--max-requests", type=int, default=DEFAULT_MAX_REQUESTS)
    parser.add_argument("--seconds", type=float, default=120)
    parser.add_argument("--timeout", type=float, default=15)
    args = parser.parse_args()
    if not args.allow_remote:
        parser.error("Remote read-only probes require explicit operator opt-in (--allow-remote)")
    try:
        limits = Limits(
            args.max_requests, args.seconds, args.timeout, args.repetitions, args.warmups
        )
        target = approved_target(args.target, args.allow_target)
    except ValueError:
        parser.error(
            "Invalid campaign limits or HTTPS allowlist target; "
            "credentials belong in the environment"
        )
    return args, limits, target


def main() -> int:
    args, limits, target = arguments()
    report = asyncio.run(
        run_http(
            target,
            args.allow_target,
            cases(args.names or list(CASES)),
            limits,
            args.output,
            allow_remote=True,
            authorization=os.environ.get("NCI_SIP_BENCHMARK_AUTHORIZATION"),
        )
    )
    return 0 if report["complete"] else 1


if __name__ == "__main__":
    exit_on_sigterm()
    raise SystemExit(main())
