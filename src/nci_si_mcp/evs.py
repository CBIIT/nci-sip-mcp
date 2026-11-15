"""NCI EVS REST client and normalization logic."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from http import HTTPStatus
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from .http_client import (
    HttpClient,
    UpstreamRejectedError,
)
from .models import NcitConcept, utc_now_iso
from .request_policy import (
    LICENSE_KEY_HEADER as LICENSE_KEY_HEADER,  # noqa: PLC0414 - public export.
)
from .validation import RelationshipKind, validate_text_target

if TYPE_CHECKING:
    from .release import ReleaseContext

SOURCE_VOCABULARIES = {"ncit": "NCI Thesaurus"}

# What a concept request asks EVS to include: enough to build the search text
# for indexing, and additionally every relation list for a lookup.
INDEX_INCLUDE = "summary,definitions,synonyms,properties"
LOOKUP_INCLUDE = (
    f"{INDEX_INCLUDE},parents,children,roles,inverseRoles,associations,inverseAssociations"
)


TERMINOLOGIES_PATH = "/api/v1/metadata/terminologies"


def catalogue_path(terminology: str, kind: RelationshipKind) -> str:
    return f"/api/v1/metadata/{quote(terminology, safe='')}/{kind}s"


def replacements_path(terminology: str, code: str) -> str:
    return f"/api/v1/history/{quote(terminology, safe='')}/{quote(code, safe='')}/replacements"


def concept_path(terminology: str, code: str = "") -> str:
    """The path of a concept in `terminology`, or of the terminology's concept list."""

    return f"/api/v1/concept/{quote(terminology, safe='')}" + (
        f"/{quote(code, safe='')}" if code else ""
    )


def search_request(
    terminology: str, query: str, mode: str, offset: int, limit: int, status: str | None = None
) -> tuple[str, dict[str, object]]:
    """Share the search path and parameters between admission and the actual request."""
    params: dict[str, object] = {
        "term": query,
        "type": "contains" if mode == "lexical" else "startsWith",
        "include": "minimal,highlights",
        "fromRecord": offset,
        "pageSize": limit,
    }
    if status is not None:
        params["conceptStatus"] = status
    return concept_path(terminology) + "/search", params


class EVSError(RuntimeError):
    """Base error for EVS client failures; `details` is what the error record carries."""

    def __init__(self, message: str, /, **details: Any) -> None:
        super().__init__(message)
        self.details = details


class EVSNotFoundError(EVSError):
    """The requested concept does not exist in the release that was asked for."""


class EVSResponseError(EVSError):
    """EVS returned unusable content or lacks an endpoint that must exist."""


class EVSReleaseMismatchError(EVSResponseError):
    """EVS served content of another release than the one requested."""


class EVSReleaseNotFoundError(EVSError):
    """EVS does not serve the release (terminology version) a request was pinned to."""


def _object(data: Any, what: str) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise EVSResponseError(f"EVS {what} was not an object")
    return data


def _object_list(data: Any, what: str) -> list[dict[str, Any]]:
    if not isinstance(data, list) or not all(isinstance(item, dict) for item in data):
        raise EVSResponseError(f"EVS {what} was not a list of objects")
    return data


def object_list(payload: dict[str, Any], key: str) -> list[dict[str, Any]]:
    """Return the list of objects under `key` of an EVS payload; absent means empty."""

    return _object_list(payload.get(key) or [], f"field '{key}'")


def _validate_search_page(rows: list[dict[str, Any]], total: int, offset: int, limit: int) -> None:
    if len(rows) != min(limit, max(0, total - offset)):
        raise EVSResponseError("EVS search page is incomplete or exceeds its declared total")
    seen: set[str] = set()
    for row in rows:
        code = row.get("code")
        if not isinstance(code, str) or not code or code in seen:
            raise EVSResponseError("EVS search page has a missing or duplicate concept code")
        seen.add(code)


def _empty_terminologies(data: Any) -> str | None:
    return "EVS listed no terminologies" if data == [] else None


def verify_release(concepts: Iterable[dict[str, Any]], release_version: str) -> None:
    """Fail unless every concept payload was served from the release that was requested."""

    other = {str(raw.get("version") or "unknown") for raw in concepts} - {release_version}
    if other:
        served = sorted(other)
        raise EVSReleaseMismatchError(
            "EVS served content of another release than the one requested",
            requested=release_version,
            served=served,
            source="evs",
        )


def verify_content(concepts: list[dict[str, Any]], release: ReleaseContext) -> None:
    """Check self-described concept content against the call's pinned identity."""

    verify_release(concepts, release.version)
    if any(raw.get("terminology") != release.terminology for raw in concepts):
        raise EVSResponseError("EVS returned content of another or missing terminology")


def _property_values(properties: list[dict[str, Any]], kind: str) -> list[Any]:
    return [
        item.get("value") for item in properties if item.get("type") == kind and item.get("value")
    ]


def _evidence(raw: dict[str, Any]) -> dict[str, Any]:
    """The definitions, synonyms and classifying properties of a concept payload."""

    properties = object_list(raw, "properties")
    return {
        "definitions": [
            {
                "definition": item.get("definition"),
                "type": item.get("type"),
                "source": item.get("source"),
            }
            for item in object_list(raw, "definitions")
            if item.get("definition")
        ],
        "synonyms": [
            {
                "name": item.get("name"),
                "term_type": item.get("termType"),
                "type": item.get("type"),
                "source": item.get("source"),
            }
            for item in object_list(raw, "synonyms")
            if item.get("name")
        ],
        "semantic_types": _property_values(properties, "Semantic_Type"),
        "contributing_sources": _property_values(properties, "Contributing_Source"),
    }


def normalize_concept(
    raw: dict[str, Any],
    release_date: str | None,
    source: str,
    retrieved_at: str | None = None,
) -> NcitConcept:
    terminology = str(raw.get("terminology") or "ncit")
    return NcitConcept(
        code=str(raw.get("code", "")),
        preferred_name=str(raw.get("name", "")),
        # Any other terminology keeps its identifier as the label.
        source_vocabulary=SOURCE_VOCABULARIES.get(terminology, terminology),
        terminology=terminology,
        release_version=str(raw.get("version", "")),
        release_date=release_date,
        retrieved_at=retrieved_at or utc_now_iso(),
        source=source,
        evidence=_evidence(raw),
        raw=raw,
    )


# What EVS says of a request pinned to a release it does not serve.
_UNKNOWN_TERMINOLOGY = re.compile(r"Terminology not found\s*=\s*([A-Za-z0-9._-]{1,64})(?=[\s)]|$)")


class EVSClient:
    """Read-only EVS REST client.

    Every content method requires a ReleaseContext and addresses that release.
    Full concept payloads must describe the same terminology and version.
    """

    def __init__(
        self,
        base_url: str,
        timeout_seconds: float = 30.0,
        *,
        max_attempts: int = 3,
        retry_backoff_seconds: float = 0.25,
        max_response_bytes: int = 10 * 1024 * 1024,
        license_key: str | None = None,
    ) -> None:
        self.http = HttpClient(
            base_url,
            label="EVS",
            timeout_seconds=timeout_seconds,
            max_attempts=max_attempts,
            retry_backoff_seconds=retry_backoff_seconds,
            max_response_bytes=max_response_bytes,
            size_bound="NCI_SI_EVS_MAX_RESPONSE_BYTES",
            credentials={LICENSE_KEY_HEADER: license_key} if license_key else None,
        )

    @property
    def max_response_bytes(self) -> int:
        return self.http.max_response_bytes

    def uri(self, path: str, params: dict[str, Any] | None = None) -> str:
        """The URL of `path`: what a request for it asks, for an item's provenance."""

        return self.http.url(path, params)

    def get_gdc_mapset(self, release: ReleaseContext) -> dict[str, Any]:
        """Read the unpinned mapset and verify its self-described release."""
        data = self._get_existing("/api/v1/mapset/NCIt_Maps_To_GDC")
        if not isinstance(data, dict) or data.get("code") != "NCIt_Maps_To_GDC":
            raise EVSResponseError("EVS returned a malformed GDC mapset identity")
        verify_release([data], release.version)
        return data

    def get_gdc_maps(self, code: str, offset: int = 0) -> tuple[list[dict[str, Any]], int]:
        """Read one ten-row term-search page; callers must filter exact source codes."""
        data = self._get_existing(
            "/api/v1/mapset/NCIt_Maps_To_GDC/maps",
            {"term": code, "fromRecord": offset, "pageSize": 10},
        )
        if not isinstance(data, dict):
            raise EVSResponseError("EVS returned a malformed GDC mapping page")
        total = data.get("total")
        # The recorded no-match answer omits maps and explicitly declares total zero.
        rows = _object_list(data.get("maps", [] if total == 0 else None), "GDC maps")
        if type(total) is not int or total < 0 or len(rows) != min(10, max(0, total - offset)):
            raise EVSResponseError("EVS returned an incomplete GDC mapping page")
        return rows, total

    def _get_json(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        reject: Callable[[Any], str | None] | None = None,
    ) -> Any:
        """GET a JSON document; HTTP 404 raises EVSNotFoundError.

        That means "no such concept" only for a single-concept request; the other methods go
        through `_get_existing`.
        """

        try:
            return self.http.get_json(path, params, reject=reject)
        except UpstreamRejectedError as exc:
            if exc.details.get("status") != HTTPStatus.NOT_FOUND:
                raise
            unknown = _UNKNOWN_TERMINOLOGY.search(exc.body_message)
            if unknown:
                raise EVSReleaseNotFoundError(str(exc), requested=unknown[1], source="evs") from exc
            # The shared client already replaced an echoed credential; preserve release
            # classification without interpreting any other upstream-controlled token.
            if re.search(r"Terminology not found\s*=\s*\[redacted\](?=[\s)]|$)", exc.body_message):
                raise EVSReleaseNotFoundError(
                    str(exc), requested="[redacted]", source="evs"
                ) from exc
            raise EVSNotFoundError(str(exc), **exc.details) from exc

    def _get_existing(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        reject: Callable[[Any], str | None] | None = None,
    ) -> Any:
        """GET a document that must exist, so a 404 means a wrong endpoint or release."""

        try:
            return self._get_json(path, params, reject=reject)
        except EVSNotFoundError as exc:
            raise EVSResponseError(
                f"{exc}; EVS does not serve this endpoint or release, check NCI_SI_EVS_BASE_URL",
                **exc.details,
            ) from exc

    def get_api_version(self) -> dict[str, Any]:
        return _object(self._get_existing("/api/v1/version"), "version response")

    def get_terminologies(
        self, terminology: str | None = None, *, latest: bool = False, tag: str | None = None
    ) -> list[dict[str, Any]]:
        """The terminology rows EVS lists; `latest` and `tag` select a channel's current release."""

        params = {"terminology": terminology, "latest": "true" if latest else None, "tag": tag}
        reject = None
        if terminology is None and not latest and tag is None:
            # Unfiltered EVS metadata must name at least one served terminology.
            reject = _empty_terminologies
        return _object_list(
            self._get_existing(TERMINOLOGIES_PATH, params, reject=reject),
            "terminology metadata",
        )

    def get_concepts_by_codes(
        self,
        codes: Iterable[str],
        release: ReleaseContext,
        include: str = INDEX_INCLUDE,
    ) -> list[dict[str, Any]]:
        """Fetch several concepts in one request; EVS omits codes it does not know."""

        code_list = list(codes)
        if not code_list:
            return []
        data = self._get_existing(
            concept_path(release.pinned_terminology),
            {"list": ",".join(code_list), "include": include},
        )
        concepts = _object_list(data, "concept list response")
        verify_content(concepts, release)
        return concepts

    def get_relationship_catalogue(
        self, release: ReleaseContext, kind: RelationshipKind
    ) -> list[dict[str, Any]]:
        """Read one release's roles or associations, verifying every row's identity."""

        rows = _object_list(
            self._get_existing(catalogue_path(release.pinned_terminology, kind)),
            f"{kind} catalogue",
        )
        verify_content(rows, release)
        return rows

    def get_index_page(
        self, release: ReleaseContext, offset: int
    ) -> tuple[int, list[dict[str, Any]]]:
        """Read an unfiltered, pinned search page for an operator's full index build."""
        data = _object(
            self._get_existing(
                concept_path(release.pinned_terminology) + "/search",
                {"include": INDEX_INCLUDE, "pageSize": 1000, "fromRecord": offset},
            ),
            "index search page",
        )
        total = data.get("total")
        if type(total) is not int or total < 1:
            raise EVSResponseError("EVS index search page has no positive integer total")
        concepts = _object_list(data.get("concepts"), "index search concepts")
        verify_content(concepts, release)
        return total, concepts

    def search_concepts(
        self,
        release: ReleaseContext,
        query: str,
        mode: str,
        offset: int,
        limit: int,
        status: str | None = None,
    ) -> tuple[int, list[dict[str, Any]]]:
        """Preserve the pinned platform page and validate its declared total."""
        path, params = search_request(
            release.pinned_terminology, query, mode, offset, limit, status
        )
        validate_text_target(self.uri(path, params), "query")
        data = _object(
            self._get_existing(path, params),
            "search page",
        )
        total = data.get("total")
        if type(total) is not int or total < 0:
            raise EVSResponseError("EVS search page has no nonnegative integer total")
        # EVS omits concepts entirely when total is zero; a positive total still
        # fails the completeness check below if its page is missing.
        rows = _object_list(data.get("concepts", []), "search concepts")
        _validate_search_page(rows, total, offset, limit)
        verify_content(rows, release)
        return total, rows

    def get_concept(
        self,
        code: str,
        release: ReleaseContext,
        include: str = LOOKUP_INCLUDE,
    ) -> dict[str, Any]:
        try:
            data = self._get_json(
                concept_path(release.pinned_terminology, code), {"include": include}
            )
        except EVSNotFoundError as exc:
            raise EVSNotFoundError(str(exc), identifiers=[code]) from exc
        concept = _object(data, "concept response")
        verify_content([concept], release)
        return concept

    def get_descendants(
        self, code: str, max_level: int, release: ReleaseContext
    ) -> list[dict[str, Any]]:
        """Fetch the descendants EVS places within `max_level` levels, each with its `level`.

        The concept must be known to exist: a 404 here is not reported as a
        missing concept. Compact entries do not self-describe their release;
        the pinned path establishes it, and later full concept reads verify it.
        """

        data = self._get_existing(
            f"{concept_path(release.pinned_terminology, code)}/descendants",
            {"maxLevel": max_level},
        )
        return _object_list(data, "descendants response")

    def get_replacements(self, code: str, release: ReleaseContext) -> list[dict[str, Any]]:
        """Read one verified concept's compact replacement history in the pinned release.

        A history 404 is an upstream failure, not an empty history. Rows need not
        self-describe their release, but any supplied identity must agree with the pin.
        """
        rows = _object_list(
            self._get_existing(replacements_path(release.pinned_terminology, code)),
            "replacement history",
        )
        for row in rows:
            _verify_history_row(row, code, release)
        return rows

    def get_paths_to_root(self, code: str, release: ReleaseContext) -> list[list[dict[str, Any]]]:
        """Every platform path, in its order; the caller has verified the seed exists."""
        data = self._get_existing(
            concept_path(release.pinned_terminology, code) + "/pathsToRoot",
            {"include": "minimal"},
        )
        if not isinstance(data, list):
            raise EVSResponseError("EVS pathsToRoot was not a list")
        paths = [_object_list(path, "path to root") for path in data]
        for path in paths:
            _verify_path(path, code, release)
        return paths


def _verify_path(path: list[dict[str, Any]], code: str, release: ReleaseContext) -> None:
    codes = [raw.get("code") for raw in path]
    if codes[:1] != [code]:
        raise EVSResponseError("EVS returned a path that does not start with the requested concept")
    if not all(isinstance(value, str) and value for value in codes):
        raise EVSResponseError("EVS returned a path without concept codes")
    if len(set(codes)) != len(codes):
        raise EVSResponseError("EVS returned a cyclic path to root")
    verify_content(path, release)


def _verify_history_row(row: dict[str, Any], code: str, release: ReleaseContext) -> None:
    if row.get("code") != code:
        raise EVSResponseError(
            "EVS returned replacement history for another or missing source code"
        )
    if "version" in row:
        verify_release([row], release.version)
    if "terminology" in row and row["terminology"] != release.terminology:
        raise EVSResponseError("EVS returned replacement history of another terminology")
