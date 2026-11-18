# Stable installation identity

The local identity utilities give an installation a stable, opaque label for future request
traceability. They do not identify a person or a call, grant authority, or act as credentials.
This value is separate from the per-call correlation ID.

## Generate and inspect

Run the utility for the distribution you installed:

```bash
python -m nci_si_mcp.traffic_identity generate
python -m nci_si_mcp.traffic_identity show
```

The standalone acceptance distribution has its own identity and version:

```bash
python -m nci_si_acceptance.traffic_identity generate
python -m nci_si_acceptance.traffic_identity show
```

`generate` prints one UUID4 value as 32 lowercase hexadecimal characters. It does not write
configuration or change the process environment. Persist the value yourself as
`NCI_SI_INSTALLATION_ID` for the core package or `NCI_SI_ACCEPTANCE_INSTALLATION_ID` for the
acceptance package. `show` reports the configured product, installed package version, and
identity. The complete label is limited to 256 ASCII bytes; unsafe metadata or a value that
would exceed that limit is refused, never normalized or truncated.

The acceptance utility works from its own installed distribution and does not require the core
package. Neither utility contacts an upstream service.

## Persist and rotate

Keep the same value in deployment configuration across restarts and upgrades when they represent
the same installation. Replicas of one deployment may deliberately share that configuration.
Independent deployments should use independent values. These commands do not persist or compare
identities and do not detect duplicates.

A copied configuration keeps its identity. Before using a clone as a separate deployment, rotate
the value in its external configuration. A running process keeps the identity snapshot it captured;
restart it after rotation so a new snapshot uses the new value.

## Current scope

These commands only generate and inspect local configuration. The server does not yet add an HTTP
header or alter any request with this identity. Wire emission and its rollout are separate work;
do not rely on platform request traces carrying this label until that work is released. The
identity does not replace the per-call correlation ID or change its behavior.
