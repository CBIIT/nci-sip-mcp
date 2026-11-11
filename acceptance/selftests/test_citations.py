"""Independent API/MCP comparison rejects corrupted citations on its first attempt."""

import importlib
import json
import runpy
from copy import deepcopy
from dataclasses import replace
from http import HTTPStatus
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest

from nci_si_acceptance import citations
from nci_si_acceptance.fixture_server import FixtureServer, load_fixtures

FIXTURES = Path(__file__).parents[1] / "fixtures"


def verifier():
    package = importlib.import_module("nci_si_acceptance")
    module = importlib.util.find_spec("nci_si_acceptance.citations")
    assert module is not None, "acceptance must expose its independent citation comparison"
    return importlib.import_module(f"{package.__name__}.citations").verify_citations


def identity(identifier, version):
    return {"publicId": identifier, "version": version}


def answer():
    return identity("2200604", "4") | {
        "dateModified": "mcp-date",
        "dataElementConcept": identity("2226947", "1")
        | {
            "objectClass": identity("2236731", "1"),
            "property": identity("2403945", "1"),
        },
        "valueDomain": identity("2222445", "3")
        | {
            "RepresentationTerm": identity("2234813", "1"),
        },
        "conceptAssociations": [
            {
                "role": role,
                "conceptCode": code,
                "terminology": "NCI_CONCEPT_CODE",
                "primary": True,
                "displayOrder": 0,
            }
            for role, code in [
                ("objectClass", "C25190"),
                ("property", "C28421"),
                ("representationTerm", "C25284"),
            ]
        ],
    }


class Tools:
    def __init__(self, content):
        self.content, self.calls = content, []

    def call(self, name, arguments):
        self.calls.append((name, deepcopy(arguments)))
        return SimpleNamespace(is_error=False, content=self.content)


@pytest.mark.parametrize(
    ("path", "key", "value"),
    [
        (path, key, value)
        for path in (
            (),
            ("dataElementConcept",),
            ("valueDomain",),
            ("dataElementConcept", "objectClass"),
            ("dataElementConcept", "property"),
            ("valueDomain", "RepresentationTerm"),
        )
        for key, value in (("publicId", "999999"), ("version", "999"))
    ]
    + [
        (("conceptAssociations", index), key, value)
        for index in range(3)
        for key, value in (
            ("conceptCode", "C999"),
            ("terminology", "other"),
            ("primary", False),
            ("displayOrder", 1),
            ("displayOrder", "0"),
        )
    ],
)
def test_citation_mismatch_keeps_both_dates_and_never_retries(path, key, value):
    check = verifier()
    content = answer()
    target = content
    for part in path:
        target = target[part]
    target[key] = value
    tools = Tools(content)
    with FixtureServer(load_fixtures(FIXTURES)) as upstream:
        with pytest.raises(
            AssertionError, match=r"API dateModified=2026-08-25; MCP dateModified=mcp-date"
        ):
            check(tools, upstream.base_url("cadsr"))
        requests = upstream.log()
    assert [(row["path"], row["params"]) for row in requests] == [
        ("/NCIAPI/1.0/api/DataElement/2200604", {"version": ["4"]})
    ]
    assert tools.calls == [
        (
            "get_data_element",
            {
                "publicId": "2200604",
                "version": "4",
                "include": ["dataElementConcept", "conceptAssociations", "valueDomain"],
            },
        )
    ]


def test_citation_comparison_excludes_names_and_dates():
    check = verifier()
    tools = Tools(answer() | {"longName": "unrelated name"})
    with FixtureServer(load_fixtures(FIXTURES)) as upstream:
        check(tools, upstream.base_url("cadsr"))
        assert len(upstream.log()) == 1
    assert len(tools.calls) == 1


def test_reference_http_failure_is_one_attempt_and_never_calls_mcp():
    check = verifier()
    fixtures = load_fixtures(FIXTURES)
    key = next(
        key
        for key, row in fixtures.ordinary.items()
        if row.name == "recorded/cadsr/data-element-2200604-version-4.json"
    )
    fixture = fixtures.ordinary[key]
    fixtures.ordinary[key] = replace(
        fixture, responses=(replace(fixture.responses[0], status=503),)
    )
    tools = Tools(answer())
    with FixtureServer(fixtures) as upstream:
        with pytest.raises(HTTPError) as failure:
            check(tools, upstream.base_url("cadsr"))
        with failure.value:
            assert failure.value.code == HTTPStatus.SERVICE_UNAVAILABLE
            assert len(upstream.log()) == 1
    assert tools.calls == []


def test_controlled_live_citation_refuses_before_any_transport(monkeypatch):
    # Serve recorded bytes in memory even on the old path: no possible external send.
    recorded = json.loads((FIXTURES / "recorded/cadsr/data-element-2200604.json").read_bytes())
    raw = json.dumps(recorded["response"]["body"]).encode()
    fetched = []

    def recorded_response(request, **kwargs):
        fetched.append(request.full_url)
        return BytesIO(raw)

    monkeypatch.setattr(citations, "urlopen", recorded_response)
    caller = runpy.run_path(str(FIXTURES.parent / "tests/test_cadsr.py"))
    tools = Tools(answer())
    with pytest.raises(ValueError) as error:
        caller["test_live_component_citations_match_the_explicit_api_version"](tools, None)
    assert str(error.value) == "live caDSR citation comparison is disabled by run policy"
    assert fetched == []
    assert tools.calls == []
