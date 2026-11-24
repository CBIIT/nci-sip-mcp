"""Failure-only evidence at the registered-child snapshot boundary."""

import json
import linecache
import sqlite3
import sys
import time
from typing import Literal, NotRequired, TypedDict

import pytest

from nci_sip_acceptance.request_budget import SQLITE_NAMES, ControlledRun, LedgerSnapshot
from nci_sip_mcp.http_client import RequestPolicyError

PROPERTY = "accounting_snapshot_diagnostic"
ATTEMPT_PROPERTY = "accounting_snapshot_attempt"
# Enough for the accounting refusal and a failed marker; never traverse unrelated graphs.
MAX_CONTEXTS = 8
MAX_SQLITE_CODE = 2147483647  # Bound SQLite integer metadata to a signed 32-bit code.
type Ending = Literal["end", "absent", "cycle", "bound", "sink"]
type Kind = Literal["sqlite", "os", "value", "type", "overflow", "other"]
FAMILIES: dict[type[BaseException], Kind] = {
    sqlite3.Error: "sqlite",
    OSError: "os",
    ValueError: "value",
    TypeError: "type",
    OverflowError: "overflow",
}


class Entry(TypedDict):
    kind: Kind
    code: NotRequired[int]
    name: NotRequired[str]
    metadata: NotRequired[Literal["absent", "unsupported", "known"]]


class Observation(TypedDict):
    complete: bool
    chain: list[Entry]
    ending: Ending


def _sqlite(entry: Entry, error: BaseException) -> None:
    attributes = BaseException.__dict__["__dict__"].__get__(error)
    code, name = attributes.get("sqlite_errorcode"), attributes.get("sqlite_errorname")
    entry["metadata"] = "absent" if code is None and name is None else "unsupported"
    if type(code) is int and 0 <= code <= MAX_SQLITE_CODE:
        entry["code"] = code
        if type(name) is str and SQLITE_NAMES.get(code) == name:
            entry["name"], entry["metadata"] = name, "known"


def _entry(error: BaseException) -> Entry:
    entry: Entry = {"kind": "other"}
    for base, kind in FAMILIES.items():
        if isinstance(error, base):
            entry["kind"] = kind
            break
    if isinstance(error, sqlite3.Error):
        _sqlite(entry, error)
    return entry


def _finish(record: Observation, ending: Ending) -> Observation:
    record["ending"] = ending
    if ending != "end":
        record["complete"] = False
    return record


def _observe(error: RequestPolicyError) -> Observation:
    record: Observation = {"complete": True, "chain": [], "ending": "end"}
    context = BaseException.__dict__["__context__"].__get__(error)
    seen: set[int] = set()
    for _ in range(MAX_CONTEXTS):
        if context is None:
            return _finish(record, "end" if record["chain"] else "absent")
        if id(context) in seen:
            return _finish(record, "cycle")
        seen.add(id(context))
        entry = _entry(context)
        record["chain"].append(entry)
        record["complete"] &= entry["kind"] != "other" and entry.get("metadata", "known") == "known"
        context = BaseException.__dict__["__context__"].__get__(context)
    return _finish(record, "end" if context is None else "bound")


def _publish(record: Observation, node: pytest.Item) -> None:
    try:
        node.user_properties.append((PROPERTY, json.dumps(record)))
    except OSError, ValueError, TypeError:
        _finish(record, "sink")
    try:
        sys.stderr.write(json.dumps(record) + "\n")
        sys.stderr.flush()
    except OSError, ValueError, TypeError:
        _finish(record, "sink")
        for index, (key, _) in enumerate(node.user_properties):
            if key == PROPERTY:
                node.user_properties[index] = (PROPERTY, json.dumps(record))


class Attempt(TypedDict):
    elapsed_seconds: float
    function: NotRequired[str]
    line: NotRequired[int]
    statement: NotRequired[str]


def _attempt(error: RequestPolicyError, elapsed: float) -> Attempt:
    """Name the ledger statement that failed from the traceback the refusal already carries."""
    attempt: Attempt = {"elapsed_seconds": round(elapsed, 3)}
    context = BaseException.__dict__["__context__"].__get__(error)
    trace = None if context is None else context.__traceback__
    frames = []
    while trace is not None:
        frames.append(trace)
        trace = trace.tb_next
    ledger = [t for t in frames if t.tb_frame.f_code.co_filename.endswith("request_budget.py")]
    if ledger:
        innermost = ledger[-1]
        code = innermost.tb_frame.f_code
        attempt["function"] = code.co_qualname
        attempt["line"] = innermost.tb_lineno
        attempt["statement"] = linecache.getline(code.co_filename, innermost.tb_lineno).strip()
    return attempt


def _publish_attempt(attempt: Attempt, node: pytest.Item) -> None:
    text = json.dumps(attempt)
    node.user_properties.append((ATTEMPT_PROPERTY, text))
    node.add_report_section("call", ATTEMPT_PROPERTY, text)


def snapshot(run: ControlledRun, node: pytest.Item) -> LedgerSnapshot:
    started = time.monotonic()
    try:
        return run.snapshot()
    except RequestPolicyError as error:
        elapsed = time.monotonic() - started
        _publish(_observe(error), node)
        _publish_attempt(_attempt(error, elapsed), node)
        raise
