"""The concept rules answer every projection and batch the recordings cover, and nothing else."""

import json
import re
from http import HTTPStatus
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
import yaml

from nci_si_acceptance.concepts import ConceptRules, Recording, recording_key
from nci_si_acceptance.fixture_server import FixtureServer, load_fixtures

RULES = {
    "base": ["code", "name"],
    "default": "summary",
    "include": {
        "minimal": [],
        "summary": ["synonyms"],
        "parents": ["parents"],
        "roles": ["roles"],
        "full": ["synonyms", "parents", "roles"],
    },
    "relations": ["parents", "roles"],
}
C1 = {"code": "C1", "name": "One", "synonyms": [{"name": "Uno"}], "parents": [{"code": "C9"}]}


def concept(code, **fields):
    return {"code": code, "name": f"Concept {code}", **fields}


def write_manifest(root, rules=RULES):
    root.mkdir(parents=True, exist_ok=True)
    (root / "manifest.yaml").write_text(yaml.safe_dump({"evs": {"concepts": rules}}), "utf-8")


def recording(root, code, include="full", status=200, body=None, where="recorded/evs/concepts"):
    """Write a concept recording of `code`, recorded at `include`."""

    path = root / where / f"{code}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {
        "kind": "recorded",
        "recorded_on": "2026-10-02",
        "request": {
            "surface": "evs",
            "method": "GET",
            "path": f"/api/v1/concept/ncit_26.09d/{code}",
            "params": {"include": [include]},
        },
        "response": {"status": status, "body": concept(code) if body is None else body},
    }
    path.write_text(json.dumps(document), encoding="utf-8")


@pytest.fixture
def rules():
    return ConceptRules.from_manifest(RULES)


def finder(*recordings):
    table = {("ncit_26.09d", each.body["code"]): each for each in recordings}
    return lambda terminology, code: table.get((terminology, code))


def full(body):
    return Recording(
        f"{body['code']}.json",
        200,
        body,
        frozenset({"code", "name", "synonyms", "parents", "roles"}),
    )


def test_a_single_request_gets_the_base_and_the_keys_its_include_names(rules):
    find = finder(full(C1))

    def answer(query):
        params = {"include": [query]} if query else {}
        return rules.answer("/api/v1/concept/ncit_26.09d/C1", params, find).body

    assert answer("minimal") == {"code": "C1", "name": "One"}
    assert answer("parents,summary") == C1
    assert answer(None) == {"code": "C1", "name": "One", "synonyms": [{"name": "Uno"}]}
    assert answer("roles") == {"code": "C1", "name": "One"}


def test_a_batch_holds_each_code_once_never_in_the_order_requested(rules):
    find = finder(full(C1), full(concept("C2")), full(concept("C3")))

    answer = rules.answer(
        "/api/v1/concept/ncit_26.09d", {"list": ["C1,C2,C3,C1"], "include": ["minimal"]}, find
    )

    assert [each["code"] for each in answer.body] == ["C2", "C3", "C1"]
    assert answer.names == ["C1.json", "C2.json", "C3.json"]


@pytest.mark.parametrize(
    ("path", "params"),
    [
        ("/api/v1/concept/ncit_26.09d/C1", {"include": ["paths"]}),
        ("/api/v1/concept/ncit_26.09d/C1", {"include": ["minimal"], "limit": ["5"]}),
        ("/api/v1/concept/ncit_26.09d/C1", {"include": ["minimal", "summary"]}),
        ("/api/v1/concept/ncit_26.09d/C7", {"include": ["minimal"]}),
        ("/api/v1/concept/ncit_26.09d", {"include": ["minimal"]}),
        ("/api/v1/concept/ncit_26.09d", {"list": ["C1,C7"], "include": ["minimal"]}),
        ("/api/v1/concept/ncit_26.09d", {"list": ["C1,,C1"]}),
        ("/api/v1/concept/ncit_26.09d/C1/children", {}),
        ("/api/v1/concept/ncit_99.99z/C1", {"include": ["minimal"]}),
        ("/api/v1/version", {}),
        ("/api/v1/concept/ncit_26.09d", {"list": ["C1"], "limit": ["5"]}),
        ("/api/v1/concept/ncit_26.09d/C1", {"include": ["minimal,paths"]}),
        ("/api/v1/concept/ncit_26.09d", {"list": [",".join(["C1"] * 1001)]}),
    ],
)
def test_what_the_recordings_do_not_cover_goes_unanswered(rules, path, params):
    assert rules.answer(path, params, finder(full(C1))) is None


def test_a_batch_of_up_to_a_thousand_codes_is_answered_counting_duplicates(rules):
    answer = rules.answer(
        "/api/v1/concept/ncit_26.09d", {"list": [",".join(["C1"] * 1000)]}, finder(full(C1))
    )

    assert [each["code"] for each in answer.body] == ["C1"]


def test_the_found_concepts_are_rotated_whatever_the_request_left_out(rules):
    unknown = Recording("C404.json", 404, {"message": "C404 not found"}, frozenset())
    table = {("ncit_26.09d", "C1"): full(C1), ("ncit_26.09d", "C2"): full(concept("C2"))}
    table[("ncit_26.09d", "C404")] = unknown

    def find(terminology, code):
        return table.get((terminology, code))

    answer = rules.answer("/api/v1/concept/ncit_26.09d", {"list": ["C404,C1,C2"]}, find)

    assert [each["code"] for each in answer.body] == ["C2", "C1"]


def test_a_key_outside_a_recordings_include_goes_unanswered(rules):
    partial = Recording("C1.json", 200, C1, frozenset({"code", "name", "synonyms"}))

    assert rules.answer("/api/v1/concept/ncit_26.09d/C1", {"include": ["summary"]}, finder(partial))
    assert (
        rules.answer("/api/v1/concept/ncit_26.09d/C1", {"include": ["parents"]}, finder(partial))
        is None
    )


def test_a_recorded_404_answers_alone_and_drops_out_of_a_batch(rules):
    unknown = Recording("C404.json", 404, {"message": "C404 not found"}, frozenset())
    table = {("ncit_26.09d", "C1"): full(C1), ("ncit_26.09d", "C404"): unknown}

    def find(terminology, code):
        return table.get((terminology, code))

    single = rules.answer("/api/v1/concept/ncit_26.09d/C404", {"include": ["full"]}, find)
    batch = rules.answer("/api/v1/concept/ncit_26.09d", {"list": ["C404,C1"]}, find)

    assert (single.status, single.body) == (404, {"message": "C404 not found"})
    assert (batch.status, [each["code"] for each in batch.body]) == (200, ["C1"])
    assert batch.names == ["C404.json", "C1.json"]


def test_a_relation_is_answered_with_its_list_or_an_empty_one(rules):
    find = finder(full(C1))

    def answer(relation, params=None):
        found = rules.answer(f"/api/v1/concept/ncit_26.09d/C1/{relation}", params or {}, find)
        return found and found.body

    assert answer("parents") == [{"code": "C9"}]
    assert answer("roles") == []
    assert answer("synonyms") is None
    assert answer("parents", {"limit": ["1"]}) is None


def test_a_relation_the_recording_does_not_cover_or_of_an_unknown_code_goes_unanswered(rules):
    partial = Recording("C1.json", 200, C1, frozenset({"code", "name"}))
    covers = frozenset({"code", "name", "parents"})
    unknown = Recording("C404.json", 404, {"message": "C404 not found"}, covers)
    table = {("ncit_26.09d", "C1"): partial, ("ncit_26.09d", "C404"): unknown}

    def find(terminology, code):
        return table.get((terminology, code))

    assert rules.answer("/api/v1/concept/ncit_26.09d/C1/parents", {}, find) is None
    assert rules.answer("/api/v1/concept/ncit_26.09d/C404/parents", {}, find) is None
    assert rules.answer("/api/v1/concept/ncit_26.09d/C7/parents", {}, find) is None


@pytest.mark.parametrize(
    "section",
    [
        {"base": ["code"], "include": {"minimal": []}},
        {"base": ["code"], "include": [], "default": "x"},
    ],
)
def test_the_rules_need_base_include_and_default(section):
    with pytest.raises(ValueError, match=r"evs\.concepts has base, include, default and"):
        ConceptRules.from_manifest(section)


def test_the_default_is_one_of_the_include_values():
    with pytest.raises(ValueError, match=r"evs\.concepts\.default is one of its include values"):
        ConceptRules.from_manifest(RULES | {"default": "everything"})


def test_a_recording_key_is_the_terminology_and_code_of_its_path():
    assert recording_key("/api/v1/concept/ncit_26.09d/C4817") == ("ncit_26.09d", "C4817")
    for path in ("/api/v1/concept/ncit_26.09d", "/api/v1/concept/ncit_26.09d/C4817/roles"):
        with pytest.raises(ValueError, match="is not the path of one concept"):
            recording_key(path)


def fetch_json(url):
    try:
        with urlopen(url, timeout=10) as response:  # noqa: S310 - a local test server
            return response.status, json.loads(response.read())
    except HTTPError as error:
        with error:
            return error.code, json.loads(error.read())


def test_the_fixture_server_composes_answers_and_logs_the_recordings_used(tmp_path):
    write_manifest(tmp_path)
    recording(tmp_path, "C1", body=C1)
    recording(tmp_path, "C2")
    with FixtureServer(load_fixtures(tmp_path)) as running:
        base = running.base_url("evs") + "/api/v1/concept/ncit_26.09d"
        single = fetch_json(base + "/C1?include=parents")
        batch = fetch_json(base + "?list=C1%2CC2&include=minimal")
        missing = fetch_json(base + "/C3?include=minimal")
        logged = [entry["fixture"] for entry in running.log()]

    assert single == (200, {"code": "C1", "name": "One", "parents": [{"code": "C9"}]})
    assert batch == (200, [concept("C2"), {"code": "C1", "name": "One"}])
    assert missing[0] == HTTPStatus.NOT_IMPLEMENTED
    assert logged == [
        "recorded/evs/concepts/C1.json",
        "recorded/evs/concepts/C1.json, recorded/evs/concepts/C2.json",
        None,
    ]


def test_an_exact_fixture_and_a_scenarios_recording_come_before_the_ordinary_recording(tmp_path):
    write_manifest(tmp_path)
    recording(tmp_path, "C1", body=C1)
    recording(
        tmp_path, "C1", body=C1 | {"name": "Changed"}, where="scenarios/release/mismatch/concepts"
    )
    with FixtureServer(load_fixtures(tmp_path)) as running:
        url = running.base_url("evs") + "/api/v1/concept/ncit_26.09d/C1?include=minimal"
        ordinary = fetch_json(url)[1]["name"]
        running.activate("release/mismatch")
        during = fetch_json(url)[1]["name"]

    assert (ordinary, during) == ("One", "Changed")


def test_an_exact_fixture_answers_before_the_rules_and_only_a_get_is_composed(tmp_path):
    write_manifest(tmp_path)
    recording(tmp_path, "C1", body=C1)
    exact = {
        "kind": "crafted",
        "requirement": "A2.5",
        "request": {
            "surface": "evs",
            "method": "GET",
            "path": "/api/v1/concept/ncit_26.09d/C1",
            "params": {"include": ["minimal"]},
        },
        "response": {"status": 503, "body": {"message": "down"}},
    }
    (tmp_path / "scenarios/upstream/down").mkdir(parents=True)
    (tmp_path / "scenarios/upstream/down/c1.json").write_text(json.dumps(exact), encoding="utf-8")
    with FixtureServer(load_fixtures(tmp_path)) as running:
        url = running.base_url("evs") + "/api/v1/concept/ncit_26.09d/C1?include=minimal"
        running.activate("upstream/down")
        faulted = fetch_json(url)[0]
        running.activate()
        statuses = [faulted, post_status(url), fetch_json(url)[0]]

    assert statuses == [503, HTTPStatus.NOT_IMPLEMENTED, 200]


def post_status(url):
    try:
        with urlopen(Request(url, data=b"{}", method="POST"), timeout=10) as response:  # noqa: S310
            return response.status
    except HTTPError as error:
        with error:
            return error.code


def test_two_active_scenarios_recording_one_concept_are_refused(tmp_path):
    write_manifest(tmp_path)
    for scenario in ("release/a", "release/b"):
        recording(tmp_path, "C1", where=f"scenarios/{scenario}/concepts")

    with (
        FixtureServer(load_fixtures(tmp_path)) as running,
        pytest.raises(
            ValueError, match=r"the scenarios release/a, release/b answer the same request"
        ),
    ):
        running.activate("release/a", "release/b")


def test_two_recordings_of_one_concept_are_refused(tmp_path):
    write_manifest(tmp_path)
    recording(tmp_path, "C1")
    recording(tmp_path, "C1", where="crafted/concepts")

    with pytest.raises(ValueError, match=r"record the same concept"):
        load_fixtures(tmp_path)


def test_recordings_need_the_manifests_rules(tmp_path):
    recording(tmp_path, "C1")

    with pytest.raises(
        ValueError, match=r"concept recordings need the concept rules of manifest\.yaml"
    ):
        load_fixtures(tmp_path)


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        ({"include": "paths"}, "include paths is outside the manifest's table"),
        ({"status": 500}, "a concept recording answers 200 or 404"),
        ({"body": concept("C2")}, "a concept recording's body is the concept C1"),
        ({"body": ["C1"]}, "a concept recording's body is the concept C1"),
    ],
)
def test_an_unusable_recording_is_refused_naming_its_problem(tmp_path, change, problem):
    write_manifest(tmp_path)
    recording(tmp_path, "C1", **change)

    with pytest.raises(ValueError, match=re.escape(f"C1.json: {problem}")):
        load_fixtures(tmp_path)


@pytest.mark.parametrize(
    ("request_change", "problem"),
    [
        ({"surface": "evs-fhir"}, "a concept recording is a GET of /api/v1/concept/"),
        (
            {"path": "/api/v1/concept/ncit_26.09d"},
            "a concept recording is a GET of /api/v1/concept/",
        ),
        (
            {"path": "/api/v1/concept/ncit_26.09d/C1/roles"},
            "a concept recording is a GET of /api/v1/concept/",
        ),
        ({"params": {}}, "a concept recording names exactly the include it was recorded with"),
        (
            {"params": {"include": ["full"], "limit": ["5"]}},
            "a concept recording names exactly the include it was recorded with",
        ),
        (
            {"params": {"include": ["full", "summary"]}},
            "a concept recording names exactly the include it was recorded with",
        ),
        ({"method": "POST"}, "a concept recording is a GET of /api/v1/concept/"),
    ],
)
def test_a_recording_is_a_plain_concept_request(tmp_path, request_change, problem):
    write_manifest(tmp_path)
    recording(tmp_path, "C1")
    path = tmp_path / "recorded/evs/concepts/C1.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    document["request"] |= request_change
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(ValueError, match=problem):
        load_fixtures(tmp_path)


def test_a_recording_is_a_fixture_with_its_provenance(tmp_path):
    write_manifest(tmp_path)
    recording(tmp_path, "C1")
    path = tmp_path / "recorded/evs/concepts/C1.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    del document["recorded_on"]
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(ValueError, match=r"C1\.json: a recorded fixture names recorded_on"):
        load_fixtures(tmp_path)


def test_a_recording_has_one_plain_response(tmp_path):
    write_manifest(tmp_path)
    recording(tmp_path, "C1")
    path = tmp_path / "recorded/evs/concepts/C1.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    document["response"]["delay_seconds"] = 1
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(
        ValueError, match="a concept recording has one response, of status and body"
    ):
        load_fixtures(tmp_path)


@pytest.mark.parametrize(
    "extra", [{"headers": {"Accept": "application/json"}}, {"ignored": {"*": "evidence"}}]
)
def test_a_recording_naming_headers_or_ignored_parameters_is_refused(tmp_path, extra):
    write_manifest(tmp_path)
    recording(tmp_path, "C1")
    path = tmp_path / "recorded/evs/concepts/C1.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    document["request"] |= extra
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(ValueError, match="matched by its concept alone"):
        load_fixtures(tmp_path)


def exact_fixture(root, where, path, params=None):
    """An exact fixture answering GET evs `path` with `params`, written under `where`."""

    target = root / where
    target.parent.mkdir(parents=True, exist_ok=True)
    request = {"surface": "evs", "method": "GET", "path": path, "params": params or {}}
    document = {
        "kind": "crafted",
        "requirement": "self-test",
        "request": request,
        "response": {"status": 200, "body": []},
    }
    target.write_text(json.dumps(document), encoding="utf-8")


@pytest.mark.parametrize(
    ("path", "params"),
    [
        ("/api/v1/concept/ncit_26.09d/C1", {}),
        ("/api/v1/concept/ncit_26.09d/C1", {"include": ["roles"]}),
        ("/api/v1/concept/ncit_26.09d", {"list": ["C1,C2"]}),
        ("/api/v1/concept/ncit_26.09d/C1/roles", {}),
    ],
)
def test_an_ordinary_exact_fixture_where_the_rules_answer_is_refused(tmp_path, path, params):
    write_manifest(tmp_path)
    exact_fixture(tmp_path, "crafted/x.json", path, params)

    with pytest.raises(ValueError, match=r"crafted/x\.json: the concept rules answer this path"):
        load_fixtures(tmp_path)


@pytest.mark.parametrize(
    ("where", "path", "params"),
    [
        ("scenarios/upstream/down/x.json", "/api/v1/concept/ncit_26.09d/C1", {}),
        ("crafted/search.json", "/api/v1/concept/ncit_26.09d/search", {}),
        ("crafted/paths.json", "/api/v1/concept/ncit_26.09d/C1/pathsToRoot", {}),
        ("crafted/paths.json", "/api/v1/concept/ncit_26.09d/C1", {"include": ["paths"]}),
        ("crafted/batch.json", "/api/v1/concept/ncit_26.09d", {"list": [",".join(["C1"] * 1001)]}),
        ("crafted/roles.json", "/api/v1/concept/ncit_26.09d/C1/roles", {"limit": ["5"]}),
    ],
)
def test_an_exact_fixture_for_a_request_the_rules_cannot_answer_or_in_a_scenario_loads(
    tmp_path, where, path, params
):
    write_manifest(tmp_path)
    exact_fixture(tmp_path, where, path, params)

    assert load_fixtures(tmp_path)
