"""Explicit diagnostic callers own a truthful summary and a finite child lifecycle."""

import importlib
import importlib.util
import json
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

import nci_si_acceptance
import nci_si_mcp
from nci_si_acceptance import request_budget
from nci_si_acceptance.request_authorization import Authorization, Limits, RequestGrant
from nci_si_mcp.http_client import RequestPolicyError

KNOWN_BODY_BYTES = 5


def authorization():
    return Authorization((RequestGrant("GET", "http://127.0.0.1:9/probe", ()),), Limits(), "a" * 64)


def supervisor(directory):
    # Drive the new public entry point without turning its absence into collection failure.
    name = "nci_si_acceptance.request_supervision"
    assert importlib.util.find_spec(name) is not None, "An owned caller needs SupervisedRun"
    return importlib.import_module(name).SupervisedRun.create(authorization(), directory)


def test_initial_summary_precedes_admission(tmp_path):
    """Given a fresh run, entering publishes strict incomplete state before opening it."""
    owner = supervisor(tmp_path)
    with owner:
        run = owner.run
        summary = json.loads((run.directory / "summary.json").read_text())
        assert summary == {
            "schemaVersion": 1,
            "runId": run.descriptor.run_id,
            "policySha256": "a" * 64,
            "state": "incomplete",
            "limits": {
                "logical": 256,
                "attempts": 768,
                "per_logical": 3,
                "concurrent": 4,
                "seconds": 1800.0,
                "response_bytes": 1048576,
                "total_bytes": 67108864,
            },
            "reservations": {"logical": 0, "attempts": 0, "bodyBytes": 0},
            "observed": {"transportEntries": 0, "bodyBytes": 0, "peakActive": 0},
            "denials": None,
            "observationComplete": True,
        }
        assert run.snapshot().state == "open"
        assert owner.snapshot() == summary
    completed = json.loads((run.directory / "summary.json").read_text())
    assert completed == summary | {"state": "completed"}
    assert run.snapshot().state == "closed"


def test_failed_initial_publication_never_admits_work(tmp_path, monkeypatch):
    """A caller trying admission at the publication boundary must still be refused."""
    owner = supervisor(tmp_path)
    admitted = []

    def fail_replace(_source, _destination):
        try:
            with owner.run.logical(authorization().grants[0]):
                admitted.append(True)
        except RequestPolicyError:
            admitted.append(False)
        raise OSError("private publication path")

    monkeypatch.setattr("os.replace", fail_replace)
    with pytest.raises(RequestPolicyError), owner:
        pytest.fail("A failed initial publication cannot enter the caller body")
    assert admitted == [False]
    assert owner.run.snapshot().logical == 0
    assert not (owner.run.directory / "summary.json").exists()


def test_stale_owner_cannot_revive_admission(tmp_path, monkeypatch):
    owner = supervisor(tmp_path)
    with owner:
        now = request_budget._clock()
        with monkeypatch.context() as clock:
            clock.setattr(request_budget, "_clock", lambda: now + 3)
            with pytest.raises(RequestPolicyError):
                owner.run.check_wait(0)
        with pytest.raises(RequestPolicyError):
            owner.run.check_wait(0)
    assert json.loads((owner.run.directory / "summary.json").read_text())["state"] == "failed"


def test_terminal_refusal_cannot_publish_completed(tmp_path):
    owner = supervisor(tmp_path)
    with owner, pytest.raises(RequestPolicyError):
        owner.run.check_wait(1801)
    summary = json.loads((owner.run.directory / "summary.json").read_text())
    assert summary["state"] == "failed"
    assert summary["observationComplete"] is True


def test_caught_refusal_preserves_failed_state_and_five_known_bytes(tmp_path):
    entry = importlib.import_module("nci_si_acceptance.request_supervision")
    original = authorization()
    auth = Authorization(original.grants, Limits(logical=1), original.policy_sha256)
    owner = entry.SupervisedRun.create(auth, tmp_path)
    with owner:
        with (
            owner.run.logical(auth.grants[0]) as logical,
            logical.attempt(response_limit=8) as lease,
        ):
            lease.entered()
            assert lease.read(lambda _maximum: b"hello", 5) == b"hello"
        with pytest.raises(RequestPolicyError), owner.run.logical(auth.grants[0]):
            pytest.fail("The second logical request exceeds its ceiling")
    summary = json.loads((owner.run.directory / "summary.json").read_text())
    assert summary["state"] == "failed"
    assert summary["observationComplete"] is True
    assert summary["observed"]["bodyBytes"] == KNOWN_BODY_BYTES
    assert summary["reservations"]["bodyBytes"] == KNOWN_BODY_BYTES


def test_registered_short_worker_is_reaped_and_streams_closed(tmp_path):
    owner = supervisor(tmp_path)
    destination = tmp_path / "child.json"
    descriptor = tmp_path / "descriptor.json"
    descriptor.write_text(json.dumps(owner.run.descriptor.to_record()))
    roots = [str(Path(package.__file__).parents[1]) for package in (nci_si_acceptance, nci_si_mcp)]
    script = (
        "import sys,json; from pathlib import Path; "
        "sys.path[:0]=sys.argv[1:3]; import nci_si_acceptance,nci_si_mcp; "
        "sys.stdin.buffer.read(1); "
        "from nci_si_acceptance.request_authorization import "
        "Authorization,Limits,RequestGrant,RunDescriptor; "
        "from nci_si_acceptance.request_budget import ControlledRun; "
        "auth=Authorization((RequestGrant('GET','http://127.0.0.1:9/probe',()),),Limits(),'a'*64); "
        "descriptor=RunDescriptor.from_record(json.loads(Path(sys.argv[4]).read_text())); "
        "run=ControlledRun.attach(descriptor,auth,participant='worker-1'); "
        "run.check_wait(0); "
        "Path(sys.argv[3]).write_text(json.dumps([sys.executable,"
        "nci_si_acceptance.__file__,nci_si_mcp.__file__]))"
    )
    with subprocess.Popen(  # noqa: S603 - owned interpreter and explicit synthetic child program.
        [sys.executable, "-I", "-c", script, *roots, str(destination), str(descriptor)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ) as child:
        with owner:
            register = getattr(owner, "register_child", None)
            assert callable(register), "An owned run must accept explicit child custody"
            register(child, participant="worker-1", role="worker")
            child.stdin.write(b"x")
            child.stdin.flush()
            deadline = time.monotonic() + 5
            while not destination.exists():
                assert time.monotonic() < deadline, "The registered child did not finish its call"
                time.sleep(0.01)
        assert child.returncode == 0
        assert child.stdin.closed and child.stdout.closed and child.stderr.closed
    assert json.loads(destination.read_text()) == [
        sys.executable,
        nci_si_acceptance.__file__,
        nci_si_mcp.__file__,
    ]
    assert json.loads((owner.run.directory / "summary.json").read_text())["state"] == "completed"


def test_supervised_owner_cannot_open_before_initial_summary(tmp_path):
    owner = supervisor(tmp_path)
    with pytest.raises(RequestPolicyError):
        owner.run.open()
    assert owner.run.snapshot().state != "open"


def test_observer_start_failure_is_sanitized_and_closes_admission(tmp_path, monkeypatch):
    owner = supervisor(tmp_path)

    def start_failure():
        raise OSError("private thread startup diagnostic")

    monkeypatch.setattr(owner._observer, "start", start_failure)
    failure = None
    try:
        with owner:
            pytest.fail("Observer startup failed")
    except (OSError, RequestPolicyError, BaseExceptionGroup) as error:
        failure = error
    assert type(failure) is RequestPolicyError
    assert str(failure) == "Controlled upstream request refused."
    assert owner.run.snapshot().state != "open"
    assert json.loads((owner.run.directory / "summary.json").read_text())["state"] == "failed"


def test_unresolved_receipt_prevents_normal_context_return(tmp_path):
    owner = supervisor(tmp_path)

    def lost_read(_maximum):
        raise OSError("unknown read progress")

    failure = None
    try:
        with (
            owner,
            owner.run.logical(authorization().grants[0]) as logical,
            logical.attempt(response_limit=8) as lease,
            pytest.raises(RequestPolicyError),
        ):
            lease.entered()
            lease.read(lost_read, 1)
    except (RequestPolicyError, BaseExceptionGroup) as error:
        failure = error
    assert type(failure) is RequestPolicyError
    snapshot = owner.run.snapshot()
    assert snapshot.active == 1 and snapshot.pending_receipts == 1
    assert snapshot.reserved_body_bytes == len(b"123456789")
    assert snapshot.consumed_body_bytes == 0
    assert json.loads((owner.run.directory / "summary.json").read_text())["state"] == "failed"


@pytest.mark.parametrize("operation", ["enable", "open", "heartbeat", "unknown_participant"])
def test_supervision_cannot_restart_or_accept_unknown_participant(tmp_path, operation):
    owner = supervisor(tmp_path)
    with owner:
        owner.run.authorize_participant("known")
    operations = {
        "enable": owner.run.enable_supervision,
        "open": owner.run.open,
        "heartbeat": owner.run.heartbeat,
        "unknown_participant": lambda: owner.run.participant_pending("unknown"),
    }
    with pytest.raises(RequestPolicyError):
        operations[operation]()
    assert owner.run.snapshot().state != "open"


@pytest.mark.parametrize("fault", ["missing", "disabled", "duplicate", "invalid"])
def test_malformed_supervision_never_becomes_unmanaged(tmp_path, fault):
    owner = supervisor(tmp_path)
    with owner:
        pass
    queries = {
        "missing": "DELETE FROM supervision",
        "disabled": "UPDATE supervision SET enabled=0,heartbeat=NULL",
        "duplicate": "INSERT INTO supervision SELECT * FROM supervision",
        "invalid": "UPDATE supervision SET heartbeat=NULL",
    }
    with sqlite3.connect(owner.run.directory / "ledger.sqlite") as connection:
        connection.execute(queries[fault])
    connection.close()
    with pytest.raises(RequestPolicyError):
        owner.snapshot()


def test_stale_refusal_preserves_post_read_receipt(tmp_path, monkeypatch):
    owner = supervisor(tmp_path)
    with owner:
        now = request_budget._clock()
        with monkeypatch.context() as clock:

            def reader(_maximum):
                clock.setattr(request_budget, "_clock", lambda: now + 3)
                return b"hello"

            with (
                owner.run.logical(authorization().grants[0]) as logical,
                logical.attempt(response_limit=8) as lease,
                pytest.raises(RequestPolicyError),
            ):
                lease.entered()
                lease.read(reader, KNOWN_BODY_BYTES)
    summary = owner.snapshot()
    assert summary["state"] == "failed"
    assert summary["observed"]["bodyBytes"] == KNOWN_BODY_BYTES
    assert summary["reservations"]["bodyBytes"] == KNOWN_BODY_BYTES
    assert summary["observationComplete"] is True
