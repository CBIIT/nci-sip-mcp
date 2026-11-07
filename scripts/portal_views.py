"""Server-rendered local evidence pages; no raw report content or client scripting."""

from __future__ import annotations

from html import escape
from typing import Any, Literal

from scripts.evidence_benchmark import comparable
from scripts.site_assets import STYLESHEETS, FooterLink, load_footer

from nci_si_acceptance.environments import EnvironmentRecord

# Read once at start-up: a missing or malformed table stops the portal rather than render an
# empty footer.
FOOTER = load_footer()


def _footer_links(links: tuple[FooterLink, ...]) -> str:
    return "".join(
        f'<li><a href="{text(link.url)}">{text(link.label)}</a></li>\n' for link in links
    )


def text(value: Any) -> str:
    return escape("unknown" if value is None else str(value), quote=True)


def _navigation(section: str | None) -> str:
    links = []
    for path, label in (
        ("/", "Results"),
        ("/jobs", "Run checks"),
        ("/configuration", "Configuration"),
        ("/help", "Help &amp; guide"),
    ):
        current = ' aria-current="true"' if path == section else ""
        links.append(f'<a href="{path}"{current}>{label}</a>')
    return '<nav aria-label="Main">' + "".join(links) + "</nav>"


def _stylesheets() -> str:
    return "".join(f'<link rel="stylesheet" href="/assets/{name}">' for name in STYLESHEETS)


def page(
    title: str,
    content: str,
    *,
    section: Literal["/", "/jobs", "/configuration", "/help"] | None = None,
) -> str:
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{text(title)} · NCI SI local validation</title>{_stylesheets()}</head>
<body id="top"><a class="usa-skipnav skip" href="#main">Skip to content</a>
<header class="topbar"><div class="shell"><a class="brand" href="/">NCI SI · Validation</a>
{_navigation(section)}</div>
</header><main id="main" class="shell"><p class="eyebrow">Local evidence workspace</p>
<h1>{text(title)}</h1><p class="notice">No login is required locally.
UAT/PROD administration is disabled pending platform integration.</p>
{content}</main><footer class="nci-footer"><div class="nci-footer__inner">
<div class="nci-footer__main"><div class="nci-footer__identity">
<strong>National Cancer Institute</strong> <span>at the National Institutes of Health</span>
<p>Semantic Infrastructure · Local validation<br>
<a href="/help#provenance">Understand the evidence</a></p></div>
<div class="nci-footer__contact"><h2>Contact us</h2>
<a href="{text(FOOTER.contact_url)}">Contact project maintainers</a>
<nav aria-label="Government agencies"><ul>
{_footer_links(FOOTER.agencies)}</ul></nav></div></div>
<div class="nci-footer__bottom"><nav class="nci-footer__policies" aria-label="Policies"><ul>
{_footer_links(FOOTER.policies)}</ul></nav><a href="#top">Back to top ↑</a></div>
</div></footer></body></html>"""


def table(title: str, headers: list[str], rows: list[list[Any]]) -> str:
    heading = "".join(f'<th scope="col">{text(value)}</th>' for value in headers)
    body = "".join("<tr>" + "".join(f"<td>{text(v)}</td>" for v in row) + "</tr>" for row in rows)
    return (
        f'<div class="scroll" tabindex="0" role="region" aria-label="{text(title)}">'
        f"<table><caption>{text(title)}</caption><thead><tr>{heading}</tr></thead>"
        f"<tbody>{body}</tbody></table></div>"
    )


def _run_link(row: dict[str, Any] | None, absent: str) -> str:
    if row is None:
        return absent
    return (
        f'<a class="run-link" href="/runs/{text(row["run_id"])}">View run {row["sequence"]}</a> '
        f'<span class="badge {text(row["state"])}">{text(row["state"].capitalize())}</span>'
        f'<span class="meta">{text(row["kind"].capitalize())} · '
        f"Mode: {text(row.get('mode'))}</span>"
    )


def history_page(rows: list[dict[str, Any]]) -> str:
    latest = rows[0] if rows else None
    complete = next((row for row in rows if row["inventory_complete"]), None)
    body = "<p>Review recorded acceptance results and benchmark measurements. "
    body += (
        '<a href="/help#getting-started">Import your first report or learn the workflow</a>.</p>'
    )
    body += '<div class="cards"><section class="card"><h2>Latest attempt</h2><p>'
    body += _run_link(latest, "No recorded attempt") + "</p></section>"
    body += '<section class="card"><h2>Latest complete evidence</h2><p>'
    body += _run_link(complete, "No complete evidence") + "</p></section></div>"
    body += "<p>Complete describes recorded inventory, not a PASS verdict or live-service proof. "
    body += "Order is local import sequence, not a report-supplied clock. "
    body += (
        'Retention bounds this history. <a href="/help#run-status">How run status works</a>.</p>'
    )
    body += '<h2>Retained attempts</h2><ol class="run-list">'
    body += "".join(f"<li>{_run_link(row, '')}</li>" for row in rows)
    return page("Validation history", body + "</ol>" + _comparison_form(rows), section="/")


def _comparison_form(rows: list[dict[str, Any]]) -> str:
    benchmarks = [row for row in rows if row["kind"] == "benchmark"]
    if not benchmarks:
        return ""
    options = "".join(
        f'<option value="{text(row["run_id"])}">Sequence {row["sequence"]}: '
        f"{text(row['state'])} ({text(row.get('mode'))})</option>"
        for row in benchmarks
    )
    return (
        '<h2>Compare benchmarks</h2><p><a href="/help#benchmarks">'
        "When is comparison meaningful?</a>"
        '</p><form action="/compare" method="get">'
        f'<label>First run <select name="left">{options}</select></label>'
        f'<label>Second run <select name="right">{options}</select></label>'
        "<button>Compare recorded conditions</button></form>"
    )


# The verdicts that gates which could not run decide (#265): an earlier harness recorded PASS,
# the current one INCOMPLETE.
GATES_UNRUN_LABELS = {
    "PASS": "PASS (gates not run; recorded by an earlier harness)",
    "INCOMPLETE": "INCOMPLETE (gates not run)",
}
# A PASS stored before the projection kept the gate status: whether the gates ran is unknown.
GATE_STATUS_UNRECORDED = "PASS (gate status not recorded)"


def _verdict(row: dict[str, Any]) -> str:
    """A tool's native verdict, saying so when gates that could not run decide it."""

    outcome: str = row["outcome"]
    if "gates_unrun" not in row:
        # A projection stored before the gate status was projected cannot tell.
        return GATE_STATUS_UNRECORDED if outcome == "PASS" else outcome
    if row["gates_unrun"] and outcome in GATES_UNRUN_LABELS:
        return GATES_UNRUN_LABELS[outcome]
    return outcome


def _acceptance(evidence: dict[str, Any], tool: str, story: str) -> str:
    cases = evidence["cases"]
    selected = _filter_cases(cases, tool, story)
    form = f'''<form method="get"><label>Tool <input name="tool" value="{text(tool)}"></label>
<label>Story <input name="story" value="{text(story)}"></label><button>Filter</button></form>'''
    body = (
        f'<h2>Acceptance cases</h2>{form}<p role="status">{len(selected)} of {len(cases)} cases</p>'
    )
    body += (
        '<p class="help-link"><a href="/help#acceptance">Understand verdicts and filters</a></p>'
    )
    tools = evidence["tools"]
    if evidence["inventory_complete"]:
        body += table(
            "Tool verdicts",
            ["Tool", "Native verdict", "Gate-only failure"],
            [[name, _verdict(row), row["gates_only"]] for name, row in tools.items()],
        )
    else:
        absent = "No report was produced. " if tools is None else ""
        reason = evidence.get("completion_problem") or (
            "selected cases without an outcome"
            if evidence["missing"]
            else f"run state: {evidence['state']}"
        )
        body += (
            f"<p>{absent}Incomplete run; selected cases without an outcome: "
            f"{len(evidence['missing'])}; no verdict shown. "
            f"{text(reason)}.</p>"
        )
    body += table(
        "Acceptance cases",
        ["Case ID", "Tool", "Story", "Expected", "Recorded outcome", "Gate"],
        [
            [r["id"], r["tool"], r["story"], r["expected"], r["outcome"], r["gate"]]
            for r in selected
        ],
    )
    return body


def _filter_cases(cases: list[dict[str, Any]], tool: str, story: str) -> list[dict[str, Any]]:
    return [
        row
        for row in cases
        if (not tool or row["tool"] == tool) and (not story or row["story"] == story)
    ]


def _benchmark(evidence: dict[str, Any]) -> str:
    body = (
        "<h2>Benchmark measurements</h2><p>Low sample counts do not establish an SLO or capacity. "
    )
    body += (
        "Fixture timing does not establish live-service performance. Cold/warm definitions belong "
    )
    body += "to the recorded transport; a new HTTP session does not prove a cold server.</p>"
    body += (
        '<p><a href="/help#benchmarks">Reading latency, errors and comparison conditions</a></p>'
    )
    rows = []
    labels = evidence.get("phase_labels", {"cold": "cold", "warm": "warm"})
    for case in evidence["cases"] or []:
        for phase, label in labels.items():
            data = case[phase]
            rows.append(
                [
                    case["tool"],
                    label,
                    len(data["samples"]),
                    data["errors"],
                    _latency(data["summary"].get("p50Ms")),
                    _latency(data["summary"].get("p95Ms")),
                ]
            )
    body += table(
        "Benchmark measurements", ["Tool", "Phase", "Samples", "Errors", "p50 ms", "p95 ms"], rows
    )
    body += "<h3>Comparison fingerprint</h3>"
    return body + table(
        "Comparison fingerprint",
        ["Dimension", "Digest (unknown blocks comparison)"],
        [[key, evidence["fingerprint"][key]] for key in sorted(evidence["fingerprint"])],
    )


def _latency(value: float | None) -> str:
    return "unknown" if value is None else f"{value:.2f}"


def _environment_table(environment: EnvironmentRecord) -> str:
    rows = [
        ["name", environment["name"]],
        *[
            [name, "disabled" if value is None else value]
            for name, value in environment["origins"].items()
        ],
        ["policy_sha256", environment["policy_sha256"]],
    ]
    return "<p>Recorded configuration; not access approval.</p>" + table(
        "Live environment", ["Field", "Recorded value"], rows
    )


def run_page(record: dict[str, Any], *, tool: str = "", story: str = "") -> str:
    evidence = record["evidence"]
    body = "<p>Origin unverified: imported locally. Checksums bind bytes; they do not authenticate "
    body += "the runner. Server identity not independently verified.</p>"
    body += '<p><a href="/help#provenance">What does this provenance establish?</a></p>'
    body += table(
        "Run provenance",
        ["Field", "Recorded value"],
        [
            [label, evidence.get(name)]
            for name, label in {
                "kind": "Evidence type",
                "state": "Run state",
                "mode": "Mode",
                "transport": "Transport",
                "inventory_complete": "Inventory complete",
                "runner_commit": "Runner source commit",
                "server_commit": "Reported server source commit",
                "started_at": "Started at",
                "finished_at": "Finished at",
            }.items()
        ],
    )
    if "environment" in evidence:
        body += _environment_table(evidence["environment"])
    body += f"<p>Local sequence {record['sequence']}; bundle SHA-256 "
    body += f"<code>{text(record['checksum'])}</code>.</p>"
    if evidence["state"] == "unverified":
        body += "<p>Original inventory unavailable. "
        body += "Legacy bytes are retained without interpreted outcomes.</p>"
    elif evidence["kind"] == "acceptance":
        body += _acceptance(evidence, tool, story)
    else:
        body += _benchmark(evidence)
    return page("Run " + evidence["run_id"], body, section="/")


def comparison_page(left: dict[str, Any], right: dict[str, Any]) -> str:
    evidence = [left["evidence"], right["evidence"]]
    if any(row["kind"] != "benchmark" or row["state"] == "unverified" for row in evidence):
        return page(
            "Benchmark comparison",
            "<p>Comparison unavailable: verified benchmark format required.</p>",
            section="/",
        )
    ready, reasons = comparable(*evidence)
    if not ready:
        return page(
            "Benchmark comparison",
            "<p>Comparison blocked: "
            + text(", ".join(reasons))
            + "</p><p>These recorded conditions are unknown, different or incomplete. "
            '<a href="/help#benchmarks">How to interpret comparison requirements</a>.</p>',
            section="/",
        )
    body = "<p>Recorded fingerprints match. Imported origin and server identity remain unverified. "
    body += "These descriptive measurements establish no SLO or capacity claim.</p>"
    for row in evidence:
        body += "<h2>Run " + text(row["run_id"]) + "</h2>" + _benchmark(row)
    return page("Benchmark comparison", body, section="/")
