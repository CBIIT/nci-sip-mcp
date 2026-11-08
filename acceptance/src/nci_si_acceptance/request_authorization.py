"""Immutable exact local-run authorization; its private canonical bytes are never diagnostics."""

import base64
import hashlib
import hmac
import json
import math
import re
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Self, TypedDict, final, get_args
from urllib.parse import unquote, urlsplit

from nci_si_mcp.http_client import RequestPolicyError
from nci_si_mcp.validation import RequestMethod

# ASCII control characters cannot identify an HTTP destination.
CONTROL_START = 32
CONTROL_END = 127
# Each header is exactly a field name and value, not an iterable to reinterpret.
HEADER_PAIR_SIZE = 2

type HeaderRow = tuple[str, str] | list[str]
# SQLite INTEGER counters are signed 64-bit; remeasure if the storage representation changes.
MAX_COUNTER = (1 << 63) - 1
# Reviewed diagnostic policy, not measured upstream capacity: 256 logical calls at up to
# three attempts each, four in flight and 30 minutes bound work and burst exposure.
# One MiB per response and 64 MiB aggregate bound retained response data. Revisit these
# ceilings with approved run evidence before widening them.
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
@dataclass(frozen=True, slots=True, init=False)
class Authorization:
    grants: tuple[RequestGrant, ...] = field(repr=False)
    limits: Limits
    policy_sha256: str

    def __init__(
        self,
        grants: Sequence[RequestGrant],
        limits: Limits,
        policy_sha256: str,
    ) -> None:
        """Snapshot a nonempty tuple/list; never consume an arbitrary iterator."""
        _require(isinstance(grants, (tuple, list)))
        _require(bool(grants) and all(type(grant) is RequestGrant for grant in grants))
        _require(type(limits) is Limits)
        _digest(policy_sha256)
        object.__setattr__(self, "grants", tuple(grants))
        object.__setattr__(self, "limits", limits)
        object.__setattr__(self, "policy_sha256", policy_sha256)

    def binding(self, key: bytes) -> bytes:
        _require(type(self) is Authorization)
        _require(isinstance(key, bytes))
        canonical = json.dumps(
            asdict(self),
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
