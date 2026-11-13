"""The creator/retained-owner boundary survives refusal and uncertain close completion."""

import io
import sys
import threading
from dataclasses import dataclass
from typing import TextIO

import pytest
from test_request_supervision import authorization, failure_leaves

from nci_si_acceptance import request_children
from nci_si_acceptance.client import open_session
from nci_si_acceptance.request_stdio import StderrTransfer
from nci_si_acceptance.request_supervision import SupervisedRun
from nci_si_mcp.errors import RequestPolicyError


@pytest.mark.parametrize("boundary", ["closed", "sealed"])
def test_refusal_before_acceptance_keeps_creator_custody(tmp_path, boundary):
    stream = (tmp_path / "stderr.log").open("w")
    transfer = StderrTransfer(stream)
    owner = SupervisedRun.create(authorization(), tmp_path / "run")
    try:
        with owner:
            if boundary == "sealed":
                owner.register_completion(transfer, participant="stderr")
        with pytest.raises(RequestPolicyError) as refused:
            if boundary == "sealed":
                transfer.accept(threading.Thread())
            else:
                with open_session(
                    [sys.executable, "-c", "pass"],
                    {},
                    stream,
                    owner=owner,
                    stderr_transfer=transfer,
                ):
                    raise AssertionError("closed controller started a session")
        assert refused.value.reason == ("authorization" if boundary == "sealed" else "closed")
        assert not transfer.accepted and not stream.closed
    finally:
        stream.close()
    assert stream.closed


@dataclass(frozen=True, slots=True)
class TransferCase:
    owner: SupervisedRun
    transfer: StderrTransfer
    stream: TextIO
    writer: threading.Thread
    entered: threading.Event
    finished: threading.Event
    failures: list[BaseException]
    initiating: LookupError


@pytest.fixture
def transfer_case(tmp_path, monkeypatch, fault):
    release, entered, finished = (threading.Event() for _ in range(3))
    stream = (tmp_path / "stderr.log").open("w")
    actual_close = stream.close
    transfer = StderrTransfer(stream)
    owner = SupervisedRun.create(authorization(), tmp_path / "run")
    initiating = LookupError("owned initiating failure")
    writer = threading.Thread(target=release.wait if fault == "writer" else lambda: None)

    def close():
        if fault == "blocked-close":
            entered.set()
            release.wait()
        actual_close()
        if fault == "failed-close":
            raise OSError("owned file close failed")

    failures = []

    def run():
        try:
            with owner:
                owner.register_completion(transfer, participant="stderr")
                transfer.accept(writer)
                if fault == "failed-close":
                    raise initiating
        except BaseException as error:  # noqa: BLE001 - preserve the actual outer failure below.
            failures.append(error)
        finally:
            finished.set()

    finisher = threading.Thread(target=run, name="owned-stderr-finisher")
    monkeypatch.setattr(stream, "close", close)
    monkeypatch.setattr(request_children, "CLEANUP_SECONDS", 0.05)
    try:
        writer.start()
        if fault != "writer":
            writer.join(2)
            assert not writer.is_alive()
        finisher.start()
        yield TransferCase(owner, transfer, stream, writer, entered, finished, failures, initiating)
    finally:
        release.set()
        owner._stop.set()
        closer = transfer._closer
        threads = [finisher, writer, owner._observer] + (
            [closer.thread] if closer is not None else []
        )
        for thread in threads:
            if thread.ident is not None:
                thread.join(3)
            assert not thread.is_alive()
        actual_close()


@pytest.mark.parametrize("fault", ["writer", "blocked-close", "failed-close"])
def test_uncertain_or_failed_close_retains_named_custody(transfer_case, fault):
    case = transfer_case
    assert case.finished.wait(0.5), "final file close blocked the outward cleanup"
    assert len(case.failures) == 1 and isinstance(case.failures[0], BaseExceptionGroup)
    assert case.transfer.accepted and not case.transfer.closed
    assert case.owner.pending_owners == ("controlled-stderr",)
    assert (
        case.owner.snapshot()["state"] == "failed"
        and not case.owner.snapshot()["observationComplete"]
    )
    assert case.stream.closed is (fault == "failed-close")
    if fault == "writer":
        assert case.transfer._closer is None
    if fault == "blocked-close":
        assert case.entered.wait(1)
    if fault == "failed-close":
        leaves = failure_leaves(case.failures[0])
        assert leaves[0] is case.initiating
        assert any(
            isinstance(error, RequestPolicyError) and error.reason == "accounting"
            for error in leaves
        )


def test_accepted_child_startup_error_closes_the_transferred_file(tmp_path):
    stream = (tmp_path / "stderr.log").open("w")
    transfer = StderrTransfer(stream)
    owner = SupervisedRun.create(authorization(), tmp_path / "run")
    try:
        with (
            pytest.raises(BaseExceptionGroup),
            owner,
            open_session(
                [sys.executable, "-c", "raise SystemExit(7)"],
                {},
                stream,
                owner=owner,
                timeout=1,
                stderr_transfer=transfer,
            ),
        ):
            raise AssertionError("failed child yielded an initialized session")
        assert transfer.accepted and transfer.closed and stream.closed
        assert owner.pending_owners == () and owner.snapshot()["state"] == "failed"
    finally:
        stream.close()


@pytest.mark.parametrize("record", ["foreign", "other-stream", "spent"])
def test_invalid_transfer_refuses_before_new_resource_enrollment(tmp_path, record):
    stream = (tmp_path / "stderr.log").open("w")
    transfer = StderrTransfer(stream)
    offered = object() if record == "foreign" else transfer
    try:
        if record == "other-stream":
            offered = StderrTransfer(stream=io.StringIO())
        if record == "spent":
            writer = threading.Thread()
            writer.start()
            writer.join(2)
            assert not writer.is_alive()
            with SupervisedRun.create(authorization(), tmp_path / "prior") as prior:
                prior.register_completion(transfer, participant="stderr")
                transfer.accept(writer)
            assert transfer.closed and stream.closed
        with SupervisedRun.create(authorization(), tmp_path / "run") as owner:
            with (
                pytest.raises(RequestPolicyError) as refused,
                open_session(
                    [sys.executable, "-c", "pass"],
                    {},
                    stream,
                    owner=owner,
                    stderr_transfer=offered,
                ),
            ):
                raise AssertionError("invalid transfer acquired a session")
            assert refused.value.reason == "authorization"
            assert owner.pending_owners == ()
    finally:
        if not transfer.accepted:
            stream.close()
        if isinstance(offered, StderrTransfer) and not offered.accepted:
            offered.stream.close()
