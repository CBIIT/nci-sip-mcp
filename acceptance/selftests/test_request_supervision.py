"""Explicit diagnostic callers own a truthful summary and a finite child lifecycle."""

import importlib
import importlib.util
import json
import sqlite3
import subprocess
import sys
import threading
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
    with pytest.raises(RequestPolicyError), owner:
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
    with pytest.raises(RequestPolicyError), owner, pytest.raises(RequestPolicyError):
        owner.run.check_wait(1801)
    summary = json.loads((owner.run.directory / "summary.json").read_text())
    assert summary["state"] == "failed"
    assert summary["observationComplete"] is True


def test_caught_refusal_preserves_failed_state_and_five_known_bytes(tmp_path):
    entry = importlib.import_module("nci_si_acceptance.request_supervision")
    original = authorization()
    auth = Authorization(original.grants, Limits(logical=1), original.policy_sha256)
    owner = entry.SupervisedRun.create(auth, tmp_path)
    with pytest.raises(RequestPolicyError), owner:
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
    with pytest.raises(RequestPolicyError), owner:
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


def test_live_observer_after_join_timeout_prevents_completed_success(tmp_path, monkeypatch):
    owner = supervisor(tmp_path)
    release = threading.Event()
    owner._observer = threading.Thread(target=lambda: release.wait(5))
    monkeypatch.setattr("nci_si_acceptance.request_supervision.JOIN_SECONDS", 0.05)
    started = time.monotonic()
    try:
        with (
            pytest.raises(RequestPolicyError),
            owner,
            owner.run.logical(authorization().grants[0]) as logical,
            logical.attempt(response_limit=8) as lease,
        ):
            lease.entered()
            assert lease.read(lambda _maximum: b"hello", KNOWN_BODY_BYTES) == b"hello"
        assert time.monotonic() - started < 1
        summary = json.loads((owner.run.directory / "summary.json").read_text())
        assert summary["state"] == "failed"
        assert summary["observationComplete"] is True
        assert summary["observed"]["bodyBytes"] == KNOWN_BODY_BYTES
        assert owner.run.snapshot().pending_receipts == 0
    finally:
        release.set()
        owner._observer.join(timeout=1)
        assert not owner._observer.is_alive()


def test_initiating_and_cleanup_failures_remain_in_order(tmp_path, monkeypatch):
    owner = supervisor(tmp_path)
    initiating = KeyError("initiating caller failure")
    original_join = owner._join

    def failed_join():
        original_join()
        raise RequestPolicyError("accounting")

    monkeypatch.setattr(owner, "_join", failed_join)
    with pytest.raises(BaseExceptionGroup) as caught, owner:
        raise initiating
    assert caught.value.exceptions[0] is initiating
    assert type(caught.value.exceptions[1]) is RequestPolicyError
    assert not owner._observer.is_alive()
    assert json.loads((owner.run.directory / "summary.json").read_text())["state"] == "failed"


@pytest.mark.parametrize("role,code", [("worker", 7), ("service", 0)])
def test_unexpected_real_child_exit_closes_admission(tmp_path, role, code):
    owner = supervisor(tmp_path)
    script = "import sys; sys.stdin.buffer.read(1); sys.exit(int(sys.argv[1]))"
    with subprocess.Popen(  # noqa: S603 - fixed child program, owned interpreter.
        [sys.executable, "-I", "-c", script, str(code)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ) as child:
        with pytest.raises((RequestPolicyError, BaseExceptionGroup)), owner:
            owner.register_child(child, participant="child", role=role)
            child.stdin.write(b"x")
            child.stdin.flush()
            child.wait(timeout=5)
        assert child.returncode == code
        assert child.stdin.closed and child.stdout.closed and child.stderr.closed
    assert owner.run.snapshot().state == "failed"
    assert owner.snapshot()["state"] == "failed"


def test_close_precedes_actual_owned_service_stop(tmp_path, monkeypatch):
    owner = supervisor(tmp_path)
    module = importlib.import_module("nci_si_acceptance.request_supervision")
    original_stop = module.stop_child
    admitted = []

    def observe_stop(process, *, seconds):
        try:
            with owner.run.logical(authorization().grants[0]):
                admitted.append(True)
        except RequestPolicyError:
            admitted.append(False)
        original_stop(process, seconds=seconds)

    monkeypatch.setattr(module, "stop_child", observe_stop)
    with subprocess.Popen(
        [sys.executable, "-I", "-c", "import sys; sys.stdin.buffer.read()"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ) as child:
        with pytest.raises(RequestPolicyError), owner:
            owner.register_child(child, participant="service", role="service")
        assert child.returncode is not None
    assert admitted == [False]
    assert owner.run.snapshot().logical == 0


def test_registration_during_closure_leaves_rejected_child_with_caller(tmp_path, monkeypatch):
    owner = supervisor(tmp_path)
    rejected = []
    original_close = owner.run.close
    with subprocess.Popen(
        [sys.executable, "-I", "-c", "import sys; sys.stdin.buffer.read(1)"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ) as child:

        def closing():
            with pytest.raises(RequestPolicyError):
                owner.register_child(child, participant="too-late", role="worker")
            rejected.append(child.poll() is None)
            original_close()

        monkeypatch.setattr(owner.run, "close", closing)
        with owner:
            pass
        assert rejected == [True]
        assert child.poll() is None
        child.communicate(b"x", timeout=5)
        assert child.returncode == 0


def test_admission_closes_before_observer_stop_signal(tmp_path, monkeypatch):
    owner = supervisor(tmp_path)
    original_set = owner._stop.set
    admitted = []

    def observe_stop_signal():
        try:
            with owner.run.logical(authorization().grants[0]):
                admitted.append(True)
        except RequestPolicyError:
            admitted.append(False)
        original_set()

    monkeypatch.setattr(owner._stop, "set", observe_stop_signal)
    try:
        with owner:
            pass
    except RequestPolicyError:
        pass  # The deliberately probed closed admission is itself terminal (F102-7).
    assert admitted == [False]
    assert owner.run.snapshot().logical == 0


@pytest.mark.parametrize("observed", [False, True])
def test_caught_refusal_has_one_stable_outer_reason(tmp_path, observed):
    owner = supervisor(tmp_path)
    with pytest.raises(RequestPolicyError) as outer, owner:
        with pytest.raises(RequestPolicyError):
            owner.run.check_wait(1801)
        if observed:
            assert owner._stop.wait(3), "The real observer did not observe terminal refusal"
    assert outer.value.reason == "deadline"
    assert owner.snapshot()["state"] == "failed"


def test_real_observer_inspection_failure_is_terminal_and_sanitized(tmp_path, monkeypatch):
    owner = supervisor(tmp_path)
    inspected = threading.Event()
    thread_errors = []
    monkeypatch.setattr(threading, "excepthook", lambda event: thread_errors.append(event.exc_type))
    with subprocess.Popen(
        [sys.executable, "-I", "-c", "import sys; sys.stdin.buffer.read(1)"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ) as child:
        original_poll = child.poll

        def fail_observer_inspection():
            if threading.current_thread() is owner._observer and not inspected.is_set():
                inspected.set()
                raise OSError("private observer inspection diagnostic")
            return original_poll()

        monkeypatch.setattr(child, "poll", fail_observer_inspection)
        failure = None
        try:
            with owner:
                owner.register_child(child, participant="worker", role="worker")
                assert inspected.wait(3), "The real observer did not inspect its child"
                child.communicate(b"x", timeout=5)
        except (RequestPolicyError, BaseExceptionGroup) as error:
            failure = error
        assert type(failure) is RequestPolicyError
        assert failure.reason == "accounting"
        assert thread_errors == []
        assert child.returncode == 0
    assert owner.snapshot()["state"] == "failed"


def test_attached_constructor_cannot_change_controller_summary(tmp_path):
    owner = supervisor(tmp_path)
    entry = importlib.import_module("nci_si_acceptance.request_supervision")
    with owner:
        owner.run.authorize_participant("attached")
        attached = request_budget.ControlledRun.attach(
            owner.run.descriptor, authorization(), participant="attached"
        )
        path = owner.run.directory / "summary.json"
        original = path.read_bytes()
        with pytest.raises((RequestPolicyError, BaseExceptionGroup)), entry.SupervisedRun(attached):
            pytest.fail("An attached participant cannot enter controller supervision")
        assert path.read_bytes() == original
    assert owner.snapshot()["state"] == "completed"


def test_unenrolled_constructor_rejection_preserves_unmanaged_caller(tmp_path):
    entry = importlib.import_module("nci_si_acceptance.request_supervision")
    run = request_budget.ControlledRun.create(authorization(), tmp_path)
    failure = None
    try:
        with entry.SupervisedRun(run):
            pass
    except (RequestPolicyError, BaseExceptionGroup) as error:
        failure = error
    assert type(failure) is RequestPolicyError
    assert not (run.directory / "summary.json").exists()
    run.open()
    assert run.snapshot().state == "open"
    run.close()


def test_closed_constructor_cannot_overwrite_completed_summary(tmp_path):
    owner = supervisor(tmp_path)
    entry = importlib.import_module("nci_si_acceptance.request_supervision")
    with owner:
        pass
    path = owner.run.directory / "summary.json"
    original = path.read_bytes()
    with pytest.raises((RequestPolicyError, BaseExceptionGroup)), entry.SupervisedRun(owner.run):
        pytest.fail("A completed run cannot restart")
    assert path.read_bytes() == original
