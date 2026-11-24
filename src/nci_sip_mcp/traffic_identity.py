"""Generate and inspect the local installation identity without sending it."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from importlib.metadata import version
from typing import NewType
from uuid import uuid4

from .errors import PlatformError, serialise

INSTALLATION_ID_ENV = "NCI_SIP_INSTALLATION_ID"
PRODUCT = "nci-sip-mcp"
PROJECT_URL = "https://github.com/CBIIT/nci-sip-mcp"
MAX_LABEL_BYTES = 256

InstallationId = NewType("InstallationId", str)
_INSTALLATION_ID = re.compile(r"[0-9a-f]{32}\Z")
_VERSION_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9.!+_-]*\Z", re.ASCII)


@dataclass(frozen=True, slots=True)
class IdentitySnapshot:
    _raw_id: str | None = field(repr=False)
    version: str | None = field(repr=False)


def _installed_version() -> str | None:
    try:
        return version(PRODUCT)
    except UnicodeDecodeError:
        return None


def capture_identity() -> IdentitySnapshot:
    """Capture the configured value and installed package version once."""

    return IdentitySnapshot(os.environ.get(INSTALLATION_ID_ENV), _installed_version())


def product_user_agent() -> str:
    """The product-only User-Agent, whatever installation is configured (A6.2b).

    For emitters that carry no installation identity; the installed version is read and
    validated here, with the refusal `user_agent` gives.
    """

    return _label(_installed_version(), None)


def installation_label(snapshot: IdentitySnapshot) -> str:
    """Validate a captured configuration and render the complete product label."""

    raw_id = snapshot._raw_id
    if raw_id is None:
        raise _invalid_id("is missing")
    return _label(snapshot.version, _valid_id(raw_id))


def user_agent(snapshot: IdentitySnapshot) -> str:
    """The User-Agent a server sends (A6.2b): the product label, with the installation when set.

    An unset id is not an error here: identification serves platform monitoring, not access
    control. A configured id that is malformed is refused, as `installation_label` refuses it.
    """

    if snapshot._raw_id is None:
        return _label(snapshot.version, None)
    return installation_label(snapshot)


def _valid_id(raw_id: str) -> InstallationId:
    if _INSTALLATION_ID.fullmatch(raw_id) is None:
        raise _invalid_id("must be exactly 32 lowercase hexadecimal characters")
    return InstallationId(raw_id)


def _label(installed_version: str | None, installation_id: InstallationId | None) -> str:
    if (
        not isinstance(installed_version, str)
        or _VERSION_TOKEN.fullmatch(installed_version) is None
    ):
        raise _invalid_version()
    installation = f"installation={installation_id}; " if installation_id else ""
    label = f"{PRODUCT}/{installed_version} ({installation}+{PROJECT_URL})"
    if len(label.encode("ascii")) > MAX_LABEL_BYTES:
        raise _invalid_version()
    return label


def _invalid_id(reason: str) -> PlatformError:
    return PlatformError(
        "invalid_request",
        "Set NCI_SIP_INSTALLATION_ID to exactly 32 lowercase hexadecimal characters and retry.",
        parameter=INSTALLATION_ID_ENV,
        reason=reason,
    )


def _invalid_version() -> PlatformError:
    return PlatformError(
        "invalid_request",
        "The installed nci-sip-mcp version cannot form a safe label. "
        "Reinstall the package with valid metadata and retry.",
        parameter="nci-sip-mcp installed version",
        reason="must be a safe printable ASCII version and keep the complete label within "
        "256 bytes",
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m nci_sip_mcp.traffic_identity",
        description="Generate or inspect the local installation identity; no request is sent.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("generate", help="generate a token for the operator to persist")
    commands.add_parser("show", help="display the configured installation label")
    return parser


def main(arguments: Sequence[str] | None = None) -> int:
    options = _parser().parse_args(arguments)
    if options.command == "generate":
        sys.stdout.write(f"{uuid4().hex}\n")
        return 0
    try:
        sys.stdout.write(f"{installation_label(capture_identity())}\n")
    except PlatformError as error:
        sys.stdout.write(f"{json.dumps(serialise(error), indent=2, sort_keys=True)}\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
