"""An upstream that answers from fixtures and records every request it receives.

The server under test is pointed at this server instead of the live services, one
path prefix per upstream surface (`/evs`, `/cadsr`, ...). Every request is recorded,
answered or not, whatever its method:

    GET    /_log    the requests received since the last reset, as JSON
    DELETE /_log    reset: clear the log and rewind every response sequence

A request is answered by the fixture whose surface, method, path, query parameters,
headers and body match it. Paths and values are compared decoded, the order of
different parameters does not matter, and repeated values of one parameter keep their
order. Bodies are compared as parsed JSON where they are JSON, and otherwise as text
with runs of whitespace collapsed. A form-encoded body (`Content-Type:
application/x-www-form-urlencoded`, a SPARQL query) is compared field by field, decoded,
each value with runs of whitespace collapsed; its fixture names it as `form`, each field
with its text. A body is a form only where its content type says so, as the live service
reads it. A fixture without a `body` or `form` matches any request
body. A parameter the live service is shown to ignore may be declared under
`ignored`, with the evidence; it is then left out of the match, and `"*"` leaves out
every parameter (an unknown release answers 404 whatever is asked; a fault fixture
fails whatever is asked), the fixture's `params` then only recording what was asked
when it was captured. A fixture's `headers` must be present with those values,
header names in any case; among the fixtures of one path in one layer (below), the one
naming the most headers the request carries answers (a licence key granted, else the
refusal), and between header sets of the same size the first by name. A request that
no fixture answers gets HTTP 501.

A fixture is a JSON file:

    {
      "kind": "recorded",                 # or "crafted"
      "recorded_on": "2026-10-02",        # recorded: when it was captured
      "requirement": "C-1",               # crafted: the requirement it stands in for
      "request": {"surface": "evs", "method": "GET", "path": "/api/v1/version",
                  "params": {"include": ["summary"]}, "body": "...",
                  "form": {"query": "SELECT ..."},     # in place of body: a form
                  "headers": {"X-EVSRESTAPI-License-Key": "..."},
                  "ignored": {"count": "the evidence that the service ignores it"}},
      "response": {"status": 200, "headers": {}, "body": {...}}
    }

A recorded fixture's request never carries the EVS licence key: what EVS serves without
it is public, and licensed content, which EVS refuses without it (403), is never recorded.

`responses` (a list) in place of `response` answers successive requests in order,
the last one repeating; the sequence starts again at each reset. A response may wait
`delay_seconds` before it is sent, or be the fault `{"fault": "close"}`: the
connection is closed without an answer. That stands in for an unreachable service as
far as a fixture can; a refused connection cannot be produced per request. A string
`body` is sent as HTML, any other body as JSON, and no body as nothing. The fixture's
headers are sent too and override the content type; headers that frame the message
(`Content-Length`, ...) are the server's and are refused.

A scenario fixture whose path is `*` answers every request of its surface and method that
no other fixture of its scenario answers, whatever its parameters, headers and body: the
service as a whole unavailable. It matches on nothing else, and an ordinary one is refused.

Fixtures under `scenarios/<group>/<name>/` belong to the scenario `<group>/<name>`
and answer only while it is active, before any ordinary fixture: each active scenario
is a layer consulted before the ordinary fixtures, so a scenario fixture wins over an
ordinary one that names more headers or the exact body. Two fixtures for
the same request in one scenario, outside the scenarios, or in two scenarios active
together, are refused. A scenario may also hold `settings.json`, the `NCI_SI_*`
settings its server process starts with (a short timeout, a licence key); the settings
the harness makes itself (upstream URLs, mode, data directory) are refused, and so are
two scenarios active together that set the same one.

A fixture in a `concepts/` directory is a concept recording: EVS's answer to a
concept request, which `concepts.py` projects and batches by the rules the manifest
(`manifest.yaml`, `evs.concepts`) declares. A request no fixture answers exactly is
answered by those rules where the active recordings allow, a scenario's recording of
a code before the ordinary one. Every exact fixture is tried before the rules: an
ordinary one for a request the rules answer is refused, since it would answer before a
scenario's recording; an active scenario's exact fixture (a fault) answers before
another active scenario's recording, by design.

The manifest's `crafted:` line declares the fixtures `craft.py` makes: they are built in
memory when the set loads and added to the files by name; a crafted name that also lies on disk
is refused.

Every log entry records when the request arrived (`received_at`, monotonic seconds),
and the fixture or recordings that answered it.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from itertools import permutations
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any, Self
from urllib.parse import parse_qs, unquote, urlsplit

import yaml

from nci_si_acceptance.concepts import ConceptRules, Recording, recording_key

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

# The server's upstream settings (docs/implementation-plan.md §8) and the fixture surface each one
# names: the path prefix the fixture server serves it under.
UPSTREAM_VARIABLES = {
    "NCI_SI_EVS_BASE_URL": "evs",
    "NCI_SI_EVS_FHIR_BASE_URL": "evs-fhir",
    "NCI_SI_CADSR_BASE_URL": "cadsr",
    "NCI_SI_CADSR_FTP_URL": "cadsr-ftp",
    "NCI_SI_SSIS_SPARQL_URL": "ssis-sparql",
}
# What the harness sets itself, and a scenario's settings may not override.
HARNESS_VARIABLES = frozenset({*UPSTREAM_VARIABLES, "NCI_SI_UPSTREAM_MODE", "NCI_SI_DATA_DIR"})
SCENARIOS = "scenarios"
# The path of a scenario fixture that answers every request of its surface.
EVERY_PATH = "*"
LOG_PATH = "/_log"
FRAMING_HEADERS = frozenset(
    {"content-length", "transfer-encoding", "content-encoding", "connection"}
)
FAULTS = frozenset({"close"})
LICENCE_HEADER = "x-evsrestapi-license-key"
SETTINGS = "settings.json"
MANIFEST = "manifest.yaml"
# The manifest key that declares the crafted fixtures and records their digest (craft.py).
CRAFTED = "crafted"
CONCEPTS = "concepts"
RESPONSE_FIELDS = frozenset({"status", "headers", "body", "delay_seconds", "fault"})
FORM_TYPE = "application/x-www-form-urlencoded"
_JSON = {"Content-Type": "application/json"}

type Params = dict[str, list[str]]
type Fields = tuple[tuple[str, tuple[str, ...]], ...]
type Key = tuple[str, str, str, Fields, tuple[tuple[str, str], ...], str | Fields | None]
type Fixtures = dict[Key, Fixture]
type Concepts = dict[tuple[str, str], Recording]


@dataclass(frozen=True, slots=True)
class Form:
    """A form-encoded body, decoded: each field's values."""

    fields: dict[str, list[str]]


def _collapsed(text: str) -> str:
    return " ".join(text.split())


def body_key(body: Any) -> str | Fields | None:
    """A body as it is compared: a form's fields, canonical JSON, or text, with runs of
    whitespace collapsed in text."""

    if body is None:
        return None
    if isinstance(body, Form):
        return tuple(
            sorted((name, tuple(map(_collapsed, values))) for name, values in body.fields.items())
        )
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except ValueError:
            return _collapsed(body)
    return json.dumps(body, sort_keys=True, separators=(",", ":"))


def request_key(
    surface: str,
    method: str,
    path: str,
    params: Params,
    body: Any = None,
    headers: dict[str, str] | None = None,
) -> Key:
    """What a request is matched on; `body` None stands for any body, and only the
    headers a fixture names are matched."""

    query = tuple(sorted((name, tuple(values)) for name, values in params.items()))
    named = tuple(sorted((name.lower(), value) for name, value in (headers or {}).items()))
    return (surface, method.upper(), path, query, named, body_key(body))


def _merge(defaults: dict[str, str], headers: dict[str, str]) -> dict[str, str]:
    """`defaults` overridden by `headers`, matching header names in any case."""

    overridden = {name.lower() for name in headers}
    kept = {name: value for name, value in defaults.items() if name.lower() not in overridden}
    return kept | headers


@dataclass(frozen=True, slots=True)
class Response:
    status: int = HTTPStatus.OK
    headers: dict[str, str] = field(default_factory=dict)
    body: Any = None
    delay_seconds: float = 0
    fault: str | None = None

    def encode(self) -> tuple[bytes, dict[str, str]]:
        """The response body, and its headers with a content type that fits the body."""

        if self.body is None:
            return b"", self.headers
        if isinstance(self.body, str):
            html = {"Content-Type": "text/html; charset=utf-8"}
            return self.body.encode(), _merge(html, self.headers)
        return json.dumps(self.body).encode(), _merge(_JSON, self.headers)


@dataclass(frozen=True, slots=True)
class Fixture:
    name: str
    responses: tuple[Response, ...]
    ignored: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class FixtureSet:
    """The ordinary fixtures, and the fixtures of each scenario.

    Concept recordings are kept apart, by (terminology, code), under the scenario they
    belong to; the ordinary ones under None.
    """

    ordinary: Fixtures
    scenarios: dict[str, Fixtures]
    settings: dict[str, dict[str, str]] = field(default_factory=dict)
    rules: ConceptRules | None = None
    recordings: dict[str | None, Concepts] = field(default_factory=dict)

    def settings_of(self, scenarios: tuple[str, ...]) -> dict[str, str]:
        """The server settings the scenarios start their server with."""

        return {
            name: value
            for scenario in scenarios
            for name, value in self.settings.get(scenario, {}).items()
        }


def _request_problem(request: Any) -> str | None:
    if not isinstance(request, dict) or not all(
        isinstance(request.get(part), str) for part in ("surface", "method", "path")
    ):
        return "a fixture names its request: surface, method and path"
    return (
        _params_problem(request.get("params", {}))
        or _headers_problem(request.get("headers", {}))
        or _form_problem(request)
        or _ignored_problem(request)
    )


def _form_problem(request: dict[str, Any]) -> str | None:
    form = request.get("form", {})
    if not isinstance(form, dict) or not all(isinstance(text, str) for text in form.values()):
        return "a form maps each field to its text"
    if form and "body" in request:
        return "a request has a body or a form, not both"
    return None


def _params_problem(params: Any) -> str | None:
    if not isinstance(params, dict) or not all(
        isinstance(values, list) and all(isinstance(value, str) for value in values)
        for values in params.values()
    ):
        return "params maps each parameter to a list of strings"
    return None


def _headers_problem(headers: Any) -> str | None:
    if not isinstance(headers, dict) or not all(
        isinstance(name, str) and isinstance(value, str) for name, value in headers.items()
    ):
        return "headers maps each header name to its value"
    return None


def _ignored_problem(request: dict[str, Any]) -> str | None:
    ignored = request.get("ignored", {})
    if not isinstance(ignored, dict) or not all(
        isinstance(evidence, str) and evidence for evidence in ignored.values()
    ):
        return "an ignored parameter names the evidence that the service ignores it"
    if set(ignored) & set(request.get("params", {})):
        return "an ignored parameter is not also matched"
    return None


def _response_problem(response: dict[str, Any]) -> str | None:
    if not set(response) <= RESPONSE_FIELDS:
        return f"a response has only {', '.join(sorted(RESPONSE_FIELDS))}"
    framing = sorted(set(map(str.lower, response.get("headers", {}))) & FRAMING_HEADERS)
    if framing:
        return f"the server frames the response; remove {', '.join(framing)}"
    if response.get("fault", "close") not in FAULTS:
        return f"a fault is one of {', '.join(sorted(FAULTS))}"
    return None


def _problem(document: dict[str, Any]) -> str | None:
    """What makes a fixture document unusable, if anything."""

    if ("response" in document) == ("responses" in document):
        return "a fixture has either a response or responses"
    responses = document.get("responses") or [document["response"]]
    problems = [_request_problem(document.get("request")), *map(_response_problem, responses)]
    return next((problem for problem in problems if problem), None) or _provenance_problem(document)


def _provenance_problem(document: dict[str, Any]) -> str | None:
    kind = document.get("kind")
    if kind == "recorded":
        if LICENCE_HEADER in map(str.lower, document.get("request", {}).get("headers", {})):
            return "a recording is made without the licence key: licensed content is not recorded"
        return None if document.get("recorded_on") else "a recorded fixture names recorded_on"
    if kind == "crafted":
        return None if document.get("requirement") else "a crafted fixture names its requirement"
    return "kind is recorded or crafted"


def _matched_params(request: dict[str, Any]) -> Params:
    """The parameters a fixture matches on: none where it ignores them all."""

    return {} if "*" in request.get("ignored", {}) else request.get("params", {})


def _read_fixture(name: str, document: Any) -> tuple[Key, Fixture]:
    if problem := _problem(document):
        raise ValueError(f"{name}: {problem}")
    request = document["request"]
    form = request.get("form")
    key = request_key(
        request["surface"],
        request["method"],
        request["path"],
        _matched_params(request),
        Form({name: [text] for name, text in form.items()}) if form else request.get("body"),
        request.get("headers"),
    )
    responses = tuple(
        Response(**response) for response in document.get("responses") or [document["response"]]
    )
    return key, Fixture(name, responses, frozenset(request.get("ignored", {})))


def _read_recording(
    name: str, document: Any, rules: ConceptRules | None
) -> tuple[tuple[str, str], Recording]:
    """A concept recording, keyed by the terminology and code its request names."""

    if rules is None:
        raise ValueError(f"{name}: concept recordings need the concept rules of {MANIFEST}")
    problem = _problem(document) or _recording_problem(document, rules)
    if problem:
        raise ValueError(f"{name}: {problem}")
    request, response = document["request"], document["response"]
    covers = rules.keys(request["params"]["include"][0]) or frozenset()
    return recording_key(request["path"]), Recording(
        name, response["status"], response.get("body"), covers
    )


def _recording_problem(document: dict[str, Any], rules: ConceptRules) -> str | None:
    response = document.get("response")
    if response is None or not set(response) <= {"status", "body"}:
        return "a concept recording has one response, of status and body"
    if set(document["request"]) & {"headers", "ignored"}:
        return "a concept recording is matched by its concept alone: no headers, nothing ignored"
    return rules.recording_problem(document["request"], response["status"], response.get("body"))


def read_manifest(root: Path) -> dict[str, Any]:
    """The manifest of a fixture set: empty where the set has none, refused where it is not a
    mapping, since the concept rules and the crafted layer would then be left out unseen."""

    path = root / MANIFEST
    if not path.is_file():
        return {}
    manifest = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise ValueError(f"{path}: {MANIFEST} must be a mapping")
    return manifest


def _rules(manifest: dict[str, Any]) -> ConceptRules | None:
    section = manifest.get("evs", {}).get("concepts")
    return None if section is None else ConceptRules.from_manifest(section)


def fixture_documents(root: Path) -> dict[str, Any]:
    """Every fixture document of the set by name: the files, and the crafted ones the manifest
    declares, made in memory."""

    return _documents(root, read_manifest(root))


def _documents(root: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    documents = {
        path.relative_to(root).as_posix(): json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(root.rglob("*.json"))
    }
    if CRAFTED not in manifest:
        return documents
    # craft.py imports this module for its constants, so it is imported when it is needed.
    from nci_si_acceptance.craft import craft  # noqa: PLC0415

    crafted = craft(root)
    if twice := sorted(set(crafted) & set(documents)):
        raise ValueError(f"{', '.join(twice)}: crafted in memory and also on disk; delete it")
    return documents | crafted


def _scenario_of(name: str) -> str | None:
    parts = name.split("/")
    if parts[0] != SCENARIOS:
        return None
    if len(parts) < 4:  # noqa: PLR2004 - scenarios/<group>/<name>/<fixture>
        raise ValueError(f"{name}: a scenario fixture lies in scenarios/<group>/<name>/")
    return f"{parts[1]}/{parts[2]}"


def _read_settings(name: str, settings: Any, scenario: str | None) -> dict[str, str]:
    if scenario is None:
        raise ValueError(f"{name}: settings belong to a scenario")
    if not isinstance(settings, dict) or not all(
        key.startswith("NCI_SI_") and isinstance(value, str) for key, value in settings.items()
    ):
        raise ValueError(f"{name}: settings are NCI_SI_* names with string values")
    if reserved := sorted(set(settings) & HARNESS_VARIABLES):
        raise ValueError(f"{name}: the harness sets {', '.join(reserved)}")
    return settings


def load_fixtures(root: Path) -> FixtureSet:
    """Every fixture below `root`, sorted into the ordinary ones and those of each scenario."""

    if not root.is_dir():
        raise ValueError(f"{root} is not a fixture directory")
    manifest = read_manifest(root)
    fixtures = FixtureSet({}, {}, rules=_rules(manifest))
    for name, document in sorted(_documents(root, manifest).items()):
        scenario, path = _scenario_of(name), PurePosixPath(name)
        if path.name == SETTINGS:
            fixtures.settings[str(scenario)] = _read_settings(name, document, scenario)
        elif path.parent.name == CONCEPTS:
            _add_recording(fixtures, scenario, _read_recording(name, document, fixtures.rules))
        else:
            _add_fixture(fixtures, scenario, name, document)
    return fixtures


def _add_recording(
    fixtures: FixtureSet, scenario: str | None, keyed: tuple[tuple[str, str], Recording]
) -> None:
    key, recording = keyed
    table = fixtures.recordings.setdefault(scenario, {})
    if key in table:
        raise ValueError(f"{recording.name} and {table[key].name} record the same concept")
    table[key] = recording


def _add_fixture(fixtures: FixtureSet, scenario: str | None, name: str, document: Any) -> None:
    table = fixtures.ordinary if scenario is None else fixtures.scenarios.setdefault(scenario, {})
    key, fixture = _read_fixture(name, document)
    if _misplaced_everywhere(key, scenario):
        raise ValueError(
            f"{fixture.name}: a fixture for every path belongs to a scenario and matches on"
            " nothing else"
        )
    if scenario is None and _composed_path(fixtures.rules, key):
        # Exact fixtures are tried before the rules, so this one would answer in place of
        # an active scenario's recording of the concept.
        raise ValueError(f"{fixture.name}: the concept rules answer this path; record the concept")
    if key in table:
        raise ValueError(f"{fixture.name} and {table[key].name} answer the same request")
    table[key] = fixture


def _misplaced_everywhere(key: Key, scenario: str | None) -> bool:
    """Whether a fixture for every path is ordinary, or matches on more than its surface."""

    return key[2] == EVERY_PATH and (scenario is None or key != _everywhere(key[0], key[1]))


def _everywhere(surface: str, method: str) -> Key:
    """The key of a fixture that answers every path of a surface and method."""

    return request_key(surface, method, EVERY_PATH, {})


def _composed_path(rules: ConceptRules | None, key: Key) -> bool:
    surface, method, path, query = key[:4]
    params = {name: list(values) for name, values in query}
    return (
        rules is not None and (surface, method) == ("evs", "GET") and rules.composes(path, params)
    )


def _layers(
    fixtures: FixtureSet, scenarios: tuple[str, ...]
) -> tuple[list[Fixtures], list[Concepts]]:
    """The fixtures and the concept recordings in the order they are consulted: the
    scenarios, then the rest."""

    _check_known(fixtures, scenarios)
    layers = [fixtures.scenarios.get(scenario, {}) for scenario in scenarios]
    recordings = [fixtures.recordings.get(scenario, {}) for scenario in scenarios]
    if clash := _clash(fixtures, scenarios, layers, recordings):
        raise ValueError(f"the scenarios {', '.join(scenarios)} {clash}")
    return [*layers, fixtures.ordinary], [*recordings, fixtures.recordings.get(None, {})]


def _clash(
    fixtures: FixtureSet,
    scenarios: tuple[str, ...],
    layers: list[Fixtures],
    recordings: list[Concepts],
) -> str | None:
    """Why scenarios cannot be active together, if they cannot."""

    if _overlap(layers) or _overlap(recordings):
        return "answer the same request"
    if _overlap(fixtures.settings.get(scenario, {}) for scenario in scenarios):
        return "set the same setting"
    if _hidden(layers, recordings):
        return (
            "cannot be active together: one answers every path of a surface the other has"
            " fixtures for"
        )
    return None


def _hidden(layers: list[Fixtures], recordings: list[Concepts]) -> bool:
    """Whether a scenario's fixture for every path would answer for another active scenario,
    its concept recordings included (they answer EVS concept requests)."""

    surfaces = [
        _surfaces(layer, recorded) for layer, recorded in zip(layers, recordings, strict=True)
    ]
    everywhere = [{key[:2] for key in layer if key[2] == EVERY_PATH} for layer in layers]
    pairs = permutations(range(len(layers)), 2)
    return any(everywhere[one] & surfaces[other] for one, other in pairs)


def _surfaces(layer: Fixtures, recorded: Concepts) -> set[tuple[str, str]]:
    """The surfaces and methods a scenario answers on; its recordings answer EVS GET."""

    return {key[:2] for key in layer} | ({("evs", "GET")} if recorded else set())


def _check_known(fixtures: FixtureSet, scenarios: tuple[str, ...]) -> None:
    known = {*fixtures.scenarios, *fixtures.settings, *fixtures.recordings}
    if unknown := sorted(set(scenarios) - known):
        raise ValueError(f"no such scenario: {', '.join(unknown)}")


def _overlap(tables: Iterable[dict[Any, Any]]) -> bool:
    keys = [key for table in tables for key in table]
    return len(keys) != len(set(keys))


def _left_out(names: set[str], ignored: frozenset[str]) -> list[str]:
    """Which of these parameter names `ignored` leaves out of the match."""

    return sorted(names if "*" in ignored else names & ignored)


def _header_sets(layers: list[Fixtures]) -> dict[tuple[str, str, str], list[tuple[str, ...]]]:
    """The header names the fixtures of each path match on, the most specific first and
    those of one size by name, so that the order never depends on hashing."""

    named: dict[tuple[str, str, str], set[tuple[str, ...]]] = {}
    for layer in layers:
        for key in layer:
            named.setdefault(key[:3], set()).add(tuple(name for name, _ in key[4]))
    return {
        path: sorted(sets, key=lambda names: (-len(names), names)) for path, sets in named.items()
    }


def _ignored_by_path(layers: list[Fixtures]) -> dict[tuple[str, str, str], frozenset[str]]:
    """The parameters left out of the match, per request path, over every active fixture.

    A fixture that matches on a parameter another fixture of its path ignores could
    never answer, so it is refused.
    """

    ignored: dict[tuple[str, str, str], frozenset[str]] = {}
    for layer in layers:
        for key, fixture in layer.items():
            ignored[key[:3]] = ignored.get(key[:3], frozenset()) | fixture.ignored
    for layer in layers:
        for key, fixture in layer.items():
            if clash := _left_out({name for name, _ in key[3]}, ignored[key[:3]]):
                names = ", ".join(clash)
                raise ValueError(f"{fixture.name} matches on {names}, which its path ignores")
    return ignored


class FixtureServer:
    """The fixture upstream, by default on a free local port, served from a background thread.

    `address` is where it listens; `url` is how the harness itself reaches it.
    """

    def __init__(self, fixtures: FixtureSet, address: tuple[str, int] = ("127.0.0.1", 0)) -> None:
        self.fixtures = fixtures
        self._layers: list[Fixtures] = []
        self._recordings: list[Concepts] = []
        self._headers: dict[tuple[str, str, str], list[tuple[str, ...]]] = {}
        self._ignored: dict[tuple[str, str, str], frozenset[str]] = {}
        self._served: dict[str, int] = {}
        self._log: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self.activate()
        self._http = ThreadingHTTPServer(address, _handler(self))
        # A wildcard address is not one to connect to.
        host = address[0] if address[0] != "0.0.0.0" else "127.0.0.1"  # noqa: S104
        self.url = f"http://{host}:{self._http.server_port}"
        self._thread = threading.Thread(target=self._http.serve_forever, daemon=True)

    def __enter__(self) -> Self:
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self._http.shutdown()
        self._http.server_close()
        self._thread.join()

    def base_url(self, surface: str) -> str:
        return f"{self.url}/{surface}"

    def activate(self, *scenarios: str) -> None:
        """Answer from these scenarios' fixtures first; no argument ends them. The log stays."""

        layers, recordings = _layers(self.fixtures, scenarios)
        ignored, headers = _ignored_by_path(layers), _header_sets(layers)
        with self._lock:
            self._layers, self._recordings = layers, recordings
            self._ignored, self._headers = ignored, headers

    def log(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._log)

    def reset(self) -> None:
        with self._lock:
            self._log.clear()
            self._served.clear()

    def _find(
        self, path: tuple[str, str, str], params: Params, body: str | Form, headers: dict[str, str]
    ) -> Fixture | None:
        """The fixture for a request: the active scenarios' layers first, then the ordinary
        one; within a layer the most headers matched first, the exact body before any body."""

        carried = self._carried(path, headers)
        for layer in self._layers:
            for named in carried:
                key = request_key(*path, params, body, named)
                if fixture := layer.get(key) or layer.get((*key[:5], None)):
                    return fixture
            if fixture := layer.get(_everywhere(*path[:2])):
                return fixture
        return None

    def _carried(self, path: tuple[str, str, str], headers: dict[str, str]) -> list[dict[str, str]]:
        """The header sets the fixtures of this path name that the request carries, with the
        request's values, the most specific first."""

        lowered = {name.lower(): value for name, value in headers.items()}
        return [
            {name: lowered[name] for name in names}
            for names in self._headers.get(path, [])
            if all(name in lowered for name in names)
        ]

    def _next(
        self, path: tuple[str, str, str], params: Params, body: str | Form, headers: dict[str, str]
    ) -> tuple[str | None, Response | None]:
        """The fixture answering a request and its response in turn."""

        fixture = self._find(path, params, body, headers)
        if fixture is None:
            return None, None
        turn = self._served.get(fixture.name, 0)
        self._served[fixture.name] = turn + 1
        return fixture.name, fixture.responses[min(turn, len(fixture.responses) - 1)]

    def _recording(self, terminology: str, code: str) -> Recording | None:
        for layer in self._recordings:
            if recording := layer.get((terminology, code)):
                return recording
        return None

    def _composed(
        self, path: tuple[str, str, str], params: Params
    ) -> tuple[str | None, Response | None]:
        """The answer the concept rules compose from the active recordings, if they can."""

        surface, method, path_only = path
        rules = self.fixtures.rules
        if rules is None or (surface, method) != ("evs", "GET"):
            return None, None
        answer = rules.answer(path_only, params, self._recording)
        if answer is None:
            return None, None
        return ", ".join(answer.names), Response(answer.status, {}, answer.body)

    def answer(self, method: str, target: str, headers: dict[str, str], body: str) -> Response:
        """Choose the response to one upstream request, and record the request."""

        url = urlsplit(target)
        surface, _, rest = unquote(url.path).removeprefix("/").partition("/")
        path, params = f"/{rest}", parse_qs(url.query, keep_blank_values=True)
        entry = {"surface": surface, "method": method, "path": path, "params": params}
        # The path and query as sent, undecoded: an encoded / and a literal one match alike,
        # and only this tells them apart (A7.6).
        entry["raw"] = "/" + target.removeprefix("/").partition("/")[2]
        entry["received_at"] = time.monotonic()
        with self._lock:
            where = (surface, method.upper(), path)
            left_out = _left_out(set(params), self._ignored.get(where, frozenset()))
            matched = {name: values for name, values in params.items() if name not in left_out}
            fixture, response = self._next(where, matched, _decoded(body, headers), headers)
            if fixture is None:
                fixture, response = self._composed(where, params)
            entry |= {"headers": headers, "body": body, "fixture": fixture}
            self._log.append(entry)
        if response is None:
            message = {"error": "no fixture answers this request", **entry}
            return Response(HTTPStatus.NOT_IMPLEMENTED, _JSON, message)
        return response


def _decoded(body: str, headers: dict[str, str]) -> str | Form:
    """A request body as it is matched: decoded into its fields where the request says it is a
    form, as the live service reads it; any other as sent."""

    kind = {name.lower(): value for name, value in headers.items()}.get("content-type", "")
    if kind.partition(";")[0].strip().lower() != FORM_TYPE:
        return body
    return Form(parse_qs(body, keep_blank_values=True))


def _handler(server: FixtureServer) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, body: bytes, headers: dict[str, str]) -> None:
            self.send_response(status)
            for name, value in headers.items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _upstream(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length).decode("utf-8", "replace")
            response = server.answer(self.command, self.path, dict(self.headers), body)
            time.sleep(response.delay_seconds)
            if response.fault == "close":
                self.close_connection = True
                return
            self._send(response.status, *response.encode())

        def do_GET(self) -> None:
            if self.path == LOG_PATH:
                self._send(HTTPStatus.OK, json.dumps(server.log()).encode(), _JSON)
            else:
                self._upstream()

        def do_DELETE(self) -> None:
            if self.path == LOG_PATH:
                server.reset()
                self._send(HTTPStatus.NO_CONTENT, b"", {})
            else:
                self._upstream()

        # http.server finds a handler by these names.
        do_HEAD = do_POST = do_PUT = do_PATCH = do_OPTIONS = _upstream  # noqa: N815

        def log_message(self, format: str, *args: object) -> None:
            """Silent: the request log is the record."""

    return Handler
