"""Record the fixture set from the live services, as the manifest says.

    pdm run acceptance-record [--fixtures DIR]

Every request the manifest lists under `record.requests` is made live, without a licence
key or credentials, and becomes a recorded fixture, dated today. A request is a GET with
`Accept: application/json` unless its entry gives a `method`, a `body` (JSON, or text sent
as written) or a `form` (sent form-encoded: a SPARQL query), and the `headers` to send:
those it gives are sent alone and are part of the fixture, so that the fixture server
answers only a request that carries them. Each concept under
`record.concepts` is recorded once, at the include it is listed under, in
`recorded/evs/concepts/`. Each entry under
`record.derived` becomes a crafted fixture for the request form a requirement prescribes
where EVS does not answer it yet, carrying the answer of a recording.
Nothing is written unless all of this holds:

- the live monthly NCIt release is the one the manifest pins (`evs.release`):
  re-pinning is a re-recording under change control;
- every request is answered, with the status its entry expects (`status`, 200 unless
  given), and every concept recording is one the fixture server accepts;
- every sample under `record.samples`, asked live, equals the answer the concept
  rules compose from the new recordings, a batch compared code by code because EVS
  keeps no order;
- every derived fixture comes from a recording the manifest makes, of the pinned
  release where the answer names one;
- every file under the recorded surfaces, and every derived fixture, is one the
  manifest produces.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from http import HTTPStatus
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.error import HTTPError
from urllib.parse import parse_qs, quote, urlencode, urlsplit
from urllib.request import Request, urlopen

import yaml

from nci_si_acceptance.concepts import ConceptRules, Recording, recording_key
from nci_si_acceptance.fixture_server import CONCEPTS, FORM_TYPE, MANIFEST

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

type Params = dict[str, list[str]]
type Fetch = Callable[..., tuple[int, Any]]

FIXTURES = Path(__file__).parents[2] / "fixtures"
RECORDED = "recorded"
# Payload fields that name the release or version of the content they carry: a concept's
# `version`, a map's `sourceTerminologyVersion`.
RELEASE_FIELDS = ("version", "sourceTerminologyVersion")
# Release discovery and the API version: what names releases rather than serving content
# from one, and needs no pinned form.
DISCOVERY = frozenset(
    {
        "recorded/evs/version.json",
        "recorded/evs/terminologies.json",
        "recorded/evs/release-monthly.json",
        "recorded/evs/release-weekly.json",
    }
)
TIMEOUT_SECONDS = 120


class RecordingError(Exception):
    """The recording does not hold; each problem is one line."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("\n".join(problems))
        self.problems = problems


@dataclass(frozen=True, slots=True)
class Planned:
    """One request to record, and the fixture file it becomes."""

    file: str
    surface: str
    path: str
    params: Params
    # Parameters the service is shown to ignore, with the evidence (fixture_server.py).
    ignored: dict[str, str] = field(default_factory=dict)
    status: int = HTTPStatus.OK
    method: str = "GET"
    # The headers sent and recorded; None sends Accept: application/json and records none.
    headers: dict[str, str] | None = None
    body: Any = None
    # A form-encoded body, each field with its text; in place of `body`.
    form: dict[str, str] | None = None

    def sent(self) -> dict[str, Any]:
        """The method, headers and body or form to send, where the request is not a plain GET."""

        sent = {"method": self.method, "headers": self.headers, "body": self.body}
        if self.form is not None:
            sent["form"] = self.form
        return {} if sent == {"method": "GET", "headers": None, "body": None} else sent


def _split(target: str) -> tuple[str, Params]:
    url = urlsplit(target)
    return url.path, parse_qs(url.query, keep_blank_values=True)


def plan(manifest: dict[str, Any]) -> list[Planned]:
    """The requests the manifest asks to be recorded."""

    record, release = manifest["record"], manifest["evs"]["release"]
    planned = [
        Planned(
            entry["fixture"],
            entry["surface"],
            *_split(entry["path"]),
            entry.get("ignored", {}),
            entry.get("status", HTTPStatus.OK),
            entry.get("method", "GET"),
            entry.get("headers"),
            entry.get("body"),
            entry.get("form"),
        )
        for entry in record.get("requests", [])
    ]
    for include, codes in record.get("concepts", {}).items():
        planned += [
            Planned(
                f"{RECORDED}/evs/{CONCEPTS}/{code}.json",
                "evs",
                f"/api/v1/concept/{release}/{code}",
                {"include": [include]},
            )
            for code in codes
        ]
    return planned


def _document(planned: Planned, status: int, body: Any, today: str) -> dict[str, Any]:
    document = {"kind": "recorded", "recorded_on": today, "request": request_of(planned)}
    document["response"] = {"status": status, "body": body}
    return document


def request_of(planned: Planned) -> dict[str, Any]:
    """The request a planned recording's fixture holds, as the fixture server matches it."""

    request: dict[str, Any] = {
        "surface": planned.surface,
        "method": planned.method,
        "path": planned.path,
    }
    optional = {
        "params": planned.params,
        "headers": planned.headers,
        "body": planned.body,
        "form": planned.form,
        "ignored": planned.ignored,
    }
    return request | {key: value for key, value in optional.items() if value}


class Recorder:
    """Records the planned requests through `fetch` and checks the result."""

    def __init__(self, manifest: dict[str, Any], fetch: Fetch, today: str) -> None:
        self.manifest, self.fetch, self.today = manifest, fetch, today
        self.rules = ConceptRules.from_manifest(manifest["evs"]["concepts"])
        self.problems: list[str] = []

    def record(self, planned: Iterable[Planned]) -> dict[str, dict[str, Any]]:
        """The fixture documents by file; RecordingError if anything does not hold."""

        self._check_pin()
        documents = {each.file: document for each in planned if (document := self._one(each))}
        self._check_samples(documents)
        derived = self._derive(documents)
        if self.problems:
            raise RecordingError(self.problems)
        return documents | derived

    def _fetch(
        self, label: str, surface: str, path: str, params: Params, **sent: Any
    ) -> tuple[int, Any] | None:
        """The live answer, or None with the failure among the problems; `sent` is the
        method, headers and body of a request that is not a plain GET."""

        try:
            return self.fetch(surface, path, params, **sent)
        except OSError as error:
            self.problems.append(f"{label}: {error}")
            return None

    def _derive(self, documents: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        derived = {}
        for entry in self.manifest["record"].get("derived", []):
            source = documents.get(entry["from"])
            if source is None:
                self.problems.append(f"{entry['fixture']}: {entry['from']} is not recorded")
                continue
            if (served := _served_release(source)) not in (None, self.version):
                self.problems.append(f"{entry['fixture']}: {entry['from']} serves {served}")
            derived[entry["fixture"]] = _derived(entry, source)
        return derived

    @property
    def version(self) -> str:
        """The pinned release as a payload names it: `26.09d` for `ncit_26.09d`."""

        return self.manifest["evs"]["release"].rpartition("_")[2]

    def _one(self, planned: Planned) -> dict[str, Any] | None:
        answer = self._fetch(
            planned.file, planned.surface, planned.path, planned.params, **planned.sent()
        )
        if answer is None:
            return None
        status, body = answer
        document = _document(planned, status, body, self.today)
        refused = self._recording_problem(planned, document)
        problems = [self._status_problem(planned, status), refused]
        self.problems += [f"{planned.file}: {problem}" for problem in problems if problem]
        # A concept recording the fixture server would refuse is withheld, so that the
        # samples are composed only from usable ones.
        return None if refused else document

    @staticmethod
    def _status_problem(planned: Planned, status: int) -> str | None:
        if status != planned.status:
            return f"answered {status}, where {planned.status} is expected"
        return None

    def _recording_problem(self, planned: Planned, document: dict[str, Any]) -> str | None:
        if Path(planned.file).parent.name != CONCEPTS:
            return None
        response = document["response"]
        return self.rules.recording_problem(
            document["request"], response["status"], response["body"]
        )

    def _check_pin(self) -> None:
        pinned = self.manifest["evs"]["release"]
        query = {"terminology": ["ncit"], "latest": ["true"], "tag": ["monthly"]}
        answer = self._fetch("the release query", "evs", "/api/v1/metadata/terminologies", query)
        if answer is None:
            return
        status, rows = answer
        live = (
            [row.get("terminologyVersion") for row in _objects(rows)]
            if status == HTTPStatus.OK
            else []
        )
        if live != [pinned]:
            self.problems.append(
                f"the live monthly release query answered {status} with {live}, the fixture set "
                f"is pinned to {pinned}: re-pinning is a re-recording under change control"
            )

    def _check_samples(self, documents: dict[str, dict[str, Any]]) -> None:
        recordings = _recordings(documents, self.rules)
        for sample in self.manifest["record"].get("samples", []):
            path, params = _split(sample)
            composed = self.rules.answer(path, params, lambda *key: recordings.get(key))
            answer = self._fetch(f"sample {sample}", "evs", path, params)
            if answer is None:
                continue
            batch = "list" in params
            live = (answer[0], _ordered(answer[1], batch=batch))
            if composed is None:
                self.problems.append(f"sample {sample}: the recordings cannot answer it")
            elif (composed.status, _ordered(composed.body, batch=batch)) != live:
                self.problems.append(f"sample {sample}: the composed answer differs from EVS's")


def _objects(rows: Any) -> list[dict[str, Any]]:
    """The objects of a list answer; nothing for any other answer."""

    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def _ordered(body: Any, *, batch: bool) -> Any:
    """A batch answer sorted by code, so that it compares without its order but with
    any duplicate."""

    if batch and isinstance(body, list):
        return sorted(body, key=lambda concept: concept["code"])
    return body


def reported_releases(payload: Any) -> set[str]:
    """Every release or version a payload reports for its content, at any depth, a SPARQL
    result's bound value included."""

    if isinstance(payload, list):
        return set().union(*map(reported_releases, payload))
    if not isinstance(payload, dict):
        return set()
    own = {str(bound(payload[key])) for key in RELEASE_FIELDS if key in payload}
    return own.union(*map(reported_releases, payload.values()))


def bound(value: Any) -> Any:
    """A value as it reads: a SPARQL result's binding ({ type, value }) by its value."""

    is_binding = isinstance(value, dict) and set(value) >= {"type", "value"}
    return value["value"] if is_binding else value


def _served_release(document: dict[str, Any]) -> str | None:
    body = document["response"]["body"]
    return body.get("version") if isinstance(body, dict) else None


def _derived(entry: dict[str, Any], source: dict[str, Any]) -> dict[str, Any]:
    """A crafted fixture: the request a requirement prescribes, a recording's answer."""

    path, params = _split(entry["path"])
    request: dict[str, Any] = {
        "surface": source["request"]["surface"],
        "method": entry.get("method", "GET"),
    }
    request.update({key: entry[key] for key in ("headers", "form") if key in entry})
    request["path"] = path
    if params:
        request["params"] = params
    if entry.get("ignored"):
        request["ignored"] = entry["ignored"]
    document = {
        "kind": "crafted",
        "requirement": entry["requirement"],
        "derived_from": entry["from"],
    }
    return document | {"request": request, "response": source["response"]}


def _recordings(
    documents: dict[str, dict[str, Any]], rules: ConceptRules
) -> dict[tuple[str, str], Recording]:
    """The concept recordings among the documents, as the fixture server would read them."""

    recordings = {}
    for file, document in documents.items():
        if Path(file).parent.name != CONCEPTS:
            continue
        request, response = document["request"], document["response"]
        covers = rules.keys(request["params"]["include"][0]) or frozenset()
        recordings[recording_key(request["path"])] = Recording(
            file, response["status"], response["body"], covers
        )
    return recordings


def stale(root: Path, manifest: dict[str, Any]) -> list[str]:
    """Files under the recorded surfaces, and derived fixtures, that the manifest does
    not produce."""

    planned = plan(manifest)
    produced = {each.file for each in planned}
    produced |= {entry["fixture"] for entry in manifest["record"].get("derived", [])}
    found = [*_recorded_files(root, {each.surface for each in planned}), *_derived_files(root)]
    return [
        f"{name}: not produced by {MANIFEST}; remove it or add it there"
        for path in found
        if (name := path.relative_to(root).as_posix()) not in produced
    ]


def _recorded_files(root: Path, surfaces: set[str]) -> list[Path]:
    return [
        path
        for surface in sorted(surfaces)
        for path in sorted((root / RECORDED / surface).rglob("*.json"))
    ]


def _derived_files(root: Path) -> list[Path]:
    return [
        path
        for path in sorted(root.glob("crafted/**/*.json"))
        if "derived_from" in json.loads(path.read_text(encoding="utf-8"))
    ]


def write(root: Path, documents: dict[str, dict[str, Any]]) -> None:
    for file, document in documents.items():
        path = root / file
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(document, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")


def live_fetch(bases: dict[str, str]) -> Fetch:
    """A fetch from the live services at the manifest's base URLs."""

    def fetch(
        surface: str,
        path: str,
        params: Params,
        method: str = "GET",
        headers: dict[str, str] | None = None,
        body: Any = None,
        form: dict[str, str] | None = None,
    ) -> tuple[int, Any]:
        query = f"?{urlencode(params, doseq=True)}" if params else ""
        # caDSR's API paths hold colons (NCIFormAPI.v2_0:NciFormApiRad).
        url = bases[surface] + quote(path, safe="/$:") + query
        sent = {"Accept": "application/json"} if headers is None else dict(headers)
        # A text body is sent as written (a SPARQL query), any other as JSON.
        data = body.encode() if isinstance(body, str) else json.dumps(body).encode()
        data = None if body is None else data
        if form is not None:
            data = urlencode(form).encode()
            sent["Content-Type"] = FORM_TYPE
        request = Request(url, data=data, headers=sent, method=method)  # noqa: S310 - https from the manifest
        try:
            with urlopen(request, timeout=TIMEOUT_SECONDS) as response:  # noqa: S310
                return response.status, _parse(response.read())
        except HTTPError as error:
            with error:
                return error.code, _parse(error.read())

    return fetch


def _parse(raw: bytes) -> Any:
    text = raw.decode("utf-8")
    try:
        return json.loads(text)
    except ValueError:
        return text


def main(arguments: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Record the fixture set from the live services.")
    parser.add_argument("--fixtures", type=Path, default=FIXTURES, help="the fixture directory")
    options = parser.parse_args(arguments)
    manifest = yaml.safe_load((options.fixtures / MANIFEST).read_text(encoding="utf-8"))
    planned = plan(manifest)
    today = datetime.now(UTC).date().isoformat()
    recorder = Recorder(manifest, live_fetch(manifest["surfaces"]), today)
    try:
        documents = recorder.record(planned)
        if problems := stale(options.fixtures, manifest):
            raise RecordingError(problems)
    except RecordingError as error:
        sys.stderr.write(f"Nothing was written:\n{error}\n")
        return 1
    write(options.fixtures, documents)
    sys.stdout.write(f"Recorded {len(documents)} fixtures on {today}.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
