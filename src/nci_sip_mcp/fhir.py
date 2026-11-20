"""EVS FHIR R4 expansion, with the A3.2 verified unpinned fallback."""

from typing import Any

from .evs import EVSResponseError, _object_list, verify_release
from .http_client import HttpClient
from .models import attribution_of, live_provenance, release_ref
from .release import ReleaseContext

# The canonical NCIt system, as the recorded EVS ValueSet.url names it.
NCIT_SYSTEM = "http://ncicb.nci.nih.gov/xml/owl/EVS/Thesaurus.owl"


def expand(
    client: HttpClient,
    release: ReleaseContext,
    code: str,
    count: int,
    offset: int,
    active_only: bool,
) -> dict[str, Any]:
    canonical = f"{NCIT_SYSTEM}?fhir_vs={code}"
    params = {"url": canonical}
    # EVS rejects system-version and enumerates only latest monthly subsets.
    # Do not try an unsupported pin or cache an unverified expansion.
    raw = client.get_json("/ValueSet/$expand", params)
    _verify_value_set(raw, release, canonical)
    provenance = _provenance(raw, release, client.url("/ValueSet/$expand", params))
    members = [_member(row, provenance, canonical) for row in _contains(raw)]
    if active_only:
        members = [member for member in members if not member.get("inactive")]
    page = members[offset : offset + count]
    return {
        "members": page,
        "total": len(members),
        "truncation": {"occurred": False},
    } | ({"provenance": provenance} if not page else {})


def _verify_value_set(raw: Any, release: ReleaseContext, canonical: str) -> None:
    if not isinstance(raw, dict) or raw.get("resourceType") != "ValueSet":
        raise EVSResponseError("EVS FHIR returned no ValueSet")
    if raw.get("url") != canonical or raw.get("title") != release.terminology:
        raise EVSResponseError("EVS FHIR returned another value set or terminology")
    if not isinstance(raw.get("version"), str) or not raw["version"]:
        raise EVSResponseError("EVS FHIR returned a missing or invalid ValueSet version")
    verify_release([raw], release.version)


def _contains(raw: dict[str, Any]) -> list[dict[str, Any]]:
    expansion = raw.get("expansion")
    if not isinstance(expansion, dict):
        raise EVSResponseError("EVS FHIR returned no expansion")
    rows = _object_list(expansion.get("contains", []), "FHIR expansion members")
    total = expansion.get("total", len(rows))
    offset = expansion.get("offset", 0)
    if type(total) is not int or total != len(rows) or type(offset) is not int or offset != 0:
        raise EVSResponseError("EVS FHIR returned an incomplete expansion")
    return rows


def _member(row: dict[str, Any], provenance: dict[str, Any], canonical: str) -> dict[str, Any]:
    if row.get("system") not in (NCIT_SYSTEM, canonical) or "contains" in row:
        raise EVSResponseError("EVS FHIR returned a nested or foreign-system member")
    inactive = row.get("inactive", False)
    if not isinstance(inactive, bool):
        raise EVSResponseError("EVS FHIR returned a non-boolean inactive mark")
    result = {
        "code": _member_text(row, "code"),
        "terminology": "ncit",
        "name": _member_text(row, "display"),
        "provenance": provenance,
    }
    return result | {"inactive": True} if inactive else result


def _member_text(row: dict[str, Any], field: str) -> str:
    value = row.get(field)
    if not isinstance(value, str) or not value:
        raise EVSResponseError(f"EVS FHIR returned a member without its {field}")
    return value


def _provenance(raw: dict[str, Any], release: ReleaseContext, uri: str) -> dict[str, Any]:
    return live_provenance(
        release_ref(release.terminology, release.version, release.date),
        "evs_fhir",
        uri=uri,
        upstream={"url": raw["url"], "version": raw["version"]},
        attribution=attribution_of(raw, "evs", "copyright"),
    )
