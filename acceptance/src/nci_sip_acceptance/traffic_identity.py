"""Generate and inspect the acceptance installation identity without sending it."""

from __future__ import annotations

import argparse
import os
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from importlib.metadata import version
from typing import NewType
from uuid import uuid4

INSTALLATION_ID_ENV = "NCI_SIP_ACCEPTANCE_INSTALLATION_ID"
PRODUCT = "nci-sip-acceptance"
PROJECT_URL = "https://github.com/CBIIT/nci-sip-mcp"
MAX_LABEL_BYTES = 256
ID_ERROR = (
    "Set NCI_SIP_ACCEPTANCE_INSTALLATION_ID to exactly 32 lowercase hexadecimal characters "
    "and retry."
)
VERSION_ERROR = (
    "The installed nci-sip-acceptance version cannot form a safe label. "
    "Reinstall the acceptance package with valid metadata and retry."
)

InstallationId = NewType("InstallationId", str)
_INSTALLATION_ID = re.compile(r"[0-9a-f]{32}\Z")
_VERSION_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9.!+_-]*\Z", re.ASCII)


@dataclass(frozen=True, slots=True)
class IdentitySnapshot:
    _raw_id: str | None = field(repr=False)
    version: str | None = field(repr=False)


def capture_identity() -> IdentitySnapshot:
    """Capture the configured value and installed package version once."""

    try:
        installed_version = version(PRODUCT)
    except UnicodeDecodeError:
        installed_version = None
    return IdentitySnapshot(os.environ.get(INSTALLATION_ID_ENV), installed_version)


def installation_label(snapshot: IdentitySnapshot) -> str:
    """Validate a captured configuration and render the complete acceptance label."""

    raw_id = snapshot._raw_id
    if raw_id is None or _INSTALLATION_ID.fullmatch(raw_id) is None:
        raise ValueError(ID_ERROR)
    return _label(snapshot.version, InstallationId(raw_id))


def product_user_agent(snapshot: IdentitySnapshot) -> str:
    """The User-Agent of acceptance's direct requests: the product alone, never an installation."""

    return _label(snapshot.version, None)


def _label(installed_version: str | None, installation_id: InstallationId | None) -> str:
    if (
        not isinstance(installed_version, str)
        or _VERSION_TOKEN.fullmatch(installed_version) is None
    ):
        raise ValueError(VERSION_ERROR)
    installation = f"installation={installation_id}; " if installation_id else ""
    label = f"{PRODUCT}/{installed_version} ({installation}+{PROJECT_URL})"
    if len(label.encode("ascii")) > MAX_LABEL_BYTES:
        raise ValueError(VERSION_ERROR)
    return label


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m nci_sip_acceptance.traffic_identity",
        description="Generate or inspect the local acceptance identity; no request is sent.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("generate", help="generate a token for the operator to persist")
    commands.add_parser("show", help="display the configured acceptance label")
    return parser


def main(arguments: Sequence[str] | None = None) -> int:
    options = _parser().parse_args(arguments)
    if options.command == "generate":
        sys.stdout.write(f"{uuid4().hex}\n")
        return 0
    try:
        sys.stdout.write(f"{installation_label(capture_identity())}\n")
    except ValueError as error:
        sys.stderr.write(f"{error}\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
