"""The owned adapter preserves the public SDK wire and borrowed diagnostic contract."""

import io
import json
import sys
from pathlib import Path

import anyio
from mcp import types
from mcp.shared.message import SessionMessage
from test_request_stdio import _reap_for_test
from test_request_supervision import authorization

from nci_si_acceptance.client import open_session
from nci_si_acceptance.request_stdio import StdioOwner
from nci_si_acceptance.request_supervision import SupervisedRun


def test_actual_pipes_preserve_utf8_framing_and_deliver_parse_error_as_value(tmp_path):
    sink = io.StringIO()
    owner = SupervisedRun.create(authorization(), tmp_path / "run")
    script = """import json,sys
request=json.loads(sys.stdin.buffer.readline())
assert request['params']=={'value':'漢🧬'}
sys.stderr.buffer.write('漢'.encode()+b'\\xff\\n');sys.stderr.buffer.flush()
sys.stdout.buffer.write(b'not-json\\n')
sys.stdout.buffer.write((json.dumps({'jsonrpc':'2.0','id':request['id'],'result':request['params']},ensure_ascii=False)+'\\n').encode());sys.stdout.buffer.flush()
"""
    resource = StdioOwner(owner, [sys.executable, "-c", script], {}, sink)

    async def exercise():
        try:
            resource.process = await anyio.open_process(resource.command)
            with anyio.fail_after(3):
                async with anyio.create_task_group() as tasks:
                    tasks.start_soon(resource._stdout)
                    tasks.start_soon(resource._stdin)
                    tasks.start_soon(resource._stderr)
                    try:
                        message = types.jsonrpc_message_adapter.validate_json(
                            '{"jsonrpc":"2.0","id":7,"method":"echo","params":{"value":"漢🧬"}}'
                        )
                        await resource.write.send(SessionMessage(message))
                        await resource.write.aclose()
                        malformed = await resource.read.receive()
                        response = await resource.read.receive()
                        assert isinstance(malformed, ValueError)
                        assert response.message.model_dump(by_alias=True, exclude_unset=True) == {
                            "jsonrpc": "2.0",
                            "id": 7,
                            "result": {"value": "漢🧬"},
                        }
                        assert await resource.process.wait() == 0
                    finally:
                        tasks.cancel_scope.cancel()
        finally:
            with anyio.CancelScope(shield=True):
                await _reap_for_test(resource)

    anyio.run(exercise)
    assert sink.getvalue() == "漢�\n" and not sink.closed


def test_repeated_sdk_listing_receives_distinct_server_responses(tmp_path):
    replies = tmp_path / "replies.json"
    replies.write_text(json.dumps([{"tool": "owned", "arguments": {}, "result": {"ok": True}}]))
    owner = SupervisedRun.create(authorization(), tmp_path / "run")
    with (
        owner,
        open_session(
            [sys.executable, str(Path(__file__).with_name("live_content_server.py")), str(replies)],
            {},
            errlog=io.StringIO(),
            timeout=3,
            owner=owner,
        ) as session,
    ):
        listings = [session.list_tools().tools for _ in range(2)]
        assert [[(tool.name, tool.description) for tool in tools] for tools in listings] == [
            [("owned", "listing 1")],
            [("owned", "listing 2")],
        ]
    assert owner.snapshot()["state"] == "completed"
