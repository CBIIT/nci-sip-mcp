"""Safe native acceptance projections bound to the run's original catalogue snapshots."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from typing import Any

from scripts.evidence_envelope import EvidenceError, decode_json, validate_envelope
from scripts.inventory_records import digest, read_snapshots

from nci_sip_acceptance.report import (
    CANNOT_RUN,
    FAILED,
    UNRUN,
    NativeReport,
    ReportFieldError,
    complete,
    completion_problem,
    fails_by_gates_only,
    gate_names,
    own_unrun,
    require_field,
    tool_outcome,
)

# Earlier tool rows carry this field; absence and null imply different availability.
_RECORDED_TOOL_FIELD = "implemented_as"


def require(condition: bool, path: str) -> None:
    """Reject contradictions without including private values in diagnostics."""
    try:
        require_field(condition, path)
    except ReportFieldError as error:
        raise EvidenceError(str(error)) from None


def _native_header(report: dict[str, Any], catalogue: dict[str, Any]) -> None:
    require(report["suite"]["digest"] == catalogue["suite_digest"], "suite.digest")
    require(set(report["tools"]) == set(catalogue["tools"]), "tools")


def _native_cases(report: dict[str, Any], context: dict[str, Any]) -> None:
    require(set(report["tests"]) <= set(context["selection"]), "tests")
    for index, (name, row) in enumerate(report["tests"].items()):
        expected = context["catalogue"]["cases"][name]
        require(row["gate"] == expected["gate"], f"tests[{index}].gate")
        require(row["tool"] == expected["tool"], f"tests[{index}].tool")


def _gates(report: dict[str, Any], field: str, outcomes: tuple[str, ...]) -> list[str]:
    expected = gate_names(report["tests"], outcomes)
    require(report[field] == expected, field)
    return expected


def _tool(
    row: Any, counts: Counter[str], group: str, failed: bool, unrun: bool, path: str
) -> dict[str, Any]:
    require(row["group"] == group, f"{path}.group")
    require(row["counts"] == dict(counts), f"{path}.counts")
    availability = _availability(row)
    require(row["outcome"] in _verdicts(counts, failed, unrun, availability), f"{path}.outcome")
    expected = fails_by_gates_only(counts, failed)
    require(row["gates_only"] == expected, f"{path}.gates_only")
    return {
        "group": group,
        "outcome": row["outcome"],
        "gates_only": row["gates_only"],
        # A gate could not run and the tool's own tests all ran; the portal says so for a PASS
        # or INCOMPLETE verdict.
        "gates_unrun": unrun and not own_unrun(counts),
        "counts": dict(counts),
    }


def _verdicts(
    counts: Counter[str], failed: bool, unrun: bool, availability: tuple[bool | None, ...]
) -> set[str]:
    """The verdicts a harness gives the row. A report written before #265 left a tool PASS
    while a gate did not run; that stays the recorded run's verdict, displayed unchanged."""

    gates = {unrun, False}
    return {tool_outcome(counts, failed, gated, value) for value in availability for gated in gates}


def _availability(row: dict[str, Any]) -> tuple[bool | None, ...]:
    """The availability verdicts the row's outcome may rest on.

    A recorded row names the tool it was served as. The native report cannot distinguish an
    unstarted server from a missing tool if every alias is null, and a current row has no
    alias: preserve any valid harness verdict instead of inventing availability.
    """

    if _RECORDED_TOOL_FIELD not in row:
        return (None, False, True)
    alias = row[_RECORDED_TOOL_FIELD]
    return (None, False) if alias is None else (True,)


def _native(raw: bytes, context: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        value = decode_json(raw)
    except EvidenceError:
        raise EvidenceError("Invalid acceptance field: report") from None
    try:
        report = NativeReport.from_dict(value).to_dict()
    except ReportFieldError as error:
        raise EvidenceError(str(error)) from None
    _native_header(report, context["catalogue"])
    _native_cases(report, context)
    failed = bool(_gates(report, "failed_gates", FAILED))
    _gates(report, "unrun_gates", UNRUN)
    # Only a gate that could not run keeps a tool from passing; one skipped by design in a live
    # run (not_live) does not.
    unrun = bool(gate_names(report["tests"], CANNOT_RUN))
    tools = {}
    for index, (name, row) in enumerate(report["tools"].items()):
        counts = Counter(row["outcome"] for row in report["tests"].values() if row["tool"] == name)
        tools[name] = _tool(
            row, counts, context["catalogue"]["tools"][name], failed, unrun, f"tools[{index}]"
        )
    return report, tools


def _case_rows(context: dict[str, Any], tests: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "id": digest(name.encode()),
            "story": context["stories"][name],
            "expected": context["expectations"][name],
            "outcome": tests.get(name, {}).get("outcome"),
            **context["catalogue"]["cases"][name],
        }
        for name in context["selection"]
    ]


def _inventory_complete(native: dict[str, Any], missing: list[str], state: str) -> bool:
    return (
        not missing
        and state in ("completed", "failed")
        and ("run" not in native or complete(native))
    )


def project_acceptance(
    envelope: bytes,
    report: bytes | None,
    *,
    catalogue: bytes,
    stories: bytes,
    expectations: bytes,
    selection: bytes,
) -> dict[str, Any]:
    """Validate a native report against bound original snapshots; return only safe fields.

    This is a local projection, not an authentication or public publication decision. The
    caller supplies snapshots from the recorded run, never substitutes the current checkout.
    A historical import without these snapshots remains unverified outside this adapter.
    Reports with run metadata must also pass the harness's completion predicate. Older
    imports without it retain the independent selection-bound completeness check alone;
    no historical completion fields are invented. Partial evidence remains displayable.
    """
    record = validate_envelope(envelope, report)
    require(record["kind"] == "acceptance", "kind")
    context = read_snapshots(
        record,
        {
            "catalogue": catalogue,
            "stories": stories,
            "expectations": expectations,
            "selection": selection,
        },
    )
    native, tools = _native(report, context) if report is not None else ({"tests": {}}, None)
    tests = native["tests"]
    missing = [digest(name.encode()) for name in context["selection"] if name not in tests]
    result = record | {
        "mode": native.get("mode"),
        "transport": native.get("transport"),
        "inventory_complete": _inventory_complete(native, missing, record["state"]),
        "completion_problem": completion_problem(native) if "run" in native else None,
        "missing": missing,
        "cases": _case_rows(context, tests),
        "tools": tools,
        "counts": dict(Counter(row["outcome"] for row in tests.values()))
        if report is not None
        else None,
    }
    result.update(_recorded_environment(native))
    return result


def _recorded_environment(native: Mapping[str, object]) -> dict[str, object]:
    return {"environment": native["environment"]} if "environment" in native else {}
