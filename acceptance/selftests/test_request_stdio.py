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

from nci_si_acceptance import request_children
from nci_si_acceptance.client import open_session
from nci_si_acceptance.request_authorization import Authorization, Limits, RequestGrant
from nci_si_acceptance.request_stdio import StdioOwner
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
            with open_session(command, {}, errlog=errors, timeout=3, owner=owner) as session:
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
                timeout=3,
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
                monkeypatch.setattr(request_children, "CLEANUP_SECONDS", 0.1)
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
                with open_session(
                    command, {}, errlog, owner=owner, timeout=3, own_errlog=True
                ) as session:
                    assert not errlog.closed
                    observed = [tool.name for tool in session.list_tools().tools]
            except TypeError as error:
                observed = {"entry_error": str(error)}
            assert observed == []
            assert not errlog.closed
        assert errlog.closed
        assert owner.pending_owners == () and owner.snapshot()["state"] == "completed"
    finally:
        errlog.close()
