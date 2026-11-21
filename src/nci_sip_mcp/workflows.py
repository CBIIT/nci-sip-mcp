"""Bounded workflows over the same content producers as the fine-grained tools."""

from __future__ import annotations

from itertools import batched
from typing import Annotated, Any, NotRequired, TypedDict

from . import cadsr_content, cadsr_matching, content, seam
from .bounds import HARD_MAX_EDGES, Budget, budgeted, current_budget
from .caching import select_cache_hint
from .catalogue import exclusion_codes, load_catalogue
from .context import Context
from .errors import InputValidationError, PlatformError
from .models import Truncation, results_cut
from .parameters import (
    Described,
    MatchFilters,
    NcitRelease,
    RegistryRelease,
    count_bound,
    describe_fields,
)
from .permissions import require, require_operation
from .release import ReleaseContext
from .release_selection import implicit_selection
from .validation import (
    NCIT_CODE_FORM,
    RELEASE_FORM,
    bounded,
    validate_identifier,
)


class DictionaryColumn(TypedDict):
    name: str
    description: NotRequired[str]
    sampleValues: NotRequired[list[str]]


describe_fields(
    DictionaryColumn,
    name="Column name as written in the dictionary, for example primary_site.",
    description="What the column holds, in words; it is sent as the entity's user tip.",
    sampleValues='Example values of the column, for example ["male", "female"]; they are '
    "aligned to value meanings.",
)


def _ground_options(code: str | None, text: str | None, commons: str | None) -> None:
    if (code is None) == (text is None):
        raise InputValidationError("Give exactly one of conceptCode or text", "conceptCode")
    if code is not None:
        validate_identifier(code, NCIT_CODE_FORM, "conceptCode")
    if text is not None:
        cadsr_matching._text(text, "text")
    if commons is not None:
        cadsr_matching._text(commons, "commons")


def _ground_registry(context: Context, requested: str | None) -> None:
    cadsr_content.refuse_pinned(context, requested, "pinned grounding")


def _text_code(context: Context, text: str, selected: ReleaseContext) -> str:
    require("search_concepts")
    pin = None if implicit_selection() else selected.version
    try:
        found = content.search_concepts(context, "ncit", text, pin)["results"]
    except InputValidationError as exc:
        # Fixed options, validated release/text and no cursor leave only query refusal here.
        raise InputValidationError(str(exc), "text") from exc
    if not found:
        raise PlatformError(
            "not_found", "No concept matches. Try other text or give a conceptCode."
        )
    return found[0]["concept"]["code"]


def _hop(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    return rows[: seam.MAX_RESULTS], results_cut(len(rows), seam.MAX_RESULTS)


def _ground_hops(
    context: Context, selected: ReleaseContext, code: str, commons: str | None
) -> dict[str, Any]:
    require("find_data_elements_for_concept")
    graphs = context.ssis.get_graph_identities()
    provenance = seam._provenance(context, selected, graphs)
    elements = context.ssis.find_data_elements(code, maximum=seam.MAX_RESULTS)
    values = context.ssis.find_permissible_values(code, maximum=seam.MAX_RESULTS)
    hops = {
        "dataElements": [seam._element_use(row, provenance) for row in elements],
        "permissibleValues": [seam._value_use(row, provenance) for row in values],
    }
    if commons is not None:
        hops["storedValues"] = _stored_hop(context, selected, code, commons)
    return _bounded_hops(hops, provenance)


def _bounded_hops(
    hops: dict[str, list[dict[str, Any]]], provenance: dict[str, Any]
) -> dict[str, Any]:
    result: dict[str, Any] = {"provenance": provenance}
    cuts = {}
    for name, rows in hops.items():
        result[name], cuts[name] = _hop(rows)
    cut = next((record for record in cuts.values() if record["occurred"]), None)
    result["truncation"] = (cut | {"perHop": cuts}) if cut else {"occurred": False}
    return result


def _stored_hop(
    context: Context, selected: ReleaseContext, code: str, commons: str
) -> list[dict[str, Any]]:
    require_operation("resolve_stored_value", {"commons": commons})
    if commons != "GDC":
        return seam._crosswalk(context, selected, code, commons, None)["storedValues"]
    return _gdc_hop(context, selected, code)


def _gdc_hop(context: Context, selected: ReleaseContext, code: str) -> list[dict[str, Any]]:
    require("resolve_stored_value")
    source, provenance = seam.gdc_provenance(context, selected)
    result: list[dict[str, Any]] = []
    for rows, _ in seam.gdc_pages(context, code):
        result.extend(seam.gdc_values(rows, code, source, provenance))
        if len(result) > seam.MAX_RESULTS:
            return result[: seam.MAX_RESULTS + 1]
        # Checked again before the generator fetches the next page.
        require("resolve_stored_value")
    return result


def ground_value(
    context: Context,
    conceptCode: Annotated[
        str | None,
        Described(
            "NCIt code of the concept to ground, for example C3262. Give exactly one of "
            "conceptCode and text.",
            pattern=NCIT_CODE_FORM,
        ),
    ] = None,
    text: Annotated[
        str | None,
        Described(
            "Words naming the concept, for example lung carcinoma; the first result of a "
            "lexical search is used and named in the result. Give exactly one of "
            "conceptCode and text."
        ),
    ] = None,
    commons: Annotated[
        str | None,
        Described(
            "Data commons whose stored values to add, for example GDC. Leave unset for no "
            "stored values."
        ),
    ] = None,
    release: NcitRelease = None,
    registryRelease: RegistryRelease = None,
) -> dict[str, Any]:
    """Ground a concept, or a text that names one, in both EVS and caDSR: the concept, the data
    elements and permissible values that use it, and optionally a commons' stored values.
    Data element uses name every graph role and actual matching conceptCode, with optional
    integer displayOrder; the graph gives no primary flag, so use get_data_element for
    caDSR's primary indicator.
    Optional workflowStatuses and registrationStatuses are unchanged sorted sets across
    the graph's merged versions, unpaired and possibly mixed. For one version's own
    status pair, call get_data_element with that explicit version.
    Representation-term use is not covered by this route, so absence does not establish non-use.

    registryRelease is left unset today (C-1).
    Text is bounded by its inputSchema maxLength; longer input is refused with invalid_request
    before any upstream or index work, never shortened.

    Returns the concept, dataElements, permissibleValues and, with commons, storedValues; every
    joined record names both content states. Each hop holds at most 1000 results; a cut reports
    a full perHop truncation record and never cuts another hop.

    not_found when text matches no concept (try other text or a conceptCode); invalid_request
    unless exactly one of the two is given; capability_unavailable for a published registry pin
    (C-1), release_not_available for an unpublished one; upstream_unavailable when a source
    cannot be read.
    """
    _ground_options(conceptCode, text, commons)
    require_operation("ground_value", {"text": text, "commons": commons})
    if release is not None:
        validate_identifier(release, RELEASE_FORM, "release")
    if text is not None:
        content._search_target(context, "ncit", release, text, "lexical", 10, "text")
    with budgeted(current_budget() or Budget()):
        _ground_registry(context, registryRelease)
        selected = seam._selected(context, release)
        code = conceptCode if conceptCode is not None else _text_code(context, text or "", selected)
        pin = None if implicit_selection() else selected.version
        require("get_concept")
        concept = content.get_concept(context, "ncit", code, pin)
        result = {"concept": concept} | _ground_hops(context, selected, code, commons)
    seam._mixed_cache()
    return result


def _column(column: DictionaryColumn) -> tuple[dict[str, Any], list[str]]:
    if not isinstance(column, dict) or set(column) - set(DictionaryColumn.__annotations__):
        raise InputValidationError("Use name, description and sampleValues only", "columns")
    entity = {"entity": cadsr_matching._text(column.get("name"), "columns.name")}
    if "description" in column:
        entity["entityUserTip"] = cadsr_matching._text(column["description"], "columns.description")
    samples = column.get("sampleValues", [])
    if not isinstance(samples, list):
        raise InputValidationError("Must be a list of text values", "columns.sampleValues")
    for sample in samples:
        cadsr_matching._text(sample, "columns.sampleValues")
    return entity, samples


def _align_columns(
    context: Context, samples: list[list[str]], state: dict[str, str]
) -> list[list[dict[str, Any]]]:
    headers = cadsr_matching._vm_headers("restricted", None)
    responses: dict[tuple[str, ...], list[dict[str, Any]]] = {}
    aligned = []
    for values in samples:
        matches = []
        for batch in batched(values, 10, strict=False):
            if batch not in responses:
                responses[batch], _ = cadsr_matching.match_values(
                    context, list(batch), headers, state
                )
            matches.extend(responses[batch])
        aligned.append(matches)
    return aligned


def harmonize_data_dictionary(
    context: Context,
    columns: Annotated[
        list[DictionaryColumn],
        Described(
            "The columns of the data dictionary to match, 1 to 10. Each has a name and "
            "may have a description and sample values.",
            min_items=1,
            max_items=10,
        ),
    ],
    registryRelease: RegistryRelease = None,
    filters: Annotated[
        MatchFilters | None,
        Described(
            "Narrow the data elements matched by context, workflow status, registration "
            "status, classification scheme or value domain type. Leave unset for no "
            "filter."
        ),
    ] = None,
) -> dict[str, Any]:
    """Match the columns of a data dictionary to caDSR data elements, aligning their sample values,
    in one call.

    registryRelease is left unset today (C-1); every match names the same registry state.
    Each column name, description and sample value is bounded by its inputSchema maxLength;
    longer input is refused with invalid_request before any upstream work, never shortened.

    Returns the columns in caller order, each with every platform match and its aligned sample
    values, plus the names that matched nothing. Results are computed from the text given.

    invalid_request for bad input, before any request. Any failed request fails the whole call
    and never becomes an empty match: timeout, upstream_unavailable, capability_unavailable for
    a published pin (C-1), release_not_available for an unpublished one.
    """
    prepared = [_column(column) for column in cadsr_matching._list(columns, "columns")]
    headers = cadsr_matching._headers(filters, 10)
    require_operation("harmonize_data_dictionary", {"columns": columns})
    with budgeted(current_budget() or Budget()):
        state = cadsr_matching._matching_release(context, registryRelease)
        groups, provenance = cadsr_matching.match_entities(
            context, [entity for entity, _ in prepared], headers, state, 10
        )
        alignment = _align_columns(context, [samples for _, samples in prepared], state)
    select_cache_hint(resolution=False, computed=True)
    return _dictionary_result(columns, groups, alignment, provenance)


def _dictionary_result(
    columns: list[DictionaryColumn],
    groups: list[list[dict[str, Any]]],
    alignment: list[list[dict[str, Any]]],
    provenance: dict[str, Any],
) -> dict[str, Any]:
    results = [
        {"name": column["name"], "matches": matches, "permissibleValueAlignment": aligned}
        for column, matches, aligned in zip(columns, groups, alignment, strict=True)
    ]
    return {
        "columns": results,
        "unmatched": [column["name"] for column in results if not column["matches"]],
        "provenance": provenance,
    }


def _exclusions(graph: dict[str, Any], code: str) -> list[dict[str, Any]]:
    return [
        {
            "code": edge["targetCode"],
            "terminology": edge["targetTerminology"],
            "edge": edge,
            "provenance": edge["provenance"],
        }
        for edge in graph["edges"]
        if edge["sourceCode"] == code and edge["provenance"].get("polarity") == "negative"
    ]


def _cohort(
    context: Context, selected: ReleaseContext, code: str, budget: Budget, negative: bool
) -> dict[str, Any]:
    depth, maximum = budget.depth, budget.nodes
    require("get_concept_neighborhood")
    exclusions = exclusion_codes(context.settings, "ncit")
    load_catalogue(context.evs, selected, exclusions)
    # Roles are read independently: a small cohort bound must not hide an exclusion.
    budget.depth, budget.nodes, budget.edges = 1, 1000, HARD_MAX_EDGES
    roles = content._graph_record(
        content._graph(context, selected, code, ["role"], budget, exclusions=exclusions)
    )
    excluded = _exclusions(roles, code)
    withheld = set() if negative else {row["code"] for row in excluded}
    budget.depth, budget.nodes = depth, maximum + len(withheld) + 1
    require("get_concept_hierarchy")
    graph = content._graph_record(content._graph(context, selected, code, ["child"], budget))
    return _cohort_result(graph, roles, excluded, withheld, maximum)


def _cohort_result(
    graph: dict[str, Any],
    roles: dict[str, Any],
    excluded: list[dict[str, Any]],
    withheld: set[str],
    maximum: int,
) -> dict[str, Any]:
    members = [node["code"] for node in graph["nodes"] if node["code"] not in withheld]
    codes = members[:maximum]
    cut = _cohort_cut(graph["truncation"], roles["truncation"], len(members), maximum)
    present = set(codes)
    edges = [edge for edge in graph["edges"] if {edge["sourceCode"], edge["targetCode"]} <= present]
    return {
        "codes": codes,
        "excluded": excluded,
        "edges": edges + [row["edge"] for row in excluded],
        "truncation": cut,
        "provenance": graph["nodes"][0]["provenance"],
    }


def _cohort_cut(
    cut: dict[str, Any], role_cut: dict[str, Any], count: int, maximum: int
) -> dict[str, Any]:
    if count > maximum:
        cut = Truncation(
            occurred=True,
            bound="nodes",
            limit=maximum,
            reached=maximum,
            omitted=count - maximum,
            exact=False,
        ).to_dict()
    if role_cut["occurred"] and role_cut["bound"] != "depth":
        cut = role_cut
    return cut


def expand_cohort(
    context: Context,
    conceptCode: Annotated[
        str,
        Described(
            "NCIt code of the concept whose cohort to expand, for example C3262.",
            pattern=NCIT_CODE_FORM,
        ),
    ],
    release: NcitRelease = None,
    maxDepth: Annotated[int, count_bound("How many levels of descendants to include.", 2, 4)] = 2,
    includeNegative: Annotated[
        bool,
        Described(
            "Keep the codes an exclusion role withholds in codes; they are listed in "
            "excluded either way. Default false."
        ),
    ] = False,
    maxNodes: Annotated[
        int, count_bound("Most codes to return, the concept itself included.", 200, 1000)
    ] = 200,
) -> dict[str, Any]:
    """Expand an NCIt concept into the cohort of its descendants, leaving out the codes its own
    exclusion roles withhold.

    Returns codes, excluded (every exclusion assertion, whether or not the code is kept), edges
    and truncation naming any bound that omitted content. The role and descendant walks each keep
    the hard traversal edge bound, regardless of maxNodes. Only the root's exclusion roles govern
    the cohort, not those of descendants.

    not_found for an unknown code; bound_exceeded when the request limit is spent before a
    result exists; release_not_available when a stateful handshake HTTP session's or stdio
    connection's pinned release is withdrawn; upstream_unavailable otherwise.
    Sessionless 2026-07-28 HTTP resolves omitted releases per call.
    """
    validate_identifier(conceptCode, NCIT_CODE_FORM, "conceptCode")
    require_operation("expand_cohort", {})
    if type(includeNegative) is not bool:
        raise InputValidationError("includeNegative must be boolean", "includeNegative")
    budget = Budget(
        depth=bounded(maxDepth, 4, "maxDepth"), nodes=bounded(maxNodes, 1000, "maxNodes")
    )
    with budgeted(budget):
        selected = seam._selected(context, release)
        result = _cohort(context, selected, conceptCode, budget, includeNegative)
    select_cache_hint(resolution=False, implicit=implicit_selection())
    return result
