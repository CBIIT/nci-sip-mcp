"""Explicit diagnostic callers own a truthful summary and a finite child lifecycle."""

import importlib
import importlib.util
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

import nci_si_acceptance
import nci_si_mcp
from nci_si_acceptance import request_budget
from nci_si_acceptance.request_authorization import Authorization, Limits, RequestGrant
from nci_si_mcp.http_client import RequestPolicyError


def authorization():
    return Authorization((RequestGrant("GET", "http://127.0.0.1:9/probe", ()),), Limits(), "a" * 64)


def supervisor(directory):
    # Drive the new public entry point without turning its absence into collection failure.
    name = "nci_si_acceptance.request_supervision"
    assert importlib.util.find_spec(name) is not None, "An owned caller needs SupervisedRun"
    return importlib.import_module(name).SupervisedRun.create(authorization(), directory)


def test_initial_summary_precedes_admission(tmp_path):
    """Given a fresh run, entering publishes strict incomplete state before opening it."""
    owner = supervisor(tmp_path)
    with owner:
        run = owner.run
        summary = json.loads((run.directory / "summary.json").read_text())
        assert summary == {
            "schemaVersion": 1,
            "runId": run.descriptor.run_id,
            "policySha256": "a" * 64,
            "state": "incomplete",
            "limits": {
                "logical": 256,
                "attempts": 768,
                "per_logical": 3,
                "concurrent": 4,
                "seconds": 1800.0,
                "response_bytes": 1048576,
                "total_bytes": 67108864,
            },
            "reservations": {"logical": 0, "attempts": 0, "bodyBytes": 0},
            "observed": {"transportEntries": 0, "bodyBytes": 0, "peakActive": 0},
            "denials": None,
            "observationComplete": True,
        }
        assert run.snapshot().state == "open"
        assert owner.snapshot() == summary
    completed = json.loads((run.directory / "summary.json").read_text())
    assert completed == summary | {"state": "completed"}
    assert run.snapshot().state == "closed"


def test_failed_initial_publication_never_admits_work(tmp_path, monkeypatch):
    """A caller trying admission at the publication boundary must still be refused."""
    owner = supervisor(tmp_path)
    admitted = []

    def fail_replace(_source, _destination):
        try:
            with owner.run.logical(authorization().grants[0]):
                admitted.append(True)
        except RequestPolicyError:
            admitted.append(False)
        raise OSError("private publication path")

    monkeypatch.setattr("os.replace", fail_replace)
    with pytest.raises(RequestPolicyError), owner:
        pytest.fail("A failed initial publication cannot enter the caller body")
    assert admitted == [False]
    assert owner.run.snapshot().logical == 0
    assert not (owner.run.directory / "summary.json").exists()


def test_stale_owner_cannot_revive_admission(tmp_path, monkeypatch):
    owner = supervisor(tmp_path)
    with owner:
        now = request_budget._clock()
        with monkeypatch.context() as clock:
            clock.setattr(request_budget, "_clock", lambda: now + 3)
            with pytest.raises(RequestPolicyError):
                owner.run.check_wait(0)
        with pytest.raises(RequestPolicyError):
            owner.run.check_wait(0)
    assert json.loads((owner.run.directory / "summary.json").read_text())["state"] == "failed"


def test_terminal_refusal_cannot_publish_completed(tmp_path):
    owner = supervisor(tmp_path)
    with owner, pytest.raises(RequestPolicyError):
        owner.run.check_wait(1801)
    summary = json.loads((owner.run.directory / "summary.json").read_text())
    assert summary["state"] == "failed"
    assert summary["observationComplete"] is True


def test_registered_short_worker_is_reaped_and_streams_closed(tmp_path):
    owner = supervisor(tmp_path)
    destination = tmp_path / "child.json"
    descriptor = tmp_path / "descriptor.json"
    descriptor.write_text(json.dumps(owner.run.descriptor.to_record()))
    roots = [str(Path(package.__file__).parents[1]) for package in (nci_si_acceptance, nci_si_mcp)]
    script = (
        "import sys,json; from pathlib import Path; "
        "sys.path[:0]=sys.argv[1:3]; import nci_si_acceptance,nci_si_mcp; "
        "sys.stdin.buffer.read(1); "
        "from nci_si_acceptance.request_authorization import "
        "Authorization,Limits,RequestGrant,RunDescriptor; "
        "from nci_si_acceptance.request_budget import ControlledRun; "
        "auth=Authorization((RequestGrant('GET','http://127.0.0.1:9/probe',()),),Limits(),'a'*64); "
        "descriptor=RunDescriptor.from_record(json.loads(Path(sys.argv[4]).read_text())); "
        "run=ControlledRun.attach(descriptor,auth,participant='worker-1'); "
        "run.check_wait(0); "
        "Path(sys.argv[3]).write_text(json.dumps([sys.executable,"
        "nci_si_acceptance.__file__,nci_si_mcp.__file__]))"
    )
    with subprocess.Popen(  # noqa: S603 - owned interpreter and explicit synthetic child program.
        [sys.executable, "-I", "-c", script, *roots, str(destination), str(descriptor)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ) as child:
        with owner:
            register = getattr(owner, "register_child", None)
            assert callable(register), "An owned run must accept explicit child custody"
            register(child, participant="worker-1", role="worker")
            child.stdin.write(b"x")
            child.stdin.flush()
            deadline = time.monotonic() + 5
            while not destination.exists():
                assert time.monotonic() < deadline, "The registered child did not finish its call"
                time.sleep(0.01)
        assert child.returncode == 0
        assert child.stdin.closed and child.stdout.closed and child.stderr.closed
    assert json.loads(destination.read_text()) == [
        sys.executable,
        nci_si_acceptance.__file__,
        nci_si_mcp.__file__,
    ]
    assert json.loads((owner.run.directory / "summary.json").read_text())["state"] == "completed"
