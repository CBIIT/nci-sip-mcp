"""Execution failure and positively settled resource custody are separate outcomes."""

import io
import json
import os
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

from nci_si_acceptance import request_children, request_stdio
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

STARTUP_ERROR_CHILD = """import json,sys
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
    child = STARTUP_ERROR_CHILD
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


def test_caught_initialization_error_closes_peer_before_outer_owner_cleanup(
    terminal_case, tmp_path, monkeypatch
):
    owner, _, _, _ = terminal_case
    marker = tmp_path / "peer-eof"
    child = STARTUP_ERROR_CHILD.replace(
        "sys.exit(7)",
        "with open(sys.argv[1], 'wb') as receipt: receipt.write(b'owned EOF\\n')\nsys.exit(7)",
        1,
    )
    acknowledged = threading.Event()
    real_open = anyio.open_process

    async def open_process(*args, **kwargs):
        process = await real_open(*args, **kwargs)
        actual_wait = process.wait

        async def wait():
            code = await actual_wait()
            acknowledged.set()
            return code

        monkeypatch.setattr(process, "wait", wait)
        return process

    monkeypatch.setattr(anyio, "open_process", open_process)
    opened, caught, outward, before_outer, receipt_before_outer = False, None, None, False, None
    try:
        with owner:
            try:
                with open_session(
                    [sys.executable, "-c", child, str(marker)],
                    {},
                    io.StringIO(),
                    timeout=3,
                    owner=owner,
                ):
                    opened = True
            except BaseException as error:  # noqa: BLE001 - record, then assert outside capture.
                caught = error
            before_outer = acknowledged.wait(5)
            if before_outer:
                receipt_before_outer = marker.read_bytes()
    except BaseException as error:  # noqa: BLE001 - observe outer failure without swallowing assertions.
        outward = error
    _assert_failed_terminal_resources_settled(terminal_case, 7)
    assert opened is False
    assert (before_outer, receipt_before_outer) == (True, b"owned EOF\n")
    assert caught is not None and outward is not None
    causes = [error for error in failure_leaves(caught) if isinstance(error, MCPError)]
    assert len(causes) == 1
    expected_code = -32603
    assert (causes[0].code, causes[0].message) == (expected_code, "owned initialization refused")
    assert any(error is causes[0] for error in failure_leaves(outward))


@pytest.fixture
def held_process_cleanup(terminal_case, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    real_open = anyio.open_process

    async def open_process(*args, **kwargs):
        process = await real_open(*args, **kwargs)
        actual_wait = process.wait

        async def wait():
            entered.set()
            if not await anyio.to_thread.run_sync(release.wait, 8):
                raise TimeoutError("Owned process-wait barrier was not released")
            return await actual_wait()

        monkeypatch.setattr(process, "wait", wait)
        return process

    monkeypatch.setattr(anyio, "open_process", open_process)
    try:
        yield entered, release
    finally:
        release.set()


def _assert_failed_terminal_resources_settled(case, expected_status):
    owner, resources, waited, closed = case
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
    assert owner.pending_owners == ()
    summary = json.loads((owner.run.directory / "summary.json").read_text())
    assert (summary["state"], summary["observationComplete"]) == ("failed", True)


def test_sdk_startup_error_is_delivered_while_process_cleanup_is_held(
    terminal_case, held_process_cleanup
):
    owner, resources, _, _ = terminal_case
    entered, release = held_process_cleanup
    finished = threading.Event()
    opened, errors = [], []

    def connect():
        try:
            with open_session(
                [sys.executable, "-c", STARTUP_ERROR_CHILD],
                {},
                io.StringIO(),
                timeout=3,
                owner=owner,
            ):
                opened.append(True)
        except BaseException as error:  # noqa: BLE001 - record actor errors; assert outside capture.
            errors.append(error)
        finally:
            finished.set()

    caller = threading.Thread(target=connect, name="owned-startup-error-caller")
    cleanup_seen, delivered, held, producer_alive, outward = False, False, False, False, None
    try:
        try:
            with owner:
                caller.start()
                cleanup_seen = entered.wait(2)
                delivered = finished.wait(5)
                held = not release.is_set()
                producer_alive = resources[0].thread.is_alive()
                release.set()
                caller.join(3)
        except BaseException as error:  # noqa: BLE001 - compare the actual owner failure below.
            outward = error
        _assert_failed_terminal_resources_settled(terminal_case, 7)
        assert (cleanup_seen, delivered, held, producer_alive) == (True, True, True, True)
        assert not caller.is_alive() and opened == []
        assert len(errors) == 1 and outward is not None
        causes = [error for error in failure_leaves(errors[0]) if isinstance(error, MCPError)]
        assert len(causes) == 1
        expected_code = -32603
        assert (causes[0].code, causes[0].message) == (
            expected_code,
            "owned initialization refused",
        )
        assert any(error is causes[0] for error in failure_leaves(outward))
    finally:
        release.set()
        if caller.ident is not None:
            caller.join(6)
        assert not caller.is_alive()


def test_sdk_exit_failure_after_ready_preserves_original_event(terminal_case, monkeypatch):
    owner, _, _, _ = terminal_case
    original = LookupError("Owned SDK exit failed after readiness")
    completed = threading.Event()
    actual_exit = request_stdio.Client.__aexit__

    async def exit_client(self, *exception):
        await actual_exit(self, *exception)
        completed.set()
        raise original

    monkeypatch.setattr(request_stdio.Client, "__aexit__", exit_client)
    unlisted = object()
    listing, exit_seen, outward = unlisted, False, None
    try:
        with owner:
            with open_session(
                [sys.executable, "-c", CHILD, "0"], {}, io.StringIO(), timeout=3, owner=owner
            ) as session:
                listing = session.list_tools()
            exit_seen = completed.wait(3)
    except BaseException as error:  # noqa: BLE001 - assert real SDK exit error after physical proof.
        outward = error
    _assert_failed_terminal_resources_settled(terminal_case, 0)
    assert getattr(listing, "tools", unlisted) == []
    assert exit_seen
    assert outward is not None
    assert any(error is original for error in failure_leaves(outward))


def test_sdk_exit_error_during_owner_cleanup_preserves_original_event(terminal_case, monkeypatch):
    owner, _, _, _ = terminal_case
    original = LookupError("Owned SDK exit failed during owner cleanup")
    wait_entered, sdk_ready = threading.Event(), threading.Event()
    real_open, actual_exit = anyio.open_process, request_stdio.Client.__aexit__
    delegated_exits, returned_statuses = [], []

    async def open_process(*args, **kwargs):
        process = await real_open(*args, **kwargs)
        actual_wait = process.wait

        async def wait():
            wait_entered.set()
            code = await actual_wait()
            if not await anyio.to_thread.run_sync(sdk_ready.wait, 5):
                raise TimeoutError("Owned SDK-exit barrier was not released")
            returned_statuses.append(code)
            return code

        monkeypatch.setattr(process, "wait", wait)
        return process

    async def exit_client(self, *exception):
        await actual_exit(self, *exception)
        delegated_exits.append(True)
        if not await anyio.to_thread.run_sync(wait_entered.wait, 3):
            raise TimeoutError("Actual process wait did not begin during cleanup")
        sdk_ready.set()
        raise original

    monkeypatch.setattr(anyio, "open_process", open_process)
    monkeypatch.setattr(request_stdio.Client, "__aexit__", exit_client)
    unlisted = object()
    listing, outward = unlisted, None
    try:
        try:
            with (
                owner,
                open_session(
                    [sys.executable, "-c", CHILD, "7"], {}, io.StringIO(), timeout=3, owner=owner
                ) as session,
            ):
                listing = session.list_tools()
        except BaseException as error:  # noqa: BLE001 - compare actual error leaves outside capture.
            outward = error
        _assert_failed_terminal_resources_settled(terminal_case, 7)
        assert getattr(listing, "tools", unlisted) == []
        assert delegated_exits == [True]
        assert wait_entered.is_set() and sdk_ready.is_set()
        expected_status = 7
        assert returned_statuses and set(returned_statuses) == {expected_status}
        assert outward is not None
        assert any(error is original for error in failure_leaves(outward))
    finally:
        # Release only the test-owned wait barrier; actual resource fallback belongs to the fixture.
        sdk_ready.set()


def test_closed_handles_do_not_release_a_live_transport_thread(terminal_case, monkeypatch):
    owner, resources, waited, closed = terminal_case
    original = LookupError("Owned SDK exit ends the actual transport event loop")
    loop_exited, release = threading.Event(), threading.Event()
    clock = [time.monotonic()]
    actual_run, actual_exit = anyio.run, request_stdio.Client.__aexit__
    monkeypatch.setattr(request_children, "time", SimpleNamespace(monotonic=lambda: clock[0]))

    async def exit_client(self, *exception):
        await actual_exit(self, *exception)
        raise original

    def run(function, *args, **kwargs):
        try:
            return actual_run(function, *args, **kwargs)
        finally:
            if isinstance(getattr(function, "__self__", None), StdioOwner):
                # The event loop/tasks really ended; only the owning OS thread is held.
                loop_exited.set()
                release.wait(8)

    monkeypatch.setattr(request_stdio.Client, "__aexit__", exit_client)
    monkeypatch.setattr(anyio, "run", run)
    unlisted = object()
    listing, seen, outward, deadline = unlisted, False, None, None
    try:
        try:
            with owner:
                deadline = owner.cleanup_deadline()
                with open_session(
                    [sys.executable, "-c", CHILD, "0"], {}, io.StringIO(), timeout=3, owner=owner
                ) as session:
                    listing = session.list_tools()
                seen = loop_exited.wait(5)
                clock[0] += request_children.CLEANUP_SECONDS + 1
        except BaseException as error:  # noqa: BLE001 - assert live-thread custody outside capture.
            outward = error
        assert (seen, getattr(listing, "tools", unlisted)) == (True, [])
        assert len(resources) == 1
        resource = resources[0]
        assert resource.process is not None
        assert (resource.process.returncode, resource.thread.is_alive()) == (0, True)
        assert (set(waited), closed) == ({0}, {"stdin", "stdout", "stderr"})
        assert [
            (state.open_send_streams, state.open_receive_streams)
            for state in (resource.read.statistics(), resource.write.statistics())
        ] == [(0, 0), (0, 0)]
        assert owner.pending_owners == (resource.name,)
        summary = owner.snapshot()
        assert (summary["state"], summary["observationComplete"]) == ("failed", False)
        assert owner.cleanup_deadline() is deadline
        assert deadline.remaining() == 0
        assert outward is not None
        assert any(error is original for error in failure_leaves(outward))
    finally:
        # Release only after the actual alive-thread/pending-token observations.
        release.set()
        if owner._observer.ident is not None:
            owner._observer.join(3)
        assert not owner._observer.is_alive()


@pytest.mark.skipif(
    not all(hasattr(os, name) for name in ("waitid", "WNOWAIT", "CLD_KILLED")),
    reason="Owns the POSIX non-consuming child termination receipt",
)
def test_outside_cleanup_kills_child_before_transport_loop_resumes(terminal_case, monkeypatch):
    owner, resources, _, _ = terminal_case
    held, release, terminated = threading.Event(), threading.Event(), threading.Event()
    clock, receipts, errors = [time.monotonic()], [], []
    actual_exit = request_stdio.Client.__aexit__
    monkeypatch.setattr(request_children, "time", SimpleNamespace(monotonic=lambda: clock[0]))

    async def exit_client(self, *exception):
        held.set()
        release.wait(8)
        await actual_exit(self, *exception)

    def observe_exit():
        try:
            receipts.append(os.waitid(os.P_PID, resources[0].process.pid, os.WEXITED | os.WNOWAIT))
        except BaseException as error:  # noqa: BLE001 - record OS witness failures for caller assertion.
            errors.append(error)
        finally:
            terminated.set()

    monkeypatch.setattr(request_stdio.Client, "__aexit__", exit_client)
    witness = threading.Thread(target=observe_exit, name="owned-nonconsuming-child-exit-witness")
    unlisted = object()
    listing, hold_seen, killed_before_release, failure = unlisted, False, False, None
    try:
        try:
            with owner:
                owner.cleanup_deadline()
                with open_session(
                    [sys.executable, "-c", CHILD, "owner-kill"],
                    {},
                    io.StringIO(),
                    timeout=3,
                    owner=owner,
                ) as session:
                    listing = session.list_tools()
                hold_seen = held.wait(3)
                witness.start()
                clock[0] += request_children.CLEANUP_SECONDS + 1
        except BaseException as error:  # noqa: BLE001 - assert OS effect before independent fallback.
            failure = error
        killed_before_release = terminated.wait(2)
        assert (hold_seen, getattr(listing, "tools", unlisted), release.is_set()) == (
            True,
            [],
            False,
        )
        assert killed_before_release
        assert errors == [] and len(receipts) == 1
        assert [(receipt.si_pid, receipt.si_code, receipt.si_status) for receipt in receipts] == [
            (resources[0].process.pid, os.CLD_KILLED, signal.SIGKILL)
        ]
        assert (resources[0].thread.is_alive(), owner.pending_owners) == (
            True,
            (resources[0].name,),
        )
        summary = owner.snapshot()
        assert (summary["state"], summary["observationComplete"]) == ("failed", False)
        assert failure is not None
        assert any(
            isinstance(error, RequestPolicyError) and error.reason == "accounting"
            for error in failure_leaves(failure)
        )
    finally:
        # A later fallback kill cannot repair the pre-release termination observation.
        if resources:
            _stop_child_for_test(resources[0])
        release.set()
        if witness.ident is not None:
            witness.join(3)
        assert not witness.is_alive()
