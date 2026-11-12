"""Shared controlled-run accounting at real process and HTTP caller boundaries."""

import hmac
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import traceback
from contextlib import ExitStack, closing, contextmanager
from dataclasses import FrozenInstanceError, fields, replace
from functools import partial
from pathlib import Path
from types import SimpleNamespace

import pytest

import nci_si_acceptance
import nci_si_acceptance.request_budget as request_budget_module
import nci_si_mcp
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
    DEFAULT_SECONDS,
    MAX_COUNTER,
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
import nci_si_mcp
from nci_si_mcp import http_client
from contextlib import contextmanager
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import ProxyHandler, build_opener
from nci_si_mcp.http_client import HttpClient, RequestPolicyError
from nci_si_mcp.bounds import CallDeadlineError, deadline_scope
from nci_si_mcp.http_client import UpstreamUnavailableError
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
        if settings.get("entry_fault") == "callback":
            with deadline_scope(0):
                on_start()
        on_start()
        lease.entered()
        if settings.get("entry_fault") == "opening":
            raise OSError("synthetic local opening failure")
        try:
            return build_opener(ProxyHandler({})).open(
                self.request_snapshot, timeout=timeout_seconds
            )
        except HTTPError as response:
            return response

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
    settings["base"], label="EVS", timeout_seconds=2, max_attempts=settings.get("max_attempts", 1),
    retry_backoff_seconds=0, max_response_bytes=1024, size_bound="local-test",
    request_policy=policy,
)
accepted, refused = 0, []
for round_number in range(settings.get("rounds", 2)):
    try:
        client.get_json(settings.get("path", "/api/v1/version"))
        accepted += 1
    except RequestPolicyError as error:
        refused.append(error.reason)
    except (CallDeadlineError, UpstreamUnavailableError) as error:
        refused.append(type(error).__name__)
    if round_number == 0 and settings.get("barrier", False):
        Path(sys.argv[1]).with_suffix(".round1").write_text("finished")
        sys.stdin.readline()
print(json.dumps({
    "accepted": accepted, "refused": refused, "interpreter": sys.executable,
    "package": nci_si_acceptance.__file__, "core": nci_si_mcp.__file__,
    "tempdir": tempfile.gettempdir(),
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
    if child.poll() is None:
        child.terminate()
        try:
            child.wait(timeout=2)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=2)
    if child.stdout is not None and not child.stdout.closed:
        child.communicate(timeout=2)


def launch_actor(actor, directory, number, settings, run):
    participant = f"child-{number}"
    settings = settings | {"participant": participant}
    settings["descriptor"] = run.descriptor.to_record()
    config = directory / f"child-{number}.json"
    config.write_text(json.dumps(settings))
    # Isolated children must use this execution tree, including mutation snapshots.
    bootstrap = (
        "import runpy, sys; sys.path[:0] = sys.argv[1:3]; "
        "sys.argv = sys.argv[3:]; runpy.run_path(sys.argv[0], run_name='__main__')"
    )
    roots = [str(Path(package.__file__).parents[1]) for package in (nci_si_acceptance, nci_si_mcp)]
    child = subprocess.Popen(  # noqa: S603 - this test wrote the actor and uses its own interpreter.
        [sys.executable, "-I", "-c", bootstrap, *roots, str(actor), str(config)],
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
        except RequestPolicyError as error:
            error.add_note(f"Unexpected parent policy refusal: {error.reason}")
            raise
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
    assert {Path(row["core"]).resolve() for row in results} == {Path(nci_si_mcp.__file__).resolve()}
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
            # Controller cleanup can surface the expected policy refusal.
            # Independently verify child rejection and zero receiver contacts below.
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
def tiny_run(tmp_path, **overrides):
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
    limits.update(overrides)
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
            with closing(sqlite3.connect(run.directory / "ledger.sqlite")) as blocker, blocker:
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
        ("UPDATE run SET logical=?", None),
        ("UPDATE run SET logical=?", -1),
        ("UPDATE run SET attempts=?", -1),
        ("UPDATE run SET reserved=?", -1),
        ("UPDATE run SET entered=?", -1),
        ("UPDATE run SET consumed=?", -1),
        ("UPDATE run SET active=?", -1),
        ("UPDATE run SET peak=?", -1),
        ("UPDATE run SET logical=?", 3),
        ("UPDATE run SET logical=2,attempts=?", 4),
        ("UPDATE run SET logical=1,attempts=?", 3),
        ("UPDATE run SET logical=1,attempts=1,entered=?", 2),
        ("UPDATE run SET logical=1,attempts=1,active=?", 1),
        ("UPDATE run SET logical=1,attempts=2,peak=?", 2),
        ("UPDATE run SET peak=?", 1),
        ("UPDATE run SET reserved=?", 13),
        (None, "unexpected"),
    ],
)
def test_snapshot_rejects_incoherent_ledger_values(tmp_path, query, value):
    grant = RequestGrant("GET", "http://127.0.0.1:9/owned", ())
    limits = Limits(
        logical=2, attempts=3, per_logical=2, concurrent=1, response_bytes=5, total_bytes=12
    )
    run = ControlledRun.create(Authorization((grant,), limits, "a" * 64), tmp_path)
    try:
        run.open()
        if query is None:
            (run.directory / "failed").write_text(value)
        else:
            with (
                closing(sqlite3.connect(run.directory / "ledger.sqlite")) as connection,
                connection,
            ):
                connection.execute(query, (value,))
        assert refusal(run.snapshot) == (
            RequestPolicyError,
            "Controlled upstream request refused.",
            None,
        )
    finally:
        if query is None:
            (tmp_path / "faulted-failure.txt").write_bytes((run.directory / "failed").read_bytes())
            (run.directory / "failed").unlink()
        run.close()


@contextmanager
def retirement_run(
    tmp_path,
    *,
    opened=True,
    total_bytes=12,
    response_bytes=5,
    per_logical=1,
    seconds=DEFAULT_SECONDS,
):
    grant = RequestGrant("GET", "http://127.0.0.1:9/owned", ())
    limits = Limits(
        logical=2,
        attempts=2,
        per_logical=per_logical,
        concurrent=2,
        seconds=seconds,
        response_bytes=response_bytes,
        total_bytes=total_bytes,
    )
    run = ControlledRun.create(Authorization((grant,), limits, "a" * 64), tmp_path)
    try:
        if opened:
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


@pytest.mark.parametrize("operation", ["create", "wait"])
@pytest.mark.parametrize(
    "clock",
    [
        float("nan"),
        float("inf"),
        float("-inf"),
        None,
        True,
        "invalid",
        pytest.param(1 << 4096, id="overflow"),
    ],
)
def test_invalid_monotonic_clock_is_terminal(tmp_path, monkeypatch, operation, clock):
    with retirement_run(tmp_path) as (run, _):
        calls = {
            "create": lambda: ControlledRun.create(run.authorization, tmp_path / "invalid-clock"),
            "wait": lambda: run.check_wait(0),
        }

        def invoke():
            try:
                calls[operation]()
            except RequestPolicyError as error:
                assert error.reason == "accounting"
                raise

        with monkeypatch.context() as patch:
            patch.setattr(
                "nci_si_acceptance.request_budget.time", SimpleNamespace(monotonic=lambda: clock)
            )
            observed = refusal(invoke)
        assert observed == (RequestPolicyError, "Controlled upstream request refused.", None)


def test_unrepresentable_run_deadline_is_terminal(tmp_path, monkeypatch):
    with retirement_run(tmp_path) as (run, _):
        authority = replace(
            run.authorization, limits=replace(run.authorization.limits, seconds=1e308)
        )
        with monkeypatch.context() as patch:
            patch.setattr(
                "nci_si_acceptance.request_budget.time", SimpleNamespace(monotonic=lambda: 1e308)
            )
            observed = refusal(lambda: ControlledRun.create(authority, tmp_path / "overflow-clock"))
        assert observed == (RequestPolicyError, "Controlled upstream request refused.", None)


@pytest.mark.parametrize("clock", [-1.5, 0.0, 1.5])
def test_finite_monotonic_origins_are_supported(tmp_path, monkeypatch, clock):
    with retirement_run(tmp_path) as (parent, _), monkeypatch.context() as patch:
        patch.setattr(
            "nci_si_acceptance.request_budget.time", SimpleNamespace(monotonic=lambda: clock)
        )
        run = ControlledRun.create(parent.authorization, tmp_path / "finite-clock")
        try:
            run.open()
            run.check_wait(0)
            snapshot = run.snapshot()
            assert (snapshot.state, snapshot.logical, snapshot.attempts) == ("open", 0, 0)
        finally:
            run.close()


@pytest.mark.parametrize("invalid", [False, True])
def test_rejected_snapshot_closes_other_participant_admission(tmp_path, invalid):
    with retirement_run(tmp_path) as (run, grant):
        run.authorize_participant("other")
        other = ControlledRun.attach(run.descriptor, run.authorization, participant="other")
        if invalid:
            with (
                closing(sqlite3.connect(run.directory / "ledger.sqlite")) as connection,
                connection,
            ):
                connection.execute("UPDATE run SET logical=?", (-1,))
            with pytest.raises(RequestPolicyError) as rejected:
                run.snapshot()
            assert rejected.value.reason == "accounting"
        else:
            assert run.snapshot().logical == 0

        def enter():
            # Declared ledger entry is independent of a receiver contact.
            with other.logical(grant) as logical, logical.attempt(response_limit=5) as lease:
                lease.entered()

        observed = refusal(enter)
        expected = (
            (RequestPolicyError, "Controlled upstream request refused.", None) if invalid else None
        )
        assert observed == expected
        with closing(sqlite3.connect(run.directory / "ledger.sqlite")) as connection, connection:
            quantities = connection.execute("SELECT logical,attempts,entered FROM run").fetchone()
        assert quantities == ((-1, 0, 0) if invalid else (1, 1, 1))


@pytest.mark.parametrize(
    "clock,failure",
    [
        (None, "accounting"),
        (float("nan"), "accounting"),
        pytest.param(OSError("synthetic-private-clock-detail"), "accounting", id="raised-oserror"),
        ("deadline", "deadline"),
        ("close", "closed"),
        pytest.param("transient", "accounting", id="recovered-receipt-clock"),
    ],
)
def test_known_read_survives_later_refusal(tmp_path, monkeypatch, clock, failure):
    with retirement_run(tmp_path) as (run, grant):
        with run.logical(grant) as logical, logical.attempt(response_limit=5) as lease:
            lease.entered()
            with monkeypatch.context() as patch:
                clock_values = iter([None])

                def monotonic():
                    if clock == "deadline":
                        return run.started + run.authorization.limits.seconds
                    if isinstance(clock, OSError):
                        raise clock
                    if clock == "transient":
                        return next(clock_values, run.started)
                    return clock

                def reader(maximum):
                    if clock == "close":
                        run.close()
                    else:
                        patch.setattr(
                            "nci_si_acceptance.request_budget.time",
                            SimpleNamespace(monotonic=monotonic),
                        )
                    return b"known"[:maximum]

                def read():
                    try:
                        lease.read(reader, 6)
                    except RequestPolicyError as error:
                        assert "synthetic-private-clock-detail" not in "".join(
                            traceback.format_exception(error)
                        )
                        raise

                observed = refusal(read)
            assert observed == (RequestPolicyError, "Controlled upstream request refused.", None)
            snapshot = run.snapshot()
            assert (
                snapshot.state,
                snapshot.active,
                snapshot.pending_receipts,
                snapshot.reserved_body_bytes,
                snapshot.consumed_body_bytes,
                snapshot.failure,
            ) == ("failed", 1, 0, 6, 5, failure)
        snapshot = run.snapshot()
        assert (
            snapshot.active,
            snapshot.pending_receipts,
            snapshot.reserved_body_bytes,
            snapshot.consumed_body_bytes,
            snapshot.observation_complete,
        ) == (0, 0, 5, 5, True)


@pytest.mark.parametrize("directory_exists", [False, True])
def test_attach_never_creates_missing_state(tmp_path, directory_exists):
    with retirement_run(tmp_path) as (run, _):
        missing = tmp_path / "missing"
        if directory_exists:
            missing.mkdir()
        descriptor = replace(run.descriptor, directory=missing)
        assert refusal(
            lambda: ControlledRun.attach(descriptor, run.authorization, participant="worker")
        ) == (RequestPolicyError, "Controlled upstream request refused.", None)
        assert not (missing / "ledger.sqlite").exists()
        assert not (missing / "key").exists()
        assert (run.snapshot().state, run.snapshot().logical) == ("open", 0)


@pytest.mark.parametrize("operation", ["open", "register", "close", "reopen", "attach_closed"])
def test_only_controller_can_change_run_lifecycle(tmp_path, operation):
    with retirement_run(tmp_path) as (run, _):
        run.authorize_participant("worker")
        worker = ControlledRun.attach(run.descriptor, run.authorization, participant="worker")
        if operation == "attach_closed":
            run.close()
        calls = {
            "open": worker.open,
            "register": lambda: worker.authorize_participant("unauthorized"),
            "close": worker.close,
            "reopen": run.open,
            "attach_closed": lambda: ControlledRun.attach(
                run.descriptor, run.authorization, participant="worker"
            ),
        }
        assert refusal(calls[operation]) == (
            RequestPolicyError,
            "Controlled upstream request refused.",
            None,
        )
        with closing(sqlite3.connect(run.directory / "ledger.sqlite")) as connection, connection:
            state = connection.execute("SELECT state FROM run").fetchone()[0]
            workers = connection.execute("SELECT name FROM workers ORDER BY name").fetchall()
        assert state == ("closed" if operation == "attach_closed" else "open")
        assert workers == [("controller",), ("worker",)]
        assert (run.snapshot().state, run.snapshot().logical, run.snapshot().attempts) == (
            "failed",
            0,
            0,
        )


@contextmanager
def changed_run_identity(run, change, monkeypatch):
    grant = run.authorization.grants[0]
    attributes = {
        "run": ("descriptor", replace(run.descriptor, run_id="b" * 32)),
        "policy": ("descriptor", replace(run.descriptor, policy_sha256="b" * 64)),
        "participant": ("participant", "unregistered"),
        "authorization": (
            "authorization",
            replace(run.authorization, limits=replace(run.authorization.limits, per_logical=2)),
        ),
        "method": (
            "authorization",
            replace(run.authorization, grants=(replace(grant, method="POST"),)),
        ),
        "credential": (
            "authorization",
            replace(
                run.authorization, grants=(replace(grant, credential_origin="http://127.0.0.1:9"),)
            ),
        ),
    }
    stored = {
        "stored_run": (0, "b" * 32),
        "stored_policy": (1, "b" * 64),
        "origin": (2, run.started + 1),
    }
    key = run.directory / "key"
    original_key = key.read_bytes()
    with closing(sqlite3.connect(run.directory / "ledger.sqlite")) as connection, connection:
        original = connection.execute("SELECT id,policy,started FROM run").fetchone()
    changed = list(original)
    index, value = stored.get(change, (2, run.started))
    changed[index] = value
    with monkeypatch.context() as patch:
        try:
            if change in attributes:
                patch.setattr(run, *attributes[change])
            if change == "key":
                key.write_bytes(b"changed".ljust(32, b"!"))
            if change in stored:
                with (
                    closing(sqlite3.connect(run.directory / "ledger.sqlite")) as connection,
                    connection,
                ):
                    connection.execute("UPDATE run SET id=?,policy=?,started=?", changed)
            yield
        finally:
            if change == "key":
                key.write_bytes(original_key)
            if change in stored:
                with (
                    closing(sqlite3.connect(run.directory / "ledger.sqlite")) as connection,
                    connection,
                ):
                    connection.execute("UPDATE run SET id=?,policy=?,started=?", original)


def receipt_quantities(run, lease):
    with closing(sqlite3.connect(run.directory / "ledger.sqlite")) as connection, connection:
        ledger = connection.execute("SELECT active,reserved,consumed FROM run").fetchone()
        receipt = connection.execute(
            "SELECT consumed,pending,retired FROM leases WHERE id=?", (lease.lease_id,)
        ).fetchone()
    return ledger, receipt


@pytest.mark.parametrize(
    "change",
    [
        "valid",
        "run",
        "stored_run",
        "policy",
        "stored_policy",
        "participant",
        "authorization",
        "method",
        "credential",
        "key",
        "origin",
    ],
)
@pytest.mark.parametrize(
    "operation",
    [
        "open",
        "close",
        "register",
        "wait",
        "logical",
        "attempt",
        "entry",
        "read",
        "receipt",
        "release",
        "snapshot",
    ],
)
def test_every_ledger_operation_checks_identity(tmp_path, monkeypatch, change, operation):
    with (
        retirement_run(tmp_path, opened=operation != "open") as (run, grant),
        ExitStack() as resources,
        ExitStack() as identities,
    ):
        logical = (
            resources.enter_context(run.logical(grant))
            if operation in {"attempt", "entry", "read", "receipt", "release"}
            else None
        )
        lease = (
            resources.enter_context(logical.attempt(response_limit=5))
            if operation in {"entry", "read", "receipt", "release"}
            else None
        )
        if operation in {"read", "receipt"}:
            lease.entered()
        reads = []

        def reader(maximum):
            reads.append(maximum)
            if operation == "receipt":
                identities.enter_context(changed_run_identity(run, change, monkeypatch))
            return b"known"[:maximum]

        if operation != "receipt":
            identities.enter_context(changed_run_identity(run, change, monkeypatch))
        calls = {
            "open": run.open,
            "close": run.close,
            "register": lambda: run.authorize_participant("new-worker"),
            "wait": lambda: run.check_wait(0),
            "logical": lambda: resources.enter_context(run.logical(grant)),
            "attempt": lambda: resources.enter_context(logical.attempt(response_limit=5)),
            "entry": partial(AttemptLease.entered, lease),
            "read": lambda: lease.read(reader, 6),
            "receipt": lambda: lease.read(reader, 6),
            "release": partial(AttemptLease.release, lease),
            "snapshot": run.snapshot,
        }
        observed = refusal(calls[operation])
        assert observed == (
            None
            if change == "valid"
            else (RequestPolicyError, "Controlled upstream request refused.", None)
        )
        assert reads == (
            [6] if operation == "receipt" or (operation == "read" and change == "valid") else []
        )

        if operation == "receipt":
            assert receipt_quantities(run, lease) == (
                ((1, 6, 5), (5, 0, 0)) if change == "valid" else ((1, 6, 0), (0, 6, 0))
            )
    if operation == "receipt":
        assert receipt_quantities(run, lease) == (
            ((0, 5, 5), (5, 0, 1)) if change == "valid" else ((1, 6, 0), (0, 6, 0))
        )


@pytest.mark.parametrize(
    "attempts,per_logical,expected_accepted,expected_refused,expected_ledger",
    [
        (2, 2, (1, 0), ((), ("attempt_limit",)), (2, 2, 2, "attempt_limit")),
        (4, 1, (0, 0), (("attempt_limit",), ("closed",)), (1, 1, 1, "attempt_limit")),
        (3, 2, (1, 1), ((), ()), (2, 3, 3, None)),
    ],
)
def test_children_share_global_and_per_logical_attempt_caps(
    tmp_path, attempts, per_logical, expected_accepted, expected_refused, expected_ledger
):
    actor = tmp_path / "retry.py"
    actor.write_text(ACTOR)
    fixtures = FixtureSet(
        {
            request_key("evs", "GET", "/api/v1/version", {}): Fixture(
                "retry", (Response(status=503, body={}), Response(body={}))
            )
        },
        {},
    )
    limits = {
        "logical": 4,
        "attempts": attempts,
        "per_logical": per_logical,
        "concurrent": 2,
        "seconds": 30,
        "response_bytes": 1024,
        "total_bytes": 8192,
    }
    with (
        FixtureServer(fixtures) as upstream,
        run_context(upstream.base_url("evs"), tmp_path, limits) as run,
    ):
        settings = {
            "url": run.authorization.grants[0].url,
            "base": upstream.base_url("evs"),
            "limits": limits,
            "max_attempts": 2,
            "rounds": 1,
        }
        children = []
        try:
            children.append(launch_actor(actor, tmp_path, 0, settings | {"barrier": True}, run))
            children.append(launch_actor(actor, tmp_path, 1, settings, run))
            children[0].stdin.write(b"ready\n")
            children[0].stdin.flush()
            wait_for_first_round([tmp_path / "child-0.round1"])
            second = release_and_collect([children[1]])[0]
            first = release_and_collect([children[0]])[0]
            assert (first["accepted"], second["accepted"]) == expected_accepted
            assert (tuple(first["refused"]), tuple(second["refused"])) == expected_refused
            snapshot = run.snapshot()
            assert (
                snapshot.logical,
                snapshot.attempts,
                snapshot.transport_entries,
                snapshot.failure,
            ) == expected_ledger
            assert len(upstream.log()) == expected_ledger[2]
            assert (snapshot.active, snapshot.pending_receipts, snapshot.observation_complete) == (
                0,
                0,
                True,
            )
        finally:
            for child in children:
                stop_actor(child)


def test_other_process_holds_concurrency_until_its_receipt(tmp_path):
    actor = tmp_path / "concurrent.py"
    actor.write_text(HELD_ACTOR)
    with tiny_run(tmp_path, concurrent=1, total_bytes=12) as (run, grant, limits, upstream):
        child = launch_actor(actor, tmp_path, 0, {"url": grant.url, "limits": limits}, run)
        try:
            child.stdin.write(b"ready\n")
            child.stdin.flush()
            wait_for_first_round([tmp_path / "child-0.reading"])
            with (
                pytest.raises(RequestPolicyError) as refused,
                run.logical(grant) as logical,
                logical.attempt(response_limit=5),
            ):
                pytest.fail("Active child capacity admitted another reservation")
            assert refused.value.reason == "concurrency_limit"
            assert len(upstream.log()) == 1
            output, error = child.communicate(b"read\n", timeout=10)
            assert child.returncode == 0, error.decode()
            assert json.loads(output) == {"reason": "closed"}
            snapshot = run.snapshot()
            assert (snapshot.logical, snapshot.attempts, snapshot.transport_entries) == (2, 1, 1)
            assert (snapshot.consumed_body_bytes, snapshot.reserved_body_bytes) == (5, 5)
            assert (snapshot.active, snapshot.pending_receipts, snapshot.observation_complete) == (
                0,
                0,
                True,
            )
            assert snapshot.failure == "concurrency_limit"
        finally:
            stop_actor(child)


@pytest.mark.parametrize("layout", ["valid", "missing", "duplicate"])
def test_snapshot_requires_one_run_record(tmp_path, layout):
    with (
        retirement_run(tmp_path) as (run, _),
        closing(sqlite3.connect(run.directory / "ledger.sqlite")) as storage,
        storage,
    ):
        storage.execute("CREATE TEMP TABLE saved_run AS SELECT * FROM run")
        storage.execute(
            {
                "valid": "SELECT 1",
                "missing": "DELETE FROM run",
                "duplicate": "INSERT INTO run SELECT * FROM run",
            }[layout]
        )
        storage.commit()
        try:
            assert refusal(run.snapshot) == (
                None
                if layout == "valid"
                else (RequestPolicyError, "Controlled upstream request refused.", None)
            )
        finally:
            # Preserve actual fault bytes before restoring only this fixture for cleanup.
            (tmp_path / "faulted-run-ledger.sqlite").write_bytes(
                (run.directory / "ledger.sqlite").read_bytes()
            )
            storage.execute("DELETE FROM run")
            storage.execute("INSERT INTO run SELECT * FROM saved_run")


@pytest.mark.parametrize("requested,allowance", [(3, 4), (5, 6), (7, 6)])
@pytest.mark.parametrize("extra", [False, True])
def test_response_allowance_includes_only_its_sentinel(tmp_path, requested, allowance, extra):
    with (
        retirement_run(tmp_path) as (run, grant),
        run.logical(grant) as logical,
        logical.attempt(response_limit=requested) as lease,
    ):
        assert run.snapshot().reserved_body_bytes == allowance
        lease.entered()
        reads = []

        def reader(maximum):
            reads.append(maximum)
            return b"x" * maximum

        observed = refusal(lambda: lease.read(reader, allowance + int(extra)))
        assert reads == ([] if extra else [allowance])
        assert observed == (
            (RequestPolicyError, "Controlled upstream request refused.", None) if extra else None
        )
        assert run.snapshot().consumed_body_bytes == (0 if extra else allowance)


@pytest.mark.parametrize("total_bytes,exhausted", [(10, True), (11, False)])
def test_consumed_bytes_leave_room_for_the_next_sentinel(tmp_path, total_bytes, exhausted):
    with retirement_run(tmp_path, total_bytes=total_bytes) as (run, grant):
        retired_lease(run, grant)

        def reserve():
            with run.logical(grant) as logical, logical.attempt(response_limit=5):
                pass

        assert refusal(reserve) == (
            (RequestPolicyError, "Controlled upstream request refused.", None)
            if exhausted
            else None
        )
        snapshot = run.snapshot()
        assert (snapshot.logical, snapshot.attempts, snapshot.transport_entries) == (
            2,
            1 if exhausted else 2,
            1,
        )
        assert (snapshot.reserved_body_bytes, snapshot.consumed_body_bytes) == (5, 5)
        assert snapshot.failure == ("byte_limit" if exhausted else None)


def test_large_reservation_refuses_before_integer_promotion(tmp_path):
    with (
        retirement_run(tmp_path, total_bytes=MAX_COUNTER, response_bytes=MAX_COUNTER // 2) as (
            run,
            grant,
        ),
        run.logical(grant) as first,
        first.attempt(response_limit=MAX_COUNTER),
    ):
        with (
            pytest.raises(RequestPolicyError) as refused,
            run.logical(grant) as second,
            second.attempt(response_limit=MAX_COUNTER),
        ):
            pytest.fail("Overflowing reservation was admitted")
        assert refused.value.reason == "byte_limit"


def test_unavailable_receipt_storage_retains_uncertainty(tmp_path):
    with retirement_run(tmp_path) as (run, grant):
        with run.logical(grant) as logical, logical.attempt(response_limit=5) as lease:
            lease.entered()
            storage = sqlite3.connect(run.directory / "ledger.sqlite")
            returned = []

            def reader(maximum):
                storage.execute("BEGIN IMMEDIATE")
                returned.append(b"known"[:maximum])
                return returned[-1]

            try:
                with pytest.raises(RequestPolicyError) as refused:
                    lease.read(reader, 6)
                assert refused.value.reason == "accounting"
            finally:
                storage.rollback()
                storage.close()
            assert returned == [b"known"]
            assert receipt_quantities(run, lease) == ((1, 6, 0), (0, 6, 0))
        assert receipt_quantities(run, lease) == ((1, 6, 0), (0, 6, 0))
    snapshot = run.snapshot()
    assert (snapshot.active, snapshot.pending_receipts, snapshot.observation_complete) == (
        1,
        1,
        False,
    )
    with closing(sqlite3.connect(run.directory / "ledger.sqlite")) as storage, storage:
        storage.execute("UPDATE run SET active=0")
    assert refusal(run.snapshot) == (
        RequestPolicyError,
        "Controlled upstream request refused.",
        None,
    )


@pytest.mark.parametrize("fault", [OSError, ValueError])
def test_reader_failure_retains_unknown_progress(tmp_path, fault):
    with (
        retirement_run(tmp_path) as (run, grant),
        run.logical(grant) as logical,
        logical.attempt(response_limit=5) as lease,
    ):
        lease.entered()

        def reader(_maximum):
            raise fault("synthetic-private-reader-detail")

        with pytest.raises(RequestPolicyError) as refused:
            lease.read(reader, 6)
        assert refused.value.reason == "transport"
        assert "synthetic-private-reader-detail" not in "".join(
            traceback.format_exception(refused.value)
        )
        assert receipt_quantities(run, lease) == ((1, 6, 0), (0, 6, 0))
    assert receipt_quantities(run, lease) == ((1, 6, 0), (0, 6, 0))


def test_unavailable_release_keeps_known_bytes_until_cleanup(tmp_path):
    with retirement_run(tmp_path) as (run, grant):
        with run.logical(grant) as logical, logical.attempt(response_limit=5) as lease:
            lease.entered()
            assert lease.read(lambda maximum: b"known"[:maximum], 6) == b"known"
            storage = sqlite3.connect(run.directory / "ledger.sqlite")
            try:
                storage.execute("BEGIN IMMEDIATE")
                with pytest.raises(RequestPolicyError) as refused:
                    lease.release()
                assert refused.value.reason == "accounting"
            finally:
                storage.rollback()
                storage.close()
            assert receipt_quantities(run, lease) == ((1, 6, 5), (5, 0, 0))
        assert receipt_quantities(run, lease) == ((0, 5, 5), (5, 0, 1))


def test_reaped_child_does_not_reclaim_unknown_progress(tmp_path):
    actor = tmp_path / "reaped.py"
    actor.write_text(HELD_ACTOR)
    with tiny_run(tmp_path) as (run, grant, limits, upstream):
        child = launch_actor(actor, tmp_path, 0, {"url": grant.url, "limits": limits}, run)
        try:
            child.stdin.write(b"ready\n")
            child.stdin.flush()
            wait_for_first_round([tmp_path / "child-0.reading"])
            stop_actor(child)
            assert child.poll() is not None
            run.close()
            snapshot = run.snapshot()
            assert (
                snapshot.state,
                snapshot.failure,
                snapshot.active,
                snapshot.pending_receipts,
            ) == ("closed", None, 1, 1)
            assert (
                snapshot.reserved_body_bytes,
                snapshot.consumed_body_bytes,
                snapshot.observation_complete,
            ) == (6, 0, False)
            assert len(upstream.log()) == 1
        finally:
            stop_actor(child)


@pytest.mark.parametrize(
    "delay,reason",
    [
        (0, None),
        (0.5, None),
        (-1, "authorization"),
        (True, "authorization"),
        (False, "authorization"),
        (None, "authorization"),
        ("1", "authorization"),
        (float("nan"), "authorization"),
        (float("inf"), "authorization"),
        (float("-inf"), "authorization"),
        (1 << 4096, "authorization"),
        ("deadline", "deadline"),
    ],
)
def test_wait_boundary(tmp_path, monkeypatch, delay, reason):
    with retirement_run(tmp_path) as (run, _), monkeypatch.context() as patch:
        patch.setattr(
            "nci_si_acceptance.request_budget.time", SimpleNamespace(monotonic=lambda: run.started)
        )
        if delay == "deadline":
            delay = run.authorization.limits.seconds
        if reason is None:
            run.check_wait(delay)
            snapshot = run.snapshot()
            assert (snapshot.state, snapshot.logical, snapshot.attempts) == ("open", 0, 0)
        else:
            with pytest.raises(RequestPolicyError) as refused:
                run.check_wait(delay)
            assert (refused.value.reason, str(refused.value)) == (
                reason,
                "Controlled upstream request refused.",
            )


def test_storage_failure_suppresses_private_traceback(tmp_path):
    with retirement_run(tmp_path / "synthetic-private-storage-detail") as (run, _):
        key = run.directory / "key"
        saved = run.directory / "key.saved"
        key.rename(saved)
        try:
            with pytest.raises(RequestPolicyError) as refused:
                run.snapshot()
            assert (refused.value.reason, str(refused.value)) == (
                "accounting",
                "Controlled upstream request refused.",
            )
            assert "synthetic-private-storage-detail" not in "".join(
                traceback.format_exception(refused.value)
            )
        finally:
            saved.rename(key)


@pytest.mark.parametrize(
    "fault,entries,reason",
    [("callback", 0, "CallDeadlineError"), ("opening", 1, "UpstreamUnavailableError")],
)
def test_callback_and_declared_entry_do_not_imply_receiver_contact(
    tmp_path, fault, entries, reason
):
    actor = tmp_path / "entry.py"
    actor.write_text(ACTOR)
    with tiny_run(tmp_path) as (run, grant, limits, upstream):
        settings = {
            "url": grant.url,
            "base": grant.url.removesuffix("/owned"),
            "path": "/owned",
            "headers": [],
            "limits": limits,
            "rounds": 1,
            "entry_fault": fault,
        }
        child = launch_actor(actor, tmp_path, 0, settings, run)
        try:
            result = release_and_collect([child])[0]
            assert (result["accepted"], result["refused"]) == (0, [reason])
            snapshot = run.snapshot()
            assert (snapshot.logical, snapshot.attempts, snapshot.transport_entries) == (
                1,
                1,
                entries,
            )
            assert (
                snapshot.active,
                snapshot.pending_receipts,
                snapshot.reserved_body_bytes,
                snapshot.consumed_body_bytes,
                snapshot.observation_complete,
            ) == (0, 0, 0, 0, True)
            assert upstream.log() == []
        finally:
            stop_actor(child)


def test_unentered_lease_refuses_before_invoking_reader(tmp_path):
    with retirement_run(tmp_path) as (run, grant):
        with run.logical(grant) as logical, logical.attempt(response_limit=5) as lease:
            reads = []

            def reader(maximum):
                reads.append(maximum)
                return b"known"[:maximum]

            assert refusal(lambda: lease.read(reader, 6)) == (
                RequestPolicyError,
                "Controlled upstream request refused.",
                None,
            )
            assert reads == []
            snapshot = run.snapshot()
            assert (
                snapshot.transport_entries,
                snapshot.consumed_body_bytes,
                snapshot.pending_receipts,
            ) == (0, 0, 0)
            assert receipt_quantities(run, lease) == ((1, 6, 0), (0, 0, 0))
        snapshot = run.snapshot()
        assert (
            snapshot.active,
            snapshot.reserved_body_bytes,
            snapshot.observation_complete,
            snapshot.failure,
        ) == (0, 0, True, "accounting")


@pytest.mark.parametrize("boundary,success_state", [("create", "created"), ("register", "open")])
@pytest.mark.parametrize("read_only", [False, True])
def test_storage_boundaries_close_real_connections(
    tmp_path, monkeypatch, boundary, success_state, read_only
):
    grant = RequestGrant("GET", "http://127.0.0.1:9/owned", ())
    authorization = Authorization((grant,), Limits(), "a" * 64)
    original_connect, connections, run = sqlite3.connect, [], None
    if boundary == "register":
        run = ControlledRun.create(authorization, tmp_path)
        run.open()

    def connect(*args, **kwargs):
        connection = original_connect(*args, **kwargs)
        connections.append(connection)
        if read_only:
            connection.execute("PRAGMA query_only=ON")
        return connection

    def invoke_boundary():
        if boundary == "create":
            return ControlledRun.create(authorization, tmp_path)
        run.authorize_participant("worker")
        return run

    try:
        with monkeypatch.context() as patch:
            patch.setattr("nci_si_acceptance.request_budget.sqlite3.connect", connect)
            if read_only:
                assert refusal(invoke_boundary) == (
                    RequestPolicyError,
                    "Controlled upstream request refused.",
                    None,
                )
            else:
                run = invoke_boundary()
        if run is not None:
            assert run.snapshot().state == ("failed" if read_only else success_state)
        with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
            connections[0].execute("SELECT 1")
    finally:
        for connection in connections:
            connection.close()
        if run is not None:
            run.close()


@pytest.mark.parametrize(
    "participant", [None, True, "", "bad name", "x" * 65, "controller", pytest.param([], id="list")]
)
@pytest.mark.parametrize("operation", ["attach", "register"])
def test_participant_boundary_refuses_unsupported_identity(tmp_path, participant, operation):
    with retirement_run(tmp_path) as (run, _):
        action = (
            partial(
                ControlledRun.attach, run.descriptor, run.authorization, participant=participant
            )
            if operation == "attach"
            else partial(run.authorize_participant, participant)
        )

        def denied():
            try:
                action()
            except RequestPolicyError as error:
                assert error.reason == "authorization"
                raise

        assert refusal(denied) == (RequestPolicyError, "Controlled upstream request refused.", None)
        with closing(sqlite3.connect(run.directory / "ledger.sqlite")) as storage:
            assert storage.execute("SELECT name FROM workers").fetchall() == [("controller",)]
        expected = (
            ("failed", "authorization")
            if operation == "register" and participant == "controller"
            else ("open", None)
        )
        snapshot = run.snapshot()
        assert (snapshot.state, snapshot.failure) == expected
        if expected[0] == "open":
            run.check_wait(0)


def test_create_refuses_unavailable_parent_storage(tmp_path):
    with retirement_run(tmp_path) as (run, _):
        blocked = tmp_path / "not-a-directory"
        blocked.write_text("owned synthetic obstruction")
        assert refusal(partial(ControlledRun.create, run.authorization, blocked)) == (
            RequestPolicyError,
            "Controlled upstream request refused.",
            None,
        )
        assert blocked.read_text() == "owned synthetic obstruction"


@pytest.mark.parametrize(
    "fault", ["empty-key", "short-key", "long-key", "infinite-origin", "overflow-deadline"]
)
def test_persisted_identity_boundary_refuses_corruption(tmp_path, fault):
    with retirement_run(
        tmp_path, seconds=1e308 if fault == "overflow-deadline" else DEFAULT_SECONDS
    ) as (run, _):
        key = run.directory / "key"
        original, started = key.read_bytes(), run.started
        altered = {"empty-key": b"", "short-key": b"x" * 31, "long-key": b"x" * 33}
        value = {"infinite-origin": float("inf"), "overflow-deadline": 1e308}.get(fault, started)
        bad_key = altered.get(fault, original)
        identity = json.dumps([run.descriptor.run_id, value, 0], separators=(",", ":")).encode()
        binding = hmac.digest(bad_key, run.authorization.binding(bad_key) + identity, "sha256")
        with closing(sqlite3.connect(run.directory / "ledger.sqlite")) as storage:
            original_binding = storage.execute("SELECT binding FROM run").fetchone()[0]
        try:
            key.write_bytes(bad_key)
            # Bind the altered state so a later HMAC check cannot hide the owning shape guard.
            with closing(sqlite3.connect(run.directory / "ledger.sqlite")) as storage, storage:
                storage.execute("UPDATE run SET started=?,binding=?", (value, binding))
            assert refusal(run.snapshot) == (
                RequestPolicyError,
                "Controlled upstream request refused.",
                None,
            )
        finally:
            # Preserve the actual fault bytes before restoring this fixture for cleanup.
            (tmp_path / "faulted-identity-ledger.sqlite").write_bytes(
                (run.directory / "ledger.sqlite").read_bytes()
            )
            faulted_key = tmp_path / "faulted-identity-key"
            faulted_key.write_bytes(key.read_bytes())
            faulted_key.chmod(0o600)
            key.write_bytes(original)
            run.started = started
            with closing(sqlite3.connect(run.directory / "ledger.sqlite")) as storage, storage:
                storage.execute("UPDATE run SET started=?,binding=?", (started, original_binding))


@pytest.mark.parametrize("operation", ["attempt", "read"])
@pytest.mark.parametrize("maximum", [None, True, 0, -1, 1.5, MAX_COUNTER + 1])
def test_invalid_lease_limits_refuse_before_reader(tmp_path, operation, maximum):
    with (
        retirement_run(tmp_path) as (run, grant),
        run.logical(grant) as logical,
        ExitStack() as leases,
    ):
        lease = (
            leases.enter_context(logical.attempt(response_limit=5)) if operation == "read" else None
        )
        reads = []
        if lease is not None:
            lease.entered()

        def reader(count):
            reads.append(count)
            return b"known"[:count]

        action = (
            partial(lease.read, reader, maximum)
            if lease is not None
            else lambda: leases.enter_context(logical.attempt(response_limit=maximum))
        )
        with pytest.raises(RequestPolicyError) as refused:
            action()
        expected = (
            "response_limit"
            if operation == "read" and maximum == MAX_COUNTER + 1
            else "authorization"
        )
        assert (refused.value.reason, str(refused.value)) == (
            expected,
            "Controlled upstream request refused.",
        )
        assert reads == []
        assert run.snapshot().consumed_body_bytes == 0


@pytest.mark.parametrize("operation,pending", [("entry", 0), ("pending-read", 6)])
def test_duplicate_entry_and_pending_read_refuse_without_consumption(tmp_path, operation, pending):
    with (
        retirement_run(tmp_path) as (run, grant),
        run.logical(grant) as logical,
        logical.attempt(response_limit=5) as lease,
    ):
        lease.entered()
        reads = []

        def inner_reader(maximum):
            reads.append(maximum)
            return b"x"[:maximum]

        def reader(_maximum):
            return lease.read(inner_reader, 1)

        action = lease.entered if operation == "entry" else partial(lease.read, reader, 6)
        assert refusal(action) == (
            RequestPolicyError,
            "Controlled upstream request refused.",
            None,
        )
        assert reads == []
        assert receipt_quantities(run, lease) == ((1, 6, 0), (0, pending, 0))
        assert run.snapshot().transport_entries == 1


@pytest.mark.parametrize("payload", [None, "known", bytearray(b"known"), b"too-long"])
def test_reader_payload_shape_and_bound_refuse_without_receipt(tmp_path, payload):
    with retirement_run(tmp_path) as (run, grant):
        with run.logical(grant) as logical, logical.attempt(response_limit=5) as lease:
            lease.entered()
            assert refusal(lambda: lease.read(lambda _maximum: payload, 6)) == (
                RequestPolicyError,
                "Controlled upstream request refused.",
                None,
            )
            assert receipt_quantities(run, lease) == ((1, 6, 0), (0, 6, 0))
        assert receipt_quantities(run, lease) == ((1, 6, 0), (0, 6, 0))


def test_receipt_pending_mismatch_preserves_unknown_progress(tmp_path):
    with (
        retirement_run(tmp_path) as (run, grant),
        run.logical(grant) as logical,
        logical.attempt(response_limit=5) as lease,
    ):
        lease.entered()

        def reader(maximum):
            with closing(sqlite3.connect(run.directory / "ledger.sqlite")) as storage, storage:
                storage.execute("UPDATE leases SET pending=2 WHERE id=?", (lease.lease_id,))
            return b"known"[:maximum]

        assert refusal(partial(lease.read, reader, 6)) == (
            RequestPolicyError,
            "Controlled upstream request refused.",
            None,
        )
        assert receipt_quantities(run, lease) == ((1, 6, 0), (0, 2, 0))


@pytest.mark.parametrize("operation", ["register", "logical", "attempt", "entry", "read", "wait"])
@pytest.mark.parametrize("barrier", ["closed", "failed", "deadline"])
def test_terminal_admission_refuses_new_work(tmp_path, monkeypatch, operation, barrier):
    with retirement_run(tmp_path, per_logical=2) as (run, grant):
        with (
            run.logical(grant) as logical,
            logical.attempt(response_limit=5) as lease,
            ExitStack() as resources,
        ):
            if operation == "read":
                lease.entered()
            if barrier == "closed":
                run.close()
            elif barrier == "failed":
                with pytest.raises(RequestPolicyError) as initial:
                    run.check_wait(-1)
                assert initial.value.reason == "authorization"
            else:
                monkeypatch.setattr(
                    "nci_si_acceptance.request_budget.time",
                    SimpleNamespace(
                        monotonic=lambda: run.started + run.authorization.limits.seconds
                    ),
                )
            reads = []

            def reader(maximum):
                reads.append(maximum)
                return b"known"[:maximum]

            actions = {
                "register": partial(run.authorize_participant, "late"),
                "logical": lambda: resources.enter_context(run.logical(grant)),
                "attempt": lambda: resources.enter_context(logical.attempt(response_limit=5)),
                "entry": lease.entered,
                "read": partial(lease.read, reader, 6),
                "wait": partial(run.check_wait, 0),
            }
            with pytest.raises(RequestPolicyError) as refused:
                actions[operation]()
            assert (
                type(refused.value),
                refused.value.reason,
                str(refused.value),
                refused.value.__cause__,
            ) == (
                RequestPolicyError,
                "deadline" if barrier == "deadline" else "closed",
                "Controlled upstream request refused.",
                None,
            )
            assert reads == []
            assert receipt_quantities(run, lease) == ((1, 6, 0), (0, 0, 0))
            with closing(sqlite3.connect(run.directory / "ledger.sqlite")) as storage:
                assert storage.execute("SELECT name FROM workers").fetchall() == [("controller",)]
        assert receipt_quantities(run, lease) == ((0, 0, 0), (0, 0, 1))


def test_expired_created_run_refuses_open(tmp_path, monkeypatch):
    with retirement_run(tmp_path, opened=False) as (run, _):
        monkeypatch.setattr(
            "nci_si_acceptance.request_budget.time",
            SimpleNamespace(monotonic=lambda: run.started + run.authorization.limits.seconds),
        )
        with pytest.raises(RequestPolicyError) as refused:
            run.open()
        assert (refused.value.reason, str(refused.value)) == (
            "deadline",
            "Controlled upstream request refused.",
        )
        with closing(sqlite3.connect(run.directory / "ledger.sqlite")) as storage:
            assert storage.execute("SELECT state FROM run").fetchall() == [("created",)]


def test_failed_publication_keeps_the_participant_locally_refused(tmp_path, monkeypatch):
    with retirement_run(tmp_path) as (run, _):
        original_open = Path.open

        def unavailable(path, mode="r", *args, **kwargs):
            if path == run.directory / "failed" and mode == "x":
                raise OSError("synthetic-private-publication-detail")
            return original_open(path, mode, *args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(Path, "open", unavailable)
            with pytest.raises(RequestPolicyError) as initial:
                run.check_wait(-1)
        assert (initial.value.reason, str(initial.value), initial.value.__cause__) == (
            "accounting",
            "Controlled upstream request refused.",
            None,
        )
        assert "synthetic-private-publication-detail" not in "".join(
            traceback.format_exception(initial.value)
        )
        assert not (run.directory / "failed").exists()
        with pytest.raises(RequestPolicyError) as later:
            run.check_wait(0)
        assert later.value.reason == "closed"


def test_minimum_read_keeps_active_and_retired_receipts_truthful(tmp_path):
    with retirement_run(tmp_path) as (run, grant):
        returned = []

        def consume():
            with run.logical(grant) as logical, logical.attempt(response_limit=1) as lease:
                lease.entered()
                returned.append(lease.read(lambda maximum: b"x"[:maximum], 1))
                live = run.snapshot()
                assert (
                    live.active,
                    live.pending_receipts,
                    live.consumed_body_bytes,
                    live.reserved_body_bytes,
                    live.observation_complete,
                ) == (1, 0, 1, 2, False)
                with pytest.raises(FrozenInstanceError):
                    live.consumed_body_bytes = 0

        assert refusal(consume) is None
        assert returned == [b"x"]
        retired = run.snapshot()
        assert (
            retired.active,
            retired.pending_receipts,
            retired.consumed_body_bytes,
            retired.reserved_body_bytes,
            retired.observation_complete,
        ) == (0, 0, 1, 1, True)


@pytest.mark.parametrize("change", ["method", "url", "headers", "body", "credential"])
def test_unapproved_logical_grant_never_enters_the_context(tmp_path, change):
    with retirement_run(tmp_path) as (run, grant):
        changes = {
            "method": {"method": "POST"},
            "url": {"url": "http://127.0.0.1:9/unapproved"},
            "headers": {"headers": (("accept", "text/plain"),)},
            "body": {"body": b"changed"},
            "credential": {"credential_origin": "http://127.0.0.1:9"},
        }
        unapproved, entered = replace(grant, **changes[change]), []

        def reserve():
            with run.logical(unapproved):
                entered.append(True)

        assert refusal(reserve) == (
            RequestPolicyError,
            "Controlled upstream request refused.",
            None,
        )
        assert entered == []
        result = run.snapshot()
        assert (
            result.logical,
            result.attempts,
            result.transport_entries,
            result.state,
            result.failure,
        ) == (0, 0, 0, "failed", "authorization")


@pytest.mark.skipif(os.name != "posix", reason="Owner-only POSIX mode bits are a POSIX contract")
def test_created_ledger_files_remain_owner_private_under_permissive_umask(tmp_path):
    previous = os.umask(0)
    try:
        with retirement_run(tmp_path, opened=False) as (run, _):
            assert [
                path.stat().st_mode & 0o777
                for path in (run.directory, run.directory / "key", run.directory / "ledger.sqlite")
            ] == [0o700, 0o600, 0o600]
    finally:
        os.umask(previous)


@pytest.mark.parametrize("retired", [None, 2])
def test_release_refuses_an_invalid_retirement_record(tmp_path, retired):
    with retirement_run(tmp_path) as (run, grant):
        lease = retired_lease(run, grant)
        with closing(sqlite3.connect(run.directory / "ledger.sqlite")) as storage, storage:
            storage.execute("UPDATE leases SET retired=? WHERE id=?", (retired, lease.lease_id))
        with pytest.raises(RequestPolicyError) as rejected:
            lease.release()
        assert (rejected.value.reason, str(rejected.value), rejected.value.__cause__) == (
            "accounting",
            "Controlled upstream request refused.",
            None,
        )
        assert receipt_quantities(run, lease) == ((0, 5, 5), (5, 0, retired))


def test_failed_creator_cannot_reopen_when_the_clock_also_fails(tmp_path, monkeypatch):
    with retirement_run(tmp_path, opened=False) as (run, _):
        with pytest.raises(RequestPolicyError) as initial:
            run.check_wait(-1)
        assert initial.value.reason == "authorization"
        with monkeypatch.context() as patch:
            patch.setattr(
                "nci_si_acceptance.request_budget.time", SimpleNamespace(monotonic=lambda: None)
            )
            with pytest.raises(RequestPolicyError) as rejected:
                run.open()
        assert (rejected.value.reason, str(rejected.value), rejected.value.__cause__) == (
            "closed",
            "Controlled upstream request refused.",
            None,
        )
        with closing(sqlite3.connect(run.directory / "ledger.sqlite")) as storage:
            assert storage.execute("SELECT state FROM run").fetchall() == [("created",)]


def test_worker_open_refuses_as_closed_before_reading_a_broken_marker(tmp_path):
    with retirement_run(tmp_path) as (run, _):
        run.authorize_participant("worker")
        worker = ControlledRun.attach(run.descriptor, run.authorization, participant="worker")
        marker = run.directory / "failed"
        marker.mkdir()
        try:
            with pytest.raises(RequestPolicyError) as rejected:
                worker.open()
            assert (rejected.value.reason, str(rejected.value), rejected.value.__cause__) == (
                "closed",
                "Controlled upstream request refused.",
                None,
            )
            with closing(sqlite3.connect(run.directory / "ledger.sqlite")) as storage:
                assert storage.execute("SELECT state FROM run").fetchall() == [("open",)]
            assert marker.is_dir()
        finally:
            marker.rmdir()


@pytest.mark.parametrize(
    "maximum",
    [True, 0, -1, float("nan"), float("inf"), "1", 10**400],
    ids=["boolean", "zero", "negative", "nan", "infinite", "string", "overflow"],
)
def test_transport_timeout_refuses_invalid_maximum(tmp_path, maximum):
    with retirement_run(tmp_path) as (run, _grant):
        assert hasattr(run, "timeout"), "Transport timeout must use guarded ledger time."
        with pytest.raises(RequestPolicyError) as refused:
            run.timeout(maximum)
        assert refused.value.reason == "authorization"
        assert run.snapshot().failure == "authorization"


def test_transport_timeout_uses_verified_run_origin(tmp_path, monkeypatch):
    # An exact origin keeps this deadline assertion independent of float rounding.
    monkeypatch.setattr(request_budget_module, "_clock", lambda: 1000.0)
    with retirement_run(tmp_path) as (run, _grant):
        assert hasattr(run, "timeout"), "Transport timeout must use guarded ledger time."
        origin = run.started
        monkeypatch.setattr(request_budget_module, "_clock", lambda: origin + 2)
        expected_remaining = 1798.0
        assert run.timeout(2000) == expected_remaining
        assert run.timeout(1) == 1.0
        run.close()
        with pytest.raises(RequestPolicyError) as refused:
            run.timeout(1)
        assert refused.value.reason == "closed"


@pytest.mark.parametrize("operation", ["timeout", "refuse"])
def test_transport_seams_validate_identity(tmp_path, operation):
    with retirement_run(tmp_path) as (run, _grant):
        assert hasattr(run, operation), (
            "Transport gates must validate the existing ledger identity."
        )
        original = run.descriptor
        run.descriptor = replace(original, run_id="b" * 32)
        try:
            with pytest.raises(RequestPolicyError) as refused:
                getattr(run, operation)(1 if operation == "timeout" else "transport")
            assert refused.value.reason == "authorization"
        finally:
            run.descriptor = original


def test_transport_refusal_keeps_first_reason_and_validates_reason(tmp_path):
    with retirement_run(tmp_path) as (run, _grant):
        assert hasattr(run, "refuse"), "Transport refusal must retain the first shared failure."
        with pytest.raises(RequestPolicyError) as first:
            run.refuse("not-a-policy-reason")
        assert first.value.reason == "authorization"
        with pytest.raises(RequestPolicyError) as later:
            run.refuse("transport")
        assert later.value.reason == "authorization"
        assert run.snapshot().failure == "authorization"


class InvalidEntryOrder:
    """A misbehaving cooperating callback must not bypass the lease boundary."""

    refused_before_entry = False

    def __init__(self, mode):
        self.mode = mode

    def invoke(self, *, admit, operation):
        if self.mode == "before-admission":
            return operation()
        admit()
        if self.mode == "duplicate-admission":
            admit()
        if self.mode == "omitted-operation":
            return None
        operation()
        return operation()


@pytest.mark.parametrize(
    "mode,effects,entries,active,reserved,pending",
    [
        ("before-admission", 0, 0, 0, 0, 0),
        ("repeated-operation", 1, 1, 0, 0, 0),
        ("omitted-operation", 0, 0, 1, 6, 1),
        ("duplicate-admission", 0, 0, 1, 6, 1),
    ],
)
def test_transport_callback_order_fails_closed(
    tmp_path, mode, effects, entries, active, reserved, pending
):
    with retirement_run(tmp_path) as (run, grant):
        operations = []
        with run.logical(grant) as logical, logical.attempt(response_limit=5) as lease:
            with pytest.raises(RequestPolicyError) as refused:
                lease.transport(InvalidEntryOrder(mode), lambda: operations.append("entry"))
            assert (refused.value.reason, str(refused.value), refused.value.__cause__) == (
                "accounting",
                "Controlled upstream request refused.",
                None,
            )
        observed = run.snapshot()
        assert (
            operations,
            observed.transport_entries,
            observed.active,
            observed.reserved_body_bytes,
            observed.pending_receipts,
        ) == (["entry"] * effects, entries, active, reserved, pending)
        assert (observed.state, observed.failure, observed.observation_complete) == (
            "failed",
            "accounting",
            not pending,
        )
        with pytest.raises(RequestPolicyError) as closed:
            run.check_wait(0)
        assert closed.value.reason == "closed"


@pytest.mark.parametrize(
    "action,entered,entry_pending,pending",
    [
        ("release", 0, None, 0),
        ("release", 0, 2, 0),
        ("_prepare_entry", 1, 0, 0),
        ("_prepare_entry", 0, 1, 0),
        ("_prepare_entry", 0, 0, 1),
        ("entered", 0, 1, 0),
        ("_abort_entry", 0, 0, 0),
        ("_abort_entry", 1, 1, 0),
        ("_receipt_entry", 0, 0, 0),
        ("_receipt_entry", 1, 1, 0),
    ],
)
def test_entry_transition_refuses_inconsistent_persisted_state(
    tmp_path, action, entered, entry_pending, pending
):
    with (
        retirement_run(tmp_path) as (run, grant),
        run.logical(grant) as logical,
        logical.attempt(response_limit=5) as lease,
        closing(sqlite3.connect(run.directory / "ledger.sqlite")) as storage,
    ):
        with storage:
            storage.execute(
                "UPDATE leases SET entered=?,entry_pending=?,pending=? WHERE id=?",
                (entered, entry_pending, pending, lease.lease_id),
            )
        try:
            with pytest.raises(RequestPolicyError) as refused:
                getattr(lease, action)()
            assert (refused.value.reason, str(refused.value), refused.value.__cause__) == (
                "accounting",
                "Controlled upstream request refused.",
                None,
            )
            assert storage.execute(
                "SELECT entered,entry_pending,pending,retired FROM leases WHERE id=?",
                (lease.lease_id,),
            ).fetchone() == (entered, entry_pending, pending, 0)
            assert storage.execute(
                "SELECT entered,active,reserved,consumed FROM run"
            ).fetchone() == (0, 1, 6, 0)
            with pytest.raises(RequestPolicyError) as closed:
                run.check_wait(0)
            assert closed.value.reason == "closed"
        finally:
            # Repair only the deliberately corrupted fixture so ordinary release runs.
            with storage:
                storage.execute(
                    "UPDATE leases SET entered=0,entry_pending=0,pending=0 WHERE id=?",
                    (lease.lease_id,),
                )


def test_old_entry_schema_refuses_attachment_without_migration(tmp_path):
    with retirement_run(tmp_path) as (run, _grant):
        run.authorize_participant("worker")
        with closing(sqlite3.connect(run.directory / "ledger.sqlite")) as storage:
            with storage:
                storage.execute("ALTER TABLE leases DROP COLUMN entry_pending")
            try:
                with pytest.raises(RequestPolicyError) as refused:
                    ControlledRun.attach(run.descriptor, run.authorization, participant="worker")
                assert (refused.value.reason, str(refused.value), refused.value.__cause__) == (
                    "accounting",
                    "Controlled upstream request refused.",
                    None,
                )
                assert tuple(row[1] for row in storage.execute("PRAGMA table_info(leases)")) == (
                    "id",
                    "request",
                    "participant",
                    "allowance",
                    "consumed",
                    "pending",
                    "entered",
                    "retired",
                )
                assert storage.execute(
                    "SELECT entered,active,reserved,logical,attempts FROM run"
                ).fetchone() == (0, 0, 0, 0, 0)
            finally:
                # Restore only the owned old-schema fixture for normal close/cleanup.
                with storage:
                    storage.execute("ALTER TABLE leases ADD COLUMN entry_pending INTEGER")
        with pytest.raises(RequestPolicyError) as closed:
            run.check_wait(0)
        assert closed.value.reason == "closed"


def test_pending_entry_receipt_refuses_body_read(tmp_path):
    with (
        retirement_run(tmp_path) as (run, grant),
        run.logical(grant) as logical,
        logical.attempt(response_limit=5) as lease,
        closing(sqlite3.connect(run.directory / "ledger.sqlite")) as storage,
    ):
        lease.entered()
        with storage:
            storage.execute("UPDATE leases SET entry_pending=1 WHERE id=?", (lease.lease_id,))
        reads = []

        def reader(maximum):
            reads.append(maximum)
            return b"x"[:maximum]

        try:
            with pytest.raises(RequestPolicyError) as refused:
                lease.read(reader, 1)
            assert (refused.value.reason, str(refused.value), refused.value.__cause__) == (
                "accounting",
                "Controlled upstream request refused.",
                None,
            )
            assert reads == []
            assert storage.execute(
                "SELECT entered,entry_pending,pending,consumed FROM leases WHERE id=?",
                (lease.lease_id,),
            ).fetchone() == (1, 1, 0, 0)
            observed = run.snapshot()
            assert (
                observed.transport_entries,
                observed.consumed_body_bytes,
                observed.active,
                observed.reserved_body_bytes,
                observed.pending_receipts,
                observed.observation_complete,
                observed.failure,
            ) == (1, 0, 1, 6, 1, False, "accounting")
            with pytest.raises(RequestPolicyError) as closed:
                run.check_wait(0)
            assert closed.value.reason == "closed"
        finally:
            # Only the deliberately injected inconsistency is repaired for ordinary release.
            with storage:
                storage.execute("UPDATE leases SET entry_pending=0 WHERE id=?", (lease.lease_id,))


def test_wrong_logical_record_has_sanitized_refusal(tmp_path):
    with retirement_run(tmp_path) as (run, _grant):
        entered = []

        def reserve():
            with run.logical(object()):
                entered.append(True)

        assert refusal(reserve) == (
            RequestPolicyError,
            "Controlled upstream request refused.",
            None,
        )
        assert entered == []
        result = run.snapshot()
        assert (result.logical, result.attempts, result.transport_entries) == (0, 0, 0)
        assert (result.state, result.failure) == ("failed", "authorization")
