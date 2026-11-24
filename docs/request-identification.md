# Stable installation identity

The local identity utilities give an installation a stable, opaque label for request
traceability. They do not identify a person or a call, grant authority, or act as credentials.
This value is separate from the per-call correlation ID.

## Generate and inspect from a checkout

From the repository root, the PDM environment includes both the server and standalone
acceptance packages:

```bash
pdm run python -m nci_sip_mcp.traffic_identity generate
pdm run python -m nci_sip_acceptance.traffic_identity generate
```

Each `generate` command prints one UUID4 value as 32 lowercase hexadecimal characters. For a
persistent installation, save each value in durable deployment configuration under its matching
variable:
`NCI_SIP_INSTALLATION_ID` for the core package or `NCI_SIP_ACCEPTANCE_INSTALLATION_ID` for the
acceptance package. It does not write configuration or change the process environment. Keep the
saved value across restarts and upgrades; do not generate a fresh value at startup. For isolated
CI jobs, use the job-scoped lifecycle described below instead.

Before running `show`, set the corresponding variable to its previously saved value. For a local
shell, replace the placeholders below with those saved values:

```bash
export NCI_SIP_INSTALLATION_ID='<saved-core-value>'
export NCI_SIP_ACCEPTANCE_INSTALLATION_ID='<saved-acceptance-value>'
```

Then inspect the configured labels:

```bash
pdm run python -m nci_sip_mcp.traffic_identity show
pdm run python -m nci_sip_acceptance.traffic_identity show
```

`show` reports the configured product, the version of the package that owns the command, and its
identity. The complete label is limited to 256 ASCII bytes; unsafe metadata or a value that would
exceed that limit is refused, never normalized or truncated.

The acceptance utility works from its own installed distribution and does not require the core
package. Neither utility contacts an upstream service.

## Run from installed environments

The [local server guide](local-guide.md#server) names the installed server environment
`SERVER_VENV`. Set `ACCEPTANCE_VENV` to the environment where you installed the standalone
acceptance distribution. Use each environment's Python directly:

```bash
"$SERVER_VENV/bin/python" -m nci_sip_mcp.traffic_identity generate
"$ACCEPTANCE_VENV/bin/python" -m nci_sip_acceptance.traffic_identity generate
```

Save each generated value in the environment's durable deployment configuration. Before running
`show`, set each variable to its previously saved value; the utility never generates or persists
one during inspection:

```bash
export NCI_SIP_INSTALLATION_ID='<saved-core-value>'
export NCI_SIP_ACCEPTANCE_INSTALLATION_ID='<saved-acceptance-value>'
"$SERVER_VENV/bin/python" -m nci_sip_mcp.traffic_identity show
"$ACCEPTANCE_VENV/bin/python" -m nci_sip_acceptance.traffic_identity show
```

## Persist and rotate

For a persistent installation, keep the same value in durable deployment configuration across
restarts and upgrades. Replicas of one deployment may deliberately share that configuration.
Independent deployments should use independent values. These commands do not persist or compare
identities and do not detect duplicates.

A copied configuration keeps its identity. Before using a clone as a separate deployment, rotate
the value in its external configuration. Run `show` again to inspect the updated configuration;
the utility does not retain a snapshot between invocations.

## Images and ephemeral CI jobs

A wheel or container image carries the software, not a real installation ID. Supply the value
through runtime configuration; do not bake it into the artifact.

For an isolated CI job that emits this label, provision one job-scoped value per independently
configured emitting installation, and reuse it for that installation's own processes. A server
child launched by the acceptance harness is not given the harness's value: it sends the product
label alone unless its own configuration sets one.
Configure server and direct acceptance independently, with a distinct value for each. Give each
independent job a new value for each installation. This short-lived configuration is distinct from
a persistent installation's durable value, which remains stable across restarts and upgrades. The
value is not a credential.

## Current scope

The server reads this identity once at start-up and sends it in the `User-Agent` of every platform
request it makes (convention A6.2b): `nci-sip-mcp/<version> (installation=<id>; +https://github.com/CBIIT/nci-sip-mcp)`.
With no `NCI_SIP_INSTALLATION_ID` configured it sends the product label alone,
`nci-sip-mcp/<version> (+https://github.com/CBIIT/nci-sip-mcp)`; a configured value that is not 32 lowercase hexadecimal characters stops
start-up with an error naming the variable, before any upstream is contacted. The `generate`
and `show` commands remain local and send nothing. Governed acceptance requests
(the exact-request grants of a controlled run, and the release preflight) send the product label
alone, `nci-sip-mcp/<version> (+https://github.com/CBIIT/nci-sip-mcp)`, whatever
`NCI_SIP_INSTALLATION_ID` holds, and each grant declares that exact `user-agent`: a request with
another value, or none, is refused before contact. The acceptance recorder and citation reads
send the product-only `nci-sip-acceptance/<version> (+https://github.com/CBIIT/nci-sip-mcp)`;
the Shared SI contracts script and the installer's reachability probe send the core product-only
label above. Only the server, with a configured id, sends `installation=`; no other emitter
sends an installation identity. The identity does not replace the per-call correlation ID or
change its behavior. The CI guidance above describes the configuration lifecycle for an emitting
job; it applies to the server now and to the acceptance package once that work is released.
