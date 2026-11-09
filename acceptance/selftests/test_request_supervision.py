"""Explicit diagnostic callers own a truthful summary and a finite child lifecycle."""

import importlib
import importlib.util
import json

import pytest

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
