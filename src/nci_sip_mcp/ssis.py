"""Shared SI contract client; content release comparison belongs to #38's tools.

Only validated identifiers enter fixed SPARQL templates. The extra row is retained so
the caller can distinguish a complete answer from a cut; it does not establish a total.
Graph dates and versions are returned as supplied, never replaced by another surface.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from functools import partial
from typing import Any, NotRequired, TypedDict, cast
from urllib.parse import quote, unquote

from .config import Settings
from .errors import InputValidationError, PlatformError
from .http_client import HttpClient
from .results import GraphConceptUse
from .validation import (
    GRAPH_CONCEPT_ROLES,
    NCIT_CODE_FORM,
    GraphConceptRole,
    bounded,
    validate_identifier,
)

NCIT_GRAPH = "http://ncicb.nci.nih.gov/xml/owl/EVS/Thesaurus.rdf"
CADSR_GRAPH = "http://cbiit.nci.nih.gov/caDSR"
_GRAPHS = (NCIT_GRAPH, CADSR_GRAPH)
MAXIMUM = 1000
STATUS_FIELDS = ("workflowStatuses", "registrationStatuses")
_NCIT_NAMESPACE = "http://ncicb.nci.nih.gov/xml/owl/EVS/Thesaurus.owl#"
_ROLE_TUPLE_SIZE = 3
_PREFIXES = """PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX owl: <http://www.w3.org/2002/07/owl#>
PREFIX dc: <http://purl.org/dc/elements/1.1/>
PREFIX mdr: <http://www.iso.org/11179/MDR#>
PREFIX cadsr: <http://cbiit.nci.nih.gov/caDSR#>
PREFIX ncit: <http://ncicb.nci.nih.gov/xml/owl/EVS/Thesaurus.owl#>
"""
_IDENTITIES = f"""SELECT ?graph ?version ?date
WHERE {{
  VALUES ?graph {{ <{NCIT_GRAPH}> <{CADSR_GRAPH}> }}
  GRAPH ?graph {{
    ?ontology dc:date ?date .
    OPTIONAL {{ ?ontology owl:versionInfo ?version }}
  }}
}}
LIMIT {len(_GRAPHS) + 1}"""
_ELEMENTS = """SELECT ?id (MIN(?label) AS ?name)
  (GROUP_CONCAT(DISTINCT CONCAT(ENCODE_FOR_URI(?useRole),"|",ENCODE_FOR_URI(STR(?concept)),"|",COALESCE(ENCODE_FOR_URI(STR(?order)),""));separator=" ") AS ?roles)
  (GROUP_CONCAT(DISTINCT IF(BOUND(?workflowStatus),CONCAT("v",ENCODE_FOR_URI(STR(?workflowStatus))),"");separator=" ") AS ?workflowStatuses)
  (GROUP_CONCAT(DISTINCT IF(BOUND(?registrationStatus),CONCAT("v",ENCODE_FOR_URI(STR(?registrationStatus))),"");separator=" ") AS ?registrationStatuses)
WHERE {
  CONCEPT_CLAUSE
  GRAPH <CADSR_GRAPH> {
    VALUES ?membership { cadsr:main_concept cadsr:minor_concept }
    ?node ?membership ?concept .
    { ?element mdr:Object_Class ?node . BIND("objectClass" AS ?useRole) }
    UNION
    { ?element mdr:Property ?node . BIND("property" AS ?useRole) }
    UNION
    { ?value cadsr:has_concept ?node . ?element mdr:permitted_value ?value . BIND("valueMeaning" AS ?useRole) }
    OPTIONAL { ?node cadsr:display_order ?order }
    ?element cadsr:publicId ?id ;
      rdfs:label ?label .
    OPTIONAL { ?element mdr:administration_status ?workflowStatus }
    OPTIONAL { ?element mdr:registration_status ?registrationStatus }
  }
}
GROUP BY ?id
ORDER BY ?id
""".replace("CADSR_GRAPH", CADSR_GRAPH)  # noqa: E501 - exact live-probed SPARQL wire text
_VALUES = """SELECT DISTINCT ?id ?value ?concept
WHERE {
  CONCEPT_CLAUSE
  GRAPH <CADSR_GRAPH> {
    VALUES ?role { cadsr:main_concept cadsr:minor_concept }
    ?node ?role ?concept .
    ?pv cadsr:has_concept ?node ;
      mdr:value ?value .
    ?element mdr:permitted_value ?pv ;
      cadsr:publicId ?id .
  }
}
ORDER BY ?id ?value ?concept
""".replace("CADSR_GRAPH", CADSR_GRAPH)


class GraphIdentity(TypedDict):
    """Actual graph metadata, including its original date spelling and optional version."""

    graph: str
    date: str
    version: NotRequired[str]


def _decode(component: str) -> str:
    if re.search(r"%(?![0-9A-Fa-f]{2})", component):
        raise _malformed()
    try:
        return unquote(component, errors="strict")
    except UnicodeDecodeError as exc:
        raise _malformed() from exc


def graph_statuses(value: str) -> list[str]:
    """Decode unpaired sets without trimming text; v alone is an actual empty literal."""
    tokens = [token for token in value.split(" ") if token]
    if any(not token.startswith("v") for token in tokens):
        raise _malformed()
    return sorted({_decode(token[1:]) for token in tokens})


def _canonical_element(row: dict[str, str]) -> dict[str, str]:
    result = row | {"roles": _canonical_roles(row["roles"])}
    for field in STATUS_FIELDS:
        if field in row:
            result[field] = " ".join(
                "v" + quote(text, safe="") for text in graph_statuses(row[field])
            )
    return result


def _role_tuple(token: str) -> GraphConceptUse:
    parts = token.split("|")
    if len(parts) != _ROLE_TUPLE_SIZE:
        raise _malformed()
    role, concept, order = map(_decode, parts)
    code = concept.removeprefix(_NCIT_NAMESPACE)
    if role not in GRAPH_CONCEPT_ROLES or concept == code or not re.fullmatch(NCIT_CODE_FORM, code):
        raise _malformed()
    record: GraphConceptUse = {"role": cast("GraphConceptRole", role), "conceptCode": code}
    if parts[2]:
        if not re.fullmatch(r"[+-]?[0-9]+", order.strip()):
            raise _malformed()
        record["displayOrder"] = int(order.strip())
    return record


def graph_roles(value: str) -> list[GraphConceptUse]:
    """Decode complete tuples; normalize and deduplicate independently of aggregate order."""
    records = [_role_tuple(token) for token in value.split(" ")]
    unique = {(r["role"], r["conceptCode"], r.get("displayOrder")): r for r in records}
    return [
        unique[key]
        for key in sorted(unique, key=lambda k: (k[0], k[1], k[2] is not None, k[2] or 0))
    ]


def _canonical_roles(value: str) -> str:
    return " ".join(
        "|".join(
            (
                r["role"],
                quote(_NCIT_NAMESPACE + r["conceptCode"], safe=""),
                str(r["displayOrder"]) if "displayOrder" in r else "",
            )
        )
        for r in graph_roles(value)
    )


def _malformed() -> PlatformError:
    return PlatformError(
        "upstream_unavailable",
        "Shared SI returned incomplete or malformed results. Retry later.",
        surface="ssis",
    )


def _list(payload: Any, key: str, item_type: type) -> list[Any]:
    value = payload.get(key) if isinstance(payload, dict) else None
    if not isinstance(value, list) or any(not isinstance(row, item_type) for row in value):
        raise _malformed()
    return value


def _term(term: Any, key: str) -> str:
    if not isinstance(term, dict) or term.get("type") not in ("uri", "literal", "typed-literal"):
        raise _malformed()
    value = term.get("value")
    if not isinstance(value, str):
        raise _malformed()
    if key in STATUS_FIELDS and term["type"] != "literal":
        raise _malformed()
    return value


def _binding(row: dict[str, Any], required: tuple[str, ...]) -> dict[str, str]:
    if not all(key in row for key in required):
        raise _malformed()
    return {key: _term(value, key) for key, value in row.items()}


def _concept_clause(code: str, expand: bool) -> str:
    validate_identifier(code, NCIT_CODE_FORM, "conceptCode")
    if type(expand) is not bool:
        raise InputValidationError("expandDescendants must be boolean", "expandDescendants")
    if expand:
        return f"GRAPH <{NCIT_GRAPH}> {{ ?concept rdfs:subClassOf* ncit:{code} . }}"
    return f"VALUES ?concept {{ ncit:{code} }}"


def _valid_date(value: str) -> bool:
    try:
        date.fromisoformat(value)
    except ValueError:
        try:
            # The recorded English month spelling assumes the server's C/English locale.
            datetime.strptime(value, "%B %d, %Y")
        except ValueError:
            return False
    return True


def _identity(row: dict[str, str]) -> GraphIdentity:
    if row["graph"] not in _GRAPHS or not _valid_date(row["date"]):
        raise _malformed()
    identity: GraphIdentity = {"graph": row["graph"], "date": row["date"]}
    if "version" in row:
        if not row["version"].strip():
            raise _malformed()
        identity["version"] = row["version"]
    return identity


class SSISClient:
    """Bounded SPARQL operations used by the cross-domain tools.

    The graph version is input to #38's comparison with the effective release. This
    client neither compares it with EVS nor fabricates a registry release from a date.
    """

    def __init__(self, settings: Settings) -> None:
        transport = partial(
            HttpClient,
            timeout_seconds=settings.timeout_seconds,
            max_attempts=3,
            retry_backoff_seconds=0.25,
            max_response_bytes=10 * 1024 * 1024,
            size_bound="ssis_response_bytes",
        )
        self.sparql_http = transport(settings.ssis_sparql_url, label="SSIS_SPARQL")

    def _query(self, query: str, required: tuple[str, ...], row_limit: int) -> list[dict[str, str]]:
        payload = self.sparql_http.post_form(
            "/sparql", {"query": _PREFIXES + query}, accept="application/sparql-results+json"
        )
        results = payload.get("results") if isinstance(payload, dict) else None
        rows = _list(results, "bindings", dict)
        if len(rows) > row_limit:
            raise _malformed()
        return [_binding(row, required) for row in rows]

    def get_graph_identities(self) -> list[GraphIdentity]:
        """Return unchanged metadata; joins normalize dates and check release only for expansion."""
        rows = self._query(_IDENTITIES, ("graph", "date"), len(_GRAPHS) + 1)
        identities = [_identity(row) for row in rows]
        if sorted(row["graph"] for row in identities) != sorted(_GRAPHS):
            raise _malformed()
        return identities

    def find_data_elements(
        self, concept_code: str, *, expand_descendants: bool = False, maximum: int = MAXIMUM
    ) -> list[dict[str, str]]:
        maximum = bounded(maximum, MAXIMUM, "maximum")
        query = _ELEMENTS.replace(
            "CONCEPT_CLAUSE", _concept_clause(concept_code, expand_descendants)
        )
        rows = self._query(query + f"LIMIT {maximum + 1}", ("id", "name", "roles"), maximum + 1)
        return [_canonical_element(row) for row in rows]

    def find_permissible_values(
        self, concept_code: str, *, expand_descendants: bool = False, maximum: int = MAXIMUM
    ) -> list[dict[str, str]]:
        maximum = bounded(maximum, MAXIMUM, "maximum")
        query = _VALUES.replace("CONCEPT_CLAUSE", _concept_clause(concept_code, expand_descendants))
        return self._query(query + f"LIMIT {maximum + 1}", ("id", "value", "concept"), maximum + 1)
