"""Immutable exact local-run authorization; its private canonical bytes are never diagnostics."""

import base64
import hashlib
import hmac
import json
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Self, TypedDict, final, get_args
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit

from nci_si_acceptance.concepts import MAX_BATCH
from nci_si_mcp.errors import safe_correlation
from nci_si_mcp.http_client import RequestPolicyError
from nci_si_mcp.validation import RELEASE_CHANNELS, RELEASE_FORM, ReleaseChannel, RequestMethod

# These fields belong to direct transport framing, not caller-selected request content.
TRANSPORT_HEADERS = frozenset(
    (
        "host",
        "content-length",
        "transfer-encoding",
        "connection",
        "proxy-authorization",
        "proxy-connection",
        "expect",
        "upgrade",
        "te",
        "trailer",
        "accept-encoding",
        "cookie",
    )
)

# ASCII control characters cannot identify an HTTP destination.
CONTROL_START = 32
CONTROL_END = 127
# Each header is exactly a field name and value, not an iterable to reinterpret.
HEADER_PAIR_SIZE = 2
# A relation endpoint suffix consists of exactly a concept code and relation name.
RELATION_PATH_PARTS = 2

type HeaderRow = tuple[str, str] | list[str]
# SQLite INTEGER counters are signed 64-bit; remeasure if the storage representation changes.
MAX_COUNTER = (1 << 63) - 1
# Reviewed diagnostic defaults, not measured upstream capacity: 256 logical calls at up to
# three attempts each, four in flight and 30 minutes bound work and burst exposure.
# One MiB per response and 64 MiB aggregate bound retained response data. Revisit these
# defaults with approved run evidence before widening them.
DEFAULT_LOGICAL = 256
DEFAULT_ATTEMPTS = 768
DEFAULT_PER_LOGICAL = 3
DEFAULT_CONCURRENT = 4
DEFAULT_SECONDS = 1800
DEFAULT_RESPONSE_BYTES = 1048576
DEFAULT_TOTAL_BYTES = 67108864


def _require(condition: bool) -> None:
    if not condition:
        raise RequestPolicyError("authorization")


def _text(value: object) -> str:
    if not isinstance(value, str):
        raise RequestPolicyError("authorization")
    return value


def _digest(value: object) -> None:
    text = _text(value)
    _require(re.fullmatch(r"[a-f0-9]{64}", text) is not None)


def _control(char: str) -> bool:
    # URL/origin and header values share the same ASCII control classification.
    value = ord(char)
    return value < CONTROL_START or value == CONTROL_END


def _url_text(url: str) -> None:
    _require(not any(char.isspace() or _control(char) for char in url))
    _require("#" not in url)


def _url_path(path: str) -> None:
    _require(not any(part in (".", "..") for part in unquote(path).split("/")))
    _require(not re.search(r"%2f|%5c|%25", path, re.IGNORECASE))
    _require("\\" not in path)


def destination(url: str) -> tuple[str, str, int]:
    url = _text(url)
    try:
        parts = urlsplit(url)
        port = (
            parts.port
            if parts.port is not None
            else {"https": 443, "http": 80}.get(parts.scheme, 0)
        )
    except ValueError:
        raise RequestPolicyError("authorization") from None
    hostname = parts.hostname
    if parts.scheme not in ("http", "https") or not hostname:
        raise RequestPolicyError("authorization")
    _require(parts.username is None and port > 0)
    _url_text(url)
    _url_path(parts.path)
    return parts.scheme, hostname, port


def _header_valid(name: str, value: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9-]+", name)) and not any(_control(char) for char in value)


def _pair(row: HeaderRow) -> tuple[str, str]:
    _require(isinstance(row, (tuple, list)) and len(row) == HEADER_PAIR_SIZE)
    _require(all(isinstance(value, str) for value in row))
    return row[0], row[1]


def _headers(values: Sequence[HeaderRow]) -> tuple[tuple[str, str], ...]:
    _require(isinstance(values, (tuple, list)))
    pairs = tuple(map(_pair, values))
    _require(all(_header_valid(name, value) for name, value in pairs))
    headers = tuple((name.lower(), value) for name, value in pairs)
    _require(len({name for name, _ in headers}) == len(headers))
    return tuple(sorted(headers))


def _credential_destination(url: str, base: str) -> bool:
    origin_matches = destination(url) == destination(base)
    path, prefix = urlsplit(url).path, urlsplit(base).path.rstrip("/")
    return (
        origin_matches
        and not urlsplit(base).query
        and (path == prefix or path.startswith(prefix + "/"))
    )


def _seconds(value: float) -> float:
    _require(isinstance(value, (int, float)) and not isinstance(value, bool))
    try:
        seconds = float(value)
    except OverflowError:
        raise RequestPolicyError("authorization") from None
    _require(math.isfinite(seconds) and seconds > 0)
    return seconds


@final
@dataclass(frozen=True, slots=True)
class Limits:
    logical: int = DEFAULT_LOGICAL
    attempts: int = DEFAULT_ATTEMPTS
    per_logical: int = DEFAULT_PER_LOGICAL
    concurrent: int = DEFAULT_CONCURRENT
    seconds: float = DEFAULT_SECONDS
    response_bytes: int = DEFAULT_RESPONSE_BYTES
    total_bytes: int = DEFAULT_TOTAL_BYTES

    def __post_init__(self) -> None:
        integers = (
            self.logical,
            self.attempts,
            self.per_logical,
            self.concurrent,
            self.response_bytes,
            self.total_bytes,
        )
        _require(
            all(
                isinstance(value, int) and not isinstance(value, bool) and 0 < value <= MAX_COUNTER
                for value in integers
            )
        )
        seconds = _seconds(self.seconds)
        _require(self.per_logical <= self.attempts and self.concurrent <= self.attempts)
        _require(self.response_bytes + 1 <= self.total_bytes)
        object.__setattr__(self, "seconds", seconds)


@final
@dataclass(frozen=True, slots=True, init=False)
class RequestGrant:
    method: RequestMethod
    url: str = field(repr=False)
    headers: tuple[tuple[str, str], ...] = field(repr=False)
    body: bytes | None = field(default=None, repr=False)
    credential_origin: str | None = field(default=None, repr=False)

    def __init__(
        self,
        method: RequestMethod,
        url: str,
        headers: Sequence[HeaderRow],
        body: bytes | None = None,
        credential_origin: str | None = None,
    ) -> None:
        """Snapshot tuple/list header rows; unsupported sequence shapes are refused."""
        object.__setattr__(self, "method", method)
        object.__setattr__(self, "url", url)
        object.__setattr__(self, "headers", _headers(headers))
        object.__setattr__(self, "body", body)
        object.__setattr__(self, "credential_origin", credential_origin)
        self.__post_init__()

    def __post_init__(self) -> None:
        destination(self.url)
        _require(self.method in get_args(RequestMethod.__value__))
        _require(self.body is None or isinstance(self.body, bytes))
        headers = self.headers
        sensitive = {"authorization", "x-evsrestapi-license-key"} & {name for name, _ in headers}
        if sensitive and self.credential_origin is None:
            raise RequestPolicyError("authorization")
        if self.credential_origin is not None:
            _require(_credential_destination(self.url, self.credential_origin))


def _private_bytes(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


@final
@dataclass(frozen=True, slots=True)
class ReleaseTarget:
    terminology: str
    channels: tuple[ReleaseChannel, ...]

    def __post_init__(self) -> None:
        _require(
            type(self.terminology) is str
            and re.fullmatch(r"[a-z][a-z0-9]*", self.terminology) is not None
        )
        _require(type(self.channels) is tuple and bool(self.channels))
        _require(
            all(type(channel) is str and channel in RELEASE_CHANNELS for channel in self.channels)
        )
        _require(len(set(self.channels)) == len(self.channels))

    def accepts(self, segment: str) -> bool:
        return (
            re.fullmatch(re.escape(self.terminology) + "_" + RELEASE_FORM.strip("^$"), segment)
            is not None
        )


@final
@dataclass(frozen=True, slots=True)
class ExactRequestFamily:
    request: RequestGrant = field(repr=False)
    correlation: bool = False
    release: ReleaseTarget | None = None

    def __post_init__(self) -> None:
        _require(type(self.request) is RequestGrant and type(self.correlation) is bool)
        _require(not (set(dict(self.request.headers)) & TRANSPORT_HEADERS))
        _require(not self.correlation or "x-correlation-id" not in dict(self.request.headers))
        _release_template(self.request.url, self.release)


def _release_template(url: str, release: ReleaseTarget | None) -> None:
    if release is None:
        _require("{" not in url and "}" not in url)
        return
    _require(type(release) is ReleaseTarget)
    _require(urlsplit(url).path.split("/").count("{release}") == 1)
    _require(url.count("{") == 1 and url.count("}") == 1)


def _strings(values: tuple[str, ...], pattern: str) -> None:
    _require(type(values) is tuple and bool(values))
    _require(all(type(value) is str and re.fullmatch(pattern, value) for value in values))
    _require(len(set(values)) == len(values))


@final
@dataclass(frozen=True, slots=True)
class ConceptRequestFamily:
    request: RequestGrant = field(repr=False)
    codes: tuple[str, ...]
    includes: tuple[str, ...]
    relations: tuple[str, ...]
    release: ReleaseTarget
    correlation: bool = False

    def __post_init__(self) -> None:
        ExactRequestFamily(self.request, self.correlation, self.release)
        _require(self.request.method == "GET" and self.request.body is None)
        _require(self.request.url.endswith("/api/v1/concept/{release}"))
        _strings(self.codes, r"C[0-9]+")
        _strings(self.includes, r"[A-Za-z]+(?:,[A-Za-z]+)*")
        _strings(self.relations, r"[A-Za-z]+")


type RequestFamily = ExactRequestFamily | ConceptRequestFamily


def _release_segment(template: str, release: ReleaseTarget, url: str) -> str | None:
    before, _marker, _after = template.partition("{release}")
    if not url.startswith(before):
        return None
    segment = url[len(before) :].split("/", 1)[0].split("?", 1)[0]
    return segment if release.accepts(segment) else None


def _query(query: str) -> dict[str, str]:
    pairs = parse_qsl(query, keep_blank_values=True)
    _require(urlencode(pairs) == query and len(dict(pairs)) == len(pairs))
    return dict(pairs)


def _batch_matches(family: ConceptRequestFamily, query: dict[str, str]) -> bool:
    codes = query.get("list", "").split(",")
    return (
        set(query) == {"list", "include"}
        and len(codes) <= MAX_BATCH
        and all(code in family.codes for code in codes)
        and query["include"] in family.includes
    )


def _concept_tail(family: ConceptRequestFamily, suffix: list[str], query: dict[str, str]) -> bool:
    if suffix[0] not in family.codes:
        return False
    if len(suffix) == 1:
        return set(query) == {"include"} and query["include"] in family.includes
    return len(suffix) == RELATION_PATH_PARTS and suffix[1] in family.relations and not query


def _concept_matches(family: ConceptRequestFamily, base: str, url: str) -> bool:
    query = _query(urlsplit(url).query)
    path = url.partition("?")[0]
    if path == base:
        return _batch_matches(family, query)
    # The packet owner already matched the exact origin/prefix and one release segment.
    return _concept_tail(family, path[len(base) + 1 :].split("/"), query)


def _family_packet(
    family: RequestFamily, url: str, headers: tuple[tuple[str, str], ...]
) -> tuple[RequestGrant, tuple[ReleaseTarget, str] | None] | None:
    destination(url)
    _require(not url.endswith("?"))
    base = family.request.url
    requirement: tuple[ReleaseTarget, str] | None = None
    if family.release is not None:
        segment = _release_segment(base, family.release, url)
        if segment is None:
            return None
        base = base.replace("{release}", segment)
        requirement = (family.release, segment)
    matches = (
        _concept_matches(family, base, url) if type(family) is ConceptRequestFamily else url == base
    )
    if not matches:
        return None
    grant = family.request
    return RequestGrant(
        grant.method, url, headers, grant.body, grant.credential_origin
    ), requirement


def _family_headers(
    family: RequestFamily, headers: tuple[tuple[str, str], ...]
) -> tuple[tuple[str, str], ...]:
    if not family.correlation:
        return headers
    correlation = dict(headers).get("x-correlation-id")
    _require(correlation is None or safe_correlation(correlation) is not None)
    return tuple(pair for pair in headers if pair[0] != "x-correlation-id")


def _grant_record(grant: RequestGrant) -> dict[str, object]:
    return {
        "method": grant.method,
        "url": grant.url,
        "headers": grant.headers,
        "body": grant.body,
        "credential_origin": grant.credential_origin,
    }


def _family_record(family: RequestFamily) -> dict[str, object]:
    # Bind declared scalar values, never arbitrary dataclass attachments on those values.
    target = family.release
    record: dict[str, object] = {
        "kind": type(family).__name__,
        "request": _grant_record(family.request),
        "correlation": family.correlation,
        "release": (
            {"terminology": target.terminology, "channels": target.channels}
            if target is not None
            else None
        ),
    }
    if type(family) is ConceptRequestFamily:
        record.update(codes=family.codes, includes=family.includes, relations=family.relations)
    return record


@final
@dataclass(frozen=True, slots=True, init=False)
class Authorization:
    grants: tuple[RequestGrant, ...] = field(repr=False)
    families: tuple[RequestFamily, ...] = field(default=(), kw_only=True, repr=False)
    limits: Limits
    policy_sha256: str

    def __init__(
        self,
        grants: Sequence[RequestGrant],
        limits: Limits,
        policy_sha256: str,
        *,
        families: Sequence[RequestFamily] = (),
    ) -> None:
        """Snapshot a nonempty tuple/list; never consume an arbitrary iterator."""
        _require(isinstance(grants, (tuple, list)))
        _require(bool(grants) and all(type(grant) is RequestGrant for grant in grants))
        _require(type(limits) is Limits)
        _require(isinstance(families, (tuple, list)))
        _require(
            all(type(family) in (ExactRequestFamily, ConceptRequestFamily) for family in families)
        )
        _digest(policy_sha256)
        object.__setattr__(self, "grants", tuple(grants))
        object.__setattr__(self, "limits", limits)
        object.__setattr__(self, "policy_sha256", policy_sha256)
        object.__setattr__(self, "families", tuple(families))

    def admit(
        self, method: str, url: str, headers: Sequence[HeaderRow], body: object
    ) -> RequestGrant:
        """Resolve declared forms into exact immutable bytes; no other metadata may vary."""
        return self._match(method, url, headers, body)[0]

    def release_targets(self) -> tuple[ReleaseTarget, ...]:
        return tuple(
            dict.fromkeys(family.release for family in self.families if family.release is not None)
        )

    def _match(
        self, method: str, url: str, headers: Sequence[HeaderRow], body: object
    ) -> tuple[RequestGrant, tuple[ReleaseTarget, str] | None]:
        canonical = _headers(headers)
        for grant in self.grants:
            if (method, url, canonical, body) == (
                grant.method,
                grant.url,
                grant.headers,
                grant.body,
            ):
                return grant, None
        for family in self.families:
            grant = family.request
            if (method, _family_headers(family, canonical), body) == (
                grant.method,
                grant.headers,
                grant.body,
            ):
                matched = _family_packet(family, url, canonical)
                if matched is not None:
                    return matched
        raise RequestPolicyError("authorization")

    def binding(self, key: bytes) -> bytes:
        _require(type(self) is Authorization)
        _require(isinstance(key, bytes))
        payload: dict[str, object] = {
            "grants": [_grant_record(grant) for grant in self.grants],
            "limits": {
                "logical": self.limits.logical,
                "attempts": self.limits.attempts,
                "per_logical": self.limits.per_logical,
                "concurrent": self.limits.concurrent,
                "seconds": self.limits.seconds,
                "response_bytes": self.limits.response_bytes,
                "total_bytes": self.limits.total_bytes,
            },
            "policy_sha256": self.policy_sha256,
        }
        if self.families:
            payload["families"] = [_family_record(family) for family in self.families]
        canonical = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
            default=_private_bytes,
        ).encode()
        return hmac.digest(key, canonical, hashlib.sha256)


class DescriptorRecord(TypedDict):
    run_id: str
    policy_sha256: str
    directory: str


@dataclass(frozen=True, slots=True)
class RunDescriptor:
    run_id: str
    policy_sha256: str
    directory: Path = field(repr=False)

    def __post_init__(self) -> None:
        _require(re.fullmatch(r"[a-f0-9]{32}", _text(self.run_id)) is not None)
        _digest(self.policy_sha256)
        _require(isinstance(self.directory, Path))
        _require(self.directory.is_absolute())

    def to_record(self) -> DescriptorRecord:
        return {
            "run_id": self.run_id,
            "policy_sha256": self.policy_sha256,
            "directory": str(self.directory),
        }

    @classmethod
    def from_record(cls, value: object) -> Self:
        if not isinstance(value, dict) or set(value) != {"run_id", "policy_sha256", "directory"}:
            raise RequestPolicyError("authorization")
        return cls(
            _text(value["run_id"]), _text(value["policy_sha256"]), Path(_text(value["directory"]))
        )
