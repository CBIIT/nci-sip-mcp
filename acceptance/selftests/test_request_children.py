"""Receipts have run-local authority and require actual outside-thread settlement."""

import json
import subprocess
import sys
import threading
import time
from contextlib import nullcontext
from types import SimpleNamespace

import pytest
from test_request_supervision import authorization, failure_leaves

from nci_si_acceptance import request_children
from nci_si_acceptance.request_children import CleanupDeadline, Completions, CompletionToken
from nci_si_acceptance.request_stdio import _PortalCall
from nci_si_acceptance.request_supervision import SupervisedRun
from nci_si_mcp.errors import RequestPolicyError


@pytest.fixture
def calls():
    owned = []

    def create(operation):
        call = _PortalCall(operation)
        owned.append(call)
        return call

    yield create
    for call in owned:
        if call.thread.ident is not None:
            call.thread.join(2)
        assert not call.thread.is_alive()


@pytest.mark.parametrize("release_before_cleanup", [False, True])
def test_zero_http_leases_do_not_settle_an_owned_thread(
    tmp_path, monkeypatch, calls, release_before_cleanup
):
    entered, release = threading.Event(), threading.Event()

    def operation():
        entered.set()
        release.wait()
        return "settled"

    call = calls(operation)
    owner = SupervisedRun.create(authorization(), tmp_path)
    if not release_before_cleanup:
        monkeypatch.setattr(request_children, "CLEANUP_SECONDS", 0.05)
    expected = nullcontext() if release_before_cleanup else pytest.raises(BaseExceptionGroup)
    try:
        with expected, owner:
            owner.register_completion(call, participant="owned-thread")
            call.start()
            assert entered.wait(1)
            snapshot = owner.run.snapshot()
            assert (snapshot.active, snapshot.pending_receipts, snapshot.transport_entries) == (
                0,
                0,
                0,
            )
            assert owner.snapshot()["observationComplete"] is False
            if release_before_cleanup:
                release.set()
        assert owner.snapshot()["observationComplete"] is release_before_cleanup
        assert owner.pending_owners == (() if release_before_cleanup else (call.name,))
        assert owner.snapshot()["state"] == ("completed" if release_before_cleanup else "failed")
    finally:
        release.set()


@pytest.mark.parametrize("corruption", ["run_id", "policy_sha256", "participant", "clone", "type"])
def test_foreign_or_changed_receipt_cannot_clear_an_owner(calls, corruption):
    registry = Completions("b" * 32, "a" * 64)
    call = calls(lambda: "completed")
    token = registry.register(call, "participant")
    call.start()
    original = token.run_id, token.policy_sha256, token.participant

    class Untrusted:
        def __hash__(self):
            raise AssertionError("unsupported receipt was used before type validation")

    offer = token
    if corruption in ("run_id", "policy_sha256", "participant"):
        object.__setattr__(token, corruption, "foreign")
    else:
        offer = CompletionToken(*original) if corruption == "clone" else Untrusted()
    try:
        with pytest.raises(RequestPolicyError) as denied:
            registry.settle(offer, CleanupDeadline())
        assert denied.value.reason == "authorization"
        assert registry.pending == (call.name,)
    finally:
        for field, value in zip(("run_id", "policy_sha256", "participant"), original, strict=True):
            object.__setattr__(token, field, value)
    registry.settle(token, CleanupDeadline())
    assert registry.pending == () and call.result.result() == "completed"


def test_completion_cleanup_sees_closed_admission(tmp_path):
    owner = SupervisedRun.create(authorization(), tmp_path / "run")
    admitted = []

    class Completion:
        name = "admission-witness"

        def finish(self, _deadline):
            try:
                with owner.run.logical(authorization().grants[0]):
                    admitted.append(True)
            except RequestPolicyError:
                admitted.append(False)

    try:
        with owner:
            owner.register_completion(Completion(), participant="witness")
    except RequestPolicyError:
        pass  # The real refused admission is terminal, even during cleanup.
    assert admitted == [False]
    assert owner.run.snapshot().logical == 0
    assert owner.pending_owners == ()


@pytest.mark.parametrize(("end", "now"), [(float("inf"), 1.0), (2.0, float("nan"))])
def test_nonfinite_cleanup_time_refuses_accounting(monkeypatch, end, now):
    monkeypatch.setattr(request_children, "time", SimpleNamespace(monotonic=lambda: now))
    with pytest.raises(RequestPolicyError) as refused:
        CleanupDeadline(end).remaining()
    assert refused.value.reason == "accounting"


def test_owned_call_start_failure_preserves_its_cleanup_receipt(calls, monkeypatch):
    call = calls(lambda: "unused")
    initiating = RuntimeError("owned thread start failure")

    def refuse_start():
        raise initiating

    monkeypatch.setattr(call.thread, "start", refuse_start)
    observed = []
    for operation in (call.start, lambda: call.finish(CleanupDeadline(time.monotonic() + 0.05))):
        try:
            operation()
        except Exception as error:  # noqa: BLE001 - compare both actual operation outcomes below.
            observed.append(error)
    assert observed == [initiating, initiating]
    assert call.started.exception(timeout=0) is initiating
    assert call.thread.ident is None


def test_completion_registration_before_entry_refuses_without_custody(tmp_path, calls):
    owner = SupervisedRun.create(authorization(), tmp_path / "run")
    call = calls(lambda: "unused")
    with pytest.raises(RequestPolicyError) as refused:
        owner.register_completion(call, participant="early")
    assert refused.value.reason == "authorization"
    assert owner.pending_owners == ()
    assert call.thread.ident is None


def test_successful_controlled_child_releases_actual_custody(tmp_path, calls):
    owner = SupervisedRun.create(authorization(), tmp_path / "run")
    call = calls(lambda: "settled")
    with subprocess.Popen(
        [sys.executable, "-I", "-c", "pass"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ) as child:
        failure = None
        try:
            with owner:
                assert child.wait(timeout=3) == 0
                owner.register_child(child, participant="worker", role="worker")
                owner.register_completion(call, participant="caller")
                call.start()
        except RequestPolicyError as error:
            failure = error.reason
        assert failure is None
        assert child.returncode == 0
        assert all(stream.closed for stream in (child.stdin, child.stdout, child.stderr))
        assert not call.thread.is_alive() and call.result.result() == "settled"
        assert owner.pending_owners == ()
        summary = json.loads((owner.run.directory / "summary.json").read_text())
        assert summary["state"] == "completed" and summary["observationComplete"] is True


def test_sealed_receipt_is_one_use_and_callbacks_do_not_hold_registry_lock(calls):
    registry = Completions("b" * 32, "a" * 64)
    entered, release, finishing = threading.Event(), threading.Event(), threading.Event()

    def observe():
        entered.set()
        release.wait()
        return registry.pending

    call = calls(observe)
    token = registry.register(call, "participant")
    settler = None
    try:
        call.start()
        assert entered.wait(1)
        with pytest.raises(RequestPolicyError):
            registry.register(call, "duplicate")
        assert registry.seal() == (token,)
        with pytest.raises(RequestPolicyError):
            registry.register(calls(lambda: "late"), "participant")
        original_finish = call.finish

        def finish(deadline):
            finishing.set()
            original_finish(deadline)

        call.finish = finish
        errors = []

        def settle():
            try:
                registry.settle(token, CleanupDeadline(time.monotonic() + 0.25))
            except BaseException as error:  # noqa: BLE001 - assert original callback outcome below.
                errors.append(error)

        settler = threading.Thread(target=settle, name="owned-receipt-settler")
        settler.start()
        assert finishing.wait(1)
        release.set()
        settler.join(1)
        assert not settler.is_alive() and errors == []
        assert call.result.result() == (call.name,) and registry.pending == ()
        with pytest.raises(RequestPolicyError) as repeated:
            registry.settle(token, CleanupDeadline())
        assert repeated.value.reason == "authorization"
    finally:
        release.set()
        if settler is not None and settler.ident is not None:
            settler.join(21)
            assert not settler.is_alive()


def test_shutdown_cannot_miss_an_accepted_owner_before_registration_returns(
    tmp_path, monkeypatch, calls
):
    owner = SupervisedRun.create(authorization(), tmp_path)
    call = calls(lambda: "unused")
    accepted, release = threading.Event(), threading.Event()
    original = owner._completions.register

    def register(resource, participant):
        token = original(resource, participant)
        accepted.set()
        if not release.wait(3):
            raise TimeoutError("owned registration safety release expired")
        return token

    monkeypatch.setattr(owner._completions, "register", register)
    monkeypatch.setattr(request_children, "CLEANUP_SECONDS", 0.05)
    errors = []

    def enroll():
        try:
            owner.register_completion(call, participant="owned")
        except BaseException as error:  # noqa: BLE001 - preserve the actor's original outcome.
            errors.append(error)

    actor = threading.Thread(target=enroll, name="owned-registration-actor")
    outward = None
    try:
        try:
            with owner:
                actor.start()
                assert accepted.wait(1)
        except BaseException as error:  # noqa: BLE001 - assert the exact failed ownership outcome.
            outward = error
        report = json.loads((owner.run.directory / "summary.json").read_text())
        assert report["observationComplete"] is False
        assert report["state"] == "failed" and isinstance(outward, BaseExceptionGroup)
    finally:
        release.set()
        actor.join(2)
        assert not actor.is_alive() and errors == []


def test_clock_failure_still_closes_admission_and_never_restarts_cleanup_time(
    tmp_path, monkeypatch, calls
):
    owner = SupervisedRun.create(authorization(), tmp_path)
    entered, release = threading.Event(), threading.Event()
    interrupt = KeyboardInterrupt("owned clock interruption")

    def operation():
        entered.set()
        release.wait()

    def failed_clock():
        raise interrupt

    call = calls(operation)
    outward = None
    try:
        try:
            with owner:
                owner.register_completion(call, participant="owned")
                call.start()
                assert entered.wait(1)
                monkeypatch.setattr(
                    request_children, "time", SimpleNamespace(monotonic=failed_clock)
                )
        except BaseException as error:  # noqa: BLE001 - preserve the original outward failure.
            outward = error
        assert owner.run.snapshot().state in {"closed", "failed"}
        assert outward is not None and owner.snapshot()["state"] == "failed"
        assert failure_leaves(outward)[0] is interrupt
        deadline = owner.cleanup_deadline()
        assert deadline.remaining() == 0 and owner.cleanup_deadline() is deadline
    finally:
        release.set()
        owner._stop.set()
        owner._observer.join(2)
        assert not owner._observer.is_alive()
