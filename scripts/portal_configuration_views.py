"""Accessible recorded configuration and no-apply proposal previews."""

from typing import Any
from urllib.parse import parse_qs

from scripts.portal_configuration import MAX_VALUE, LocalConfiguration, SnapshotInvalidError
from scripts.portal_views import page, table, text

from nci_sip_mcp.config_snapshot import PROPOSABLE


def configuration_page(configuration: LocalConfiguration) -> tuple[int, str]:
    try:
        snapshot = configuration.read()
    except OSError as error:
        problem = (
            "The selected target startup snapshot is malformed."
            if isinstance(error, SnapshotInvalidError)
            else "No target startup snapshot is readable."
        )
        return 503, page(
            "Configuration unavailable",
            f"<p>{problem} Remote configuration is unknown.</p>"
            '<p><a href="/help#configuration">Set up configuration evidence</a>.</p>',
            section="/configuration",
        )
    body = "<p>Recorded startup configuration, not a live-state or availability guarantee. "
    body += "The selected target may have stopped since capture. "
    body += '<a href="/help#configuration">Understand settings and proposals</a>.</p>'
    body += table(
        "Selected target snapshot",
        ["Field", "Recorded value"],
        [
            ["Target instance", snapshot["instance"]],
            ["Captured at (UTC)", snapshot["captured_at"]],
            ["Package version", snapshot["package_version"]],
            ["Revision", snapshot["revision"]],
        ],
    )
    rows = [
        [name, row["value"], row["origin"], "Required"]
        for name, row in snapshot["settings"].items()
    ]
    body += table(
        "Recorded safe settings", ["Setting", "Value", "Origin", "Restart after change"], rows
    )
    body += (
        "<h2>Prepare a change proposal</h2><p>Deployment environment or IaC remains authoritative. "
    )
    body += "This preview neither applies settings nor restarts a server.</p>"
    options = "".join(f'<option value="{name}">{name}</option>' for name in PROPOSABLE)
    body += f'''<form method="post" action="/configuration/proposals">
<input type="hidden" name="instance" value="{text(snapshot["instance"])}">
<input type="hidden" name="revision" value="{text(snapshot["revision"])}">
<label>Setting <select name="setting">{options}</select></label>
<label>Proposed value <input name="value" maxlength="{MAX_VALUE}" required
aria-describedby="proposal-help"></label>
<button>Preview proposal</button></form><p id="proposal-help">Use the setting's existing type
and range. Log level is DEBUG, INFO, WARNING, ERROR or CRITICAL.
<a href="/help#configuration">Proposal help</a>.</p>'''
    return 200, page("Configuration", body, section="/configuration")


def propose(configuration: LocalConfiguration, raw: bytes) -> str:
    fields = parse_qs(
        raw.decode("utf-8"), keep_blank_values=True, strict_parsing=True, max_num_fields=4
    )
    if set(fields) != {"instance", "revision", "setting", "value"} or any(
        len(items) != 1 for items in fields.values()
    ):
        raise ValueError("Invalid proposal fields")
    proposal = configuration.propose(**{key: items[0] for key, items in fields.items()})
    return proposal_page(proposal)


def proposal_page(proposal: dict[str, Any]) -> str:
    body = (
        '<p role="status"><strong>Nothing has been applied.</strong> This is an advisory proposal '
    )
    body += "for the deployment owner to review and implement through the authoritative "
    body += "environment or IaC. A target restart is required; this page does not apply, restart "
    body += "or roll back any replica.</p>"
    body += table(
        "Proposed change",
        ["Setting", "Recorded before", "Proposed after"],
        [[proposal["setting"], proposal["before"], proposal["after"]]],
    )
    body += table(
        "Proposal basis",
        ["Field", "Recorded value"],
        [
            ["Target instance", proposal["instance"]],
            ["Revision", proposal["revision"]],
            ["Captured at (UTC)", proposal["captured_at"]],
        ],
    )
    body += "<p>Recheck the target configuration before implementing a proposal. "
    body += '<a href="/configuration">Return to configuration</a> · '
    body += '<a href="/help#configuration">Configuration help</a>.</p>'
    return page("Configuration proposal", body, section="/configuration")
