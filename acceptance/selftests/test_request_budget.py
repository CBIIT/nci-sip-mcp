"""Shared controlled-run accounting at real process and HTTP caller boundaries."""

import importlib
import importlib.util
import json
import subprocess
import sys
import tempfile
from contextlib import nullcontext
from pathlib import Path

import pytest

import nci_si_acceptance
from conftest import FIXTURES
from nci_si_acceptance.fixture_server import FixtureServer, load_fixtures
from nci_si_mcp.http_client import RequestPolicyError

# This explicit consumer drives today's HttpClient if the new entry is absent. Absence
# therefore produces actual excess local contacts in the red revision, not an import error.
ACTOR = """import importlib, importlib.util, json, sys, tempfile
import nci_si_acceptance
from nci_si_mcp import http_client
from contextlib import contextmanager
from pathlib import Path
from urllib.request import ProxyHandler, build_opener
from nci_si_mcp.http_client import HttpClient, RequestPolicyError
from nci_si_mcp.request_policy import Exchange

http_client._OPENER = build_opener(ProxyHandler({}), http_client._SameOriginRedirects())
settings = json.loads(Path(sys.argv[1]).read_text())
sys.stdin.readline()  # The parent transfers ownership before releasing the child.
module = None
if importlib.util.find_spec("nci_si_acceptance.request_budget") is not None:
    module = importlib.import_module("nci_si_acceptance.request_budget")

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
        with self.logical.attempt() as lease:
            on_start()
            lease.entered()
            with build_opener(ProxyHandler({})).open(
                self.request_snapshot, timeout=timeout_seconds
            ) as response:
                body = lease.read(response.read, response_limit + 1)
                return Exchange(response.status, dict(response.headers), body)

run = None
if module is not None:
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
policy = ActorPolicy(run, grant) if run is not None else None
client = HttpClient(
    settings["base"], label="EVS", timeout_seconds=2, max_attempts=1,
    retry_backoff_seconds=0, max_response_bytes=1024, size_bound="local-test",
    request_policy=policy,
)
accepted, refused = 0, []
for _ in range(2):
    try:
        client.get_json("/api/v1/version")
        accepted += 1
    except RequestPolicyError as error:
        refused.append(error.reason)
print(json.dumps({
    "accepted": accepted, "refused": refused, "interpreter": sys.executable,
    "package": nci_si_acceptance.__file__, "tempdir": tempfile.gettempdir(),
}))
"""


EXPECTED_CONTACTS = 2
EXPECTED_REFUSALS = 2


def optional_budget():
    if importlib.util.find_spec("nci_si_acceptance.request_budget") is None:
        return None
    return importlib.import_module("nci_si_acceptance.request_budget")


def run_context(module, base, directory, limits):
    if module is None:
        return nullcontext(None)
    grant = module.RequestGrant("GET", base + "/api/v1/version", (("accept", "application/json"),))
    authorization = module.Authorization((grant,), module.Limits(**limits), "a" * 64)
    return module.ControlledRun.create(authorization, directory / "runs")


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
    if run is not None:
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
        if run is not None:
            run.register(participant, child, kind="worker")
    except BaseException:
        stop_actor(child)
        raise
    return child


def release_and_collect(children):
    # Release both registered children before waiting, so requests genuinely compete.
    for child in children:
        child.stdin.write(b"ready\n")
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
        settings = {"base": base, "url": base + "/api/v1/version", "limits": limits}
        try:
            with run_context(optional_budget(), base, tmp_path, limits) as run:
                for number in range(2):
                    children.append(launch_actor(actor, tmp_path, number, settings, run))
                results = release_and_collect(children)
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
            with run_context(optional_budget(), base, tmp_path, limits) as run:
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
