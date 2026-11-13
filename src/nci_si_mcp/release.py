"""The release model: which release a call reads, resolved once and threaded through it.

A `ReleaseContext` names the EVS release every request of one call is pinned to. It is resolved
explicitly by `resolve_evs_release` for discovery. Content calls use the supplied release or
the shared selection scope's implicit NCIt pin. Discovery operations remain fresh.
Stateful handshake HTTP sessions and stdio connections retain an implicit content pin (X-22);
sessionless HTTP resolves per call, regardless of the configured session mode.
`registry_state` is the caDSR counterpart: caDSR publishes no registry release, so the state
is the export's date and never an invented identifier (A3.8).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from .errors import PlatformError, with_next_step
from .validation import (
    RELEASE_CHANNELS,
    RELEASE_FORM,
)

if TYPE_CHECKING:
    from .evs import EVSClient


@dataclass(frozen=True, slots=True)
class ReleaseContext:
    """One release of an EVS terminology, as one call pins its requests to it."""

    terminology: str
    channel: str
    version: str
    date: str | None
    # The path segment that addresses exactly this release, for example `ncit_26.09d`.
    pinned_terminology: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "terminology": self.terminology,
            "channel": self.channel,
            "version": self.version,
            "date": self.date,
        }


CHANNEL_NEXT_STEP = (
    "Retry later, or set NCI_SI_RELEASE_CHANNEL to the other channel (monthly or weekly)"
)


def _not_available(
    message: str,
    requested: str,
    found: list[str] | None = None,
    next_step: str = CHANNEL_NEXT_STEP,
) -> PlatformError:
    return PlatformError(
        "release_not_available",
        with_next_step(message, next_step),
        requested=requested,
        source="evs",
        **({"found": found} if found else {}),
    )


def resolve_evs_release(evs: EVSClient, terminology: str, channel: str) -> ReleaseContext:
    """The release `channel` currently names for `terminology`, or `release_not_available`.

    `latest` is set per channel, so the query asks for the rows that are both latest and
    tagged with the channel and requires exactly one. Zero or several rows mean EVS does not
    name the release, and none is guessed.
    """

    rows = evs.get_terminologies(terminology, latest=True, tag=channel)
    row = _one_release_row(rows, f"{terminology} {channel}")
    _verify_selection(row, terminology, channel)
    return ReleaseContext(
        terminology=terminology,
        channel=channel,
        version=str(row["version"]),
        date=row.get("date"),
        pinned_terminology=row.get("terminologyVersion") or f"{terminology}_{row['version']}",
    )


def _verify_selection(row: dict[str, Any], terminology: str, channel: str) -> None:
    tags = row.get("tags")
    if row.get("terminology") != terminology or row.get("latest") is not True:
        raise _not_available(
            "EVS returned conflicting release identity", f"{terminology} {channel}"
        )
    if not isinstance(tags, dict) or tags.get(channel) != "true":
        raise _not_available("EVS returned a release without the requested channel", channel)
    version = row.get("version")
    if not isinstance(version, str) or re.fullmatch(RELEASE_FORM, version) is None:
        raise _not_available("EVS returned an invalid release version", f"{terminology} {channel}")
    _verify_pinned_segment(row.get("terminologyVersion"), terminology)


def _verify_pinned_segment(segment: Any, terminology: str) -> None:
    # EVS may use a path spelling different from the display version, but it must
    # still be a single release-qualified segment of the requested terminology.
    if segment is None:
        return
    if (
        not isinstance(segment, str)
        or re.fullmatch(re.escape(terminology) + "_" + RELEASE_FORM.strip("^$"), segment) is None
    ):
        raise _not_available("EVS returned a conflicting pinned terminology", terminology)


def served_evs_release(
    rows: list[dict[str, Any]], terminology: str, version: str, preferred_channel: str
) -> ReleaseContext:
    """Select an explicitly requested served version, without resolving a moving alias."""
    matching = [
        row
        for row in rows
        if row.get("terminology") == terminology and row.get("version") == version
    ]
    if len(matching) != 1:
        raise _not_available(
            "EVS does not uniquely name this release",
            version,
            next_step="Read resolve_release for served versions",
        )
    row = matching[0]
    channel = _served_channel(row, version, preferred_channel)
    return ReleaseContext(
        terminology,
        channel,
        version,
        row.get("date"),
        row.get("terminologyVersion") or f"{terminology}_{version}",
    )


def _served_channel(row: dict[str, Any], version: str, preferred: str) -> str:
    tags = row.get("tags")
    tags = tags if isinstance(tags, dict) else {}
    channels = {channel for channel in RELEASE_CHANNELS if tags.get(channel) == "true"}
    if preferred in channels:
        return preferred
    if len(channels) != 1:
        raise _not_available(
            "EVS does not identify this release's channel",
            version,
            next_step="Retry after its metadata is corrected",
        )
    return channels.pop()


def _one_release_row(rows: list[dict[str, Any]], requested: str) -> dict[str, Any]:
    versions = [str(row.get("version") or "") for row in rows]
    if len(rows) != 1:
        raise _not_available(
            f"EVS lists {len(rows)} releases as latest, not exactly one",
            requested,
            versions,
        )
    (row,) = rows
    if not versions[0]:
        raise _not_available("The latest release has no version", requested)
    return row


def current_terminologies(rows: list[dict[str, Any]], channel: str) -> list[dict[str, Any]]:
    """One current row per terminology; NCIt's latest flag is scoped by channel."""

    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        terminology = str(row.get("terminology") or "")
        if not terminology:
            raise _not_available(
                "EVS listed a release without a terminology", "terminology listing"
            )
        grouped.setdefault(terminology, []).append(row)
    return [
        _one_release_row(_current_rows(entries, terminology, channel), terminology)
        for terminology, entries in grouped.items()
    ]


def _current_rows(
    rows: list[dict[str, Any]], terminology: str, channel: str
) -> list[dict[str, Any]]:
    current = [row for row in rows if row.get("latest") is True]
    if terminology == "ncit":
        return [row for row in current if (row.get("tags") or {}).get(channel) == "true"]
    return current


@dataclass(frozen=True, slots=True)
class RegistryState:
    """The content state of the caDSR registry (A3.8).

    The identifier is absent while caDSR publishes no registry release; the export's date
    is then the most specific provenance available, not a reproducible registry release.
    """

    identifier: str | None
    generated_at: str
    source_distribution: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "published": self.identifier is not None,
            "generatedAt": self.generated_at,
            "sourceDistribution": self.source_distribution,
            **({"identifier": self.identifier} if self.identifier is not None else {}),
        }


class RegistryMetadataError(ValueError):
    """The registry supplied unusable release metadata, not a missing requested release."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.details = {"surface": "cadsr"}


def _generation_date(value: str | None, published: bool) -> str:
    """Validate an ISO date; the export listing is local time at minute precision."""

    if not isinstance(value, str) or not value.strip():
        raise RegistryMetadataError("The caDSR generation date is missing or not text")
    try:
        parsed = datetime.fromisoformat(value)
        if not published and (
            parsed.tzinfo is not None or parsed.isoformat(timespec="minutes") != value
        ):
            raise ValueError
        return value
    except ValueError, OverflowError:
        raise RegistryMetadataError("The caDSR generation date is invalid") from None


def registry_state(
    generation_date: str | None,
    upstream_identifier: str | None = None,
    *,
    source_distribution: str,
) -> RegistryState:
    """The registry state from upstream metadata, never an invented identifier or date.

    Without an identifier, `generation_date` is the export folder's ISO local date-time
    at minute precision, without an offset. No timezone is assumed or converted.
    With one, it is that release's own ISO-8601 date, passed through unchanged. The caller
    names the distribution the date came from (`releasedCDEsXML-OD.zip` today).
    """

    if upstream_identifier is not None and not (
        isinstance(upstream_identifier, str) and upstream_identifier.strip()
    ):
        raise RegistryMetadataError("The caDSR registry release identifier is blank or not text")
    if not isinstance(source_distribution, str) or not source_distribution.strip():
        raise RegistryMetadataError("The caDSR source distribution is missing or not text")
    return RegistryState(
        identifier=upstream_identifier,
        generated_at=_generation_date(generation_date, upstream_identifier is not None),
        source_distribution=source_distribution,
    )
