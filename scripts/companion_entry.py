"""Local container entry: fixed offline worker profile, stdout diagnostics and owned shutdown."""

from __future__ import annotations

import sys
from pathlib import Path

from scripts.portal import serve_portal
from scripts.portal_configuration import LocalConfiguration
from scripts.portal_store import EvidenceStore
from scripts.processes import exit_on_sigterm

from nci_sip_mcp.config import configure_logging


def main() -> None:
    # This image is for local composition only; it is not a deployed identity boundary.
    sys.stderr = sys.stdout
    configure_logging("INFO")
    exit_on_sigterm()
    serve_portal(
        EvidenceStore(Path("/state/evidence.sqlite")),
        8081,
        retention=100,
        remote=None,
        configuration=LocalConfiguration(Path("/configuration/target.json")),
        host="0.0.0.0",  # noqa: S104 - container port is published only on host loopback
    )


if __name__ == "__main__":
    main()
