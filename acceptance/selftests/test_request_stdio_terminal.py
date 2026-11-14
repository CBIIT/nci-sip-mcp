"""Execution failure and positively settled resource custody are separate outcomes."""

import io
import json
import signal
import sys

import anyio
import pytest
from test_request_stdio import _reap_for_test, _stop_child_for_test
from test_request_supervision import authorization, failure_leaves

from nci_si_acceptance.client import open_session
from nci_si_acceptance.request_stdio import StdioOwner
from nci_si_acceptance.request_supervision import SupervisedRun
from nci_si_mcp.errors import RequestPolicyError

CHILD = """import json,signal,sys
mode=sys.argv[1]
if mode=='owner-kill': signal.signal(signal.SIGTERM,signal.SIG_IGN)
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


@pytest.mark.parametrize("mode", ["0", "7", "owner-term", "owner-kill"])
def test_child_exit_status_preserves_terminal_outcome(terminal_case, mode):
    owner, resources, waited, closed = terminal_case
    outward = None
    try:
        with (
            owner,
            open_session(
                [sys.executable, "-c", CHILD, mode], {}, io.StringIO(), timeout=3, owner=owner
            ) as session,
        ):
            assert session.list_tools().tools == []
    except BaseException as error:  # noqa: BLE001 - assert the complete caller outcome below.
        outward = error
    assert len(resources) == 1
    resource = resources[0]
    process = resource.process
    assert process is not None
    expected_code = {"0": 0, "7": 7, "owner-term": -signal.SIGTERM, "owner-kill": -signal.SIGKILL}[
        mode
    ]
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
    assert summary["state"] == ("failed" if mode == "7" else "completed")
    if mode == "7":
        assert outward is not None
        assert any(
            isinstance(error, RequestPolicyError) and error.reason == "transport"
            for error in failure_leaves(outward)
        )
    else:
        assert outward is None
