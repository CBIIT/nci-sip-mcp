"""What the suite's tests read from a tool's result and from the upstream request log."""

from __future__ import annotations

import re
from datetime import datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from nci_sip_acceptance.spec import RECORDS, REQUIRED_TOOLS

if TYPE_CHECKING:
    from collections.abc import Iterable

    from nci_sip_acceptance.tools import Result


# The caDSR export folder's listing, and the export whose date is the registry's state.
EXPORT_LISTING = "recorded/cadsr-ftp/cde-xml-listing.json"
EXPORT = "releasedCDEsXML-OD.zip"
# The release of a caDSR item while caDSR publishes no registry release: the registry alone (X-21).
UNPINNED_REGISTRY = {"registry": "cadsr"}


def export_date(listing: str) -> str:
    """The date the export folder's listing gives the export, ISO-8601."""

    (dated,) = re.findall(rf">{re.escape(EXPORT)}</a>\s+(\d{{4}}-\d{{2}}-\d{{2}})", listing)
    return dated


def error_code(result: Result) -> str | None:
    """The code of an error result that is one error record and nothing else, or None."""

    content = result.content
    record = content.get("error") if isinstance(content, dict) else None
    if not result.is_error or not isinstance(record, dict) or set(content) != {"error"}:
        return None
    return record.get("code")


def requests_naming(log: Iterable[dict[str, Any]], codes: Iterable[str]) -> list[dict[str, Any]]:
    """The upstream requests that name any of `codes`, in their path or their parameters."""

    wanted = set(codes)
    return [entry for entry in log if wanted & _words(entry)]


def _words(entry: dict[str, Any]) -> set[str]:
    """Every path segment and every comma-separated parameter value of a request."""

    values = [value for values in entry["params"].values() for value in values]
    return {word for text in [*entry["path"].split("/"), *values] for word in text.split(",")}


# The fields of a provenance record, and those every item's carries (X-7).
PROVENANCE_FIELDS = RECORDS["provenance"]["fields"] | RECORDS["traversal"]["fields"]
CARRIED = ("release", "source", "retrievedAt", "servedBy", "correlationId")


def provenance_of(item: Any) -> dict[str, Any]:
    return (item.get("provenance") or {}) if isinstance(item, dict) else {}


def wrong_fields(provenance: dict[str, Any], names: Iterable[str]) -> list[str]:
    """The fields among `names` that `provenance` lacks or holds outside their closed set."""

    return [
        name
        for name in names
        if not _valid(provenance.get(name), PROVENANCE_FIELDS[name])
        or (name == "correlationId" and not is_name(provenance.get(name)))
    ]


def wrong_origin(provenance: dict[str, Any], *, uri: bool, origin: bool) -> list[str]:
    """Wrong origin fields under obligations fixed by the call, not its claimed source."""

    upstream = provenance.get("upstream")
    valid = {
        "sourceUri": is_url(provenance.get("sourceUri")) if uri else "sourceUri" not in provenance,
        "upstream": isinstance(upstream, dict) and bool(upstream)
        if origin
        else "upstream" not in provenance,
    }
    return [name for name, held in valid.items() if not held]


def _valid(value: Any, field: dict[str, Any]) -> bool:
    return value is not None and value in field.get("values", [value])


def _parsed(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(value)
    except TypeError, ValueError:
        return None


def is_iso8601(value: Any) -> bool:
    """Whether `value` is an ISO-8601 date or timestamp."""

    return _parsed(value) is not None


def is_timestamp(value: Any) -> bool:
    """Whether `value` is an ISO-8601 timestamp with a time zone."""

    parsed = _parsed(value)
    return parsed is not None and parsed.tzinfo is not None


def is_count(value: Any) -> bool:
    """Whether `value` is a positive integer."""

    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def is_name(value: Any) -> bool:
    """Whether `value` is a non-empty string."""

    return isinstance(value, str) and value != ""


def is_url(value: Any) -> bool:
    """Whether `value` is an absolute http(s) URL, as an upstream request has."""

    if not is_name(value):
        return False
    parts = urlsplit(value)
    return parts.scheme in ("http", "https") and bool(parts.netloc)


# M2.2: governed content that no release pins is cached briefly, at most this long.
SHORT_TTL = 3_600_000


def hint_fits(ttl: Any, scope: Any, pinned: bool) -> bool:
    """Whether a caching hint is of the class M2.2 and M2.3 give governed content: public, and
    ttlMs above 0, any length for release-pinned content, at most SHORT_TTL for content that no
    release pins."""

    return is_count(ttl) and (pinned or ttl <= SHORT_TTL) and scope == "public"


def release_of(item: Any) -> tuple[Any, Any]:
    """The terminology or registry and the release an item's provenance names."""

    release = provenance_of(item).get("release") or {}
    return release.get("terminology", release.get("registry")), release.get("identifier")


def pinned_release(name: str, pinned: dict[str, str]) -> tuple[str, str | None]:
    """The release a call's items name: for a caDSR call, which pins no registry release, the
    registry alone (X-21); for any other, the fixture set's, which a cross-domain item names in
    release beside the registry state (provenance)."""

    if REQUIRED_TOOLS[name] == "cadsr":
        return "cadsr", None
    return pinned["terminology"], pinned["release"]


def identity(item: Any) -> Any:
    """An item by what it is: a concept by terminology and code, an edge by its ends and its
    relationship's code, a mapping by its target terminology and code, a release by its
    terminology and version, a caDSR item by its public id and version (a code map by its data
    element's), the registry's state by whether it publishes a release and its date, a context
    by its name."""

    if not isinstance(item, dict):
        return item
    if "sourceCode" in item:
        relationship = (item.get("provenance") or {}).get("relationship") or {}
        return item.get("sourceCode"), item.get("targetCode"), relationship.get("code")
    return _terminology_identity(item)


def _terminology_identity(item: dict[str, Any]) -> Any:
    """A concept, a mapping or a release by its terminology and code or version; anything else
    by the registry's identity."""

    # A concept carries a version too, so a code decides first.
    if "code" in item:
        return item.get("terminology"), item["code"]
    if "targetTerminology" in item:
        return item["targetTerminology"], item.get("targetCode")
    # A record of a release has a version in place of a code.
    if "terminology" in item:
        return item["terminology"], item.get("version")
    return _registry_identity(item)


def _registry_identity(item: dict[str, Any]) -> Any:
    """The registry's state, a caDSR item by its public id and version, a code map by its data
    element's, a context by its name."""

    if "published" in item:
        return item["published"], item.get("identifier"), item.get("generatedAt")
    element = item.get("dataElement")
    owner: dict[str, Any] = element if isinstance(element, dict) else item
    if "publicId" in owner:
        return owner.get("publicId"), owner.get("version")
    return item.get("name")


def sparql_rows(document: dict[str, Any]) -> list[dict[str, str]]:
    """The rows of a recorded SPARQL answer, each variable by its value."""

    bindings = document["response"]["body"]["results"]["bindings"]
    return [{key: value["value"] for key, value in row.items()} for row in bindings]


def bare_code(iri: str) -> str:
    """The code an NCIt IRI ends in: C4817 of ...Thesaurus.owl#C4817."""

    return iri.rpartition("#")[2]


def element_ids(uses: list[dict[str, Any]]) -> set[str]:
    """Graph associations identify data elements by public ID only."""

    return {use["dataElement"]["publicId"] for use in uses}


def value_ids(uses: list[dict[str, Any]]) -> set[tuple[str, str, str]]:
    """Permissible value uses by data element, value and the concept it stands for."""

    return {
        (
            use["dataElement"]["publicId"],
            use["value"],
            use["conceptCode"],
        )
        for use in uses
    }
