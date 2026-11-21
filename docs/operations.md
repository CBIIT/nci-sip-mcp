# Operations and authorization evidence

The NCI Semantic Infrastructure Program MCP server is built to production standards and is likely
to become the production codebase. This is an operator handoff and a source of inputs to
an authorization to operate (ATO), not an ATO, a compliance determination or evidence of
an approved deployment. Cloud One/AWS integration remains [issue #292](https://github.com/CBIIT/nci-si-mcp/issues/292).
See [deployment](deployment.md), [HTTP transport](transport.md) and
[governed HTTP](governed-http.md) for the serving boundaries.

## Available signals

These bounded inventory rows are checked against the owning runtime interfaces in
`tests/test_docs.py`. The second column is JSON: HTTP status and body, or field/code names.
The `:unavailable` suffix identifies a response case, not another HTTP path. Fields listed
for events are event-specific; `audit.emit` also adds `timestamp`, `level`, `event` and
`correlationId` to structured records.

| Interface | Contract | Meaning and owner |
| --- | --- | --- |
| `/health` | `[200, {"status": "ok"}]` | HTTP process response; `transport.create_http_app`. No dependency check. |
| `/ready` | `[200, {"status": "ready"}]` | Local index is usable, or absent when optional; `transport._readiness_error`. |
| `/ready:unavailable` | `[503, {"status": "not_ready"}]` | Required index absent, or index storage/compatibility/capability failure. |
| `http_not_ready` | `["errorType"]` | Warning only on transition into not-ready, not on every probe; `transport.create_http_app`. |
| `call_completed` | `["elapsedMs", "errorType", "outboundRequests", "parameters", "release", "responseCode", "resultSize", "status", "target", "tool", "truncation"]` | One completion per audited call, including validation failures; `audit.Audit.finish`. |
| `ErrorCode` | `["bound_exceeded", "timeout", "upstream_unavailable"]` | Selected caller error codes for alert derivation; `errors.ErrorCode`. |
| `ncit://index/manifest/{release}` | `["builtAt", "concepts", "embedding", "provenance", "terminology", "version"]` | Active index manifest for the requested release; `registry.SPECS`, `models.IndexManifest.to_result`. |

Readiness checks the **local index, not upstream availability**. With an optional index,
absence permits live tools; an existing unusable index still makes readiness fail.
Health remains responsive while readiness waits for storage. Host/origin restrictions
cover both paths. Deployment probes must use the approved boundary and access policy.

Completion `status` is `ok` or `error`; `responseCode` is `ok`, a caller error code, or
`internal_error` for an unexpected failure. `errorType` can be null; exception text is not
logged. `elapsedMs` is call duration, `outboundRequests` counts actual completed HTTP
attempts (including retries), and `resultSize` is serialized response bytes or null on an
unexpected failure. `truncation` carries the returned truncation record or null; a bounded
successful result is not automatically an error. The manifest build time supports derived
index age; it does not measure upstream release currency or install a freshness alarm.

Existing behavior owners remain `tests/test_transport.py` (health, required/optional index,
storage responsiveness, transition warnings), `tests/test_audit.py` (completion, redaction,
latency, bytes, retries and truncation), `tests/test_server.py` (release-bound resources),
and `tests/test_index_builds.py` (public manifest). The documentation contract adds no
replacement runtime health or audit suite.

## Operational settings

Selected operational knobs only; [QUICKSTART](../QUICKSTART.md#settings) is the full inventory.
JSON defaults below are compared with `Settings.from_env` in an empty environment. Defaults
are implementation facts, not approved service objectives or deployment recommendations.
Every setting named on this page appears in this table.

| Setting | Default | Operational effect |
| --- | --- | --- |
| `NCI_SIP_LOG_LEVEL` | `"INFO"` | Diagnostic verbosity; required completion audit remains enabled. |
| `NCI_SIP_TIMEOUT_SECONDS` | `30.0` | General upstream request timeout. |
| `NCI_SIP_MATCH_TIMEOUT_SECONDS` | `45.0` | Match request timeout. |
| `NCI_SIP_EVS_MAX_ATTEMPTS` | `3` | Maximum EVS REST/FHIR attempts. |
| `NCI_SIP_EVS_RETRY_BACKOFF_SECONDS` | `0.25` | EVS REST/FHIR retry backoff base; the shared client caps the wait. |
| `NCI_SIP_EVS_MAX_RESPONSE_BYTES` | `10485760` | EVS REST/FHIR response size bound. |
| `NCI_SIP_HTTP_REQUIRE_INDEX` | `false` | Whether readiness requires an active local index. |
| `NCI_SIP_HTTP_MAX_REQUEST_BYTES` | `4194304` | HTTP request body size bound. |
| `NCI_SIP_HTTP_AUTH_MODE` | `"trusted-local"` | Local trust default; hosted identity integration must select an approved mode. |
| `NCI_SIP_HTTP_AUTH_FACTORY` | `null` | Installed provider integration, unset by default; required mode fails closed without one. |

caDSR and Shared SI use fixed policies: three attempts, 0.25-second backoff and a 10 MiB
response bound. The EVS-specific settings above do not change those clients.

## Deployment-derived alerts and service objectives

The server has no metrics endpoint, built-in log store or index freshness alarm. Available
records must be collected and aggregated outside the process. These are wiring requirements
for #292, not currently installed alarms or approved thresholds.

| Derived signal | Available input | Platform action and open decision |
| --- | --- | --- |
| Process/readiness loss | Health/readiness status and transition warning above | Configure probes, restart/routing policy and alert evaluation window. Readiness must not stand in for an upstream probe. |
| Upstream unavailable | Completion response code and outbound attempt count | Group by operation/release and distinguish service failures from deployment network failures; approve rate/window and escalation. |
| Timeouts | Completion timeout code and duration | Track frequency and duration distribution; approve latency objective and timeout alert window. |
| Bounds reached | Completion `responseCode == "bound_exceeded"` or `truncation.occurred == true` | Separate refused requests from successful partial results; approve which bounds warrant an alert. |
| Caller error rate | Completion status and response code | Separate invalid input, permissions, upstream and internal failures; approve denominator, exclusions, minimum traffic and threshold. |
| Index age | Manifest build time, release and deployment clock | Compute age externally, verify expected release separately; approve freshness policy, clock source and alarm threshold. |
| Collection loss | Collector delivery and storage acknowledgments | Platform monitors pipeline failure; stderr emission alone proves neither delivery nor retention. |

Uptime target, measurement boundary, maintenance exclusions, latency/error budgets, support
hours, on-call owner, escalation route and incident response times remain **open sponsor and
platform decisions**. No uptime percentage or alert threshold is approved here. Record the
approved values and deployment evidence before using them as operational commitments.

## Audit delivery and retention

The path is **process stderr → platform collector → approved platform log store**.
`config.configure_logging` and `audit.JsonFormatter` supply JSON on stderr; the server
does not ship, persist, rotate or expire these records itself. Free text and undeclared
parameters are hashed; credentials are redacted. Hashing permits correlation, not secrecy
for guessable inputs. Do not put credentials or raw upstream bodies in incident tickets.

| Stage | Available now | Cloud One/AWS platform obligation (#292) |
| --- | --- | --- |
| Process | Structured stderr and per-call correlation, required completion audit | Capture every replica's stderr without mixing protocol stdout; preserve record integrity and timestamps. |
| Collector | No collector installed by the server | Select/configure agent or runtime driver; secure transport, buffering, backpressure, delivery monitoring and loss response. |
| Approved store | No server log store or retention policy | Select destination and region, access roles, encryption, integrity protection, search/export, backup and deletion controls. |
| Retention and access | No approved duration | Obtain sponsor/platform retention and disposal approval; define incident/legal holds, access review and evidence retrieval procedure. |

Retain deployment evidence of collector configuration, successful delivery and retrieval,
access restrictions, pipeline-failure response and approved retention enforcement. Repository
tests prove emission and redaction contracts, not those hosting properties.

## NIST SP 800-53 evidence inputs

Family references identify relevant implementation evidence only. Control selection,
tailoring, assessment and authorization belong to the sponsor and authorizing organization.
The hosting platform must supply and assess the complementary controls; this table makes
no claim that a family or individual control is satisfied.

| Family | Implementation evidence | Existing test evidence | Platform obligation |
| --- | --- | --- | --- |
| AC — Access Control | `permissions.py` narrows the served surface; `transport.HTTPBoundary` enforces host/origin restrictions and protected no-store responses | `tests/test_permissions.py`; `tests/test_transport.py` auth/scope and host/origin refusal cases | Approve access policy, role mapping, least privilege, network ingress and access review. |
| AU — Audit and Accountability | `audit.py`, `config.configure_logging`: correlated completion records, parameter hashing and secret redaction | `tests/test_audit.py` completion, validation rejection, secret redaction and retry-accounting cases | Deliver records to the approved store; establish retention, integrity, review and loss detection. |
| IA — Identification and Authentication | `http_auth.py` loads the configured provider; required mode fails closed; secured sessions bind the principal | `tests/test_http_access.py`; `tests/test_permissions.py` principal binding; `tests/test_transport.py` auth/scope refusals | Supply approved issuer/verifier, credential lifecycle, key rotation and identity assurance. Local trust is not hosted identity assurance. |
| SC — System and Communications Protection | `transport.py` HTTP boundary; `http_client.py` origin-bound credentials and redirect policy | `tests/test_transport.py`; `tests/test_http_client.py`; `tests/test_http_content.py` credential/redirect cases | Terminate approved TLS, isolate networks and storage, manage certificates and encryption keys. |
| SI — System and Information Integrity | `upstream.parse_upstream_json` rejects masked errors; shared HTTP bounds; local index verification | `tests/test_upstream.py`; `tests/test_http_client.py`; `tests/test_transport.py` readiness cases | Monitor vulnerabilities, upstream drift, alert delivery and incident handling; patch deployed images and verify configuration. |
| CM — Configuration Management | `config.Settings` validates settings; tracked locks, CI gates and head-specific reviewer clearance | `tests/test_config.py`; `tests/test_release_config.py`; `tests/test_docs.py` literal governance and operations contract | Approve baselines, deployment changes and rollback; retain sponsor approvals and deployment inventory. |

## Change approval and handoff

The approval path and head-specific merge clearance are recorded in
[AGENTS](../AGENTS.md#how-work-is-done-issues-into-a-milestone-branch-one-reviewed-milestone-into-main) and
[CONTRIBUTING](../CONTRIBUTING.md#pull-requests-and-releases). This is a human governance
record, not a claim that GitHub automatically verifies sponsor approval. Specification
change control remains separate.

Before a hosted handoff, the sponsor/platform owners must resolve the service objectives,
alert thresholds and support ownership above, select the log pipeline and retention policy,
approve identity/TLS/network/storage configuration, and assemble assessment evidence for
the selected authorization boundary. The repository supplies implementation and offline
test evidence; deployment verification and authorization remain open responsibilities.
