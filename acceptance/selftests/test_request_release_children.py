"""Real attached children inherit the copied source and authenticated run selection."""

import json
import subprocess
import sys
from contextlib import ExitStack, nullcontext
from pathlib import Path

import pytest
from test_request_release_boundaries import ROOT, ROW, scenario

import nci_si_acceptance
import nci_si_mcp
from nci_si_acceptance.owned_process import stop_child
from nci_si_acceptance.request_budget import ControlledRun
from nci_si_mcp.http_client import RequestPolicyError

CHILD = """
import sys,json
from pathlib import Path
sys.path[:0]=sys.argv[1:3]
import nci_si_acceptance,nci_si_mcp
from nci_si_acceptance.environments import Environment,SURFACES
from nci_si_acceptance.request_catalogue import compile_policy
from nci_si_acceptance.request_authorization import RunDescriptor
from nci_si_acceptance.request_budget import ControlledRun
from nci_si_acceptance.request_transport import ControlledHttpPolicy
from nci_si_mcp.http_client import HttpClient,RequestPolicyError
if sys.stdin.buffer.read(1)!=b'x': sys.exit(2)
descriptor,origin,mode=sys.argv[3:]
environment=Environment('evs-only',{s:origin if s=='evs' else None for s in SURFACES},{},'e'*64)
policy=compile_policy(environment)
run=ControlledRun.attach(RunDescriptor.from_record(json.loads(Path(descriptor).read_text())),
    policy.authorization,participant='owned-worker')
try:
    if mode=='select': policy.select_release(run,channel='monthly')
    else:
        client=HttpClient(origin,label='owned',timeout_seconds=1,max_attempts=1,
            retry_backoff_seconds=0,max_response_bytes=1024,size_bound='owned',
            request_policy=ControlledHttpPolicy(run))
        result=client.get_json('/api/v1/concept/ncit_26.09e',{'list':'C4817','include':'summary'})
except RequestPolicyError as error: result={'reason':error.reason}
print(json.dumps(dict(result=result,imports=[nci_si_acceptance.__file__,nci_si_mcp.__file__,
    ControlledRun.logical.__wrapped__.__code__.co_filename])))
"""


@pytest.mark.parametrize("mode", ["select", "read"])
def test_attached_child_cannot_select_but_can_read_committed_release(tmp_path, mode):
    replies = [] if mode == "select" else [[ROW], {"ok": True}]
    with scenario(tmp_path, replies) as (receiver, policy, owner), ExitStack() as resources:
        descriptor = tmp_path / "descriptor.json"
        descriptor.write_text(json.dumps(owner.run.descriptor.to_record()))
        roots = [
            str(Path(package.__file__).parents[1]) for package in (nci_si_acceptance, nci_si_mcp)
        ]
        expected = pytest.raises(RequestPolicyError) if mode == "select" else nullcontext()
        with expected, owner:
            if mode == "read":
                policy.select_release(owner.run, channel="monthly")
            child = subprocess.Popen(  # noqa: S603 - isolated owned child and explicit source roots
                [
                    sys.executable,
                    "-I",
                    "-W",
                    "error::ResourceWarning",
                    "-c",
                    CHILD,
                    *roots,
                    str(descriptor),
                    receiver.origin,
                    mode,
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            resources.callback(child.stdin.close)
            resources.callback(child.stdout.close)
            resources.callback(child.stderr.close)
            resources.callback(stop_child, child, seconds=1)
            owner.register_child(child, participant="owned-worker", role="worker")
            output, errors = child.communicate(b"x", timeout=5)
            report = json.loads(output)
            assert child.returncode == 0 and errors == b""
            assert report["imports"] == [
                nci_si_acceptance.__file__,
                nci_si_mcp.__file__,
                ControlledRun.logical.__wrapped__.__code__.co_filename,
            ]
            assert report["result"] == (
                {"reason": "authorization"} if mode == "select" else {"ok": True}
            )
        assert (
            child.returncode == 0
            and child.stdin.closed
            and child.stdout.closed
            and child.stderr.closed
        )
        snapshot = owner.run.snapshot()
        expected_counts = (0, 0, 0) if mode == "select" else (2, 2, 2)
        assert (snapshot.logical, snapshot.attempts, snapshot.transport_entries) == expected_counts
        assert receiver.contacts == len(receiver.requests) == expected_counts[0]
        if mode == "read":
            assert receiver.requests[-1][1] == ROOT + "?list=C4817&include=summary"
        assert owner.snapshot()["state"] == ("failed" if mode == "select" else "completed")
