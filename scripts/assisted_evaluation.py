"""Measure preregistered fixture tasks through real MCP with no model/provider execution."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import platform
import subprocess
from contextlib import redirect_stderr
from importlib.metadata import version
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter, time
from typing import Any

import yaml
from mcp.client import Client
from mcp.shared.exceptions import MCPError
from scripts.assisted_scoring import assess, summarize, validate_plan

from nci_sip_acceptance.fixture_server import FixtureServer, load_fixtures
from nci_sip_acceptance.suite import unmatched_requests
from nci_sip_mcp.config import Settings
from nci_sip_mcp.context import Context
from nci_sip_mcp.permissions import Authority, Principal
from nci_sip_mcp.registry import SPECS
from nci_sip_mcp.server import create_mcp

ROOT = Path(__file__).resolve().parents[1]
TASKS = ROOT / "evaluation/assisted-tasks.yaml"
ARMS = ("deterministic", "scripted-client")


def planned_runs(plan: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "task": task["id"],
            "split": task["split"],
            "arm": arm,
            "repetition": repeat,
            "condition": "first-in-session" if repeat == 0 else "warm-session",
            "status": "incomplete",
        }
        for task in plan["tasks"]
        for arm in ARMS
        for repeat in range(plan["repetitions"])
    ]


async def execute(client: Any, task: dict[str, Any], arm: str, deadline: float) -> dict[str, Any]:
    start = perf_counter()
    record: dict[str, Any] = {"status": "completed", "results": [], "protocol_errors": []}
    try:
        # A JSON round trip is the deterministic planner double, not model inference.
        proposed = (
            json.loads(json.dumps(task["calls"])) if arm == "scripted-client" else task["calls"]
        )
        calls = validate_plan(task, proposed)
        async with asyncio.timeout(deadline):
            for call in calls:
                answer = await client.call_tool(call["name"], call["arguments"])
                record["results"].append(answer.structured_content)
                record["protocol_errors"].append(answer.is_error)
                if answer.is_error:
                    break
    except TimeoutError:
        record["status"] = "timeout"
    except asyncio.CancelledError:
        record["status"] = "cancelled"
    except (MCPError, ValueError) as exc:
        record["status"] = "failed"
        record["error_type"] = type(exc).__name__
    finally:
        record["elapsed_ms"] = (perf_counter() - start) * 1000
        record["bytes"] = len(json.dumps(record["results"]).encode())
    return record


def fixture_settings(upstream: FixtureServer, task: dict[str, Any], directory: Path) -> Settings:
    scenario_settings = upstream.fixtures.settings_of(tuple(task.get("scenarios", [])))
    return Settings(
        upstream_mode="fixture",
        data_dir=directory,
        evs_base_url=upstream.base_url("evs"),
        evs_fhir_base_url=upstream.base_url("evs-fhir"),
        cadsr_base_url=upstream.base_url("cadsr"),
        cadsr_ftp_url=upstream.base_url("cadsr-ftp"),
        ssis_sparql_url=upstream.base_url("ssis-sparql"),
        cadsr_credential=scenario_settings.get("NCI_SIP_CADSR_CREDENTIAL"),
        evs_max_attempts=1,
        timeout_seconds=5,
    )


async def measure_task(
    task: dict[str, Any],
    slots: list[dict[str, Any]],
    upstream: FixtureServer,
    directory: Path,
    deadline: float,
) -> None:
    upstream.activate(*task.get("scenarios", []))
    settings = fixture_settings(upstream, task, directory)
    capabilities = frozenset(task.get("capabilities", [spec.name for spec in SPECS if spec.name]))

    async def authority() -> Authority:
        return Authority(
            Principal("https://evaluation.invalid", "fixture-caller"),
            capabilities,
            "fixture-policy-v1",
            time() + deadline + 10,
        )

    context = Context(settings)
    server = create_mcp(settings, context=context, authority_resolver=authority)
    async with Client(server, cache=None) as client:
        for slot in slots:
            upstream.reset()
            record = await execute(client, task, slot["arm"], deadline)
            record["requests"] = len(upstream.log())
            record["unmatched_requests"] = len(unmatched_requests(upstream.log()))
            slot.update(assess(task, record))
            slot["result_sha256"] = hashlib.sha256(
                json.dumps(record["results"], sort_keys=True).encode()
            ).hexdigest()
            slot["unmatched_requests"] = record["unmatched_requests"]
            slot["protocol_errors"] = record["protocol_errors"]
            if record["status"] == "cancelled":
                raise asyncio.CancelledError


def build_report(plan: dict[str, Any], slots: list[dict[str, Any]]) -> dict[str, Any]:
    tasks = {task["id"]: task for task in plan["tasks"]}
    rows = [
        slot if "correct" in slot else slot | assess(tasks[slot["task"]], slot) for slot in slots
    ]
    return {
        "schema": 1,
        "evidence_kind": "offline-fixture-mechanics",
        "model": None,
        "tokens": None,
        "cost": None,
        "generated_answer_support": "not evaluated",
        "release": plan["release"],
        "rows": rows,
        "summary": {arm: summarize([row for row in rows if row["arm"] == arm]) for arm in ARMS},
        "by_split": grouped_summary(rows, "split"),
        "by_condition": grouped_summary(rows, "condition"),
        "limits": {
            "deadline_seconds": plan["deadline_seconds"],
            "repetitions": plan["repetitions"],
        },
    }


def grouped_summary(rows: list[dict[str, Any]], key: str) -> dict[str, Any]:
    return {
        value: summarize([row for row in rows if row[key] == value])
        for value in sorted({row[key] for row in rows})
    }


def metadata() -> dict[str, Any]:
    sources = [
        TASKS,
        Path(__file__),
        Path(__file__).with_name("assisted_scoring.py"),
        ROOT / "pdm.lock",
    ]
    return {
        "base_commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"],  # noqa: S607 - fixed repository query
            cwd=ROOT,
            text=True,
        ).strip(),
        "working_tree_dirty": bool(
            subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT)  # noqa: S607
        ),
        "python": platform.python_version(),
        "mcp": version("mcp"),
        "source_sha256": {
            str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sources
        },
    }


def run(plan: dict[str, Any], slots: list[dict[str, Any]], directory: Path) -> None:
    fixtures = load_fixtures(ROOT / "acceptance/fixtures")
    for task in plan["tasks"]:
        for arm in ARMS:
            selected = [slot for slot in slots if slot["task"] == task["id"] and slot["arm"] == arm]
            with FixtureServer(fixtures) as upstream:
                asyncio.run(
                    measure_task(
                        task,
                        selected,
                        upstream,
                        directory / task["id"] / arm,
                        plan["deadline_seconds"],
                    )
                )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    plan = yaml.safe_load(TASKS.read_text())
    slots = planned_runs(plan)
    provenance = metadata()
    (ROOT / "tmp").mkdir(exist_ok=True)
    try:
        with (
            TemporaryDirectory(prefix="assisted-", dir=ROOT / "tmp") as directory,
            (Path(directory) / "diagnostics.log").open("w") as log,
            redirect_stderr(log),
        ):
            run(plan, slots, Path(directory))
    finally:
        report = build_report(plan, slots)
        report["environment"] = provenance
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")
    print(json.dumps(report["summary"], indent=2))
    return int(any(not row["correct"] for row in report["rows"]))


if __name__ == "__main__":
    raise SystemExit(main())
