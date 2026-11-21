"""Effective-release EVS content entries and the interim NCIt index."""

from __future__ import annotations

from dataclasses import replace
from itertools import batched
from typing import Annotated, Any, NoReturn, get_args
from urllib.parse import urlsplit

from ncit_index.errors import NoActiveIndexError

from . import cursor as cursors
from . import fhir
from .bounds import (
    HARD_MAX_BATCH_CODES,
    HARD_MAX_DEPTH,
    HARD_MAX_EDGES,
    HARD_MAX_NODES,
    HARD_MAX_PER_KIND,
    MAX_BATCH_TARGET_BYTES,
    Budget,
    CallDeadlineError,
    RequestBudgetError,
    budgeted,
    current_budget,
)
from .catalogue import exclusion_codes, load_catalogue
from .context import Context
from .errors import InputValidationError, PlatformError, call_correlation_id
from .evs import (
    EVSReleaseNotFoundError,
    EVSResponseError,
    concept_path,
    normalize_concept,
    replacements_path,
    search_request,
)
from .models import (
    TraversalEdge,
    TraversalProvenance,
    TraversalResult,
    Truncation,
    attribution_of,
    live_provenance,
    release_ref,
    upstream_origin,
    utc_now_iso,
    with_attribution,
)
from .parameters import Code, Cursor, Described, Release, Terminology, count_bound
from .release import ReleaseContext, resolve_evs_release
from .release_selection import implicit_selection, select
from .traversal import BATCH_SIZE, traverse_ncit
from .validation import (
    MAX_INDEX_SEARCH_LIMIT,
    NCIT_CODE_FORM,
    ConceptInclude,
    CrossDomainTerminology,
    HierarchyDirection,
    NeighborhoodKind,
    PublicSearchMode,
    RetiredSelection,
    bounded,
    validate_choice,
    validate_expansion_options,
    validate_identifier,
    validate_text_target,
)


def _code(code: str, terminology: str) -> str:
    if terminology == "ncit":
        return validate_identifier(code, NCIT_CODE_FORM, "code")
    return code


def _record(raw: dict[str, Any], provenance: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(raw.get("active"), bool):
        raise EVSResponseError("EVS returned a concept without its boolean active status")
    if not raw.get("code") or not raw.get("name"):
        raise EVSResponseError("EVS returned a concept without its code or name")
    provenance = with_attribution(provenance, raw, "evs")
    if provenance["source"] != "evs_index":
        provenance = provenance | {"upstream": upstream_origin(raw)}
    result = {
        "code": raw["code"],
        "terminology": raw["terminology"],
        "name": raw["name"],
        "active": raw["active"],
        "provenance": _provenance(provenance),
    }
    if raw.get("conceptStatus") not in (None, ""):
        result["status"] = raw["conceptStatus"]
    if raw.get("leaf") is not None:
        result["leaf"] = _leaf(raw["leaf"])
    return result


def _leaf(value: Any) -> bool:
    if not isinstance(value, bool):
        raise EVSResponseError("EVS returned a concept whose leaf flag is not a boolean")
    return value


def _provenance(provenance: dict[str, Any]) -> dict[str, Any]:
    # The legacy walker names the direction of navigation. Public hierarchy
    # provenance names the assertion, which points from the child to its parent.
    kind = provenance.get("relationship", {}).get("kind")
    if kind in ("parent", "child"):
        return provenance | {"direction": "out" if kind == "parent" else "in"}
    return provenance


def get_concept(
    context: Context,
    terminology: Terminology,
    code: Code,
    release: Release = None,
    include: Annotated[
        list[ConceptInclude] | None,
        Described(
            "Sections to add: any of synonyms, definitions, properties, semanticType. "
            "Leave unset for the base record only."
        ),
    ] = None,
) -> dict[str, Any]:
    """Read one concept of a terminology by its code, with its status and, if asked, its synonyms,
    definitions, properties and semantic types.

    include adds synonyms, definitions, properties and semanticType.

    Returns code, terminology, name, active, the status and leaf flag EVS gives when it gives
    them, and provenance naming the verified release.

    not_found when the code is absent; release_not_available when the release is not served;
    upstream_unavailable when EVS cannot answer.
    """
    code = _code(code, terminology)
    sections, upstream = _includes(include)
    selected = select(context, terminology, release)
    raw = context.evs.get_concept(
        code,
        release=selected,
        include=upstream,
    )
    if raw.get("code") != code:
        raise EVSResponseError("EVS returned a concept other than the one requested")
    return project_concept(context, selected, raw, sections)


def _includes(include: list[ConceptInclude] | None) -> tuple[list[ConceptInclude], str]:
    sections = list(dict.fromkeys(include or []))
    for section in sections:
        validate_choice(section, get_args(ConceptInclude), "include")
    upstream = ["properties" if item == "semanticType" else item for item in sections]
    return sections, ",".join(dict.fromkeys(["minimal", *upstream]))


def get_concept_subsets(
    context: Context,
    terminology: Terminology,
    code: Code,
    release: Release = None,
) -> dict[str, Any]:
    """List the subsets a concept belongs to.

    Returns subsets in platform order, each with code, terminology, name and provenance. These
    are the concept's Concept_In_Subset associations, computed ones without a relationship code
    included. An empty list still carries provenance.

    not_found for an unknown code; upstream_unavailable when the concept's content is malformed
    or EVS cannot answer.
    """
    rows, provenance = _concept_rows(context, terminology, release, code, "associations")
    subsets = []
    for row in rows:
        if _text_fields(row, ("type",))["type"] == "Concept_In_Subset":
            fields = _text_fields(row, ("relatedCode", "relatedName"))
            subsets.append(
                {
                    "code": fields["relatedCode"],
                    "terminology": terminology,
                    "name": fields["relatedName"],
                    "provenance": with_attribution(provenance, row, "evs"),
                }
            )
    return {"subsets": subsets} | ({"provenance": provenance} if not subsets else {})


def expand_value_set(
    context: Context,
    terminology: Annotated[
        CrossDomainTerminology,
        Described("Terminology of the subset; only ncit is served."),
    ],
    release: Release = None,
    valueSet: Annotated[
        str | None,
        Described(
            "Code of the subset to expand, for example C165258. Give exactly one of "
            "valueSet and code."
        ),
    ] = None,
    code: Annotated[
        str | None,
        Described(
            "Code of the subset to expand, as an alternative to valueSet. Give exactly "
            "one of valueSet and code."
        ),
    ] = None,
    count: Annotated[
        int,
        Described(
            "Most members on a page. Default 200, at most 1000; a larger value is applied "
            "as 1000 and a value below 1 is refused."
        ),
    ] = 200,
    offset: Annotated[
        int,
        Described(
            "How many members to skip, to reach a later page. Default 0; a value below 0 "
            "is refused."
        ),
    ] = 0,
    activeOnly: Annotated[
        bool, Described("Return only the active members. Default false.")
    ] = False,
) -> dict[str, Any]:
    """List the members of an NCIt subset (a value set), a page at a time.

    count and offset page the members in place of a cursor; activeOnly filters before paging. EVS
    cannot pin an expansion, so release must match what it serves now.

    Returns members in platform order with FHIR provenance, total and truncation. Pages, clamped
    and past-the-end ones included, are not truncation; inactive appears on a member only when
    true.

    invalid_request for a count below 1, an offset below 0, for valueSet and code given both or
    neither, or for a terminology other than ncit; release_mismatch when EVS
    serves another version (historical expansion is not promised); upstream_unavailable
    otherwise.
    """
    supplied = [value for value in (valueSet, code) if value is not None]
    if len(supplied) != 1:
        raise InputValidationError("Supply exactly one of valueSet or code", "valueSet")
    identifier = _code(supplied[0], terminology)
    count = validate_expansion_options(count, offset, activeOnly)
    selected = select(context, terminology, release)
    return fhir.expand(context.fhir, selected, identifier, count, offset, activeOnly)


def get_concept_mappings(
    context: Context,
    terminology: Terminology,
    code: Code,
    release: Release = None,
    targetTerminology: Annotated[
        str | None,
        Described(
            "Only the mappings to this target terminology, written exactly as the "
            "platform names it, case included. Leave unset for all."
        ),
    ] = None,
) -> dict[str, Any]:
    """List the mappings a concept carries to other terminologies.

    Returns mappings in platform order with the record's values unchanged: target code,
    terminology, name and type, and the target's version and term type when present. An empty
    list still carries provenance; licence text passes through only when EVS supplies it.

    not_found for an unknown code; upstream_unavailable when a required field is missing or EVS
    cannot answer, failing the whole call.
    """
    rows, provenance = _concept_rows(context, terminology, release, code, "maps")
    mappings = [_mapping_record(row, provenance) for row in rows]
    if targetTerminology is not None:
        mappings = [row for row in mappings if row["targetTerminology"] == targetTerminology]
    return {"mappings": mappings} | ({"provenance": provenance} if not mappings else {})


def _concept_rows(
    context: Context, terminology: str, release: str | None, code: str, section: str
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    code = _code(code, terminology)
    selected = select(context, terminology, release)
    raw = context.evs.get_concept(code, release=selected, include=f"minimal,{section}")
    if raw.get("code") != code:
        raise EVSResponseError("EVS returned a concept other than the one requested")
    rows = raw.get(section, [])
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise EVSResponseError(f"EVS returned malformed concept {section}")
    return rows, project_concept(context, selected, raw, [])["provenance"]


def _text_fields(row: dict[str, Any], fields: tuple[str, ...]) -> dict[str, str]:
    result = {}
    for field in fields:
        value = row.get(field)
        if not isinstance(value, str) or not value:
            raise EVSResponseError(f"EVS returned a missing or invalid {field}")
        result[field] = value
    return result


def _mapping_record(row: dict[str, Any], provenance: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = _text_fields(
        row, ("targetCode", "targetTerminology", "targetName", "type")
    )
    for field in ("targetTermType", "targetTerminologyVersion"):
        if row.get(field) not in (None, ""):
            result.update(_text_fields(row, (field,)))
    return result | {"provenance": with_attribution(provenance, row, "evs")}


def project_concept(
    context: Context, release: ReleaseContext, raw: dict[str, Any], sections: list[ConceptInclude]
) -> dict[str, Any]:
    """Project retrieved concept content without changing the call's release selection."""
    uri = context.evs.uri(concept_path(release.pinned_terminology, raw["code"]))
    concept = normalize_concept(raw, release_date=release.date, source="live_evs")
    result = _record(raw, concept.provenance(uri).to_dict())
    return result | {section: _section(raw, section) for section in sections}


def resolve_retired_code(
    context: Context,
    terminology: Terminology,
    code: Code,
    release: Release = None,
) -> dict[str, Any]:
    """Tell whether a code is retired and which concepts replace it.

    Returns code, terminology, the platform's active flag, its status text unchanged,
    replacements (code, name, terminology, provenance and optional action/date from the same
    history row, unchanged) and provenance. An active concept
    has no replacements; a retired one that names none gets []. Status text never decides
    whether a concept is active.

    not_found for an unknown code; upstream_unavailable when the replacement history cannot be
    read, never an empty list in its place.
    """
    code = _code(code, terminology)
    selected = select(context, terminology, release)
    concept = get_concept(context, terminology, code, release)
    result = {key: value for key, value in concept.items() if key != "name"}
    result["replacements"] = []
    if not concept["active"]:
        rows = context.evs.get_replacements(code, selected)
        uri = context.evs.uri(replacements_path(selected.pinned_terminology, code))
        result["replacements"] = [
            _replacement_record(row, selected, uri) for row in rows if "replacementCode" in row
        ]
    return result


def _replacement_record(row: dict[str, Any], release: ReleaseContext, uri: str) -> dict[str, Any]:
    code, name = row.get("replacementCode"), row.get("replacementName")
    for key, value in (("code", code), ("name", name)):
        if not isinstance(value, str) or not value:
            raise EVSResponseError(f"EVS returned a replacement without its {key}")
    provenance = live_provenance(
        release_ref(release.terminology, release.version, release.date),
        "evs_rest",
        uri=uri,
        upstream=upstream_origin(row),
        attribution=attribution_of(row, "evs"),
    )
    result = {
        "code": code,
        "terminology": release.terminology,
        "name": name,
        "provenance": provenance,
    }
    for key in ("action", "date"):
        if key in row:
            if not isinstance(row[key], str):
                raise EVSResponseError(f"EVS returned a replacement with a nontext {key}")
            result[key] = row[key]
    return result


def get_concepts(
    context: Context,
    terminology: Terminology,
    codes: Annotated[
        list[str],
        Described(
            'Codes of the concepts to fetch, for example ["C3262", "C2991"]; at most 650. '
            "Results keep this order.",
            max_items=650,
        ),
    ],
    release: Release = None,
    include: Annotated[
        list[ConceptInclude] | None,
        Described(
            "Sections to add: any of synonyms, definitions, properties, semanticType. "
            "Leave unset for the base record only."
        ),
    ] = None,
) -> dict[str, Any]:
    """Read several concepts of a terminology by code in one call, in the order asked.

    Returns concepts and missing (the codes EVS does not know), both in input order with
    duplicates kept. Each concept carries its verified release, status and provenance. Empty
    input returns two empty lists.

    invalid_request when there are more than 650 codes or the encoded request exceeds 7000
    bytes; bound_exceeded when the response is too large, never partial concepts (split the
    codes and call again). A nonempty batch is one platform call.
    """
    requested = _batch_codes(codes, terminology)
    unique = list(dict.fromkeys(requested))
    sections, upstream = _includes(include)
    selected = select(context, terminology, release)
    if not unique:
        return {"concepts": [], "missing": [], "provenance": _empty_provenance(selected)}
    _batch_target(context, selected, unique, upstream)
    raw = context.evs.get_concepts_by_codes(unique, selected, include=upstream)
    found = _reconcile_batch(raw, set(unique))
    result = {
        "concepts": [
            project_concept(context, selected, found[code], sections)
            for code in requested
            if code in found
        ],
        "missing": [code for code in requested if code not in found],
    }
    if not found:
        result["provenance"] = _empty_provenance(selected)
    return result


def _empty_provenance(release: ReleaseContext) -> dict[str, Any]:
    return live_provenance(
        release_ref(release.terminology, release.version, release.date), "evs_rest"
    )


def _batch_codes(codes: list[str], terminology: str) -> list[str]:
    if len(codes) > HARD_MAX_BATCH_CODES:
        raise InputValidationError(f"codes accepts at most {HARD_MAX_BATCH_CODES} entries", "codes")
    return [_code(code, terminology) for code in codes]


def _batch_target(
    context: Context, release: ReleaseContext, codes: list[str], include: str
) -> None:
    url = urlsplit(
        context.evs.uri(
            concept_path(release.pinned_terminology), {"list": ",".join(codes), "include": include}
        )
    )
    target = f"{url.path}?{url.query}"
    if len(target.encode()) > MAX_BATCH_TARGET_BYTES:
        raise InputValidationError(
            f"The encoded batch request exceeds {MAX_BATCH_TARGET_BYTES} bytes; "
            "request fewer codes",
            "codes",
        )


def _reconcile_batch(raw: list[dict[str, Any]], requested: set[str]) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    for item in raw:
        code = item.get("code")
        if not isinstance(code, str) or code not in requested or code in found:
            raise EVSResponseError(
                "EVS returned a missing, unsolicited or duplicate batch identity"
            )
        found[code] = item
    return found


def _section(raw: dict[str, Any], section: str) -> list[Any]:
    if section == "semanticType":
        return [item["value"] for item in raw.get("properties", []) if item.get("code") == "P106"]
    return raw.get(section, [])


def list_relationships(
    context: Context,
    terminology: Terminology,
    release: Release = None,
) -> dict[str, Any]:
    """List the roles and associations a terminology defines, each with its polarity.

    Returns relationships, each with code, terminology, name, kind (role or association),
    polarity and provenance. For NCIt, polarity follows the configured exclusion codes (R135 to
    R142 by default), never names; other terminologies have no exclusion set. Use it to see
    which relationship names exist before a walk.

    internal_error with details.missingCodes when configured exclusion codes are missing from
    the catalogue, in which case no list is returned; upstream_unavailable for other upstream
    errors.
    """

    with budgeted(Budget()):
        selected = select(context, terminology, release)
        relationships = load_catalogue(
            context.evs, selected, exclusion_codes(context.settings, terminology)
        )
    return {"relationships": relationships} | (
        {"provenance": _empty_provenance(selected)} if not relationships else {}
    )


def search_concepts(
    context: Context,
    terminology: Terminology,
    query: Annotated[str, Described("Text to search for, for example kinase inhibitor.")],
    release: Release = None,
    mode: Annotated[
        PublicSearchMode,
        Described(
            "How to match: lexical (EVS order, the default), typeahead (names that start "
            "with the text), or semantic and hybrid (ranked with scores from the local "
            "NCIt index)."
        ),
    ] = "lexical",
    limit: Annotated[int, count_bound("Most results on a page.", 10, 1000)] = 10,
    cursor: Cursor = None,
    retired: Annotated[
        RetiredSelection,
        Described(
            "Which concepts to return: include, all statuses (the default), or only the "
            "retired ones."
        ),
    ] = "include",
) -> dict[str, Any]:
    """Find concepts of a terminology whose name, synonyms or definitions match a text, a page at a
    time.

    semantic and hybrid rank by meaning from the local NCIt index, NCIt only; leaving retired
    concepts out and the upstream search types are not offered.

    Returns results with the concept's code, name, active flag, and the status and leaf flag EVS
    gives where it gives them, but no sections, so read a concept by code for its definitions.
    Semantic and hybrid results come from the index, so their provenance has no sourceUri or
    upstream. Also totalKnown and nextCursor: pass nextCursor
    back unchanged as cursor for the next page. Lexical and typeahead keep EVS order and have no
    scores. Semantic and hybrid carry a score (not comparable between queries) and matchedOn
    naming name, synonym or definition; exact preferred names win ties.

    Text beyond its published maxLength is invalid_request, never shortened. URL-carried
    text also has a 7,000-byte encoded request-target ceiling including filters and pins.
    Local lexical scoring uses the first 64 tokens; vectors use the whole text.

    capability_unavailable when the local index or NumPy is missing; release_mismatch when the
    index holds another release; invalid_request for an indexed mode on another terminology;
    cursor_expired when the index or an explicit release changed since the cursor;
    release_not_available when a stateful handshake HTTP session's or stdio connection's pinned
    release is withdrawn (start a new session or name a release). Sessionless 2026-07-28 HTTP
    resolves omitted releases per call.
    """
    limit = bounded(limit, MAX_INDEX_SEARCH_LIMIT, "limit")
    _search_options(query, mode, retired)
    indexed = mode in ("semantic", "hybrid")
    _search_target(context, terminology, release, query, mode, limit)
    arguments = {
        "tool": "search_concepts",
        "terminology": terminology,
        "release": release,
        "query": query,
        "mode": mode,
        "limit": limit,
        "retired": retired,
    }
    if indexed and terminology != "ncit":
        raise InputValidationError("The interim index supports only ncit", "terminology")
    cursors.validate_before_selection(cursor, arguments, indexed=indexed)
    selected = select(context, terminology, release)
    arguments["release"] = selected.version
    position = cursors.decode(cursor, arguments, indexed=indexed)
    try:
        status = _retired_status(context, selected) if retired == "only" else None
        handler = _indexed_search if indexed else _live_search
        return handler(context, selected, arguments, position, status)
    except EVSReleaseNotFoundError:
        if _keep_release_error(cursor):
            raise
        _expired_release(context, selected)


def _keep_release_error(cursor: str | None) -> bool:
    return cursor is None or implicit_selection()


def _search_target(
    context: Context,
    terminology: str,
    release: str | None,
    query: str,
    mode: str,
    limit: int,
    parameter: str = "query",
) -> None:
    if mode in ("semantic", "hybrid"):
        return
    # Admission precedes discovery; the actual request checks its resolved fields again.
    pinned = terminology + "_"
    if release is not None:
        pinned += release
    path, params = search_request(pinned, query, mode, 0, limit)
    validate_text_target(context.evs.uri(path, params), parameter)


def _indexed_search(
    context: Context,
    selected: ReleaseContext,
    arguments: dict[str, Any],
    position: cursors.Position,
    status: str | None,
) -> dict[str, Any]:
    try:
        hits, total, manifest = context.index.search_page(
            arguments["query"],
            context.embedding_provider,
            arguments["limit"],
            "vector" if arguments["mode"] == "semantic" else "hybrid",
            requested_release=selected.version,
            offset=position.offset,
            build_id=position.build_id,
            retired_status=status,
        )
    except NoActiveIndexError:
        capability = "semantic/hybrid search without an active NCIt index"
        raise PlatformError(
            "capability_unavailable",
            f"{capability} is not implemented. Use a supported option or retry after it is "
            "available.",
            capability=capability,
        ) from None
    results = []
    for hit in hits:
        results.append(
            {
                "concept": _record(hit.concept.raw, hit.concept.provenance(None).to_dict()),
                "score": hit.score,
                "matchedOn": hit.matched_on,
            }
        )
    result = _search_page(results, total, arguments, position, manifest.build_id)
    if not results:
        result["provenance"] = manifest.provenance().to_dict()
    return result


def _live_search(
    context: Context,
    selected: ReleaseContext,
    arguments: dict[str, Any],
    position: cursors.Position,
    status: str | None,
) -> dict[str, Any]:
    total, rows = context.evs.search_concepts(
        selected,
        arguments["query"],
        arguments["mode"],
        position.offset,
        arguments["limit"],
        status,
    )
    results = [_live_match(context, selected, row, arguments["mode"], status) for row in rows]
    result = _search_page(results, total, arguments, position)
    if not rows:
        result["provenance"] = live_provenance(
            release_ref(selected.terminology, selected.version, selected.date),
            "evs_rest",
            uri=context.evs.uri(concept_path(selected.pinned_terminology) + "/search"),
        )
    return result


def _live_match(
    context: Context,
    selected: ReleaseContext,
    raw: dict[str, Any],
    mode: str,
    status: str | None,
) -> dict[str, Any]:
    if not isinstance(raw.get("code"), str) or not raw["code"]:
        raise EVSResponseError("EVS search returned a concept without its code")
    if status and (raw.get("conceptStatus") != status or raw.get("active") is not False):
        raise EVSResponseError("EVS search did not honor the requested retired status")
    return {"concept": project_concept(context, selected, raw, [])} | _live_highlight(raw, mode)


def _live_highlight(raw: dict[str, Any], mode: str) -> dict[str, str]:
    if mode == "lexical" and "highlight" in raw:
        if not isinstance(raw["highlight"], str):
            raise EVSResponseError("EVS search returned a non-text highlight")
        return {"matchedOn": raw["highlight"]}
    return {}


def _search_page(
    results: list[dict[str, Any]],
    total: int,
    arguments: dict[str, Any],
    position: cursors.Position,
    build_id: str | None = None,
) -> dict[str, Any]:
    if position.offset and position.offset >= total:
        raise InputValidationError("The cursor position is beyond this search", "cursor")
    result: dict[str, Any] = {"results": results, "totalKnown": total}
    following = position.offset + len(results)
    if following < total:
        result["nextCursor"] = cursors.encode(arguments, following, build_id)
    return result


def _retirement_metadata(context: Context, selected: ReleaseContext) -> dict[str, Any]:
    rows = [
        row
        for row in context.evs.get_terminologies()
        if row.get("terminology") == selected.terminology and row.get("version") == selected.version
    ]
    if not rows:
        raise EVSReleaseNotFoundError("EVS no longer lists the requested release")
    if len(rows) != 1:
        raise EVSResponseError("EVS lists ambiguous retirement metadata for the pinned release")
    metadata = rows[0].get("metadata", {})
    if not isinstance(metadata, dict):
        raise EVSResponseError("EVS returned malformed retirement metadata")
    return metadata


def _retired_status(context: Context, selected: ReleaseContext) -> str:
    metadata = _retirement_metadata(context, selected)
    status = metadata.get("retiredStatusValue")
    choices = metadata.get("conceptStatuses", [])
    if not isinstance(choices, list):
        raise EVSResponseError("EVS returned malformed concept status choices")
    if not isinstance(status, str) or not status or status not in choices:
        raise InputValidationError("This terminology has no selectable retired status", "retired")
    return status


def _expired_release(context: Context, selected: ReleaseContext) -> NoReturn:
    current = resolve_evs_release(context.evs, selected.terminology, selected.channel)
    raise PlatformError(
        "cursor_expired",
        "EVS no longer serves the cursor release. Restart with the current release.",
        cursorRelease=selected.version,
        currentRelease=current.version,
    ) from None


def _search_options(query: str, mode: str, retired: str) -> None:
    validate_choice(mode, get_args(PublicSearchMode), "mode")
    validate_choice(retired, get_args(RetiredSelection), "retired")
    if not isinstance(query, str) or not query.strip():
        raise InputValidationError("query must not be blank", "query")


def get_concept_hierarchy(
    context: Context,
    terminology: Terminology,
    code: Code,
    direction: Annotated[
        HierarchyDirection,
        Described(
            "Which way to walk: parent, child, or pathsToRoot for every path to the root "
            "(depth, limit and cursor do not apply to it)."
        ),
    ],
    release: Release = None,
    depth: Annotated[int, count_bound("How many levels to walk.", 1, 4)] = 1,
    limit: Annotated[int, count_bound("Most concepts on a page.", 200, 1000)] = 200,
    cursor: Cursor = None,
) -> dict[str, Any]:
    """Walk a concept's hierarchy: its parents or children to a depth, or every path to the root.

    Returns nodes (the concepts reached, not the one asked about), each with traversal
    provenance, and truncation. nextCursor continues in breadth-first platform order with the
    same arguments. A depth cut is reported only when unseen targets remain; leaves and cycles
    to returned nodes are complete, and an unknown continuation has exact=false. pathsToRoot
    returns every platform path in order and each reached concept once; depth, limit and cursor
    do not apply to it.

    bound_exceeded when paging cannot reach the page within the request limit: narrow the
    concept or depth. cursor_expired when an explicit release was withdrawn;
    release_not_available when a stateful handshake HTTP session's or stdio connection's pinned
    release was (start a new session or name a release); not_found for an unknown code.
    Sessionless 2026-07-28 HTTP resolves omitted releases per call.
    """
    code = _code(code, terminology)
    validate_choice(direction, get_args(HierarchyDirection), "direction")
    if direction != "pathsToRoot":
        depth = bounded(depth, HARD_MAX_DEPTH, "depth")
        limit = bounded(limit, HARD_MAX_NODES, "limit")
    arguments = {
        "terminology": terminology,
        "release": release,
        "code": code,
        "direction": direction,
        "depth": depth,
        "limit": limit,
    }
    if direction != "pathsToRoot":
        cursors.validate_before_selection(cursor, arguments)
    budget = Budget(depth=depth, paged=True)
    with budgeted(budget):
        selected = select(context, terminology, release)
        if direction == "pathsToRoot":
            return _paths_to_root(context, selected, code)
        arguments["release"] = selected.version
        return _hierarchy(context, selected, arguments, cursor, budget)


def _hierarchy(
    context: Context,
    selected: ReleaseContext,
    arguments: dict[str, Any],
    cursor: str | None,
    budget: Budget,
) -> dict[str, Any]:
    offset = cursors.decode(cursor, arguments).offset
    # The hierarchy has a page allowance, not a total node or edge allowance.
    # Reserve the seed and a lookahead node; replay remains request-bounded.
    budget.nodes = offset + arguments["limit"] + 2
    budget.edges = budget.nodes**2
    graph = _hierarchy_replay(
        context, selected, arguments["code"], arguments["direction"], budget, cursor
    )
    return _hierarchy_page(graph, arguments, offset, arguments["limit"])


def _hierarchy_page(
    graph: TraversalResult, arguments: dict[str, Any], offset: int, limit: int
) -> dict[str, Any]:
    # A hierarchy walk has exactly one seed, emitted first.
    projected = _graph_record(graph)["nodes"]
    nodes = projected[1:]
    if offset and offset >= len(nodes) and not graph.deadline_exhausted:
        raise InputValidationError("The cursor position is beyond this hierarchy", "cursor")
    result: dict[str, Any] = {
        "nodes": nodes[offset : offset + limit],
        "truncation": _truncation(graph.truncation),
    }
    if len(nodes) > offset + limit and not graph.deadline_exhausted:
        result["nextCursor"] = cursors.encode(arguments, offset + limit)
    if not result["nodes"]:
        result["provenance"] = projected[0]["provenance"]
    return result


def _hierarchy_replay(
    context: Context,
    release: ReleaseContext,
    code: str,
    direction: str,
    budget: Budget,
    cursor: str | None,
) -> TraversalResult:
    try:
        graph = _graph(context, release, code, [direction], budget)
        if graph.deadline_exhausted:
            return graph
        if budget.exhausted:
            raise RequestBudgetError(budget.requests, budget.attempts)
        return graph
    except EVSReleaseNotFoundError:
        if cursor is None or implicit_selection():
            raise
        _expired_release(context, release)
    except RequestBudgetError as exc:
        raise PlatformError(
            "bound_exceeded",
            "Hierarchy replay exhausted its request budget. Narrow the query with "
            "a nearer starting concept or smaller depth.",
            **exc.details,
        ) from None


def _paths_to_root(context: Context, release: ReleaseContext, code: str) -> dict[str, Any]:
    with budgeted(current_budget() or Budget()):
        seed = context.evs.get_concept(code, release=release, include="minimal")
        if seed.get("code") != code:
            raise EVSResponseError("EVS returned a concept other than the requested path seed")
        paths = context.evs.get_paths_to_root(code, release=release)
    uri = context.evs.uri(
        concept_path(release.pinned_terminology, code) + "/pathsToRoot", {"include": "minimal"}
    )
    nodes = _path_nodes(paths, release, uri, code)
    result: dict[str, Any] = {
        "nodes": list(nodes.values()),
        "paths": [[raw["code"] for raw in path] for path in paths],
        "truncation": {"occurred": False},
    }
    if not nodes:
        result["provenance"] = _record(seed, _path_provenance(release, uri, 0))["provenance"]
    return result


def _path_nodes(
    paths: list[list[dict[str, Any]]], release: ReleaseContext, uri: str, code: str
) -> dict[str, dict[str, Any]]:
    nodes: dict[str, dict[str, Any]] = {}
    for path in paths:
        for depth, raw in enumerate(path):
            if raw["code"] != code and raw["code"] not in nodes:
                nodes[raw["code"]] = _record(raw, _path_provenance(release, uri, depth))
    return nodes


def _path_provenance(release: ReleaseContext, uri: str, depth: int) -> dict[str, Any]:
    return TraversalProvenance(
        release=release_ref(release.terminology, release.version, release.date),
        source="evs_rest",
        served_by="live",
        retrieved_at=utc_now_iso(),
        correlation_id=call_correlation_id(),
        source_uri=uri,
        depth=depth,
        relationship={"kind": "parent", "name": ""} if depth else None,
        direction="out" if depth else None,
        polarity="positive" if depth else None,
    ).to_dict()


def get_concept_neighborhood(
    context: Context,
    terminology: Terminology,
    code: Code,
    release: Release = None,
    depth: Annotated[int, count_bound("How many steps to walk from the concept.", 2, 4)] = 2,
    kinds: Annotated[
        list[NeighborhoodKind] | None,
        Described(
            "Relationship kinds to follow: any of parent, child, role, association, "
            "inverseRole, inverseAssociation. Leave unset for all six."
        ),
    ] = None,
    maxNodes: Annotated[
        int, count_bound("Most concepts to return, the starting concept included.", 200, 1000)
    ] = 200,
    maxEdges: Annotated[int, count_bound("Most relationships to return.", 1000, 5000)] = 1000,
    budgetPerKind: Annotated[
        int | None,
        Described(
            "Most concepts any one kind may add, at most 1000. Leave unset to let the "
            "kinds take turns within maxNodes."
        ),
    ] = None,
    includeNegative: Annotated[
        bool,
        Described(
            "Also walk through negative assertions, such as exclusion roles; they are "
            "returned marked either way. Default false."
        ),
    ] = False,
) -> dict[str, Any]:
    """Map the relationships around a concept, parents, children, roles and associations and their
    inverses, to a depth.

    budgetPerKind bounds the nodes each kind adds; otherwise kinds take turns within maxNodes.
    Negative assertions and their targets are returned marked but not expanded unless
    includeNegative is true or a positive route reaches them.

    Returns nodes (the seed at depth 0) and edges, each with traversal provenance; qualifiers
    and evidence pass through unchanged and a relationship with no upstream code stays positive.
    truncation names the first bound that dropped anything. Forward kinds are checked at the
    final frontier: unseen targets mean a depth cut, leaves and cycles to returned nodes do not.
    Selected inverse kinds report depth at any nonempty frontier with omitted=0 and exact=false,
    because their lists are not read just to count continuation.

    internal_error with details.missingCodes when configured exclusion codes are missing from
    the role and association catalogues (no graph is returned); bound_exceeded before any graph
    exists when the request limit is spent; not_found for an unknown code.
    """
    code = _code(code, terminology)
    selected_kinds = _kinds(kinds)
    if not isinstance(includeNegative, bool):
        raise InputValidationError("includeNegative must be boolean", "includeNegative")
    budget = Budget(
        depth=bounded(depth, HARD_MAX_DEPTH, "depth"),
        nodes=bounded(maxNodes, HARD_MAX_NODES, "maxNodes"),
        edges=bounded(maxEdges, HARD_MAX_EDGES, "maxEdges"),
        per_kind=None
        if budgetPerKind is None
        else bounded(budgetPerKind, HARD_MAX_PER_KIND, "budgetPerKind"),
    )
    with budgeted(budget):
        selected = select(context, terminology, release)
        exclusions = exclusion_codes(context.settings, terminology)
        load_catalogue(context.evs, selected, exclusions)
        result = _graph_record(
            _graph(
                context,
                selected,
                code,
                selected_kinds,
                budget,
                exclusions=exclusions,
                include_negative=includeNegative,
            )
        )
    return result


def _kinds(kinds: list[NeighborhoodKind] | None) -> list[str]:
    selected = list(get_args(NeighborhoodKind)) if kinds is None else list(dict.fromkeys(kinds))
    if not selected:
        raise InputValidationError("kinds must not be empty", "kinds")
    for kind in selected:
        validate_choice(kind, get_args(NeighborhoodKind), "kinds")
    aliases = {"inverseRole": "inverse_role", "inverseAssociation": "inverse_association"}
    return [aliases.get(kind, kind) for kind in selected]


def _graph(
    context: Context,
    release: ReleaseContext,
    code: str,
    kinds: list[str],
    budget: Budget,
    *,
    exclusions: frozenset[str] = frozenset(),
    include_negative: bool = True,
) -> TraversalResult:
    with budgeted(budget):
        graph = traverse_ncit(
            context.evs,
            [code],
            release,
            kinds,
            budget,
            exclusions=exclusions,
            include_negative=include_negative,
        )
        return _hydrate(context, graph, release, budget, kinds)


def _graph_record(graph: TraversalResult) -> dict[str, Any]:
    nodes = [
        _record(graph.concepts[node.code], node.provenance.to_dict())
        for node in graph.nodes
        if node.code in graph.concepts
    ]
    present = {node["code"] for node in nodes}
    return {
        "nodes": nodes,
        "edges": [
            _edge(edge) for edge in graph.edges if {edge.source_code, edge.target_code} <= present
        ],
        "truncation": _truncation(graph.truncation),
    }


def _hydrate(
    context: Context,
    graph: TraversalResult,
    release: ReleaseContext,
    budget: Budget,
    kinds: list[str],
) -> TraversalResult:
    """Hydrate node status in batches, retaining verified content when a bound stops work."""

    if graph.deadline_exhausted:
        return graph
    concepts = dict(graph.concepts)
    missing = [node.code for node in graph.nodes if node.code not in concepts]
    for batch in batched(missing, BATCH_SIZE, strict=False):
        try:
            raw = context.evs.get_concepts_by_codes(batch, release=release, include="minimal")
        except (RequestBudgetError, CallDeadlineError) as exc:
            # Returning relation names as full concepts would invent their active status.
            # The caller gets the verified portion, with the first omission retained.
            partial = replace(
                graph, concepts=concepts, deadline_exhausted=isinstance(exc, CallDeadlineError)
            )
            return replace(partial, truncation=_hydration_cut(partial, budget, kinds, exc))
        by_code = _reconcile_batch(raw, set(batch))
        if set(by_code) != set(batch):
            raise EVSResponseError("EVS did not return exactly the requested graph concepts")
        concepts.update(by_code)
    return replace(graph, concepts=concepts)


def _hydration_cut(
    graph: TraversalResult,
    budget: Budget,
    kinds: list[str],
    error: RequestBudgetError | CallDeadlineError | None = None,
) -> Truncation:
    missing = {node.code for node in graph.nodes} - graph.concepts.keys()
    cut = _hydration_bound(budget, len(missing), error)
    record = graph.truncation if graph.truncation.occurred else cut
    if len(kinds) > 1:
        per_kind = {kind: _hydration_kind_cut(graph, budget, missing, kind, cut) for kind in kinds}
        record = replace(record, per_kind=per_kind)
    return record


def _hydration_bound(
    budget: Budget, omitted: int, error: RequestBudgetError | CallDeadlineError | None
) -> Truncation:
    if isinstance(error, CallDeadlineError):
        limit, reached, omitted = error.figures
        return Truncation(True, "deadline", limit, reached, omitted, False)
    return _request_cut(budget, omitted)


def _hydration_kind_cut(
    graph: TraversalResult,
    budget: Budget,
    missing: set[str],
    kind: str,
    cut: Truncation,
) -> Truncation:
    record = (graph.truncation.per_kind or {}).get(kind, Truncation(False))
    if record.occurred:
        return record
    targets = {edge.target_code for edge in graph.edges if edge.edge_type == kind} & missing
    if not targets:
        return record
    return cut if cut.bound == "deadline" else _request_cut(budget, len(targets))


def _request_cut(budget: Budget, omitted: int) -> Truncation:
    return Truncation(
        occurred=True,
        bound="requests",
        limit=budget.requests,
        reached=budget.attempts,
        omitted=omitted,
        exact=False,
    )


def _truncation(record: Truncation) -> dict[str, Any]:
    result = record.to_dict()
    aliases = {"inverse_role": "inverseRole", "inverse_association": "inverseAssociation"}
    if "perKind" in result:
        result["perKind"] = {
            aliases.get(kind, kind): value for kind, value in result["perKind"].items()
        }
    return result


def _edge(edge: TraversalEdge) -> dict[str, Any]:
    source, target = edge.source_code, edge.target_code
    if edge.edge_type in ("child", "inverse_role", "inverse_association"):
        source, target = target, source
    return {
        "sourceCode": source,
        "sourceTerminology": edge.provenance.release["terminology"],
        "targetCode": target,
        "targetTerminology": edge.provenance.release["terminology"],
        "provenance": _provenance(edge.provenance.to_dict()),
    }
