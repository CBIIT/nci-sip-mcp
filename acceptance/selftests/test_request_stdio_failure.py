"""A pipe-close failure cannot erase the initiating event or certify completion."""

import io
import sys
import threading

import anyio
import pytest
from test_request_stdio import _reap_for_test
from test_request_supervision import authorization, failure_leaves

from nci_si_acceptance import request_children
from nci_si_acceptance.request_stdio import StdioOwner
from nci_si_acceptance.request_supervision import SupervisedRun
from nci_si_mcp.errors import RequestPolicyError


@pytest.mark.parametrize("after_close", [False, True])
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

    async def fail_after_spawn(_portal):
        raise initiating

    async def failed_close(name):
        if name == "stdin":
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
