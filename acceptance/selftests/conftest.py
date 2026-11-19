"""What the self-tests that run the suite against compliant_server.py share: a copy of the
suite and its fixture set, and the outcome of each test of a run. And the sharding by which CI
runs the self-tests in several jobs at once."""

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from nci_sip_acceptance.craft import Recorded, craft, release_session
from nci_sip_acceptance.record import FIXTURES

# Independent literal expected origins shared by policy and native-report owners.
REVIEWED_ORIGINS = {
    "evs": "https://api-evsrest.nci.nih.gov",
    "evs-fhir": "https://api-evsrest.nci.nih.gov/fhir/r4",
    "cadsr": None,
    "cadsr-ftp": None,
    "cadsr-contracts": None,
    "ssis": None,
    "ssis-sparql": None,
}

# "i/n": run the i-th of n shards of the self-tests (CI's selftest jobs).
SHARD_VARIABLE = "SELFTEST_SHARD"


def shard(nodeids: list[str], spec: str) -> set[str]:
    """The node ids of shard `spec`, "i/n": every n-th in order from the i-th, so that the n
    shards share the tests out evenly, a parametrized test's cases over all of them, and
    together hold each test once."""

    index, count = (int(part) for part in spec.split("/"))
    if not 1 <= index <= count:
        raise ValueError(f"{SHARD_VARIABLE}={spec}: the shard is one of 1 to {count}")
    return set(sorted(nodeids)[index - 1 :: count])


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    spec = os.environ.get(SHARD_VARIABLE)
    if not spec:
        return
    kept = shard([item.nodeid for item in items], spec)
    config.hook.pytest_deselected(items=[item for item in items if item.nodeid not in kept])
    items[:] = [item for item in items if item.nodeid in kept]


# A copy of the suite's conftest runs outside a suite tree, so a plugin written into the copy's
# directory stands in for the identity of the tree (test_suite_identity.py tests the real one).
STAND_IN = "stand_in"
STAND_IN_SOURCE = (
    "from nci_sip_acceptance import report\n"
    "report.suite_state = lambda config: {'version': '0', 'fixture_set': 'x', 'digest': 'ab'}\n"
)


def stand_in(pytester: pytest.Pytester) -> None:
    (pytester.path / f"{STAND_IN}.py").write_text(STAND_IN_SOURCE, encoding="utf-8")


def synthetic_live(pytester: pytest.Pytester) -> None:
    """Opt a copied-suite component simulation into local-only execution, not policy tests."""
    conftest = pytester.path / "tests/conftest.py"
    conftest.write_text(f"{conftest.read_text()}\n{SYNTHETIC_LIVE}", encoding="utf-8")


SYNTHETIC_LIVE = """import os as _synthetic_os
from nci_sip_acceptance import client as _synthetic_client
from nci_sip_acceptance.denied_surfaces import DeniedSurfaces as _SyntheticRefusal

_synthetic_configure = pytest_configure

def pytest_configure(config):
    if _synthetic_os.environ.get("NCI_SIP_ACCEPTANCE_MODE") != "live":
        return _synthetic_configure(config)
    denied = _SyntheticRefusal()
    endpoint = denied.__enter__()
    config.add_cleanup(lambda: denied.__exit__(None, None, None))
    patch = pytest.MonkeyPatch()
    config.add_cleanup(patch.undo)
    original = server_environment
    def offline_environment(mode, data, fixture_url, **kwargs):
        return original("fixture", data, endpoint, **kwargs)
    patch.setitem(globals(), "server_environment", offline_environment)
    patch.setattr(_synthetic_client, "selection", lambda mode: None)
    return _synthetic_configure(config)
"""


SUITE = Path(__file__).parent.parent / "tests"
COMPLIANT_SERVER = Path(__file__).parent / "compliant_server.py"
# Any parameters: the compliant server sends a call's free text as parameters (A7.7).
VERSION = {
    "surface": "evs",
    "method": "GET",
    "path": "/api/v1/version",
    "ignored": {"*": "the compliant server's free text"},
}
LICENCE_KEY = "selftest-licence-key"
CURRENT = {"status": 200, "body": {"version": "26.09d"}}
# MedDRA's licence text in the listing, and the text given with the content under
# license/attributed: another, so that a server that joins the listing's text shows there too,
# and longer than the cut the alters-attribution defect makes.
LICENCE_TEXT = "MedDRA is licensed for NCI work; any other use needs a subscription."
GIVEN_TEXT = "MedDRA content, licensed for NCI work only, as given with this concept."
# The answers to the compliant server's requests: EVS's version, which names the release it
# serves, in each scenario the suite uses; and the licensed concept, only with the key. The
# terminology listing is read by the suite itself (X-17), never asked.
ANSWERS = {
    "recorded/evs/terminologies.json": {
        "request": VERSION | {"path": "/api/v1/metadata/terminologies", "ignored": {}},
        "response": {
            "status": 200,
            "body": [
                {"terminology": "ncit", "version": "26.09d"},
                {"terminology": "ncit", "version": "26.08e"},
                {
                    "terminology": "mdr",
                    "version": "29_0",
                    "metadata": {"licenseText": LICENCE_TEXT},
                },
            ],
        },
    },
    "crafted/version.json": {"response": CURRENT},
    "scenarios/retired/with-replacement/version.json": {"response": CURRENT},
    "scenarios/traversal/deep-fanout/version.json": {"response": CURRENT},
    "scenarios/release/unknown/version.json": {"response": {"status": 404, "body": {}}},
    "scenarios/release/mismatch/version.json": {
        "response": {"status": 200, "body": {"version": "26.08e"}}
    },
    "scenarios/upstream/unavailable/version.json": {"response": {"status": 503, "body": {}}},
    "scenarios/upstream/rate-limited/version.json": {
        "responses": [{"status": 429, "headers": {"Retry-After": "1"}, "body": {}}, CURRENT]
    },
    "scenarios/license/restricted/granted.json": {
        "request": VERSION
        | {
            "path": "/api/v1/concept/mdr_29_0/10000000",
            "headers": {"X-EVSRESTAPI-License-Key": LICENCE_KEY},
        },
        "response": {"status": 200, "body": {}},
    },
    "scenarios/license/restricted/search.json": {
        "request": VERSION
        | {
            "path": "/api/v1/concept/mdr_29_0/search",
            "headers": {"X-EVSRESTAPI-License-Key": LICENCE_KEY},
        },
        "response": {"status": 200, "body": {}},
    },
    "scenarios/license/attributed/granted.json": {
        "request": VERSION
        | {
            "path": "/api/v1/concept/mdr_29_0/10000000",
            "headers": {"X-EVSRESTAPI-License-Key": LICENCE_KEY},
        },
        "response": {"status": 200, "body": {"licenseText": GIVEN_TEXT}},
    },
    "scenarios/license/attributed/search.json": {
        "request": VERSION
        | {
            "path": "/api/v1/concept/mdr_29_0/search",
            "headers": {"X-EVSRESTAPI-License-Key": LICENCE_KEY},
        },
        "response": {"status": 200, "body": {"concepts": [{"licenseText": GIVEN_TEXT}]}},
    },
    "scenarios/license/restricted/refused.json": {
        "request": VERSION | {"path": "/api/v1/concept/mdr_29_0/10000000"},
        "response": {"status": 403, "body": {}},
    },
}
ANSWERS.update(release_session(Recorded(SUITE.parent / "fixtures")))


def _fixture(path: Path, answer: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {"kind": "crafted", "requirement": "self-test", "request": VERSION}
    path.write_text(json.dumps(document | answer), encoding="utf-8")


@pytest.fixture
def compliant(pytester, monkeypatch):
    """The suite's tests against the compliant server, with fixtures for its requests."""

    fixtures = pytester.mkdir("fixtures")
    # The prepare step indexes nothing here: no tool of the stub searches an index.
    manifest = "evs:\n  release: ncit_26.09d\nrecord:\n  concepts: {}\n"
    (fixtures / "manifest.yaml").write_text(manifest, encoding="utf-8")
    for path, answer in ANSWERS.items():
        _fixture(fixtures / path, answer)
    for scenario in ("restricted", "attributed"):
        settings = fixtures / "scenarios" / "license" / scenario / "settings.json"
        settings.write_text(json.dumps({"NCI_SIP_EVS_LICENSE_KEY": LICENCE_KEY}), encoding="utf-8")
    stand_in(pytester)
    tests = pytester.mkdir("tests")
    for name in (
        "conftest.py",
        "test_protocol.py",
        "test_crosscutting.py",
        "test_release_session.py",
        "calls.yaml",
    ):
        (tests / name).write_text((SUITE / name).read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setenv("NCI_SIP_ACCEPTANCE_MODE", "fixture")
    monkeypatch.setenv("NCI_SIP_ACCEPTANCE_SERVER", f"{sys.executable} {COMPLIANT_SERVER}")
    monkeypatch.setenv("NCI_SIP_ACCEPTANCE_PROFILE", "evs")
    monkeypatch.setenv("NCI_SIP_ACCEPTANCE_PREPARE", "true")
    monkeypatch.delenv("COMPLIANT_SERVER_DEFECT", raising=False)
    return pytester


@pytest.fixture
def outcomes(compliant):
    """Run the suite's tests in a file, or those `-k` selects, and return each outcome by name."""

    def run(*arguments):
        report = compliant.path / "report.json"
        compliant.runpytest_subprocess(
            *arguments, "-p", "no:cacheprovider", "-p", STAND_IN, f"--report={report}"
        )
        tests = json.loads(report.read_text(encoding="utf-8"))["tests"]
        return {nodeid.partition("::")[2]: test["outcome"] for nodeid, test in tests.items()}

    return run


# The crafted layer of the real fixture set, built from the files on disk.
CRAFTED = craft(FIXTURES)


def crafted_digest(documents):
    """The digest the manifest records for crafted documents: SHA-256 of their canonical JSON,
    by name. Stated here independently of craft.py."""

    canonical = json.dumps(documents, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def without_crafted(root):
    """A copy of the fixture set at `root` with every file the generator makes left out, and the
    manifest's crafted line written."""

    for path in sorted(FIXTURES.rglob("*")):
        name = path.relative_to(FIXTURES).as_posix()
        if path.is_file() and name not in CRAFTED:
            (root / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(path, root / name)
    lines = (root / "manifest.yaml").read_text(encoding="utf-8").splitlines()
    kept = [line for line in lines if not line.startswith("crafted:")]
    text = "\n".join([*kept, f"crafted: {crafted_digest(CRAFTED)}", ""])
    (root / "manifest.yaml").write_text(text, encoding="utf-8")
    return root


def furnished_child(command, environment):
    return subprocess.run(  # noqa: S603 - callers furnish isolated argv and local-only bases.
        command, env=environment, capture_output=True, text=True, timeout=30, check=False
    )
