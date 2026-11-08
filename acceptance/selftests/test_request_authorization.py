"""The explicit diagnostic caller's private authority and ledger record contracts."""

from nci_si_acceptance.request_authorization import (
    Authorization,
    Limits,
    RequestGrant,
    RunDescriptor,
)
from nci_si_acceptance.request_budget import ControlledRun


def test_explicit_attachment_and_receipt_snapshot(tmp_path):
    grant = RequestGrant("GET", "http://127.0.0.1:9/owned", ())
    authorization = Authorization((grant,), Limits(), "a" * 64)
    run = ControlledRun.create(authorization, tmp_path)
    try:
        assert run.snapshot().state == "created"
        run.open()
        run.authorize_participant("local-worker")
        descriptor = RunDescriptor.from_record(run.descriptor.to_record())
        attached = ControlledRun.attach(descriptor, authorization, participant="local-worker")
        with attached.logical(grant) as logical, logical.attempt(response_limit=5) as lease:
            lease.entered()
            assert lease.read(lambda maximum: b"known"[:maximum], 6) == b"known"
        snapshot = run.snapshot()
        assert (snapshot.run_id, snapshot.policy_sha256) == (descriptor.run_id, "a" * 64)
        assert (
            snapshot.state,
            snapshot.logical,
            snapshot.attempts,
            snapshot.reserved_body_bytes,
            snapshot.transport_entries,
            snapshot.consumed_body_bytes,
            snapshot.active,
            snapshot.peak_active,
            snapshot.pending_receipts,
            snapshot.observation_complete,
            snapshot.failure,
        ) == ("open", 1, 1, 5, 1, 5, 0, 1, 0, True, None)
    finally:
        run.close()
    assert run.snapshot().state == "closed"
