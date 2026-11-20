"""Opaque continuation positions bound to applied arguments and optional index identity."""

import base64
import json
import re
from dataclasses import dataclass
from typing import Any

from .errors import InputValidationError
from .validation import RELEASE_FORM

MAX_CURSOR_LENGTH = 8192


@dataclass(frozen=True)
class Position:
    offset: int = 0
    build_id: str | None = None


def encode(arguments: dict[str, Any], offset: int, build_id: str | None = None) -> str:
    record = {"v": 1, "arguments": arguments, "offset": offset}
    if build_id is not None:
        record["build"] = build_id
    payload = json.dumps(record, separators=(",", ":"))
    return base64.urlsafe_b64encode(payload.encode()).decode()


def decode(token: str | None, arguments: dict[str, Any], *, indexed: bool = False) -> Position:
    if token is None:
        return Position()
    payload = _payload(token)
    if payload.get("v") != 1 or payload.get("arguments") != arguments:
        raise InputValidationError("The cursor does not continue these applied arguments", "cursor")
    offset = payload.get("offset")
    if type(offset) is not int or offset <= 0:
        raise InputValidationError("The cursor has an invalid position", "cursor")
    return Position(offset, _build_id(payload, indexed))


def validate_before_selection(
    token: str | None, arguments: dict[str, Any], *, indexed: bool = False
) -> None:
    """Check a continuation locally before discovering an omitted release.

    Only release equality waits for discovery; the cursor never selects that release.
    The caller must decode again with the independently selected effective release.
    """
    if token is None:
        return
    if arguments.get("release") is None:
        recorded = _payload(token).get("arguments")
        release = recorded.get("release") if isinstance(recorded, dict) else None
        if not isinstance(release, str) or not re.fullmatch(RELEASE_FORM, release):
            raise InputValidationError("The cursor has no valid release identity", "cursor")
        arguments = arguments | {"release": release}
    decode(token, arguments, indexed=indexed)


def _build_id(payload: dict[str, Any], indexed: bool) -> str | None:
    build = payload.get("build")
    if indexed:
        if not isinstance(build, str) or not re.fullmatch(r"[0-9a-f]{32}", build):
            raise InputValidationError("The cursor has no valid index build identity", "cursor")
    elif "build" in payload:
        raise InputValidationError("An indexed cursor cannot continue this tool or mode", "cursor")
    return build


def _payload(token: str) -> dict[str, Any]:
    if not token or len(token) > MAX_CURSOR_LENGTH:
        raise InputValidationError("The cursor is empty or too long", "cursor")
    try:
        value = json.loads(base64.b64decode(token, altchars=b"-_", validate=True))
    except ValueError, UnicodeError, RecursionError:
        raise InputValidationError("The cursor is malformed", "cursor") from None
    if not isinstance(value, dict):
        raise InputValidationError("The cursor is not a continuation record", "cursor")
    return value
