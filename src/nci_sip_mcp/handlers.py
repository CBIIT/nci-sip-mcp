"""Domain handlers shared by the MCP and CLI registry."""

from __future__ import annotations

import logging
from collections.abc import Callable
from itertools import batched
from typing import Annotated, Any, cast, get_args

from ncit_index.errors import NoActiveIndexError
from ncit_index.index import require_index_release
from ncit_index.records import IndexManifest

from . import indexing
from .audit import emit
from .bounds import (
    DEFAULT_MAX_DEPTH,
    DEFAULT_MAX_EDGES,
    DEFAULT_MAX_NODES,
    Budget,
    budgeted,
)
from .catalogue import exclusion_codes
from .content import get_concept
from .context import Context
from .errors import (
    PlatformError,
    is_error_record,
)
from .evaluation import evaluate_build, evaluate_retrieval
from .evaluation_sets import production_set
from .evs import (
    TERMINOLOGIES_PATH,
    EVSError,
    EVSResponseError,
    concept_path,
    normalize_concept,
)
from .http_client import UpstreamError, UpstreamUnavailableError
from .index_adapter import manifest_record, results_truncation
from .indexing import full_build
from .invocation import error_record
from .models import (
    live_provenance,
    release_ref,
    upstream_origin,
    with_attribution,
)
from .parameters import Described, Terminology
from .release import ReleaseContext, current_terminologies, resolve_evs_release, served_evs_release
from .traversal import (
    select_edge_types,
    traverse_ncit,
)
from .validation import (
    ConceptInclude,
    Direction,
    EdgeType,
    ReleaseChannel,
    SearchMode,
    validate_channel,
    validate_kind_budget,
    validate_ncit_code,
    validate_ncit_codes,
    validate_search,
    validate_terminology,
    validate_traversal,
)

logger = logging.getLogger(__name__)


def _release(context: Context) -> ReleaseContext:
    """The NCIt release the configured channel names now: resolved for one call, never kept."""

    return resolve_evs_release(context.evs, "ncit", context.settings.release_channel)


def resolve_release(
    context: Context,
    terminology: Terminology,
    channel: Annotated[
        ReleaseChannel | None,
        Described(
            "Release channel, monthly or weekly. Leave unset to use the channel the "
            "server is configured with (monthly by default)."
        ),
    ] = None,
) -> dict[str, Any]:
    """Find the current release of an EVS terminology in its monthly or weekly channel.

    Returns the terminology, channel, version and date of the release, the other versions EVS
    serves in alternatives, and provenance naming the live EVS listing. Use it to learn which
    release to name in later calls.

    release_not_available when no single release is latest in that channel; upstream_unavailable
    when EVS cannot answer. A guessed version is never returned, and discovery is always fresh.
    """

    terminology = validate_terminology(terminology)
    selected_channel = validate_channel(
        context.settings.release_channel if channel is None else channel
    )
    selected = resolve_evs_release(context.evs, terminology, selected_channel).to_dict()
    return selected | {
        "alternatives": _alternatives(context.evs.get_terminologies(), selected),
        "provenance": _release_provenance(context, selected),
    }


def _alternatives(rows: list[dict[str, Any]], selected: dict[str, Any]) -> list[str]:
    rows = [row for row in rows if row.get("terminology") == selected["terminology"]]
    versions = [str(row.get("version") or "") for row in rows]
    if not all(versions):
        raise EVSResponseError("EVS listed an alternative release without a version")
    alternatives = dict.fromkeys(versions)
    alternatives.pop(selected["version"], None)
    return list(alternatives)


def list_terminologies(context: Context) -> dict[str, Any]:
    """List the terminologies EVS serves, each with its current release.

    No arguments. NCIt is listed at the release of the configured channel (monthly unless set
    otherwise), because EVS can mark both channels latest; every other terminology at its sole
    latest release.

    Returns terminologies, each with its name, release and live EVS provenance, and, where EVS
    sends them, its channel tags, the status value that marks a retired concept
    (retiredStatusValue) and its licence text as the provenance's attribution. Use it to see what
    can be read, at which release, and whether retired concepts can be told apart.

    release_not_available when the current release of any one listed terminology is ambiguous
    or missing: the whole listing fails closed rather than omit that terminology or guess its
    release, so one terminology's metadata fault also hides the others; upstream_unavailable
    when EVS cannot answer, an empty listing included (it is unusable metadata, reported with
    its HTTP status and attempt count). No release is invented.
    """

    rows = current_terminologies(context.evs.get_terminologies(), context.settings.release_channel)
    return {"terminologies": [_terminology(context, row) for row in rows]}


def _terminology(context: Context, row: dict[str, Any]) -> dict[str, Any]:
    metadata = row.get("metadata")
    if metadata is None:
        metadata = {}
    if not isinstance(metadata, dict):
        raise EVSResponseError("EVS listed a terminology with malformed metadata")
    provenance = with_attribution(_release_provenance(context, row), metadata, "evs")
    item = {
        "terminology": row["terminology"],
        "release": row["version"],
        "provenance": provenance | {"upstream": upstream_origin(row)},
    }
    # Passed through as EVS sends them; absent where it sends none.
    if row.get("tags") is not None:
        item["tags"] = _text_mapping(row["tags"], "tags")
    retired = metadata.get("retiredStatusValue")
    if retired is not None:
        if not isinstance(retired, str):
            raise EVSResponseError("EVS listed a terminology with a malformed retiredStatusValue")
        item["retiredStatusValue"] = retired
    return item


def _text_mapping(value: Any, name: str) -> dict[str, str]:
    if not isinstance(value, dict) or not all(
        isinstance(key, str) and isinstance(text, str) for key, text in value.items()
    ):
        raise EVSResponseError(f"EVS listed a terminology with malformed {name}")
    return value


def release_info(context: Context) -> dict[str, Any]:
    """Report the EVS API version, the configured channel's NCIt release and the local index.

    The call succeeds even when EVS cannot be reached: `evs_api` and
    `selected_release` (the release the configured channel names, monthly by
    default) then hold an error object, and the report has no `provenance`, which
    otherwise names the selected release. A selected release contains `terminology`,
    `channel`, `version` and `date`; its pinned request path is internal. `active_index` is
    null until an index has been built. `embedding.active_index_compatible`
    says whether the CLI search can use the index: it is false when there is
    none or when it was built with other embedding settings."""

    def evs_status(fetch: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        try:
            return fetch()
        except (EVSError, UpstreamError, PlatformError) as exc:
            return error_record(exc)

    manifest = context.index.get_active_manifest()
    selected = evs_status(lambda: _release(context).to_dict())
    report = {
        "evs_api": evs_status(context.evs.get_api_version),
        "selected_release": selected,
        "active_index": manifest.to_result() if manifest else None,
        "embedding": {
            "provider": context.embedding_provider.name,
            "model": context.embedding_provider.model,
            "active_index_compatible": bool(
                manifest
                and manifest.embedding_matches(
                    context.embedding_provider.name, context.embedding_provider.model
                )
            ),
        },
    }
    # The report is dated and attributed by the release it selected, so there is none to
    # name when EVS could not say.
    if is_error_record(selected):
        return report
    return report | {"provenance": _release_provenance(context, selected)}


def _release_provenance(context: Context, selected: dict[str, Any]) -> dict[str, Any]:
    """The provenance of the release report: the selected release, read from EVS now."""

    return live_provenance(
        release_ref(selected["terminology"], selected["version"], selected.get("date")),
        "evs_rest",
        uri=context.evs.uri(TERMINOLOGIES_PATH),
    )


def _fetch_for_index(
    context: Context, codes: list[str], release: ReleaseContext
) -> tuple[list[dict[str, Any]], set[str]]:
    """Fetch the payloads pinned to the release, and the codes EVS returned.

    EVS omits the codes it does not know.
    """

    raw_concepts: list[dict[str, Any]] = []
    for batch in batched(codes, context.settings.index_batch_size, strict=False):
        raw_concepts.extend(context.evs.get_concepts_by_codes(batch, release=release))
    returned_codes = {str(raw.get("code") or "") for raw in raw_concepts}
    if not returned_codes <= set(codes):
        raise EVSResponseError("EVS returned a concept that was not requested")
    return raw_concepts, returned_codes


def index_codes(context: Context, codes: list[str]) -> dict[str, Any]:
    """Index a small list of NCIt codes from the configured channel's current release."""

    normalized_codes = validate_ncit_codes(codes)
    release = _release(context)
    raw_concepts, returned_codes = _fetch_for_index(context, normalized_codes, release)
    missing_codes = [code for code in normalized_codes if code not in returned_codes]
    if missing_codes:
        raise PlatformError(
            "not_found",
            f"NCIt release {release.version} has no concept {', '.join(missing_codes)}; "
            "the index was not changed. Remove the codes, or check them against that release.",
            identifiers=missing_codes,
        )
    manifest = indexing.open_index(context.settings.data_dir).upsert_concepts(
        raw_concepts=(normalize_concept(raw, release.date, "active_cache") for raw in raw_concepts),
        release_date=release.date,
        embedding_provider=context.embedding_provider,
        expected_release_version=release.version,
    )
    emit(
        logger,
        logging.INFO,
        "index_build_complete",
        release=manifest.release_version,
        concepts=manifest.concept_count,
    )
    return manifest_record(manifest).to_result()


def index_build(context: Context) -> dict[str, Any]:
    """Build and evaluate all NCIt from the configured channel without activation."""
    built = full_build(context, _release(context))
    return _evaluated_build(context, built)


def index_builds(context: Context) -> dict[str, Any]:
    """List completed operator snapshots and identify the active build."""
    return {
        "builds": [
            item.to_dict() for item in indexing.open_index(context.settings.data_dir).list_builds()
        ]
    }


def index_rebuild(context: Context, build_id: str) -> dict[str, Any]:
    """Rebuild stored raw concepts offline; evaluate new production snapshots."""
    built = indexing.open_index(context.settings.data_dir).rebuild(
        build_id, context.embedding_provider
    )
    return _evaluated_build(context, built)


def _evaluated_build(context: Context, built: IndexManifest) -> dict[str, Any]:
    if built.build_kind == "production":
        built = evaluate_build(
            indexing.open_index(context.settings.data_dir),
            context.embedding_provider,
            production_set(),
            built.build_id,
        )
        return {
            "buildId": built.build_id,
            "manifest": manifest_record(built).to_result(),
            "evaluation": built.evaluation_report,
        }
    return {"buildId": built.build_id, "manifest": manifest_record(built).to_result()}


def index_activate(context: Context, build_id: str) -> dict[str, Any]:
    """Activate a completed build; the replaced build is retained for rollback.

    `pendingRemoval` names builds the activation superseded but could not yet delete (a
    storage failure, logged as `index_build_removal_pending`); activating any completed build
    (the active one included) retries.
    """
    index = indexing.open_index(context.settings.data_dir)
    active = index.activate(build_id)
    return {
        "buildId": active.build_id,
        "manifest": manifest_record(active).to_result(),
        "pendingRemoval": index.pending_removal(),
    }


def search(
    context: Context,
    query: str,
    limit: int = 10,
    mode: SearchMode = "hybrid",
    include_raw: bool = False,
) -> dict[str, Any]:
    """Search the locally indexed NCIt concepts by text.

    The index holds only the concepts an operator loaded with the
    `index-sample` or `index-build` CLI commands, all from the NCIt release named
    in the `provenance.release` of its hits. A sample is not all of NCIt; no
    tool here adds to it. `mode` is `hybrid` (0.55 * BM25 + 0.45 * vector), `bm25` or
    `vector`; `limit` is 1 to 100.

    Each entry of `score_components` is min-max normalized over the
    fields scored for this query: the best is 1.0 however poor the match,
    the weakest is 0.0 even when it matches, and when only one field is
    scored, or all tie, they are all 1.0. Exact preferred-name hits score 1
    and win ties, comparing case-insensitively after NFC and whitespace collapsing.
    A component that was not computed
    for a concept (the other one in `bm25` or `vector` mode, or `bm25` for
    a concept without a matching term) is 0.0. Scores therefore order the
    hits of one query and are not comparable across queries. `vector` and
    `hybrid` modes rank by similarity and return up to `limit` concepts
    whether or not anything matches the query; `bm25` returns only
    concepts that share a term with it.

    Each hit's concept carries a `provenance` record: `source` is
    `evs_index`, `servedBy` is `index` and `retrievedAt` is when the concept
    was indexed; it has no `sourceUri` or `upstream`, which only a live read
    carries. A search with no hit carries the `provenance` itself.
    `truncation` is `{occurred: false}` unless `limit` left scored concepts
    out; it then names the `results` bound and how many were `omitted`,
    `exact` where that is a count and not a lower bound."""
    query, limit, normalized_mode = validate_search(query, limit, mode)
    hits, total, manifest = context.index.search_page(
        query, context.embedding_provider, limit=limit, mode=normalized_mode
    )
    result: dict[str, Any] = {
        "query": query,
        "mode": normalized_mode,
        "hits": [hit.to_dict(None, include_raw=include_raw) for hit in hits],
        "truncation": results_truncation(total, limit).to_dict(),
    }
    # A result with no item has none to carry the provenance (M3.2).
    if not hits:
        result["provenance"] = manifest.provenance().to_dict()
    return result


def _concept_uri(context: Context, code: str, pinned_terminology: str) -> str:
    """The URL of the concept in the release that the terminology segment pins."""

    return context.evs.uri(concept_path(pinned_terminology, code))


def lookup(
    context: Context, code: str, live_only: bool = False, include_raw: bool = False
) -> dict[str, Any]:
    """Look up one NCIt concept by code (C followed by digits, no leading zero) in live EVS.

    The request is pinned to the current release of the configured channel
    (`NCI_SIP_RELEASE_CHANNEL`, monthly by default), resolved afresh for this call and
    named by the concept's `provenance.release`; a live answer has
    `provenance.source: evs_rest` and `servedBy: live`, and
    `provenance.upstream` holds the terminology and version EVS gave it. A
    code that release does not contain returns `not_found`. If EVS cannot
    be reached and the concept is in the local index, it is served from
    there instead, with `source: evs_index`, `servedBy: index`, no
    `sourceUri` or `upstream`, and a `fallback` object giving the reason;
    otherwise the call fails with
    `upstream_unavailable`.

    When the local index holds a different release than the current
    one, the call fails with `release_mismatch` for every code, so that
    results from two releases are never mixed. `live_only=true` skips both
    that check and the fallback."""

    code = validate_ncit_code(code)
    manifest, cached = (None, None) if live_only else context.index.get_concept_snapshot(code)
    try:
        release = _release(context)
        if manifest and manifest.release_version != release.version:
            raise PlatformError(
                "release_mismatch",
                f"The current {release.channel} release is {release.version} but the active "
                f"index holds {manifest.release_version}. Build the current release with "
                "`index-build` and activate its passing build with `index-activate`, "
                "or use live_only to read live EVS without consulting the index.",
                requested=release.version,
                served=[manifest.release_version],
                source="index",
            )
        raw = context.evs.get_concept(code, release=release)
    except UpstreamUnavailableError as exc:
        if not cached:
            raise
        emit(
            logger,
            logging.WARNING,
            "lookup_cache_fallback",
            code=code,
            errorType=type(exc).__name__,
        )
        result = cached.to_dict(None, include_raw=include_raw)
        # Fixed wording: upstream text would reach the client verbatim.
        result["fallback"] = {
            "reason": "upstream_unavailable",
            "message": f"Live EVS failed ({type(exc).__name__}); this is the cached copy.",
        }
        return result

    concept = normalize_concept(raw, release_date=release.date, source="live_evs")
    uri = _concept_uri(context, concept.code, release.pinned_terminology)
    return concept.to_dict(uri, include_raw=include_raw)


def traverse(
    context: Context,
    start_codes: list[str],
    direction: Direction = "out",
    max_depth: int = DEFAULT_MAX_DEPTH,
    max_nodes: int = DEFAULT_MAX_NODES,
    max_edges: int = DEFAULT_MAX_EDGES,
    include_hierarchy: bool = True,
    include_roles: bool = True,
    include_associations: bool = True,
    relationship_names: list[str] | None = None,
    edge_types: list[EdgeType] | None = None,
    budget_per_kind: int | None = None,
) -> dict[str, Any]:
    """Walk NCIt relationships breadth-first from the start codes, in live EVS.

    `direction` `out` follows `child`, `role` and `association` edges, `in`
    follows `parent`, `inverse_role` and `inverse_association` edges, and
    `both` follows all six. The include flags switch hierarchy, role and
    association edges off. `edge_types` narrows the walk to the listed
    types; naming a type that the direction or the include flags exclude
    is an `invalid_request`. It is also the only way to get `descendant`
    edges (direction `out` or `both`, hierarchy included), which link each
    start code directly to every descendant that EVS places within
    `max_depth` levels. EVS gives a descendant one level, which can be
    deeper than its shortest path, so `descendant` edges can miss concepts
    that a `child` walk of the same depth reaches: up to a few percent at
    depth 2, and up to a third at depth 3 or 4, depending on the concept.
    Use `child` edges when every concept within `max_depth` is needed.
    `relationship_names` filters role and association edges by name, ignoring
    case, for example `Disease_Has_Finding`. Hierarchy edges remain included
    and carry no invented relationship name.

    Limits are clamped to depth 4, 1,000 nodes and 5,000 edges, and the
    result reports the effective `max_depth`, `max_nodes` and `max_edges`.
    `budget_per_kind` optionally limits new nodes per relationship kind,
    clamped to 1,000. Kinds take turns across the whole frontier at each
    depth; starts count against the global node limit, and edges to
    existing nodes spend no kind allowance. Mixed-kind truncation includes
    `perKind` records. A traversal shares 200 HTTP attempts across release
    discovery, retries and split batches. Exhaustion before any graph is
    available returns `bound_exceeded`; otherwise it returns a partial graph
    with `requests` truncation unless an earlier bound already dropped something.
    An exhausted kind uses `kind_budget`. Unread kinds report a lower-bound
    omitted count of zero with `exact: false` when their relation count is unknown.
    Nearer nodes claim the limits before farther ones. `truncation` is
    `{occurred: false}` unless something was dropped. It then names the
    first `bound` that still omits something: `depth`, `nodes`, `edges`, `kind_budget`,
    `requests`, or `upstream_cap`, when the relations or descendants of a concept
    were too large for the EVS response limit to read, which raising the
    node and edge limits does not help (for descendants, a smaller
    `max_depth` can). `limit` is that bound's value, `reached` what had
    been counted, and `omitted` a lower bound on nodes, edges or unread work
    left out; `exact` is false, since what lies beyond a dropped
    item was never read. A bounded final-frontier check of selected relation lists
    reports `depth` only when unseen targets remain, counting distinct targets
    one level further. Leaves and cycles to returned nodes are complete.
    The check spends the same request budget; an earlier bound still wins.
    A reported global node cut skips it; kinds already truncated are excluded.
    Descendant checks use final child lists. Inverse kinds never read final lists
    only to count continuation: each selected inverse kind at a nonempty frontier reports
    depth with omitted=0 and exact=false, without claiming a leaf or continuation.
    Every edge connects two nodes of the result. Every node and edge
    carries a `provenance` record: the release of the configured channel all data is read
    from, and how the item was reached: its `depth` (an edge has that of
    the node it reaches), and for any item but the start codes the
    `relationship` `{kind, code?, name?}` (a role or association has a
    name and a code when upstream supplies one, a hierarchy link only its kind),
    the `direction` in which that edge type is followed and the `polarity`,
    `negative` for configured NCIt exclusion roles by code (R135 to R142 by
    default). A start code that release does not contain returns `not_found`."""
    validate_kind_budget(budget_per_kind)
    start_codes, normalized_direction, selected_types, relationship_names = validate_traversal(
        start_codes,
        direction,
        max_depth,
        max_nodes,
        max_edges,
        edge_types,
        relationship_names,
    )
    selected = select_edge_types(
        normalized_direction, include_hierarchy, include_roles, include_associations, selected_types
    )
    budget = Budget(depth=max_depth, nodes=max_nodes, edges=max_edges, per_kind=budget_per_kind)
    with budgeted(budget):
        return traverse_ncit(
            context.evs,
            start_codes,
            _release(context),
            selected,
            budget,
            exclusions=exclusion_codes(context.settings, "ncit"),
            relationship_names=relationship_names,
        ).to_dict()


def evaluate(context: Context, build_id: str | None = None) -> dict[str, Any]:
    """Score the versioned queries on a candidate or the active build; gate production only."""
    index = indexing.open_index(context.settings.data_dir)
    if build_id is None:
        active = index.get_active_manifest()
        if active is None:
            raise NoActiveIndexError("No active NCIt index is available")
        build_id = active.build_id
    dataset = production_set()
    codes = {code for gold in dataset.queries for code in gold.expected_codes}
    manifest, missing = index.evaluation_inputs(build_id, codes)
    if manifest.build_kind not in {"sample", "legacy"}:
        evaluated = evaluate_build(index, context.embedding_provider, dataset, build_id)
        return cast("dict[str, Any]", evaluated.evaluation_report)
    results = evaluate_retrieval(
        index, context.embedding_provider, dataset.queries, build_id=build_id
    )
    return {
        "build_id": build_id,
        "evaluation_version": dataset.version,
        "gate_applies": False,
        "results": [result.to_dict() for result in results],
        "gold_codes_not_indexed": missing,
    }


def concept_resource(context: Context, release: str, code: str) -> dict[str, Any]:
    """One NCIt concept pinned to release, with all get_concept content sections."""
    return get_concept(context, "ncit", code, release, include=list(get_args(ConceptInclude)))


def release_resource(context: Context, version: str) -> dict[str, Any]:
    """One served NCIt version with its upstream channel, date, alternatives and provenance."""
    rows = context.evs.get_terminologies()
    selected = served_evs_release(rows, "ncit", version, context.settings.release_channel).to_dict()
    return selected | {
        "alternatives": _alternatives(rows, selected),
        "provenance": _release_provenance(context, selected),
    }


def index_resource(context: Context, release: str) -> dict[str, Any]:
    """The active NCIt index manifest, only when it holds the requested release."""
    active = context.index.get_active_manifest()
    if active is None:
        raise PlatformError(
            "capability_unavailable",
            "No active NCIt index is available. Run index-build and index-activate first.",
            capability="NCIt index",
        )
    require_index_release(active, release)
    return active.to_result()
