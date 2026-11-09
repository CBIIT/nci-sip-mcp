"""Strict sanitized diagnostics, separate from private request authorization and ledger."""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict, get_args

from nci_si_acceptance.request_authorization import MAX_COUNTER, Limits
from nci_si_mcp.http_client import RequestPolicyError
from nci_si_mcp.validation import SummaryState

if TYPE_CHECKING:
    from nci_si_acceptance.request_budget import LedgerSnapshot


class SummaryLimits(TypedDict):
    logical: int
    attempts: int
    per_logical: int
    concurrent: int
    seconds: float
    response_bytes: int
    total_bytes: int


class Reservations(TypedDict):
    logical: int
    attempts: int
    bodyBytes: int


class Observed(TypedDict):
    transportEntries: int
    bodyBytes: int
    peakActive: int


class RunSummary(TypedDict):
    schemaVersion: int
    runId: str
    policySha256: str
    state: SummaryState
    limits: SummaryLimits
    reservations: Reservations
    observed: Observed
    denials: None
    observationComplete: bool


STATES: dict[str, SummaryState] = {value: value for value in get_args(SummaryState.__value__)}


def _require(condition: bool) -> None:
    if not condition:
        raise RequestPolicyError("accounting") from None


def _record(value: object, keys: tuple[str, ...]) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != set(keys):
        raise RequestPolicyError("accounting") from None
    return {key: value[key] for key in keys}


def _count(value: object) -> int:
    if type(value) is not int or not 0 <= value <= MAX_COUNTER:
        raise RequestPolicyError("accounting") from None
    return value


def _identity(value: object, expected: str, length: int) -> str:
    if not isinstance(value, str) or re.fullmatch(f"[a-f0-9]{{{length}}}", value) is None:
        raise RequestPolicyError("accounting") from None
    _require(value == expected)
    return value


def _limits(value: object) -> SummaryLimits:
    row = _record(value, tuple(SummaryLimits.__annotations__))
    seconds = row["seconds"]
    if not isinstance(seconds, (int, float)) or isinstance(seconds, bool):
        raise RequestPolicyError("accounting") from None
    limits = Limits(
        logical=_count(row["logical"]),
        attempts=_count(row["attempts"]),
        per_logical=_count(row["per_logical"]),
        concurrent=_count(row["concurrent"]),
        seconds=seconds,
        response_bytes=_count(row["response_bytes"]),
        total_bytes=_count(row["total_bytes"]),
    )
    return limits_record(limits)


def limits_record(limits: Limits) -> SummaryLimits:
    return {
        "logical": limits.logical,
        "attempts": limits.attempts,
        "per_logical": limits.per_logical,
        "concurrent": limits.concurrent,
        "seconds": limits.seconds,
        "response_bytes": limits.response_bytes,
        "total_bytes": limits.total_bytes,
    }


def validate_summary(value: object, *, run_id: str, policy_sha256: str) -> RunSummary:
    row = _record(value, tuple(RunSummary.__annotations__))
    _require(type(row["schemaVersion"]) is int and row["schemaVersion"] == 1)
    _require(row["denials"] is None)
    complete = row["observationComplete"]
    if type(complete) is not bool:
        raise RequestPolicyError("accounting") from None
    state = row["state"]
    if not isinstance(state, str) or state not in STATES:
        raise RequestPolicyError("accounting") from None
    _require(state != "completed" or complete)
    reserved = _record(row["reservations"], tuple(Reservations.__annotations__))
    observed = _record(row["observed"], tuple(Observed.__annotations__))
    result: RunSummary = {
        "schemaVersion": 1,
        "runId": _identity(row["runId"], run_id, 32),
        "policySha256": _identity(row["policySha256"], policy_sha256, 64),
        "state": STATES[state],
        "limits": _limits(row["limits"]),
        "reservations": {
            "logical": _count(reserved["logical"]),
            "attempts": _count(reserved["attempts"]),
            "bodyBytes": _count(reserved["bodyBytes"]),
        },
        "observed": {
            "transportEntries": _count(observed["transportEntries"]),
            "bodyBytes": _count(observed["bodyBytes"]),
            "peakActive": _count(observed["peakActive"]),
        },
        "denials": None,
        "observationComplete": complete,
    }
    _relationships(result)
    return result


def _relationships(value: RunSummary) -> None:
    limits, reserved, observed = value["limits"], value["reservations"], value["observed"]
    _require(
        all(
            (
                reserved["logical"] <= limits["logical"],
                reserved["attempts"]
                <= min(limits["attempts"], reserved["logical"] * limits["per_logical"]),
                observed["transportEntries"] <= reserved["attempts"],
                observed["bodyBytes"] <= reserved["bodyBytes"] <= limits["total_bytes"],
                reserved["bodyBytes"] <= reserved["attempts"] * (limits["response_bytes"] + 1),
                observed["bodyBytes"]
                <= observed["transportEntries"] * (limits["response_bytes"] + 1),
                observed["peakActive"] <= min(limits["concurrent"], reserved["attempts"]),
                reserved["attempts"] == 0 or observed["peakActive"] >= 1,
                not value["observationComplete"] or reserved["bodyBytes"] == observed["bodyBytes"],
            )
        )
    )


def project(snapshot: LedgerSnapshot, limits: Limits, state: SummaryState) -> RunSummary:
    return validate_summary(
        {
            "schemaVersion": 1,
            "runId": snapshot.run_id,
            "policySha256": snapshot.policy_sha256,
            "state": state,
            "limits": limits_record(limits),
            "reservations": {
                "logical": snapshot.logical,
                "attempts": snapshot.attempts,
                "bodyBytes": snapshot.reserved_body_bytes,
            },
            "observed": {
                "transportEntries": snapshot.transport_entries,
                "bodyBytes": snapshot.consumed_body_bytes,
                "peakActive": snapshot.peak_active,
            },
            "denials": None,
            "observationComplete": snapshot.observation_complete,
        },
        run_id=snapshot.run_id,
        policy_sha256=snapshot.policy_sha256,
    )


def read_summary(path: Path, *, run_id: str, policy_sha256: str) -> RunSummary:
    if not isinstance(path, Path):
        raise RequestPolicyError("accounting") from None
    try:
        value: object = json.loads(path.read_text())
        return validate_summary(value, run_id=run_id, policy_sha256=policy_sha256)
    except OSError, ValueError, OverflowError:
        raise RequestPolicyError("accounting") from None


def publish(path: Path, summary: RunSummary) -> None:
    temporary: Path | None = None
    failures: list[BaseException] = []
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(summary, stream, allow_nan=False, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
    except OSError, ValueError:
        failures.append(RequestPolicyError("accounting"))
    except BaseException as error:
        failures.append(error)
        raise
    finally:
        _publication_cleanup(temporary, failures)


def _publication_cleanup(temporary: Path | None, failures: list[BaseException]) -> None:
    if temporary is not None:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            failures.append(RequestPolicyError("accounting"))
    if len(failures) == 1:
        raise failures[0] from None
    if failures:
        raise BaseExceptionGroup("Controlled publication failed.", failures) from None
