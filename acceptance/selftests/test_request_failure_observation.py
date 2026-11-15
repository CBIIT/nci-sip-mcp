"""Initiating accounting evidence survives cleanup without disclosing private diagnostics."""

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from xml.etree import ElementTree

import pytest
from request_failure_observation import snapshot

from nci_si_acceptance.request_authorization import Authorization, Limits, RequestGrant
from nci_si_acceptance.request_budget import ControlledRun
from nci_si_mcp.http_client import RequestPolicyError

pytest_plugins = ["pytester"]
PROPERTY = "accounting_snapshot_diagnostic"
CANARY = "private-observation-canary"


def ledger(directory):
    grant = RequestGrant("GET", "http://127.0.0.1:9/owned", ())
    return ControlledRun.create(Authorization((grant,), Limits(), "a" * 64), directory)


def retained(node):
    values = dict(node.user_properties)
    assert PROPERTY in values, "The initiating snapshot observation was not retained"
    return json.loads(values[PROPERTY])


def test_real_snapshot_retains_refusal_before_cleanup(tmp_path, request, capsys):
    run = ledger(tmp_path)
    with closing(sqlite3.connect(run.directory / "ledger.sqlite")) as blocker, blocker:
        blocker.execute("BEGIN IMMEDIATE")
        with pytest.raises(RequestPolicyError) as caught:
            snapshot(run, request.node)
        assert caught.value.reason == "accounting" and caught.value.__suppress_context__
        assert isinstance(caught.value.__context__, sqlite3.OperationalError)
        record = retained(request.node)
        assert record == {
            "complete": True,
            "chain": [{"kind": "sqlite", "code": 5, "name": "SQLITE_BUSY", "metadata": "known"}],
            "ending": "end",
        }
        assert json.loads(capsys.readouterr().err) == record
    after = run.snapshot()
    assert (after.state, after.transport_entries, after.failure) == ("failed", 0, "accounting")


def test_junit_keeps_initiating_record_after_teardown_failure(pytester):
    for name in (
        "request_failure_observation.py",
        "test_request_transport_children.py",
        "http_receiver.py",
    ):
        source = Path(__file__).with_name(name)
        (pytester.path / name).write_bytes(source.read_bytes())
    pytester.makeconftest(f'''
import sqlite3
from contextlib import closing
from threading import current_thread, main_thread
import pytest
from nci_si_acceptance.request_budget import ControlledRun
from nci_si_mcp.http_client import RequestPolicyError
PRIVATE_CANARY = "{CANARY}"
@pytest.fixture(autouse=True)
def controlled_refusal(monkeypatch):
    original = ControlledRun.snapshot
    injected = False
    def held_snapshot(run):
        nonlocal injected
        value = original(run)
        ready = value.consumed_body_bytes == 7 and value.pending_receipts == 1
        if not injected and current_thread() is main_thread() and ready:
            injected = True
            with closing(sqlite3.connect(run.directory / "ledger.sqlite")) as blocker, blocker:
                blocker.execute("BEGIN IMMEDIATE")
                try:
                    return original(run)
                except RequestPolicyError as error:
                    assert isinstance(error.__context__, sqlite3.OperationalError)
                    error.__context__.args = (PRIVATE_CANARY,)
                    raise
        return value
    monkeypatch.setattr(ControlledRun, "snapshot", held_snapshot)
    yield
    raise RuntimeError("later cleanup")
''')
    result = pytester.runpytest_subprocess(
        "test_request_transport_children.py::test_registered_children_share_transport_capacity[1-130-concurrency_limit]",
        "--junitxml=result.xml",
        "-q",
        "-W",
        "error::ResourceWarning",
        "-W",
        "error::pytest.PytestUnraisableExceptionWarning",
        "-W",
        "error::pytest.PytestUnhandledThreadExceptionWarning",
        timeout=60,
    )
    result.assert_outcomes(failed=1, errors=1)
    root = ElementTree.parse(pytester.path / "result.xml").getroot()  # noqa: S314
    properties = root.findall(f".//property[@name='{PROPERTY}']")
    assert properties, "The final JUnit report lost the initiating snapshot observation"
    records = [json.loads(prop.attrib["value"]) for prop in properties]
    assert all(
        record
        == {
            "complete": True,
            "chain": [{"kind": "sqlite", "code": 5, "name": "SQLITE_BUSY", "metadata": "known"}],
            "ending": "end",
        }
        for record in records
    )
    assert (
        CANARY
        not in result.stdout.str()
        + result.stderr.str()
        + (pytester.path / "result.xml").read_text()
    )


def refusal(context):
    error = RequestPolicyError("accounting")
    error.__context__, error.__suppress_context__ = context, True
    return error


@pytest.mark.parametrize(
    "kind,error,complete",
    [
        ("os", OSError(CANARY), True),
        ("value", ValueError(CANARY), True),
        ("type", TypeError(CANARY), True),
        ("overflow", OverflowError(CANARY), True),
        ("other", type(CANARY, (Exception,), {})(CANARY), False),
    ],
)
def test_closed_families_hide_private_content(
    tmp_path, request, monkeypatch, capsys, kind, error, complete
):
    run = ledger(tmp_path)
    initiating = refusal(error)

    def fail():
        raise initiating

    monkeypatch.setattr(run, "snapshot", fail)
    with pytest.raises(RequestPolicyError) as caught:
        snapshot(run, request.node)
    assert caught.value is initiating and caught.value.__context__ is error
    record = retained(request.node)
    assert record == {"complete": complete, "chain": [{"kind": kind}], "ending": "end"}
    assert CANARY not in json.dumps(record) + capsys.readouterr().err


@pytest.mark.parametrize(
    "code,name,expected,complete,metadata",
    [
        (5, "SQLITE_BUSY", {"kind": "sqlite", "code": 5, "name": "SQLITE_BUSY"}, True, "known"),
        (5, CANARY, {"kind": "sqlite", "code": 5}, False, "unsupported"),
        (999, "SQLITE_BUSY", {"kind": "sqlite", "code": 999}, False, "unsupported"),
        (True, "SQLITE_BUSY", {"kind": "sqlite"}, False, "unsupported"),
        (-1, "SQLITE_BUSY", {"kind": "sqlite"}, False, "unsupported"),
        (2147483648, CANARY, {"kind": "sqlite"}, False, "unsupported"),
        (None, None, {"kind": "sqlite"}, False, "absent"),
    ],
)
def test_sqlite_fields_are_closed(
    tmp_path, request, monkeypatch, capsys, code, name, expected, complete, metadata
):
    error = sqlite3.OperationalError(CANARY)
    vars(error).update(sqlite_errorcode=code, sqlite_errorname=name)
    run = ledger(tmp_path)
    initiating = refusal(error)

    def fail():
        raise initiating

    monkeypatch.setattr(run, "snapshot", fail)
    with pytest.raises(RequestPolicyError) as caught:
        snapshot(run, request.node)
    assert caught.value is initiating
    assert retained(request.node) == {
        "complete": complete,
        "chain": [dict(expected, metadata=metadata)],
        "ending": "end",
    }
    assert CANARY not in json.dumps(retained(request.node)) + capsys.readouterr().err


@pytest.mark.parametrize(
    "shape,ending,count",
    [
        ("absent", "absent", 0),
        ("cycle", "cycle", 1),
        ("long", "bound", 8),
    ],
)
def test_context_observation_is_bounded(tmp_path, request, monkeypatch, shape, ending, count):
    context = None if shape == "absent" else ValueError(CANARY)
    if shape == "cycle":
        context.__context__ = context
    if shape == "long":
        for _ in range(8):
            next_context = ValueError(CANARY)
            next_context.__context__ = context
            context = next_context
    initiating = refusal(context)
    run = ledger(tmp_path)

    def fail():
        raise initiating

    monkeypatch.setattr(run, "snapshot", fail)
    with pytest.raises(RequestPolicyError) as caught:
        snapshot(run, request.node)
    record = retained(request.node)
    assert caught.value is initiating and initiating.__context__ is context
    assert record == {"complete": False, "chain": [{"kind": "value"}] * count, "ending": ending}


def test_sink_failure_preserves_original_refusal(tmp_path, request, monkeypatch):
    run = ledger(tmp_path)
    initiating = refusal(ValueError(CANARY))

    def fail():
        raise initiating

    def broken_write(text):
        raise OSError(CANARY)

    monkeypatch.setattr(run, "snapshot", fail)
    monkeypatch.setattr("sys.stderr.write", broken_write)
    with pytest.raises(RequestPolicyError) as caught:
        snapshot(run, request.node)
    assert caught.value is initiating and initiating.reason == "accounting"
    assert retained(request.node) == {
        "complete": False,
        "chain": [{"kind": "value"}],
        "ending": "sink",
    }


def test_success_leaves_no_observation(tmp_path, request, capsys):
    run = ledger(tmp_path)
    before = run.snapshot()
    assert snapshot(run, request.node) == before
    assert PROPERTY not in dict(request.node.user_properties)
    assert capsys.readouterr().err == ""
