"""Registered attached callers share presend capacity and retain observed wire progress."""

import json
import subprocess
import sys
import time
from contextlib import ExitStack
from pathlib import Path
from threading import Event

import pytest
from http_receiver import WireReceiver

import nci_si_acceptance
import nci_si_mcp
from nci_si_acceptance.request_authorization import Authorization, Limits, RequestGrant
from nci_si_acceptance.request_supervision import SupervisedRun
from nci_si_mcp.http_client import RequestPolicyError

# The first scripted fragment is independently seven bytes before the remaining five.
FRAGMENT_BYTES = 7

CHILD = """
import json,sys
from pathlib import Path
sys.path[:0]=sys.argv[1:3]
import nci_si_acceptance,nci_si_mcp
from nci_si_acceptance.request_authorization import Authorization,Limits,RequestGrant,RunDescriptor
from nci_si_acceptance.request_budget import ControlledRun
from nci_si_acceptance.request_transport import ControlledHttpPolicy
from nci_si_mcp.http_client import HttpClient,RequestPolicyError
if sys.stdin.buffer.read(1)!=b'x':
    sys.exit(2)
descriptor=RunDescriptor.from_record(json.loads(Path(sys.argv[3]).read_text()))
origin,participant,destination,concurrent,total=sys.argv[4:]
grant=RequestGrant('GET',origin+'/owned',(('accept','application/json'),))
auth=Authorization((grant,),Limits(concurrent=int(concurrent),response_bytes=64,total_bytes=int(total)),'a'*64)
run=ControlledRun.attach(descriptor,auth,participant=participant)
client=HttpClient(origin,label='owned',timeout_seconds=5,max_attempts=3,retry_backoff_seconds=0,
    max_response_bytes=64,size_bound='owned',request_policy=ControlledHttpPolicy(run))
try:
    client.get_json('/owned')
    reason=None
except RequestPolicyError as error:
    reason=error.reason
report=Path(destination)
pending=report.with_suffix(".pending")
pending.write_text(json.dumps(dict(reason=reason,core=nci_si_mcp.__file__,harness=nci_si_acceptance.__file__)))
pending.replace(report)
"""


def wait_for(predicate):
    deadline = time.monotonic() + 5
    while not predicate():
        assert time.monotonic() < deadline, "The owned child did not reach its barrier"
        time.sleep(0.01)


@pytest.mark.parametrize(
    "concurrent,total,reason", [(1, 130, "concurrency_limit"), (2, 65, "byte_limit")]
)
def test_registered_children_share_transport_capacity(tmp_path, concurrent, total, reason):
    released = Event()

    def partial(handler):
        handler.wfile.write(b'HTTP/1.1 200 OK\r\nContent-Length: 12\r\n\r\n{"ok": ')
        handler.wfile.flush()
        assert released.wait(5), "The parent did not release its owned response"
        handler.wfile.write(b"true}")

    with WireReceiver([partial]) as receiver, ExitStack() as resources:
        grant = RequestGrant("GET", receiver.origin + "/owned", (("accept", "application/json"),))
        auth = Authorization(
            (grant,), Limits(concurrent=concurrent, response_bytes=64, total_bytes=total), "a" * 64
        )
        owner = SupervisedRun.create(auth, tmp_path)
        descriptor = tmp_path / "descriptor.json"
        descriptor.write_text(json.dumps(owner.run.descriptor.to_record()))
        roots = [
            str(Path(package.__file__).parents[1]) for package in (nci_si_acceptance, nci_si_mcp)
        ]
        children, destinations = [], [tmp_path / f"child-{index}.json" for index in range(2)]
        try:
            with pytest.raises(RequestPolicyError) as stopped, owner:
                for index, destination in enumerate(destinations):
                    participant = f"worker-{index}"
                    child = resources.enter_context(
                        subprocess.Popen(  # noqa: S603 - owned isolated interpreter
                            [
                                sys.executable,
                                "-I",
                                "-c",
                                CHILD,
                                *roots,
                                str(descriptor),
                                receiver.origin,
                                participant,
                                str(destination),
                                str(concurrent),
                                str(total),
                            ],
                            stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                        )
                    )
                    owner.register_child(child, participant=participant, role="worker")
                    children.append(child)
                children[0].stdin.write(b"x")
                children[0].stdin.flush()

                def awaiting_remainder():
                    snapshot = owner.run.snapshot()
                    return (
                        snapshot.consumed_body_bytes == FRAGMENT_BYTES
                        and snapshot.pending_receipts == 1
                    )

                wait_for(awaiting_remainder)
                children[1].stdin.write(b"x")
                children[1].stdin.flush()
                wait_for(destinations[1].exists)
                assert json.loads(destinations[1].read_text())["reason"] == reason
                assert receiver.contacts == 1
                released.set()
                wait_for(destinations[0].exists)
        finally:
            released.set()
        assert stopped.value.reason == reason
        reports = [json.loads(path.read_text()) for path in destinations]
        assert [report["reason"] for report in reports] == ["closed", reason]
        assert all(
            report["core"] == nci_si_mcp.__file__
            and report["harness"] == nci_si_acceptance.__file__
            for report in reports
        )
        assert all(
            child.returncode == 0
            and child.stdin.closed
            and child.stdout.closed
            and child.stderr.closed
            for child in children
        )
        snapshot = owner.run.snapshot()
        assert (
            snapshot.logical,
            snapshot.attempts,
            snapshot.transport_entries,
            receiver.contacts,
        ) == (2, 1, 1, 1)
        assert (snapshot.consumed_body_bytes, snapshot.active, snapshot.pending_receipts) == (
            12,
            0,
            0,
        )
        assert owner.snapshot()["state"] == "failed"
