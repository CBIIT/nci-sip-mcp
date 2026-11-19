"""Release-specific relationship catalogues and polarity by code."""

from __future__ import annotations

from typing import Any, get_args

from .config import Settings
from .errors import PlatformError
from .evs import EVSClient, EVSResponseError, catalogue_path
from .models import (
    attribution_of,
    live_provenance,
    release_ref,
    upstream_origin,
)
from .release import ReleaseContext
from .validation import Polarity, RelationshipKind


def exclusion_codes(settings: Settings, terminology: str) -> frozenset[str]:
    """Only NCIt has a specified exclusion set; its configured default is pinned to spec/."""

    return frozenset(settings.exclusion_role_codes) if terminology == "ncit" else frozenset()


def polarity(code: str | None, exclusions: frozenset[str]) -> Polarity:
    return "negative" if code in exclusions else "positive"


def load_catalogue(
    client: EVSClient, release: ReleaseContext, exclusions: frozenset[str]
) -> list[dict[str, Any]]:
    """Read and validate both catalogues once within the caller's request budget."""

    records = []
    present: set[str] = set()
    for kind in get_args(RelationshipKind):
        rows = client.get_relationship_catalogue(release, kind)
        _identities(rows, present)
        records.extend(_record(client, release, kind, row, exclusions) for row in rows)
    missing = sorted(exclusions - present)
    if missing:
        raise PlatformError(
            "internal_error",
            "The release catalogue lacks configured exclusion codes. "
            "Ask the operator to check the exclusion configuration against this release.",
            missingCodes=missing,
        )
    return records


def _identities(rows: list[dict[str, Any]], seen: set[str]) -> None:
    for row in rows:
        code, name = row.get("code"), row.get("name")
        if not isinstance(code, str) or not code:
            raise EVSResponseError("EVS returned a relationship without its code")
        if not isinstance(name, str) or not name or code in seen:
            raise EVSResponseError("EVS returned a missing relationship name or duplicate code")
        seen.add(code)


def _record(
    client: EVSClient,
    release: ReleaseContext,
    kind: RelationshipKind,
    row: dict[str, Any],
    exclusions: frozenset[str],
) -> dict[str, Any]:
    provenance = live_provenance(
        release_ref(release.terminology, release.version, release.date),
        "evs_rest",
        uri=client.uri(catalogue_path(release.pinned_terminology, kind)),
        upstream=upstream_origin(row),
        attribution=attribution_of(row, "evs"),
    )
    return {
        "code": row["code"],
        "terminology": row["terminology"],
        "name": row["name"],
        "kind": kind,
        "polarity": polarity(row["code"], exclusions),
        "provenance": provenance,
    }
