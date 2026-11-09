"""Validation and normalization for public service inputs."""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Literal, get_args
from urllib.parse import urlsplit

from .bounds import HARD_MAX_NODES
from .errors import InputValidationError

type PolicyReason = Literal[
    "authorization",
    "logical_limit",
    "attempt_limit",
    "concurrency_limit",
    "deadline",
    "response_limit",
    "byte_limit",
    "accounting",
    "closed",
    "transport",
]
type BodyState = Literal["complete", "proven_empty", "unreadable"]
type RequestMethod = Literal["GET", "POST"]
type LedgerState = Literal["created", "open", "closed", "failed"]
type SummaryState = Literal["incomplete", "failed", "completed"]
type ChildRole = Literal["worker", "service"]

SearchMode = Literal["hybrid", "bm25", "vector"]
UnresolvedConceptReason = Literal["compound_code", "missing_source", "non_ncit_source"]
Direction = Literal["out", "in", "both"]
EdgeType = Literal[
    "parent",
    "child",
    "descendant",
    "role",
    "inverse_role",
    "association",
    "inverse_association",
]

# The closed value sets of the configuration, once here like the ones above.
Profile = Literal["evs", "cadsr", "unified"]
UpstreamMode = Literal["live", "fixture"]
ReleaseChannel = Literal["monthly", "weekly"]
Transport = Literal["stdio", "streamable-http"]
HTTPAuthMode = Literal["trusted-local", "required"]
HTTPSessions = Literal["stateful", "stateless"]
ReleaseSelection = Literal["explicit", "session-held", "freshly-resolved"]
ConceptInclude = Literal["synonyms", "definitions", "properties", "semanticType"]
PublicSearchMode = Literal["lexical", "typeahead", "semantic", "hybrid"]
RetiredSelection = Literal["include", "only"]
RelationshipKind = Literal["role", "association"]
Polarity = Literal["positive", "negative"]
HierarchyDirection = Literal["parent", "child", "pathsToRoot"]
NeighborhoodKind = Literal[
    "parent", "child", "role", "association", "inverseRole", "inverseAssociation"
]
TruncationBound = Literal[
    "results", "depth", "nodes", "edges", "kind_budget", "requests", "upstream_cap", "deadline"
]
# "ssis_facade" is in the specification's closed set; this server does not emit it.
ProvenanceSource = Literal[
    "evs_rest", "evs_fhir", "evs_index", "cadsr_rest", "cadsr_export", "ssis_facade", "ssis_sparql"
]
ServedBy = Literal["live", "cache", "index", "fixture"]
DataElementInclude = Literal[
    "permissibleValues",
    "valueDomain",
    "conceptAssociations",
    "alternateNames",
    "classificationSchemes",
    "referenceDocuments",
    "dataElementConcept",
]
RegistrySearchMode = Literal["lexical", "semantic", "hybrid"]
DataElementConceptRole = Literal["objectClass", "property", "representationTerm"]
GraphConceptRole = Literal["objectClass", "property", "valueMeaning"]
GRAPH_CONCEPT_ROLES = frozenset(get_args(GraphConceptRole))
MatchStrictness = Literal["restricted", "unrestricted"]
MatchedItemType = Literal["Concept", "ValueMeaning"]
CodeMapSource = Literal["CRDC"]
CrossDomainTerminology = Literal["ncit"]
StoredValueConfidence = Literal["asserted", "none"]
DatasetName = Literal["ncit", "ssis_ncit_graph", "ssis_cadsr_graph", "cadsr_export"]

PROFILES = frozenset(get_args(Profile))
UPSTREAM_MODES = frozenset(get_args(UpstreamMode))
RELEASE_CHANNELS = frozenset(get_args(ReleaseChannel))
TRANSPORTS = frozenset(get_args(Transport))
HTTP_SESSIONS = frozenset(get_args(HTTPSessions))
HTTP_AUTH_MODES = frozenset(get_args(HTTPAuthMode))

# The stated forms of the identifiers that go into an upstream path or query: the `patterns` of
# spec/tools.yaml, once. A test holds them equal to the specification.
TERMINOLOGY_FORM = "^[a-z][a-z0-9_]*$"
RELEASE_FORM = "^[A-Za-z0-9][A-Za-z0-9._-]*$"
NCIT_CODE_FORM = "^C[1-9][0-9]*$"
REGISTRY_ID_FORM = "^[1-9][0-9]*$"
ITEM_VERSION_FORM = "^[0-9]+([.][0-9]+)?$"
NCIT_CODE_RE = re.compile(NCIT_CODE_FORM)
SEARCH_MODES = frozenset(get_args(SearchMode))
TRAVERSAL_DIRECTIONS = frozenset(get_args(Direction))
TRAVERSAL_EDGE_TYPES = frozenset(get_args(EdgeType))
MAX_SEARCH_LIMIT = 100
MAX_INDEX_SEARCH_LIMIT = 1000


def validate_identifier(value: str, pattern: str, parameter: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        raise InputValidationError(f"{parameter} must match {pattern}", parameter)
    return value


def validate_choice(value: str, choices: tuple[str, ...], parameter: str) -> str:
    if value not in choices:
        raise InputValidationError(f"{parameter} must be one of: {', '.join(choices)}", parameter)
    return value


def bounded(value: int, maximum: int, parameter: str) -> int:
    if not _is_int(value) or value < 1:
        raise InputValidationError(f"{parameter} must be a positive integer", parameter)
    return min(value, maximum)


def validate_terminology(terminology: str) -> str:
    if not isinstance(terminology, str) or not re.fullmatch(TERMINOLOGY_FORM, terminology):
        raise InputValidationError(
            "terminology must be a lowercase identifier starting with a letter", "terminology"
        )
    return terminology


def validate_expansion_options(count: int, offset: int, active_only: bool) -> int:
    count = bounded(count, 1000, "count")
    if not _is_int(offset) or offset < 0:
        raise InputValidationError("offset must be a nonnegative integer", "offset")
    if not isinstance(active_only, bool):
        raise InputValidationError("activeOnly must be boolean", "activeOnly")
    return count


def validate_channel(channel: str) -> str:
    if channel not in RELEASE_CHANNELS:
        raise InputValidationError("channel must be monthly or weekly", "channel")
    return channel


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def validate_kind_budget(value: int | None) -> None:
    if value is not None and (not _is_int(value) or value < 1):
        raise InputValidationError("budget_per_kind must be a positive integer", "budget_per_kind")


def validate_ncit_code(code: str, parameter: str = "code") -> str:
    normalized = str(code or "").strip().upper()
    if not NCIT_CODE_RE.fullmatch(normalized):
        raise InputValidationError(
            "NCIt code must have the form C followed by digits with no leading zero, "
            f"not {str(code)[:40]!r}",
            parameter,
        )
    return normalized


def validate_ncit_codes(codes: Iterable[str], parameter: str = "codes") -> list[str]:
    normalized = list(dict.fromkeys(validate_ncit_code(code, parameter) for code in codes))
    if not normalized:
        raise InputValidationError("At least one NCIt code is required", parameter)
    return normalized


def validate_search(
    query: str, limit: int, mode: str, *, maximum: int = MAX_SEARCH_LIMIT
) -> tuple[str, int, str]:
    normalized_query = str(query or "").strip()
    if not normalized_query:
        raise InputValidationError("Search query must not be blank", "query")
    if not _is_int(limit) or not 1 <= limit <= maximum:
        raise InputValidationError(f"Search limit must be between 1 and {maximum}", "limit")
    normalized_mode = str(mode or "").lower()
    if normalized_mode not in SEARCH_MODES:
        allowed = ", ".join(sorted(SEARCH_MODES))
        raise InputValidationError(f"Search mode must be one of: {allowed}", "mode")
    return normalized_query, limit, normalized_mode


# URL queries must leave room for path/filter encoding. Recheck recorded EVS URL ceilings
# (HTTP 400 at 8,046 bytes) before raising this owner-approved semantic bound.
MAX_QUERY_TEXT = 1000
# CDE 6148373 v1 has 1,011 code points in the July 2026 export (text SHA-256
# 0c579c0318b0d9c0e8afc23a84b07700372d3ccfd998c70941a8bd417076938e).
# 2,000 leaves room beyond that incomplete snapshot; remeasure the export before changing.
MAX_QUESTION_TEXT = 2000
# Matching/grounding carry short phrases in bodies, not documents. The owner selected
# 2,000 for those fields; review published match contracts and real inputs before raising.
MAX_MATCH_TEXT = 2000
# Dictionary metadata can contain descriptions and samples. The owner's 10,000-code-point
# allowance bounds per-field work; remeasure real dictionary fields before changing it.
MAX_DICTIONARY_TEXT = 10000
# EVS rejected an 8,046-byte target; 7,000 is its existing conservative batch budget.
# For caDSR this is server policy, not measured capacity. Recheck encoded routes before raising.
MAX_TEXT_TARGET_BYTES = 7000

TEXT_LIMITS = {
    "query": MAX_QUERY_TEXT,
    "questionText": MAX_QUESTION_TEXT,
    "text": MAX_MATCH_TEXT,
    "entities.name": MAX_MATCH_TEXT,
    "entities.userTip": MAX_MATCH_TEXT,
    "entities.permissibleValues": MAX_MATCH_TEXT,
    "values": MAX_MATCH_TEXT,
    "columns.name": MAX_DICTIONARY_TEXT,
    "columns.description": MAX_DICTIONARY_TEXT,
    "columns.sampleValues": MAX_DICTIONARY_TEXT,
}


def text_limit_description() -> str:
    """The refusal contract shared by handler descriptions, without duplicating limits."""
    return (
        "Text beyond inputSchema maxLength is invalid_request before upstream/index work, "
        "never shortened. URL-carried text also has a separate "
        f"{MAX_TEXT_TARGET_BYTES:,}-byte encoded-target ceiling including filters and pins; "
        "for caDSR it is conservative server policy, not measured capacity."
    )


def validate_text_lengths(value: object, path: str = "") -> None:
    """Validate caller text once, including nested records, without changing its content."""
    if isinstance(value, dict):
        for key, item in value.items():
            validate_text_lengths(item, f"{path}.{key}" if path else str(key))
    elif isinstance(value, list):
        for item in value:
            validate_text_lengths(item, path)
    elif isinstance(value, str):
        _text_length(value, path)


def _text_length(value: str, path: str) -> None:
    maximum = TEXT_LIMITS.get(path)
    if maximum is not None and len(value) > maximum:
        raise InputValidationError(
            f"Use at most {maximum} Unicode code points; never shortened", path
        )


def validate_text_target(url: str, parameter: str) -> None:
    """Bound the actual encoded path/query, never the unencoded caller text."""
    parts = urlsplit(url)
    # The request builder already percent-encoded this ASCII target, so bytes == characters.
    size = len(parts.path + ("?" + parts.query if parts.query else ""))
    if size > MAX_TEXT_TARGET_BYTES:
        raise InputValidationError(
            f"Encoded request target is {size} bytes; use at most {MAX_TEXT_TARGET_BYTES} bytes",
            parameter,
        )


def _validate_limits(codes: list[str], max_depth: int, max_nodes: int, max_edges: int) -> None:
    if not _is_int(max_depth) or max_depth < 0:
        raise InputValidationError("max_depth must be a non-negative integer", "max_depth")
    for field, value in (("max_nodes", max_nodes), ("max_edges", max_edges)):
        if not _is_int(value) or value < 1:
            raise InputValidationError(f"{field} must be a positive integer", field)
    node_limit = min(max_nodes, HARD_MAX_NODES)
    if len(codes) > node_limit:
        raise InputValidationError(
            f"{len(codes)} start codes exceed the node limit of {node_limit} "
            f"(max_nodes, at most {HARD_MAX_NODES})",
            "start_codes",
        )


def _normalize_edge_types(edge_types: Iterable[str] | None) -> list[str] | None:
    if not edge_types:
        return None
    normalized = list(
        dict.fromkeys(str(edge_type or "").strip().lower() for edge_type in edge_types)
    )
    if not TRAVERSAL_EDGE_TYPES.issuperset(normalized):
        allowed = ", ".join(sorted(TRAVERSAL_EDGE_TYPES))
        raise InputValidationError(f"Edge type must be one of: {allowed}", "edge_types")
    return normalized


def _normalize_relationship_names(names: Iterable[str] | None) -> list[str] | None:
    if not names:
        return None
    normalized = list(dict.fromkeys(str(name or "").strip() for name in names))
    if "" in normalized:
        raise InputValidationError("Relationship names must not be blank", "relationship_names")
    return normalized


def validate_traversal(
    start_codes: Iterable[str],
    direction: str,
    max_depth: int,
    max_nodes: int,
    max_edges: int,
    edge_types: Iterable[str] | None,
    relationship_names: Iterable[str] | None = None,
) -> tuple[list[str], str, list[str] | None, list[str] | None]:
    codes = validate_ncit_codes(start_codes, "start_codes")
    normalized_direction = str(direction or "").lower()
    if normalized_direction not in TRAVERSAL_DIRECTIONS:
        allowed = ", ".join(sorted(TRAVERSAL_DIRECTIONS))
        raise InputValidationError(f"Traversal direction must be one of: {allowed}", "direction")
    _validate_limits(codes, max_depth, max_nodes, max_edges)
    return (
        codes,
        normalized_direction,
        _normalize_edge_types(edge_types),
        _normalize_relationship_names(relationship_names),
    )
