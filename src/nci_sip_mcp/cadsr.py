"""Contract-backed caDSR operations; no local substitute for upstream content."""

from __future__ import annotations

import base64
import json
import re
from functools import partial
from html.parser import HTMLParser
from http import HTTPStatus
from typing import Any
from urllib.parse import urlsplit

from .audit import secrets
from .config import CADSR_STAGING_ORIGIN, Settings
from .errors import PlatformError
from .http_client import HttpClient, UpstreamRejectedError
from .release import RegistryMetadataError, RegistryState, registry_state
from .validation import (
    ITEM_VERSION_FORM,
    REGISTRY_ID_FORM,
    bounded,
    validate_identifier,
    validate_text_target,
)

DATA_API = "/NCIAPI/1.0/api"
FORM_API = "/NCIFormAPI.v2_0:NciFormApiRad"
CDE_MATCH = "/NCIAPI.v2_0.cdeMatch.api:cdeMatch_rad/cdeMatch"
VM_MATCH = "/vmMatch/v1/vmMatch"
EXPORT_FOLDER = "/CDE/XML/"
DISTRIBUTION = "releasedCDEsXML-OD.zip"
_LISTING_DATE = re.compile(r"(?<!\S)\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}(?!\S)")

# Bound retries for transient caDSR failures; the count is unmeasured. Re-measure
# failure and recovery rates and request latency before changing it.
CADSR_MAX_ATTEMPTS = 3
# Space retries to avoid immediate repeat load; the delay is unmeasured. Re-measure
# caDSR recovery times and retry traffic before changing it.
CADSR_RETRY_BACKOFF_SECONDS = 0.25
# Bound response memory; the limit is unmeasured. Re-measure caDSR response sizes
# and memory use before changing it.
CADSR_MAX_RESPONSE_BYTES = 10 * 1024 * 1024


def _form_absence(public_id: str, payload: bytes, status: int | None) -> None:
    if status != HTTPStatus.OK:
        return
    try:
        data = json.loads(payload)
    except ValueError, RecursionError:
        # ValueError: bad JSON, bad UTF-8 or an integer over the configured digit limit; the
        # common parser reports malformed JSON and HTML.
        return
    if _empty_error_form(data):
        # recorded/cadsr/form-unknown.json supplies no machine-readable absence code.
        # A genuine platform failure in exactly this shape would also read as not found.
        raise PlatformError(
            "not_found",
            "No form has that public id and version. Check the identifier or version.",
            identifiers=[public_id],
        )


def _empty_error_form(data: Any) -> bool:
    if not isinstance(data, dict) or "form" not in data or data["form"] is not None:
        return False
    envelope = data.get("apiResponse")
    return isinstance(envelope, dict) and envelope.get("type") == "E"


class _ExportListing(HTMLParser):
    """Read logical rows in Apache preformatted and table directory listings."""

    def __init__(self) -> None:
        super().__init__()
        self.rows: list[str] = []
        self.text: list[str] = []
        self.links = 0
        self.in_row = False

    def finish_row(self) -> None:
        self.rows.extend([" ".join(self.text)] * self.links)
        self.text = []
        self.links = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "tr":
            self.finish_row()
            self.in_row = True
        if tag == "a" and dict(attrs).get("href") == DISTRIBUTION:
            self.links += 1

    def handle_endtag(self, tag: str) -> None:
        if tag == "tr":
            self.finish_row()
            self.in_row = False

    def handle_data(self, data: str) -> None:
        if self.in_row:
            self.text.append(data)
            return
        parts = data.split("\n")
        self.text.append(parts[0])
        for part in parts[1:]:
            self.finish_row()
            self.text.append(part)


def export_state(listing: str) -> RegistryState:
    parser = _ExportListing()
    parser.feed(listing)
    parser.close()
    parser.finish_row()
    if len(parser.rows) != 1:
        raise RegistryMetadataError("The caDSR export listing must name one distribution row")
    dates = _LISTING_DATE.findall(parser.rows[0])
    if len(dates) != 1:
        raise RegistryMetadataError("The caDSR export distribution has no unambiguous date")
    date, time = dates[0].split()
    return registry_state(f"{date}T{time}", source_distribution=DISTRIBUTION)


def _payload(response: Any, key: str) -> Any:
    if not isinstance(response, dict) or key not in response:
        raise PlatformError(
            "upstream_unavailable",
            "caDSR returned an incomplete response. Retry later.",
            surface="cadsr",
        )
    return response[key]


def _items(response: Any, key: str, item_type: type = dict) -> list[Any]:
    value = _payload(response, key)
    if not isinstance(value, list) or any(not isinstance(item, item_type) for item in value):
        raise PlatformError(
            "upstream_unavailable",
            "caDSR returned a malformed list. Retry later.",
            surface="cadsr",
        )
    return value


def _item(response: Any, key: str) -> dict[str, Any] | None:
    value = _payload(response, key)
    envelope = response.get("apiResponse")
    if value is None and isinstance(envelope, dict) and envelope.get("type") == "I":
        return None
    if not isinstance(value, dict):
        raise PlatformError(
            "upstream_unavailable",
            "caDSR returned a malformed item. Retry later.",
            surface="cadsr",
        )
    return value


def _version_params(version: str | None) -> dict[str, Any]:
    if version is not None:
        validate_identifier(version, ITEM_VERSION_FORM, "version")
    return {"version": version}


def _verify_registry_pin(response: Any, requested: str | None) -> None:
    if requested is None:
        return
    actual = response.get("registryRelease") if isinstance(response, dict) else None
    if actual != requested:
        raise PlatformError(
            "release_mismatch",
            "caDSR did not confirm the requested registry release. "
            "Retry after the platform fixes its pinning.",
            requested=requested,
            served=[actual] if isinstance(actual, str) and actual else [],
            source="cadsr",
        )


def data_element_request(
    public_id: str, version: str | None, registry_release: str | None
) -> tuple[str, dict[str, Any]]:
    """C-1's requested pinned route differs from today's public-id path."""
    params = _version_params(version) | {"registryRelease": registry_release}
    if registry_release is not None:
        return f"{DATA_API}/DataElement", params | {"publicId": public_id}
    return f"{DATA_API}/DataElement/{public_id}", params


def question_request(text: str, pin: str | None) -> tuple[str, dict[str, object]]:
    """Use the same question target for admission and the upstream request."""
    return f"{DATA_API}/DataElements/ReferenceDocument", {
        "documentText": text,
        "documentType": "Preferred Question Text",
        "headerOnly": "true",
        "registryRelease": pin,
    }


def search_request(query: str, size: int, pin: str | None) -> tuple[str, dict[str, object]]:
    """Use the same keyword target for admission and the upstream request."""
    return f"{DATA_API}/DataElement/search", {
        "keyword": query,
        "pageSize": size,
        "registryRelease": pin,
    }


def _is_staging(base_url: str) -> bool:
    """Whether `base_url` addresses the staging origin: scheme, host and effective port."""

    def origin(url: str) -> tuple[str, str | None, int | None]:
        parts = urlsplit(url)
        return parts.scheme, parts.hostname, parts.port or 443

    return origin(base_url) == origin(CADSR_STAGING_ORIGIN)


class CaDSRClient:
    def __init__(self, settings: Settings, *, user_agent: str) -> None:
        credential = settings.cadsr_credential
        headers = (
            {"Authorization": "Basic " + base64.b64encode(credential.encode()).decode()}
            if credential
            else {}
        )
        hint = "check the NIH VPN for caDSR staging" if _is_staging(settings.cadsr_base_url) else ""
        transport = partial(
            HttpClient,
            label="CADSR",
            max_attempts=CADSR_MAX_ATTEMPTS,
            retry_backoff_seconds=CADSR_RETRY_BACKOFF_SECONDS,
            max_response_bytes=CADSR_MAX_RESPONSE_BYTES,
            size_bound="cadsr_response_bytes",
            user_agent=user_agent,
        )
        self.http = transport(
            settings.cadsr_base_url,
            timeout_seconds=settings.timeout_seconds,
            credentials=headers,
            redact_values=secrets(None, credential),
            unresolved_hint=hint,
        )
        self.match_http = transport(
            settings.cadsr_base_url,
            timeout_seconds=settings.match_timeout_seconds,
            credentials=headers,
            redact_values=secrets(None, credential),
            unresolved_hint=hint,
        )
        self.export_http = transport(
            settings.cadsr_ftp_url,
            timeout_seconds=settings.timeout_seconds,
        )

    def get_data_element(
        self, public_id: str, version: str | None = None, *, registry_release: str | None = None
    ) -> dict[str, Any] | None:
        validate_identifier(public_id, REGISTRY_ID_FORM, "publicId")
        path, params = data_element_request(public_id, version, registry_release)
        response = self.http.get_json(path, params)
        _verify_registry_pin(response, registry_release)
        return _item(response, "DataElement")

    def get_by_question_text(
        self, text: str, *, registry_release: str | None = None
    ) -> list[dict[str, Any]]:
        path, params = question_request(text, registry_release)
        validate_text_target(self.http.url(path, params), "questionText")
        response = self.http.get_json(path, params)
        _verify_registry_pin(response, registry_release)
        return _items(response, "DataElements")

    def list_contexts(self, *, registry_release: str | None = None) -> list[str]:
        response = self.http.get_json(
            "/NCILovAPI/1.0/api/getContextNames", {"registryRelease": registry_release}
        )
        _verify_registry_pin(response, registry_release)
        return _items(response, "contextNames", str)

    def get_form(self, public_id: str, version: str | None = None) -> dict[str, Any] | None:
        validate_identifier(public_id, REGISTRY_ID_FORM, "publicId")
        response = self.http.get_json(
            f"{FORM_API}/Form/{public_id}",
            _version_params(version),
            interpret=partial(_form_absence, public_id),
        )
        return _item(response, "form")

    def get_crdc_list(self, *, registry_release: str | None = None) -> list[dict[str, Any]]:
        response = self.http.get_json(
            f"{DATA_API}/DataElements/getCRDCList", {"registryRelease": registry_release}
        )
        _verify_registry_pin(response, registry_release)
        return _items(response, "CRDCDataElements")

    def search_data_elements(
        self, query: str, page_size: int, *, registry_release: str | None = None
    ) -> dict[str, Any]:
        """OP-C03 requested operation, not served by caDSR today (C-3).

        Tested against the crafted OP-C03 fixtures. A live type-E answer remains
        an upstream error; there is no fallback or undocumented filter. A verified
        registry pin follows the requested future C-1 contract.
        """
        size = bounded(page_size, 1000, "pageSize")
        path, params = search_request(query, size, registry_release)
        validate_text_target(self.http.url(path, params), "query")
        response = self.http.get_json(path, params)
        _verify_registry_pin(response, registry_release)
        _items(response, "DataElements")
        return response

    def match_data_element(self, entity: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
        """Send one contract apiinput object, with no alternative-body retry."""
        response = self.match_http.post_json(CDE_MATCH, entity, headers=headers)
        value = _item(response, "matchResults")
        if value is None:
            raise PlatformError(
                "upstream_unavailable", "caDSR omitted match results. Retry later.", surface="cadsr"
            )
        return value

    def match_value_meanings(
        self, entities: list[dict[str, str]], headers: dict[str, str]
    ) -> list[dict[str, Any]]:
        response = self.match_http.post_json(VM_MATCH, entities, headers=headers)
        return _items(response, "matchResults")

    def get_registry_releases(self) -> list[dict[str, Any]]:
        """Only the recorded empty 404 denotes the currently unserved release listing.

        A wrong base URL returning the same empty 404 is indistinguishable; nonempty
        errors remain failures, and no response body is retained in error metadata.
        """
        try:
            response = self.http.get_json(f"{DATA_API}/registry/releases")
        except UpstreamRejectedError as exc:
            if exc.details.get("status") != HTTPStatus.NOT_FOUND or not exc.empty_body:
                raise
            return []
        return _items(response, "registryReleases")

    def resolve_registry_release(self) -> RegistryState:
        """Resolve registry state without inventing an identifier or a timezone.

        The export folder gives local server time without a zone: generatedAt preserves
        its minute precision and carries no offset, never the ZIP's Last-Modified date.
        """
        releases = self.get_registry_releases()
        if not releases:
            return export_state(self.export_http.get_text(EXPORT_FOLDER))
        latest = [row for row in releases if row.get("latest") is True]
        if len(latest) != 1:
            raise RegistryMetadataError("caDSR must name exactly one latest registry release")
        row = latest[0]
        if not row.get("identifier"):
            raise RegistryMetadataError("The caDSR registry release identifier is missing")
        return registry_state(
            row.get("generatedAt"),
            row["identifier"],
            source_distribution=self.http.url(f"{DATA_API}/registry/releases"),
        )
