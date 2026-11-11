"""The suite's fixture set loads and is what the manifest records."""

import json
import re
import shutil
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pytest
import yaml

from conftest import CRAFTED, crafted_digest, without_crafted
from nci_si_acceptance.fixture_server import FixtureServer, load_fixtures
from nci_si_acceptance.record import FIXTURES, plan, request_of, stale

pytest_plugins = ["pytester"]

MANIFEST = yaml.safe_load((FIXTURES / "manifest.yaml").read_text(encoding="utf-8"))
DOCUMENTS = {
    path.relative_to(FIXTURES).as_posix(): json.loads(path.read_text(encoding="utf-8"))
    for path in sorted(FIXTURES.rglob("*.json"))
    if path.name != "settings.json"
}


def test_the_fixture_set_loads():
    fixtures = load_fixtures(FIXTURES)

    assert fixtures.ordinary
    assert fixtures.recordings[None]


def test_the_recorded_set_is_exactly_what_the_manifest_records():
    planned = plan(MANIFEST)

    assert [each.file for each in planned if each.file not in DOCUMENTS] == []
    assert stale(FIXTURES, MANIFEST) == []
    # Each recording holds the request its entry makes, headers and body included, and the
    # status its entry expects.
    assert [
        each.file
        for each in planned
        if (DOCUMENTS[each.file]["request"], DOCUMENTS[each.file]["response"]["status"])
        != (request_of(each), each.status)
    ] == []


def test_the_manifest_records_the_digest_of_what_craft_py_makes_now():
    # A change to the generated scenarios shows as this one line (pdm run acceptance-craft).
    assert MANIFEST.get("crafted") == crafted_digest(CRAFTED)


def test_every_scenario_file_on_disk_is_recorded():
    recorded = {each.file for each in plan(MANIFEST)}
    scenario_files = {
        path.relative_to(FIXTURES).as_posix() for path in (FIXTURES / "scenarios").rglob("*.json")
    }

    assert sorted(scenario_files - recorded) == []


def test_the_crafted_scenarios_are_served_with_no_crafted_file_on_disk(tmp_path):
    fixtures = load_fixtures(without_crafted(tmp_path))

    # Concept recordings, which is all traversal/starvation holds, load apart from exact fixtures.
    assert "traversal/starvation" in fixtures.recordings
    assert "release/mismatch" in fixtures.scenarios
    with FixtureServer(fixtures) as running:
        # A synthetic concept recording and a recorded answer with its release changed.
        for scenario, name in (
            ("traversal/starvation", "scenarios/traversal/starvation/concepts/C99200000.json"),
            ("release/mismatch", "scenarios/release/mismatch/OP-E06-subset-gdc.json"),
        ):
            running.activate(scenario)
            document = CRAFTED[name]
            assert _answer(running, document["request"]) == document["response"], name


@pytest.mark.parametrize(
    "name", ["scenarios/release/two-latest/latest.json", "crafted/OP-C03/search-over-cap.json"]
)
def test_a_crafted_name_that_also_lies_on_disk_is_refused(tmp_path, name):
    root = without_crafted(tmp_path)
    (root / name).parent.mkdir(parents=True, exist_ok=True)
    (root / name).write_text(json.dumps(CRAFTED[name]), encoding="utf-8")

    with pytest.raises(ValueError, match=re.escape(name)):
        load_fixtures(root)


def _answer(running, request):
    """The status and body the fixture server gives for a fixture's request."""

    query = urlencode(request.get("params", {}), doseq=True)
    url = running.base_url(request["surface"]) + request["path"] + (f"?{query}" if query else "")
    try:
        with urlopen(Request(url, headers=request.get("headers", {})), timeout=10) as response:  # noqa: S310
            return {"status": response.status, "body": json.loads(response.read() or "null")}
    except HTTPError as error:
        with error:
            return {"status": error.code, "body": json.loads(error.read() or "null")}


def test_every_recorded_fixture_is_dated_and_pinned():
    recorded = [doc for name, doc in DOCUMENTS.items() if name.startswith("recorded/")]
    release = MANIFEST["evs"]["release"]

    assert {doc["kind"] for doc in recorded} == {"recorded"}
    assert all(doc["recorded_on"] for doc in recorded)
    concepts = [
        doc["response"]["body"]
        for name, doc in DOCUMENTS.items()
        if name.startswith("recorded/") and "/concepts/" in name
    ]
    assert {f"ncit_{body['version']}" for body in concepts if "version" in body} == {release}


def test_every_derived_fixture_names_its_requirement_and_answers_as_its_recording():
    for entry in MANIFEST["record"].get("derived", []):
        document = DOCUMENTS[entry["fixture"]]
        assert (document["kind"], document["requirement"]) == ("crafted", entry["requirement"])
        assert document["response"] == DOCUMENTS[entry["from"]]["response"]


def test_the_fixture_directory_is_the_packages_own():
    assert Path(__file__).parents[1] / "fixtures" == FIXTURES


def _posted(url, entry):
    """The status of a query request made as a server might: its text on one line, in a form
    or as the body."""

    if "form" in entry:
        form = {name: " ".join(text.split()) for name, text in entry["form"].items()}
        headers = entry["headers"] | {"Content-Type": "application/x-www-form-urlencoded"}
        data = urlencode(form).encode()
    else:
        headers, data = entry["headers"], " ".join(entry["body"].split()).encode()
    request = Request(url, data=data, headers=headers, method="POST")  # noqa: S310
    try:
        with urlopen(request, timeout=10) as response:  # noqa: S310 - the local fixture server
            return response.status
    except HTTPError as error:
        with error:
            return error.code


def test_each_published_query_is_answered_by_its_recording_whatever_its_whitespace():
    # Asked in a form as prescribed, or posted directly, which the live endpoint refuses.
    forms = [entry for entry in MANIFEST["record"]["requests"] if entry["surface"] == "ssis-sparql"]
    with FixtureServer(load_fixtures(FIXTURES)) as running:
        statuses = [
            _posted(running.base_url(entry["surface"]) + entry["path"], entry) for entry in forms
        ]
        answered = [entry["fixture"] for entry in running.log()]

    assert {"form", "body"} <= {key for entry in forms for key in entry}
    assert statuses == [entry.get("status", 200) for entry in forms]
    assert answered == [entry["fixture"] for entry in forms]


FRESH_COPIES = """import pytest

NAME = "recorded/evs/terminologies.json"


@pytest.mark.tool("resolve_release")
def test_a_changes_what_it_read(recorded):
    recorded(NAME)["response"]["status"] = 0


@pytest.mark.tool("resolve_release")
def test_b_reads_it_unchanged(recorded):
    assert recorded(NAME)["response"]["status"] == 200
"""


def test_each_read_of_a_fixture_is_a_fresh_copy(compliant, outcomes):
    # The suite reads the fixture set once per session; a test changing what it read must not
    # change what the next test reads.
    (compliant.path / "tests" / "test_copies.py").write_text(FRESH_COPIES, encoding="utf-8")

    assert outcomes("tests/test_copies.py", "-p", "no:randomly") == {
        "test_a_changes_what_it_read": "passed",
        "test_b_reads_it_unchanged": "passed",
    }


CRAFTED_READ = """import json

import pytest

NAME = "scenarios/traversal/starvation/concepts/C99200000.json"


@pytest.mark.tool("get_concept")
def test_reads_a_crafted_fixture(recorded):
    expected = json.loads(open("expected.json", encoding="utf-8").read())
    assert recorded(NAME) == expected
"""


def test_the_suite_reads_crafted_fixtures_from_memory(compliant, outcomes):
    # The real fixture set, whose crafted fixtures are no files, read through the suite.
    name = "scenarios/traversal/starvation/concepts/C99200000.json"
    shutil.rmtree(compliant.path / "fixtures")
    shutil.copytree(FIXTURES, compliant.path / "fixtures")
    (compliant.path / "expected.json").write_text(json.dumps(CRAFTED[name]), encoding="utf-8")
    (compliant.path / "tests" / "test_crafted_read.py").write_text(CRAFTED_READ, encoding="utf-8")

    assert outcomes("tests/test_crafted_read.py") == {"test_reads_a_crafted_fixture": "passed"}
