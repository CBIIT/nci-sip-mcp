# Request identification

Every request the server sends to a platform service says which product and, optionally, which
deployment sent it (the `User-Agent`), and which call it belongs to (`X-Correlation-ID`). Platform
teams use the two fields to recognise our traffic and to assist; neither field identifies a person,
grants authority, or acts as a credential.

| Question | Carrier | Where it comes from |
|---|---|---|
| Which deployment of which product? | `User-Agent` | The installed package version and, when configured, `NCI_SIP_INSTALLATION_ID` |
| Which call? | `X-Correlation-ID` | The call's `_meta.correlationId`, or one the server generates (A6.2, M7.1); unchanged by the label |

The label form is convention A6.2b in the [specification](specification.md).

## The labels

Three forms exist. `<v>` is the installed package version.

| Sender | `User-Agent` |
|---|---|
| Server and core, installation id configured | `nci-sip-mcp/<v> (installation=<32 lowercase hex>; +https://github.com/CBIIT/nci-sip-mcp)` |
| Server and core, no id configured | `nci-sip-mcp/<v> (+https://github.com/CBIIT/nci-sip-mcp)` |
| Acceptance recorder and citation reads | `nci-sip-acceptance/<v> (+https://github.com/CBIIT/nci-sip-mcp)` |

- **Governed acceptance runs and the release preflight** send the second form: product only, never
  an installation id. A server launched by the acceptance suite receives none: the suite strips
  every inherited `NCI_SIP_*` variable from its environment and sets only its own run settings.
- **The installer's EVS and caDSR reachability checks (`evs_reachable`, `cadsr_reachable`) and the
  Shared SI contract script** send the second form, never an installation id. The installer's
  check of the local HTTP `/ready` endpoint is not platform traffic and sends no label.
- **Only the server and core can send `installation=`.** There is no separate installation header,
  and the acceptance harness gives the server it launches no identity (it strips every inherited
  `NCI_SIP_*` variable from that server's environment, then sets only its own run settings).
- **Localhost-only callers send no label.**
- **The dependency-age script** calls the public package registries, not a platform service. It
  identifies itself as `nci-sip-mcp-dependency-age` and carries no installation.

## When to configure an installation id

The id is optional. Without it, the server still sends the product label. Configure
`NCI_SIP_INSTALLATION_ID` when a deployment sends bulk live EVS or caDSR requests, or expects
platform-team support, so the platform team can tell deployments apart. Fixture sessions and
single live probes that expect no support do not need one.

## Per-call correlation and your audit record

The server sends the call's correlation id in `X-Correlation-ID` on every platform request of that
call, including retries. The same value is the `correlationId` of the `call_completed` record the
server writes to stderr (see [audit records](../QUICKSTART.md#audit-records)). When a platform team
quotes a correlation id, search your stderr records for it; the record gives the tool, the target,
the release and the outcome of that call. The caller's own `_meta.correlationId` passes through
unchanged when it is well formed.

## From an installation id to a deployment

The installation id appears in the `User-Agent` only. The local audit record does not carry it. When
a platform team quotes an installation id, find the deployment from your own record of which id you
assigned to which deployment; the server stores no such table. When a platform team quotes a
correlation id, use the section above.

## Start-up checks and troubleshooting

The server reads `NCI_SIP_INSTALLATION_ID` and the installed version once, at start-up, and refuses
to start before contacting any upstream when either is unusable. A malformed id gives an error
that names `NCI_SIP_INSTALLATION_ID` and never repeats the rejected value. An unsafe installed
version gives an error that names the package metadata and asks you to reinstall the package.
Every CLI command, including local-only ones such as `search`, starts
the same context and stops too.

| Situation | Result |
|---|---|
| No id configured | Starts; sends the product-only label |
| Id is not exactly 32 lowercase hexadecimal characters | Refused at start-up, naming `NCI_SIP_INSTALLATION_ID` |
| Installed version cannot form a safe label, or the label exceeds 256 ASCII bytes | Refused at start-up, naming the package metadata; reinstall the package |
| Two deployments share one id | Not detected; give independent deployments independent ids |

## Generate and inspect from a checkout

From the repository root, the PDM environment includes both the server and standalone
acceptance packages:

```bash
pdm run python -m nci_sip_mcp.traffic_identity generate
pdm run python -m nci_sip_acceptance.traffic_identity generate
```

Each `generate` command prints one UUID4 value as 32 lowercase hexadecimal characters. For a
persistent installation, save the server value in durable deployment configuration under
`NCI_SIP_INSTALLATION_ID`. It does not write configuration or change the process environment. Keep
the saved value across restarts and upgrades; do not generate a fresh value at startup.

The acceptance package has its own utility and variable, `NCI_SIP_ACCEPTANCE_INSTALLATION_ID`. The
acceptance suite never sends that value; it exists to inspect an acceptance installation's label.

Before running `show`, set the variable to its previously saved value:

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

Save the generated server value in the environment's durable deployment configuration. Before
running `show`, set each variable to its previously saved value; the utility never generates or
persists one during inspection:

```bash
export NCI_SIP_INSTALLATION_ID='<saved-core-value>'
export NCI_SIP_ACCEPTANCE_INSTALLATION_ID='<saved-acceptance-value>'
"$SERVER_VENV/bin/python" -m nci_sip_mcp.traffic_identity show
"$ACCEPTANCE_VENV/bin/python" -m nci_sip_acceptance.traffic_identity show
```

## Persist, replicate, clone and rotate

For a persistent installation, keep the same value in durable deployment configuration across
restarts and upgrades. Replicas of one deployment may deliberately share that configuration.
Independent deployments should use independent values. These commands do not persist or compare
identities and do not detect duplicates.

A copied configuration keeps its identity. Before using a clone as a separate deployment, rotate
the value in its external configuration, restart, and run `show` again to inspect it; the utility
does not retain a snapshot between invocations. A restart is needed because the server reads the
value once at start-up.

## Images and ephemeral CI jobs

A wheel or container image carries the software, not a real installation id. Supply the value
through runtime configuration; do not bake it into the artifact.

Only the server sends an installation id. An acceptance run, a CI job or a launched server child
needs none, and the acceptance suite gives its launched server none, so such traffic carries the
product-only label. For a job that does need a distinct server identity (bulk live work), provision
one job-scoped value for that server. This short-lived value is distinct from a persistent
installation's durable one, and it is not a credential.

## For platform teams

What you will see from us on a platform request the server makes during a tool call or CLI command:

```text
User-Agent: nci-sip-mcp/<v> (installation=<32 hex>; +https://github.com/CBIIT/nci-sip-mcp)
X-Correlation-ID: <caller-supplied or generated id, [A-Za-z0-9._:-]{1,128}>
```

An example log line, assuming your access log records both fields (an example, not observed
retention):

```text
... "GET /api/v1/concept/ncit_<release>/C4817?include=minimal" ... "nci-sip-mcp/<v> (installation=<id>; +https://github.com/CBIIT/nci-sip-mcp)" X-Correlation-ID=<id>
```

- `installation` is an opaque identifier for one deployment. It is not a secret and not a
  credential. Requests without it come from a deployment that has none configured, from acceptance
  or CI traffic, or from the recorder (a different product token).
- `X-Correlation-ID` joins one request to the audit record of the call that made it. Send us the
  value and the time and we find the call from our stderr records. Requests made outside a call
  carry `User-Agent` only and no correlation id: the installer's EVS and caDSR reachability checks,
  the Shared SI contract script, and the recorder and citation reads.
- Both fields are self-reported. They are meant for monitoring and assistance, not authentication
  or access control.

### What we do not claim

We make no claim that any platform gateway logs `User-Agent` or `X-Correlation-ID`, how long a log
is kept, what a field is called there, or how to query it. Source emission is not proof that a
gateway kept the header.

| Platform | Logs the fields | Retention | Status | Owner |
|---|---|---|---|---|
| EVS | not confirmed | not confirmed | unconfirmed | project coordinator |
| caDSR | not confirmed | not confirmed | unconfirmed | project coordinator |
| Shared SI | not confirmed | not confirmed | unconfirmed | project coordinator |

## Compatibility and rollout

The label form is versioned with convention A6.2b; a change of form is a specification change. A
deployment without an id keeps working and sends the product-only label. The id is opaque, so
rotating it changes only how a deployment is recognised, never what the server returns.
