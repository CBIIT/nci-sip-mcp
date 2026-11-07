"""One benchmark report and selection model, retaining the existing transport wire shapes."""

# ruff: noqa: N815 -- field names are the existing JSON names
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field, fields, replace
from typing import Any, Literal, cast

from scripts.benchmark_limits import Limits
from scripts.benchmark_records import (
    MAX_MEASUREMENT,
    Case,
    Conditions,
    SelectedCase,
    Suite,
    Transport,
    hexadecimal,
    identifier,
    number,
    require,
    shape,
    wire_fields,
)
from scripts.evidence_envelope import EvidenceError, decode_json, timestamp
from scripts.vocabulary import RUN_STATES, HttpStop, RunState, RunStop, is_run_stop

from nci_si_acceptance.environments import EnvironmentRecord, environment_record
from nci_si_acceptance.report import Absent

MAX_REPETITIONS = 10_000


def fingerprint_value(value: Any) -> str:
    """Hash planned values without exposing arguments or host labels."""
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


@dataclass(frozen=True)
class Fingerprint:
    mode: str | None = None
    transport: str | None = None
    definitions: str | None = None
    workload: str | None = None
    profile: str | None = None
    release: str | None = None
    index: str | None = None
    model: str | None = None
    hardware: str | None = None
    environment: str | None = None
    placement: str | None = None
    warmup: str | None = None
    samples: str | None = None
    concurrency: str | None = None
    timeouts: str | None = None

    @classmethod
    def from_dict(cls, value: Any, measured: Fingerprint) -> Fingerprint:
        shape(value, wire_fields(cls, "stdio"), "selection.fingerprint")
        for name, expected in asdict(measured).items():
            if value[name] is not None:
                hexadecimal(value[name], f"selection.fingerprint.{name}")
            require(expected is None or value[name] == expected, f"selection.fingerprint.{name}")
        return cls(**value)


FINGERPRINT_FIELDS = {item.name for item in fields(Fingerprint)}


def fingerprint(
    mode: str,
    transport: Transport,
    conditions: dict[str, Any],
    cases: list[dict[str, Any]],
    repetitions: int,
    limits: Limits | None = None,
) -> Fingerprint:
    result = {
        "mode": mode,
        "transport": transport,
        "definitions": conditions,
        "workload": cases,
        "samples": repetitions,
    }
    if limits is not None:
        result |= {
            "warmup": limits.warmups,
            "concurrency": 1,
            "timeouts": {
                key: getattr(limits, key) for key in ("seconds", "timeout", "max_requests")
            },
        }
    return Fingerprint(**{key: fingerprint_value(value) for key, value in result.items()})


@dataclass(frozen=True)
class Report:
    transport: Transport
    mode: Literal["fixture", "live"]
    createdAt: str
    complete: bool
    expectedCases: list[str]
    conditions: Conditions | dict[str, Any]
    cases: list[Case]
    schema: int | None = field(default=None, metadata={"transport": "streamable-http"})
    state: RunState | None = field(default=None, metadata={"transport": "streamable-http"})
    stopReason: RunStop | HttpStop | None = field(
        default=None, metadata={"transport": "streamable-http"}
    )
    httpRequests: int | None = field(default=None, metadata={"transport": "streamable-http"})
    limits: Limits | None = field(default=None, metadata={"transport": "streamable-http"})
    targetSha256: str | None = field(default=None, metadata={"transport": "streamable-http"})
    serverVersion: str | None = field(default=None, metadata={"transport": "stdio"})
    sourceSha256: str | None = field(default=None, metadata={"transport": "stdio"})
    python: str | None = field(default=None, metadata={"transport": "stdio"})
    platform: str | None = field(default=None, metadata={"transport": "stdio"})
    suite: Suite | None = field(default=None, metadata={"transport": "stdio"})
    repetitions: int | None = field(default=None, metadata={"transport": "stdio"})

    environment: EnvironmentRecord | Absent = field(
        default=Absent.FIELD, metadata={"transport": "stdio"}
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            key: value
            for key, value in asdict(self).items()
            if key in wire_fields(type(self), self.transport) and value != Absent.FIELD
        } | {"cases": [case.to_dict(self.transport) for case in self.cases]}

    @classmethod
    def from_dict(cls, value: Any, execution_state: str) -> Report:
        require(isinstance(value, dict), "report")
        transport = value.get("transport")
        require(transport in ("stdio", "streamable-http"), "transport")
        environment = _environment(value)
        shape(value, _report_fields(value, transport), "")
        require(value["mode"] in ("fixture", "live"), "mode")
        require(type(value["complete"]) is bool, "complete")
        converted = (
            _http_header(value, execution_state)
            if transport == "streamable-http"
            else _stdio_header(value)
        )
        limits = converted.get("limits")
        repetitions = limits.repetitions if limits else value["repetitions"]
        counts = {"cold": repetitions, "warm": repetitions}
        if limits:
            counts["warmups"] = limits.warmups
        require(isinstance(value["cases"], list), "cases")
        cases = [
            Case.from_dict(
                row,
                transport,
                counts,
                f"cases[{index}]",
                limits.max_requests if limits else MAX_MEASUREMENT,
            )
            for index, row in enumerate(value["cases"])
        ]
        report = cls(**(value | converted | {"cases": cases, "environment": environment}))
        report.missing()
        if limits:
            _totals(report)
        return report

    def missing(self) -> list[str]:
        expected = _expected_cases(self.expectedCases)
        found = [case.selected.tool for case in self.cases]
        if self.limits:
            require(found == expected, "cases")
            missing = [
                case.selected.tool for case in self.cases if not _complete_case(case, self.limits)
            ]
        else:
            missing = _missing_stdio(found, expected)
        require(not self.complete or not missing, "complete")
        return missing

    def fingerprint(self, cases: list[SelectedCase] | None = None) -> Fingerprint:
        selected = cases if cases is not None else [case.selected for case in self.cases]
        conditions = (
            asdict(self.conditions) if isinstance(self.conditions, Conditions) else self.conditions
        )
        repetitions = self.limits.repetitions if self.limits else self.repetitions
        measured = fingerprint(
            self.mode,
            self.transport,
            conditions,
            [asdict(case) for case in selected],
            cast("int", repetitions),
            self.limits,
        )

        if self.environment != Absent.FIELD:
            return replace(measured, environment=fingerprint_value(self.environment))
        return measured


def _report_fields(value: dict[str, object], transport: Transport) -> set[str]:
    expected = wire_fields(Report, transport)
    if "environment" not in value:
        expected.discard("environment")
    return expected


def _environment(value: dict[str, object]) -> EnvironmentRecord | Absent:
    if "environment" not in value:
        return Absent.FIELD
    require(value.get("mode") == "live" and value.get("transport") == "stdio", "environment")
    try:
        return environment_record(value["environment"])
    except ValueError:
        raise EvidenceError("Invalid benchmark field: environment") from None


def _stdio_header(value: dict[str, Any]) -> dict[str, Any]:
    repetitions = value["repetitions"]
    require(type(repetitions) is int and 0 < repetitions <= MAX_REPETITIONS, "repetitions")
    for name in ("createdAt", "serverVersion", "sourceSha256", "python", "platform"):
        require(isinstance(value[name], str), name)
    hexadecimal(value["sourceSha256"], "sourceSha256")
    require(isinstance(value["conditions"], dict), "conditions")
    return {"suite": Suite.from_dict(value["suite"])}


def _expected_cases(expected: Any) -> list[str]:
    require(isinstance(expected, list) and bool(expected), "expectedCases")
    for index, name in enumerate(expected):
        identifier(name, f"expectedCases[{index}]")
    require(len(set(expected)) == len(expected), "expectedCases")
    return expected


def _missing_stdio(found: list[str], expected: list[str]) -> list[str]:
    require(len(set(found)) == len(found) and set(found) <= set(expected), "cases")
    return [name for name in expected if name not in found]


def _http_header(value: dict[str, Any], execution_state: str) -> dict[str, Any]:
    require(type(value["schema"]) is int and value["schema"] == 1, "schema")
    limits = Limits.from_dict(value["limits"])
    number(value["httpRequests"], "httpRequests", integer=True, maximum=limits.max_requests)
    hexadecimal(value["targetSha256"], "targetSha256")
    try:
        timestamp(value["createdAt"])
    except EvidenceError:
        require(False, "createdAt")
    _termination(value, execution_state)
    return {"limits": limits, "conditions": Conditions.from_dict(value["conditions"])}


def _termination(value: dict[str, Any], execution_state: str) -> None:
    state = value["state"]
    require(isinstance(state, str) and state in RUN_STATES, "state")
    require(value["complete"] == (state == "completed"), "complete")
    if state in ("running", "completed"):
        require(value["stopReason"] is None, "stopReason")
    else:
        require(is_run_stop(value["stopReason"]), "stopReason")
        require((state == "cancelled") == (value["stopReason"] == "cancelled"), "stopReason")
    require(execution_state != "completed" or state == "completed", "state")


def _complete_case(case: Case, limits: Limits) -> bool:
    return all(
        len(phase.samples) == (limits.warmups if name == "warmups" else limits.repetitions)
        for name, phase in case.phases().items()
    )


def _totals(report: Report) -> None:
    samples = [
        sample
        for case in report.cases
        for phase in case.phases().values()
        for sample in phase.samples
    ]
    correlations = [sample.correlationId for sample in samples]
    require(len(set(correlations)) == len(correlations), "cases.correlationId")
    require(
        sum(cast("int", sample.httpRequests) for sample in samples)
        <= cast("int", report.httpRequests),
        "httpRequests",
    )


@dataclass(frozen=True)
class Selection:
    schema: int
    cases: list[SelectedCase]
    fingerprint: Fingerprint

    @classmethod
    def from_dict(cls, value: Any, report: Report) -> Selection:
        shape(value, wire_fields(cls, report.transport), "selection")
        require(type(value["schema"]) is int and value["schema"] == 1, "selection.schema")
        require(isinstance(value["cases"], list), "selection.cases")
        cases = [
            SelectedCase.from_dict(row, f"selection.cases[{index}]")
            for index, row in enumerate(value["cases"])
        ]
        require([case.tool for case in cases] == report.expectedCases, "selection.cases")
        selected = {case.tool: case for case in cases}
        for index, case in enumerate(report.cases):
            require(case.selected == selected[case.selected.tool], f"selection.cases[{index}]")
        return cls(1, cases, Fingerprint.from_dict(value["fingerprint"], report.fingerprint(cases)))


def selection_fingerprint(raw: bytes | None, digest: str, report: Report) -> Fingerprint:
    if raw is None:
        return report.fingerprint()
    require(hashlib.sha256(raw).hexdigest() == digest, "selection_sha256")
    return Selection.from_dict(decode_json(raw), report).fingerprint
