"""Explicit diagnostic callers own a truthful summary and a finite child lifecycle."""

import ast
import dis
import hashlib
import hmac
import importlib
import importlib.util
import inspect
import json
import signal
import sqlite3
import subprocess
import sys
import textwrap
import threading
import time
from contextlib import closing, contextmanager
from pathlib import Path

import pytest

import nci_si_acceptance
import nci_si_mcp
from nci_si_acceptance import request_budget
from nci_si_acceptance.request_authorization import Authorization, Limits, RequestGrant
from nci_si_mcp.http_client import RequestPolicyError

KNOWN_BODY_BYTES = 5
OBSERVED_FAILURE_COUNT = 2
FAILED_WORKER_EXIT = 7


def failure_leaves(error):
    if isinstance(error, BaseExceptionGroup):
        return [leaf for child in error.exceptions for leaf in failure_leaves(child)]
    return [error]


def marker_publication_failure(monkeypatch, run, active, stage="open", error=None):
    original_open = Path.open
    failures = []

    def open_marker(path, mode="r", *args, **options):
        if active.is_set() and path == run.directory / "failed" and mode == "x":
            if stage == "open":
                failures.append("marker")
                raise PermissionError("private marker publication diagnostic")
            stream = original_open(path, mode, *args, **options)

            @contextmanager
            def after_close():
                with stream:
                    yield stream
                failures.append("marker")
                if error is not None:
                    raise error
                raise OSError("synthetic post-close marker publication failure")

            return after_close()
        return original_open(path, mode, *args, **options)

    monkeypatch.setattr(Path, "open", open_marker)
    return failures


def latch_prior_failure(owner, origin):
    run = owner.run
    if origin == "attached":
        run.authorize_participant("prior")
        run = request_budget.ControlledRun.attach(
            run.descriptor, authorization(), participant="prior"
        )
    reason = "transport" if origin == "transport" else "accounting"
    with pytest.raises(RequestPolicyError) as caught:
        run._deny(reason)
    return reason, caught.value if run is owner.run else None


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


@pytest.mark.parametrize("elapsed", [0, 1.5, 2])
def test_heartbeat_refresh_preserves_inclusive_freshness_window(tmp_path, monkeypatch, elapsed):
    owner = supervisor(tmp_path)
    with owner:
        now = request_budget._clock()
        with monkeypatch.context() as clock:
            clock.setattr(request_budget, "_clock", lambda: now + 1)
            owner.run.heartbeat()
            clock.setattr(request_budget, "_clock", lambda: now + 1 + elapsed)
            refused = False
            try:
                with owner.run.logical(authorization().grants[0]):
                    pass
            except RequestPolicyError:
                refused = True
            assert refused is False
            assert owner.run.snapshot().logical == 1


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
            with pytest.raises(RequestPolicyError) as duplicate:
                register(child, participant="duplicate", role="worker")
            assert duplicate.value.reason == "authorization"
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


def test_zero_exit_worker_with_pending_read_refuses_live_admission(tmp_path, monkeypatch):
    owner = supervisor(tmp_path)
    descriptor = tmp_path / "descriptor.json"
    descriptor.write_text(json.dumps(owner.run.descriptor.to_record()))
    roots = [str(Path(package.__file__).parents[1]) for package in (nci_si_acceptance, nci_si_mcp)]
    script = (
        "import sys,json,os; from pathlib import Path; sys.path[:0]=sys.argv[1:3]; "
        "sys.stdin.buffer.read(1); "
        "from nci_si_acceptance.request_authorization import "
        "Authorization,Limits,RequestGrant,RunDescriptor; "
        "from nci_si_acceptance.request_budget import ControlledRun; "
        "auth=Authorization((RequestGrant('GET','http://127.0.0.1:9/probe',()),),Limits(),'a'*64); "
        "run=ControlledRun.attach(RunDescriptor.from_record(json.loads(Path(sys.argv[3]).read_text())),"
        "auth,participant='worker'); logical=run.logical(auth.grants[0]); "
        "request=logical.__enter__(); attempt=request.attempt(response_limit=8); "
        "lease=attempt.__enter__(); lease.entered(); lease.read(lambda _maximum:os._exit(0),1)"
    )
    observed = threading.Event()
    original_pulse = owner._pulse
    with subprocess.Popen(  # noqa: S603 - owned interpreter, fixed program and private synthetic IPC.
        [sys.executable, "-I", "-c", script, *roots, str(descriptor)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ) as child:

        def pulse():
            exited = child.poll() is not None
            try:
                original_pulse()
            finally:
                if exited:
                    observed.set()

        monkeypatch.setattr(owner, "_pulse", pulse)
        with pytest.raises(RequestPolicyError) as terminal, owner:
            owner.register_child(child, participant="worker", role="worker")
            child.stdin.write(b"x")
            child.stdin.flush()
            assert child.wait(timeout=5) == 0
            assert observed.wait(3), "The actual observer did not inspect the exited worker"
            admitted = False
            try:
                with owner.run.logical(authorization().grants[0]):
                    admitted = True
            except RequestPolicyError:
                pass
            assert admitted is False
            snapshot = owner.run.snapshot()
            assert (snapshot.logical, snapshot.attempts) == (1, 1)
        assert terminal.value.reason == "accounting"
        assert child.stdin.closed and child.stdout.closed and child.stderr.closed
    snapshot = owner.run.snapshot()
    assert (snapshot.state, snapshot.active, snapshot.pending_receipts) == ("failed", 1, 1)
    assert (snapshot.reserved_body_bytes, snapshot.consumed_body_bytes) == (9, 0)
    assert owner.snapshot()["state"] == "failed" and not snapshot.observation_complete


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


@pytest.mark.parametrize(
    "fault",
    ["missing", "disabled", "duplicate", "invalid", "null", "range", "unmanaged", "infinite"],
)
def test_malformed_supervision_never_becomes_unmanaged(tmp_path, fault):
    owner = supervisor(tmp_path)
    with owner:
        pass
    queries = {
        "missing": "DELETE FROM supervision",
        "disabled": "UPDATE supervision SET enabled=0,heartbeat=NULL",
        "duplicate": "INSERT INTO supervision SELECT * FROM supervision",
        "invalid": "UPDATE supervision SET heartbeat=NULL",
        "null": "UPDATE supervision SET enabled=NULL",
        "range": "UPDATE supervision SET enabled=2",
        "unmanaged": "UPDATE supervision SET enabled=0",
        "infinite": "UPDATE supervision SET heartbeat=9e999",
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
@pytest.mark.parametrize("observed", [False, True])
@pytest.mark.parametrize("prior", [False, True])
@pytest.mark.parametrize("marker_stage", [None, "open", "after_close"])
def test_unexpected_real_child_exit_closes_admission(
    tmp_path, monkeypatch, role, code, observed, prior, marker_stage
):
    owner = supervisor(tmp_path)
    active = threading.Event()
    marker_errors = marker_publication_failure(monkeypatch, owner.run, active, marker_stage)
    script = "import sys; sys.stdin.buffer.read(1); sys.exit(int(sys.argv[1]))"
    with subprocess.Popen(  # noqa: S603 - fixed child program, owned interpreter.
        [sys.executable, "-I", "-c", script, str(code)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ) as child:
        failure = None
        try:
            with owner:
                owner.register_child(child, participant="child", role=role)
                if prior:
                    with pytest.raises(RequestPolicyError):
                        owner.run._deny("transport")
                if marker_stage is not None:
                    active.set()
                child.stdin.write(b"x")
                child.stdin.flush()
                child.wait(timeout=5)
                if observed:
                    assert owner._stop.wait(3), "The actual observer did not report terminal state"
        except (RequestPolicyError, BaseExceptionGroup) as error:
            failure = error
        record_failed = {None: 0, "open": 1, "after_close": int(not prior)}[marker_stage]
        expected = ["transport"] * (1 + prior) + ["accounting"] * record_failed
        assert type(failure) is (ExceptionGroup if len(expected) > 1 else RequestPolicyError)
        errors = failure_leaves(failure)
        assert [error.reason for error in errors] == expected
        assert marker_errors == ["marker"] * record_failed
        assert child.returncode == code
        assert child.stdin.closed
        assert child.stdout.closed
        assert child.stderr.closed
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
        with pytest.raises(RequestPolicyError) as failure, owner:
            owner.register_child(child, participant="service", role="service")
        assert failure.value.reason == "closed"
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


@pytest.mark.parametrize("record_fault", [False, True])
def test_real_observer_inspection_failure_is_terminal_and_sanitized(
    tmp_path, monkeypatch, record_fault
):
    owner = supervisor(tmp_path)
    inspected = threading.Event()
    thread_errors = []
    monkeypatch.setattr(threading, "excepthook", lambda event: thread_errors.append(event.exc_type))
    original_mark = owner.run._mark_failed
    active = threading.Event()
    marker_errors = marker_publication_failure(monkeypatch, owner.run, active)
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
                if record_fault:
                    original_mark("transport")
                    active.set()
                raise OSError("private observer inspection diagnostic")
            return original_poll()

        monkeypatch.setattr(child, "poll", fail_observer_inspection)
        failure = None
        try:
            with owner:
                owner.register_child(child, participant="worker", role="worker")
                assert inspected.wait(3), "The real observer did not inspect its child"
                child.communicate(b"x", timeout=5)
                owner._observer.join(timeout=2.5)
                assert not owner._observer.is_alive()
        except (RequestPolicyError, BaseExceptionGroup) as error:
            failure = error
        assert type(failure) is (ExceptionGroup if record_fault else RequestPolicyError)
        errors = failure.exceptions if record_fault else (failure,)
        assert [error.reason for error in errors] == (
            ["transport", "accounting", "accounting"] if record_fault else ["accounting"]
        )
        assert thread_errors == []
        assert marker_errors == ["marker"] * record_fault
        assert child.returncode == 0
    assert owner.snapshot()["state"] == "failed"


def test_uncaught_child_observation_remains_the_initiating_event(tmp_path):
    owner = supervisor(tmp_path)
    with subprocess.Popen([sys.executable, "-I", "-c", "raise SystemExit(7)"]) as child:
        initiating = None
        with pytest.raises(RequestPolicyError) as caught, owner:
            owner.register_child(child, participant="failed", role="worker")
            child.wait(timeout=5)
            try:
                owner._pulse()
            except RequestPolicyError as error:
                initiating = error
                raise
        assert caught.value is initiating
        assert caught.value.reason == "transport"
    assert owner.snapshot()["state"] == "failed"


def test_competing_duplicate_offers_transfer_one_child(tmp_path, monkeypatch):
    owner = supervisor(tmp_path)
    barrier = threading.Barrier(2)
    original = owner._registered
    results = []

    def competing(process, participant):
        registered = original(process, participant)
        barrier.wait(timeout=3)
        return registered

    monkeypatch.setattr(owner, "_registered", competing)
    with subprocess.Popen(
        [sys.executable, "-I", "-c", "import sys; sys.stdin.buffer.read(1)"], stdin=subprocess.PIPE
    ) as child:

        def register(participant):
            try:
                owner.register_child(child, participant=participant, role="service")
                results.append("accepted")
            except RequestPolicyError:
                results.append("rejected")

        with owner:
            threads = [threading.Thread(target=register, args=(name,)) for name in ("one", "two")]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=5)
                assert not thread.is_alive()
            assert sorted(results) == ["accepted", "rejected"]
            assert len(owner._children) == 1 and owner._children[0].process is child
        assert child.poll() is not None and child.stdin.closed


@pytest.mark.parametrize("prior", [None, "transport", "accounting", "attached"])
@pytest.mark.parametrize("marker_stage", [None, "open", "after_close"])
def test_one_close_failure_is_one_event_with_prior_failure_preserved(
    tmp_path, monkeypatch, prior, marker_stage
):
    owner = supervisor(tmp_path)
    failed = []
    active = threading.Event()
    marker_errors = marker_publication_failure(monkeypatch, owner.run, active, marker_stage)

    def fail_close():
        failed.append("close")
        if marker_stage is not None:
            active.set()
        raise OSError("private close diagnostic")

    monkeypatch.setattr(owner.run, "close", fail_close)
    failure = None
    prior_error = None
    prior_reason = None
    try:
        with owner:
            if prior:
                prior_reason, prior_error = latch_prior_failure(owner, prior)
    except (RequestPolicyError, BaseExceptionGroup) as error:
        failure = error
    record_failed = {None: 0, "open": 1, "after_close": int(prior is None)}[marker_stage]
    expected = ["accounting"] * (1 + record_failed)
    if prior:
        expected.insert(0, prior_reason)
    assert type(failure) is (ExceptionGroup if len(expected) > 1 else RequestPolicyError)
    errors = failure_leaves(failure)
    if prior_error is not None:
        assert errors[0] is prior_error
    assert [error.reason for error in errors] == expected
    assert failed == ["close"]
    assert marker_errors == ["marker"] * record_failed
    assert not owner._observer.is_alive()
    assert owner.run.snapshot().state == "failed"
    assert owner.snapshot()["state"] == "failed"


@pytest.mark.parametrize("prior", [False, True])
def test_real_close_contention_preserves_original_failure_events(tmp_path, monkeypatch, prior):
    owner = supervisor(tmp_path)
    original_close = owner.run.close
    lock = sqlite3.connect(owner.run.directory / "ledger.sqlite")
    close_errors, prior_errors = [], []

    def close_and_release():
        try:
            original_close()
        except RequestPolicyError as error:
            close_errors.append(error)
            raise
        finally:
            lock.rollback()
            lock.close()

    monkeypatch.setattr(owner.run, "close", close_and_release)
    failure = None
    try:
        with owner:
            if prior:
                _reason, error = latch_prior_failure(owner, "accounting")
                prior_errors.append(error)
            lock.execute("BEGIN IMMEDIATE")
    except (RequestPolicyError, BaseExceptionGroup) as error:
        failure = error
    assert type(failure) is (ExceptionGroup if prior else RequestPolicyError)
    errors = failure.exceptions if prior else (failure,)
    assert list(errors) == prior_errors + close_errors
    assert len(close_errors) == 1 and close_errors[0].reason == "accounting"
    assert not owner._observer.is_alive()
    assert owner.run.snapshot().state == "failed" and owner.snapshot()["state"] == "failed"


@pytest.mark.parametrize("reason,delay", [("deadline", 1801), ("accounting", 0)])
def test_later_marker_publication_keeps_earlier_local_latch(tmp_path, monkeypatch, reason, delay):
    owner = supervisor(tmp_path)
    active = threading.Event()
    active.set()
    marker_errors = marker_publication_failure(monkeypatch, owner.run, active)
    close_calls = []

    def fail_close():
        close_calls.append("close")
        raise OSError("synthetic later close failure")

    monkeypatch.setattr(owner.run, "close", fail_close)
    failure = None
    try:
        with owner:
            with closing(sqlite3.connect(owner.run.directory / "ledger.sqlite")) as lock:
                if reason == "accounting":
                    lock.execute("BEGIN IMMEDIATE")
                try:
                    with pytest.raises(RequestPolicyError) as inner:
                        owner.run.check_wait(delay)
                finally:
                    lock.rollback()
            assert inner.value.reason == "accounting"
            assert owner.run.snapshot().failure == reason
            active.clear()
    except (RequestPolicyError, BaseExceptionGroup) as error:
        failure = error
    assert type(failure) is ExceptionGroup
    assert [error.reason for error in failure_leaves(failure)] == [reason, "accounting"]
    assert (owner.run.directory / "failed").read_text() == reason
    assert marker_errors == ["marker"] and close_calls == ["close"]
    assert not owner._observer.is_alive() and owner.snapshot()["state"] == "failed"


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
        with pytest.raises(RequestPolicyError) as failure:
            entry.SupervisedRun(attached)
        assert failure.value.reason == "authorization"
        assert path.read_bytes() == original
    assert owner.snapshot()["state"] == "completed"


def test_unenrolled_constructor_rejection_preserves_unmanaged_caller(tmp_path):
    entry = importlib.import_module("nci_si_acceptance.request_supervision")
    run = request_budget.ControlledRun.create(authorization(), tmp_path)
    failure = None
    try:
        entry.SupervisedRun(run)
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
    with pytest.raises(RequestPolicyError) as failure:
        entry.SupervisedRun(owner.run)
    assert failure.value.reason == "authorization"
    assert path.read_bytes() == original


@pytest.mark.parametrize("owner_state", ["paused", "lost"])
def test_actual_controller_loss_refuses_attached_participant(tmp_path, owner_state):
    descriptor = tmp_path / "controller.json"
    roots = [str(Path(package.__file__).parents[1]) for package in (nci_si_acceptance, nci_si_mcp)]
    script = """
import sys,json
from pathlib import Path
sys.path[:0] = sys.argv[1:3]
from nci_si_acceptance.request_authorization import Authorization,Limits,RequestGrant
from nci_si_acceptance.request_supervision import SupervisedRun
auth=Authorization((RequestGrant('GET','http://127.0.0.1:9/probe',()),),Limits(),'a'*64)
with SupervisedRun.create(auth,Path(sys.argv[4])) as owner:
    owner.run.authorize_participant('participant')
    temporary=Path(sys.argv[3]+'.tmp')
    temporary.write_text(json.dumps(owner.run.descriptor.to_record()))
    temporary.replace(sys.argv[3])
    sys.stdin.buffer.read(1)
"""
    with subprocess.Popen(  # noqa: S603 - explicit owned controller with injected source roots.
        [sys.executable, "-I", "-c", script, *roots, str(descriptor), str(tmp_path / "runs")],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ) as controller:
        deadline = time.monotonic() + 5
        while not descriptor.exists():
            assert time.monotonic() < deadline and controller.poll() is None
            time.sleep(0.01)
        run = request_budget.ControlledRun.attach(
            request_budget.RunDescriptor.from_record(json.loads(descriptor.read_text())),
            authorization(),
            participant="participant",
        )
        if owner_state == "paused":
            controller.send_signal(signal.SIGSTOP)
        else:
            controller.terminate()
            controller.wait(timeout=5)
        try:
            time.sleep(3)  # Independent headroom beyond the declared two-second stale window.
            with pytest.raises(RequestPolicyError) as caught:
                run.check_wait(0)
            assert caught.value.reason == "accounting"
            assert run.snapshot().logical == 0
        finally:
            if owner_state == "paused":
                controller.send_signal(signal.SIGCONT)
            controller.communicate(b"x", timeout=5)
        assert controller.returncode != 0
    expected = "failed" if owner_state == "paused" else "incomplete"
    assert json.loads((run.directory / "summary.json").read_text())["state"] == expected


@pytest.mark.parametrize("operation", ["heartbeat", "pending", "snapshot"])
def test_new_supervision_operations_recheck_private_identity(tmp_path, operation):
    owner = supervisor(tmp_path)
    actions = {
        "heartbeat": owner.run.heartbeat,
        "pending": lambda: owner.run.participant_pending("registered"),
        "snapshot": owner.snapshot,
    }
    with pytest.raises(RequestPolicyError), owner:
        owner.run.authorize_participant("registered")
        actions[operation]()
        with closing(sqlite3.connect(owner.run.directory / "ledger.sqlite")) as db, db:
            db.execute("UPDATE run SET policy=?", ("b" * 64,))
        try:
            with pytest.raises(RequestPolicyError) as caught:
                actions[operation]()
            assert caught.value.reason == "authorization"
        finally:
            with closing(sqlite3.connect(owner.run.directory / "ledger.sqlite")) as db, db:
                db.execute("UPDATE run SET policy=?", ("a" * 64,))


@pytest.mark.parametrize("role", ["worker", "service"])
def test_actual_unreaped_child_failure_preserves_initiating_exception(tmp_path, monkeypatch, role):
    owner = supervisor(tmp_path)
    module = importlib.import_module("nci_si_acceptance.request_supervision")
    original_stop = module.stop_child
    initiating = KeyError("initiating caller failure")

    def fail_reap(_process, *, seconds):
        raise subprocess.TimeoutExpired(["owned-child"], seconds)

    monkeypatch.setattr(module, "stop_child", fail_reap)
    monkeypatch.setattr(module, "STOP_SECONDS", 0.02)
    with subprocess.Popen(
        [sys.executable, "-I", "-c", "import time; time.sleep(30)"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ) as child:
        try:
            with pytest.raises(BaseExceptionGroup) as caught, owner:
                owner.register_child(child, participant="service", role=role)
                raise initiating
            assert tuple(type(error) for error in caught.value.exceptions) == (
                KeyError,
                ExceptionGroup if role == "worker" else RequestPolicyError,
            )
            reasons = [error.reason for error in failure_leaves(caught.value)[1:]]
            assert reasons == (["transport", "accounting"] if role == "worker" else ["accounting"])
            assert caught.value.exceptions[0] is initiating
            assert child.poll() is None
            assert owner.snapshot()["state"] == "failed"
        finally:
            original_stop(child, seconds=0.05)
        assert child.poll() is not None


def test_paused_real_observer_cannot_bypass_shutdown_budget(tmp_path, monkeypatch):
    owner = supervisor(tmp_path)
    arrived, release, leaving, finished = (threading.Event() for _ in range(4))
    original_pulse = owner._pulse
    failures = []

    def paused_pulse():
        original_pulse()
        arrived.set()
        assert release.wait(3), "The caller did not release its owned observer"

    def run():
        try:
            with owner:
                assert arrived.wait(3), "The real observer did not reach its pulse"
                leaving.set()
        except (RequestPolicyError, BaseExceptionGroup) as error:
            failures.append(error)
        finally:
            finished.set()

    monkeypatch.setattr(owner, "_pulse", paused_pulse)
    monkeypatch.setattr("nci_si_acceptance.request_supervision.JOIN_SECONDS", 0.05)
    controller = threading.Thread(target=run)
    controller.start()
    try:
        assert leaving.wait(3)
        assert finished.wait(0.5), "Observer pause bypassed the bounded shutdown failure"
        assert owner.run.snapshot().state != "open"
        assert len(failures) == 1 and type(failures[0]) is RequestPolicyError
        assert owner.snapshot()["state"] == "failed"
        assert owner.snapshot()["observationComplete"] is True
    finally:
        release.set()
        controller.join(timeout=3)
        owner._observer.join(timeout=3)
        assert not controller.is_alive() and not owner._observer.is_alive()


@pytest.mark.parametrize("role", ["unknown", None, [], 1])
def test_invalid_role_keeps_child_with_caller(tmp_path, role):
    owner = supervisor(tmp_path)
    with subprocess.Popen(
        [sys.executable, "-I", "-c", "import sys; sys.stdin.buffer.read(1)"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ) as child:
        with owner:
            with pytest.raises(RequestPolicyError):
                owner.register_child(child, participant="rejected", role=role)
            assert child.poll() is None
            child.communicate(b"x", timeout=5)
        assert child.returncode == 0


@pytest.mark.parametrize("operation", ["heartbeat", "pending"])
def test_attached_participant_cannot_control_owner_observation(tmp_path, operation):
    owner = supervisor(tmp_path)
    with pytest.raises(RequestPolicyError) as outer, owner:
        owner.run.authorize_participant("attached")
        attached = request_budget.ControlledRun.attach(
            owner.run.descriptor, authorization(), participant="attached"
        )
        action = (
            attached.heartbeat
            if operation == "heartbeat"
            else lambda: attached.participant_pending("attached")
        )
        with pytest.raises(RequestPolicyError):
            action()
    assert outer.value.reason == "authorization"


def pause_registration_return(call, phase, selected, arrived, release):
    def paused(*args):
        result = call(*args)
        if phase == selected:
            arrived.set()
            assert release.wait(3)
        return result

    return paused


@pytest.mark.parametrize("point", ["authorization", "registry"])
def test_registration_race_preserves_caller_custody_and_shutdown_bound(
    tmp_path, monkeypatch, point
):
    owner = supervisor(tmp_path)
    arrived, release, finished = (threading.Event() for _ in range(3))
    registrations, shutdown = [], []
    monkeypatch.setattr(
        owner.run,
        "authorize_participant",
        pause_registration_return(
            owner.run.authorize_participant, "authorization", point, arrived, release
        ),
    )
    monkeypatch.setattr(
        owner,
        "_registered",
        pause_registration_return(owner._registered, "registry", point, arrived, release),
    )
    with subprocess.Popen(
        [sys.executable, "-I", "-c", "import sys; sys.stdin.buffer.read(1)"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ) as child:

        def register():
            outcome = None
            try:
                owner.register_child(child, participant="racing", role="service")
            except RequestPolicyError as error:
                outcome = error.reason
            registrations.append(outcome)

        registrar = threading.Thread(target=register)

        def control():
            try:
                with owner:
                    registrar.start()
                    assert arrived.wait(3)
            except (RequestPolicyError, BaseExceptionGroup) as error:
                shutdown.append(type(error))
            finally:
                finished.set()

        controller = threading.Thread(target=control)
        controller.start()
        try:
            assert arrived.wait(3)
            assert finished.wait(0.5), "Registration blocked bounded admission closure"
        finally:
            release.set()
            registrar.join(timeout=3)
            controller.join(timeout=3)
            assert not registrar.is_alive() and not controller.is_alive()
        assert registrations == ["authorization"]
        assert shutdown == []
        assert child.poll() is None
        child.communicate(b"x", timeout=5)
        assert child.returncode == 0


def handoff_line(method, point):
    source, first = inspect.getsourcelines(method)
    nodes = ast.walk(ast.parse(textwrap.dedent("".join(source))))
    line = (
        first
        + next(
            node.lineno
            for node in nodes
            if isinstance(node, ast.Attribute) and node.attr == "append"
        )
        - 1
    )
    if point == "after":
        # Exception-table bytecode can have no source line.
        offsets = dict(dis.findlinestarts(method.__func__.__code__))
        line = min(value for value in filter(None, offsets.values()) if value > line)
    return line


@contextmanager
def pause_handoff(method, point, registrar, arrived, release, *, line=None):
    if line is None:
        line = handoff_line(method, point)
    monitor = sys.monitoring
    tool = next(value for value in (3, 4) if monitor.get_tool(value) is None)

    def pause(_code, current):
        if threading.current_thread() is registrar and current == line:
            arrived.set()
            assert release.wait(3), "The caller did not release its registrar"

    monitor.use_tool_id(tool, "owned-registration-schedule")
    try:
        monitor.register_callback(tool, monitor.events.LINE, pause)
        monitor.set_local_events(tool, method.__func__.__code__, monitor.events.LINE)
        yield
    finally:
        monitor.free_tool_id(tool)


@pytest.mark.parametrize("point", ["before", "after"])
def test_handoff_append_order_decides_custody_during_shutdown(tmp_path, monkeypatch, point):
    owner = supervisor(tmp_path)
    arrived, release, finished = (threading.Event() for _ in range(3))
    registrations, shutdown = [], []
    monkeypatch.setattr("nci_si_acceptance.request_supervision.JOIN_SECONDS", 0.05)
    monkeypatch.setattr("nci_si_acceptance.request_supervision.STOP_SECONDS", 0.2)
    with subprocess.Popen(
        [sys.executable, "-I", "-c", "import sys; sys.stdin.buffer.read(1)"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ) as child:

        def register():
            result = "accepted"
            try:
                owner.register_child(child, participant="racing", role="service")
            except RequestPolicyError:
                result = "rejected"
            registrations.append(result)

        registrar = threading.Thread(target=register)

        def control():
            try:
                with owner:
                    registrar.start()
                    assert arrived.wait(3)
            except (RequestPolicyError, BaseExceptionGroup) as error:
                shutdown.append(type(error))
            finally:
                finished.set()

        controller = threading.Thread(target=control)
        with pause_handoff(owner.register_child, point, registrar, arrived, release):
            controller.start()
            try:
                assert arrived.wait(3)
                assert finished.wait(1.5), "The registrar blocked bounded shutdown"
            finally:
                release.set()
                registrar.join(timeout=3)
                controller.join(timeout=3)
                assert not registrar.is_alive() and not controller.is_alive()
        assert registrations == ["accepted" if point == "after" else "rejected"]
        assert owner.snapshot()["state"] == ("failed" if shutdown else "completed")
        if point == "before":
            assert child.poll() is None
            child.communicate(b"x", timeout=5)
            assert child.returncode == 0
        else:
            assert child.returncode is not None and child.returncode != 0


@pytest.mark.parametrize("run", [None, object()])
def test_constructor_refuses_foreign_run_before_attribute_access(run):
    entry = importlib.import_module("nci_si_acceptance.request_supervision")
    with pytest.raises(RequestPolicyError) as caught:
        entry.SupervisedRun(run)
    assert caught.value.reason == "authorization"


def test_reentry_is_closed_and_preserves_completed_summary(tmp_path):
    owner = supervisor(tmp_path)
    with owner:
        pass
    path = owner.run.directory / "summary.json"
    original = path.read_bytes()
    with pytest.raises(RequestPolicyError) as caught, owner:
        pytest.fail("A completed context cannot reenter")
    assert caught.value.reason == "closed" and path.read_bytes() == original


def test_unmanaged_heartbeat_refuses_implicit_supervision(tmp_path):
    run = request_budget.ControlledRun.create(authorization(), tmp_path)
    run.open()
    with pytest.raises(RequestPolicyError) as caught:
        run.heartbeat()
    assert caught.value.reason == "authorization"
    assert not (run.directory / "summary.json").exists()
    run.close()


def test_competing_local_failure_keeps_first_completed_latch(tmp_path, monkeypatch):
    owner = supervisor(tmp_path)
    active = threading.Event()
    active.set()
    marker_errors = marker_publication_failure(monkeypatch, owner.run, active)
    arrived, release = threading.Event(), threading.Event()
    worker_errors = []

    def actor():
        try:
            owner.run.check_wait(float("nan"))
        except RequestPolicyError as error:
            worker_errors.append(error.reason)

    thread = threading.Thread(target=actor)
    source, first = inspect.getsourcelines(owner.run._mark_failed)
    statement = ast.parse(textwrap.dedent("".join(source))).body[0].body[0]
    if isinstance(statement, ast.If):
        statement = statement.body[0]
    line = first + statement.lineno - 1
    with (
        pytest.raises(RequestPolicyError) as outer,
        owner,
        pause_handoff(owner.run._mark_failed, "before", thread, arrived, release, line=line),
    ):
        thread.start()
        try:
            assert arrived.wait(3), "The actor did not reach its first-latch boundary"
            with pytest.raises(RequestPolicyError) as inner:
                owner.run.check_wait(1801)
            assert inner.value.reason == "accounting"
            assert owner.run.snapshot().failure == "deadline"
        finally:
            release.set()
            thread.join(timeout=3)
            assert not thread.is_alive()
        assert owner.run.snapshot().failure == "deadline"
    assert outer.value.reason == "deadline"
    assert worker_errors == ["accounting"]
    assert marker_errors == ["marker", "marker"]
    assert not owner._observer.is_alive()


def test_marker_interruption_keeps_initiating_close_failure(tmp_path, monkeypatch):
    owner = supervisor(tmp_path)
    active = threading.Event()
    interruption = KeyboardInterrupt("synthetic marker context interruption")
    marker_errors = marker_publication_failure(
        monkeypatch, owner.run, active, stage="after_close", error=interruption
    )

    def fail_close():
        active.set()
        raise OSError("synthetic initiating close failure")

    monkeypatch.setattr(owner.run, "close", fail_close)
    failure = None
    try:
        with owner:
            pass
    except (RequestPolicyError, BaseExceptionGroup, KeyboardInterrupt) as error:
        failure = error
    errors = failure_leaves(failure)
    assert [type(error) for error in errors] == [RequestPolicyError, KeyboardInterrupt]
    assert errors[0].reason == "accounting"
    assert errors[1] is interruption
    assert marker_errors == ["marker"] and not owner._observer.is_alive()
    assert json.loads((owner.run.directory / "summary.json").read_text())["state"] == "failed"


def test_suspended_closure_cannot_keep_renewing_caller_authority(tmp_path, monkeypatch):
    owner = supervisor(tmp_path)
    opened, leaving, closing, release = (threading.Event() for _ in range(4))
    outcomes = []
    original_close = owner.run.close

    def suspended_close():
        closing.set()
        assert release.wait(8), "The caller did not release admission closure"
        original_close()

    def control():
        try:
            with owner:
                owner.run.authorize_participant("caller")
                opened.set()
                assert leaving.wait(5), "The caller did not request shutdown"
        except (RequestPolicyError, BaseExceptionGroup) as error:
            outcomes.append(error)

    monkeypatch.setattr(owner.run, "close", suspended_close)
    controller = threading.Thread(target=control)
    controller.start()
    try:
        assert opened.wait(3), "The controller did not open its run"
        caller = request_budget.ControlledRun.attach(
            owner.run.descriptor, authorization(), participant="caller"
        )
        leaving.set()
        assert closing.wait(3), "The controller did not begin closure"
        deadline = time.monotonic() + 3
        owner._observer.join(timeout=3)
        time.sleep(max(0, deadline - time.monotonic()))
        with (
            pytest.raises(RequestPolicyError) as refused,
            caller.logical(authorization().grants[0]),
        ):
            pass
        assert refused.value.reason == "accounting"
        assert caller.snapshot().logical == 0
    finally:
        leaving.set()
        release.set()
        controller.join(timeout=8)
        if owner._observer.ident is not None:
            owner._observer.join(timeout=3)
        assert not controller.is_alive() and not owner._observer.is_alive()
    assert [error.reason for error in outcomes] == ["accounting"]
    assert owner.snapshot()["state"] == "failed"


@pytest.mark.parametrize("state", ["failed", "completed"])
def test_initial_summary_must_be_incomplete_before_admission(tmp_path, state):
    owner = supervisor(tmp_path)
    path = owner.run.directory / "summary.json"
    value = owner.snapshot() | {"state": state}
    path.write_text(json.dumps(value))
    reader = importlib.import_module("nci_si_acceptance.request_summary").read_summary
    assert reader(path, run_id=owner.run.descriptor.run_id, policy_sha256="a" * 64) == value
    try:
        with pytest.raises(RequestPolicyError) as caught:
            owner.run.open()
        assert caught.value.reason == "accounting"
        assert owner.run.snapshot().state == "failed"
        assert owner.run.snapshot().logical == 0
    finally:
        owner.run.close()


@pytest.mark.parametrize("enabled,heartbeat", [(2, 1.0), (0, 1.0)])
def test_matching_identity_cannot_hide_invalid_supervision(tmp_path, enabled, heartbeat):
    run = request_budget.ControlledRun.create(authorization(), tmp_path)
    key = (run.directory / "key").read_bytes()
    # Independent wire-format expectation: authorization binding plus compact identity JSON.
    identity = json.dumps([run.descriptor.run_id, run.started, enabled], separators=(",", ":"))
    binding = hmac.digest(key, authorization().binding(key) + identity.encode(), hashlib.sha256)
    with closing(sqlite3.connect(run.directory / "ledger.sqlite")) as connection, connection:
        connection.execute("UPDATE supervision SET enabled=?,heartbeat=?", (enabled, heartbeat))
        connection.execute("UPDATE run SET binding=?", (binding,))
    with pytest.raises(RequestPolicyError) as failure:
        run.snapshot()
    assert failure.value.reason == "accounting"


def test_future_heartbeat_refuses_admission(tmp_path, monkeypatch):
    owner = supervisor(tmp_path)
    with pytest.raises(RequestPolicyError) as outer, owner:
        now = request_budget._clock()
        with monkeypatch.context() as clock:
            clock.setattr(request_budget, "_clock", lambda: now + 1)
            owner.run.heartbeat()
            clock.setattr(request_budget, "_clock", lambda: now + 0.5)
            with (
                pytest.raises(RequestPolicyError) as failure,
                owner.run.logical(authorization().grants[0]),
            ):
                pytest.fail("Future heartbeat authorized a logical request")
            assert failure.value.reason == "accounting"
    assert outer.value.reason == "accounting"
    assert owner.run.snapshot().logical == 0


@pytest.mark.parametrize("fault", ["nonowner", "enabled", "open", "closed", "failed"])
def test_enable_supervision_requires_fresh_controller(tmp_path, fault):
    run = request_budget.ControlledRun.create(authorization(), tmp_path)
    selected = run
    if fault == "nonowner":
        selected = request_budget.ControlledRun(run.descriptor, authorization(), "controller")
    if fault == "enabled":
        run.enable_supervision()
    if fault in ("open", "closed"):
        run.open()
    if fault == "closed":
        run.close()
    if fault == "failed":
        with pytest.raises(RequestPolicyError):
            run.check_wait(0)
    try:
        with pytest.raises(RequestPolicyError) as failure:
            selected.enable_supervision()
        assert failure.value.reason == "authorization"
    finally:
        run.close()


@pytest.mark.parametrize("fault", ["failed", "stale", "closed"])
def test_supervision_entry_rechecks_created_owner(tmp_path, monkeypatch, fault):
    owner = supervisor(tmp_path)
    if fault == "failed":
        with pytest.raises(RequestPolicyError):
            owner.run.check_wait(0)
    if fault == "closed":
        owner.run.close()
    if fault == "stale":
        now = request_budget._clock()
        monkeypatch.setattr(request_budget, "_clock", lambda: now + 3)
    try:
        with pytest.raises(RequestPolicyError) as failure, owner:
            pytest.fail("Invalid created owner entered supervision")
        assert failure.value.reason == ("accounting" if fault == "stale" else "authorization")
        assert not (owner.run.directory / "summary.json").exists()
    finally:
        owner.run.close()


def test_nonowner_created_handle_cannot_own_supervision(tmp_path):
    owner = supervisor(tmp_path)
    other = request_budget.ControlledRun(owner.run.descriptor, authorization(), "controller")
    try:
        with pytest.raises(RequestPolicyError) as failure:
            other.require_supervision_owner()
        assert failure.value.reason == "authorization"
        assert owner.snapshot()["state"] == "incomplete"
    finally:
        owner.run.close()


@pytest.mark.parametrize("participant", [1, True])
def test_pending_participant_requires_text_identity(tmp_path, participant):
    owner = supervisor(tmp_path)
    with owner:
        owner.run.authorize_participant("1")
        assert owner.run.participant_pending("1") is False
        with pytest.raises(RequestPolicyError) as failure:
            owner.run.participant_pending(participant)
        assert failure.value.reason == "authorization"
    assert owner.snapshot()["state"] == "completed"


def test_closed_heartbeat_preserves_completed_record(tmp_path):
    owner = supervisor(tmp_path)
    with owner:
        pass
    before = owner.snapshot()
    path = owner.run.directory / "summary.json"
    contents = path.read_bytes()
    with pytest.raises(RequestPolicyError) as failure:
        owner.run.heartbeat()
    assert failure.value.reason == "closed"
    assert owner.snapshot() == before
    assert path.read_bytes() == contents
    assert not (owner.run.directory / "failed").exists()


def test_two_distinct_owned_workers_complete_and_close_streams(tmp_path):
    owner = supervisor(tmp_path)
    command = [sys.executable, "-I", "-c", "import sys; sys.stdin.buffer.read(1)"]
    with (
        subprocess.Popen(  # noqa: S603 - owned interpreter and fixed synthetic barrier program.
            command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        ) as first,
        subprocess.Popen(  # noqa: S603 - owned interpreter and fixed synthetic barrier program.
            command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        ) as second,
    ):
        failure = None
        try:
            try:
                with owner:
                    try:
                        owner.register_child(first, participant="first", role="worker")
                        owner.register_child(second, participant="second", role="worker")
                    finally:
                        first.stdin.write(b"x")
                        first.stdin.flush()
                        second.stdin.write(b"x")
                        second.stdin.flush()
            except (RequestPolicyError, BaseExceptionGroup) as error:
                failure = error
            assert failure is None
            assert owner.snapshot()["state"] == "completed"
            assert first.returncode == second.returncode == 0
            assert all(
                stream.closed
                for child in (first, second)
                for stream in (child.stdin, child.stdout, child.stderr)
            )
        finally:
            for child in (first, second):
                if child.poll() is None:
                    child.kill()
                child.wait(timeout=5)


def test_unreleased_attempt_refuses_clean_context_return(tmp_path):
    owner = supervisor(tmp_path)
    logical_context = attempt_context = None
    try:
        with pytest.raises(RequestPolicyError) as failure, owner:
            logical_context = owner.run.logical(authorization().grants[0])
            logical = logical_context.__enter__()
            attempt_context = logical.attempt(response_limit=8)
            attempt_context.__enter__()
        assert failure.value.reason == "accounting"
        assert owner.snapshot()["state"] == "failed"
        assert owner.snapshot()["observationComplete"] is False
    finally:
        if attempt_context is not None:
            attempt_context.__exit__(None, None, None)
        if logical_context is not None:
            logical_context.__exit__(None, None, None)


def test_inflight_real_heartbeat_after_closure_preserves_completion(tmp_path, monkeypatch):
    owner = supervisor(tmp_path)
    arrived, release = threading.Event(), threading.Event()
    original_heartbeat, original_join = owner.run.heartbeat, owner._join

    def paused_heartbeat():
        arrived.set()
        assert release.wait(3), "Caller did not release owned observer pulse"
        original_heartbeat()

    def release_at_join():
        release.set()
        original_join()

    monkeypatch.setattr(owner.run, "heartbeat", paused_heartbeat)
    monkeypatch.setattr(owner, "_join", release_at_join)
    failure = None
    try:
        try:
            with owner:
                assert arrived.wait(3), "Real observer never reached heartbeat"
        except (RequestPolicyError, BaseExceptionGroup) as error:
            failure = error
        assert failure is None
        assert owner.snapshot()["state"] == "completed"
        assert not (owner.run.directory / "failed").exists()
    finally:
        release.set()
        owner._observer.join(timeout=3)
        assert not owner._observer.is_alive()


@pytest.mark.parametrize("corrupt", [False, True])
def test_terminal_validation_precedes_failure_selection(tmp_path, monkeypatch, corrupt):
    owner = supervisor(tmp_path)
    original_join = owner._join

    def corrupt_after_closure():
        original_join()
        if corrupt:
            with (
                closing(sqlite3.connect(owner.run.directory / "ledger.sqlite")) as connection,
                connection,
            ):
                connection.execute("UPDATE run SET logical=-1")
        (owner.run.directory / "failed").write_text("transport")

    monkeypatch.setattr(owner, "_join", corrupt_after_closure)
    with pytest.raises((RequestPolicyError, BaseExceptionGroup)) as failure, owner:
        pass
    assert [leaf.reason for leaf in failure_leaves(failure.value)] == (
        ["accounting", "accounting"] if corrupt else ["transport"]
    )
    assert not owner._observer.is_alive()


def test_unrelated_body_group_keeps_observed_child_failure(tmp_path):
    owner = supervisor(tmp_path)
    original = KeyError("synthetic initiating failure")
    body = ExceptionGroup("caller group", [original])
    with subprocess.Popen(
        [sys.executable, "-I", "-c", "import sys; sys.stdin.buffer.read(1); sys.exit(7)"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ) as child:
        with pytest.raises(ExceptionGroup) as failure, owner:
            owner.register_child(child, participant="failed-worker", role="worker")
            child.communicate(b"x", timeout=5)
            assert owner._stop.wait(3), "Real observer did not observe failed owned child"
            raise body
        assert failure.value.exceptions[0] is body
        leaves = failure_leaves(failure.value)
        assert leaves[0] is original
        assert len(leaves) == OBSERVED_FAILURE_COUNT
        assert type(leaves[1]) is RequestPolicyError and leaves[1].reason == "transport"
        assert child.returncode == FAILED_WORKER_EXIT
    assert owner.snapshot()["state"] == "failed"
