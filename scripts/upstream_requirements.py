"""Generate upstream requests from explicit evidence and per-test acceptance outcomes."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any

import yaml

from nci_sip_acceptance.report import FAILED, FIXTURE_ONLY, combine
from nci_sip_acceptance.requirements import repo_path

ROOT = Path(__file__).resolve().parents[1]
DIRECTORY = Path("docs/upstream")
EVIDENCE = Path("docs/evidence/phase-5")
TEAMS = {"evs": "EVS", "cadsr": "caDSR", "ssis": "Shared SI"}
PARAGRAPHS = ("observation", "reproduction", "expected", "impact", "workaround", "acceptance")


def functions(report: dict[str, Any], name: str) -> list[str]:
    return [nodeid for nodeid in report["tests"] if nodeid.split("[", 1)[0] == name]


def validate_reports(fixture: dict[str, Any], live: dict[str, Any]) -> None:
    if (fixture["mode"], live["mode"]) != ("fixture", "live"):
        raise ValueError("Expected fixture and live reports, in that order")
    if fixture["suite"] != live["suite"]:
        raise ValueError("Reports must name the same suite identity")
    if set(fixture["tests"]) != set(live["tests"]) or not fixture["tests"]:
        raise ValueError("Reports must cover the same nonempty set of test identities")
    if set(fixture["tools"]) != set(live["tools"]):
        raise ValueError("Reports must cover the same tools")


def validate_entry(entry: dict[str, Any], fixture: dict[str, Any], root: Path) -> None:
    required = {"id", "team", "title", "platform", "source", "requirements", "tests", "evidence"}
    if set(entry) != required | set(PARAGRAPHS):
        raise ValueError("Each entry must supply exactly the documented catalogue fields")
    if entry["team"] not in TEAMS:
        raise ValueError("Unknown upstream team")
    for key in required - {"requirements", "tests", "evidence"} | set(PARAGRAPHS):
        if not isinstance(entry[key], str) or not entry[key].strip():
            raise ValueError(f"Missing text for {key}")
    validate_references(entry, fixture, root)


def validate_references(entry: dict[str, Any], fixture: dict[str, Any], root: Path) -> None:
    known = yaml.safe_load((root / "spec/requirements.yaml").read_text())
    checks = {
        "requirements": lambda value: value in known,
        "tests": lambda value: bool(functions(fixture, value)),
        "evidence": lambda value: (root / value).is_file(),
    }
    for field, check in checks.items():
        values = entry[field]
        if not isinstance(values, list) or not values:
            raise ValueError(f"A nonempty {field} list is required")
        if not all(isinstance(value, str) and check(value) for value in values):
            raise ValueError(f"Unknown or missing {field} in {entry['id']}")


def qualifications(
    catalogue: dict[str, Any], fixture: dict[str, Any], live: dict[str, Any]
) -> dict[str, str]:
    entries = {entry["id"]: entry for entry in catalogue["entries"]}
    result = {}
    for nodeid, identifier in catalogue["live_failures"].items():
        try:
            entry, observed = entries[identifier], live["tests"][nodeid]
        except KeyError as exc:
            raise ValueError("Unknown exact test or entry in a live qualification") from exc
        if observed["outcome"] not in FAILED:
            raise ValueError("A live qualification needs an observed failure, never an unrun test")
        if fixture["tests"][nodeid]["outcome"] != "passed":
            raise ValueError("A live qualification cannot excuse a fixture failure")
        if nodeid.split("[", 1)[0] not in entry["tests"]:
            raise ValueError("A qualification must belong to its entry's affected tests")
        result[nodeid] = identifier
    return result


def counts(report: dict[str, Any], nodeids: list[str]) -> str:
    counted = Counter(report["tests"][nodeid]["outcome"] for nodeid in nodeids)
    return ", ".join(f"{number} {outcome}" for outcome, number in sorted(counted.items()))


def evidence_line(root: Path, name: str) -> str:
    path = root / name
    description = "document"
    if path.suffix == ".json":
        data = json.loads(path.read_text())
        description = data.get("kind", "measurement report")
        if description == "recorded":
            description += f" on {data['recorded_on']}"
        if request := data.get("request"):
            description += f"; {request['method']} {request['surface']} {request['path']}"
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return f"- [{name}](../../{name}) — {description}; SHA-256 `{digest}`."


def entry_text(entry: dict[str, Any], fixture: dict[str, Any], live: dict[str, Any], root: Path):
    lines = [
        f"## {entry['id']}",
        "",
        f"**{entry['title']}**",
        "",
        f"Platform requirement/operation: **{entry['platform']}**.",
        "Specification requirements: "
        + ", ".join(f"`{key}`" for key in entry["requirements"])
        + ".",
        f"Source discussion: [issue evidence]({entry['source']}).",
        "",
    ]
    for field in PARAGRAPHS:
        label = "Acceptance criteria" if field == "acceptance" else field.capitalize()
        lines += [f"**{label}.** {entry[field]}", ""]
    lines += ["Evidence (linked request/response files retain their original form):", ""]
    lines += [evidence_line(root, name) for name in entry["evidence"]]
    lines += ["", "| Affected test function | Fixture | Live |", "|---|---|---|"]
    for name in entry["tests"]:
        nodeids = functions(fixture, name)
        lines.append(
            f"| `{repo_path(name)}` | {counts(fixture, nodeids)} | {counts(live, nodeids)} |"
        )
    return [*lines, ""]


def overview(
    catalogue: dict[str, Any],
    fixture: dict[str, Any],
    live: dict[str, Any],
    excused: dict[str, str],
) -> str:
    combined = combine(fixture, live, excused)
    qualified = sum(outcome == FIXTURE_ONLY for outcome, _ in combined.values())
    lines = [
        "# Upstream requirements packages",
        "",
        "Generated by `pdm run python scripts/upstream_requirements.py`; edit catalogue.yaml,",
        "then regenerate. `--check` verifies the checked-in output. No service is contacted.",
        "",
        "[EVS](evs.md) · [caDSR](cadsr.md) · [Shared SI](ssis.md) · "
        "[Source catalogue](catalogue.yaml)",
        "",
        "[Team responses recorded 8 October 2026](team-responses-2026-10-08.md) clarify "
        "upstream fixes, search capabilities and the Shared SI informational response.",
        "",
        f"Acceptance report suite digest: `{fixture['suite']['digest']}`.",
        "Inputs: [fixture](../evidence/phase-5/acceptance-fixture.json) and",
        "[live](../evidence/phase-5/acceptance-live.json). "
        "These are recorded snapshots, not a new run.",
        "",
        f"**{qualified} formal PASS (fixture only) tool rows; "
        f"{len(excused)} qualified live failures.**",
        "Known capability requests below remain separate from these acceptance exceptions.",
        "Affected fixture tests demonstrate caller behavior; their pass is not proof that an",
        "upstream enhancement exists. A crafted regression scenario is not itself a defect.",
        "",
        f"Live cases: {counts(live, list(live['tests']))}.",
        f"Live gates unrun: {len(live['unrun_gates'])}; failed: {len(live['failed_gates'])}.",
        "The combined renderer retains fixture PASS when no live-capable test failed;",
        "unrun content and gates do not establish live content acceptance. "
        "See the [benchmark boundary](../benchmark.md).",
        "",
        "| Tool | Combined outcome | Exact-test qualification entries |",
        "|---|---|---|",
    ]
    lines += [
        f"| `{tool}` | {outcome} | {', '.join(ids)} |" for tool, (outcome, ids) in combined.items()
    ]
    lines += ["", "## Qualified live failures", ""]
    lines += [
        f"- `{repo_path(nodeid)}` → `{entry}`" for nodeid, entry in sorted(excused.items())
    ] or ["None."]
    lines += [
        "",
        "## Capability requests",
        "",
        "| Entry | Team | Upstream reference |",
        "|---|---|---|",
    ]
    lines += [
        f"| [{entry['title']}]({entry['team']}.md#{entry['id']}) | "
        f"{TEAMS[entry['team']]} | {entry['platform']} |"
        for entry in catalogue["entries"]
    ]
    return "\n".join(lines) + "\n"


def generate(root: Path = ROOT) -> dict[str, str]:
    catalogue = yaml.safe_load((root / DIRECTORY / "catalogue.yaml").read_text())
    # Frozen snapshots checked by test-set identity, not fresh runs.
    fixture = json.loads((root / EVIDENCE / "acceptance-fixture.json").read_text())
    live = json.loads((root / EVIDENCE / "acceptance-live.json").read_text())
    validate_reports(fixture, live)
    entries = catalogue["entries"]
    if len({entry["id"] for entry in entries}) != len(entries):
        raise ValueError("Duplicate catalogue entry id")
    for entry in entries:
        validate_entry(entry, fixture, root)
    excused = qualifications(catalogue, fixture, live)
    output = {
        "README.md": overview(catalogue, fixture, live, excused),
        "ssis-dependencies.md": ssis_dependencies(root),
    }
    for team, title in TEAMS.items():
        lines = [
            f"# {title} upstream requirements",
            "",
            "Generated from [catalogue.yaml](catalogue.yaml). "
            "Read the [evidence boundary](README.md) first.",
            "",
        ]
        for entry in entries:
            if entry["team"] == team:
                lines += entry_text(entry, fixture, live, root)
        output[f"{team}.md"] = "\n".join(lines)
    return output


def ssis_dependencies(root: Path) -> str:
    register = yaml.safe_load((root / DIRECTORY / "ssis-queries.yaml").read_text())
    lines = [
        "# Shared SI query dependencies",
        "",
        (
            "Generated from [ssis-queries.yaml](ssis-queries.yaml) by "
            "`pdm run python scripts/upstream_requirements.py`; never edit the table by hand."
        ),
        "",
        (
            "The [team response relayed on 8 October 2026](team-responses-2026-10-08.md) "
            "states maintenance mode and no funding for new API development or maintenance. "
            "This is an attributed dated report, not an independently verified current status."
        ),
        "",
        f"Risk-record owner: **{register['owner']}**. "
        "EVS SPARQL and caDSR APIs are published candidate alternatives, not implemented "
        "or equivalent fallbacks. A missing upstream capability stays visible.",
        "",
        f"Transport: `{register['transport']}`. "
        f"Coverage: {register['weekly']}. "
        f"Caller error fallback: `{register['fallback']}`.",
        "",
        (
            "Run `pdm run python -m scripts.ssis_contracts` to attempt all registered families. "
            "Each is reported; any structural or transport failure exits nonzero. The checker "
            "validates SELECT variables, bindings, term shapes, identifiers and row bounds. "
            "Valid empty content results and content/count/release "
            "changes pass; graph identities require both known graphs. "
            "This is platform conformance, not a new MCP acceptance requirement."
        ),
        "",
        "| Family / live check ID | Client operation | Consumers | Requirements | Evidence |",
        "|---|---|---|---|---|",
    ]
    for entry in register["queries"]:
        evidence = (
            "../../acceptance/src/nci_sip_acceptance/craft.py"
            if entry["evidence"].startswith("crafted/ssis-public-id/")
            else f"../../acceptance/fixtures/{entry['evidence']}"
        )
        lines.append(
            f"| `{entry['id']}` | `{entry['operation']}` | "
            f"{', '.join(entry['consumers'])} | {', '.join(entry['requirements'])} | "
            f"[{entry['evidence']}]({evidence}) |"
        )
    lines += [
        "",
        (
            "The bounded identity request is crafted from "
            "recorded evidence, not a new live recording. Element queries have new live "
            "recordings with complete role tuples; value public-ID projections are crafted "
            "in memory from historical captures and are not complete current answers. "
            "Identifier and remaining-budget "
            "LIMIT instances are parameters of these families. Historical REST façade, "
            "unbounded identity and values-by-public-id forms in the "
            "[request history](../../acceptance/request-forms/ssis.md) are inactive. "
            "The checker calls only anonymous Shared SI; it does not join EVS or caDSR "
            "or substitute their results when Shared SI fails."
        ),
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    for name, content in generate(ROOT).items():
        path = ROOT / DIRECTORY / name
        if args.check:
            if not path.is_file() or path.read_text() != content:
                raise SystemExit(f"Regenerate {path.relative_to(ROOT)}")
        else:
            path.write_text(content)


if __name__ == "__main__":
    main()
