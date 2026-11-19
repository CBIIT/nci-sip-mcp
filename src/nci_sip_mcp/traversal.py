"""Breadth-first NCIt traversal bounded by depth, node count and edge count.

The walk reads live EVS level by level in batched concept requests, so its cost
grows with the number of nodes expanded rather than with the number of edges.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field, replace
from itertools import batched, zip_longest
from typing import Any

from .audit import emit
from .bounds import (
    Budget,
    CallDeadlineError,
    RequestBudgetError,
    check_deadline,
    current_deadline,
)
from .catalogue import polarity
from .errors import InputValidationError, call_correlation_id
from .evs import (
    EVSClient,
    EVSNotFoundError,
    EVSResponseError,
    concept_path,
    object_list,
)
from .http_client import UpstreamTooLargeError
from .models import (
    TraversalEdge,
    TraversalNode,
    TraversalProvenance,
    TraversalResult,
    Truncation,
    attribution_of,
    release_ref,
    upstream_origin,
    utc_now_iso,
)
from .release import ReleaseContext

# Concepts per EVS request. Inverse relations of hub concepts run to megabytes
# each (3.4 MB for the subset concept C116977 in release 26.09d), so walks that
# follow inverse roles or inverse associations use small batches to stay under
# the response-size limit.
BATCH_SIZE = 50
INVERSE_BATCH_SIZE = 10

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class EdgeKind:
    """How one edge type is read from EVS and reported."""

    list_key: str  # the relation list in an EVS concept payload
    direction: str  # "out" or "in": the way the edge is followed, as its provenance states
    include_group: str  # the include flag that makes it available
    hierarchy: bool  # a parent/child link: it has no relationship name and names its target `code`


# Edge types in the order they are followed. `descendant` uses its own endpoint, and
# the final-depth check needs only children.
EDGE_KINDS: dict[str, EdgeKind] = {
    "child": EdgeKind("children", "out", "hierarchy", True),
    "descendant": EdgeKind("children", "out", "hierarchy", True),
    "parent": EdgeKind("parents", "in", "hierarchy", True),
    "role": EdgeKind("roles", "out", "roles", False),
    "inverse_role": EdgeKind("inverseRoles", "in", "roles", False),
    "association": EdgeKind("associations", "out", "associations", False),
    "inverse_association": EdgeKind("inverseAssociations", "in", "associations", False),
}


def _available_edge_types(direction: str, included: dict[str, bool]) -> list[str]:
    return [
        edge_type
        for edge_type, kind in EDGE_KINDS.items()
        if included[kind.include_group] and direction in (kind.direction, "both")
    ]


def _default_edge_types(available: list[str]) -> list[str]:
    selected = [edge_type for edge_type in available if edge_type != "descendant"]
    if not selected:
        raise InputValidationError(
            "The include flags leave no edge type to traverse", "include_hierarchy"
        )
    return selected


def select_edge_types(
    direction: str,
    include_hierarchy: bool,
    include_roles: bool,
    include_associations: bool,
    edge_types: Iterable[str] | None,
) -> list[str]:
    """Resolve the direction, include flags and explicit edge types to follow.

    `descendant` is followed only when named in `edge_types`, because `child`
    edges already reach those concepts within the depth limit.
    """

    included = {
        "hierarchy": include_hierarchy,
        "roles": include_roles,
        "associations": include_associations,
    }
    available = _available_edge_types(direction, included)
    requested = set(edge_types or [])
    if not requested:
        return _default_edge_types(available)
    excluded = sorted(requested - set(available))
    if excluded:
        raise InputValidationError(
            f"Edge types {', '.join(excluded)} are excluded by direction "
            f"'{direction}' and the include flags",
            "edge_types",
        )
    return [edge_type for edge_type in available if edge_type in requested]


def _fetch_batch(
    client: EVSClient, batch: list[str], release: ReleaseContext, include: str
) -> Iterator[tuple[list[dict[str, Any]], list[str], list[str]]]:
    """Fetch one batch, halving it while the response is too large.

    Each yielded chunk names its concepts, oversized codes and requested codes.
    Oversized concepts come back with their minimal payload only. Yielding each
    successful half preserves it if a later request exhausts the budget.
    """

    try:
        yield (
            client.get_concepts_by_codes(batch, release=release, include=include),
            [],
            batch,
        )
        return
    except UpstreamTooLargeError as exc:
        emit(
            logger,
            logging.INFO,
            "traverse_batch_too_large",
            concepts=len(batch),
            errorType=type(exc).__name__,
        )
        if len(batch) == 1:
            # Relations are already lost even if the minimal fallback cannot be sent.
            yield [], list(batch), []
            minimal = client.get_concepts_by_codes(batch, release=release, include="minimal")
            yield minimal, [], batch
            return
    middle = len(batch) // 2
    yield from _fetch_batch(client, batch[:middle], release, include)
    yield from _fetch_batch(client, batch[middle:], release, include)


def _fetch_concepts(
    client: EVSClient,
    codes: list[str],
    release: ReleaseContext,
    include: str,
    batch_size: int,
) -> Iterator[tuple[dict[str, dict[str, Any]], list[str], list[str]]]:
    """Fetch concepts with their relations, pinned to and verified against the release.

    Yields each successful chunk by code, missing codes and oversized codes.
    Processing each chunk before the next request preserves limit chronology.
    """

    try:
        for batch in batched(codes, batch_size, strict=False):
            for concepts, too_large, requested in _fetch_batch(
                client, list(batch), release, include
            ):
                found = _by_code(concepts)
                if not found.keys() <= set(requested):
                    raise EVSResponseError("EVS returned a graph concept that was not requested")
                yield found, [code for code in requested if code not in found], too_large
    except RequestBudgetError:
        # Preserve earlier batches, including the successful half of a split batch.
        pass


# What the walk reads of a concept (node fields) and of a relation item (the target, its
# provenance). Anything else EVS sends is dropped as the batch is extracted, so a frontier
# of hub concepts does not keep megabytes of relation payload while its kinds are followed.
# A malformed relation list therefore fails here, at fetch time, not when a kind reads it.
_NODE_FIELDS = ("code", "name", "active", "conceptStatus", "terminology", "version", "licenseText")
_ITEM_FIELDS = (
    "code",
    "name",
    "relatedCode",
    "relatedName",
    "type",
    "qualifiers",
    "evidence",
    "licenseText",
)
_LIST_KEYS = tuple(dict.fromkeys(kind.list_key for kind in EDGE_KINDS.values()))


def _slim_item(item: dict[str, Any]) -> dict[str, Any]:
    return {key: item[key] for key in _ITEM_FIELDS if key in item}


def _slim(raw: dict[str, Any]) -> dict[str, Any]:
    slim = {key: raw[key] for key in _NODE_FIELDS if key in raw}
    for list_key in _LIST_KEYS:
        if list_key in raw:
            slim[list_key] = [_slim_item(item) for item in object_list(raw, list_key)]
    return slim


def _by_code(concepts: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(raw.get("code") or ""): _slim(raw) for raw in concepts}


def _rotate(
    groups: Iterable[list[tuple[str, str, dict[str, Any]]]],
) -> list[tuple[str, str, dict[str, Any]]]:
    return [item for row in zip_longest(*groups) for item in row if item is not None]


def _level(item: dict[str, Any], depth_limit: int) -> int:
    level = item.get("level")
    if not isinstance(level, int) or isinstance(level, bool) or not 1 <= level <= depth_limit:
        raise EVSResponseError(
            f"EVS returned a descendant at level {level!r}; expected 1 to {depth_limit}"
        )
    return level


def _relationship(edge_type: str, item: dict[str, Any]) -> dict[str, str]:
    """The relationship that brought a relation item in: its kind and its name (the record
    requires one; a hierarchy link's is empty) and, for a role or an association, its code
    where upstream gave one. No code is invented."""

    name = _relationship_name(edge_type, item)
    if EDGE_KINDS[edge_type].hierarchy:
        return {"kind": edge_type, "name": name}
    relationship = {"kind": "role" if "role" in edge_type else "association", "name": name}
    code = item.get("code")
    return relationship | ({"code": str(code)} if code else {})


def _target_code(code: str, edge_type: str, item: dict[str, Any]) -> str:
    # A role or association item names its target in `relatedCode`; its own
    # `code` is that of the relationship.
    target_key = "code" if EDGE_KINDS[edge_type].hierarchy else "relatedCode"
    target = str(item.get(target_key) or "")
    if not target:
        raise EVSResponseError(
            f"EVS returned a {edge_type} relation of {code} without a {target_key}"
        )
    return target


def _relationship_name(edge_type: str, item: dict[str, Any]) -> str:
    if EDGE_KINDS[edge_type].hierarchy:
        return ""
    return str(item.get("type") or edge_type)


def _edge(
    code: str,
    source_name: str,
    edge_type: str,
    item: dict[str, Any],
    provenance: TraversalProvenance,
) -> TraversalEdge:
    """Build the edge that a relation item of concept `code` stands for."""

    target = _target_code(code, edge_type, item)
    name_key = "name" if EDGE_KINDS[edge_type].hierarchy else "relatedName"
    return TraversalEdge(
        source_code=code,
        target_code=target,
        edge_type=edge_type,
        relationship_name=_relationship_name(edge_type, item),
        provenance=provenance,
        target_name=str(item.get(name_key) or ""),
        source_name=source_name,
    )


def _missing_concepts(release: ReleaseContext, missing: list[str], depth: int) -> Exception:
    codes = ", ".join(missing)
    if depth == 0:
        return EVSNotFoundError(
            f"NCIt release {release.version} has no concept {codes}", identifiers=missing
        )
    return EVSResponseError(
        f"EVS relations in release {release.version} refer to {codes}, which it does not serve"
    )


def _node_payloads(concepts: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    # Retain status, not the relation lists already processed.
    return {
        code: {key: raw[key] for key in _NODE_FIELDS if key in raw}
        for code, raw in concepts.items()
    }


def _edge_key(edge: TraversalEdge) -> tuple[str, str, str, str]:
    # Names can collide, including across positive and negative relationship codes.
    identity = json.dumps(
        [(edge.provenance.relationship or {}).get("code"), edge.relationship_name]
    )
    return edge.source_code, edge.target_code, edge.edge_type, identity


@dataclass
class _Walk:
    """One traversal in progress: its limits and what it has emitted so far."""

    client: EVSClient
    release: ReleaseContext
    edge_types: list[str]
    budget: Budget
    name_filter: set[str]
    starts: set[str]
    exclusions: frozenset[str]
    include_negative: bool = True
    expandable: set[str] = field(default_factory=set)
    retrieved_at: str = field(default_factory=utc_now_iso)
    correlation_id: str = field(default_factory=call_correlation_id)
    nodes: dict[str, TraversalNode] = field(default_factory=dict)
    concepts: dict[str, dict[str, Any]] = field(default_factory=dict)
    deadline_exhausted: bool = False
    depth_omitted: dict[str, set[str]] = field(default_factory=dict)
    edges: list[TraversalEdge] = field(default_factory=list)
    seen_edges: set[tuple[str, str, str, str]] = field(default_factory=set)
    unexpanded: list[str] = field(default_factory=list)
    dropped_by_kind: dict[str, dict[tuple[str, str, str, str], TraversalEdge]] = field(
        default_factory=dict
    )
    # Insertion order is the chronology of unresolved omissions. Four-part keys
    # identify dropped edges; one-part keys identify a kind whose lists went unread.
    bounds_reached: dict[tuple[str, ...], str] = field(default_factory=dict)
    # Descendants of the start codes by level; level n is emitted with the
    # other edges that reach depth n.
    descendants: dict[int, list[tuple[str, dict[str, Any]]]] = field(default_factory=dict)
    # Edge types whose relations come with the concept payload; descendants
    # have their own request.
    payload_types: list[str] = field(init=False)
    batch_size: int = field(init=False)

    def __post_init__(self) -> None:
        self.expandable.update(self.starts)
        self.payload_types = [
            edge_type for edge_type in self.edge_types if edge_type != "descendant"
        ]
        inverse = any(edge_type.startswith("inverse") for edge_type in self.edge_types)
        self.batch_size = INVERSE_BATCH_SIZE if inverse else BATCH_SIZE

    def _provenance(
        self,
        depth: int,
        uri: str,
        edge_type: str | None = None,
        item: dict[str, Any] | None = None,
        upstream: dict[str, Any] | None = None,
    ) -> TraversalProvenance:
        """The provenance of an item at `depth`, read from `uri`; `edge_type` and `item` name
        the relation that brought it in, and the concept asked about has none."""

        item = item or {}
        relationship = _relationship(edge_type, item) if edge_type else None
        return TraversalProvenance(
            release=release_ref(self.release.terminology, self.release.version, self.release.date),
            source="evs_rest",
            served_by="live",
            retrieved_at=self.retrieved_at,
            correlation_id=self.correlation_id,
            source_uri=uri,
            upstream=upstream,
            attribution=attribution_of(item, "evs"),
            depth=depth,
            relationship=relationship,
            direction=EDGE_KINDS[edge_type].direction if edge_type else None,
            polarity=polarity(relationship.get("code"), self.exclusions) if relationship else None,
            qualifiers=item.get("qualifiers"),
            evidence=item.get("evidence"),
        )

    def _concept_uri(self, code: str) -> str:
        return self.client.uri(concept_path(self.release.pinned_terminology, code))

    def _node(self, code: str, name: str, provenance: TraversalProvenance) -> TraversalNode:
        return TraversalNode(
            code=code,
            preferred_name=name,
            terminology=self.release.terminology,
            provenance=provenance,
        )

    def _mark_unexpanded(self, codes: Iterable[str], kinds: Iterable[str]) -> None:
        self.unexpanded += [code for code in codes if code not in self.unexpanded]
        self._mark_unread(kinds, "upstream_cap")

    def _mark_unread(self, kinds: Iterable[str], bound: str) -> None:
        for kind in kinds:
            self.bounds_reached.setdefault((kind,), bound)

    def _mark_unread_starts(
        self, frontier: list[str], concepts: dict[str, dict[str, Any]], depth: int
    ) -> None:
        # A start code the budget left unread is missing from the nodes whichever edge types
        # were selected, so every kind reports it. Only a depth claim, which was made without
        # the unread starts, gives way; an earlier real bound remains the one reported.
        if depth != 0 or set(frontier) <= concepts.keys():
            return
        for kind in self.edge_types:
            if self.bounds_reached.get((kind,), "depth") == "depth":
                self.bounds_reached[(kind,)] = "requests"

    def truncation(self) -> Truncation:
        """The record of the first bound that dropped anything, counting what it dropped.

        The concepts and relations beyond a dropped one were never read, so the count is a
        lower bound, and `exact` is False.
        """

        record = self._bound_record()
        if record.occurred and len(self.edge_types) > 1:
            return replace(
                record, per_kind={kind: self._bound_record(kind) for kind in self.edge_types}
            )
        return record

    def _bound_record(self, kind: str | None = None) -> Truncation:
        first = next(
            (key for key in self.bounds_reached if self._reportable(key, kind)),
            None,
        )
        if first is None:
            return Truncation(occurred=False)
        bound = self.bounds_reached[first]
        first_kind = self._bound_kind(first)
        limit, reached, omitted = self._figures(bound, kind, first_kind)
        return Truncation(
            occurred=True,
            bound=bound,
            limit=limit,
            reached=reached,
            omitted=omitted,
            exact=False,
        )

    def _figures(self, bound: str, kind: str | None, first_kind: str) -> tuple[int, int, int]:
        """The limit, the amount reached and the count dropped, computed for `bound` only."""

        if bound == "deadline" and (deadline := current_deadline()):
            return deadline.figures()
        figures: dict[str, Callable[[], tuple[int, int, int]]] = {
            "depth": lambda: (self.budget.depth, self.budget.depth, self._depth_count(kind)),
            "nodes": lambda: (
                self.budget.nodes,
                len(self.nodes),
                self._kind_omitted(kind, "nodes"),
            ),
            "edges": lambda: (
                self.budget.edges,
                len(self.edges),
                self._kind_omitted(kind, "edges"),
            ),
            # The bound is recorded only while per_kind is set (bounds.py), so 0 never shows.
            "kind_budget": lambda: (
                self.budget.per_kind or 0,
                self.budget.added_by_kind[first_kind],
                self._kind_omitted(first_kind, "kind_budget"),
            ),
            "requests": lambda: (self.budget.requests, self.budget.attempts, int(kind is None)),
            "upstream_cap": lambda: (
                self.client.max_response_bytes,
                self.client.max_response_bytes,
                len(self.unexpanded) if kind is None else 0,
            ),
        }
        return figures[bound]()

    def _reportable(self, key: tuple[str, ...], kind: str | None) -> bool:
        return (kind is None or self._bound_kind(key) == kind) and (
            not self.budget.paged or self.bounds_reached[key] != "nodes"
        )

    def _depth_count(self, kind: str | None) -> int:
        if kind is not None:
            return len(self.depth_omitted.get(kind, set()))
        return len(set().union(*self.depth_omitted.values()))

    @staticmethod
    def _bound_kind(key: tuple[str, ...]) -> str:
        return key[0] if len(key) == 1 else key[2]

    def _kind_omitted(self, kind: str | None, bound: str) -> int:
        dropped = (
            self.dropped_by_kind.get(kind, {})
            if kind is not None
            else {
                key: edge for group in self.dropped_by_kind.values() for key, edge in group.items()
            }
        )
        if bound == "edges":
            return sum(self.bounds_reached[key] == "edges" for key in dropped)
        return len({edge.target_code for edge in dropped.values()} - self.nodes.keys())

    def _depth_types(self, frontier: list[str]) -> list[str]:
        # A depth check cannot improve a bound already reported. An exactly full
        # result with no dropped node still needs the check to distinguish a leaf.
        if "nodes" in self.bounds_reached.values():
            return []
        truncated = {self._bound_kind(key) for key in self.bounds_reached}
        kinds = [kind for kind in self.edge_types if kind not in truncated]
        self._mark_inverse_depth(frontier, kinds)
        return [kind for kind in kinds if not kind.startswith("inverse")]

    def _mark_inverse_depth(self, frontier: list[str], kinds: list[str]) -> None:
        # Hub inverse lists can be megabytes each. Do not fetch them solely to
        # count continuation: zero is an explicit lower bound, not a leaf claim.
        for kind in kinds:
            if kind.startswith("inverse") and frontier:
                self.bounds_reached.setdefault((kind,), "depth")

    @staticmethod
    def _needs_fetch(frontier: list[str], depth: int, kinds: list[str]) -> bool:
        # Always verify the starts, including a depth-zero walk.
        return depth == 0 or bool(kinds and frontier)

    def fetch(self, frontier: list[str], depth: int) -> dict[str, dict[str, Any]]:
        """Read the concepts of the frontier, with the relations to follow from them."""

        kinds = self._depth_types(frontier) if depth == self.budget.depth else self.payload_types
        if not self._needs_fetch(frontier, depth, kinds):
            return {}
        relations = list(dict.fromkeys(EDGE_KINDS[edge_type].list_key for edge_type in kinds))
        include = ",".join(["minimal", *relations])
        concepts: dict[str, dict[str, Any]] = {}
        try:
            for found, missing, oversized in _fetch_concepts(
                self.client, frontier, self.release, include, self.batch_size
            ):
                self._checked_chunk(found, missing, oversized, depth, kinds)
                concepts.update(found)
        except CallDeadlineError:
            # Retain verified initial batches before deciding whether a partial graph exists.
            self.concepts.update(_node_payloads(concepts))
            self.start(frontier, concepts, depth)
            raise
        if self.budget.exhausted:
            self._mark_unread(kinds, "requests")
        self._mark_unread_starts(frontier, concepts, depth)
        self.concepts.update(_node_payloads(concepts))
        return concepts

    def _checked_chunk(
        self,
        found: dict[str, dict[str, Any]],
        missing: list[str],
        oversized: list[str],
        depth: int,
        kinds: list[str],
    ) -> None:
        if missing:
            raise _missing_concepts(self.release, missing, depth)
        if oversized:
            # Their relation lists are absent, so no depth claim is made from them.
            emit(
                logger,
                logging.WARNING,
                "traverse_relations_too_large",
                codes=oversized,
                bound="NCI_SIP_EVS_MAX_RESPONSE_BYTES",
            )
            self._mark_unexpanded(oversized, kinds)
        if depth == self.budget.depth:
            self._check_depth(found, kinds)

    def _check_depth(self, concepts: dict[str, dict[str, Any]], kinds: list[str]) -> None:
        """Note each kind that has an unseen target one level beyond the last frontier."""

        for code, kind, item in self._payload_relations(list(concepts), concepts, kinds):
            check_deadline()
            target = _target_code(code, kind, item)
            if (
                target in self.nodes
                or target in self.starts
                or self._name_excluded(_relationship_name(kind, item))
            ):
                continue
            self.depth_omitted.setdefault(kind, set()).add(target)
            self.bounds_reached.setdefault((kind,), "depth")

    def start(
        self, start_codes: list[str], concepts: dict[str, dict[str, Any]], depth: int = 0
    ) -> None:
        """Emit the start nodes and read their descendants when those are followed."""

        if depth != 0:
            return
        for code in start_codes:
            if code not in concepts:
                continue
            raw = concepts[code]
            # A start code was read in full, so EVS said where it comes from; a node reached
            # through a relation list was named there and nothing more.
            provenance = self._provenance(0, self._concept_uri(code), upstream=upstream_origin(raw))
            self.nodes[code] = self._node(code, str(raw.get("name") or ""), provenance)
        if self.budget.depth > 0 and "descendant" in self.edge_types:
            self._read_descendants(list(self.nodes))

    def _mark_deadline(self) -> None:
        self._mark_unread(self.edge_types, "deadline")

    def _read_descendants(self, start_codes: list[str]) -> None:
        for code in start_codes:
            try:
                items = self.client.get_descendants(code, self.budget.depth, release=self.release)
            except UpstreamTooLargeError as exc:
                emit(
                    logger,
                    logging.WARNING,
                    "traverse_descendants_too_large",
                    code=code,
                    errorType=type(exc).__name__,
                )
                self._mark_unexpanded([code], ["descendant"])
                continue
            except RequestBudgetError:
                self._mark_unread(["descendant"], "requests")
                break
            for item in items:
                check_deadline()
                level = _level(item, self.budget.depth)
                self.descendants.setdefault(level, []).append((code, item))

    def _found(
        self, frontier: list[str], concepts: dict[str, dict[str, Any]], depth: int
    ) -> list[tuple[str, str, dict[str, Any]]]:
        """The relation items that lead from the frontier to depth + 1."""

        found = self._payload_relations(frontier, concepts, self.payload_types)
        found += [(code, "descendant", item) for code, item in self.descendants.get(depth + 1, [])]
        grouped: dict[str, list[tuple[str, str, dict[str, Any]]]] = {
            kind: [] for kind in self.edge_types
        }
        for item in found:
            check_deadline()
            grouped[item[1]].append(item)
        return _rotate(grouped.values())

    @staticmethod
    def _payload_relations(
        frontier: list[str], concepts: dict[str, dict[str, Any]], kinds: list[str]
    ) -> list[tuple[str, str, dict[str, Any]]]:
        return [
            (code, edge_type, item)
            for code in frontier
            for edge_type in kinds
            for item in object_list(concepts.get(code, {}), EDGE_KINDS[edge_type].list_key)
        ]

    def _skips(self, edge: TraversalEdge, key: tuple[str, str, str, str]) -> bool:
        """Whether the name filter excludes the edge or it was emitted before."""

        return key in self.seen_edges or self._name_excluded(edge.relationship_name)

    def _name_excluded(self, relationship_name: str) -> bool:
        """Whether the relationship-name filter excludes this name; hierarchy has none."""

        return bool(
            self.name_filter
            and relationship_name
            and relationship_name.lower() not in self.name_filter
        )

    def _add(self, edge: TraversalEdge) -> bool:
        """Emit an allowed edge; return whether its target newly qualifies for expansion."""

        key = _edge_key(edge)
        if self._skips(edge, key):
            return False
        new_node = edge.target_code not in self.nodes
        if self._drops(edge, key, new_node):
            return False
        self.seen_edges.add(key)
        self.edges.append(edge)
        if new_node:
            self.budget.added_by_kind[edge.edge_type] += 1
            held = replace(
                edge.provenance, source_uri=self._concept_uri(edge.target_code), attribution=None
            )
            self.nodes[edge.target_code] = self._node(edge.target_code, edge.target_name, held)
        eligible = self.include_negative or edge.provenance.polarity != "negative"
        if eligible and edge.target_code not in self.expandable:
            self.expandable.add(edge.target_code)
            return True
        return False

    def _drops(self, edge: TraversalEdge, key: tuple[str, str, str, str], new: bool) -> bool:
        bound = self.budget.node_bound(len(self.nodes), edge.edge_type) if new else None
        if bound is None and len(self.edges) >= self.budget.edges:
            bound = "edges"
        if bound is None:
            return False
        self.dropped_by_kind.setdefault(edge.edge_type, {}).setdefault(key, edge)
        if not new and self.bounds_reached.get(key) in {"nodes", "kind_budget"}:
            del self.bounds_reached[key]
        self.bounds_reached.setdefault(key, bound)
        return True

    def _reconcile_edges(self) -> list[str]:
        """Another kind may have admitted a target whose earlier edge hit its kind budget."""

        reached = []
        for dropped in self.dropped_by_kind.values():
            for key, edge in list(dropped.items()):
                check_deadline()
                if self._add(edge):
                    reached.append(edge.target_code)
                if key in self.seen_edges:
                    del dropped[key]
                    self.bounds_reached.pop(key)
        return reached

    def stopped(self, frontier: list[str], depth: int) -> bool:
        if self.budget.exhausted:
            bound = "requests"
        elif "edges" in self.bounds_reached.values():
            bound = "edges"
        else:
            return False
        self._mark_unread(self._remaining_kinds(frontier, depth), bound)
        return True

    def _remaining_kinds(self, frontier: list[str], depth: int) -> list[str]:
        kinds = list(self.payload_types) if frontier else []
        final_descendants = (
            frontier and depth + 1 == self.budget.depth and "descendant" in self.edge_types
        )
        if final_descendants or any(level > depth + 1 for level in self.descendants):
            kinds.append("descendant")
        return kinds

    def _edge_uri(self, code: str, edge_type: str) -> str:
        """The URL that holds the relation: the concept's, or its descendants'."""

        uri = self._concept_uri(code)
        return f"{uri}/descendants" if edge_type == "descendant" else uri

    def follow(
        self, frontier: list[str], concepts: dict[str, dict[str, Any]], depth: int
    ) -> list[str]:
        """Emit frontier edges and return newly eligible expansion targets."""

        reached: list[str] = []
        for code, edge_type, item in self._found(frontier, concepts, depth):
            check_deadline()
            provenance = self._provenance(
                depth + 1, self._edge_uri(code, edge_type), edge_type, item
            )
            edge = _edge(code, self.nodes[code].preferred_name, edge_type, item, provenance)
            if self._add(edge):
                reached.append(edge.target_code)
        reached.extend(self._reconcile_edges())
        return reached

    def result(self, start_codes: list[str]) -> TraversalResult:
        return TraversalResult(
            start_codes=start_codes,
            nodes=list(self.nodes.values()),
            edges=self.edges,
            truncation=self.truncation(),
            max_depth=self.budget.depth,
            max_nodes=self.budget.nodes,
            max_edges=self.budget.edges,
            concepts=self.concepts,
            deadline_exhausted=self.deadline_exhausted,
        )


def traverse_ncit(
    client: EVSClient,
    start_codes: list[str],
    release: ReleaseContext,
    edge_types: list[str],
    budget: Budget,
    *,
    exclusions: frozenset[str],
    relationship_names: list[str] | None = None,
    include_negative: bool = True,
) -> TraversalResult:
    """Walk breadth-first from the start codes along the given edge types.

    `start_codes` is the output of `validate_traversal`, which makes them
    distinct and fit the node limit, and `edge_types` is the output of
    `select_edge_types`. The caller builds one `budget` and scopes it around
    release discovery and this walk so the HTTP client counts the same attempts.
    Every request is pinned to `release`, and each fetched concept is verified against it.

    The walk proceeds one depth at a time over all start codes together, so
    nearer nodes claim the node and edge limits before farther ones. A
    `descendant` edge runs from a start code to a descendant that EVS places
    within `budget.depth` hierarchy levels, and reaches as deep as that level.
    EVS gives a descendant one level, which can exceed its shortest path.

    An edge is emitted only when both of its nodes are. The truncation record
    names the first node, edge, kind or response-size bound that dropped something.
    Request exhaustion preserves the graph and reports requests unless an earlier
    bound dropped something; it raises RequestBudgetError when no start was read.
    The last frontier is read with the selected relation lists to detect depth cuts.
    Forward kinds count distinct unseen targets as omitted; leaves and cycles do not.
    Descendant checks use only the last frontier's children. Inverse kinds never fetch
    final lists solely to check continuation: any nonempty final frontier reports
    depth for selected inverse kinds with omitted=0 and exact=false. Continuation is unknown.
    A prior global node cut skips the check; kinds already truncated are excluded.
    """

    walk = _Walk(
        client=client,
        release=release,
        edge_types=edge_types,
        budget=budget,
        name_filter={name.lower() for name in relationship_names or []},
        starts=set(start_codes),
        exclusions=exclusions,
        include_negative=include_negative,
    )
    try:
        return _run_walk(walk, start_codes)
    except CallDeadlineError:
        walk.deadline_exhausted = True
        if not walk.nodes:
            raise
        walk._mark_deadline()
        return walk.result(start_codes)


def _run_walk(walk: _Walk, start_codes: list[str]) -> TraversalResult:
    frontier = start_codes
    concepts = walk.fetch(frontier, 0)
    walk.start(start_codes, concepts)
    if not walk.nodes and walk.budget.exhausted:
        raise RequestBudgetError(walk.budget.requests, walk.budget.attempts)
    for depth in range(walk.budget.depth):
        check_deadline()
        frontier = walk.follow(frontier, concepts, depth)
        if walk.stopped(frontier, depth):
            break
        concepts = walk.fetch(frontier, depth + 1)
    return walk.result(start_codes)
