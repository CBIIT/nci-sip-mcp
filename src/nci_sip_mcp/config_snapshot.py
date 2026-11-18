"""Opt-in safe startup evidence, without live telemetry or configuration authority."""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from tempfile import mkstemp
from typing import Any
from uuid import uuid4

from . import __version__
from .config import Settings
from .validation import UPSTREAM_MODES

# Newly introduced settings are excluded unless deliberately added here.
READ_ONLY = (
    "profile",
    "upstream_mode",
    "release_channel",
    "transport",
    "http_sessions",
    "http_require_index",
)
PROPOSABLE = (
    "timeout_seconds",
    "match_timeout_seconds",
    "evs_max_attempts",
    "evs_retry_backoff_seconds",
    "evs_max_response_bytes",
    "index_batch_size",
    "log_level",
)
SAFE_FIELDS = READ_ONLY + PROPOSABLE
MAX_BYTES = 16384
MAX_STAMP = 40
FIELDS = {"schema", "instance", "captured_at", "package_version", "settings", "revision"}


def _revision(snapshot: dict[str, Any]) -> str:
    values = {key: value for key, value in snapshot.items() if key != "revision"}
    return hashlib.sha256(json.dumps(values, sort_keys=True, allow_nan=False).encode()).hexdigest()


def capture(
    settings: Settings, environment_names: set[str], *, cli_transport: bool = False
) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for name in SAFE_FIELDS:
        origin = "environment" if f"NCI_SIP_{name.upper()}" in environment_names else "default"
        if name == "transport" and cli_transport:
            origin = "cli"
        values[name] = {"value": getattr(settings, name), "origin": origin}
    snapshot = {
        "schema": 1,
        "instance": uuid4().hex,
        "captured_at": datetime.now(UTC).isoformat(),
        "package_version": __version__,
        "settings": values,
    }
    snapshot["revision"] = _revision(snapshot)
    return validate_snapshot(snapshot)


def _metadata(snapshot: dict[str, Any]) -> None:
    if set(snapshot) != FIELDS or type(snapshot["schema"]) is not int or snapshot["schema"] != 1:
        raise ValueError("Unsupported configuration snapshot")
    _identity(snapshot)
    stamp = snapshot["captured_at"]
    if not isinstance(stamp, str) or len(stamp) > MAX_STAMP:
        raise ValueError("Invalid capture time")
    if datetime.fromisoformat(stamp).utcoffset() != UTC.utcoffset(None):
        raise ValueError("Capture time must be UTC")


def _identity(snapshot: dict[str, Any]) -> None:
    for name, pattern in (
        ("instance", r"[a-f0-9]{32}"),
        ("revision", r"[a-f0-9]{64}"),
        ("package_version", r"[A-Za-z0-9.+-]{1,100}"),
    ):
        if not isinstance(snapshot[name], str) or re.fullmatch(pattern, snapshot[name]) is None:
            raise ValueError("Invalid configuration identity")


def _values(rows: Any) -> dict[str, Any]:
    if not isinstance(rows, dict) or set(rows) != set(SAFE_FIELDS):
        raise ValueError("Configuration settings must match the safe allowlist")
    defaults = Settings()
    values: dict[str, Any] = {}
    for name, row in rows.items():
        _setting(name, row, defaults)
        values[name] = row["value"]
    if values["upstream_mode"] not in UPSTREAM_MODES:
        raise ValueError("Invalid upstream mode")
    # Validate safe values without needing excluded fixture endpoints or provider settings.
    validation_values: dict[str, Any] = values | {"upstream_mode": "live"}
    Settings(**validation_values)
    return values


def _setting(name: str, row: Any, defaults: Settings) -> None:
    if not isinstance(row, dict) or set(row) != {"value", "origin"}:
        raise ValueError("Invalid setting record")
    if row["origin"] not in ("default", "environment", "cli"):
        raise ValueError("Invalid setting origin")
    expected = type(getattr(defaults, name))
    types = (int, float) if expected is float else (expected,)
    if type(row["value"]) not in types:
        raise ValueError("Invalid setting type")


def validate_snapshot(snapshot: Any) -> dict[str, Any]:
    if not isinstance(snapshot, dict):
        raise ValueError("Configuration snapshot must be an object")
    _metadata(snapshot)
    _values(snapshot["settings"])
    if _revision(snapshot) != snapshot["revision"]:
        raise ValueError("Configuration snapshot revision mismatch")
    return snapshot


def read_snapshot(path: Path) -> dict[str, Any]:
    if path.is_symlink():
        raise ValueError("Configuration snapshot cannot be a symlink")
    with path.open("rb") as stream:
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("Configuration snapshot is too large")
    return validate_snapshot(json.loads(raw))


@contextmanager
def serving_snapshot(
    path: Path, settings: Settings, environment_names: set[str], *, cli_transport: bool = False
) -> Iterator[dict[str, Any]]:
    snapshot = capture(settings, environment_names, cli_transport=cli_transport)
    raw = json.dumps(snapshot, sort_keys=True).encode()
    _publish(path, raw)
    try:
        yield snapshot
    finally:
        _remove_owned(path, raw)


def _publish(path: Path, raw: bytes) -> None:
    # Same-directory hard-link publication is atomic and refuses an existing target.
    descriptor, name = mkstemp(dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
        os.link(temporary, path)
    finally:
        temporary.unlink()


def _remove_owned(path: Path, raw: bytes) -> None:
    try:
        with path.open("rb") as stream:
            unchanged = stream.read(MAX_BYTES + 1) == raw
    except FileNotFoundError:
        return
    if unchanged and not path.is_symlink():
        path.unlink()
