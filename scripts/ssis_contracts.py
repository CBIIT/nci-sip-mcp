"""Check every registered Shared SI query anonymously; structural drift exits nonzero."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

import yaml

from nci_sip_mcp.config import Settings
from nci_sip_mcp.errors import PlatformError
from nci_sip_mcp.http_client import HttpClient, UpstreamError
from nci_sip_mcp.seam import NCIT_NAMESPACE
from nci_sip_mcp.ssis import (
    CADSR_GRAPH,
    NCIT_GRAPH,
    STATUS_FIELDS,
    SSISClient,
    _valid_date,
    graph_roles,
    graph_statuses,
)
from nci_sip_mcp.traffic_identity import product_user_agent
from nci_sip_mcp.validation import NCIT_CODE_FORM

ROOT = Path(__file__).resolve().parents[1]
REGISTER = ROOT / "docs/upstream/ssis-queries.yaml"


class CapturedRequestError(Exception):
    """Stop request planning before transport or the runtime content parser."""

    def __init__(self, path: str, fields: dict[str, str], accept: str) -> None:
        self.path, self.fields, self.accept = path, fields, accept


class Capture:
    """Obtain the real client's request without making an upstream call."""

    def post_form(self, path: str, fields: dict[str, str], *, accept: str) -> Any:
        raise CapturedRequestError(path, fields, accept)


def request(entry: dict[str, Any]) -> CapturedRequestError:
    client: Any = SSISClient(Settings(), user_agent=product_user_agent())
    client.sparql_http = Capture()
    try:
        getattr(client, entry["operation"])(*entry["args"], **entry["kwargs"])
    except CapturedRequestError as captured:
        return captured
    raise ValueError("Registered operation issued no query")


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def term_value(key: str, term: Any) -> str:
    """Check consumed fields only; ignore unused datatype and xml:lang metadata."""
    require(isinstance(term, dict), f"{key}: term must be an object")
    require(term.get("type") in ("uri", "literal", "typed-literal"), f"{key}: invalid term type")
    value = term.get("value")
    if key in STATUS_FIELDS:
        require(term.get("type") == "literal", f"{key}: expected aggregate literal")
    require(isinstance(value, str), f"{key}: term value must be a string")
    if key in ("concept", "graph"):
        require(
            term["type"] == "uri" and bool(re.fullmatch(r"https?://\S+", value)),
            f"{key}: expected absolute URI",
        )
    return value


def semantics(values: dict[str, str]) -> None:
    if "id" in values:
        require(bool(re.fullmatch(r"[1-9][0-9]*", values["id"])), "id: invalid public identifier")
    if "version" in values:
        require(bool(values["version"].strip()), "version: must not be blank")
    if "graph" in values:
        require(values["graph"] in (NCIT_GRAPH, CADSR_GRAPH), "graph: unexpected identity")
    if "date" in values:
        require(_valid_date(values["date"]), "date: invalid graph date")


def content_semantics(values: dict[str, str]) -> None:
    for field in STATUS_FIELDS:
        if field in values:
            graph_statuses(values[field])
    if "roles" in values:
        graph_roles(values["roles"])
    if "name" in values:
        require(bool(values["name"].strip()), "name: must not be blank")
    if "concept" in values:
        uri = values["concept"]
        require(
            uri.startswith(NCIT_NAMESPACE)
            and bool(re.fullmatch(NCIT_CODE_FORM, uri.removeprefix(NCIT_NAMESPACE))),
            "concept: expected NCIt concept URI",
        )


def binding(row: Any, entry: dict[str, Any]) -> None:
    require(isinstance(row, dict), "binding must be an object")
    require(set(entry["required"]) <= row.keys(), "binding missing required variables")
    require(row.keys() <= set(entry["selected"]), "binding has unselected variables")
    values = {key: term_value(key, term) for key, term in row.items()}
    semantics(values)
    if entry["id"] != "graph-identities":
        content_semantics(values)


def validate(payload: Any, entry: dict[str, Any]) -> None:
    require(isinstance(payload, dict), "response must be an object")
    head = payload.get("head")
    selected = head.get("vars") if isinstance(head, dict) else None
    require(
        selected == entry["selected"], "head.vars must be exactly: " + ", ".join(entry["selected"])
    )
    results = payload.get("results")
    rows = results.get("bindings") if isinstance(results, dict) else None
    if not isinstance(rows, list):
        raise ValueError("results.bindings must be a list")
    require(len(rows) <= entry["limit"], "bindings exceed requested LIMIT")
    for row in rows:
        binding(row, entry)
    if entry["id"] == "graph-identities":
        require(
            {NCIT_GRAPH, CADSR_GRAPH} <= {row["graph"]["value"] for row in rows},
            "graph: required identity missing",
        )


def check(entry: dict[str, Any], http: HttpClient) -> dict[str, str]:
    result = {"id": entry["id"], "status": "pass"}
    try:
        planned = request(entry)
        payload = http.post_form(planned.path, planned.fields, accept=planned.accept)
        validate(payload, entry)
    except ValueError as exc:
        result.update(status="fail", error=str(exc))
    except UpstreamError as exc:
        # Upstream bodies/exception messages are not report content.
        result.update(status="fail", error=type(exc).__name__)
    except PlatformError as exc:
        if exc.code != "upstream_unavailable":
            raise
        result.update(status="fail", error=exc.code)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=Settings().ssis_sparql_url)
    args = parser.parse_args()
    register = yaml.safe_load(REGISTER.read_text())
    http = HttpClient(
        args.url,
        label="SSIS_SPARQL",
        timeout_seconds=30,
        max_attempts=1,
        max_response_bytes=10 * 1024 * 1024,
        retry_backoff_seconds=0,
        size_bound="ssis_response_bytes",
    )
    report = [check(entry, http) for entry in register["queries"]]
    print(json.dumps(report, indent=2))
    raise SystemExit(int(any(row["status"] == "fail" for row in report)))


if __name__ == "__main__":
    main()
