"""Shared controlled-run accounting at real process and HTTP caller boundaries."""

import json
import sqlite3
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import fields, replace
from pathlib import Path

import pytest

import nci_si_acceptance
from conftest import FIXTURES
from nci_si_acceptance.fixture_server import (
    Fixture,
    FixtureServer,
    FixtureSet,
    Response,
    load_fixtures,
    request_key,
)
from nci_si_acceptance.request_authorization import (
    Authorization,
    Limits,
    RequestGrant,
    RunDescriptor,
)
from nci_si_acceptance.request_budget import AttemptLease, ControlledRun
from nci_si_mcp.http_client import RequestPolicyError

# This explicit local consumer exercises shared accounting through the real HttpClient.
ACTOR = """import json, sys, tempfile
import nci_si_acceptance
from nci_si_mcp import http_client
from contextlib import contextmanager
from pathlib import Path
from urllib.request import ProxyHandler, build_opener
from nci_si_mcp.http_client import HttpClient, RequestPolicyError
from nci_si_mcp.request_policy import Exchange

http_client._OPENER = build_opener(ProxyHandler({}), http_client._SameOriginRedirects())
settings = json.loads(Path(sys.argv[1]).read_text())
sys.stdin.readline()  # The parent authorizes the participant before releasing its child.
import nci_si_acceptance.request_budget as module

class ActorPolicy:
    def __init__(self, run, grant):
        self.run, self.grant = run, grant
    @contextmanager
    def request(self, request):
        if request.full_url != self.grant.url:
            raise RequestPolicyError("authorization")
        self.request_snapshot = request
        with self.run.logical(self.grant) as logical:
            self.logical = logical
            yield self
    def check_wait(self, delay_seconds):
        self.run.check_wait(delay_seconds)
    def exchange(self, *, timeout_seconds, response_limit, on_start):
        with self.logical.attempt(response_limit=response_limit) as lease:
            with self._enter_transport(lease, on_start, timeout_seconds) as response:
                body = lease.read(response.read, response_limit + 1)
                return Exchange(response.status, dict(response.headers), body)
    def _enter_transport(self, lease, on_start, timeout_seconds):
        # This local actor declares entry here; receiver contacts remain independent.
        on_start()
        lease.entered()
        return build_opener(ProxyHandler({})).open(
            self.request_snapshot, timeout=timeout_seconds
        )

try:
    body = settings.get("body")
    grant = module.RequestGrant(
        "GET", settings["url"],
        tuple(tuple(row) for row in settings.get("headers", (("accept", "application/json"),))),
        body.encode() if body is not None else None,
    )
    authorization = module.Authorization(
        (grant,), module.Limits(**settings["limits"]), settings.get("policy", "a" * 64)
    )
    run = module.ControlledRun.attach(
        module.RunDescriptor.from_record(settings["descriptor"]), authorization,
        participant=settings["participant"],
    )
except RequestPolicyError as error:
    print(json.dumps({"accepted": 0, "refused": [error.reason]}))
    sys.exit(0)
policy = ActorPolicy(run, grant)
client = HttpClient(
    settings["base"], label="EVS", timeout_seconds=2, max_attempts=1,
    retry_backoff_seconds=0, max_response_bytes=1024, size_bound="local-test",
    request_policy=policy,
)
accepted, refused = 0, []
for round_number in range(2):
    try:
        client.get_json("/api/v1/version")
        accepted += 1
    except RequestPolicyError as error:
        refused.append(error.reason)
    if round_number == 0 and settings.get("barrier", False):
        Path(sys.argv[1]).with_suffix(".round1").write_text("finished")
        sys.stdin.readline()
print(json.dumps({
    "accepted": accepted, "refused": refused, "interpreter": sys.executable,
    "package": nci_si_acceptance.__file__, "tempdir": tempfile.gettempdir(),
}))
"""


EXPECTED_CONTACTS = 2
EXPECTED_REFUSALS = 2


@contextmanager
def run_context(base, directory, limits):
    grant = RequestGrant("GET", base + "/api/v1/version", (("accept", "application/json"),))
    authorization = Authorization((grant,), Limits(**limits), "a" * 64)
    run = ControlledRun.create(authorization, directory / "runs")
    try:
        run.open()
        yield run
    finally:
        # The test parent owns child joins/reaping, not the ledger.
        run.close()


def stop_actor(child):
    if child.poll() is not None:
        return
    child.terminate()
    try:
        child.wait(timeout=2)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait(timeout=2)


def launch_actor(actor, directory, number, settings, run):
    participant = f"child-{number}"
    settings = settings | {"participant": participant}
    settings["descriptor"] = run.descriptor.to_record()
    config = directory / f"child-{number}.json"
    config.write_text(json.dumps(settings))
    child = subprocess.Popen(  # noqa: S603 - this test wrote the actor and uses its own interpreter.
        [sys.executable, "-I", str(actor), str(config)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        run.authorize_participant(participant)
    except BaseException:
        stop_actor(child)
        raise
    return child


def wait_for_first_round(markers):
    deadline = time.monotonic() + 10
    while not all(path.is_file() for path in markers):
        if time.monotonic() >= deadline:
            raise TimeoutError("Owned actors did not finish their first round")
        time.sleep(0.01)


def release_and_collect(children, markers=()):
    # Release both registered children before waiting, so requests genuinely compete.
    for child in children:
        child.stdin.write(b"ready\n")
        child.stdin.flush()
    if markers:
        wait_for_first_round(markers)
        for child in children:
            child.stdin.write(b"exhaustion\n")
            child.stdin.flush()
    results = []
    for child in children:
        output, error = child.communicate(timeout=10)
        assert child.returncode == 0, error.decode()
        results.append(json.loads(output))
    return results


def test_real_children_share_the_logical_request_ceiling(tmp_path):
    actor = tmp_path / "actor.py"
    actor.write_text(ACTOR)
    limits = {
        "logical": 2,
        "attempts": 6,
        "per_logical": 3,
        "concurrent": 2,
        "seconds": 30,
        "response_bytes": 1024,
        "total_bytes": 16384,
    }
    children, results = [], []
    with FixtureServer(load_fixtures(FIXTURES)) as upstream:
        base = upstream.base_url("evs")
        settings = {
            "base": base,
            "url": base + "/api/v1/version",
            "limits": limits,
            "barrier": True,
        }
        try:
            with run_context(base, tmp_path, limits) as run:
                for number in range(2):
                    children.append(launch_actor(actor, tmp_path, number, settings, run))
                markers = [tmp_path / f"child-{number}.round1" for number in range(2)]
                results = release_and_collect(children, markers)
        except RequestPolicyError:
            # Exhaustion remains a failed diagnostic run even if a child handled its refusal.
            pass
        finally:
            for child in children:
                stop_actor(child)
        assert len(upstream.log()) == EXPECTED_CONTACTS
    assert sum(result["accepted"] for result in results) == EXPECTED_CONTACTS
    assert sum(len(result["refused"]) for result in results) == EXPECTED_REFUSALS
    assert {Path(row["interpreter"]).resolve() for row in results} == {
        Path(sys.executable).resolve()
    }
    assert {Path(row["package"]).resolve() for row in results} == {
        Path(nci_si_acceptance.__file__).resolve()
    }
    assert {Path(row["tempdir"]).resolve() for row in results} == {
        Path(tempfile.gettempdir()).resolve()
    }


@pytest.mark.parametrize("change", ["url", "headers", "body", "limits", "policy"])
def test_attached_child_changed_authorization_has_zero_contacts(tmp_path, change):
    actor = tmp_path / "actor.py"
    actor.write_text(ACTOR)
    limits = {
        "logical": 2,
        "attempts": 6,
        "per_logical": 3,
        "concurrent": 2,
        "seconds": 30,
        "response_bytes": 1024,
        "total_bytes": 16384,
    }
    results, children = [], []
    with FixtureServer(load_fixtures(FIXTURES)) as upstream:
        base = upstream.base_url("evs")
        settings = {"base": base, "url": base + "/api/v1/version", "limits": limits}
        changes = {
            "url": {"url": base + "/unapproved"},
            "headers": {"headers": [["accept", "text/plain"]]},
            "body": {"body": "changed body"},
            "limits": {"limits": limits | {"per_logical": 2}},
            "policy": {"policy": "b" * 64},
        }
        try:
            with run_context(base, tmp_path, limits) as run:
                child = launch_actor(actor, tmp_path, 0, settings | changes[change], run)
                children.append(child)
                results = release_and_collect(children)
        except RequestPolicyError:
            pass
        finally:
            for child in children:
                stop_actor(child)
        assert upstream.log() == []
    assert results == [{"accepted": 0, "refused": ["authorization"]}]


# The storage wait is0.1s; allow finite scheduler headroom, revisiting on measured slow hosts.
CONTENTION_RESPONSE_SECONDS = 2

HELD_ACTOR = """import json, sys
from pathlib import Path
from urllib.request import ProxyHandler, build_opener
from nci_si_acceptance.request_budget import (
    Authorization, ControlledRun, Limits, RequestGrant, RunDescriptor,
)
from nci_si_mcp.http_client import RequestPolicyError

settings = json.loads(Path(sys.argv[1]).read_text())
sys.stdin.readline()
reason = None
try:
    grant = RequestGrant("GET", settings["url"], ())
    authorization = Authorization((grant,), Limits(**settings["limits"]), "a" * 64)
    run = ControlledRun.attach(RunDescriptor.from_record(settings["descriptor"]), authorization,
                               participant=settings["participant"])
    with run.logical(grant) as logical, logical.attempt(response_limit=5) as lease:
        # This raw accounting caller declares transport entry; it is not the production provider.
        lease.entered()
        with build_opener(ProxyHandler({})).open(grant.url, timeout=2) as response:
            def held_read(maximum):
                Path(sys.argv[1]).with_suffix(".reading").write_text("pending")
                sys.stdin.readline()
                return response.read(maximum)
            lease.read(held_read, 6)
except RequestPolicyError as error:
    reason = error.reason
print(json.dumps({"reason": reason}))
"""


@contextmanager
def tiny_run(tmp_path):
    fixtures = FixtureSet(
        {request_key("evs", "GET", "/owned", {}): Fixture("known", (Response(body="known"),))}, {}
    )
    limits = {
        "logical": 2,
        "attempts": 2,
        "per_logical": 1,
        "concurrent": 2,
        "seconds": 30,
        "response_bytes": 5,
        "total_bytes": 6,
    }
    with FixtureServer(fixtures) as upstream:
        grant = RequestGrant("GET", upstream.base_url("evs") + "/owned", ())
        run = ControlledRun.create(
            Authorization((grant,), Limits(**limits), "a" * 64), tmp_path / "runs"
        )
        try:
            run.open()
            yield run, grant, limits, upstream
        finally:
            run.close()


def test_inflight_exhaustion_preserves_admitted_read_receipts(tmp_path):
    actor = tmp_path / "held.py"
    actor.write_text(HELD_ACTOR)
    with tiny_run(tmp_path) as (run, grant, limits, upstream):
        child = launch_actor(actor, tmp_path, 0, {"url": grant.url, "limits": limits}, run)
        try:
            child.stdin.write(b"ready\n")
            child.stdin.flush()
            wait_for_first_round([tmp_path / "child-0.reading"])
            before = run.snapshot()
            assert (
                before.active,
                before.pending_receipts,
                before.reserved_body_bytes,
                before.transport_entries,
                before.consumed_body_bytes,
            ) == (1, 1, 6, 1, 0)
            with (
                pytest.raises(RequestPolicyError) as refused,
                run.logical(grant) as logical,
                logical.attempt(response_limit=5),
            ):
                pytest.fail("Exhausted aggregate capacity admitted another transport")
            assert refused.value.reason == "byte_limit"
            output, error = child.communicate(b"read\n", timeout=10)
            assert child.returncode == 0, error.decode()
            assert json.loads(output) == {"reason": "closed"}
            after = run.snapshot()
            assert (
                after.state,
                after.logical,
                after.attempts,
                after.transport_entries,
                after.consumed_body_bytes,
                after.reserved_body_bytes,
                after.active,
                after.pending_receipts,
                after.observation_complete,
                after.failure,
            ) == ("failed", 2, 1, 1, 5, 5, 0, 0, True, "byte_limit")
            assert len(upstream.log()) == 1
        finally:
            stop_actor(child)


def test_real_sqlite_contention_closes_admission(tmp_path):
    actor = tmp_path / "blocked.py"
    actor.write_text(HELD_ACTOR)
    with tiny_run(tmp_path) as (run, grant, limits, upstream):
        child = launch_actor(actor, tmp_path, 0, {"url": grant.url, "limits": limits}, run)
        try:
            with sqlite3.connect(run.directory / "ledger.sqlite") as blocker:
                blocker.execute("BEGIN IMMEDIATE")
                started = time.monotonic()
                with pytest.raises(RequestPolicyError) as refused:
                    run.check_wait(0)
                # The ledger permits 0.1s contention; allow scheduler headroom in this test.
                assert time.monotonic() - started < CONTENTION_RESPONSE_SECONDS
                assert refused.value.reason == "accounting"
            output, error = child.communicate(b"ready\n", timeout=10)
            assert child.returncode == 0, error.decode()
            assert upstream.log() == []
            assert json.loads(output) == {"reason": "closed"}
            snapshot = run.snapshot()
            assert (
                snapshot.state,
                snapshot.attempts,
                snapshot.transport_entries,
                snapshot.consumed_body_bytes,
                snapshot.failure,
            ) == ("failed", 0, 0, 0, "accounting")
        finally:
            stop_actor(child)


def refusal(construct):
    try:
        construct()
    except (
        RequestPolicyError,
        TypeError,
        ValueError,
        AttributeError,
        OverflowError,
        KeyError,
    ) as error:
        return type(error), str(error), error.__cause__
    return None


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


@pytest.mark.parametrize(
    "query,value",
    [
        ("UPDATE run SET consumed=?", 2),
        ("UPDATE run SET peak=?", 5),
        ("UPDATE run SET state=?", "unexpected"),
    ],
)
def test_snapshot_rejects_incoherent_ledger_values(tmp_path, query, value):
    grant = RequestGrant("GET", "http://127.0.0.1:9/owned", ())
    run = ControlledRun.create(Authorization((grant,), Limits(), "a" * 64), tmp_path)
    try:
        run.open()
        with sqlite3.connect(run.directory / "ledger.sqlite") as connection:
            connection.execute(query, (value,))
        assert refusal(run.snapshot) == (
            RequestPolicyError,
            "Controlled upstream request refused.",
            None,
        )
    finally:
        run.close()


@contextmanager
def retirement_run(tmp_path):
    grant = RequestGrant("GET", "http://127.0.0.1:9/owned", ())
    limits = Limits(
        logical=2, attempts=2, per_logical=1, concurrent=2, response_bytes=5, total_bytes=12
    )
    run = ControlledRun.create(Authorization((grant,), limits, "a" * 64), tmp_path)
    try:
        run.open()
        yield run, grant
    finally:
        run.close()


def retired_lease(run, grant):
    with run.logical(grant) as logical, logical.attempt(response_limit=5) as lease:
        lease.entered()
        assert lease.read(lambda maximum: b"known"[:maximum], 6) == b"known"
    return lease


@pytest.mark.parametrize("closed", [False, True])
def test_release_is_identity_checked_and_idempotent(tmp_path, closed):
    with retirement_run(tmp_path) as (run, grant):
        retired = retired_lease(run, grant)
        with run.logical(grant) as logical, logical.attempt(response_limit=5):
            if closed:
                run.close()
            assert refusal(retired.release) is None
            snapshot = run.snapshot()
            assert (
                snapshot.logical,
                snapshot.attempts,
                snapshot.active,
                snapshot.reserved_body_bytes,
                snapshot.consumed_body_bytes,
                snapshot.pending_receipts,
                snapshot.state,
            ) == (2, 2, 1, 11, 5, 0, "closed" if closed else "open")
        assert (run.snapshot().active, run.snapshot().reserved_body_bytes) == (0, 5)


@pytest.mark.parametrize(
    "change", ["run", "policy", "authorization", "participant", "unknown", "foreign"]
)
def test_retired_release_checks_authority(tmp_path, change):
    with retirement_run(tmp_path) as (run, grant):
        run.authorize_participant("other")
        other = ControlledRun.attach(run.descriptor, run.authorization, participant="other")
        retired = retired_lease(run, grant)
        with run.logical(grant) as logical, logical.attempt(response_limit=5):
            run.close()
            original = run.descriptor, run.authorization, run.participant
            target = retired
            if change == "run":
                run.descriptor = replace(run.descriptor, run_id="b" * 32)
            elif change == "policy":
                run.descriptor = replace(run.descriptor, policy_sha256="b" * 64)
            elif change == "authorization":
                run.authorization = replace(
                    run.authorization, limits=replace(run.authorization.limits, per_logical=2)
                )
            elif change == "participant":
                run.participant = "unregistered"
            elif change == "unknown":
                target = AttemptLease(run, "0" * 32)
            else:
                target = AttemptLease(other, retired.lease_id)
            try:
                observed = refusal(target.release)
            finally:
                run.descriptor, run.authorization, run.participant = original
            assert observed == (RequestPolicyError, "Controlled upstream request refused.", None)
            snapshot = run.snapshot()
            assert (
                snapshot.active,
                snapshot.reserved_body_bytes,
                snapshot.consumed_body_bytes,
                snapshot.pending_receipts,
            ) == (1, 11, 5, 0)


@pytest.mark.parametrize("operation", ["entry", "read"])
def test_retired_lease_refuses_new_entry_or_read(tmp_path, operation):
    with retirement_run(tmp_path) as (run, grant):
        if operation == "entry":
            # A never-entered retired lease must not claim entry after its refund.
            with run.logical(grant) as logical, logical.attempt(response_limit=5) as retired:
                pass
        else:
            # This lease has already entered; its remaining byte was refunded.
            retired = retired_lease(run, grant)
        with run.logical(grant) as logical, logical.attempt(response_limit=5):
            reads = []

            def reader(maximum):
                reads.append(maximum)
                return b"x"[:maximum]

            action = retired.entered if operation == "entry" else lambda: retired.read(reader, 1)
            observed = refusal(action)
            assert reads == []
            assert observed == (RequestPolicyError, "Controlled upstream request refused.", None)
            snapshot = run.snapshot()
            expected = (2, 2, 1, 6, 0, 0, 0) if operation == "entry" else (2, 2, 1, 11, 1, 5, 0)
            assert (
                snapshot.logical,
                snapshot.attempts,
                snapshot.active,
                snapshot.reserved_body_bytes,
                snapshot.transport_entries,
                snapshot.consumed_body_bytes,
                snapshot.pending_receipts,
            ) == expected


@pytest.mark.parametrize(
    "boundary", ["create", "attach_authority", "attach_descriptor", "directory"]
)
@pytest.mark.parametrize("kind", ["none", "boolean", "text", "probe"])
def test_run_boundaries_refuse_shapes_before_field_use(tmp_path, boundary, kind):
    reads = []

    class Probe:
        def __getattr__(self, name):
            reads.append(name)
            raise AttributeError(name)

    value = {"none": None, "boolean": False, "text": "invalid", "probe": Probe()}[kind]
    with retirement_run(tmp_path) as (run, _):
        run.authorize_participant("worker")
        calls = {
            "create": lambda: ControlledRun.create(value, tmp_path / "rejected"),
            "attach_authority": lambda: ControlledRun.attach(
                run.descriptor, value, participant="worker"
            ),
            "attach_descriptor": lambda: ControlledRun.attach(
                value, run.authorization, participant="worker"
            ),
            "directory": lambda: ControlledRun.create(run.authorization, value),
        }
        observed = refusal(calls[boundary])
        assert reads == []
        assert observed == (RequestPolicyError, "Controlled upstream request refused.", None)


@pytest.mark.parametrize("boundary", ["create", "attach_authority", "attach_descriptor"])
def test_run_boundaries_refuse_extended_records(tmp_path, boundary):
    with retirement_run(tmp_path) as (run, _):
        run.authorize_participant("worker")
        value = run.descriptor if boundary == "attach_descriptor" else run.authorization
        extended = type("ExtendedRecord", (type(value),), {})(
            **{field.name: getattr(value, field.name) for field in fields(value)}
        )
        calls = {
            "create": lambda: ControlledRun.create(extended, tmp_path / "rejected"),
            "attach_authority": lambda: ControlledRun.attach(
                run.descriptor, extended, participant="worker"
            ),
            "attach_descriptor": lambda: ControlledRun.attach(
                extended, run.authorization, participant="worker"
            ),
        }
        assert refusal(calls[boundary]) == (
            RequestPolicyError,
            "Controlled upstream request refused.",
            None,
        )
