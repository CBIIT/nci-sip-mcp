"""The register of upstream request forms, generated from the manifest.

    pdm run acceptance-register [--fixtures DIR]

The register holds an initial form for every platform operation the tools rest
on: from the published API documentation and what was verified live where the
operation exists, and a draft naming its requirement where it does not. The manifest
holds each form with its operation and rationale; this writes the readable register
from it, one view for each team (EVS, caDSR, the Shared SI Service) and one for all
(`acceptance/request-forms/`). A self-test fails when the files are not current.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from nci_sip_acceptance.fixture_server import MANIFEST, SCENARIOS, SETTINGS, fixture_documents
from nci_sip_acceptance.record import DISCOVERY, FIXTURES, reported_releases

REGISTER = FIXTURES.parent / "request-forms"
EVS_SURFACES, CADSR_SURFACES = {"evs", "evs-fhir"}, {"cadsr", "cadsr-ftp"}
SSIS_SURFACES = {"ssis", "ssis-sparql"}
VIEWS = {
    "evs": ("the EVS team", EVS_SURFACES),
    "cadsr": ("the caDSR team", CADSR_SURFACES),
    "ssis": ("the Shared SI team", SSIS_SURFACES),
    "all": ("every team", None),
}
FALLBACK_HEADER = (
    "| Operation | Prescribed form (crafted) | Served form (recorded) | Release reported |"
)
REQUESTS_HEADER = "| Operation | Request | Expected | Made | Rationale | Fixture |"
SCENARIO_HEADER = "| Operation | Request | Expected | Rationale | Fixture |"
SHARED_HEADER = "| Operation | Request | Expected | Fixture |"
INTRODUCTION = """\
The upstream requests the acceptance suite's fixtures answer, each with the platform operation it
serves (its `OP-` id, or the requirement where the operation is missing) and why it has this
form. They are initial versions, from the published API documentation and live checks
where the operation exists and a draft where it does not, for the EVS, caDSR and Shared SI teams
to refine as needed, each change with the approval of the branch chief or a delegate.

Two kinds of request are answered whatever their form: EVS concept requests, by rules over one
recording per concept (below), and requests whose parameters the service is shown to ignore.
"""
RULES = """\
## Concept requests answered by rule

One recording per concept answers each projection (`include`) and relation list of that concept
it covers, and a batch is composed from the recordings of the concepts it names
(`acceptance/src/nci_sip_acceptance/concepts.py`).

| Operations | Form |
|---|---|
| OP-E05 | `GET /api/v1/concept/{terminology}_{release}/{code}?include=…` |
| OP-E07 | `GET /api/v1/concept/{terminology}_{release}?list=…&include=…`, ≤ 1,000 codes |
| OP-E10, E11, E14 to E17, E20 | `GET /api/v1/concept/{terminology}_{release}/{code}/{relation}` |
"""
SCENARIOS_INTRODUCTION = """\
## Scenarios

Each scenario provokes one case; its fixtures answer before the ordinary
ones while a test selects it. A recorded fixture is what the service answers today. A crafted one
stands in for a case the service does not produce on demand, under the requirement it names, and
answers the ordinary forms above.
"""
CADSR = """\
## caDSR: no registry release, and JSON only when asked for

caDSR publishes no registry release (C-1, A3.8.1): every caDSR form below is served without one,
the path the inventory names for it (OP-C08) answers 404, and the export's date is the registry's
only content state (A3.8.2). The forms the inventory names with `registryRelease` are crafted in
`cadsr/with-registry-release`. Every caDSR request names `Accept: application/json`, which the
contracts prescribe (M3.2): without it the API answers HTTP 200 with HTML, recorded for two paths,
and the fixture naming the most headers a request carries answers it. A refusal of the arguments
and an unknown data element both come back as HTTP 200, with `apiResponse` saying so (X-15).

harmonize_data_dictionary's form is prescribed: each column goes to CDE Match as one entity, its
name and its description as `userTip`, and its sample values go to vmMatch only. CDE Match is
matched on its whole body, so a form left to each server would leave its answers unanswerable.
"""
SSIS = """\
## Shared SI: graph identities, and the query text

The Shared SI Service names no release (S-1, S-2): the NCIt and caDSR graphs each carry an
untyped `dc:date`, in two formats, and only NCIt an `owl:versionInfo`; the identity query below
reads them (A3.7.1). For the SPARQL endpoint the suite prescribes the query text: each query
below is matched with runs of whitespace collapsed, sent as a form-encoded POST (a direct POST
of the query is refused) asking for `application/sparql-results+json`. Bounded identities ask for
three rows; content asks for at most the shared remaining maximum + 1, to detect a cut. The
bounded variants reuse recorded answers as explicitly crafted fixtures. Public-ID projections
are crafted in memory: element queries group publicId and value queries select distinct
publicId/value/concept before LIMIT. Historical bounded captures are not complete current
distinct answers; cap scenarios use 1001 synthetic distinct IDs. Original queries stay below
as recorded evidence. Teams may propose changes
to any form. The façade answers HTML unless `Accept: application/json` is sent, and a
missing argument with HTTP 200 (X-15).
"""
FALLBACK = """\
## Operations without a pinned form upstream

These are served unpinned today. The verified fallback is approved for this server: the tool
calls the unpinned form, compares the release the payload reports with the one requested, and
fails closed on a difference. NCI approves each fallback from this list. A
mapset that reports a version of its own, not an NCIt release, cannot be verified that way: it is a
content state of its own, named in provenance and not presented as release-verified.
"""


def _split_form(entry: dict[str, Any]) -> str:
    """A request as the manifest writes it, safe in a table cell: its method, surface and
    path, and the headers and body it is made with where the manifest names them."""

    form = f"`{entry.get('method', 'GET')} {entry['surface']} {entry['path']}`"
    if "headers" in entry:
        named = ", ".join(f"`{name}: {value}`" for name, value in entry["headers"].items())
        form += f" with {named}" if named else " with no header"
    return _cell(form + _payload(entry))


def _payload(entry: dict[str, Any]) -> str:
    """What a request carries besides its headers: a JSON body, shown here, or a form's
    fields or a text body, whose texts the register lists below."""

    if "form" in entry:
        return f", form {', '.join(f'`{name}`' for name in entry['form'])} (below)"
    if isinstance(entry.get("body"), str):
        return ", body as written (below)"
    return f", body `{json.dumps(entry['body'])}`" if "body" in entry else ""


def _expected(entry: dict[str, Any]) -> str:
    ignored = sorted(entry.get("ignored", {}))
    if ignored == ["*"]:
        return f"{entry.get('status', 200)}; every parameter ignored"
    return f"{entry.get('status', 200)}{'; ignores ' + ', '.join(ignored) if ignored else ''}"


def _cell(text: str) -> str:
    return " ".join(str(text).split()).replace("|", "\\|")


def _shown(entry: dict[str, Any], surfaces: set[str] | None) -> bool:
    return surfaces is None or entry["surface"] in surfaces


def _row(entry: dict[str, Any], *cells: str) -> str:
    """A request as a table row: its operation, form and expected answer, then `cells`."""

    middle = "".join(f" | {_cell(cell)}" for cell in cells)
    return (
        f"| {entry['operation']} | {_split_form(entry)} | {_expected(entry)}{middle} "
        f"| `{entry['fixture']}` |"
    )


def _in_scenario(entry: dict[str, Any]) -> bool:
    return entry["fixture"].startswith(f"{SCENARIOS}/")


def _request_rows(manifest: dict[str, Any], surfaces: set[str] | None) -> list[str]:
    """The ordinary requests, each crafted form a requirement prescribes after the recording
    whose answer it carries."""

    derived = {entry["from"]: entry for entry in manifest["record"].get("derived", [])}
    rows = []
    for entry in manifest["record"].get("requests", []):
        if _in_scenario(entry) or not _shown(entry, surfaces):
            continue
        rows.append(_row(entry, "recorded", entry["rationale"]))
        if pinned := derived.get(entry["fixture"]):
            crafted = pinned | {"surface": entry["surface"]}
            rows.append(_row(crafted, f"crafted for {pinned['requirement']}", pinned["rationale"]))
    return rows


def _fallback_rows(manifest: dict[str, Any], root: Path, surfaces: set[str] | None) -> list[str]:
    """The operations served unpinned: each with the pinned form a requirement prescribes,
    where one is crafted, and the release the recorded payload reports."""

    requests = {entry["fixture"]: entry for entry in manifest["record"].get("requests", [])}
    prescribed = {entry["from"]: entry for entry in manifest["record"].get("derived", [])}
    releases = {manifest["evs"]["release"], *_listed_releases(root)}
    return [
        _fallback_row(entry, prescribed.get(entry["fixture"]), root)
        for entry in requests.values()
        if _unpinned(entry, releases) and _shown(entry, surfaces)
    ]


def _listed_releases(root: Path) -> set[str]:
    """Each release the recorded terminology listing names, as a path names it
    (`go_2026-07-26`), so that a request pinned to another terminology counts as pinned."""

    listing = root / "recorded/evs/terminologies.json"
    rows = json.loads(listing.read_text(encoding="utf-8"))["response"]["body"]
    return {f"{row['terminology']}_{row['version']}" for row in rows}


def _fallback_row(entry: dict[str, Any], pinned: dict[str, Any] | None, root: Path) -> str:
    pinned_form = _split_form(pinned | {"surface": entry["surface"]}) if pinned else "—"
    document = json.loads((root / entry["fixture"]).read_text(encoding="utf-8"))
    releases = ", ".join(sorted(reported_releases(document["response"]["body"]))) or "none"
    return f"| {entry['operation']} | {pinned_form} | {_split_form(entry)} | {releases} |"


def _unpinned(entry: dict[str, Any], releases: set[str]) -> bool:
    """A recorded request naming no release that is not release discovery."""

    recorded = entry["fixture"].startswith("recorded/") and entry["surface"] in EVS_SURFACES
    named = any(release in entry["path"] for release in releases)
    return recorded and entry["fixture"] not in DISCOVERY and not named


def _table(header: str, rows: list[str]) -> list[str]:
    if not rows:
        return ["None yet.", ""]
    columns = header.count("|") - 1
    return [header, "|" + "---|" * columns, *rows, ""]


def _scenario_documents(root: Path) -> dict[str, list[dict[str, Any]]]:
    """The fixtures of each scenario of the set, recorded or crafted, without its settings, in
    the order of their names."""

    found: dict[str, list[dict[str, Any]]] = {}
    for name, document in sorted(fixture_documents(root).items()):
        path = PurePosixPath(name)
        if path.parts[0] != SCENARIOS:
            continue
        documents = found.setdefault("/".join(path.parts[1:3]), [])
        if path.name != SETTINGS:
            documents.append(document)
    return found


def _made(documents: list[dict[str, Any]]) -> str:
    """Whether a scenario's fixtures are recorded, crafted (under which requirement), or both."""

    crafted = Counter(item["requirement"] for item in documents if item["kind"] == "crafted")
    parts = ["Recorded."] if sum(crafted.values()) < len(documents) else []
    parts += [
        f"Crafted, {count:,} fixture{'s' if count > 1 else ''}, for {requirement}."
        for requirement, count in crafted.items()
    ]
    return " ".join(parts)


def _scenario(
    name: str, provokes: str, documents: list[dict[str, Any]], requests: list[dict[str, Any]]
) -> list[str]:
    """One scenario: what it provokes, how it is made, and the requests recorded for it."""

    entries = [entry for entry in requests if entry["fixture"].startswith(f"{SCENARIOS}/{name}/")]
    return [f"### `{name}`", "", f"{provokes}. {_made(documents)}", "", *_scenario_table(entries)]


def _scenario_table(entries: list[dict[str, Any]]) -> list[str]:
    """The requests recorded for a scenario, a rationale they all share stated once."""

    rationales = {entry["rationale"] for entry in entries}
    if len(entries) > 1 and len(rationales) == 1:
        rows = [_row(entry) for entry in entries]
        return [f"Each request: {_cell(rationales.pop())}", "", *_table(SHARED_HEADER, rows)]
    rows = [_row(entry, entry["rationale"]) for entry in entries]
    return _table(SCENARIO_HEADER, rows) if rows else []


def _scenario_names(manifest: dict[str, Any], found: dict[str, Any]) -> list[str]:
    """The scenarios in the manifest's order, each described there and in the fixture set."""

    described = list(manifest.get("scenarios", {}))
    if differing := sorted(set(found).symmetric_difference(described)):
        raise ValueError(
            f"{MANIFEST} describes the scenarios of the fixture set and no others: "
            + ", ".join(differing)
        )
    return described


def _touches(documents: list[dict[str, Any]], surfaces: set[str] | None) -> bool:
    return surfaces is None or any(item["request"]["surface"] in surfaces for item in documents)


def _scenarios(manifest: dict[str, Any], root: Path, surfaces: set[str] | None) -> list[str]:
    requests = [entry for entry in manifest["record"].get("requests", []) if _in_scenario(entry)]
    lines = []
    found = _scenario_documents(root)
    for name in _scenario_names(manifest, found):
        documents = found[name]
        if _touches(documents, surfaces):
            lines += _scenario(name, manifest["scenarios"][name], documents, requests)
    return lines or ["None yet.", ""]


def _texts(entry: dict[str, Any]) -> dict[str, str]:
    """The texts a request carries: each field of its form, or its body where that is text."""

    body = entry.get("body")
    return entry.get("form", {}) | ({"body": body} if isinstance(body, str) else {})


def _queries(manifest: dict[str, Any], surfaces: set[str] | None) -> list[str]:
    """The text of each request that carries one (the SPARQL queries), by fixture."""

    lines = []
    requests = {entry["fixture"]: entry for entry in manifest["record"].get("requests", [])}
    derived = [requests[entry["from"]] | entry for entry in manifest["record"].get("derived", [])]
    for entry in [*requests.values(), *derived]:
        if not _shown(entry, surfaces):
            continue
        for name, text in _texts(entry).items():
            lines += [f"### `{entry['fixture']}`: `{name}`", "", "```sparql", text, "```", ""]
    return ["## Query texts", "", *lines] if lines else []


def _release_sections(manifest: dict[str, Any], root: Path, surfaces: set[str] | None) -> list[str]:
    """What a view asks of its team on releases: the EVS operations served unpinned, caDSR's
    want of a registry release, and the Shared SI graphs' identities."""

    lines = []
    if surfaces is None or surfaces & EVS_SURFACES:
        lines += [FALLBACK, *_table(FALLBACK_HEADER, _fallback_rows(manifest, root, surfaces))]
    if surfaces is None or surfaces & CADSR_SURFACES:
        lines.append(CADSR)
    if surfaces is None or surfaces & SSIS_SURFACES:
        lines.append(SSIS)
    return lines


def render(root: Path = FIXTURES) -> dict[str, str]:
    """Each view of the register, by file name."""

    manifest = yaml.safe_load((root / MANIFEST).read_text(encoding="utf-8"))
    views = {}
    for name, (audience, surfaces) in VIEWS.items():
        lines = [
            f"# Upstream request forms, for {audience}",
            "",
            f"Generated from `acceptance/fixtures/{MANIFEST}` by `pdm run acceptance-register`;"
            " do not edit by hand.",
            "",
            INTRODUCTION,
            *_release_sections(manifest, root, surfaces),
            "## Requests",
            "",
            *_table(REQUESTS_HEADER, _request_rows(manifest, surfaces)),
            *_queries(manifest, surfaces),
        ]
        if surfaces is None or "evs" in surfaces:
            lines.append(RULES)
        lines += [SCENARIOS_INTRODUCTION, *_scenarios(manifest, root, surfaces)]
        views[f"{name}.md"] = "\n".join(lines).rstrip() + "\n"
    return views


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write the register of upstream request forms.")
    parser.add_argument("--fixtures", type=Path, default=FIXTURES, help="the fixture directory")
    options = parser.parse_args(arguments)
    target = options.fixtures.parent / REGISTER.name
    target.mkdir(exist_ok=True)
    for name, text in render(options.fixtures).items():
        (target / name).write_text(text, encoding="utf-8")
    sys.stdout.write(f"Wrote the register to {target}.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
