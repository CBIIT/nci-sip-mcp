"""An explicit controlled session owns its actual child beyond the client's context."""

import io
import json
import sys
import threading
import traceback
from contextlib import suppress
from pathlib import Path

import anyio
import pytest
from test_request_supervision import authorization, failure_leaves

from nci_si_acceptance import request_children, request_stdio
from nci_si_acceptance.client import open_session
from nci_si_acceptance.request_authorization import Authorization, Limits, RequestGrant
from nci_si_acceptance.request_stdio import StderrTransfer, StdioOwner
from nci_si_acceptance.request_supervision import SupervisedRun
from nci_si_mcp.errors import RequestPolicyError


async def _reap_for_test(resource):
    """After production returns, independently prevent an orphan without repairing its proof."""
    process = resource.process
    if process is not None:
        if process.returncode is None:
            # Actual-parent wait below supplies the receipt even if the signal races exit.
            with suppress(ProcessLookupError):
                process.kill()
        with anyio.fail_after(3):
            await process.wait()
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    await stream.aclose()
    # These are this implementation's owned in-memory handles, not SDK private handles.
    for stream in (resource.read, resource.write, resource._read_send, resource._write_receive):
        await stream.aclose()


def test_failure_before_process_acquisition_closes_actual_endpoints(tmp_path, monkeypatch):
    owner = SupervisedRun.create(authorization(), tmp_path / "run")
    resource = StdioOwner(owner, [sys.executable, "-c", "pass"], {}, io.StringIO())
    initiating = FileNotFoundError("owned acquisition failure")

    async def refuse_acquisition(*_args, **_kwargs):
        raise initiating

    monkeypatch.setattr(anyio, "open_process", refuse_acquisition)
    try:
        with pytest.raises((RequestPolicyError, BaseExceptionGroup)) as outward, owner:
            owner.register_completion(resource, participant="stdio")
            resource.thread.start()
            resource.thread.join(2)
            assert not resource.thread.is_alive()
        assert failure_leaves(resource.initiating) == [initiating]
        assert resource.process is None
        assert resource.waited is False
        assert resource.process_closed is False
        assert [
            (state.open_send_streams, state.open_receive_streams)
            for state in (resource.read.statistics(), resource.write.statistics())
        ] == [(0, 0), (0, 0)]
        summary = json.loads((owner.run.directory / "summary.json").read_text())
        assert summary["state"] == "failed"
        assert summary["observationComplete"] is True
        assert list(map(type, failure_leaves(outward.value))) == [RequestPolicyError]
    finally:
        resource.thread.join(2)
        assert not resource.thread.is_alive()
        anyio.run(_reap_for_test, resource)


def test_missing_executable_is_reported_once_by_the_public_controlled_session(tmp_path):
    owner = SupervisedRun.create(authorization(), tmp_path / "run")
    missing = tmp_path / "nonexistent-owned-executable"
    assert not missing.exists()
    with (
        pytest.raises(BaseException) as outward,
        owner,
        open_session([str(missing)], {}, io.StringIO(), timeout=0.2, owner=owner),
    ):
        raise AssertionError("a nonexistent executable yielded a session")
    summary = json.loads((owner.run.directory / "summary.json").read_text())
    assert summary["state"] == "failed"
    assert summary["observationComplete"] is True
    assert owner.pending_owners == ()
    assert list(map(type, failure_leaves(outward.value))) == [FileNotFoundError]


def test_sealed_controller_refuses_the_actual_offer_and_returns_creator_custody(
    tmp_path, monkeypatch
):
    owner = SupervisedRun.create(authorization(), tmp_path / "run")
    offered, release = threading.Event(), threading.Event()
    resources, failures = [], []
    register = owner.register_completion
    marker = tmp_path / "child-started"
    stream = (tmp_path / "stderr.log").open("w")
    transfer = StderrTransfer(stream)

    def hold_offer(resource, **options):
        resources.append(resource)
        offered.set()
        assert release.wait(2), "creator was not released after controller closure"
        return register(resource, **options)

    def caller():
        try:
            with open_session(
                [
                    sys.executable,
                    "-c",
                    "from pathlib import Path;Path(__import__('sys').argv[1]).touch()",
                    str(marker),
                ],
                {},
                stream,
                timeout=0.2,
                owner=owner,
                stderr_transfer=transfer,
            ):
                raise AssertionError("a sealed controller yielded a session")
        except BaseException as error:  # noqa: BLE001 - retain the actual actor event for the caller.
            failures.append(error)

    actor = threading.Thread(target=caller, name="owned-refused-offer")
    monkeypatch.setattr(owner, "register_completion", hold_offer)
    try:
        with owner:
            actor.start()
            assert offered.wait(2)
        assert owner.run.snapshot().state == "closed"
        release.set()
        actor.join(3)
        assert not actor.is_alive()
        assert list(map(type, failures)) == [RequestPolicyError]
        assert failures[0].reason == "authorization"
        assert len(resources) == 1
        resource = resources[0]
        assert resource.thread.ident is None and resource.process is None
        assert not marker.exists()
        assert not transfer.accepted and not stream.closed
        assert owner.pending_owners == ()
        assert [
            (s.open_send_streams, s.open_receive_streams)
            for s in (resource.read.statistics(), resource.write.statistics())
        ] == [(0, 0), (0, 0)]
    finally:
        release.set()
        for resource in resources:
            resource._stop.set()
            if resource.thread.ident is not None:
                resource.thread.join(5)
            assert not resource.thread.is_alive()
            anyio.run(_reap_for_test, resource)
        if actor.ident is not None:
            actor.join(3)
        assert not actor.is_alive()
        stream.close()


def test_acquisition_and_endpoint_shutdown_failures_remain_distinct(tmp_path, monkeypatch):
    owner = SupervisedRun.create(authorization(), tmp_path / "run")
    resource = StdioOwner(owner, [str(tmp_path / "missing")], {}, io.StringIO())
    cleanup = LookupError("owned endpoint cleanup failure")

    async def close_endpoints(_deadline):
        raise cleanup

    monkeypatch.setattr(resource, "_close_endpoints", close_endpoints)
    try:
        with pytest.raises(BaseExceptionGroup) as outward, owner:
            owner.register_completion(resource, participant="stdio")
            resource.thread.start()
            resource.portal_ready.result(timeout=3)
        assert not resource.thread.is_alive()
        assert resource.process is None
        assert owner.pending_owners == (resource.name,)
        summary = json.loads((owner.run.directory / "summary.json").read_text())
        assert (summary["state"], summary["observationComplete"]) == ("failed", False)
        assert [
            (s.open_send_streams, s.open_receive_streams)
            for s in (resource.read.statistics(), resource.write.statistics())
        ] == [(1, 1), (1, 1)]
        leaves = failure_leaves(outward.value)
        assert list(map(type, leaves)) == [FileNotFoundError, LookupError]
        assert leaves[1] is cleanup
    finally:
        resource.thread.join(3)
        assert not resource.thread.is_alive()
        anyio.run(_reap_for_test, resource)


def test_real_sdk_session_keeps_completion_pending_until_external_cleanup(tmp_path):
    replies = tmp_path / "replies.json"
    replies.write_text(
        json.dumps([{"tool": "owned", "arguments": {"value": "input"}, "result": {"ok": True}}])
    )
    command = [
        sys.executable,
        str(Path(__file__).with_name("live_content_server.py")),
        str(replies),
    ]
    authorization = Authorization(
        (RequestGrant("GET", "http://127.0.0.1:9/unused", ()),), Limits(), "a" * 64
    )
    owner = SupervisedRun.create(authorization, tmp_path / "run")
    errors = io.StringIO()
    with owner:
        try:
            with open_session(command, {}, errlog=errors, owner=owner) as session:
                session._resource.read_seconds = 3
                observed = {
                    "lists": [[tool.name for tool in session.list_tools().tools] for _ in range(2)],
                    "call": session.call_tool("owned", {"value": "input"}).structured_content,
                }
                assert owner.snapshot()["observationComplete"] is False
            assert owner.snapshot()["observationComplete"] is False
        except TypeError as error:
            # The real entry's refusal is an observable failed session, including absence.
            observed = {"entry_error": str(error)}
        assert observed == {"lists": [["owned"], ["owned"]], "call": {"ok": True}}
    summary = json.loads((owner.run.directory / "summary.json").read_text())
    assert summary["state"] == "completed" and summary["observationComplete"] is True
    assert not errors.closed


def test_cancel_immediately_after_spawn_settles_before_independent_fallback(tmp_path, monkeypatch):
    owner = SupervisedRun.create(
        Authorization((RequestGrant("GET", "http://127.0.0.1:9/unused", ()),), Limits(), "a" * 64),
        tmp_path / "run",
    )
    replies = tmp_path / "replies.json"
    replies.write_text("[]")
    resource = StdioOwner(
        owner,
        [sys.executable, str(Path(__file__).with_name("live_content_server.py")), str(replies)],
        {},
        io.StringIO(),
    )
    control, observed = {}, {}
    original_run = anyio.run

    def guarded_run(function):
        async def guarded():
            with anyio.CancelScope() as scope:
                control["scope"] = scope
                try:
                    await function()
                except BaseException as error:  # noqa: BLE001 - re-raised after independent cleanup.
                    control["cancelled"] = error
                finally:
                    with anyio.CancelScope(shield=True):
                        process = resource.process
                        if process is not None:
                            observed.update(
                                returncode=process.returncode,
                                waited=resource.waited,
                                closed=frozenset(resource.closed),
                            )
                        await _reap_for_test(resource)
            if "cancelled" in control:
                raise control["cancelled"]

        return original_run(guarded)

    async def cancel_at_owned_handle(_portal):
        assert resource.process is not None
        control["scope"].cancel()
        await anyio.lowlevel.checkpoint()

    monkeypatch.setattr(anyio, "run", guarded_run)
    monkeypatch.setattr(resource, "_io_scope", cancel_at_owned_handle)
    outward = None
    try:
        with owner:
            owner.register_completion(resource, participant="stdio")
            resource.thread.start()
            resource.thread.join(5)
            assert not resource.thread.is_alive()
    except BaseException as error:  # noqa: BLE001 - assert the exact initiating event below.
        outward = error
    finally:
        resource.thread.join(25)
        assert not resource.thread.is_alive()
    assert observed == {
        "returncode": 0,
        "waited": True,
        "closed": frozenset({"stdin", "stdout", "stderr"}),
    }
    assert outward is control["cancelled"]
    assert owner.snapshot()["state"] == "failed" and owner.pending_owners == ()


def test_held_stderr_sink_cannot_block_finish_scheduling_past_shared_deadline(
    tmp_path, monkeypatch
):
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()

    class HeldSink(io.StringIO):
        def write(self, text):
            entered.set()
            if not release.wait(10):
                raise TimeoutError("owned test safety release expired")
            return super().write(text)

    controller = SupervisedRun.create(
        Authorization((RequestGrant("GET", "http://127.0.0.1:9/unused", ()),), Limits(), "a" * 64),
        tmp_path / "run",
    )
    resource = StdioOwner(
        controller,
        [
            sys.executable,
            "-c",
            'import sys;sys.stderr.write("held\\n");sys.stderr.flush();sys.stdin.buffer.read()',
        ],
        {},
        HeldSink(),
    )
    original_run = anyio.run

    def guarded_run(function):
        async def guarded():
            try:
                await function()
            finally:
                with anyio.CancelScope(shield=True):
                    await _reap_for_test(resource)

        return original_run(guarded)

    monkeypatch.setattr(anyio, "run", guarded_run)
    failures = []
    deadline = None
    blocked_stack = ""

    def finish():
        try:
            resource.finish(deadline)
        except BaseException as error:  # noqa: BLE001 - assert failure and retain actual owner.
            failures.append(error)
        finally:
            finished.set()

    finisher = threading.Thread(target=finish, name="owned-stdio-deadline-probe")
    resource.thread.start()
    try:
        resource.portal_ready.result(timeout=3)
        assert entered.wait(3), "real child did not reach the owned sink"
        monkeypatch.setattr(request_children, "CLEANUP_SECONDS", 0.1)
        deadline = controller.cleanup_deadline()
        finisher.start()
        returned = finished.wait(0.5)
        if not returned:
            frame = sys._current_frames().get(finisher.ident)
            if frame is not None:
                blocked_stack = "".join(traceback.format_stack(frame))
    finally:
        release.set()
        if finisher.ident is not None:
            finisher.join(5)
        resource.thread.join(5)
    assert not finisher.is_alive() and not resource.thread.is_alive()
    assert resource.process.returncode is not None
    assert returned, "finish blocked in portal scheduling beyond shared deadline\n" + blocked_stack
    assert failures, "unproved blocked cleanup cannot report settlement"


@pytest.mark.parametrize("held_after_portal_exit", [False, True])
def test_rpc_timeout_retains_actual_caller_until_outside_cleanup(
    tmp_path, monkeypatch, held_after_portal_exit
):
    replies = tmp_path / "replies.json"
    replies.write_text("[]")
    owner = SupervisedRun.create(authorization(), tmp_path / "run")
    entered, release = threading.Event(), threading.Event()

    def held_schedule():
        entered.set()
        release.wait()

    async def immediate():
        return "ready"

    async def pending():
        entered.set()
        await anyio.sleep_forever()

    resource, timeout = None, None
    try:
        with (
            pytest.raises(BaseException) as outward,
            owner,
            open_session(
                [
                    sys.executable,
                    str(Path(__file__).with_name("live_content_server.py")),
                    str(replies),
                ],
                {},
                errlog=io.StringIO(),
                owner=owner,
            ) as session,
        ):
            resource = session._resource
            assert session._call(immediate) == "ready"
            resource.read_seconds = 0.05
            with pytest.raises(RequestPolicyError) as timeout:
                resource.call(held_schedule) if held_after_portal_exit else session._call(pending)
            assert entered.is_set() and timeout.value.reason == "deadline"
            calls = tuple(owner_ for owner_, _ in resource._calls._owners.values())
            assert any(call.thread.is_alive() for call in calls)
            assert owner.snapshot()["observationComplete"] is False
            if held_after_portal_exit:
                # Isolate the held caller after the SDK's two-second child-exit grace.
                monkeypatch.setattr(request_children, "CLEANUP_SECONDS", 3.0)
        assert timeout is not None, failure_leaves(outward.value)
        assert timeout.value in failure_leaves(outward.value)
        assert owner.pending_owners == ((resource.name,) if held_after_portal_exit else ())
        assert owner.snapshot()["observationComplete"] is (not held_after_portal_exit)
        assert owner.snapshot()["state"] == "failed"
        assert not resource.thread.is_alive()
        assert any(call.thread.is_alive() for call in calls) is held_after_portal_exit
        assert resource.waited and resource.closed == {"stdin", "stdout", "stderr"}
    finally:
        release.set()
        if resource is not None:
            for call, _ in resource._calls._owners.values():
                if call.thread.ident is not None:
                    call.thread.join(5)
                assert not call.thread.is_alive()
            resource._stop.set()
            resource._client_stop.set()
            resource.thread.join(5)
            assert not resource.thread.is_alive()


def test_transferred_stderr_file_remains_open_until_actual_outside_cleanup(tmp_path):
    replies = tmp_path / "replies.json"
    replies.write_text("[]")
    command = [
        sys.executable,
        str(Path(__file__).with_name("live_content_server.py")),
        str(replies),
    ]
    owner = SupervisedRun.create(authorization(), tmp_path / "run")
    errlog = (tmp_path / "stderr.log").open("w", encoding="utf-8")
    try:
        with owner:
            observed = None
            try:
                transfer = request_stdio.StderrTransfer(errlog)
                with open_session(
                    command, {}, errlog, owner=owner, stderr_transfer=transfer
                ) as session:
                    session._resource.read_seconds = 3
                    assert not errlog.closed
                    observed = [tool.name for tool in session.list_tools().tools]
            except (AttributeError, TypeError) as error:
                observed = {"entry_error": str(error)}
            assert observed == []
            assert not errlog.closed
        assert errlog.closed
        assert owner.pending_owners == () and owner.snapshot()["state"] == "completed"
    finally:
        errlog.close()
