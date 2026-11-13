"""A pipe-close failure cannot erase the initiating event or certify completion."""

import asyncio
import io
import subprocess
import sys
import threading

import anyio
import pytest
from test_request_stdio import _reap_for_test
from test_request_supervision import authorization, failure_leaves

from nci_si_acceptance import request_children, request_stdio
from nci_si_acceptance.client import open_session
from nci_si_acceptance.request_stdio import StderrTransfer, StdioOwner
from nci_si_acceptance.request_supervision import SupervisedRun
from nci_si_mcp.errors import RequestPolicyError


async def cancel_owned_read(*_args):
    task = asyncio.current_task()
    assert task is not None
    task.cancel()
    await anyio.lowlevel.checkpoint()
    raise AssertionError("owned read task did not cancel")


@pytest.mark.parametrize(
    "fault", ["stdin", "stdout", "stderr", "utf8", "broken-stdin", "cancel-read"]
)
def test_real_pipe_failures_preserve_the_original_error_and_actual_custody(
    tmp_path, monkeypatch, fault
):
    owner = SupervisedRun.create(authorization(), tmp_path / "run")
    marker = tmp_path / "stdin-closed"
    script = {
        "utf8": (
            "import sys;sys.stdout.buffer.write(b'\\xff\\n');"
            "sys.stdout.buffer.flush();sys.stdin.buffer.read()"
        ),
        "broken-stdin": (
            "import os,sys,time;from pathlib import Path;os.close(0);"
            "Path(sys.argv[1]).touch();time.sleep(30)"
        ),
    }.get(fault, "import sys;sys.stdin.buffer.read()")
    resource = StdioOwner(
        owner, [sys.executable, "-c", script, str(marker)], {}, io.StringIO(), read_seconds=0.2
    )
    original_open, spontaneous_exit = anyio.open_process, True
    missing = fault in ("stdin", "stdout", "stderr")
    initiated, original_initiate = threading.Event(), resource._initiate

    def initiate(error):
        original_initiate(error)
        initiated.set()

    async def open_process(*args, **options):
        assert [options[name] for name in ("stdin", "stdout", "stderr")] == [subprocess.PIPE] * 3
        if missing:
            options[fault] = subprocess.DEVNULL  # Real nullable handle; faulted PIPE postcondition.
        process = await original_open(*args, **options)
        if fault == "broken-stdin":
            with anyio.fail_after(2):
                while not marker.exists():  # noqa: ASYNC110 - actual child filesystem readiness.
                    await anyio.sleep(0.01)
        elif fault == "cancel-read":
            monkeypatch.setattr(process.stdout, "receive", cancel_owned_read)
        return process

    monkeypatch.setattr(anyio, "open_process", open_process)
    monkeypatch.setattr(resource, "_initiate", initiate)
    try:
        with (
            pytest.raises(
                (RequestPolicyError, UnicodeDecodeError, asyncio.CancelledError, BaseExceptionGroup)
            ) as outward,
            owner,
        ):
            owner.register_completion(resource, participant="stdio")
            resource.thread.start()
            initiated.wait(2)  # Synchronize only; assert the actual outward contract below.
            if fault not in ("broken-stdin", "cancel-read"):
                resource.thread.join(5)
                spontaneous_exit = not resource.thread.is_alive()
        assert owner.pending_owners == ((resource.name,) if missing else ())
        assert owner.snapshot()["state"] == "failed"
        assert owner.snapshot()["observationComplete"] is (not missing)
        assert type(resource.process.returncode) is int
        assert resource.waited
        assert resource.process_closed
        assert [
            (s.open_send_streams, s.open_receive_streams)
            for s in (resource.read.statistics(), resource.write.statistics())
        ] == [(0, 0), (0, 0)]
        assert spontaneous_exit, "unexpected pipe failure left its actual transport thread alive"
        leaves = failure_leaves(outward.value)
        expected_type = {"utf8": UnicodeDecodeError, "cancel-read": asyncio.CancelledError}.get(
            fault, RequestPolicyError
        )
        # Missing handles fail both the initial pipe contract and the actual close attempt.
        assert list(map(type, leaves)) == [expected_type] * (2 if missing else 1)
        if expected_type is RequestPolicyError:
            assert [error.reason for error in leaves] == (
                ["accounting", "accounting"] if missing else ["transport"]
            )
    finally:
        resource._stop.set()
        resource.thread.join(5)
        assert not resource.thread.is_alive()
        anyio.run(_reap_for_test, resource)


@pytest.mark.parametrize("caller_failure", [False, True])
def test_late_decode_failure_is_preserved_beside_the_callers_error(
    tmp_path, monkeypatch, caller_failure
):
    owner = SupervisedRun.create(authorization(), tmp_path / "run")
    marker = tmp_path / "decoder-returned"
    script = (
        "import sys,time;from pathlib import Path;sys.stdin.buffer.read();"
        "sys.stdout.buffer.write(b'\\xff\\n');sys.stdout.buffer.flush();"
        "end=time.monotonic()+2\n"
        "while not Path(sys.argv[1]).exists() and time.monotonic()<end:time.sleep(.01)"
    )
    resource = StdioOwner(owner, [sys.executable, "-c", script, str(marker)], {}, io.StringIO())
    original_stdout = resource._stdout
    initiating = LookupError("owned caller failure")

    async def stdout():
        try:
            await original_stdout()
        finally:
            marker.touch()  # Actual decoder unwound; the child can now exit.

    monkeypatch.setattr(resource, "_stdout", stdout)
    try:
        with pytest.raises(BaseException) as outward, owner:
            owner.register_completion(resource, participant="stdio")
            resource.thread.start()
            resource.portal_ready.result(timeout=3)
            if caller_failure:
                raise initiating
        assert owner.snapshot()["state"] == "failed"
        assert owner.snapshot()["observationComplete"] is True
        assert owner.pending_owners == ()
        assert type(resource.process.returncode) is int
        assert resource.waited and resource.process_closed
        assert resource.closed == {"stdin", "stdout", "stderr"}
        assert [
            (s.open_send_streams, s.open_receive_streams)
            for s in (resource.read.statistics(), resource.write.statistics())
        ] == [(0, 0), (0, 0)]
        leaves = failure_leaves(outward.value)
        assert list(map(type, leaves)) == (
            [LookupError, UnicodeDecodeError] if caller_failure else [UnicodeDecodeError]
        )
        if caller_failure:
            assert leaves[0] is initiating
    finally:
        resource._stop.set()
        resource.thread.join(5)
        assert not resource.thread.is_alive()
        anyio.run(_reap_for_test, resource)


def test_accepted_thread_start_failure_retains_each_resource_until_fallback(tmp_path, monkeypatch):
    owner = SupervisedRun.create(authorization(), tmp_path / "run")
    initiating = RuntimeError("owned stdio thread start failure")
    original_register, resources = owner.register_completion, []
    marker = tmp_path / "child-started"
    stream = (tmp_path / "stderr.log").open("w")
    transfer = StderrTransfer(stream)

    def refuse_start():
        raise initiating

    def register(resource, **options):
        token = original_register(resource, **options)
        if isinstance(resource, StdioOwner):
            resources.append(resource)
            monkeypatch.setattr(resource.thread, "start", refuse_start)
        return token

    monkeypatch.setattr(owner, "register_completion", register)
    outward = None
    try:
        try:
            with (
                owner,
                open_session(
                    [
                        sys.executable,
                        "-c",
                        "from pathlib import Path;Path(__import__('sys').argv[1]).touch()",
                        str(marker),
                    ],
                    {},
                    stream,
                    timeout=1,
                    owner=owner,
                    stderr_transfer=transfer,
                ),
            ):
                raise AssertionError("failed thread yielded a session")
        except Exception as error:  # noqa: BLE001 - preserve initiating and cleanup outcomes.
            outward = error
        resource = resources[0]
        leaves = failure_leaves(outward)
        assert leaves[0] is initiating
        assert list(map(type, leaves)) == [RuntimeError, RequestPolicyError, RequestPolicyError]
        assert [error.reason for error in leaves[1:]] == ["accounting", "accounting"]
        assert owner.pending_owners == (resource.name, transfer.name)
        assert resource.process is None
        assert resource.thread.ident is None
        assert not marker.exists()
        assert [
            (s.open_send_streams, s.open_receive_streams)
            for s in (resource.read.statistics(), resource.write.statistics())
        ] == [(1, 1), (1, 1)]
        assert transfer.accepted
        assert not transfer.closed
        assert not stream.closed
        assert owner.snapshot()["state"] == "failed"
        assert owner.snapshot()["observationComplete"] is False
    finally:
        for resource in resources:
            anyio.run(_reap_for_test, resource)
        stream.close()


@pytest.mark.parametrize("after_close", [False, True, pytest.param(None, id="missing-stream")])
def test_failed_pipe_close_preserves_initiating_and_cleanup_failures(
    tmp_path, monkeypatch, after_close
):
    owner = SupervisedRun.create(authorization(), tmp_path / "run")
    sink = io.StringIO()
    resource = StdioOwner(
        owner, [sys.executable, "-c", "import sys;sys.stdin.buffer.read()"], {}, sink
    )
    initiating = LookupError("owned initiating event")
    close, original_run = resource._close, anyio.run
    observed = {}

    if after_close is None:
        original_open = anyio.open_process

        async def missing_stdin(*args, **options):
            assert [options[name] for name in ("stdin", "stdout", "stderr")] == [
                subprocess.PIPE,
                subprocess.PIPE,
                subprocess.PIPE,
            ]
            # Fault the acquisition postcondition with a real nullable Process handle.
            return await original_open(*args, **(options | {"stdin": subprocess.DEVNULL}))

        monkeypatch.setattr(anyio, "open_process", missing_stdin)

    async def fail_after_spawn(_portal):
        raise initiating

    async def failed_close(name):
        if name == "stdin" and after_close is not None:
            if after_close:
                await close(name)
            raise OSError("owned close failure")
        await close(name)

    def guarded_run(function):
        async def guarded():
            try:
                await function()
            finally:
                observed.update(waited=resource.waited, closed=frozenset(resource.closed))
                with anyio.CancelScope(shield=True):
                    await _reap_for_test(resource)

        return original_run(guarded)

    monkeypatch.setattr(resource, "_io_scope", fail_after_spawn)
    monkeypatch.setattr(resource, "_close", failed_close)
    monkeypatch.setattr(anyio, "run", guarded_run)
    try:
        with pytest.raises(BaseExceptionGroup) as outward, owner:
            owner.register_completion(resource, participant="stdio")
            resource.thread.start()
            resource.portal_ready.result(timeout=3)
        leaves = failure_leaves(outward.value)
        assert leaves[0] is initiating
        assert (resource.process.stdin is None) is (after_close is None)
        assert any(
            isinstance(error, RequestPolicyError) and error.reason == "accounting"
            for error in leaves
        )
        assert observed == {
            "waited": True,
            "closed": frozenset(
                {"stdin", "stdout", "stderr"} if after_close else {"stdout", "stderr"}
            ),
        }
        assert owner.pending_owners == (resource.name,)
        assert owner.snapshot()["state"] == "failed" and not owner.snapshot()["observationComplete"]
        assert not sink.closed
    finally:
        resource._stop.set()
        resource.thread.join(5)
        assert not resource.thread.is_alive()


@pytest.fixture
def cleanup_case(tmp_path, monkeypatch, fault):
    owner = SupervisedRun.create(authorization(), tmp_path / "run")
    resource = StdioOwner(
        owner, [sys.executable, "-c", "import time;time.sleep(30)"], {}, io.StringIO()
    )
    initiating, cleanup = LookupError("owned initiating event"), LookupError("owned cleanup event")
    opened, closed, admission = anyio.open_process, resource._close, owner._close_admission
    attempted = set()

    async def open_process(*args, **options):
        process = await opened(*args, **options)
        wait = process.wait

        async def failed_wait():
            if "wait" not in attempted:
                attempted.add("wait")
                raise cleanup
            return await wait()

        def failed_signal():
            raise OSError("owned signal failure")

        if fault == "wait":
            monkeypatch.setattr(process, "wait", failed_wait)
        if fault == "signal":
            monkeypatch.setattr(process, "terminate", failed_signal)
        return process

    async def fail_after_spawn(_portal):
        raise initiating

    async def close(name):
        await closed(name)
        if fault == "close" and name == "stdin":
            raise cleanup

    def close_admission():
        admission()
        if fault == "admission" and "admission" not in attempted:
            attempted.add("admission")
            raise cleanup

    monkeypatch.setattr(request_stdio, "EXIT_SECONDS", 0.02)
    monkeypatch.setattr(anyio, "open_process", open_process)
    monkeypatch.setattr(resource, "_io_scope", fail_after_spawn)
    monkeypatch.setattr(resource, "_close", close)
    monkeypatch.setattr(owner, "_close_admission", close_admission)
    try:
        yield owner, resource, initiating, cleanup
    finally:
        resource._stop.set()
        resource.thread.join(5)
        assert not resource.thread.is_alive()
        anyio.run(_reap_for_test, resource)


@pytest.mark.parametrize("fault", ["close", "wait", "signal", "admission"])
def test_cleanup_operation_failures_preserve_their_actual_result(cleanup_case, fault):
    owner, resource, initiating, cleanup = cleanup_case
    with pytest.raises(BaseExceptionGroup) as outward, owner:
        owner.register_completion(resource, participant="stdio")
        resource.thread.start()
        resource.portal_ready.result(timeout=3)
    assert not resource.thread.is_alive()
    assert type(resource.process.returncode) is int
    assert resource.waited and resource.process_closed
    assert resource.closed == {"stdin", "stdout", "stderr"}
    assert [
        (s.open_send_streams, s.open_receive_streams)
        for s in (resource.read.statistics(), resource.write.statistics())
    ] == [(0, 0), (0, 0)]
    assert owner.pending_owners == (resource.name,)
    assert (owner.snapshot()["state"], owner.snapshot()["observationComplete"]) == ("failed", False)
    leaves = failure_leaves(outward.value)
    assert list(map(type, leaves)) == [
        LookupError,
        RequestPolicyError if fault == "signal" else LookupError,
    ]
    assert leaves[0] is initiating
    if fault == "signal":
        assert leaves[1].reason == "accounting"
    else:
        assert leaves[1] is cleanup


def test_actual_stdout_backpressure_cannot_extend_the_shared_cleanup_deadline(
    tmp_path, monkeypatch
):
    owner = SupervisedRun.create(authorization(), tmp_path / "run")
    marker = tmp_path / "all-output-written"
    script = (
        "import sys;from pathlib import Path;"
        'sys.stdout.buffer.write(b\'{"jsonrpc":"2.0","id":7,"result":{}}\\n\'*100000);'
        "sys.stdout.buffer.flush();Path(sys.argv[1]).touch()"
    )
    resource = StdioOwner(owner, [sys.executable, "-c", script, str(marker)], {}, io.StringIO())
    original_run, original_open, process_close_returned, observed, actual_close = (
        anyio.run,
        anyio.open_process,
        threading.Event(),
        {},
        {},
    )

    async def open_process(*args, **kwargs):
        process = await original_open(*args, **kwargs)
        close = process.aclose
        actual_close["operation"] = close

        async def close_process():
            await close()
            process_close_returned.set()

        monkeypatch.setattr(process, "aclose", close_process)
        return process

    async def held_consumer(_seconds):
        await anyio.sleep_forever()

    async def wait_for_backpressure():
        with anyio.fail_after(3):
            # Public channel statistics expose readiness, but offer no waitable event.
            while resource._read_send.statistics().tasks_waiting_send != 1:  # noqa: ASYNC110
                await anyio.sleep(0.01)

    def guarded_run(function):
        async def guarded():
            try:
                await function()
            finally:
                observed["process_close_returned"] = process_close_returned.is_set()
                with anyio.CancelScope(shield=True):
                    await _reap_for_test(resource)
                    await actual_close["operation"]()

        return original_run(guarded)

    monkeypatch.setattr(resource, "_client", held_consumer)
    monkeypatch.setattr(anyio, "run", guarded_run)
    monkeypatch.setattr(anyio, "open_process", open_process)
    monkeypatch.setattr(request_children, "CLEANUP_SECONDS", 0.1)
    try:
        with pytest.raises(BaseExceptionGroup), owner:
            owner.register_completion(resource, participant="stdio")
            resource.thread.start()
            portal = resource.portal_ready.result(timeout=3)
            portal.call(wait_for_backpressure)
            assert not marker.exists() and resource.process.returncode is None
        assert owner.pending_owners == (resource.name,)
        assert owner.snapshot()["state"] == "failed" and not owner.snapshot()["observationComplete"]
        assert owner.cleanup_deadline().remaining() == 0
    finally:
        resource._stop.set()
        resource.thread.join(5)
        assert not resource.thread.is_alive()
        assert resource.process.returncode is not None
    assert observed["process_close_returned"] is True
