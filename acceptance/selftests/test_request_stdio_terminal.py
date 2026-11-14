"""Execution failure and positively settled resource custody are separate outcomes."""

import io
import json
import signal
import sys
import threading
import time
from types import SimpleNamespace

import anyio
import pytest
from mcp.shared.exceptions import MCPError
from test_request_stdio import _reap_for_test, _stop_child_for_test
from test_request_supervision import authorization, failure_leaves

from nci_si_acceptance import request_stdio
from nci_si_acceptance.client import open_session
from nci_si_acceptance.request_stdio import StdioOwner
from nci_si_acceptance.request_supervision import SupervisedRun
from nci_si_mcp.errors import RequestPolicyError

CHILD = """import json,signal,sys
mode=sys.argv[1]
if mode=='owner-kill': signal.signal(signal.SIGTERM,signal.SIG_IGN)
if mode=='owner-exit7': signal.signal(signal.SIGTERM,lambda *_:sys.exit(7))
for line in sys.stdin:
    request=json.loads(line)
    if 'id' not in request: continue
    method=request['method']
    if method=='initialize':
        result={'protocolVersion':request['params']['protocolVersion'],'capabilities':{'tools':{}},'serverInfo':{'name':'owned-terminal-child','version':'1'}}
    elif method=='tools/list': result={'tools':[]}
    else: result={}
    print(json.dumps({'jsonrpc':'2.0','id':request['id'],'result':result}),flush=True)
if mode.startswith('owner-'): signal.pause()
sys.exit(int(mode))
"""


@pytest.fixture
def terminal_case(tmp_path, monkeypatch):
    owner = SupervisedRun.create(authorization(), tmp_path / "run")
    resources, waited, closed = [], [], set()
    real_register, real_open = owner.register_completion, anyio.open_process

    def register(resource, **kwargs):
        token = real_register(resource, **kwargs)
        if isinstance(resource, StdioOwner):
            resources.append(resource)
        return token

    async def open_process(*args, **kwargs):
        process = await real_open(*args, **kwargs)
        actual_wait = process.wait

        async def wait():
            code = await actual_wait()
            waited.append(code)
            return code

        try:
            monkeypatch.setattr(process, "wait", wait)
            for name in ("stdin", "stdout", "stderr"):
                stream = getattr(process, name)
                assert stream is not None
                actual_close = stream.aclose

                async def close(operation=actual_close, stream_name=name):
                    await operation()
                    closed.add(stream_name)

                monkeypatch.setattr(stream, "aclose", close)
        except BaseException:
            with anyio.CancelScope(shield=True):
                await process.aclose()
            raise
        return process

    monkeypatch.setattr(owner, "register_completion", register)
    monkeypatch.setattr(anyio, "open_process", open_process)
    try:
        yield owner, resources, waited, closed
    finally:
        for resource in resources:
            _stop_child_for_test(resource)
            if resource.thread.ident is not None:
                resource.thread.join(5)
            assert not resource.thread.is_alive()
            anyio.run(_reap_for_test, resource)


@pytest.mark.parametrize("mode", ["0", "7", "owner-term", "owner-kill", "owner-exit7"])
def test_child_exit_status_preserves_terminal_outcome(terminal_case, mode):
    owner, resources, waited, closed = terminal_case
    unlisted = object()
    listing = unlisted
    outward = None
    try:
        with (
            owner,
            open_session(
                [sys.executable, "-c", CHILD, mode], {}, io.StringIO(), timeout=3, owner=owner
            ) as session,
        ):
            listing = session.list_tools()
    except BaseException as error:  # noqa: BLE001 - assert the complete caller outcome below.
        outward = error
    assert len(resources) == 1
    resource = resources[0]
    process = resource.process
    assert process is not None
    expected_code = {
        "0": 0,
        "7": 7,
        "owner-term": -signal.SIGTERM,
        "owner-kill": -signal.SIGKILL,
        "owner-exit7": 7,
    }[mode]
    assert process.returncode == expected_code
    assert waited and all(code == expected_code for code in waited)
    assert closed == {"stdin", "stdout", "stderr"}
    assert not resource.thread.is_alive()
    assert [
        (state.open_send_streams, state.open_receive_streams)
        for state in (resource.read.statistics(), resource.write.statistics())
    ] == [(0, 0), (0, 0)]
    summary = json.loads((owner.run.directory / "summary.json").read_text())
    assert owner.pending_owners == ()
    assert summary["observationComplete"] is True
    assert getattr(listing, "tools", unlisted) == []
    assert summary["state"] == ("failed" if mode in ("7", "owner-exit7") else "completed")
    if mode in ("7", "owner-exit7"):
        assert outward is not None
        assert any(
            isinstance(error, RequestPolicyError) and error.reason == "transport"
            for error in failure_leaves(outward)
        )
    else:
        assert outward is None


@pytest.fixture
def expired_startup(terminal_case, monkeypatch):
    owner, _, _, _ = terminal_case
    acquired, release = threading.Event(), threading.Event()
    now = [time.monotonic()]
    real_open, real_register = anyio.open_process, owner.register_completion

    async def held_open(*args, **kwargs):
        acquired.set()
        # The synchronous caller owns release; wait on its actual cross-thread event.
        assert await anyio.to_thread.run_sync(release.wait, 5)
        return await real_open(*args, **kwargs)

    def register(resource, **kwargs):
        token = real_register(resource, **kwargs)
        if isinstance(resource, StdioOwner):
            actual_start = resource.thread.start

            def start():
                actual_start()
                assert acquired.wait(3), "Actual process acquisition barrier was not reached"
                now[0] += 4  # Model expiration of startup's three-second budget only.

            monkeypatch.setattr(resource.thread, "start", start)
        return token

    # Do not replace the shared time module or the SDK/read-timeout clock.
    monkeypatch.setattr(request_stdio, "time", SimpleNamespace(monotonic=lambda: now[0]))
    monkeypatch.setattr(anyio, "open_process", held_open)
    monkeypatch.setattr(owner, "register_completion", register)
    try:
        yield release
    finally:
        release.set()


def test_caught_startup_wait_expiration_terminally_fails_owner(terminal_case, expired_startup):
    owner, resources, waited, closed = terminal_case
    outward, caught, admission = None, None, None
    try:
        with owner:
            with (
                pytest.raises((TimeoutError, RequestPolicyError)) as caught,
                open_session(
                    [sys.executable, "-c", CHILD, "0"], {}, io.StringIO(), timeout=3, owner=owner
                ),
            ):
                pytest.fail("Expired startup yielded a usable session")
            expired_startup.set()
            assert resources[0].client_ready.result(timeout=3) is not None
            admission = owner.run.snapshot()
    except BaseException as error:  # noqa: BLE001 - assert terminal failure after physical proof.
        outward = error
    assert len(resources) == 1
    resource = resources[0]
    assert resource.process is not None and resource.process.returncode == 0
    assert waited and all(code == 0 for code in waited)
    assert closed == {"stdin", "stdout", "stderr"}
    assert not resource.thread.is_alive()
    assert [
        (state.open_send_streams, state.open_receive_streams)
        for state in (resource.read.statistics(), resource.write.statistics())
    ] == [(0, 0), (0, 0)]
    summary = json.loads((owner.run.directory / "summary.json").read_text())
    assert owner.pending_owners == ()
    assert summary["observationComplete"] is True
    assert summary["state"] == "failed"
    assert admission is not None and admission.state == "failed" and admission.failure == "deadline"
    assert caught is not None
    assert isinstance(caught.value, RequestPolicyError) and caught.value.reason == "deadline"
    assert outward is not None
    assert any(error is caught.value for error in failure_leaves(outward))


def test_startup_operation_timeout_preserves_original_event(terminal_case, monkeypatch):
    owner, resources, waited, closed = terminal_case
    original = TimeoutError("Owned acquisition operation timed out")

    async def refuse_acquisition(*_args, **_kwargs):
        raise original

    monkeypatch.setattr(anyio, "open_process", refuse_acquisition)
    opened, caught, outward = False, None, None
    try:
        with owner:
            try:
                with open_session(
                    [sys.executable, "-c", CHILD, "0"], {}, io.StringIO(), timeout=3, owner=owner
                ):
                    opened = True
            except BaseException as error:  # noqa: BLE001 - record, then assert outside capture.
                caught = error
    except BaseException as error:  # noqa: BLE001 - preserve the exact operation error below.
        outward = error
    assert len(resources) == 1
    resource = resources[0]
    assert resource.process is None
    assert waited == [] and closed == set()
    assert not resource.thread.is_alive()
    assert [
        (state.open_send_streams, state.open_receive_streams)
        for state in (resource.read.statistics(), resource.write.statistics())
    ] == [(0, 0), (0, 0)]
    summary = json.loads((owner.run.directory / "summary.json").read_text())
    assert summary["state"] == "failed" and summary["observationComplete"] is True
    assert owner.pending_owners == ()
    assert owner.run.snapshot().failure == "transport"
    assert opened is False
    assert caught is not None
    caught_leaves = failure_leaves(caught)
    assert len(caught_leaves) == 1 and caught_leaves[0] is original
    assert outward is not None
    assert any(error is original for error in failure_leaves(outward))


def test_caught_sdk_startup_error_survives_later_child_failure(terminal_case):
    owner, resources, waited, closed = terminal_case
    expected_status = 7
    child = """import json,sys
for line in sys.stdin:
    request=json.loads(line)
    if 'id' not in request: continue
    if request.get('method')=='initialize':
        error={'code':-32603,'message':'owned initialization refused'}
    else:
        error={'code':-32601,'message':'Method not found'}
    print(json.dumps({'jsonrpc':'2.0','id':request['id'],'error':error}),flush=True)
sys.exit(7)
"""
    opened, caught, outward = False, None, None
    try:
        with owner:
            try:
                with open_session(
                    [sys.executable, "-c", child], {}, io.StringIO(), timeout=3, owner=owner
                ):
                    opened = True
            except BaseException as error:  # noqa: BLE001 - compare the actual SDK cause below.
                caught = error
            resources[0].thread.join(3)
    except BaseException as error:  # noqa: BLE001 - assert original cause after physical proof.
        outward = error
    assert len(resources) == 1
    resource = resources[0]
    assert resource.process is not None and resource.process.returncode == expected_status
    assert waited and all(code == expected_status for code in waited)
    assert closed == {"stdin", "stdout", "stderr"}
    assert not resource.thread.is_alive()
    assert [
        (state.open_send_streams, state.open_receive_streams)
        for state in (resource.read.statistics(), resource.write.statistics())
    ] == [(0, 0), (0, 0)]
    summary = json.loads((owner.run.directory / "summary.json").read_text())
    assert owner.pending_owners == ()
    assert summary["state"] == "failed" and summary["observationComplete"] is True
    assert opened is False
    assert caught is not None and outward is not None
    causes = [error for error in failure_leaves(caught) if isinstance(error, MCPError)]
    assert len(causes) == 1
    original = causes[0]
    expected_error_code = -32603
    assert original.code == expected_error_code
    assert original.message == "owned initialization refused"
    assert any(error is original for error in failure_leaves(outward))
