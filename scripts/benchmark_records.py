"""Benchmark records shared by measurement writers and evidence readers."""

# Wire names are dataclass fields so producers and readers cannot declare different names.
# ruff: noqa: N815

from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass, field, fields
from typing import Any, Literal, get_args

from scripts.evidence_envelope import EvidenceError
from scripts.formats import SHA256_HEX
from scripts.vocabulary import HttpStop, RunStop, is_run_stop

from nci_sip_mcp.errors import ErrorCode

Transport = Literal["stdio", "streamable-http"]
MAX_MEASUREMENT = 2**63 - 1
MAX_CONDITION_TEXT = 512


def require(condition: bool, path: str) -> None:
    if not condition:
        raise EvidenceError(f"Invalid benchmark field: {path}")


def wire_fields(record: type, transport: Transport) -> set[str]:
    """Field declarations own transport applicability as well as JSON names."""
    return {
        item.name
        for item in fields(record)
        if item.metadata.get("transport", transport) == transport
    }


def shape(value: Any, expected: set[str], path: str) -> None:
    require(isinstance(value, dict), path)
    for name in sorted(expected):
        require(name in value, f"{path}.{name}".lstrip("."))
    # An unknown key can itself contain private text; report its owning record instead.
    require(set(value) == expected, path or "report")


def number(value: Any, path: str, *, integer: bool = False, maximum: int = MAX_MEASUREMENT) -> None:
    kinds = (int,) if integer else (int, float)
    require(type(value) in kinds and 0 <= value <= maximum, path)


def hexadecimal(value: Any, path: str) -> None:
    require(isinstance(value, str) and re.fullmatch(SHA256_HEX, value) is not None, path)


def identifier(value: Any, path: str) -> None:
    require(
        isinstance(value, str) and re.fullmatch(r"[a-z][a-z0-9_-]{0,99}", value) is not None, path
    )


def percentile(values: list[float], fraction: float) -> float:
    if not values or not 0 < fraction <= 1:
        raise ValueError("A percentile needs samples and a fraction in (0, 1]")
    if any(not math.isfinite(value) or value < 0 for value in values):
        raise ValueError("Timing samples must be finite and nonnegative")
    return sorted(values)[math.ceil(len(values) * fraction) - 1]


@dataclass(frozen=True)
class Sample:
    elapsedMs: float
    resultBytes: int | None
    responseCode: Literal["ok"] | ErrorCode | RunStop | HttpStop
    error: bool
    outboundRequests: int | None = None
    release: dict[str, Any] | None = field(default=None, metadata={"transport": "stdio"})
    httpRequests: int | None = field(default=None, metadata={"transport": "streamable-http"})
    correlationId: str | None = field(default=None, metadata={"transport": "streamable-http"})
    cacheState: None = field(default=None, metadata={"transport": "streamable-http"})
    serverCommit: None = field(default=None, metadata={"transport": "streamable-http"})
    replica: None = field(default=None, metadata={"transport": "streamable-http"})

    def to_dict(self, transport: Transport) -> dict[str, Any]:
        names = wire_fields(type(self), transport)
        return {key: value for key, value in asdict(self).items() if key in names}

    @classmethod
    def from_dict(
        cls, value: Any, transport: Transport, path: str, *, max_requests: int = MAX_MEASUREMENT
    ) -> Sample:
        shape(value, wire_fields(cls, transport), path)
        number(value["elapsedMs"], f"{path}.elapsedMs")
        code = value["responseCode"]
        allowed = code in ("ok", *get_args(ErrorCode))
        require(
            allowed or (transport == "streamable-http" and is_run_stop(code)),
            f"{path}.responseCode",
        )
        require(type(value["error"]) is bool and value["error"] == (code != "ok"), f"{path}.error")
        _result_size(value, transport, path)
        if transport == "stdio":
            number(value["outboundRequests"], f"{path}.outboundRequests", integer=True)
            require(isinstance(value["release"], dict), f"{path}.release")
        else:
            _http_sample(value, path, max_requests)
        return cls(**value)


def _result_size(value: dict[str, Any], transport: Transport, path: str) -> None:
    if value["resultBytes"] is None:
        require(transport == "streamable-http" and value["error"], f"{path}.resultBytes")
    else:
        number(value["resultBytes"], f"{path}.resultBytes", integer=True)


def _http_sample(value: dict[str, Any], path: str, max_requests: int) -> None:
    number(value["httpRequests"], f"{path}.httpRequests", integer=True, maximum=max_requests)
    correlation = value["correlationId"]
    require(
        isinstance(correlation, str) and re.fullmatch(r"[a-f0-9]{32}", correlation) is not None,
        f"{path}.correlationId",
    )
    for name in ("outboundRequests", "cacheState", "serverCommit", "replica"):
        require(value[name] is None, f"{path}.{name}")


@dataclass(frozen=True)
class Summary:
    calls: int
    p50Ms: float | None
    p95Ms: float | None
    errorRate: float | None
    meanResultBytes: float | None
    meanOutboundRequests: float | None

    @classmethod
    def from_dict(cls, value: Any, expected: Summary, path: str) -> Summary:
        shape(value, wire_fields(cls, "stdio"), path)
        for name, measured in asdict(expected).items():
            require(value[name] is None or type(value[name]) in (int, float), f"{path}.{name}")
            require(value[name] == measured, f"{path}.{name}")
        return cls(**value)


def _mean(values: list[int | None]) -> float | None:
    if not values or any(value is None for value in values):
        return None
    return sum(value for value in values if value is not None) / len(values)


def summarize(samples: list[dict[str, Any]]) -> dict[str, Any]:
    """Nearest-rank timings include errors; unmeasured sizes/counts stay unknown."""
    times = [row["elapsedMs"] for row in samples]
    count = len(samples)
    if not count:
        return asdict(Summary(0, None, None, None, None, None))
    return asdict(
        Summary(
            calls=count,
            p50Ms=percentile(times, 0.5),
            p95Ms=percentile(times, 0.95),
            errorRate=sum(row["error"] for row in samples) / count,
            meanResultBytes=_mean([row["resultBytes"] for row in samples]),
            meanOutboundRequests=_mean([row["outboundRequests"] for row in samples]),
        )
    )


@dataclass(frozen=True)
class Phase:
    samples: list[Sample]
    summary: Summary

    def to_dict(self, transport: Transport) -> dict[str, Any]:
        return asdict(self) | {"samples": [sample.to_dict(transport) for sample in self.samples]}

    @classmethod
    def measured(cls, samples: list[dict[str, Any]]) -> Phase:
        return cls([Sample(**row) for row in samples], Summary(**summarize(samples)))

    @classmethod
    def from_dict(
        cls, value: Any, transport: Transport, count: int, path: str, max_requests: int
    ) -> Phase:
        shape(value, wire_fields(cls, transport), path)
        rows = value["samples"]
        require(isinstance(rows, list), f"{path}.samples")
        require(
            len(rows) == count if transport == "stdio" else len(rows) <= count, f"{path}.samples"
        )
        samples = [
            Sample.from_dict(row, transport, f"{path}.samples[{index}]", max_requests=max_requests)
            for index, row in enumerate(rows)
        ]
        summary = Summary.from_dict(value["summary"], Summary(**summarize(rows)), f"{path}.summary")
        return cls(samples, summary)

    def project(self) -> dict[str, Any]:
        return {
            "summary": asdict(self.summary),
            "errors": sum(row.error for row in self.samples),
            "samples": [row.elapsedMs for row in self.samples],
        }


@dataclass(frozen=True)
class SelectedCase:
    tool: str
    arguments: dict[str, Any]
    scenario: str | None

    @classmethod
    def from_dict(cls, value: Any, path: str) -> SelectedCase:
        shape(value, wire_fields(cls, "stdio"), path)
        identifier(value["tool"], f"{path}.tool")
        require(isinstance(value["arguments"], dict), f"{path}.arguments")
        require(value["scenario"] is None or isinstance(value["scenario"], str), f"{path}.scenario")
        return cls(**value)


PHASE_LABELS = {
    "cold": "First call in new client session",
    "warm": "Warmed client session",
    "warmups": "Warm-up (excluded from measured phases)",
}


@dataclass(frozen=True)
class Case:
    selected: SelectedCase
    cold: Phase
    warm: Phase
    warmups: Phase | None = field(default=None, metadata={"transport": "streamable-http"})

    def to_dict(self, transport: Transport) -> dict[str, Any]:
        result = asdict(self.selected)
        for name, phase in self.phases().items():
            result[name] = phase.to_dict(transport)
        return result

    def phases(self) -> dict[str, Phase]:
        result = {"cold": self.cold, "warm": self.warm}
        if self.warmups is not None:
            result["warmups"] = self.warmups
        return result

    @classmethod
    def from_dict(
        cls, value: Any, transport: Transport, counts: dict[str, int], path: str, max_requests: int
    ) -> Case:
        shape(
            value,
            (wire_fields(cls, transport) - {"selected"}) | wire_fields(SelectedCase, transport),
            path,
        )
        selected = SelectedCase.from_dict(
            {name: value[name] for name in wire_fields(SelectedCase, transport)}, path
        )
        phases = {
            name: Phase.from_dict(value[name], transport, count, f"{path}.{name}", max_requests)
            for name, count in counts.items()
        }
        return cls(selected, phases["cold"], phases["warm"], phases.get("warmups"))

    def project(self) -> dict[str, Any]:
        return {"tool": self.selected.tool} | {
            name: phase.project() for name, phase in self.phases().items()
        }


@dataclass(frozen=True)
class Conditions:
    cold: str = "first measured call in a new client session; not a cold server"
    warm: str = "same client session after the recorded warm-up calls"
    timing: str = "tool-call duration; setup excluded from latency, included in budget"
    concurrency: int = 1
    remoteTermination: str = "not established by client cancellation"
    serverTelemetry: str = "unknown; no audit-fetch endpoint or trusted telemetry source"

    @classmethod
    def from_dict(cls, value: Any) -> Conditions:
        names = wire_fields(cls, "streamable-http")
        shape(value, names, "conditions")
        require(
            type(value["concurrency"]) is int and value["concurrency"] == 1,
            "conditions.concurrency",
        )
        for name in names - {"concurrency"}:
            require(
                isinstance(value[name], str) and 0 < len(value[name]) <= MAX_CONDITION_TEXT,
                f"conditions.{name}",
            )
        return cls(**value)


@dataclass(frozen=True)
class Suite:
    version: str
    fixture_set: str
    digest: str

    @classmethod
    def from_dict(cls, value: Any) -> Suite:
        shape(value, wire_fields(cls, "stdio"), "suite")
        for name in wire_fields(cls, "stdio"):
            require(isinstance(value[name], str), f"suite.{name}")
        hexadecimal(value["digest"], "suite.digest")
        return cls(**value)
